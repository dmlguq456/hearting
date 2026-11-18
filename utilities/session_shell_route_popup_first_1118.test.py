"""11-18 (the routes popup opened slowly: a press waited for Fleet's whole collection, 7-22 s): the popup comes up at once and fills later.

A press on the ROUTE section used to show the reading mark until the backend finished `collect_all`. Now the
popup stands at once from what the host already holds, and its own reading replaces it when it arrives:

1. The first time (or after the panel's route values changed, or for another route unfolded): the panel's own route
   box laid at the popup's width (`panelView.routesBody`, one drawing with the panel's), the pressed route first.
2. Opened again for the same route, at the same width, while the panel's route values have not changed: the table
   read last (Fleet's own drawing), shown at once.
3. Either says it is not this reading's, at the end of the popup's title (`HH:MM에 읽은 내용 · 다시 읽는 중`); a reading
   that fails or is cut at 12 s changes the words (`다시 읽지 못함`) and leaves the rows; the next success removes them.
4. Where the table is replaced, a person who scrolled keeps the route at the top of the window; one who pressed or
   Tabbed to a route sees it come into view.

The collection is given a synthetic delay (as a long `collect_all` is); the host runs in a private PTY. Nothing here
reaches a real account, session, artifact root or terminal.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import fcntl
import importlib.util
import json
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHOW_FRAMES = False

FAKE_NATIVE = r"""
import json,os,select,socket,sys,time,tty
harness,sid=sys.argv[1],sys.argv[2]
def event(kind):
    raw=open('/proc/self/stat').read();start=raw.rsplit(') ',1)[1].split()[19]
    req={'op':'event','event':kind,'harness':harness,'sid':sid,'pid':os.getpid(),'start':start,
         'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET']);c.sendall((json.dumps(req)+'\n').encode());c.recv(8192)
tty.setraw(0)
event('start')
pending='';said='READY_'+harness;later=[];seen=[]
def screen():
    print('\x1b[2J\x1b[H'+said+'\r\n'+''.join(s+'\r\n' for s in seen[-14:])+'❯ '+''.join('^'+chr(ord(c)+64) if ord(c)<32 else c for c in pending)+'\r\n',end='',flush=True)
if harness!='claude':print('\x1b[?1000h\x1b[?1006h',end='',flush=True)
screen()
while True:
    wait=max(0,later[0][0]-time.monotonic()) if later else None
    ready,_,_=select.select([0],[],[],wait)
    while later and later[0][0]<=time.monotonic():seen.append(later.pop(0)[1]);screen()
    if not ready:continue
    data=os.read(0,65536)
    if not data:break
    text=data.decode(errors='replace')
    if '\x1b[<' in text:
        seen.append('NATIVE_ESC_'+text.replace('\x1b','^'));text=''.join(part.split('M',1)[-1].split('m',1)[-1] if part.startswith('[<') else part for part in text.split('\x1b'))
    pending+=text
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        if line=='quit':raise SystemExit
        if line=='later':later=[(time.monotonic()+0.5+i*0.02,'LATER_%02d'%i) for i in range(12)]
        if line=='busy':event('prompt')
        if line=='calm':event('stop')
        said='DONE_'+line
    screen()
"""

LAUNCHER = r"""
const fs=require('node:fs');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
if(process.argv.some(arg=>arg.endsWith('/session-shell/host.cjs'))){
 process.env.HEARTING_SHELL_PYTHON=cfg.backend;
 process.on('uncaughtException',error=>fs.appendFileSync(cfg.diagnostic,String(error?.stack||error)+'\n'));
}
"""


# The backend as installed, with one fixture seam: Fleet's collection (a view model from `vm.json`), delayed by the
# case's `control/vm-delay` seconds and held while `vm.json.hang` exists.
BACKEND = r"""#!{python}
import json,os,sys,time
sys.path.insert(0,{utilities!r});sys.path.insert(0,{tools!r})
import session_shell as m
from fleet import session_tags
OWN={sid!r}
session_tags.assigned_tag=lambda harness,sid:'e9' if sid==OWN else None
C={control!r}
LOGGED=[]
def number(name):
    try:return float(open(C+'/'+name).read())
    except (OSError,ValueError):return 0.0
def vm(harness,sid,kind):
    if not LOGGED:
        LOGGED.append(1);open(C+'/spawns','a').write('%d\n'%os.getpid())     # one line for each process that collects
    open(C+'/collections','a').write('1\n')                                     # one line for each collection
    time.sleep(number('vm-delay'))
    while os.path.exists({vm!r}+'.hang'):time.sleep(0.2)
    from fleet.explorer.view_model import ExplorerViewModel
    return ExplorerViewModel.from_mapping(json.load(open({vm!r})))
