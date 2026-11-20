"""11-20 (the person: "그 구분줄 스캐너가 안움직이네 갑자기?"): while a session runs, the panel's moving frame (`rule motion`
`scanners`, `border`, and the head rule's light under `rule`) keeps moving, whatever came before: a plain start, the hooks'
stop that the native's own record contradicts (a Stop hook that keeps the turn going), `/compact`'s events, a press, a drag and a
double press in the native area, a fold and open of the panel, a resize.

Over the real host in a private PTY with the person's settings (side, right, 59 cells, `terminal`) in a 170 x 46 window. The
native is a fixture that writes Claude's own session record (`<config>/sessions/<pid>.json`, the identity the host reads: this
process, its start, the session) and reports the hook events. Each case records the bytes the host writes for two seconds while
the native's own record says it runs; the moving cells' colors in them must change. The color bytes are read with the time
they arrive; nothing here reaches a real account, session or terminal.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import importlib.util
import json
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SID = '00000000-0000-4000-8000-000000001120'
SPIN = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

FAKE_NATIVE = r"""
import json,os,socket,sys,time,tty
harness,sid=sys.argv[1],sys.argv[2]
raw=open('/proc/self/stat').read();START=raw.rsplit(') ',1)[1].split()[19]
RECORD=os.path.join(os.environ['CLAUDE_CONFIG_DIR'],'sessions',str(os.getpid())+'.json')
os.makedirs(os.path.dirname(RECORD),exist_ok=True)
CLAUDE=harness=='claude'
def event(kind,source=None):
    req={'op':'event','event':kind,'harness':harness,'sid':sid,'pid':os.getpid(),'start':START,
         'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    if source:req['source']=source
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET']);c.sendall((json.dumps(req)+'\n').encode());c.recv(8192)
def rollout(kind,turn):
    home=os.environ.get('CODEX_HOME')
    if not home:return
    path=os.path.join(home,'sessions','2026','11','20','rollout-2026-11-20T00-00-00-'+sid+'.jsonl')
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,'a') as f:f.write(json.dumps({'type':'event_msg','payload':{'type':kind,'turn_id':turn}})+'\n')
def record(status,waiting=None):
    value={'pid':os.getpid(),'procStart':START,'sessionId':sid,'status':status,'cwd':os.getcwd()}
    if waiting:value['waitingFor']=waiting
    open(RECORD+'.new','w').write(json.dumps(value));os.replace(RECORD+'.new',RECORD)
import threading
SPINNING=[False]
def spinner():
    n=0
    while True:
        time.sleep(.12)
        if SPINNING[0]:
            n+=1;print('\x1b[5;1H\x1b[0m'+'* working '+str(n)+'  ',end='',flush=True)
threading.Thread(target=spinner,daemon=True).start()
tty.setraw(0)
print('\x1b[?2004h',end='',flush=True)
if CLAUDE:record('idle')
event('start','startup');event('stop')
pending='';said='READY_'+harness
def screen():
    print('\x1b[2J\x1b[H'+said+'\r\n'+'❯ '+pending[-60:]+'\r\n'+'─'*18+'\r\n',end='',flush=True)
screen()
while True:
    data=os.read(0,65536)
    if not data:break
    pending+=data.decode(errors='replace')
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        if line=='quit':raise SystemExit
        if line.startswith('ev:'):
            kind,_,source=line[3:].partition(':');event(kind,source or None)
        if line.startswith('roll:'):
            kind,_,turn=line[5:].partition(':');rollout(kind,turn)
        if line.startswith('spin:'):SPINNING[0]=line=='spin:on'
        if line.startswith('rec:'):
            status,_,waiting=line[4:].partition(':');record(status,waiting or None)
        said='DONE_'+line
    screen()
