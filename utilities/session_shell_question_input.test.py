"""Question caret and input transport over a private tmux and the real shell."""
import os, sys
if __name__ == '__main__' and not os.environ.get('HEARTING_TEST_ISOLATED'):
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(__file__), '../tools/run-tests.py'), '--direct-run', sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import fcntl
import importlib.util
import json
import pty
import struct
import termios
import threading
import re
import shlex
import signal
import shutil
import subprocess
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / 'utilities/session-shell'
SID = '00000000-0000-4000-8000-000000000121'

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod)
    return mod

def cells(s):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)

NATIVE = r'''
import os,sys,tty,json,socket,time,select
cfg=json.load(open(os.environ['LOOP_CONFIG']))
tty.setraw(0)
write=os.write
def record(fd,data):
    if fd==1:
        with open(cfg['native_output'],'ab') as f:
            if f.tell()+len(data)>1048576:raise RuntimeError('output log cap')
            f.write(data)
    return write(fd,data)
os.write=record
def event(kind):
    if not os.environ.get('HEARTING_SHELL_SOCKET'):return
    birth=open('/proc/self/stat').read().rsplit(') ',1)[1].split()[19]
    value=dict(op='event',event=kind,harness='claude',sid=cfg['sid'],pid=os.getpid(),start=birth,token=os.environ['HEARTING_SHELL_TOKEN'],seq=time.monotonic_ns()//1000)
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET']);c.sendall((json.dumps(value)+'\n').encode());c.recv(8192)
event('start');event('stop')
os.write(1,b'\x1b[?2004h\x1b[?1004h\x1b[2J\x1b[HREADY\r\n> ')
last='idle'
with open(cfg['received'],'wb',buffering=0) as log:
    while True:
        if select.select([0],[],[],.05)[0]:
            data=os.read(0,4096)
            if not data or data==b'\x04':break
            if log.tell()+len(data)>1048576:raise RuntimeError('input log cap')
            log.write(data)
            if data==b'x':os.write(1,b'x')
            if data==b'\x02':os.write(1,b'\x1b[?2004l\x1b[?1004l\x1b[?25l')
            if data==b'\x03':os.write(1,b'\x1b[?2004h\x1b[?1004h\x1b[?25h')
        state=open(cfg['state']).read()
        if state!=last:event('working' if state=='working' else 'stop');last=state
'''
PRELOAD = r'''
const fs=require('node:fs'),cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG));
if(process.argv.some(a=>a.endsWith('/session-shell/host.cjs'))){
 process.env.HEARTING_SHELL_PYTHON=cfg.backend;
 const cp=require('node:child_process'),path=require('node:path');
 let caretText=null;
 const blockPath=path.join(path.dirname(process.argv.find(a=>a.endsWith('/session-shell/host.cjs'))),'block-input.cjs');
 const create=require(blockPath);
 require.cache[blockPath].exports=(...args)=>{const b=create(...args),detail=b.detail;
  b.detail=options=>{const rows=detail(options);caretText=null;if(b.typing)for(const row of rows){const at=row.findIndex(p=>p.caret);if(at>=0)caretText=row.slice(0,at+1).map(p=>p.text).join('');}return rows;};return b;};
 const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
 let observations=0;
 function observe(){
  try{
   const snap=cp.execFileSync('tmux',['-S',cfg.socket,'capture-pane','-p','-t','qi:0.0',';','display-message','-p','-t','qi:0.0','CURSOR=#{cursor_x},#{cursor_y}'],{encoding:'utf8',stdio:['ignore','pipe','ignore'],timeout:2000}).trimEnd().split('\n');
   const actual=snap.pop().slice(7).split(',').map(Number),candidates=[];
   if(caretText===null)return;
   snap.forEach((line,y)=>{const at=line.indexOf('│ '+caretText);if(at>=0)candidates.push({actual,expected:[cells(line.slice(0,at+2)+caretText),y]});});
   if(candidates.length&&observations++<2000)fs.appendFileSync(cfg.frames,JSON.stringify(candidates.at(-1))+'\n');
  }catch{}
 }
 const write=process.stdout.write.bind(process.stdout);let total=0;
 process.stdout.write=function(data,...args){const bytes=Buffer.from(data);total+=bytes.length;if(total>2097152)throw Error('output cap');fs.appendFileSync(cfg.output,bytes);const result=write(data,...args);if(bytes.includes(Buffer.from('\x1b[?2026l')))setTimeout(observe,10);return result;};
}
'''