m._popup_view_model=vm
m.session_questions=lambda harness,sid,**_kw:[]
m._request_views=lambda harness,sid,now,errors=None:([],[])
raise SystemExit(m._backend())
"""

DRIVER = r"""
const fs=require('node:fs');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let cols=160,rows=40;
const term=new Terminal({cols,rows,allowProposedApi:true,scrollback:0});
term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const child=pty.spawn(cfg.python,[cfg.entry,cfg.harness,cfg.python,cfg.fake,cfg.harness,cfg.sid],{cols,rows,cwd:cfg.project,
  env:{...process.env,TERM:'xterm-256color',NODE_OPTIONS:'--require '+cfg.launcher}});
child.onData(d=>term.write(d));
const flush=()=>new Promise(r=>term.write('',r));
const cellsOf=y=>{const line=term.buffer.active.getLine(y),out=[];for(let x=0;x<cols;x++){const c=line?.getCell(x);out.push(!c?' ':c.getWidth()===0?'':c.getChars()||' ');}return out;};
const screen=async()=>{await flush();const grid=Array.from({length:rows},(_,y)=>cellsOf(y)),out=grid.map(c=>c.join('').trimEnd());out.grid=grid;return out;};
const at=async needle=>{await flush();for(let y=0;y<rows;y++){const c=cellsOf(y),where=[];let text='';
  c.forEach((ch,x)=>{for(const _ of ch)where.push(x);text+=ch;});const i=text.indexOf(needle);if(i>=0)return {x:where[i],y};}return null;};
const until=async(test,what,ms=20000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return {s,ms:Date.now()-began};await sleep(25);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>line.includes(word));
const press=async(x,y,ms=100)=>{child.write(`\x1b[<0;${x+1};${y+1}M\x1b[<0;${x+1};${y+1}m`);await sleep(ms);};
const key=async(bytes,ms=250)=>{child.write(bytes);await sleep(ms);};
// The popup's own cells (its columns, not the panel's beside it): `rows` from its top line to its bottom line.
const box=s=>{const top=s.findIndex(l=>l.includes('╭─')&&l.includes('✕ ─╮'));if(top<0)return null;
  const g=s.grid,left=g[top].indexOf('╭'),right=g[top].indexOf('╮',left),bottom=g.findIndex((c,i)=>i>top&&c[left]==='╰'&&c.includes('╯'));
  const rowsOf=g.slice(top,bottom+1).map(c=>c.slice(left,right+1).join(''));
  return {top,bottom,left,title:rowsOf[0],rows:rowsOf};};
const popupHas=word=>s=>{const b=box(s);return !!b&&b.rows.some(l=>l.includes(word));};
const title=s=>box(s)?box(s).title:'';
const staleWords=s=>/(에 읽은 내용 · 다시 읽는 중|다시 읽지 못함)/.test(title(s));
const reading=s=>title(s).includes('다시 읽는 중'),failedRead=s=>title(s).includes('다시 읽지 못함');
const unfolded=s=>{const b=box(s);return b?b.rows.find(l=>l.includes('▶'))||'':'';};
const control=(name,value='')=>fs.writeFileSync(cfg.control+'/'+name,String(value));
const write=(file,value)=>{fs.writeFileSync(file+'.new',value,{mode:0o600});fs.renameSync(file+'.new',file);};
const plain=s=>box(s)?box(s).rows.map(l=>l.trimEnd()):[];
const frames={},checks={};
const head=()=>at(cfg.routes+' ROUTE');
const opened=async()=>{const p=await head();await press(p.x+2,p.y,0);};
const close=async()=>{await key('\x1b',300);await until(s=>!box(s),'closed');};
// What the popup first shows: the panel's box (`PANEL_R…` titles) or Fleet's table (`FLEET_R…`).
const either=s=>popupHas('PANEL_R')(s)||popupHas('FLEET_R')(s);
const kind=s=>popupHas('FLEET_R')(s)?'fleet':popupHas('PANEL_R')(s)?'panel':'none';
const filled=s=>popupHas('FLEET_R')(s)&&!staleWords(s);
// A press, then the first content and then the full one: the two times of the request.
const measure=async(label,press_=opened)=>{
  const t0=Date.now();await press_();
  const first=await until(either,'a first content ('+label+')');const m={first_ms:Date.now()-t0,first_kind:kind(first.s),first_title:title(first.s),first_has_fleet:popupHas('FLEET_R')(first.s)};
  frames[label+'_first']=plain(first.s);
  const full=await until(filled,'the filled table ('+label+')');m.full_ms=Date.now()-t0;m.full_title=title(full.s);frames[label+'_full']=plain(full.s);
  return m;};
