"""A running display host hands over to a newly installed release without touching the native.

Real launcher + PTY loop with a synthetic native and private dependency assets. The
installed release is a symlink (`link -> rel-a`); an install moves it to `rel-b`, a
copy of the session-shell sources, so the new host has another real path.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import importlib.util
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import fcntl
import struct
import pty
import re
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('shell_succession_fixture', ROOT/'utilities/session_shell_pty.test.py')
fixture = importlib.util.module_from_spec(spec); sys.modules[spec.name] = fixture; spec.loader.exec_module(fixture)

NATIVE = r"""
import json,os,select,subprocess,socket,sys,time,tty
from pathlib import Path
harness=os.environ['SUCC_HARNESS'];sid=sys.argv[1]
Path(os.environ['SUCC_PID']).write_text(json.dumps({'pid':os.getpid()}))
def event(kind):
    raw=open('/proc/self/stat').read(); start=raw.rsplit(') ',1)[1].split()[19]
    request={'op':'event','event':kind,'harness':harness,'sid':sid,'native_model':'NATIVE_SELECTED_MODEL',
        'pid':os.getpid(),'start':start,'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    registry=Path(os.environ['CLAUDE_CONFIG_DIR'])/'sessions'/str(os.getpid())
    registry=registry.with_suffix('.json');registry.parent.mkdir(parents=True,exist_ok=True)
    registry.write_text(json.dumps({'pid':os.getpid(),'procStart':start,'sessionId':sid,'status':'busy' if kind=='prompt' else 'idle'}))
    for attempt in range(40):
        try:
            with socket.socket(socket.AF_UNIX) as channel:
                channel.settimeout(5);channel.connect(os.environ['HEARTING_SHELL_SOCKET'])
                channel.sendall((json.dumps(request)+'\n').encode());channel.recv(8192)
            return
        except OSError:time.sleep(.05)
def composer():
    if harness=='codex':return '\x1b[2m›\x1b[0m \r\n\r\n'
    if harness=='opencode':return '┃\r\n┃  \r\n┃\r\n'
    return '❯ \r\n'+'─'*18+'\r\n'
tty.setraw(0)
exit_fd=os.open(os.environ['LOOP_NATIVE_EXIT'],os.O_RDWR|os.O_NONBLOCK)
# OpenCode's plugin reports a selected session as select + idle; a window with no selected
# session (SUCC_NO_SELECT) reports nothing, like OpenCode on its home route.
bound=os.environ.get('SUCC_NO_SELECT')!='1'
if bound:
    event('select' if harness=='opencode' else 'start')
    if harness=='opencode':event('idle')
def reload(signum,frame):
    # OpenCode reloads its plugins when an install rewrites its config: the old plugin reports
    # deselect, the new one select (and the session's state), with no key from a person.
    event('deselect');event('select');event('idle')
import signal
signal.signal(signal.SIGUSR1,reload)
print('\x1b[?2004hREADY\r\n'+composer(),end='',flush=True)
pending=''
while True:
    readable,_,_=select.select([0,exit_fd],[],[])
    if exit_fd in readable:break
    data=os.read(0,65536)
    if not data or data==b'\x03':break
    with open(os.environ['SUCC_READ'],'ab') as f:f.write(data)
    pending+=data.decode()
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        line=line.replace('\x1b[200~','').replace('\x1b[201~','')
        if line=='bind' and not bound:
            bound=True;event('select')    # the prompt typed on the home route opened a session
        if bound:event('submit' if harness=='opencode' else 'prompt')
        if line=='slow':time.sleep(2.0)
        if bound:event('stop')
        if line=='flood':
            print(''.join('FLOOD_%03d \x1b[3%dmcolor\x1b[0m wide 한글\r\n'%(i,i%8) for i in range(120)),end='',flush=True)
        else:print('\x1b[2J\x1b[HDONE_'+line+'\r\n'+composer()+pending,end='',flush=True)
        if line=='form':print('Do you want to proceed?\r\n1. Yes\r\n2. No\r\n',end='',flush=True)
        if line=='alt':print('\x1b[?1049h\x1b[2J\x1b[HALT_SCREEN\r\n'+composer()+'\x1b[1;5r\x1b[3;1H',end='',flush=True)
        if line=='leave':print('\x1b[?1049l',end='',flush=True)
"""

# Failure injection, selected by files in SUCC_DIR (the survival test's approach): a successor
# fails at a chosen step; an old host is made unable to write one part of its screen out.
PRELOAD = r"""
const Module=require('node:module'),fs=require('node:fs'),load=Module._load,dir=process.env.SUCC_DIR;
if(process.argv.some(arg=>arg.endsWith('/session-shell/host.cjs'))){
 const mode=()=>{try{return fs.readFileSync(dir+'/fault','utf8').trim();}catch{return '';}};
 const from=name=>new Error().stack.includes(name);
 if(process.env.HEARTING_SHELL_SUCCESSOR==='1'){
  fs.appendFileSync(dir+'/spawns',process.pid+' '+process.env.HEARTING_SHELL_GENERATION+'\n');
  const m=mode();
  if(m==='load-throw'){fs.writeSync(2,'at Module._load (node:internal/modules/cjs/loader)\r\nNode.js v24 injected load failure\r\n');throw Error('injected load failure');}
  if(m==='bypass'){fs.writeSync(Number(process.env.HEARTING_SHELL_START_FD),'BYPASS\n');process.exit(0);}
  if(m==='commit-throw'){const read=fs.readFileSync;fs.readFileSync=function(f,...a){if(String(f).includes('succession-'))throw Error('injected');return read.call(this,f,...a);};}
  if(m.split(',').includes('exit-after-switch')){const rename=fs.renameSync;fs.renameSync=function(a,b){const r=rename.call(this,a,b);if(String(b).endsWith('control.sock'))setImmediate(()=>process.exit(3));return r;};}
  // Hold the successor before it takes the control path, for as long as the `hold` file exists (at most 3 s).
  if(m==='hold-switch'){const rename=fs.renameSync;fs.renameSync=function(a,b){
   if(String(b).endsWith('control.sock')){fs.writeFileSync(dir+'/holding','1');const cell=new Int32Array(new SharedArrayBuffer(4));
    for(let i=0;i<150&&fs.existsSync(dir+'/hold');i++)Atomics.wait(cell,0,0,20);}
   return rename.call(this,a,b);};}
 }else{
  // The old host cannot listen again on the side path while it rolls back (the mode is read when it listens).
  const net=require('node:net'),listen=net.Server.prototype.listen;
  net.Server.prototype.listen=function(target,...rest){
   if(typeof target==='string'&&target.endsWith('.next')&&mode().split(',').includes('listen-error')){setImmediate(()=>this.emit('error',Object.assign(new Error('injected listen'),{code:'EACCES'})));return this;}
   return listen.call(this,target,...rest);};
  // The old host loses its display just after its seed went out (an uncaught exception while frozen).
  const renameSeed=fs.renameSync;
  fs.renameSync=function(a,b){const r=renameSeed.call(this,a,b);
   if(/succession-\d+\.json$/.test(String(b))&&mode().split(',').includes('old-throw-after-seed'))setImmediate(()=>{throw Error('injected old host failure');});return r;};
 }
 const rename=fs.renameSync;
 fs.renameSync=function(a,b){const r=rename.call(this,a,b);
  if(/succession-\d+\.json$/.test(String(b)))fs.copyFileSync(b,dir+'/seed-'+process.pid+'-'+String(b).match(/succession-(\d+)/)[1]+'.json');return r;};
 Module._load=function(name,...args){const value=load.call(this,name,...args);
  if(name==='@xterm/headless')return {...value,Terminal:new Proxy(value.Terminal,{construct(Base,args){
   const terminal=Reflect.construct(Base,args);
   const proto=Object.getPrototypeOf(terminal.buffer.normal);
   if(!proto.__patched){proto.__patched=true;const get=proto.getLine;
    proto.getLine=function(i){if(mode()==='scrollback-throw'&&from('succession.cjs')&&i<this.baseY)throw Error('injected scrollback');return get.call(this,i);};}
   const normal=terminal._core.buffers.normal;let top=normal.scrollTop;
   Object.defineProperty(normal,'scrollTop',{configurable:true,get(){return mode()==='region-missing'&&from('succession.cjs')?undefined:top;},set(v){top=v;}});
   return terminal;}})};
  return value;};
}
"""

VT = r"""
const fs=require('node:fs'),{Terminal}=require(process.env.SUCC_XTERM),crypto=require('node:crypto');
const [raw,cols,rows]=[fs.readFileSync(process.argv[2]),Number(process.argv[3]),Number(process.argv[4])];
const t=new Terminal({cols,rows,allowProposedApi:true,scrollback:1000});
t.write(raw,()=>{
 const b=t.buffer.active,lines=[];
 for(let y=0;y<rows;y++)lines.push(b.getLine(b.viewportY+y).translateToString(true));
 console.log(JSON.stringify({lines,cursor:[b.cursorX,b.cursorY],type:b.type,digest:crypto.createHash('sha256').update(JSON.stringify(lines)).digest('hex')}));process.exit(0);
});
"""

UNIT = r"""
const S=require(process.argv[2]),{Terminal}=require(process.env.SUCC_XTERM),seed=JSON.parse(require('node:fs').readFileSync(process.argv[3],'utf8'));
const t=new Terminal({cols:seed.terminal.cols,rows:seed.terminal.rows,allowProposedApi:true,scrollback:1000});
const bad=S.validate(seed);
if(bad){console.log(JSON.stringify({invalid:bad}));process.exit(0);}
S.restore(t,seed.terminal).then(()=>{const g=S.grid(t),back=S.capture(t);
 console.log(JSON.stringify({invalid:null,digest:g.digest,scrollback:g.scrollback,same:JSON.stringify(back.terminal)===JSON.stringify(seed.terminal)}));process.exit(0);});
"""


def process_row(pid):
    try:
        fields = (Path('/proc')/str(pid)/'stat').read_text().rsplit(') ', 1)[1].split()
        return {'pid': pid, 'start': fields[19], 'live': fields[0] not in ('Z', 'X')}
    except (FileNotFoundError, ProcessLookupError):
        return {'pid': pid, 'live': False}


def rpc(path, request, timeout=5):
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(timeout); channel.connect(path)
        channel.sendall((json.dumps(request)+'\n').encode())
        data = b''
        while b'\n' not in data:
            chunk = channel.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.split(b'\n')[0])


class Window:
    """One launched window: an outer PTY, the entry launcher and its synthetic native."""
    count = 0

    def __init__(self, case, harness, *, sid, extra=None, gate=False):
        self.case, self.harness, self.sid = case, harness, sid
        Window.count += 1
        self.root = case.root/('win-%d-%s' % (Window.count, harness))
        self.root.mkdir(mode=0o700)
        (self.root/'seeds').mkdir()
        self.raw = bytearray()
        self.releases = {}
        for name in ('rel-a', 'rel-b', 'rel-c'):
            utilities = self.root/name/'utilities'
            (utilities/'session-shell').mkdir(parents=True)
            for source in (ROOT/'utilities/session-shell').iterdir():
                if source.suffix in ('.cjs', '.py', '.json'):
                    shutil.copy2(source, utilities/'session-shell'/source.name)
            for source in (ROOT/'utilities').iterdir():
                if source.suffix == '.py' and not (utilities/source.name).exists():
                    (utilities/source.name).symlink_to(source)
            (self.root/name/'tools').symlink_to(ROOT/'tools')
            # Another real path for each release; the code under it is the same.
            (utilities/'session-shell'/'RELEASE').write_text(name)
            self.releases[name] = self.root/name
        self.link = self.root/'link'
        self.point('rel-a')
        native = self.root/'native.py'; native.write_text(NATIVE)
        (self.root/'fault.cjs').write_text(PRELOAD)
        (self.root/'vt.cjs').write_text(VT); (self.root/'unit.cjs').write_text(UNIT)
        self.exit_fifo = self.root/'exit'; os.mkfifo(self.exit_fifo, 0o600)
        env = dict(case.env, LOOP_NATIVE_EXIT=str(self.exit_fifo), SUCC_HARNESS=harness, SUCC_DIR=str(self.root),
                   SUCC_PID=str(self.root/'native.json'), SUCC_READ=str(self.root/'native-read'),
                   SUCC_XTERM=str(Path(case.env['NODE_PATH'])/'@xterm/headless'),
                   TERM='xterm-256color', NODE_OPTIONS='--require='+str(self.root/'fault.cjs'), **(extra or {}))
        policy = Path(env.get('XDG_CONFIG_HOME') or Path(env['HOME'])/'.config')/'hearting/session-shell.json'
        policy.parent.mkdir(parents=True, exist_ok=True); policy.write_text('{"enabled":true}'); policy.chmod(0o600)
        cache = Path(env['XDG_STATE_HOME'])/'hearting/human-waits/projection.json'
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time(); key = harness+'\0'+sid
        cache.write_text(json.dumps({'schema_version': 3, 'observed_at': now, 'items': [], 'by_session': {}, 'total': 0,
                                     'older': 0, 'complete': True, 'route_status': {key: ''}, 'route_sessions': {key: now}}))
        cache.chmod(0o600)
        self.registry = case.registry/harness
        shutil.rmtree(self.registry, ignore_errors=True); self.registry.mkdir(parents=True)
        entry = str(self.link/'utilities/session-shell/entry.py')
        self.gate, self.ready = self.root/'enter-gate', self.root/'enter-ready'
        if gate:
            # Hold the owner at its Enter boundary so a delivery stays in flight (the survival test's gate).
            os.mkfifo(self.gate, 0o600); os.mkfifo(self.ready, 0o600)
            self.ready_fd = os.open(self.ready, os.O_RDONLY | os.O_NONBLOCK)
            bootstrap = self.root/'entry-gate.py'
            bootstrap.write_text("import os,runpy,sys,time\nsys.path.insert(0,"+repr(str(self.link/'utilities/session-shell'))+")\nimport input_owner\n"
                "advance=input_owner.InputOwner.advance\n"
                "def gated(self,now):\n"
                " if self.ref is not None and self.deadline is not None and now>=self.deadline:\n"
                "  self.send()\n"
                "  if self.responses:self.capture();return\n"
                "  ready=os.open("+repr(str(self.ready))+",os.O_WRONLY);os.write(ready,b'1');os.close(ready)\n"
                "  gate=os.open("+repr(str(self.gate))+",os.O_RDWR);os.read(gate,1);os.close(gate)\n"
                " return advance(self,time.monotonic())\n"
                "input_owner.InputOwner.advance=gated\n"
                "sys.argv=["+repr(entry)+",*sys.argv[1:]]\nrunpy.run_path(sys.argv[0],run_name='__main__')\n")
            entry = str(bootstrap)
        self.master, slave = pty.openpty()
        self.attrs = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 130, 0, 0))
        self.entry = subprocess.Popen(
            [sys.executable, entry, harness, sys.executable, str(native), sid],
            env=env, cwd=case.project, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        self.until(b'READY')

    def point(self, name):
        temporary = self.root/'link.new'
        temporary.unlink(missing_ok=True); temporary.symlink_to(self.releases[name]); os.replace(temporary, self.link)

    def pump(self, seconds=.05):
        end = time.monotonic()+seconds
        while True:
            left = end-time.monotonic()
            if left <= 0:
                return
            if select.select([self.master], [], [], left)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.raw.extend(data)

    def until(self, needle, timeout=8):
        if isinstance(needle, str):
            needle = needle.encode()
        end = time.monotonic()+timeout
        while needle not in self.raw and time.monotonic() < end:
            self.pump(.1)
        self.case.assertIn(needle, self.raw, repr(bytes(self.raw[-800:])))

    def type(self, text):
        os.write(self.master, text if isinstance(text, bytes) else text.encode())

    def binding(self):
        rows = []
        for file in self.registry.glob('session-shell-*.json'):
            try:
                row = json.loads(file.read_text())
            except ValueError:
                continue
            if process_row(row['host_pid'])['live']:
                rows.append(row)
        return rows[0] if len(rows) == 1 else None

    def wait_binding(self, timeout=10, other_than=None):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            row = self.binding()
            if row and row['host_pid'] != other_than:
                return row
            self.pump(.05)
        self.case.fail('binding unavailable '+repr(self.binding()))

    def status(self, row=None):
        row = row or self.binding()
        return rpc(row['socket'], {**row, 'op': 'status'})

    def wait_empty(self, timeout=10):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            try:
                status = self.status()
            except (OSError, ValueError, TypeError, KeyError):
                status = {}
            if status.get('empty'):
                return status
            self.pump(.05)
        self.case.fail('composer never read as empty')

    def native_pid(self):
        return json.loads((self.root/'native.json').read_text())['pid']

    def screen(self, upto=None):
        data = bytes(self.raw[:upto]) if upto is not None else bytes(self.raw)
        file = self.root/'outer.raw'; file.write_bytes(data)
        env = dict(self.case.env, SUCC_XTERM=str(Path(self.case.env['NODE_PATH'])/'@xterm/headless'))
        out = subprocess.run([self.case.node, str(self.root/'vt.cjs'), str(file), '130', '30'], env=env,
                             capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def seeds(self):
        return sorted((self.root).glob('seed-*.json'))

    def spawns(self):
        file = self.root/'spawns'
        return file.read_text().split('\n')[:-1] if file.exists() else []

    def fault(self, mode):
        (self.root/'fault').write_text(mode)

    def open_gate(self):
        fd = os.open(self.gate, os.O_RDWR | os.O_NONBLOCK); os.write(fd, b'1'); os.close(fd)

    def close(self):
        if self.gate.exists():
            self.open_gate()
        try:
            fd = os.open(self.exit_fifo, os.O_RDWR | os.O_NONBLOCK); os.write(fd, b'q'); os.close(fd)
        except OSError:
            pass
        try:
            self.entry.wait(timeout=8)
        except subprocess.TimeoutExpired:
            for pid in self.tree():
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            self.entry.wait(timeout=5)
        try:
            os.close(self.master)
        except OSError:
            pass

    def env_for_shell(self):
        return {key: value for key, value in self.case.env.items()}

    def tree(self):
        out = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    if str(self.root).encode() in (entry/'cmdline').read_bytes() or Path(os.readlink(entry/'cwd')) == self.root:
                        out.append(int(entry.name))
                except OSError:
                    pass
        return out


class SessionShellSuccessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.SessionShellPTYTest.setUpClass.__func__(cls)
        # Registered after the fixture's own cleanup, so it runs before the state root is removed.
        cls.addClassCleanup(cls.reap_refreshers)

    @classmethod
    def reap_refreshers(cls):
        """The panel backend detaches a cache refresher (own session) that outlives its window;
        end the ones that belong to this fixture's state root, so nothing is left for the runner."""
        mine = ('XDG_STATE_HOME='+cls.env['XDG_STATE_HOME']).encode()
        found = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    if b'human_wait_state.py' in (entry/'cmdline').read_bytes() and mine in (entry/'environ').read_bytes().split(b'\0'):
                        found.append(int(entry.name))
                except OSError:
                    pass
        for pid in found:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        end = time.monotonic()+5
        while any(process_row(pid)['live'] for pid in found) and time.monotonic() < end:
            time.sleep(.05)

    def setUp(self):
        self.windows = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for window in self.windows:
            window.close()

    def window(self, harness='claude', **kwargs):  # noqa: D102
        window = Window(self, harness, sid='succession-'+harness+'-'+str(len(self.windows)), **kwargs)
        self.windows.append(window)
        return window

    # ---------------------------------------------------------------- helpers
    def switched(self, window, old_host, timeout=20):
        return window.wait_binding(timeout, other_than=old_host)

    def assert_keeps_native(self, window, native, host_before=None):
        self.assertEqual(window.native_pid(), native['pid'])
        self.assertEqual(process_row(native['pid']), native)
        window.type('after\r'); window.until('DONE_after')
        self.assertIn(b'after\r', (window.root/'native-read').read_bytes())

    @staticmethod
    def notice_rows(before, after, pattern):
        """True when the screen is the one it was, except for the panel's one notice line.

        The panel (spec rule 34 (c)) draws its thin rule above the session's content block (a tidy
        notice, which this line takes the slot of, is content), so the rule above the notice
        turns up beside it: that rule is the panel's section divider, not a second line of notice.
        Any other row changing, or a rule that is not directly above the notice, is a failure.
        """
        differing = [index for index, (a, b) in enumerate(zip(after, before)) if a != b]
        if not differing or not re.search(pattern, after[differing[-1]]):
            return False
        if len(differing) == 1:
            return True
        divider = after[differing[0]].split('│')[1].strip() if '│' in after[differing[0]] else ''
        return len(differing) == 2 and differing[1] == differing[0]+1 and bool(divider) and set(divider) == {'─'}

    def only_line_differs(self, window, before, pattern):
        """The screen is the one it was, except for one panel notice line (and the rule above it)."""
        after = window.screen()['lines']
        self.assertTrue(self.notice_rows(before['lines'], after, pattern),
                        [(i, before['lines'][i], after[i]) for i in range(len(after)) if after[i] != before['lines'][i]])

    def until_read(self, window, needle, timeout=5):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            data = (window.root/'native-read').read_bytes() if (window.root/'native-read').exists() else b''
            if needle in data:
                return
            window.pump(.05)
        self.fail('native never read '+repr(needle))

    def settle(self, window, command='warm'):
        window.type(command+'\r'); window.until('DONE_'+command)
        return window.wait_empty()

    # ----------------------------------------------------------------- S1
    def test_idle_window_moves_to_the_new_release_with_the_same_native(self):
        for harness in ('claude', 'codex', 'opencode'):
            with self.subTest(harness=harness):
                window = self.window(harness)
                first = window.wait_binding()
                self.settle(window)
                native = process_row(window.native_pid())
                before_raw = len(window.raw); window.pump(.5); before_raw = len(window.raw)
                before = window.screen()
                window.point('rel-b')
                second = self.switched(window, first['host_pid'])
                window.pump(.6)
                self.assertNotEqual(second['host_pid'], first['host_pid'])
                self.assertEqual(second['native_pid'], first['native_pid'])
                self.assertFalse(process_row(first['host_pid'])['live'])
                after = window.screen()
                self.assertEqual(after['lines'], before['lines'])
                self.assertEqual(after['cursor'], before['cursor'])
                self.assertNotIn(b'\x1b[?1049l', bytes(window.raw[before_raw:]))
                self.assertNotIn(b'\x1b[?1049h', bytes(window.raw[before_raw:]))
                self.assert_keeps_native(window, native)
                self.assertEqual(len(window.spawns()), 1)

    def wait_empty_then_switch(self, window, first):
        window.wait_empty()     # a silent native gives the host no repaint to re-read the composer on
        self.switched(window, first['host_pid'])

    def test_an_opencode_window_with_no_bound_session_waits_for_one(self):
        # OpenCode on its home route (or after a deselect) reports no session: that is no evidence
        # of an idle, empty composer, so the window stays as it is (the panel has no session to
        # project a waiting line for) and switches only once a session is selected and idle.
        window = self.window('opencode', extra={'SUCC_NO_SELECT': '1'})
        first = window.wait_binding()
        native = process_row(window.native_pid())
        window.point('rel-b'); window.pump(3.0)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        self.assertEqual(window.spawns(), [])
        window.type('bind\r'); window.until('DONE_bind')
        self.wait_empty_then_switch(window, first)
        self.assert_keeps_native(window, native)

    def test_an_opencode_plugin_reload_is_not_new_input_but_a_typed_draft_still_blocks(self):
        # A reload reports deselect then select of the same session. The submitted frontier survives
        # it, so an empty composer is proven again and the window switches; a draft typed before it
        # keeps the window where it is.
        window = self.window('opencode')
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        os.kill(native['pid'], signal.SIGUSR1); window.pump(1.0)
        window.wait_empty()
        window.point('rel-b')
        self.switched(window, first['host_pid'])
        self.assert_keeps_native(window, native)
        drafted = self.window('opencode')
        held = drafted.wait_binding(); self.settle(drafted)
        drafted.type('draft'); self.until_read(drafted, b'draft')
        os.kill(process_row(drafted.native_pid())['pid'], signal.SIGUSR1); drafted.pump(1.0)
        drafted.point('rel-b'); drafted.pump(3.0)
        self.assertEqual(drafted.binding()['host_pid'], held['host_pid'])
        self.assertEqual(drafted.spawns(), [])

    # ---------------------------------------------------------------- S1b
    def test_scrollback_and_modes_survive_two_hand_overs_unchanged(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        window.type('flood\r'); window.until('FLOOD_119')
        window.type('x\r'); window.until('DONE_x'); window.wait_empty()
        window.type('flood\r'); window.until('FLOOD_119')
        # DONE_x clears the screen; one more flood leaves rows above the viewport.
        window.type('y\r'); window.until('DONE_y'); window.wait_empty()
        window.point('rel-b'); second = self.switched(window, first['host_pid'])
        window.wait_empty(); window.point('rel-c'); self.switched(window, second['host_pid'])
        seeds = window.seeds()
        self.assertEqual(len(seeds), 2, seeds)
        one, two = (json.loads(path.read_text()) for path in seeds)
        self.assertTrue(one['scrollback']['carried'], one['scrollback'])
        self.assertGreaterEqual(one['scrollback']['lines'], 60)
        # The second seed was captured from the VT the first one rebuilt.
        self.assertEqual(one['terminal'], two['terminal'])
        # The host's own turn observation crosses with it: the second host starts from the marks the first
        # one carried (they only grow), so a switch shows the state it showed and orders its readings after.
        for seed in (one, two):
            self.assertEqual(set(seed['turn']), {'nativeTurn', 'turnMark', 'eventMark', 'clock', 'seenTurn', 'turnRecord', 'formSeen'})
        self.assertGreaterEqual(two['turn']['clock'], one['turn']['clock'])
        self.assertGreaterEqual(two['turn']['eventMark'], one['turn']['eventMark'])
        env = dict(self.env, SUCC_XTERM=str(Path(self.env['NODE_PATH'])/'@xterm/headless'))
        out = subprocess.run([self.node, str(window.root/'unit.cjs'), str(window.link/'utilities/session-shell/succession.cjs'), str(seeds[0])],
                             env=env, capture_output=True, text=True, timeout=30, check=True)
        unit = json.loads(out.stdout)
        self.assertEqual(unit['invalid'], None); self.assertTrue(unit['same'])
        self.assertEqual(unit['scrollback'], one['scrollback']['lines'])

    def test_alternate_screen_and_scroll_region_are_carried(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        window.type('alt\r'); window.until('ALT_SCREEN')
        window.point('rel-b'); window.pump(.5)
        # The native draws on its alternate screen; the host must still take the hand-over at idle.
        self.switched(window, first['host_pid'])
        seeds = window.seeds(); self.assertEqual(len(seeds), 1)
        seed = json.loads(seeds[0].read_text())
        self.assertEqual(seed['terminal']['active'], 'alternate')
        self.assertEqual(seed['terminal']['scrollRegion'], {'top': 0, 'bottom': 4})

    # ---------------------------------------------------------------- S2-S4
    def test_a_draft_defers_the_hand_over_and_the_enter_after_it_releases(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        window.type('draft'); self.until_read(window, b'draft')
        native = process_row(window.native_pid())
        window.point('rel-b'); window.pump(2.0)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        self.assertRegex(bytes(window.raw).decode(errors='replace'), r'New release waiting|새 판 대기')
        self.assertEqual(window.spawns(), [])
        window.type('\r'); window.until('DONE_draft')
        self.switched(window, first['host_pid'])  # no other event: the stop of that prompt releases it
        self.assertEqual((window.root/'native-read').read_bytes().count(b'draft\r'), 1)
        self.assert_keeps_native(window, native)

    def test_an_open_form_defers_the_hand_over_until_it_is_gone(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        window.type('form\r'); window.until('Do you want to proceed')
        native = process_row(window.native_pid())
        window.point('rel-b'); window.pump(2.0)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        self.assertEqual(window.spawns(), [])
        window.type('ok\r'); window.until('DONE_ok')
        self.switched(window, first['host_pid'])
        self.assert_keeps_native(window, native)

    def test_a_running_turn_defers_the_hand_over_until_it_stops(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        window.type('slow\r'); window.pump(.5)
        window.point('rel-b'); window.pump(1.0)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        self.assertEqual(window.spawns(), [])
        window.until('DONE_slow', 6)
        self.switched(window, first['host_pid'])
        self.assert_keeps_native(window, native)

    def test_an_open_panel_focus_defers_the_hand_over(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        window.type(b'\x1d'); window.pump(.3)
        self.assertTrue(window.status()['focused'])
        window.point('rel-b'); window.pump(2.0)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        window.type(b'\x1d'); window.pump(.3)
        self.switched(window, first['host_pid'])

    def open_read_page(self, window):
        cache = Path(self.env['XDG_STATE_HOME'])/'hearting/human-waits/projection.json'
        value = json.loads(cache.read_text()); key = window.harness+'\0'+window.sid
        value['observed_at'] = time.time()
        # The session's own route demand is fresh too, as the PTY tests seed it: a stale or missing
        # one wakes the producer, whose next write (no such route) would take the seeded row away.
        value['route_sessions'] = {**(value.get('route_sessions') if isinstance(value.get('route_sessions'), dict) else {}),
                                   key: time.time()}
        value['route_status'] = {**(value.get('route_status') if isinstance(value.get('route_status'), dict) else {}),
                                 key: 'route 1 · ROUTE_PAGE'}
        value['route_panels'] = {key: [{'id': 'rt-detail', 'title': 'ROUTE_PAGE', 'state': 'working', 'worker_stage': 'inline',
            'stages': [{'name': 'inline', 'st': 'active'}], 'harness': 'codex', 'model': 'gpt-fixture', 'effort': 'high',
            'ctx_pct': 40, 'agents': 2, 'visibility': {'codes': {'schema': 'route_visibility_codes_v1', 'shape': 'direct',
                'next': {'kind': 'closed'}, 'verification': {'gate': 'unproven'},
                'quoted': [['test', 'EVIDENCE_%d %s' % (index, 'bounded detail '*5)] for index in range(8)]}}}]}
        cache.with_suffix('.new').write_text(json.dumps(value)); cache.with_suffix('.new').chmod(0o600)
        os.replace(cache.with_suffix('.new'), cache)
        window.until('ROUTE_PAGE'); window.pump(.5)
        point = None
        for y, line in enumerate(window.screen()['lines']):
            if '╭─ 1 ROUTE' in line:
                point = (line.index('╭─ 1')+4, y+1)
        self.assertIsNotNone(point, window.screen()['lines'])
        window.type('\x1b[<0;%d;%dM\x1b[<0;%d;%dm' % (point[0], point[1], point[0], point[1])); window.until('PgUp/PgDn')

    def test_an_open_read_page_defers_the_hand_over_until_it_is_closed(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        self.open_read_page(window)
        native = process_row(window.native_pid())
        window.point('rel-b'); window.pump(2.0)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        self.assertEqual(window.spawns(), [])
        # The page has no notice slot: its head says a new release waits, one row under the page counter.
        wait = re.compile(r'New release waiting|새 판 대기')
        end = time.monotonic()+8
        while time.monotonic() < end and not any(wait.search(row) for row in window.screen()['lines']):
            window.pump(.3)
        rows = window.screen()['lines']
        counter = [index for index, row in enumerate(rows) if 'PgUp/PgDn' in row]
        self.assertEqual([index for index, row in enumerate(rows) if wait.search(row)], [counter[0]+1], rows)
        window.type(b'\x1b')  # closes the page; nothing else happens
        self.switched(window, first['host_pid'])
        self.assert_keeps_native(window, native)

    def test_an_opencode_plugin_reload_keeps_an_open_read_page_and_the_hand_over_waits_for_its_close(self):
        window = self.window('opencode')
        first = window.wait_binding(); self.settle(window)
        self.open_read_page(window)
        native = process_row(window.native_pid())
        os.kill(native['pid'], signal.SIGUSR1); window.pump(1.0)
        window.until('PgUp/PgDn')
        window.point('rel-b'); window.pump(2.5)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        self.assertEqual(window.spawns(), [])
        window.type(b'\x1b')
        self.wait_empty_then_switch(window, first)
        self.assert_keeps_native(window, native)

    def test_a_delivery_in_flight_defers_the_hand_over_and_is_delivered_once(self):
        from unittest import mock
        import session_shell as shell
        window = self.window(gate=True)
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        with mock.patch.dict(os.environ, window.env_for_shell(), clear=True):
            peer = shell._peer_message()
            text, ref = peer.prepare_peer_message('PEER_DELIVERY', {'harness': 'codex', 'session_id': 'synthetic-sender'},
                                                  {'harness': 'claude', 'session_id': window.sid}, defer=True)
            accepted = shell.deliver_pending(ref, 'claude', window.sid)
            self.assertTrue(accepted.get('ok'), accepted)
            self.assertTrue(select.select([window.ready_fd], [], [], 5)[0], 'owned Enter boundary not ready')
            window.point('rel-b')
            time.sleep(1.5)  # the owner is held at the Enter boundary: this delivery is in flight
            self.assertEqual(json.loads(next(window.registry.glob('session-shell-*.json')).read_text())['host_pid'], first['host_pid'])
            self.assertEqual(window.spawns(), [])
            window.open_gate()
            second = self.switched(window, first['host_pid'])
            window.pump(.5)
            binding, why = shell.exact_binding('claude', window.sid)
            self.assertEqual(binding['host_pid'], second['host_pid'], why)
        got = (window.root/'native-read').read_bytes()
        self.assertEqual(got.count(b'PEER_DELIVERY'), 1, got)
        self.assert_keeps_native(window, native)

    # ----------------------------------------------------------------- S7
    def test_a_failing_successor_leaves_the_old_host_and_the_native_untouched(self):
        for fault in ('load-throw', 'bypass', 'commit-throw', 'exit-after-switch'):
            with self.subTest(fault=fault):
                window = self.window()
                first = window.wait_binding(); self.settle(window)
                native = process_row(window.native_pid())
                window.fault(fault)
                window.pump(1.0)
                before = window.screen()
                window.point('rel-b')
                end = time.monotonic()+20
                while len(window.spawns()) < 3 and time.monotonic() < end:
                    window.pump(.1)
                window.pump(2.0)
                self.assertEqual(len(window.spawns()), 3, window.spawns())  # three tries, then no more
                row = window.wait_binding()
                self.assertEqual(row['host_pid'], first['host_pid'])
                self.assertTrue(process_row(first['host_pid'])['live'])
                self.only_line_differs(window, before, r'New release waiting|새 판 대기')
                # What a dying successor says never reaches the terminal the old host still owns.
                self.assertNotIn(b'injected load failure', bytes(window.raw))
                # Keys typed while a successor was dying still arrive, exactly once, in order.
                window.type('k1k2\r'); window.until('DONE_k1k2')
                self.assertEqual((window.root/'native-read').read_bytes().count(b'k1k2\r'), 1)
                self.assert_keeps_native(window, native)
                window.pump(1.0)
                self.assertEqual(len(window.spawns()), 3)

    def test_a_request_held_during_the_freeze_is_answered_by_the_new_host(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        window.fault('hold-switch'); (window.root/'hold').write_text('1')
        window.point('rel-b')
        end = time.monotonic()+15
        while not (window.root/'holding').exists() and time.monotonic() < end:
            window.pump(.05)
        self.assertTrue((window.root/'holding').exists(), 'successor never reached its switch')
        # The old host is frozen now: it holds this request instead of answering it.
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(8); channel.connect(first['socket'])
            channel.sendall((json.dumps({**first, 'op': 'status'})+'\n').encode())
            window.pump(.4)
            (window.root/'hold').unlink()
            data = b''
            end = time.monotonic()+10
            while b'\n' not in data and time.monotonic() < end:
                window.pump(.05)
                if select.select([channel], [], [], 0)[0]:
                    chunk = channel.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        second = self.switched(window, first['host_pid'])
        self.assertIn(b'\n', data, 'the held request was reset, not answered: '+repr(data))
        answer = json.loads(data.split(b'\n')[0])
        self.assertEqual(answer['host_pid'], second['host_pid'], answer)
        # The request names the old host, so the new one answers with its typed "attachment changed"
        # (the client re-reads the binding and asks again); what matters is it was answered, not reset.
        self.assertEqual(answer.get('reason'), 'attachment-changed', answer)
        self.assertEqual(window.status(second)['ok'], True)
        self.assertEqual(len(window.spawns()), 1)
        self.assert_keeps_native(window, native)

    def test_a_rollback_that_cannot_listen_again_falls_back_to_the_plain_relay_and_never_stays_frozen(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        window.fault('exit-after-switch,listen-error')
        window.point('rel-b')
        end = time.monotonic()+15
        while not window.spawns() and time.monotonic() < end:
            window.pump(.05)
        self.assertTrue(window.spawns())
        successor = int(window.spawns()[0].split()[0])
        # Nothing is typed until the successor has taken the control path and died: the old host is frozen and rolling back.
        while process_row(successor)['live'] and time.monotonic() < end:
            window.pump(.05)
        self.assertFalse(process_row(successor)['live'])
        window.pump(1.0)
        # The host that was frozen reads keys again: this arrives, once, at the same native.
        window.type('relay\r'); window.until('DONE_relay', 15)
        self.assertEqual((window.root/'native-read').read_bytes().count(b'relay\r'), 1)
        self.assertTrue(process_row(first['host_pid'])['live'])
        self.assertEqual(process_row(native['pid']), native)

    def test_an_old_host_that_loses_its_display_after_its_seed_drops_the_successor(self):
        """Review F1: the frozen old host falls to the plain relay after its seed went out. It tells the
        owner, so the successor is dropped before it reads: the PTY and the keys have one reader."""
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        window.fault('old-throw-after-seed')
        window.point('rel-b')
        end = time.monotonic()+15
        while not window.seeds() and time.monotonic() < end:
            window.pump(.05)
        self.assertTrue(window.seeds(), 'the old host never wrote its seed')
        successor = int(window.spawns()[0].split()[0])
        end = time.monotonic()+5
        while process_row(successor)['live'] and time.monotonic() < end:
            window.pump(.05)
        self.assertFalse(process_row(successor)['live'], 'the successor went on beside the relay')
        window.pump(3.0)  # past the 2 s a retired host is given
        self.assertTrue(process_row(first['host_pid'])['live'])
        self.assertEqual(len(window.spawns()), 1)  # a relay never asks again
        window.type('relay\r'); window.until('DONE_relay', 15)
        self.assertEqual((window.root/'native-read').read_bytes().count(b'relay\r'), 1)
        self.assertEqual(process_row(native['pid']), native)

    def test_keys_typed_while_the_successor_is_still_switching_all_arrive(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        window.fault('exit-after-switch')
        window.point('rel-b')
        for index in range(30):
            window.type('K%02d' % index); window.pump(.1)
        window.type('\r'); window.until('DONE_K00')
        sent = ''.join('K%02d' % index for index in range(30)).encode()+b'\r'
        got = (window.root/'native-read').read_bytes()
        self.assertTrue(got.replace(b'warm\r', b'').endswith(sent) or sent in got.replace(b'warm\r', b''), got)
        self.assert_keeps_native(window, native)

    # ----------------------------------------------------------------- S8
    def test_a_seed_missing_a_field_or_with_another_schema_is_refused(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        window.point('rel-b'); self.switched(window, first['host_pid'])
        seed = json.loads(window.seeds()[0].read_text())
        succession = window.link/'utilities/session-shell/succession.cjs'
        env = dict(self.env, SUCC_XTERM=str(Path(self.env['NODE_PATH'])/'@xterm/headless'))
        def refused(change, field=None):
            mutated = json.loads(json.dumps(seed)); change(mutated)
            path = window.root/'mutated.json'; path.write_text(json.dumps(mutated))
            out = subprocess.run([self.node, str(window.root/'unit.cjs'), str(succession), str(path)], env=env,
                                 capture_output=True, text=True, timeout=30, check=True)
            return json.loads(out.stdout)['invalid']
        self.assertIsNone(refused(lambda value: None))
        self.assertEqual(refused(lambda value: value.update(schema='hearting-shell-succession-v2')), 'schema')
        self.assertEqual(refused(lambda value: value.pop('outer')), 'outer')
        self.assertEqual(refused(lambda value: value.pop('native')), 'native')
        self.assertEqual(refused(lambda value: value['terminal'].pop('scrollRegion')), 'terminal')
        self.assertEqual(refused(lambda value: value.pop('scrollback')), 'scrollback')
        for name in ('sequence', 'selection', 'epoch', 'nativeEpoch', 'submitted', 'lastEnter'):
            self.assertEqual(refused(lambda value, name=name: value.pop(name)), name)

    # ---------------------------------------------------------------- S8b
    def scrollback_window(self, extra=None):
        window = self.window(extra=extra)
        first = window.wait_binding(); self.settle(window)
        window.type('flood\r'); window.until('FLOOD_119')
        window.type('y\r'); window.until('DONE_y'); window.wait_empty()
        window.type('flood\r'); window.until('FLOOD_119'); window.type('z\r'); window.until('DONE_z'); window.wait_empty()
        return window, first

    def assert_switched_without_scrollback(self, window, first, reason):
        before = window.screen()
        window.point('rel-b')
        self.switched(window, first['host_pid'])
        # Wait for the settled hand-over to show its one notice line (a deadline, not a fixed pump).
        notice = r'Switched without scrollback|스크롤백 없이 전환됨'
        end = time.monotonic()+15
        while True:
            window.pump(.1)
            after = window.screen()['lines']
            if self.notice_rows(before['lines'], after, notice):
                break
            if time.monotonic() > end:
                differing = [index for index, (a, b) in enumerate(zip(after, before['lines'])) if a != b]
                self.fail('hand-over notice never settled as the one notice line: %r' % (differing,))
        seed = json.loads(window.seeds()[0].read_text())
        self.assertEqual(seed['scrollback'], {'carried': False, 'reason': reason, 'lines': 0})
        # This is not a failure: the hand-over settled and nothing is started after it. (A first attempt that
        # timed out under load counts as a failure and retries, so the count before the settle is not asserted.)
        settled = len(window.spawns()); self.assertLessEqual(settled, 3)
        window.pump(.5); self.assertEqual(len(window.spawns()), settled)
        window.type('next\r'); window.until('DONE_next')
        end = time.monotonic()+15
        while True:
            window.pump(.1)
            shown = window.screen()['lines']
            if not any(re.search(notice, line) for line in shown):
                break
            if time.monotonic() > end:
                self.fail('the notice stayed on screen: %r' % (shown,))

    def test_a_seed_over_the_limit_carries_the_viewport_and_says_so(self):
        window, first = self.scrollback_window(extra={'HEARTING_SHELL_SEED_LIMIT': '20000'})
        self.assert_switched_without_scrollback(window, first, 'seed-too-large')

    def test_a_scrollback_that_cannot_be_written_out_carries_the_viewport_and_says_so(self):
        window, first = self.scrollback_window()
        window.fault('scrollback-throw')
        self.assert_switched_without_scrollback(window, first, 'capture-failed')

    def test_an_unreadable_scroll_region_keeps_the_old_host_and_the_waiting_line(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        native = process_row(window.native_pid())
        window.fault('region-missing'); window.point('rel-b'); window.pump(3.0)
        self.assertEqual(window.binding()['host_pid'], first['host_pid'])
        self.assertRegex(bytes(window.raw).decode(errors='replace'), r'New release waiting|새 판 대기')
        self.assert_keeps_native(window, native)

    # ----------------------------------------------------------------- S9
    def test_nothing_of_the_fixture_is_left_behind(self):
        window = self.window()
        first = window.wait_binding(); self.settle(window)
        window.point('rel-b'); self.switched(window, first['host_pid']); window.pump(.5)
        session = Path(window.binding()['socket']).parent
        names = sorted(path.name for path in session.iterdir())
        self.assertFalse([name for name in names if name.startswith('succession-') or name.endswith('.next')], names)
        window.close()
        end = time.monotonic()+10
        while window.tree() and time.monotonic() < end:
            time.sleep(.1)
        self.assertEqual(window.tree(), [])
        self.assertFalse(session.exists(), 'session directory left')


if __name__ == '__main__':
    unittest.main()