class PrivateClient:
    """An attached client on an owned PTY, used only with a private server."""
    def __init__(self, sock, env, cols, rows):
        self.output=bytearray()
        pid,fd=pty.fork()
        if pid==0:
            fcntl.ioctl(0,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
            os.execvpe('tmux',['tmux','-S',sock,'-f','/dev/null','attach','-t','qi'],env)
        self.pid,self.fd=pid,fd
        self.thread=threading.Thread(target=self.read,daemon=True);self.thread.start()
    def read(self):
        while True:
            try:data=os.read(self.fd,65536)
            except OSError:return
            if not data:return
            if len(self.output)+len(data)<=2097152:self.output.extend(data)
    def close(self):
        try:os.kill(self.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        os.waitpid(self.pid,0);os.close(self.fd);self.thread.join(timeout=2)


class QuestionInputTest(unittest.TestCase):
    def test_always_visible_answer_baseline_negative(self):
        script=r"""
const assert=require('node:assert/strict'),Module=require('node:module'),fs=require('node:fs');
const dir=process.argv[1],old=fs.readFileSync(0,'utf8').replace('↑↓ choose · Enter send · t text','↑↓ choose · Enter send');
const previous=new Module(dir+'/block-input.cjs');previous.filename=dir+'/block-input.cjs';previous.paths=module.paths;previous._compile(old,previous.filename);
const current=require(dir+'/block-input.cjs');
const measure=s=>[...s].reduce((n,c)=>n+(/\p{Mark}/u.test(c)?0:c.codePointAt(0)>=0x2e80?2:1),0);
function setup(create,extra={}){let sent,actions=[];const b=create({cellWidth:measure,changed(){},action:c=>actions.push(c),answer:async x=>(sent=x,{state:'submitted'})});
 b.update([{id:'synthetic',revision:'one',answerable:true,questions:[{id:'q',question:'Synthetic question',free_text:true,choices:[{value:'v1',label:'First'},{value:'v2',label:'Second'}],...extra}]}],'',{});b.enter();
 return {b,actions,send:async keys=>{b.input(Buffer.from(keys));await new Promise(r=>setImmediate(r));return sent},rows:(columns=96,budget=24)=>b.detail({columns,budget}),get sent(){return sent}};}
const words=rows=>rows.map(r=>r.map(p=>p.text).join('')).join('\n');
const cases={
 visible:async create=>{const x=setup(create);assert(x.rows().some(r=>r.map(p=>p.text).join('')==='› '));},
 letters:async create=>{for(const first of ['r','w','f','p','t','한','!']){const x=setup(create);assert.deepEqual((await x.send(first+'note\r'))?.answers.q,[first+'note']);assert.deepEqual(x.actions,[]);}},
 mixed:async create=>{const x=setup(create);assert.deepEqual((await x.send('2hello\r'))?.answers.q,['v2','hello']);assert(words(x.b.sent()).includes('Second, hello'));},
 down_digits:async create=>{const x=setup(create);assert.deepEqual((await x.send('2\x1b[B123\r'))?.answers.q,['v2','123']);},
 up_retains:async create=>{const x=setup(create);assert.deepEqual((await x.send('note\x1b[A2\r'))?.answers.q,['v2','note']);},
 click:async create=>{const x=setup(create);x.b.write();assert.deepEqual((await x.send('12\r'))?.answers.q,['12']);},
 empty:async create=>{const x=setup(create);assert.equal(await x.send('가\x7f\r'),undefined);assert(words(x.rows()).includes('empty answer'));},
 wrap:async create=>{for(const columns of [96,56,17])for(const budget of [30,6,2]){const x=setup(create);x.b.input(Buffer.from('한글 답변 '.repeat(30)+'끝'));const rows=x.rows(columns,budget),edit=rows.find(r=>r.some(p=>p.caret));assert(edit);const text=edit.map(p=>p.text).join('');assert(text.endsWith('끝'));assert(text.startsWith('  '));assert(measure(text)<columns);assert(!text.includes('…'));if(budget===30)assert(rows.filter(r=>r.some(p=>p.answerRow)).length>1);}},
 secret:async create=>{const x=setup(create,{secret:true});x.b.input(Buffer.from('private'));assert(!words(x.rows()).includes('private'));assert(words(x.rows()).includes('•••••••'));},
 split_utf8:async create=>{for(let at=1;at<Buffer.byteLength('한글');at++){const x=setup(create),s=Buffer.from('한글');x.b.input(s.subarray(0,at));x.b.input(s.subarray(at));assert.deepEqual((await x.send('\r'))?.answers.q,['한글']);}},
 paste_mixed:async create=>{const x=setup(create);assert.deepEqual((await x.send('2\x1b[200~hello\x1b[201~\r'))?.answers.q,['v2','hello']);},
 tab:async create=>{const x=setup(create);x.b.update([{id:'synthetic',revision:'one',answerable:true,questions:['q','q2'].map(id=>({id,question:id,free_text:true,choices:['one','two']}))}],'',{});assert.deepEqual((await x.send('2note\t1other\r'))?.answers,{q:['two','note'],q2:['one','other']});},
};
(async()=>{for(const [name,test] of Object.entries(cases)){await test(current);let failed=false;try{await test(previous.exports);}catch{failed=true;}assert(failed,'baseline must fail '+name);console.log('NEW_CASE '+name+' current=PASS baseline=FAIL');}
 for(const columns of [96,56,17])for(const budget of [24,6,2]){const draw=create=>JSON.stringify(setup(create,{free_text:false}).rows(columns,budget),(k,v)=>k==='choiceIndex'?undefined:v).replace(' · t text','');assert.equal(draw(current),draw(previous.exports));}
 for(const columns of [96,56,17])for(const budget of [24,6,2])for(const keys of ['', '\r']){
  const draw=create=>{const x=setup(create);x.b.leave();x.b.update([{id:'decision',revision:'one',answerable:true,answer_kind:'decision',questions:[{id:'q',question:'Decision'}]}],'',{});x.b.enter();x.b.input(Buffer.from(keys));return JSON.stringify(x.rows(columns,budget),(k,v)=>k==='choiceIndex'?undefined:v).replace(' · t text','');};assert.equal(draw(current),draw(previous.exports));
 }
 const x=setup(current);assert.deepEqual((await x.send('2\r')).answers.q,['v2']);
 const y=setup(current);y.b.input(Buffer.from('a'.repeat(4096)+'한\x7fZ'));assert.equal((await y.send('\r')).answers.q[0],'a'.repeat(4095)+'Z');
 for(const close of ['\x1d','\x1b']){const z=setup(current);z.b.input(Buffer.from('note'+close));await new Promise(r=>setTimeout(r,35));assert(!z.sent);assert(!z.b.focused);}
 console.log('COMPAT choice-only bytes, choice, limit, backspace, dismissal PASS');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        baseline=subprocess.check_output(['git','show','15784ff83:utilities/session-shell/block-input.cjs'],text=True,timeout=10,cwd=ROOT)
        result=subprocess.run(['node','-e',script,str(SHELL)],input=baseline,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        print(result.stdout,flush=True)

    def test_normal_exit_disables_remembered_cursor_keys_and_x10(self):
        script=r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[1]+'/host.cjs','utf8');
const body=source.slice(source.indexOf('function wireDisplay() {'),source.indexOf('// ---- release detection'));
(async()=>{
 for(const enabled of [false,true]){
  let exit,output='',code;
  const ctx={terminal:{onData(){},onWriteParsed(){},onTitleChange(){}},
   child:{onData(){},onExit(fn){exit=fn}},process:{on(){},stdin:{on(){}},stdout:{on(){}},exit(n){code=n}},
   userInput(){},switched:true,ended:false,heldStop(){},clearTimeout(){},frameTimer:0,projectionTimer:0,beatTimer:0,
   watchers:new Map(),releaseWatchers:[],server:{close(){}},source:async()=>{},backend:null,
   paintedModes:new Map([[1,enabled],[9,enabled]]),emit(s){output+=s},fs:{rmSync(){}},socketPath:'fixture',nextPath:'fixture-next',seedFile:null};
  vm.runInNewContext(body+'\nwireDisplay();',ctx);await exit({exitCode:7});
  const extra=enabled?'\x1b[?1l\x1b[?9l':'';
  assert.equal(output,'\x1b[0m'+extra+'\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?25h\x1b[?1049l');
  assert.equal(code,7);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result=subprocess.run(['node','-e',script,str(SHELL)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_small_free_text_preview_keeps_the_answer_row(self):
        script=r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]+'/block-input.cjs');
const b=create({changed(){},action(){},answer(){}});
b.update([{id:'fixture',revision:'one',answerable:true,questions:[{id:'q',question:'Keep this body',choices:['one','two'],free_text:true}]}],'',{});
b.enter();const rows=b.detail({budget:2,columns:20});
assert.equal(rows[1].map(p=>p.text).join(''),'› ');
assert.equal(b.typing,false);
b.input(Buffer.from('t'));const edit=b.detail({budget:2,columns:20});
assert(edit[1].some(p=>p.caret),'the same small budget keeps the live draft while editing');
"""
        result=subprocess.run(['node','-e',script,str(SHELL)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_fragmented_utf8_drafts_and_alt_tail(self):
        script=r'''
const assert=require('node:assert/strict'),create=require(process.argv[1]+'/block-input.cjs');
(async()=>{
 const text='가나다 한글 abc 끝',bytes=Buffer.from(text);let total=0,cases=0;
 for(const pasted of [false,true])for(const delay of [0,2])for(let size=1;size<=bytes.length;size++){
  let answer;const b=create({changed(){},action(){},answer:async x=>(answer=x,{state:'submitted'})});
  b.update([{id:'fixture',revision:'one',answerable:true,questions:[{id:'q',question:'시험 글'}]}],'',{});b.enter();
  if(pasted)b.input(Buffer.from('\x1b[200~'));
  for(let i=0;i<bytes.length;i+=size){b.input(bytes.subarray(i,i+size));if(delay)await new Promise(r=>setTimeout(r,delay));}
  if(pasted)b.input(Buffer.from('\x1b[201~'));
  b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));assert.equal(answer.answers.q[0],text);total+=bytes.length;cases++;
 }
 // Every split of Alt+Korean: no UTF-8 tail leaks into the next draft.
 const alt=Buffer.from('\x1b한');
 for(let cut=1;cut<alt.length;cut++){
  let answer;const b=create({changed(){},action(){},answer:async x=>(answer=x,{state:'submitted'})});
  b.update([{id:'fixture',revision:'one',answerable:true,questions:[{id:'q',question:'시험 글'}]}],'',{});b.enter();
  b.input(alt.subarray(0,cut));b.input(alt.subarray(cut));b.input(Buffer.from('끝\r'));await new Promise(r=>setImmediate(r));assert.equal(answer.answers.q[0],'끝');
 }
 console.log(JSON.stringify({draft_cases:cases,draft_bytes:total,alt_splits:alt.length-1}));
})().catch(e=>{console.error(e);process.exit(1)});
'''
        result=subprocess.run(['node','-e',script,str(SHELL)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        print(result.stdout.strip(),flush=True)

    def test_private_tmux_visible_choice_payload_table(self):
        if not shutil.which('tmux'):
            self.skipTest('tmux unavailable')
        with tempfile.TemporaryDirectory(prefix='qm-') as temp:
            root = Path(temp)
            runner = load('qm_isolation', ROOT/'tools/run-tests.py')
            env = runner.build_isolated_env(root, repo_root=ROOT)
            env.update(FLEET_NOW_LANG='en', CLAUDE_CONFIG_DIR=str(root/'native-config'))
            for key in ('cache', 'devdir'):
                env['npm_config_'+key] = str(root/('npm-'+key))
            env['npm_config_userconfig'] = str(root/'npmrc')
            (root/'npmrc').write_text('')
            sys.path[:0] = [str(ROOT/'tools/install'), str(ROOT/'utilities')]
            import session_shell_install
            with mock.patch.dict(os.environ, env, clear=True):
                session_shell_install.prepare_assets()
            policy = Path(env.get('XDG_CONFIG_HOME', str(Path(env['HOME'])/'.config')))/'hearting/session-shell.json'
            policy.parent.mkdir(parents=True, exist_ok=True)
            fixture = load('qm_backend', ROOT/'utilities/session_shell_wait_blink_pty.test.py')
            cases = [([], [2], ['v2']), (['가나'], [], ['가나']),
                     (['3', '가나'], [3], ['v3', '가나']),
                     (['\x1b[B', '가나'], [3], ['v3', '가나']),
                     (['3', '3', '가나'], [], ['가나']),
                     (['가나', '\x1b[A', '1'], [1], ['v1', '가나']),
                     (['click-write', '1가'], [], ['1가']),
                     (['가', '\x7f'], [], None),
                     (['3', '\t', '\t', '\t'], [3], ['v3']),
                     (['click-3', '가나'], [3], ['v3', '가나'])]
            for layout, cols in [('bottom', 100), ('side', 160)]:
                case = root/layout
                case.mkdir()
                (case/'project').mkdir()
                policy.write_text(json.dumps(dict(enabled=True, layout=layout, side_width=40,
                    side_min_columns=132, shade='none', theme='dark', rule_motion='rule')))
                policy.chmod(0o600)
                qp = case/'questions.json'
                qp.write_text('{}')
                backend = case/'backend'
                code = fixture.BACKEND.format(python=sys.executable, utilities=str(ROOT/'utilities'),
                                              tools=str(ROOT/'tools'), sid=SID, questions=str(qp))
                code = code.replace('raise SystemExit(m._backend())',
                    "def answer(binding, request):\n    with open(" + repr(str(case/'answers')) +
                    ", 'a') as log: log.write(json.dumps(request, ensure_ascii=False)+'\\n')\n" +
                    "    return {'ok': True, 'state': 'unknown', 'reason': 'synthetic recorded'}\n" +
                    "m._block_answer=answer\nraise SystemExit(m._backend())")
                backend.write_text(code)
                backend.chmod(0o700)
                cfg = dict(sid=SID, backend=str(backend), socket=str(case/'tmux.sock'), frames=str(case/'frames'),
                           output=str(case/'output'), received=str(case/'received'),
                           native_output=str(case/'native-output'), state=str(case/'state'))
                (case/'state').write_text('idle')
                (case/'config').write_text(json.dumps(cfg))
                (case/'native.py').write_text(NATIVE)
                (case/'preload.cjs').write_text(PRELOAD)
                e = dict(env, LOOP_CONFIG=str(case/'config'), NODE_OPTIONS='--require '+str(case/'preload.cjs'),
                         TERM='xterm-256color')
                sock = str(case/'tmux.sock')
                def tm(*args):
                    return subprocess.check_output(['tmux', '-S', sock, '-f', '/dev/null', *args], env=e,
                                                   stderr=subprocess.STDOUT, text=True, timeout=10)
                command = shlex.join([sys.executable, str(SHELL/'entry.py'), 'claude', sys.executable, str(case/'native.py')])
                tm('new-session', '-d', '-s', 'qi', '-x', str(cols), '-y', '30', '-c', str(case/'project'), command)
                server = int(tm('display-message', '-p', '#{pid}').strip())
                birth = Path(f'/proc/{server}/stat').read_text().rsplit(') ', 1)[1].split()[19]
                def screen():
                    return tm('capture-pane', '-p', '-t', 'qi:0.0').splitlines()
                def waitfor(predicate):
                    until = time.monotonic()+25
                    while time.monotonic() < until:
                        rows = screen()
                        if predicate(rows):
                            return rows
                        time.sleep(.08)
                    self.fail('missing synthetic screen: '+'\n'.join(screen()))
                def send(data):
                    raw = data.encode()
                    tm('send-keys', '-t', 'qi:0.0', '-H', *[f'{v:02x}' for v in raw])
                    time.sleep(.15)
                def click(needle):
                    rows = screen()
                    y = next(y for y, row in enumerate(rows) if needle in row)
                    x = cells(rows[y].split(needle)[0])+1+(2 if needle.startswith('│ ') else 0)
                    send(f'\x1b[<0;{x};{y+1}M\x1b[<0;{x};{y+1}m')
                try:
                    waitfor(lambda rows: any('READY' in row for row in rows))
                    for index, (keys, mark, payload) in enumerate(cases):
                        prompt = f'Synthetic choice {index}'
                        q = dict(id='q', question=prompt, free_text=True, recommended=1,
                                 choices=[dict(value=f'v{n}', label=f'Item {n}') for n in (1, 2, 3)])
                        items = [dict(id=f'fixture-{index}', revision=f'{index+1:064x}', answerable=True, questions=[q]),
                                 dict(id=f'other-{index}', revision=f'{index+1:064x}', answerable=True,
                                      questions=[dict(q, id='q2', question='Other synthetic question')])]
                        qp.write_text(json.dumps({SID: items}))
                        (case/'state').write_text('working' if index % 2 == 0 else 'idle')
                        waitfor(lambda rows: any(prompt in row for row in rows))
                        send('\x1d')
                        waitfor(lambda rows: any('▸ 2  Item 2' in row for row in rows))
                        for key in keys:
                            if key == 'click-write':
                                click('│ › ')
                            elif key == 'click-3':
                                click('3  Item 3')
                            else:
                                send(key)
                        rows = screen()
                        actual = [int(m.group(1)) for row in rows if (m := re.search(r'▸ ([123])  Item', row))]
                        self.assertEqual(actual, mark, (layout, keys, rows))
                        before = (case/'answers').read_text().splitlines() if (case/'answers').exists() else []
                        send('\r')
                        if payload is None:
                            waitfor(lambda rows: any('empty answer' in row for row in rows))
                        else:
                            waitfor(lambda rows: any('synthetic recorded' in row for row in rows))
                        after = (case/'answers').read_text().splitlines() if (case/'answers').exists() else []
                        sent = json.loads(after[-1])['answers']['q'] if len(after) > len(before) else None
                        self.assertEqual(sent, payload, (layout, keys))
                        print('TMUX_RULE='+json.dumps(dict(layout=layout, keys=keys, mark=actual, sent=sent), ensure_ascii=False), flush=True)
                        send('\x1d')
                    send('\x04')
                finally:
                    try:
                        stat = Path(f'/proc/{server}/stat').read_text().rsplit(') ', 1)[1].split()
                        if stat[19] == birth:
                            os.kill(server, signal.SIGTERM)
                    except FileNotFoundError:
                        pass

    def test_private_tmux_caret_and_native_bytes(self):
        if not shutil.which('tmux'):self.skipTest('tmux unavailable')
        with tempfile.TemporaryDirectory(prefix='qi-') as temp:
            root=Path(temp)
            runner=load('qi_isolation',ROOT/'tools/run-tests.py')
            env=runner.build_isolated_env(root,repo_root=ROOT)
            env.update(FLEET_NOW_LANG='en',CLAUDE_CONFIG_DIR=str(root/'native-config'))
            for key in ('cache','devdir'):env['npm_config_'+key]=str(root/('npm-'+key))
            env['npm_config_userconfig']=str(root/'npmrc');(root/'npmrc').write_text('')
            sys.path[:0]=[str(ROOT/'tools/install'),str(ROOT/'utilities')]
            import session_shell_install
            with mock.patch.dict(os.environ,env,clear=True):assets=session_shell_install.prepare_assets()
            policy=Path(env.get('XDG_CONFIG_HOME',str(Path(env['HOME'])/'.config')))/'hearting/session-shell.json'
            policy.parent.mkdir(parents=True,exist_ok=True)
            fixture=load('qi_blink',ROOT/'utilities/session_shell_wait_blink_pty.test.py')
            cache=Path(env['XDG_STATE_HOME'])/'hearting/human-waits';cache.mkdir(parents=True,exist_ok=True)
            with open(cache/'.projection.lock','w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                key='claude\0'+SID
                (cache/'projection.json').write_text(json.dumps(dict(schema_version=3,items=[],by_session={},total=0,older=0,complete=True,observed_at=time.time(),route_panels={key:[dict(id='rt-b000000000000121',title='Synthetic input check',state='idle',worker_stage='one-shot',shape='one-shot-owner',stages=[dict(name='one-shot',st='pending')],harness='claude')]},route_status={key:'route 1'},route_sessions={key:time.time()+3600})))
                (cache/'projection.json').chmod(0o600)
                measured=[];rates=[];transport=[];frame_checks=[]
                for layout,cols in [('side',160),('bottom',100)]:
                    case=root/layout;case.mkdir();(case/'project').mkdir()
                    policy.write_text(json.dumps(dict(enabled=True,layout=layout,side_width=40,side_min_columns=132,shade='none',theme='dark',rule_motion='rule')));policy.chmod(0o600)
                    questions=[dict(id='choice',question='시험 문구를 고르세요.',choices=['진행','보류','수정 — 고칠 것을 적는다'],free_text=True),dict(id='text',question='› 설명을 적으세요.'),dict(id='secret',question='시험 암호를 적으세요.',secret=True),dict(id='scroll',question='긴 질문',display={'body':[[{'text':f'설명 {i}'}] for i in range(45)]})]
                    qp=case/'questions.json';qp.write_text(json.dumps({SID:[dict(id='fixture',revision='a'*64,answerable=True,questions=questions)]}))
                    backend=case/'backend';backend.write_text(fixture.BACKEND.format(python=sys.executable,utilities=str(ROOT/'utilities'),tools=str(ROOT/'tools'),sid=SID,questions=str(qp)));backend.chmod(0o700)
                    cfg=dict(sid=SID,backend=str(backend),socket=str(case/'tmux.sock'),frames=str(case/'frames'),output=str(case/'output'),received=str(case/'received'),native_output=str(case/'native-output'),state=str(case/'state'))
                    (case/'state').write_text('idle');(case/'config').write_text(json.dumps(cfg));(case/'native.py').write_text(NATIVE);(case/'preload.cjs').write_text(PRELOAD)
                    e=dict(env,LOOP_CONFIG=str(case/'config'),NODE_OPTIONS='--require '+str(case/'preload.cjs'),TERM='xterm-256color')
                    sock=str(case/'tmux.sock')
                    def tm(*args):return subprocess.check_output(['tmux','-S',sock,'-f','/dev/null',*args],env=e,stderr=subprocess.STDOUT,text=True,timeout=10)
                    command=shlex.join([sys.executable,str(SHELL/'entry.py'),'claude',sys.executable,str(case/'native.py')])
                    tm('new-session','-d','-s','qi','-x',str(cols),'-y','30','-c',str(case/'project'),command)
                    server=int(tm('display-message','-p','#{pid}').strip())
                    birth=Path(f'/proc/{server}/stat').read_text().rsplit(') ',1)[1].split()[19]
                    def stop_server():
                        try:
                            stat=Path(f'/proc/{server}/stat').read_text().rsplit(') ',1)[1].split()
                            if stat[19]==birth:os.kill(server,signal.SIGTERM)
                        except ProcessLookupError:pass
                        except FileNotFoundError:pass
                    try:
                        def screen():return tm('capture-pane','-p','-t','qi:0.0').splitlines()
                        def waitfor(word):
                            until=time.monotonic()+25
                            while time.monotonic()<until:
                                s=screen()
                                if any(word in line for line in s):return s
                                time.sleep(.1)
                            self.fail('missing '+word+'\n'+'\n'.join(screen()))
                        def send(data,delay=.12):
                            if isinstance(data,str):data=data.encode()
                            tm('send-keys','-t','qi:0.0','-H',*[f'{v:02x}' for v in data]);time.sleep(delay)
                        def position(name):
                            s=screen();actual=list(map(int,tm('display-message','-p','-t','qi:0.0','#{cursor_x},#{cursor_y}').strip().split(',')))
                            observations=[json.loads(line) for line in Path(cfg['frames']).read_text().splitlines()] if Path(cfg['frames']).exists() else []
                            expected=observations[-1]['expected'] if observations else None
                            measured.append(dict(layout=layout,case=name,actual=actual,expected=expected,ok=actual==expected))
                        # Let the initial asynchronous projection settle its bottom-panel height.
                        waitfor('READY');waitfor('QUESTIONS');time.sleep(2)
                        for state in ('idle','working'):
                            (case/'state').write_text(state);time.sleep(.6)
                            begin=Path(cfg['output']).stat().st_size;clock=time.monotonic()
                            for _ in range(10):send('x',.1)
                            raw=Path(cfg['output']).read_bytes()[begin:]
                            synced=False;outside=0
                            for seq in re.findall(rb'\x1b(?:[78]|\[[0-9;?]*[A-Za-z])',raw):
                                if seq==b'\x1b[?2026h':synced=True
                                elif seq==b'\x1b[?2026l':synced=False
                                elif not synced and (seq in (b'\x1b7',b'\x1b8') or seq[-1:] in (b'H',b'G',b'A',b'B',b'C',b'D')):outside+=1
                            rates.append(dict(layout=layout,state=state,outside_sync_cursor=outside,seconds=round(time.monotonic()-clock,4),bytes=len(raw),cup=len(re.findall(rb'\x1b\[[0-9;]*[HG]',raw)),save=raw.count(b'\x1b7'),restore=raw.count(b'\x1b8'),modes={m.decode():len(re.findall(rb'\x1b\[\?'+m+rb'[hl]',raw)) for m in [b'25',b'1004',b'2004',b'2026',b'1000',b'1002',b'1003',b'1006',b'1']}))
                        # Native mode transitions still reach the terminal exactly once.
                        for control,flag in [(b'\x02',b'l'),(b'\x03',b'h')]:
                            start_mode=Path(cfg['output']).stat().st_size;send(control,.3)
                            raw_mode=Path(cfg['output']).read_bytes()[start_mode:]
                            for mode in (b'2004',b'1004',b'25'):
                                self.assertEqual(raw_mode.count(b'\x1b[?'+mode+flag),1,(mode,raw_mode))
                        # Lose modes outside the host, then ask for a whole repaint.
                        # Check the real pane state AND delivered paste/focus bytes.
                        tm('set','-g','focus-events','on')
                        client=PrivateClient(sock,e,cols,30)
                        try:
                            time.sleep(.4)
                            pane_tty=tm('display-message','-p','-t','qi:0.0','#{pane_tty}').strip()
                            with open(pane_tty,'wb',buffering=0) as tty:
                                tty.write(b'\x1b[?1002l\x1b[?1006l\x1b[?2004l\x1b[?1004l\x1b[?25l')
                            time.sleep(.15)
                            self.assertEqual(tm('display-message','-p','-t','qi:0.0','#{mouse_button_flag}#{mouse_sgr_flag}#{cursor_flag}').strip(),'000')
                            tm('resize-window','-t','qi:0','-x',str(cols-1),'-y','30');time.sleep(.3)
                            self.assertEqual(tm('display-message','-p','-t','qi:0.0','#{mouse_button_flag}#{mouse_sgr_flag}#{cursor_flag}').strip(),'111')
                            start=Path(cfg['received']).stat().st_size
                            tm('set-buffer','-b','qf','PASTE');tm('paste-buffer','-p','-b','qf','-t','qi:0.0');time.sleep(.2)
                            os.write(client.fd,b'\x1b[O');time.sleep(.2);os.write(client.fd,b'\x1b[I');time.sleep(.2)
                            delivered=Path(cfg['received']).read_bytes()[start:]
                            self.assertIn(b'\x1b[200~PASTE\x1b[201~',delivered)
                            self.assertIn(b'\x1b[O',delivered);self.assertIn(b'\x1b[I',delivered)
                            tm('resize-window','-t','qi:0','-x',str(cols),'-y','30');time.sleep(.3)
                        finally:client.close()
                        send('\x1d');waitfor('시험 문구')
                        for index,kind in enumerate(['choice','text','secret','scroll']):
                            if index:send('\t')
                            send('가\x7f' if kind=='choice' else '\x1b[B');position(kind+'-empty')
                            send('\r');position(kind+'-empty-enter')
                            tm('resize-window','-t','qi:0','-x','20','-y','8');time.sleep(.3);position(kind+'-small-empty')
                            tm('resize-window','-t','qi:0','-x',str(cols),'-y','30');time.sleep(.3)
                            send('a');position(kind+'-ascii')
                            send('\x7f한');position(kind+'-wide')
                            send(' 긴 한글 답변입니다.'*12);position(kind+'-long')
                            time.sleep(1.1);position(kind+'-beat')
                            send('\x1b[6~');position(kind+'-page')
                            tm('resize-window','-t','qi:0','-x','60','-y','12');time.sleep(.3);position(kind+'-resize60')
                            tm('resize-window','-t','qi:0','-x',str(cols),'-y','30');time.sleep(.3);position(kind+'-resize-back')
                        send('\x1d')
                        payload='한글 abc 끝'.encode();expected=bytearray();start=Path(cfg['received']).stat().st_size
                        # All chunk sizes and every byte boundary, interleaved with focus, paste and mouse reports.
                        for size in range(1,len(payload)+1):
                            controls=b'\x1b[I\x1b[O\x1b[200~';send(controls,0);expected.extend(controls)
                            for at in range(0,len(payload),size):send(payload[at:at+size],.002);expected.extend(payload[at:at+size])
                            send(b'\x1b[201~',0);expected.extend(b'\x1b[201~')
                            send(b'\x1b[<64;2;2M',0) # Shell consumes a wheel over its own native scrollback.
                            if size%4==0:
                                send('\x1d');send('\x1b',.1)
                                tm('resize-window','-t','qi:0','-x',str(cols-1),'-y','30')
                                tm('resize-window','-t','qi:0','-x',str(cols),'-y','30')
                        time.sleep(.4);received=Path(cfg['received']).read_bytes()[start:]
                        # A mouse report coalesced with the paste-end read can be forwarded as raw
                        # paste tail. Count it separately: it is not repeated answer text.
                        mouse=b'\x1b[<64;2;2M';forwarded=received.count(mouse);content=received.replace(mouse,b'')
                        transport.append(dict(layout=layout,wire_bytes=len(expected)+len(mouse)*len(payload),inserted=len(expected),received=len(received),mouse_reports_forwarded=forwarded,content_received=len(content),equal=content==expected))
                        if Path(cfg['frames']).exists():
                            frame_checks.extend(dict(layout=layout,**json.loads(line)) for line in Path(cfg['frames']).read_text().splitlines())
                        send('\x04')
                    finally:
                        stop_server()
                        # Only the recorded server PID with the same birth, never a process-name match.
                        for _ in range(50):
                            if not Path(f'/proc/{server}').exists():break
                            time.sleep(.02)
                plain=root/'plain';plain.mkdir()
                cfg=dict(sid=SID,received=str(plain/'received'),native_output=str(plain/'output'),state=str(plain/'state'))
                (plain/'state').write_text('idle');(plain/'config').write_text(json.dumps(cfg));(plain/'native.py').write_text(NATIVE)
                e=dict(env,LOOP_CONFIG=str(plain/'config'),TERM='xterm-256color');sock=str(plain/'tmux.sock')
                tm('new-session','-d','-s','qi','-x','100','-y','30',shlex.join([sys.executable,str(plain/'native.py')]))
                server=int(tm('display-message','-p','#{pid}').strip())
                birth=Path(f'/proc/{server}/stat').read_text().rsplit(') ',1)[1].split()[19]
                try:
                    time.sleep(.3);begin=Path(cfg['native_output']).stat().st_size;clock=time.monotonic()
                    for _ in range(10):send('x',.1)
                    raw=Path(cfg['native_output']).read_bytes()[begin:]
                    plain_result=dict(seconds=round(time.monotonic()-clock,4),bytes=len(raw),escapes=raw.count(b'\x1b'))
                    self.assertEqual(raw,b'x'*10)
                    send('\x04')
                finally:
                    stop_server()
                evidence=dict(caret=measured,frame_checks=frame_checks,rates=rates,transport=transport,plain=plain_result)
                print('QI_MEASURE='+json.dumps(evidence,ensure_ascii=False),flush=True)
                self.assertTrue(all(x['equal'] for x in transport),transport)
                self.assertGreater(len(frame_checks),80)
                self.assertTrue(all(x['actual']==x['expected'] for x in frame_checks),[x for x in frame_checks if x['actual']!=x['expected']])
                self.assertTrue(all(x['outside_sync_cursor']==0 for x in rates),rates)
                self.assertTrue(all(all(x['modes'][m]==0 for m in ('25','1004','2004','1000','1002','1003','1006','1')) for x in rates),rates)
                self.assertTrue(all(x['ok'] for x in measured),[x for x in measured if not x['ok']])

if __name__=='__main__':unittest.main()