(async()=>{try{
  await until(s=>has('READY_'+cfg.harness)(s)&&has(cfg.routes+' ROUTE')(s)&&has('PANEL_R1')(s),'the panel with its routes');await sleep(700);
  const sc=cfg.scenario;
  if(sc==='one'||sc==='many'){
    // First opening, the collection slow: the panel's own box stands under a second, Fleet's table after the delay.
    control('vm-delay',cfg.delay);
    checks.first=await measure('first');
  }
  if(sc==='many'){
    // Closed and opened again for the same route: the table read last (Fleet's), at once.
    await close();control('vm-delay',cfg.delay);
    checks.again=await measure('again');
    // Another route's row pressed: not the table read for the first one's route, the panel's box.
    await close();control('vm-delay',cfg.delay);
    checks.other=await measure('other',async()=>{const p=await at('PANEL_R4');await press(p.x+2,p.y,0);});
    // The panel's route values changed since: the same route again is the panel's box.
    await close();
    const cache=JSON.parse(fs.readFileSync(cfg.cache,'utf8'));cache.observed_at=Date.now()/1000;cache.route_panels[cfg.key][4].title='PANEL_R5 RENAMED';write(cfg.cache,JSON.stringify(cache));
    await until(has('RENAMED'),'the panel renamed');await sleep(500);control('vm-delay',cfg.delay);
    checks.changed=await measure('changed',async()=>{const p=await at('PANEL_R4');await press(p.x+2,p.y,0);});
    await close();
  }
  if(sc==='hang'){
    // The collection does not come in 12 s: what stood first stays, the words say the reading failed; a later
    // reading (Tab) that succeeds takes them away.
    fs.writeFileSync(cfg.vm+'.hang','');
    const t0=Date.now();await opened();const first=await until(either,'a first content');checks.first_ms=Date.now()-t0;checks.first_kind=kind(first.s);
    checks.first_reading=reading(first.s);frames.hang_first=plain(first.s);
    await sleep(13500);let s=await screen();
    checks.after_kind=kind(s);checks.after_failed=failedRead(s);checks.after_unreadable=!!box(s)&&box(s).rows.slice(1).some(l=>l.includes(cfg.unreadable));checks.after_title=title(s);frames.hang_after=plain(s);
    fs.unlinkSync(cfg.vm+'.hang');
    await key('\t',500);if(!popupHas('FLEET_R')(await screen()))await key('\x1b[Z',500);
    const r=await until(filled,'the table after the reading that succeeded');checks.recovered_title=title(r.s);frames.hang_recovered=plain(r.s);
  }
  if(sc==='reopen_route'){
    // The routes popup opened again from its own first screen (a press on ROUTE in its bottom line): the rows that are
    // not a reading's own keep saying so, and when the readings do not come the words say that, not nothing.
    fs.writeFileSync(cfg.vm+'.hang','');
    await opened();let r=await until(either,'a first content');checks.first_reading=reading(r.s);
    const b=box(r.s),bottom=b.rows[b.rows.length-1],x=b.left+bottom.indexOf('ROUTE');
    checks.bottom_has_route=bottom.indexOf('ROUTE')>=0;
    await press(x+1,b.bottom,0);
    r=await until(s=>!!box(s),'the popup after ROUTE again');await sleep(400);const s1=await screen();
    checks.again_kind=kind(s1);checks.again_reading=reading(s1);checks.again_title=title(s1);frames.reopen_again=plain(s1);
    // The reading under way when it was opened again is dropped, the next one is cut at 12 s too.
    r=await until(failedRead,'the words that the reading failed',40000);
    checks.failed_kind=kind(r.s);checks.failed_title=title(r.s);frames.reopen_failed=plain(r.s);
    fs.unlinkSync(cfg.vm+'.hang');
  }
  if(sc==='place_a'||sc==='place_b'||sc==='place_c'){
    await resize(120,16);control('vm-delay',cfg.delay);
    const p=await head();await press(p.x+2,p.y,0);
    let r=await until(popupHas('PANEL_R'),'the panel box first');checks.first_kind=kind(r.s);frames.place_first=plain(r.s);
    checks.first_unfolded=unfolded(r.s);
    if(sc==='place_a'){await key('\x1b[6~',600);r=await screen();checks.moved=plain(r).join('\n')!==frames.place_first.join('\n');frames.place_moved=plain(r);}
    if(sc==='place_b'){for(let i=0;i<8;i++)await key('\t',60);await sleep(300);checks.tabbed=true;}
    // Fleet's table (the last reading of the Tab case, which waits for its own second reading).
    // The Tab case waits for its own second reading by what it shows, not by a fixed margin (a slow start missed it).
    if(sc==='place_b')r=await until(s=>filled(s)&&unfolded(s).includes('FLEET_R10'),'the tabbed route unfolded',cfg.delay*3000+8000);
    else{r=await until(filled,'the table',cfg.delay*3000+8000);await sleep(cfg.delay*1000+800);}
    r=await screen();
    checks.table_kind=kind(r);checks.table_unfolded=unfolded(r);checks.table_top=plain(r)[1]||'';checks.table_stale=staleWords(r);frames.place_table=plain(r);
  }
  if(sc==='narrow'){
    await resize(60,24);control('vm-delay',cfg.delay);
    await key('\x1d',400);await until(s=>!!box(s),'a popup by Ctrl+]');
    for(let i=0;i<6&&!title(await screen()).includes('ROUTE');i++)await key('\x1b[C',300);
    const r=await until(s=>title(s).includes('ROUTE')&&reading(s)||staleWords(s),'the routes popup at 60 columns');
    checks.title=title(r.s);checks.kind=kind(r.s);frames.narrow=plain(r.s);
  }
  if(['keys','held_dies','refresh_reopen','reread_fails'].includes(sc)){
    const lines=f=>fs.existsSync(cfg.control+'/'+f)?fs.readFileSync(cfg.control+'/'+f,'utf8').split('\n').filter(Boolean):[];
    const spawned=()=>lines('spawns').map(Number),collections=()=>lines('collections').length;
    const isAlive=pid=>{try{process.kill(pid,0);return true;}catch{return false;}};
    const live=()=>spawned().filter(isAlive).length;
    const unfoldedNow=s=>unfolded(s).match(/FLEET_R\d+/)?.[0]||'';
    // What the page of a route draws that its table does not: the stage frame.
    const paged=s=>popupHas('║')(s);
    const rename=n=>{const cache=JSON.parse(fs.readFileSync(cfg.cache,'utf8'));cache.observed_at=Date.now()/1000;cache.route_panels[cfg.key][0].title='PANEL_R1 RENAMED'+n;write(cfg.cache,JSON.stringify(cache));};
    control('vm-delay',cfg.delay);
    if(sc==='keys'){
      // The table is collected once; the keys that follow draw from that collection, in the one backend held while the popup stands.
      await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      checks.opened={spawned:spawned().length,collections:collections(),live:live()};
      const first=unfoldedNow(await screen());checks.first_open=first;
      const steps=[];
      // (One route has none to step to; its page is the key.)
      for(const want of cfg.routes>1?['FLEET_R'+(Number(first.slice(7))+1),first]:[]){
        const t0=Date.now();await key(want===first?'\x1b[A':'\x1b[B',0);const r=await until(s=>unfoldedNow(s)===want,'the unfolded route '+want);
        steps.push({want,ms:Date.now()-t0,at:r.ms});
      }
      checks.down_up=steps;frames.keys_unfolded=plain(await screen());
      const t1=Date.now();await key('\r',0);const page=await until(paged,'the route page');checks.page_ms=Date.now()-t1;frames.keys_page=plain(page.s);
      const t2=Date.now();await key('\r',0);await until(s=>!paged(s)&&!!unfoldedNow(s),'the table again');checks.back_ms=Date.now()-t2;
      checks.after_keys={spawned:spawned().length,collections:collections(),live:live(),title:title(await screen())};
      await close();await sleep(1500);checks.closed_live=live();
    }
    if(sc==='held_dies'){
      await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      const held=spawned();checks.held=held.length;
      for(const pid of held)try{process.kill(pid,'SIGKILL');}catch{}
      await sleep(300);
      const first=unfoldedNow(await screen());
      const t0=Date.now();await key('\x1b[B',0);const r=await until(s=>unfoldedNow(s)!==first&&!!unfoldedNow(s),'the next route unfolded by a backend of its own',cfg.delay*1000+12000);
      checks.dies={ms:Date.now()-t0,failed:failedRead(r.s),title:title(r.s),spawned:spawned().length,collections:collections()};
      // The backend that came for it is held in turn: the next key starts nothing and collects nothing.
      const before=spawned().length;await key('\x1b[A',0);await until(s=>unfoldedNow(s)===first,'the first route again',cfg.delay*1000+12000);
      checks.after={new_processes:spawned().length-before,collections:collections()};
      await close();await sleep(1500);checks.closed_live=live();
    }
    if(sc==='refresh_reopen'){
      // The panel's route values change under the popup: the reading it starts is kept under the new values, so a popup
      // opened again after it comes with that table (Fleet's), not the panel's box.
      await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      rename(1);await until(has('R1 RENAMED1'),'the panel renamed');
      control('vm-delay',0);
      await sleep(1500);
      await close();await opened();
      const r=await until(either,'a first content');checks.reopen={kind:kind(r.s),title:title(r.s)};frames.reopen=plain(r.s);
    }
    if(sc==='reread_fails'){
      await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      control('vm-delay',0);fs.writeFileSync(cfg.vm+'.hang','');rename(1);
      await until(has('R1 RENAMED1'),'the panel renamed');
      const r=await until(failedRead,'the words that the reread failed',20000);checks.failed={title:title(r.s),rows:popupHas('FLEET_R')(r.s)};frames.reread_failed=plain(r.s);
      fs.unlinkSync(cfg.vm+'.hang');rename(2);
      const ok=await until(s=>filled(s)&&popupHas('FLEET_R')(s),'the words gone after a read that succeeded',20000);checks.recovered={title:title(ok.s)};frames.reread_recovered=plain(ok.s);
    }
  }
  fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
async function resize(c,r){cols=c;rows=r;term.resize(c,r);child.resize(c,r);await sleep(500);}
"""


STAGES = ('plan', 'plan-check', 'execute', 'impl-review', 'test', 'report')


def route_row(rid, title, state='working', staged=False):
    row = {'id': rid, 'title': title, 'state': state, 'worker_stage': 'one-shot', 'shape': 'one-shot-owner',
           'stages': [{'name': 'one-shot', 'st': 'failed' if state == 'dead' else 'active'}], 'harness': 'claude',
           'model': 'Opus 5.5', 'effort': 'high'}
    if staged:  # the producer's staged route (what a real route's row carries), for the pictures of the first screen
        row.update(shape='staged', worker_stage='execute', capability='autopilot-code', mode='dev', intensity='standard',
                   chain=[{'name': 'frame', 'state': 'done'}, {'name': 'code', 'state': 'active'}],
                   stages=[{'name': n, 'st': 'done' if i < 2 else 'active' if i == 2 else 'pending'} for i, n in enumerate(STAGES)])
    return row


def fleet_route(rid, title, state='working', staged=False):
    row = {'id': rid, 'session_tag': 'e9', 'repo': 'project', 'title': title, 'state': state, 'harness': 'claude',
           'model': 'Opus 5.5', 'effort': 'high', 'owner_gauge': [40, 0.2], 'age': '23m', 'capability': 'autopilot-code',
           'mode': 'dev', 'intensity': 'quick', 'stages': [{'name': 'one-shot', 'st': 'failed' if state == 'dead' else 'active'}],
           'worker_stage': 'one-shot'}
    if staged:
        row.update(intensity='standard', worker_stage='execute', stages=[
            {'name': n, 'st': 'done' if i < 2 else 'active' if i == 2 else 'pending', 'verdict': 'PASS' if i < 2 else '',
             'when': '%dm' % (30 - 10 * i) if i < 3 else '', 'unit': 'dev/backend' if i == 2 else '', 'out': 'artifacts/%s.md' % n if i < 2 else ''}
            for i, n in enumerate(STAGES)])
    return row


def rid(n):
    return 'rt-a%015d' % n


SIDS = {'claude': '00000000-0000-4000-8000-0000000000d1', 'codex': '00000000-0000-4000-8000-0000000000d2',
        'opencode': 'ses_routepopupfirstfixture'}


class RoutePopupFirstTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hrpf-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('route_popup_first_isolation', ROOT / 'tools/run-tests.py')
        runner = importlib.util.module_from_spec(spec); sys.modules[spec.name] = runner; spec.loader.exec_module(runner)
        cls.env = runner.build_isolated_env(cls.root, repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR'] = str(cls.root / 'native-claude')
        cls.env['FLEET_NOW_LANG'] = 'ko'
        policy = Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME']) / '.config') / 'hearting/session-shell.json'
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({'enabled': True, 'layout': 'side', 'side_width': 40, 'side_min_columns': 132}))
        policy.chmod(0o600)
        cls.env['npm_config_cache'] = str(cls.root / 'npm-cache')
        cls.env['npm_config_devdir'] = str(cls.root / 'node-gyp')
        cls.env['npm_config_userconfig'] = str(cls.root / 'npmrc'); (cls.root / 'npmrc').write_text('')
        sys.path[:0] = [str(ROOT / 'tools/install'), str(ROOT / 'utilities')]
        import session_shell_install
        with mock.patch.dict(os.environ, cls.env, clear=True):
            assets = session_shell_install.prepare_assets()
        cls.env['NODE_PATH'] = str(assets / 'node_modules')
        # The shared cache's producer would rewrite the seeded values from this fixture's real state (it has
        # none): its lock is held for the class, so the seeded cache stays.
        cache = Path(cls.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True); cache.chmod(0o700)
        cls.lock = os.open(cache / '.projection.lock', os.O_CREAT | os.O_RDWR, 0o600)
        cls.addClassCleanup(os.close, cls.lock)
        fcntl.flock(cls.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cls.fake = cls.root / 'native.py'; cls.fake.write_text(FAKE_NATIVE)
        cls.launcher = cls.root / 'launcher.cjs'; cls.launcher.write_text(LAUNCHER)
        cls.driver = cls.root / 'driver.cjs'; cls.driver.write_text(DRIVER)
        catalog = json.loads((ROOT / 'tools/i18n/messages.json').read_text())['messages']
        cls.unreadable = catalog['fleet.output.unreadable']['ko']
        cls.count = 0

    def run_case(self, harness, scenario, routes, delay=3, dead=None, staged=False):
        """One private PTY run; returns what the driver read (its `frames` and `checks`)."""
        type(self).count += 1
        case = self.root / ('%s-%s-%d' % (harness, scenario, type(self).count)); case.mkdir(mode=0o700)
        control = case / 'control'; control.mkdir()
        project = case / 'project'; project.mkdir()
        sid = SIDS[harness]
        key = harness + '\0' + sid
        target = Path(self.env['XDG_STATE_HOME']) / 'hearting/human-waits/projection.json'
        value = {'schema_version': 3, 'items': [], 'by_session': {}, 'total': 0, 'older': 0, 'complete': True,
                 'route_panels': {}, 'route_status': {}, 'route_sessions': {}}
        value.update(observed_at=time.time())
        # The failed route (the third, unless said) is the one the popup unfolds when no route is pressed.
        dead = dead or (1 if routes == 1 else 3)
        states = ['dead' if n == dead else 'working' for n in range(1, routes + 1)]
        value['route_panels'][key] = [route_row(rid(n), 'PANEL_R%d 제목' % n, states[n - 1], staged) for n in range(1, routes + 1)]
        value['route_status'][key] = 'route %d' % routes; value['route_sessions'][key] = time.time() + 3600
        target.write_text(json.dumps(value)); target.chmod(0o600)
        vm = case / 'vm.json'
        vm.write_text(json.dumps({'repos': [{'id': 'project', 'name': 'project'}], 'system': {},
            'routes': [fleet_route(rid(n), 'FLEET_R%d 제목' % n, states[n - 1], staged) for n in range(1, routes + 1)],
            'sessions': [{'tag': 'e9', 'repo_id': 'project', 'harness': harness, 'model': 'Opus 5.5', 'effort': 'high', 'context': 46,
                          'window': 200000, 'state': 'working', 'title': '이 창', 'now': '그리는 중', 'age': '23m'}]}, ensure_ascii=False))
        backend = case / 'backend'
        backend.write_text(BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'), tools=str(ROOT / 'tools'),
                                          sid=sid, vm=str(vm), control=str(control)))
        backend.chmod(0o700)
        config = {'harness': harness, 'sid': sid, 'key': key, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'cache': str(target), 'vm': str(vm),
                  'project': str(project), 'result': str(case / 'result.json'), 'control': str(control),
                  'scenario': scenario, 'routes': routes, 'delay': delay, 'unreadable': self.unreadable}
        (case / 'config.json').write_text(json.dumps(config))
        env = dict(self.env, LOOP_CONFIG=str(case / 'config.json'))
        child = subprocess.Popen([self.node, str(self.driver)], env=env, cwd=project, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _out, err = child.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM); child.communicate(timeout=10); raise
        diagnostic = (case / 'display-errors').read_text() if (case / 'display-errors').exists() else ''
        self.assertEqual(child.returncode, 0, err + diagnostic)
        result = json.loads((case / 'result.json').read_text())
        if SHOW_FRAMES:  # `--frames`: the frames this run read, for a person to look at
            print('FRAMES ' + harness + ' ' + scenario + ' ' + json.dumps(result, ensure_ascii=False), flush=True)
        return result

    def first_screen(self, harness, routes):
        result = self.run_case(harness, 'one', routes)
        c = result['checks']['first']
        print(json.dumps({'event': 'route-popup-first-pty', 'harness': harness, 'routes': routes,
                          'first_ms': c['first_ms'], 'full_ms': c['full_ms']}), flush=True)
        # The panel's box is up under a second, with Fleet's table not yet there and the words that say so; Fleet's
        # table comes after the collection's delay and the words go.
        self.assertLess(c['first_ms'], 1000, c)
        self.assertEqual(c['first_kind'], 'panel', c)
        self.assertFalse(c['first_has_fleet'], c)
        self.assertIn('다시 읽는 중', c['first_title'], c)
        self.assertIn('에 읽은 내용', c['first_title'], c)
        self.assertGreaterEqual(c['full_ms'], 2800, c)
        self.assertNotIn('읽은 내용', c['full_title'], c)
        frames = result['frames']
        # The panel's box carries each route's title (`PANEL_R…`), at the popup's width, with no box side of the panel's.
        first = '\n'.join(frames['first_first'])
        for n in range(1, routes + 1):
            self.assertIn('PANEL_R%d 제목' % n, first)
        self.assertTrue(all(row.startswith('│') or row[:1] == '╭' or row[:1] == '╰' for row in frames['first_first']), first)
        return result

    def test_claude_one_route(self):
        self.first_screen('claude', 1)

    def test_claude_five_routes(self):
        self.first_screen('claude', 5)

    def test_codex(self):
        self.first_screen('codex', 5)

    def test_opencode(self):
        self.first_screen('opencode', 5)

    def test_reopened_for_the_same_route_is_the_table_read_last_and_another_route_or_changed_values_are_not(self):
        c = self.run_case('claude', 'many', 5)['checks']
        # The same route again: Fleet's table at once (read last), the words saying so; Fleet's again comes after.
        self.assertLess(c['again']['first_ms'], 1000, c)
        self.assertEqual(c['again']['first_kind'], 'fleet', c)
        self.assertIn('에 읽은 내용', c['again']['first_title'], c)
        # Another route pressed: the table read last was for the third route unfolded; this one is the panel's box.
        self.assertLess(c['other']['first_ms'], 1000, c)
        self.assertEqual(c['other']['first_kind'], 'panel', c)
        self.assertFalse(c['other']['first_has_fleet'], c)
        # The panel's route values changed since the table was read: the panel's box again, even for the same route.
        self.assertLess(c['changed']['first_ms'], 1000, c)
        self.assertEqual(c['changed']['first_kind'], 'panel', c)
        self.assertGreaterEqual(c['changed']['full_ms'], 2800, c)
        print(json.dumps({'event': 'route-popup-first-pty', 'scenario': 'reopen', 'again_first_ms': c['again']['first_ms'],
                          'other_first_ms': c['other']['first_ms'], 'changed_first_ms': c['changed']['first_ms']}), flush=True)

    def test_a_collection_that_does_not_come_leaves_the_first_screen_and_says_so_until_a_reading_succeeds(self):
        c = self.run_case('claude', 'hang', 2, delay=0)['checks']
        self.assertLess(c['first_ms'], 1000, c)
        self.assertEqual(c['first_kind'], 'panel', c)
        self.assertTrue(c['first_reading'], c)
        # Past the host's 12 seconds: the rows stay, they are not replaced by the popup's `could not read`.
        self.assertEqual(c['after_kind'], 'panel', c)
        self.assertTrue(c['after_failed'], c)
        self.assertFalse(c['after_unreadable'], c)
        # A later reading that succeeds: Fleet's table, no words.
        self.assertNotIn('읽은 내용', c['recovered_title'], c)
        self.assertNotIn('읽지 못함', c['recovered_title'], c)

    def test_opening_the_routes_popup_again_from_its_first_screen_keeps_saying_the_rows_are_not_read(self):
        # Review of #1730: a press on ROUTE in the popup's bottom line while the first screen stands carried its rows
        # over without their words, and a reading that then failed changed nothing.
        c = self.run_case('claude', 'reopen_route', 2, delay=0)['checks']
        self.assertTrue(c['first_reading'], c)
        self.assertTrue(c['bottom_has_route'], c)
        self.assertEqual(c['again_kind'], 'panel', c)
        self.assertTrue(c['again_reading'], c)
        self.assertIn('다시 읽지 못함', c['failed_title'], c)
        self.assertEqual(c['failed_kind'], 'panel', c)

    def test_the_place_a_person_reads_at_stays_when_the_table_is_replaced(self):
        # (a) scrolled a page on the first screen: the route at the top of the window stays; the table does not pull
        # the window back to the unfolded route. (b) Tab to the next route: that route comes into view. (c) nothing:
        # the unfolded route stands in the window.
        # Twenty routes in a window of sixteen rows: neither the panel's box nor Fleet's table fits it.
        a = self.run_case('claude', 'place_a', 20, dead=2)
        b = self.run_case('claude', 'place_b', 20, dead=2)
        c = self.run_case('claude', 'place_c', 20, dead=9)
        ca, cb, cc = a['checks'], b['checks'], c['checks']
        self.assertEqual(ca['first_kind'], 'panel', ca)
        self.assertEqual(ca['table_kind'], 'fleet', ca)
        self.assertTrue(ca['moved'], ca)
        # (a) the fifth route was at the top when the table came: it is where the table's window starts (its first or
        # second row), the unfolded (second) route is not pulled back into the window.
        self.assertRegex(ca['table_top'], r'FLEET_R[56] ', ca)
        self.assertEqual(ca['table_unfolded'], '', ca)
        # (b) eight Tabs: the tenth route is unfolded and in the window though the window stood at the top.
        self.assertIn('FLEET_R10', cb['table_unfolded'], cb)
        # (c) nothing pressed: the unfolded (ninth) route is brought into the window.
        self.assertIn('FLEET_R9', cc['table_unfolded'], cc)
        for checks in (ca, cb, cc):
            self.assertFalse(checks['table_stale'], checks)

    def test_route_keys_and_the_page_draw_from_the_held_collection_and_its_backend_ends_with_the_popup(self):
        """The popup's ↑↓ and its Enter page draw from the collection the table was read from, in the one backend held while
        the popup stands: no process of their own, no second collection, and nothing left behind when it closes."""
        for routes in (1, 5):
            c = self.run_case('claude', 'keys', routes, staged=True)['checks']
            # The main assertions do not move with the machine's load: one backend, one collection, none left.
            self.assertEqual(c['opened']['spawned'], 1, c)
            self.assertEqual(c['after_keys']['spawned'], 1, c)
            self.assertEqual(c['opened']['collections'], 1, c)
            self.assertEqual(c['after_keys']['collections'], 1, c)
            self.assertEqual(c['closed_live'], 0, c)
            self.assertNotIn('읽은 내용', c['after_keys']['title'], c)
            print(json.dumps({'event': 'route-popup-keys-pty', 'routes': routes, 'steps_ms': c['down_up'], 'page_ms': c['page_ms'],
                              'back_ms': c['back_ms']}), flush=True)
            # The request's own bar: the key and the page are drawn within a second (this includes the PTY's own drawing).
            for step in c['down_up']:
                self.assertLess(step['ms'], 1000, c)
            self.assertLess(c['page_ms'], 1000, c)

    def test_a_held_backend_that_dies_is_replaced_by_the_next_key_without_the_words(self):
        c = self.run_case('claude', 'held_dies', 5, staged=True)['checks']
        self.assertEqual(c['held'], 1, c)
        self.assertFalse(c['dies']['failed'], c)
        self.assertNotIn('읽지 못함', c['dies']['title'], c)
        # The key was read by a backend of its own (it collects: the one that died took its collection with it).
        self.assertEqual(c['dies']['spawned'], 2, c)
        self.assertEqual(c['dies']['collections'], 2, c)
        # That backend is the one held from then on: the next key starts nothing and collects nothing.
        self.assertEqual(c['after']['new_processes'], 0, c)
        self.assertEqual(c['after']['collections'], 2, c)
        self.assertEqual(c['closed_live'], 0, c)

    def test_a_popup_opened_again_after_the_panels_route_values_changed_under_it_comes_with_fleets_table(self):
        # Review of #1730 (a): the reading that the changed values started was kept under the values before them, so the
        # popup opened again after it was the panel's box. The key is in place before the reading starts now.
        c = self.run_case('claude', 'refresh_reopen', 5, delay=1)['checks']
        self.assertEqual(c['reopen']['kind'], 'fleet', c)
        self.assertIn('에 읽은 내용', c['reopen']['title'], c)

    def test_a_table_read_before_that_could_not_be_read_again_says_so_and_the_next_success_takes_it_away(self):
        c = self.run_case('claude', 'reread_fails', 5, delay=1)['checks']
        self.assertIn('다시 읽지 못함', c['failed']['title'], c)
        self.assertTrue(c['failed']['rows'], c)       # the rows stay
        self.assertNotIn('읽지 못함', c['recovered']['title'], c)
        self.assertNotIn('읽은 내용', c['recovered']['title'], c)

    def test_a_narrow_popup_cuts_the_words_at_the_end_of_the_title(self):
        c = self.run_case('claude', 'narrow', 5)['checks']
        self.assertEqual(c['kind'], 'panel', c)
        self.assertRegex(c['title'], r'^╭─ 5 ROUTE', c)

    def test_the_route_box_laid_at_the_popups_width_has_no_sides_and_presses_open_routes(self):
        script = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const P=t=>({text:t});
const names=['plan','execute','report'];
const stages=at=>names.map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true,key:'stg2_on'}:{name,mark:''});
const route=n=>({id:'r'+n,title:'제목 '+n,kind:'working',state:'working',elapsed:'42m',shape:'staged',stages:stages(1),kind_word:'code',intensity:'standard',role:'owner',
  chain:[{name:'frame',state:'done'},{name:'code',state:'active'}],paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('high'),ctx:P('40%'),now:'지금 하는 일 '+n}});
const routes=[1,2,3,4,5].map(route);
const model={repo:[P('hearting/')],styles:{dim:{dim:true},stg2_on:{foreground:150,bold:true}},routes,route_count:5,route_total:5,
  requests:[{id:'q1',title:'REQUEST_TITLE_ROW',kind:'request',state:'waiting'}],messages:[{way:'→',who:'MESSAGE_ROW',text:'MESSAGE_BODY'}]};
const text=r=>r.map(p=>p.text).join('');
for(const columns of [40,80,148]){
  const b=view.routesBody({model,columns});
  assert(b&&b.rows.length>=routes.length*4,'rows at '+columns);
  for(const row of b.rows){const t=text(row);
    assert(!/^│/.test(t)&&!/│\s*$/.test(t),'a side left: '+t);
    assert(!/REQUEST_TITLE_ROW|MESSAGE_ROW|MESSAGE_BODY/.test(t),'a request or message row: '+t);
    assert(row.every(p=>!('beat' in p)&&!('spin' in p)),'a moving part');}
  assert(b.hits.length>0);
  for(const h of b.hits){assert(Array.isArray(h.action)&&h.action.length===2&&h.action[0]==='route'&&/^r[1-5]$/.test(h.action[1]),JSON.stringify(h));
    assert(h.row>=0&&h.row<b.rows.length&&h.left===0&&h.right===columns-1);}
  assert.deepEqual(b.routes,['r1','r2','r3','r4','r5']);
  assert(b.rows.some(r=>text(r).includes('제목 3')));
}
assert.equal(view.routesBody({model:{styles:{},routes:[]},columns:60}),null);
console.log('ok');
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell')], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'ok')


if __name__ == '__main__':
    if '--frames' in sys.argv:
        sys.argv.remove('--frames'); SHOW_FRAMES = True
    unittest.main()