"""

LAUNCHER = r"""
const fs=require('node:fs');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
if(process.argv.some(arg=>arg.endsWith('/session-shell/host.cjs')||arg.endsWith('/host.cjs'))){
 process.env.HEARTING_SHELL_PYTHON=cfg.backend;
 process.on('uncaughtException',error=>fs.appendFileSync(cfg.diagnostic,String(error?.stack||error)+'\n'));
}
"""

BACKEND = r"""#!{python}
import json,os,sys
sys.path.insert(0,{utilities!r});sys.path.insert(0,{tools!r})
import session_shell as m
from fleet import session_tags
session_tags.assigned_tag=lambda harness,sid:{{{sid!r}:'e9'}}.get(sid)
raise SystemExit(m._backend())
"""

# One case = a fresh host, then steps. `rec` steps record the bytes written for `ms` after settling.
DRIVER = r"""
const fs=require('node:fs');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function runCase(spec){
  let cols=170,rows=46;
  const term=new Terminal({cols,rows,allowProposedApi:true,scrollback:0});
  term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
  const mark='pfm-'+spec.name+'-'+process.pid;
  const env={...process.env,TERM:spec.term||'xterm-256color',NODE_OPTIONS:'--require '+cfg.launcher,PFM_MARK:mark};
  delete env.COLORTERM;if(spec.colorterm)env.COLORTERM='truecolor';
  let entry=cfg.entry;
  if(spec.releases){const base=cfg.project+'/../rel-'+spec.name;fs.mkdirSync(base);
    const real=cfg.entry.replace(/\/entry\.py$/,'');
    for(const r of ['one','two']){fs.mkdirSync(base+'/'+r);
      for(const f of fs.readdirSync(real)){if(f==='host.cjs')fs.writeFileSync(base+'/'+r+'/host.cjs',fs.readFileSync(real+'/host.cjs','utf8')+'\n// release '+r+'\n');
        else fs.symlinkSync(real+'/'+f,base+'/'+r+'/'+f);}}
    fs.symlinkSync(base+'/one',base+'/cur');entry=base+'/cur/entry.py';spec.steps.forEach(st=>{if(st.flip)st.flip=[base+'/two',base+'/cur'];});}
  const harness=spec.harness||cfg.harness;env.CODEX_HOME=cfg.codexHome;
  const child=pty.spawn(cfg.python,[entry,harness,cfg.python,cfg.fake,harness,cfg.sid],{cols,rows,cwd:cfg.project,env});
  let chunks=null;
  child.onData(d=>{term.write(d);if(chunks)chunks.push([Date.now(),d]);});
  const flush=()=>new Promise(r=>term.write('',r));
  const cellsOf=y=>{const line=term.buffer.active.getLine(y),out=[];for(let x=0;x<cols;x++){const c=line?.getCell(x);out.push(!c?' ':c.getWidth()===0?'':c.getChars()||' ');}return out;};
  const screen=async()=>{await flush();return Array.from({length:rows},(_,y)=>cellsOf(y).join('').trimEnd());};
  const until=async(test,what,ms=60000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return s;await sleep(40);}
    throw Error('never '+what+'\n'+(await screen()).join('\n'));};
  const has=word=>s=>s.some(line=>line.includes(word));
  const phases={};
  try{
    await until(has('READY_'+harness),'the native');
    await sleep(1500);
    for(const step of spec.steps){
      if(step.line!==undefined){child.write(step.line+'\r');await sleep(step.ms||500);}
      else if(step.raw!==undefined){child.write(step.raw);await sleep(step.ms||300);}
      else if(step.wait!==undefined)await sleep(step.wait);
      else if(step.resize){cols=step.resize[0];rows=step.resize[1];term.resize(cols,rows);child.resize(cols,rows);await sleep(step.ms||800);}
      else if(step.flip){fs.symlinkSync(step.flip[0],step.flip[1]+'.new');fs.renameSync(step.flip[1]+'.new',step.flip[1]);await sleep(step.ms||500);}
      else if(step.until){await until(has(step.until),step.until);}
      else if(step.evidence){const found=[];
        for(const pid of fs.readdirSync('/proc').filter(n=>/^\d+$/.test(n))){try{const e=fs.readFileSync('/proc/'+pid+'/environ','utf8').split('\0');
          if(e.includes('PFM_MARK='+mark)&&e.some(x=>x.startsWith('HEARTING_SHELL_GENERATION=')))found.push(e.find(x=>x.startsWith('HEARTING_SHELL_GENERATION=')));}catch{}}
        phases[step.evidence]={generations:found.sort()};}
      else if(step.rec){await sleep(step.settle??600);await flush();chunks=[];const t0=Date.now();await sleep(step.ms||2000);await flush();
        phases[step.rec]={t0,chunks,screen:await screen()};chunks=null;}
    }
  }catch(error){phases.error=String(error.stack||error);}
  finally{try{child.write('\x15quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
  return phases;
}
(async()=>{const out={};
  for(const spec of cfg.cases){fs.writeFileSync(cfg.policy,JSON.stringify({...cfg.settings,rule_motion:spec.motion||cfg.settings.rule_motion}),{mode:0o600});
    out[spec.name]=await runCase(spec);fs.writeFileSync(cfg.result,JSON.stringify(out));}
  fs.writeFileSync(cfg.result,JSON.stringify(out));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
"""


def cell_width(ch):
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 0 if unicodedata.combining(ch) else 1


def writes(chunks):
    """Every glyph the chunks write: (time ms, row, column, glyph, foreground)."""
    out = []
    row = col = None
    fg = None
    for at, chunk in chunks:
        for token in re.findall(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[78]|[^\x1b]', chunk):
            move = re.fullmatch(r'\x1b\[(\d+);(\d+)H', token)
            if move:
                row, col = int(move.group(1)), int(move.group(2))
                continue
            sgr = re.fullmatch(r'\x1b\[([0-9;]*)m', token)
            if sgr:
                codes = sgr.group(1).split(';') if sgr.group(1) else ['0']
                i = 0
                while i < len(codes):
                    c = codes[i]
                    if c in ('0', '', '39'):
                        fg = None
                    if c == '38' and i + 2 < len(codes) and codes[i + 1] == '5':
                        fg = int(codes[i + 2]); i += 2
                    elif c == '38' and i + 4 < len(codes) and codes[i + 1] == '2':
                        fg = '#' + ''.join('%02x' % int(v) for v in codes[i + 2:i + 5]); i += 4
                    i += 1
                continue
            if token.startswith('\x1b') or token in '\r\n':
                continue
            if row is None:
                continue
            out.append((at, row, col, token, fg)); col += max(1, cell_width(token))
    return out


def motion(phase):
    """What moved in a recording: the frame/rule cells whose color changed (cells with two or more colors among their
    writes, over `─│┌┐└┘`), how many changes, and the head spinner's distinct glyphs."""
    seen, changes, spin = {}, 0, set()
    for _at, row, col, ch, fg in writes(phase['chunks']):
        if ch in SPIN:
            spin.add(ch)
        if ch in '─│┌┐└┘':
            old = seen.get((row, col), 'none')
            if old != 'none' and old != fg:
                changes += 1
            seen[(row, col)] = fg
    colors = {}
    for _at, row, col, ch, fg in writes(phase['chunks']):
        if ch in '─│┌┐└┘':
            colors.setdefault((row, col), set()).add(fg)
    return {'cells': sum(1 for v in colors.values() if len(v) > 1), 'changes': changes, 'spin': len(spin),
            'bytes': sum(len(c[1].encode()) for c in phase['chunks'])}


def run_case(name, motion_kind='scanners', steps=(), **extra):
    return dict(name=name, motion=motion_kind, steps=list(steps), **extra)


WORKING = [{'line': 'rec:busy'}, {'rec': 'working', 'ms': 2000}]
ONE = [{'line': 'ev:prompt'}, {'line': 'rec:busy'}]
# A Stop hook that keeps the turn going: the hooks said stop, the native's record says busy again, and no prompt event came.
CONTINUED = [{'line': 'ev:stop'}, {'line': 'rec:idle'}, {'wait': 700}, {'line': 'rec:busy'}]
CASES = [
    # (a) a plain start: still while idle, then the native's own record says it runs (the hooks' state stays idle: no prompt event).
    run_case('a-start', steps=[{'rec': 'idle', 'ms': 1500, 'expect': 'still'}, *WORKING]),
    # (c) the hooks said stop; the native's record says busy, with no prompt event in between.
    run_case('c-stop-idle-busy', steps=[*ONE, *CONTINUED, {'rec': 'continued', 'ms': 2000}]),
    # (d) /compact: the hooks' start with the source `compact` (the shell's own turn reading starts over), then running.
    run_case('d-compact', steps=[*ONE, {'line': 'ev:start:compact'}, {'rec': 'after-compact', 'ms': 2000}, *CONTINUED,
                                 {'rec': 'after-compact-continued', 'ms': 2000}]),
    # (e) a drag over the native area (v4.64.1's selection), then running.
    run_case('e-drag', steps=[{'line': 'rec:busy'}, {'raw': '\x1b[<0;10;10M'}, {'raw': '\x1b[<32;30;10M'}, {'raw': '\x1b[<0;30;10m'},
                              {'rec': 'after-drag', 'ms': 2000}]),
    # (b) The whole chain the person had, in one window, under each motion: a hand-over to a new host generation (the installed link
    # moved while the session was idle), a prompt, a repainting native, /compact, a Stop hook that keeps the turn going (no prompt
    # event), the native's record busy; later still.
    run_case('z-chain', releases=True, steps=[{'flip': True, 'ms': 5000}, {'evidence': 'after-link-moved', 'expect': 'generation-1'}, *ONE, {'line': 'spin:on'}, {'rec': 'chain-1', 'ms': 2000},
                                              {'line': 'ev:start:compact'}, {'rec': 'chain-2-compact', 'ms': 2000}, *CONTINUED,
                                              {'rec': 'chain-3-continued', 'ms': 2000}, {'line': 'ev:stop'}, {'rec': 'chain-4-stop-again', 'ms': 2000},
                                              {'wait': 6000}, {'rec': 'chain-5-later', 'ms': 2000}]),
    run_case('z-chain-24bit', colorterm=True, releases=True, steps=[{'flip': True, 'ms': 5000}, *ONE, {'line': 'spin:on'},
                                              {'line': 'ev:start:compact'}, *CONTINUED, {'rec': 'chain-continued', 'ms': 2000}]),
    run_case('z-chain-rule', motion_kind='rule', releases=True, steps=[{'flip': True, 'ms': 5000}, *ONE, {'line': 'spin:on'},
                                              {'line': 'ev:start:compact'}, *CONTINUED, {'rec': 'chain-continued', 'ms': 2000}]),
    run_case('z-chain-border', motion_kind='border', releases=True, steps=[{'flip': True, 'ms': 5000}, *ONE, {'line': 'spin:on'},
                                              {'line': 'ev:start:compact'}, *CONTINUED, {'rec': 'chain-continued', 'ms': 2000}]),
    # Codex: its turn comes from the rollout (the hooks say prompt and stop only). A turn the rollout shows starting after the stop
    # runs the frame again.
    run_case('x-codex', harness='codex', steps=[{'line': 'ev:prompt'}, {'line': 'roll:task_started:t1'}, {'rec': 'prompt-working', 'ms': 2000},
                                                {'line': 'roll:task_complete:t1'}, {'line': 'ev:stop'}, {'line': 'roll:task_started:t2'},
                                                {'rec': 'after-stop-new-turn', 'ms': 2000}]),
    # (f) fewer than 256 colors: the frame stands still by design (`borderColors < 256`); the head's spinner still turns.
    run_case('f-eight-colors', term='xterm', steps=[{'line': 'rec:busy'}, {'rec': 'eight', 'ms': 2000, 'expect': 'frame-still'}]),
    # (g) a fold and open, a resize.
    run_case('g-fold-open', steps=[{'line': 'rec:busy'}, {'resize': [90, 24]}, {'resize': [170, 46]}, {'rec': 'after-fold', 'ms': 2000}]),
]


class MotionCasesPtyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hmc-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('shell_motion_cases_isolation', ROOT / 'tools/run-tests.py')
        runner = importlib.util.module_from_spec(spec); sys.modules[spec.name] = runner; spec.loader.exec_module(runner)
        cls.env = runner.build_isolated_env(cls.root, repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR'] = str(cls.root / 'native-claude')
        cls.env['FLEET_NOW_LANG'] = 'ko'
        cls.env.pop('COLORTERM', None); cls.env.pop('NO_COLOR', None)
        cls.policy = Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME']) / '.config') / 'hearting/session-shell.json'
        cls.policy.parent.mkdir(parents=True, exist_ok=True)
        for name in ('npm-cache', 'node-gyp'):
            cls.env['npm_config_' + ('cache' if name == 'npm-cache' else 'devdir')] = str(cls.root / name)
        cls.env['npm_config_userconfig'] = str(cls.root / 'npmrc'); (cls.root / 'npmrc').write_text('')
        sys.path[:0] = [str(ROOT / 'tools/install'), str(ROOT / 'utilities')]
        import session_shell_install
        with mock.patch.dict(os.environ, cls.env, clear=True):
            assets = session_shell_install.prepare_assets()
        cls.env['NODE_PATH'] = str(assets / 'node_modules')
        cls.fake = cls.root / 'native.py'; cls.fake.write_text(FAKE_NATIVE)
        cls.launcher = cls.root / 'launcher.cjs'; cls.launcher.write_text(LAUNCHER)
        cls.driver = cls.root / 'driver.cjs'; cls.driver.write_text(DRIVER)

    def run_cases(self, cases, settings=None):
        case = self.root / ('run-%d' % len(list(self.root.glob('run-*')))); case.mkdir(mode=0o700)
        project = case / 'project'; project.mkdir()
        backend = case / 'backend'
        backend.write_text(BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'), tools=str(ROOT / 'tools'), sid=SID))
        backend.chmod(0o700)
        config = {'harness': 'claude', 'sid': SID, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'project': str(project), 'codexHome': str(self.root / 'native-codex'),
                  'result': str(case / 'result.json'), 'policy': str(self.policy), 'cases': cases,
                  'settings': settings or {'enabled': True, 'layout': 'side', 'position': 'right', 'side_width': 59, 'theme': 'terminal',
                                           'shade': 'none', 'rule_motion': 'scanners'}}
        (case / 'config.json').write_text(json.dumps(config))
        env = dict(self.env, LOOP_CONFIG=str(case / 'config.json'))
        child = subprocess.Popen([self.node, str(self.driver)], env=env, cwd=project, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _out, err = child.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM); child.communicate(timeout=10); raise
        diagnostic = (case / 'display-errors').read_text() if (case / 'display-errors').exists() else ''
        self.assertEqual(child.returncode, 0, err + diagnostic)
        return json.loads((case / 'result.json').read_text()), diagnostic

    def test_the_frame_keeps_moving_in_every_case(self):
        result, diagnostic = self.run_cases(CASES)
        table, failures = {}, []
        for spec in CASES:
            phases = result.get(spec['name'], {})
            table[spec['name']] = {k: (motion(v) if isinstance(v, dict) and 'chunks' in v else v) for k, v in phases.items()}
            if 'error' in phases:
                failures.append((spec['name'], phases['error'][:300]))
            for step in spec['steps']:
                key = step.get('rec') or step.get('evidence')
                if not key:
                    continue
                got, expect = table[spec['name']].get(key), step.get('expect', 'moves')
                if got is None:
                    failures.append((spec['name'], key, 'not recorded'))
                elif expect == 'generation-1':
                    # The hand-over really happened: the window's host is the second generation.
                    if 'HEARTING_SHELL_GENERATION=1' not in got['generations']:
                        failures.append((spec['name'], key, got))
                elif expect == 'still':
                    if got['cells'] or got['spin']:
                        failures.append((spec['name'], key, got))
                elif expect == 'frame-still':
                    if got['cells'] or got['spin'] < 4:
                        failures.append((spec['name'], key, got))
                else:
                    # Running: the moving cells change color (the frame, or under `rule` the head rule's light) and the
                    # head's spinner turns, within the two seconds the native's own record says it runs.
                    need = 15 if spec['motion'] == 'rule' else 40
                    if got['cells'] < need or got['changes'] < need or got['spin'] < 4:
                        failures.append((spec['name'], key, got))
        print(json.dumps({'event': 'session-shell-motion-cases', 'table': table}, ensure_ascii=False), flush=True)
        self.assertEqual(failures, [], json.dumps(table))


if __name__ == '__main__':
    unittest.main()
