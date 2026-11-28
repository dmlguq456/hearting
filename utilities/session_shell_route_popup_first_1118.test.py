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
5. (11-19) A press on a route's row opens that route's page, not the table: the first screen is the stage boxes the panel's
   own values draw (`panelView.routePageFirst`), then Fleet's page; Enter goes to the table and back; a press on the
   section's head opens the table. A route with no stage line keeps the panel's box as its first screen.

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
ctl=sys.argv[3] if len(sys.argv)>3 else ''   # the case's control directory: a `native-start` file there makes the native say `start` again
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
    if ctl:wait=0.1 if wait is None else min(wait,0.1)
    ready,_,_=select.select([0],[],[],wait)
    if ctl and os.path.exists(ctl+'/native-start'):
        os.unlink(ctl+'/native-start');event('start')      # an auto-compact, `/clear` or `/resume` while a popup stands
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
SERVED=[]
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
_real_time=time.time
time.time=lambda:_real_time()-number('clock-back')                              # a collection's time, `clock-back` seconds in the past
_popup=m._panel_popup
def popup(binding,request):
    if not SERVED:
        SERVED.append(1);open(C+'/backends','a').write('%d\n'%os.getpid())      # one line for each process that serves a popup (held or one-shot, any kind)
    if request.get('kind')=='routes' and request.get('page') and os.path.exists(C+'/hold-page'):
        open(C+'/page-asked','w').close()
        while os.path.exists(C+'/hold-page'):time.sleep(0.01)
    return _popup(binding,request)
m._panel_popup=popup
m.session_questions=lambda harness,sid,**_kw:[]
m._request_views=lambda harness,sid,now,errors=None:([],[])
raise SystemExit(m._backend())
"""

DRIVER = r"""
const fs=require('node:fs');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
const enterCase=cfg.scenario.startsWith('enter_');
let cols=enterCase?132:160,rows=enterCase?46:40;
const term=new Terminal({cols,rows,allowProposedApi:true,scrollback:0});
term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const child=pty.spawn(cfg.python,[cfg.entry,cfg.harness,cfg.python,cfg.fake,cfg.harness,cfg.sid,cfg.control],{cols,rows,cwd:cfg.project,
  env:{...process.env,TERM:'xterm-256color',NODE_OPTIONS:'--require '+cfg.launcher}});
// The host draws each panel or popup frame inside synchronized output (`ESC[?2026h` … `ESC[?2026l`, host.cjs), and a
// terminal shows such a frame only once it is whole. A frame's bytes can come in two PTY reads on a loaded machine;
// a read between them saw half a popup (3 of 6 stage boxes, a page without its boxes: CI 11-26 and 11-27).
let framing=false,arrived=0,tail='';
const take=d=>{arrived++;term.write(d);const t=tail+d,open=t.lastIndexOf('\x1b[?2026h'),shut=t.lastIndexOf('\x1b[?2026l');
  if(open>=0||shut>=0)framing=open>shut;tail=t.slice(-7);};
child.onData(take);
const flush=()=>new Promise(r=>term.write('',r));
// Every byte that arrived is parsed and no frame is open (one that never closes is read as it stands after 2 s).
const whole=async()=>{for(let i=0;i<400;i++){const n=arrived;await flush();if(!framing&&n===arrived)return;await sleep(5);}};
const cellsOf=y=>{const line=term.buffer.active.getLine(y),out=[];for(let x=0;x<cols;x++){const c=line?.getCell(x);out.push(!c?' ':c.getWidth()===0?'':c.getChars()||' ');}return out;};
const screen=async()=>{await whole();const grid=Array.from({length:rows},(_,y)=>cellsOf(y)),out=grid.map(c=>c.join('').trimEnd());out.grid=grid;return out;};
const at=async needle=>{await whole();for(let y=0;y<rows;y++){const c=cellsOf(y),where=[];let text='';
  c.forEach((ch,x)=>{for(const _ of ch)where.push(x);text+=ch;});const i=text.indexOf(needle);if(i>=0)return {x:where[i],y};}return null;};
const until=async(test,what,ms=20000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return {s,ms:Date.now()-began};await sleep(25);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>line.includes(word));
// A wait for something that is not on the screen (a process, a file): it ends the moment `test()` holds, or throws at the deadline.
const settle=async(test,what,ms=8000)=>{const began=Date.now();while(Date.now()-began<ms){if(test())return Date.now()-began;await sleep(25);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
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
  await until(s=>has('READY_'+cfg.harness)(s)&&has(cfg.routes+' ROUTE')(s)&&has('PANEL_R1')(s),'the panel with its routes');
  if(!enterCase)await sleep(700);
  const sc=cfg.scenario;
  if(enterCase){
    const pending=sc==='enter_pending';
    const pageKeys=popupHas('Enter 표로 돌아가기');
    const sample=(name,s)=>{
      const b=box(s);
      frames[name]=plain(s);
      return {top:b.top+1,bottom:b.bottom+1,stage:popupHas('╔═')(s),fleet:popupHas('FLEET_R1')(s),reading:reading(s)};
    };
    if(pending)fs.writeFileSync(cfg.vm+'.hang','');
    await opened();
    const list=await until(pending?either:filled,'the route table');
    checks.table=sample('table',list.s);
    if(pending)await settle(()=>fs.existsSync(cfg.control+'/collections'),'the table collection is waiting');
    control('hold-page');
    const t0=Date.now();
    child.write('\r');
    const first=await until(pageKeys,'the page keys while the read is held');
    checks.first_ms=Date.now()-t0;
    checks.first=sample('first',first.s);
    if(pending)fs.unlinkSync(cfg.vm+'.hang');
    await settle(()=>fs.existsSync(cfg.control+'/page-asked'),'the page request is waiting');
    checks.waiting=sample('waiting',(await until(pageKeys,'the page during its own read')).s);
    fs.unlinkSync(cfg.control+'/hold-page');
    const read=await until(s=>pageKeys(s)&&popupHas('║')(s)&&filled(s),'the read page');
    checks.full_ms=Date.now()-t0;
    checks.full=sample('full',read.s);
    checks.collections=fs.readFileSync(cfg.control+'/collections','utf8').trim().split('\n').length;
  }
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
  if(sc==='row_page'||sc==='row_page_unstaged'){
    // A press on a route's row opens its page. The first screen is the stage boxes (a staged route) or the panel's box (none),
    // Fleet's page comes after the delay, Enter goes to the table and back, a press on the head opens the table.
    const staged=sc==='row_page';
    const lines=f=>fs.existsSync(cfg.control+'/'+f)?fs.readFileSync(cfg.control+'/'+f,'utf8').split('\n').filter(Boolean):[];
    const paged=s=>popupHas('║')(s);
    // (a box's name that does not fit its title is cut with `…`, as Fleet's is: `plan-che…`, `impl-rev…`; the boxes are read by its first letters)
    const boxTops=s=>plain(s).filter(l=>/┌─ (plan|execute|impl|test|report)/.test(l));
    const boxNames=s=>plain(s).flatMap(l=>l.match(/┌─ (?:plan|execute|impl|test|report)\S* /g)||[]);
    // Where a stage's box stands in the popup's rows and what its top line says past the name (the mark and the word, if there is room).
    const boxGeo=ls=>{const l=ls.find(x=>/┌─ execute/.test(x));if(!l)return null;const left=l.indexOf('┌─ execute'),end=l.indexOf('┐',left);
      return {left,width:end-left+1,tag:l.slice(left+'┌─ execute'.length,end).replace(/[─ ]/g,'')};};
    const marks=s=>{const t=boxTops(s).join('');return {done:[...t].filter(c=>c==='✓').length,active:[...t].filter(c=>c==='●').length};};
    const order=s=>{const t=plain(s).join('\n');return [/┌─ plan /,/┌─ plan-c/,/┌─ execute/].map(r=>t.search(r));};
    const widths=staged?[196,120,80]:[160];
    checks.widths={};
    for(const w of widths){
      const c={};checks.widths[w]=c;
      if(w!==160){await resize(w,40);await sleep(300);}
      control('vm-delay',cfg.delay);
      const t0=Date.now();
      let target=await at('PANEL_R2');
      // Where the window is narrow the panel stands folded (a strip): a press on its `▴` opens it, and the routes are there to press.
      if(!target){const fold=await at('▴');if(fold){await press(fold.x,fold.y,0);await sleep(500);target=await at('PANEL_R2');}}
      if(!target)throw Error('no panel row PANEL_R2 at '+w+'\n'+(await screen()).join('\n'));
      await press(target.x+2,target.y,0);
      const first=await until(s=>!!box(s)&&(boxTops(s).length>0||either(s)),'a first content at '+w);
      c.first_ms=Date.now()-t0;c.first_has_fleet=popupHas('FLEET_R')(first.s);c.first_paged=paged(first.s);c.first_title=title(first.s);
      c.first_boxes=boxNames(first.s).length;c.first_marks=marks(first.s);c.first_order=order(first.s);
      {const fl=plain(first.s),ft=fl.join('\n'),rule=fl.find(l=>l.includes('╔═'))||'';
       // the route pressed (PANEL_R2) is the one drawn, not another's
       c.first_named=rule.includes('PANEL_R2')&&!rule.includes('PANEL_R1')&&!rule.includes('PANEL_R3');
       // (11-19: the head's word is the route's form, `team`, and never `staged`)
       c.first_form=/ team ═/.test(rule)&&!/staged/.test(rule);c.first_rule_text=rule;
       // the panel's pass mark `gate: true` is not drawn as a word; an empty `out`/`unit` is `·`
       c.first_true=/\btrue\b/.test(ft);c.first_dot=/out +· +│/.test(ft)&&/unit +· +│/.test(ft);c.first_box=boxGeo(fl);}
      c.first_rule=popupHas('╔═')(first.s);c.first_keys=popupHas('Enter 표로 돌아가기')(first.s);
      c.first_top=box(first.s).top;
      frames['row_page_'+w+'_first']=plain(first.s);
      // (b) Fleet's page.
      const full=await until(s=>paged(s)&&popupHas('FLEET_R2')(s)&&!staleWords(s),'Fleet\'s page at '+w,cfg.delay*1000+20000);
      c.full_ms=Date.now()-t0;c.full_title=title(full.s);frames['row_page_'+w+'_page']=plain(full.s);
      c.full_top=box(full.s).top;
      c.full_keys=popupHas('Enter 표로 돌아가기')(full.s);c.page_box=boxGeo(plain(full.s));
      // the page's stage boxes (Fleet's own drawing: a narrow page cuts a name, `plan-che…`), inside the page's `║` frame
      const tops=plain(full.s).filter(l=>/║.*┌─ (plan|execute|impl-r|test|report)/.test(l));
      c.full_boxes=tops.flatMap(l=>[...l.matchAll(/┌─ (\S+)/g)].map(m=>m[1])).filter(n=>!/^(frame|leg)$/.test(n));
      c.full_marks={done:[...tops.join('')].filter(x=>x==='✓').length,active:[...tops.join('')].filter(x=>x==='●').length};
      c.full_no_record=popupHas(cfg.noRecord)(full.s);
      if(!staged)continue;
      // (c) Enter: the table, the route unfolded; (d) Enter: the page again.
      await key('\r',0);const tab=await until(s=>!paged(s)&&!!unfolded(s)&&popupHas('FLEET_R')(s),'the table at '+w);
      // (a narrow table cuts the name: `FLEET_R…`)
      c.table_unfolded=plain(tab.s).join('\n');c.table_has_team=/code\(team/.test(c.table_unfolded)&&!/staged/.test(c.table_unfolded);c.table_full_bracket=/code\(team·standard\)/.test(c.table_unfolded);c.table_paged=paged(tab.s);frames['row_page_'+w+'_table']=plain(tab.s);
      if(w===120){
        // (the limit: a page left for the table and closed opens at its stage boxes again, then Fleet's page after the delay)
        await close();control('vm-delay',cfg.delay);
        const tp=await at('PANEL_R2');await press(tp.x+2,tp.y,0);
        const lim=await until(s=>!!box(s)&&boxTops(s).length>0,'the stage boxes again at '+w);
        c.limit={boxes:boxNames(lim.s).length,paged:paged(lim.s),fleet:popupHas('FLEET_R')(lim.s)};frames['row_page_'+w+'_limit']=plain(lim.s);
        await until(s=>paged(s)&&popupHas('FLEET_R2')(s)&&!staleWords(s),'Fleet\'s page again at '+w,cfg.delay*1000+20000);
        await key('\r',0);await until(s=>!paged(s)&&!!unfolded(s),'the table again at '+w);
      }
      await key('\r',0);const again=await until(s=>paged(s)&&popupHas('FLEET_R2')(s),'the page again at '+w);c.again_paged=paged(again.s);
      // (e) Closed and pressed again: the page read last at once, with the words that it is not this reading's.
      await close();control('vm-delay',cfg.delay);
      const t1=Date.now();const target2=await at('PANEL_R2');await press(target2.x+2,target2.y,0);
      const back=await until(s=>!!box(s)&&(boxTops(s).length>0||either(s)),'the page read last at '+w);
      c.reopen={ms:Date.now()-t1,paged:paged(back.s),fleet:popupHas('FLEET_R2')(back.s),stale:staleWords(back.s)};
      // ←→ from the page: the routes popup that ←→ reach is the table (`↑↓ route`), not a page.
      await key('\x1b[C',300);
      for(let i=0;i<5&&!popupHas('↑↓ route')(await screen());i++)await key('\x1b[C',400);
      const arrow=await until(popupHas('↑↓ route'),'the routes popup by →');
      c.arrow={table_keys:popupHas('↑↓ route')(arrow.s),page_keys:popupHas('Enter 표로 돌아가기')(arrow.s),paged:paged(arrow.s)};
      // (f) The section's head: the table (the panel's box or Fleet's), never the page.
      await close();control('vm-delay',cfg.delay);
      // (an unfolded panel's box is titled `route N`; the folded strip and the side panel say `N ROUTE`: both are the section's head)
      {const p=(await head())||(await at('route '+cfg.routes));await press(p.x+2,p.y,0);}
      const top=await until(either,'a table from the head at '+w);
      c.head={kind:kind(top.s),paged:paged(top.s),keys:popupHas('Enter 표로 돌아가기')(top.s)};
      await close();
    }
    if(!staged){
      // A route with no stage line: the panel's box first (no stage boxes, no frame), Fleet's page after.
      checks.collections=lines('collections').length;
    }
  }
  if(sc==='hang'){
    // The collection does not come in 12 s: what stood first stays, the words say the reading failed; a later
    // reading (Tab) that succeeds takes them away.
    fs.writeFileSync(cfg.vm+'.hang','');
    const t0=Date.now();await opened();const first=await until(either,'a first content');checks.first_ms=Date.now()-t0;checks.first_kind=kind(first.s);
    checks.first_reading=reading(first.s);frames.hang_first=plain(first.s);
    await until(failedRead,'the words that the reading failed',20000);let s=await screen();
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
    else{r=await until(filled,'the table',cfg.delay*3000+8000);await sleep(800);}
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
  if(['session_start','now_start','switch_now','aged','aged_gaps','page_scroll'].includes(sc)){
    const lines=f=>fs.existsSync(cfg.control+'/'+f)?fs.readFileSync(cfg.control+'/'+f,'utf8').split('\n').filter(Boolean):[];
    const isAlive=pid=>{try{process.kill(pid,0);return true;}catch{return false;}};
    // Every process that served a popup, and how many of them are still running.
    const procs=()=>lines('backends').map(Number),alive=()=>procs().filter(isAlive).length,collected=()=>lines('collections').length;
    const spawned=()=>lines('spawns').map(Number),live=()=>spawned().filter(isAlive).length;
    const unfoldedNow=s=>unfolded(s).match(/FLEET_R\d+/)?.[0]||'';
    const paged=s=>popupHas('║')(s);
    // The native says `start` again (what an auto-compact, `/clear` or `/resume` does) with the popup standing.
    const nativeStart=async()=>{control('native-start','1');await until(s=>!box(s),'the popup closed by the start');await sleep(1500);};
    // ←→ until the popup is `name`'s.
    const toKind=async name=>{for(let i=0;i<6&&!title(await screen()).includes(name);i++)await key(i<3?'\x1b[C':'\x1b[D',400);
      await until(s=>title(s).includes(name),'the '+name+' popup');};
    control('vm-delay',cfg.delay);
    if(sc==='session_start'){
      await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      checks.before={alive:alive()};
      await nativeStart();
      checks.after={alive:alive(),live:live()};
    }
    if(sc==='now_start'){
      await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      await toKind('NOW');await settle(()=>alive()===1,'the NOW backend');
      checks.before={alive:alive(),title:title(await screen())};
      await nativeStart();
      checks.after={alive:alive()};
    }
    if(sc==='switch_now'){
      // The routes collection is still under way when the person goes to NOW: its backend ends (no late answer is drawn into NOW's
      // popup), NOW's is the one held; back at the routes the NOW one ends in turn, and a start ends the one standing.
      await opened();await until(either,'a first content');
      // (Its backend has started its collection: the move ends a process that is there.)
      for(let i=0;i<100&&!spawned().length;i++)await sleep(50);
      checks.collecting={spawned:spawned().length};
      await toKind('NOW');await settle(()=>alive()===1&&procs().length>=2,'NOW held, the routes backend ended');
      checks.on_now={alive:alive(),procs:procs().length,title:title(await screen())};
      // The routes collection's backend is gone (it is the one process that collects), so no late answer can come: one look.
      await settle(()=>live()===0,'the routes collection backend gone');
      await sleep(100);
      const late=await screen();checks.late={title:title(late),fleet:popupHas('FLEET_R')(late)};
      control('vm-delay',0);
      await toKind('ROUTE');await until(filled,'the filled table again',cfg.delay*1000+20000);
      checks.back={alive:alive(),procs:procs().length};
      await nativeStart();
      checks.after_start={alive:alive()};
    }
    if(sc==='aged'){
      // A collection older than a minute (its time is put back two minutes): the key that follows must not draw it unmarked.
      control('clock-back',120);
      await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      checks.opened={collections:collected()};
      control('clock-back',0);control('vm-delay',1);
      const first=unfoldedNow(await screen());
      const t0=Date.now();let unmarked=0;
      child.write('\x1b[B');
      let r;
      for(;;){
        const before=collected();const s=await screen();const after=collected();
        // A frame of the next route, drawn from the old collection with nothing saying so.
        if(unfoldedNow(s)&&unfoldedNow(s)!==first&&!staleWords(s)&&before===1&&after===1)unmarked++;
        if(unfoldedNow(s)&&unfoldedNow(s)!==first){r={s,ms:Date.now()-t0};break;}
        if(Date.now()-t0>20000)throw Error('never the next route\n'+s.join('\n'));
        await sleep(10);
      }
      checks.first={ms:r.ms,title:title(r.s),collections:collected(),words:staleWords(r.s),was:first,route:unfoldedNow(r.s)};
      const until_settled=Date.now()+8000;
      for(;;){
        const before=collected();const s=await screen();
        if(unfoldedNow(s)&&unfoldedNow(s)!==first&&!staleWords(s)&&before===1)unmarked++;
        if(!staleWords(s)&&collected()>=2&&unfoldedNow(s))break;
        if(Date.now()>until_settled){checks.settle_timeout=true;break;}
        await sleep(25);
      }
      await sleep(300);
      checks.settled={title:title(await screen()),collections:collected()};
      checks.unmarked=unmarked;
      const second=unfoldedNow(await screen());
      const t1=Date.now();await key('\x1b[B',0);const n=await until(s=>unfoldedNow(s)&&unfoldedNow(s)!==second,'the route after');
      checks.next={ms:Date.now()-t1,title:title(n.s),collections:collected()};
    }
    if(sc==='aged_gaps'){
      // The gaps of the aged key (review of #1735): (b) a re-read that fails says so, and the route stays the one moved to;
      // (c) keys pressed while the re-read is under way end on the last route's page with one more collection, not two;
      // (d) a held backend that died is replaced by a fresh read, which is not read a second time behind words.
      const reopenAged=async(back,delay)=>{await close();control('clock-back',back);control('vm-delay',delay);await opened();await until(filled,'the filled table',20000);};
      const routeNumber=s=>Number(unfoldedNow(s).slice(7));
      // (b)
      control('clock-back',120);await opened();await until(filled,'the filled table',cfg.delay*1000+20000);
      control('clock-back',0);fs.writeFileSync(cfg.vm+'.hang','');
      const b0=routeNumber(await screen());
      await key('\x1b[B',0);
      const moved=await until(s=>routeNumber(s)===b0+1&&reading(s),'the next route with the words that it is read again');
      checks.fails={was:b0,route:routeNumber(moved.s),reading:reading(moved.s)};
      const failed=await until(failedRead,'the words that the re-read failed',40000);
      checks.fails.failed={title:title(failed.s),route:routeNumber(failed.s)};
      fs.unlinkSync(cfg.vm+'.hang');
      await key('\x1b[B',0);
      const after=await until(s=>routeNumber(s)===b0+2&&!staleWords(s),'the words gone after the hang is lifted',30000);
      checks.fails.after={route:routeNumber(after.s),title:title(after.s)};
      // (c) (a window lower than the page, so that its place is told by the foot `1/N`)
      await close();await resize(120,16);
      await reopenAged(120,0);
      const c0=collected(),c1=routeNumber(await screen());
      control('clock-back',0);control('vm-delay',2);
      await key('\x1b[B',0);await key('\x1b[B',0);await key('\r',0);
      const want=Math.min(cfg.routes,c1+2);
      const end=await until(s=>paged(s)&&popupHas('FLEET_R'+want)(s)&&!staleWords(s),'the last route\'s page, read',30000);
      await sleep(600);
      const fin=await screen();
      checks.keys={want,collections:collected()-c0,paged:paged(fin),words:staleWords(fin),route:popupHas('FLEET_R'+want)(fin),
        foot:(plain(fin).find(l=>/\d+\/\d+ ·/.test(l))||'').match(/\d+\/\d+/)?.[0]||''};
      // (d)
      await close();await resize(160,40);
      await reopenAged(120,0);
      const d0=collected();
      // The held backend of this popup is the one that collected last: only the process this popup started is ended.
      for(const pid of spawned().slice(-1))try{process.kill(pid,'SIGKILL');}catch{}
      await sleep(300);
      control('clock-back',0);
      const d1=routeNumber(await screen());
      await key('\x1b[B',0);
      await until(s=>routeNumber(s)===d1+1,'the next route read by a backend of its own',20000);
      await sleep(1500);
      const dead=await screen();
      checks.dead={collections:collected()-d0,words:staleWords(dead),title:title(dead),route:routeNumber(dead)===d1+1};
    }
    if(sc==='page_scroll'){
      // Enter pressed on a route while the collection is still under way (the window is lower than the page): the page opens at its top.
      await resize(120,16);
      // The collection is held under way by `.hang` (not by a 2 s delay the keys must beat), so the keys below are written while it is out.
      fs.writeFileSync(cfg.vm+'.hang','');
      await opened();await until(either,'a first content');
      for(let i=0;i<8;i++)await key('\x1b[B',60);await key('\r',100);
      const pageRead=s=>paged(s)&&!staleWords(s)&&plain(s).some(l=>/\d+\/\d+ ·/.test(l));
      // The first page can also be longer than the low window. It shows a place mark before the
      // held collection is released.
      await until(s=>plain(s).some(l=>/\d+\/\d+ ·/.test(l)),'a place mark on the first screen');
      fs.unlinkSync(cfg.vm+'.hang');
      // A frame is taken once the page has stopped changing (a frame drawn between two steps of the redraw is not the page).
      const quiet=async s0=>{let last=JSON.stringify(plain(s0)),same=0,cur=s0;const began=Date.now();
        while(same<8&&Date.now()-began<10000){await sleep(25);const s=await screen(),now=JSON.stringify(plain(s));if(now===last)same++;else{same=0;last=now;cur=s;}}return cur;};
      const s1=await quiet((await until(pageRead,'the page read',cfg.delay*1000+20000)).s);checks.page_top=plain(s1).slice(0,4);checks.page_foot=(plain(s1).find(l=>/\d+\/\d+ ·/.test(l))||'').match(/\d+\/\d+/)?.[0]||'';frames.page_pressed=plain(s1);checks.page_title=title(s1);
      // The same page, asked when nothing is under way (back to the table, then Enter).
      await key('\r',500);await until(s=>!paged(s)&&filled(s),'the table again');
      await key('\r',500);await until(paged,'the page again');
      const s2=await quiet((await until(pageRead,'the calm page')).s);checks.calm_page_top=plain(s2).slice(0,4);checks.calm_foot=(plain(s2).find(l=>/\d+\/\d+ ·/.test(l))||'').match(/\d+\/\d+/)?.[0]||'';frames.page_calm=plain(s2);
    }
  }
  fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
async function resize(c,r){cols=c;rows=r;term.resize(c,r);child.resize(c,r);await sleep(500);}
"""


STAGES = ('plan', 'plan-check', 'execute', 'impl-review', 'test', 'report')


def stage_values(i, n):
    """The values a staged route's stage carries (the producer's `_panel_stage` and Fleet's route read the same record)."""
    # (a finished stage's `gate: True` is the pass mark the producer's `stage_doer` carries; the word is Fleet's box's: `PASS`, `running`, `pending`)
    return {'name': n, 'st': 'done' if i < 2 else 'active' if i == 2 else 'pending',
            'verdict': 'PASS' if i < 2 else 'running' if i == 2 else 'pending', 'gate': i < 2,
            'when': '%dm' % (30 - 10 * i) if i < 3 else '', 'unit': 'dev/backend' if i == 2 else '', 'out': 'artifacts/%s.md' % n if i < 2 else ''}


def route_nodes(rid):
    """Fleet's own node shape for a staged route (`live._node_groups`): a frame leg done, then the code leg running, its
    detail the six stages (`live._node_detail`: `todo` is a stage not begun)."""
    detail = [{'n': n, 'st': {'pending': 'todo'}.get(v['st'], v['st']), 'verdict': v['verdict'] or {'active': 'running', 'pending': 'pending'}[v['st']],
               'who': None, 'when': v['when'] or '·', 'rounds': [], 'rounds_note': '', 'unit': v['unit'] or '·', 'gate': '',
               'out': v['out'] or '·', 'says': ''} for n, v in ((n, stage_values(i, n)) for i, n in enumerate(STAGES))]
    return [{'kind': 'frame', 'n': 'frame', 'st': 'done', 'verdict': '1/1', 'lines': [], 'loops': [],
             'detail': [{'n': 'frame', 'st': 'done', 'verdict': 'PASS', 'who': None, 'when': 'done 40m ago', 'rounds': [],
                         'rounds_note': '', 'unit': '·', 'gate': '', 'out': '·', 'says': ''}],
             'title': [['frame', 'head']]},
            {'kind': 'leg', 'n': 'leg 2 · code dev', 'st': 'active', 'verdict': '2/6', 'lines': [[['owner  ', 'dim'], ['claude', 'hb_claude']]],
             'loops': [], 'detail': detail, 'title': [['leg 2 · code dev', 'head']]}]


def route_row(rid, title, state='working', staged=False):
    row = {'id': rid, 'title': title, 'state': state, 'worker_stage': 'one-shot', 'shape': 'one-shot-owner',
           'stages': [{'name': 'one-shot', 'st': 'failed' if state == 'dead' else 'active'}], 'harness': 'claude',
           'model': 'Opus 5.5', 'effort': 'high'}
    if staged:  # the producer's staged route (what a real route's row carries), for the pictures of the first screen
        row.update(shape='staged', kind_form='team', worker_stage='execute', capability='autopilot-code', mode='dev', intensity='standard',
                   chain=[{'name': 'frame', 'state': 'done'}, {'name': 'code', 'state': 'active'}],
                   stages=[stage_values(i, n) for i, n in enumerate(STAGES)])
    return row


def fleet_route(rid, title, state='working', staged=False):
    row = {'id': rid, 'session_tag': 'e9', 'repo': 'project', 'title': title, 'state': state, 'harness': 'claude',
           'model': 'Opus 5.5', 'effort': 'high', 'owner_gauge': [40, 0.2], 'age': '23m', 'capability': 'autopilot-code',
           'mode': 'dev', 'intensity': 'quick', 'stages': [{'name': 'one-shot', 'st': 'failed' if state == 'dead' else 'active'}],
           'worker_stage': 'one-shot'}
    if staged:
        row.update(intensity='standard', shape='staged', worker_stage='execute', legs=[['frame', 'done', None], ['code', 'active', None]],
                   stages=[stage_values(i, n) for i, n in enumerate(STAGES)])
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
        cls.no_record = catalog['fleet.route.no_stage_record']['ko']
        cls.count = 0

    def run_case(self, harness, scenario, routes, delay=3, dead=None, staged=False, now=False):
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
        if now:  # the panel's NOW row (a fresh summary in this session's title record), so ←→ reaches the NOW popup
            sys.path.insert(0, str(ROOT / 'tools'))
            with mock.patch.dict(os.environ, self.env, clear=True):
                from fleet import titles
                titles.write(sid, '이 창 제목', summary='그리는 중이다', harness=harness, source='refresher')
        vm = case / 'vm.json'
        vm.write_text(json.dumps({'repos': [{'id': 'project', 'name': 'project'}], 'system': {},
            'routes': [fleet_route(rid(n), 'FLEET_R%d 제목' % n, states[n - 1], staged) for n in range(1, routes + 1)],
            # a staged route's page is drawn from Fleet's nodes: its leg band and the stage boxes
            'nodes': {rid(n): route_nodes(rid(n)) for n in range(1, routes + 1)} if staged else {},
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
                  'scenario': scenario, 'routes': routes, 'delay': delay, 'unreadable': self.unreadable, 'noRecord': self.no_record}
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
        c = self.run_case('claude', 'many', 5, delay=1)['checks']
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
        self.assertGreaterEqual(c['changed']['full_ms'], 800, c)
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
        a = self.run_case('claude', 'place_a', 20, delay=2, dead=2)
        b = self.run_case('claude', 'place_b', 20, delay=2, dead=2)
        c = self.run_case('claude', 'place_c', 20, delay=2, dead=9)
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
            c = self.run_case('claude', 'keys', routes, delay=1, staged=True)['checks']
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
        c = self.run_case('claude', 'held_dies', 5, delay=1, staged=True)['checks']
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

    def test_a_session_start_while_the_routes_popup_stands_ends_its_held_backend(self):
        # Review of #1735: `start`/`select` (a native auto-compact, `/clear`, `/resume`) closed the popup but left its held
        # backend (and the collection it holds) running until the window ended.
        c = self.run_case('claude', 'session_start', 5, delay=1, staged=True)['checks']
        self.assertEqual(c['before']['alive'], 1, c)
        self.assertEqual(c['after']['alive'], 0, c)
        self.assertEqual(c['after']['live'], 0, c)

    def test_a_session_start_while_the_now_popup_stands_ends_its_held_backend(self):
        c = self.run_case('claude', 'now_start', 5, delay=1, staged=True, now=True)['checks']
        self.assertIn('NOW', c['before']['title'], c)
        self.assertEqual(c['before']['alive'], 1, c)
        self.assertEqual(c['after']['alive'], 0, c)

    def test_moving_between_now_and_routes_swaps_the_held_backend_and_draws_no_late_answer(self):
        c = self.run_case('claude', 'switch_now', 5, delay=3, staged=True, now=True)['checks']
        # The routes collection under way ends with the move to NOW; NOW's backend is the one running.
        self.assertEqual(c['collecting']['spawned'], 1, c)
        self.assertEqual(c['on_now']['alive'], 1, c)
        self.assertEqual(c['on_now']['procs'], 2, c)
        self.assertIn('NOW', c['on_now']['title'], c)
        # Its collection's late answer is not drawn into NOW's popup.
        self.assertIn('NOW', c['late']['title'], c)
        self.assertFalse(c['late']['fleet'], c)
        # Back at the routes the NOW backend ends and the routes one is the one running; a start ends it.
        self.assertEqual(c['back']['alive'], 1, c)
        self.assertEqual(c['after_start']['alive'], 0, c)

    def test_a_key_after_the_collection_is_older_than_a_minute_says_so_or_collects(self):
        # Review of #1735: a held collection a person left for over a minute was drawn again by the next key with nothing
        # saying it was not new. It is drawn at once with the words, and read again behind them.
        c = self.run_case('claude', 'aged', 5, delay=1, staged=True)['checks']
        self.assertEqual(c['opened']['collections'], 1, c)
        self.assertEqual(c['unmarked'], 0, c)
        self.assertLess(c['first']['ms'], 1000, c)
        self.assertIn('다시 읽는 중', c['first']['title'], c)
        self.assertNotIn('settle_timeout', c)
        # The route it unfolded is the one the key moved to.
        self.assertEqual(c['first']['route'], 'FLEET_R%d' % (int(c['first']['was'][7:]) + 1), c)
        self.assertEqual(c['settled']['collections'], 2, c)
        self.assertNotIn('읽은 내용', c['settled']['title'], c)
        # The collection is new now: the next key draws from it, with no more reading.
        self.assertEqual(c['next']['collections'], 2, c)
        self.assertLess(c['next']['ms'], 1000, c)
        self.assertNotIn('읽은 내용', c['next']['title'], c)

    def test_a_page_entered_while_the_table_is_collecting_opens_at_its_top(self):
        r = self.run_case('claude', 'page_scroll', 20, delay=2, dead=2, staged=True)
        c = r['checks']
        # The page of the route, with its head, from its first line (`1/N`), as when nothing was under way.
        self.assertTrue(c['calm_foot'].startswith('1/'), c)
        self.assertEqual(c['page_foot'], c['calm_foot'], c)
        self.assertEqual(c['page_top'], c['calm_page_top'], r['frames'])

    def test_a_narrow_popup_cuts_the_words_at_the_end_of_the_title(self):
        c = self.run_case('claude', 'narrow', 5)['checks']
        self.assertEqual(c['kind'], 'panel', c)
        self.assertRegex(c['title'], r'^╭─ 5 ROUTE', c)

    def test_a_press_on_a_routes_row_opens_its_page_with_the_stage_boxes_first_and_a_press_on_the_head_opens_the_table(self):
        c = self.run_case('claude', 'row_page', 3, delay=1, staged=True)['checks']['widths']
        for width, w in c.items():
            print(json.dumps({'event': 'route-popup-page-first-pty', 'width': width, 'first_ms': w['first_ms'], 'full_ms': w['full_ms'],
                              'reopen_ms': w['reopen']['ms']}), flush=True)
            # (a) The stage boxes stand under a second, from the panel's own values: the stages in order, two done and one running
            # (the boxes' own marks), no Fleet row, the words that it is not this reading's, the page's key line.
            self.assertLess(w['first_ms'], 1000, w)
            self.assertFalse(w['first_has_fleet'], w)
            self.assertFalse(w['first_paged'], w)
            self.assertTrue(w['first_rule'], w)
            # (a window shows the boxes its rows hold: the first row of them at least, in order)
            self.assertGreaterEqual(w['first_boxes'], 3, w)
            self.assertEqual(w['first_marks'], {'done': 2, 'active': 1}, w)
            self.assertTrue(all(x >= 0 for x in w['first_order']) and w['first_order'] == sorted(w['first_order']), w)
            # (the route pressed, not another's; no `true` for the panel's pass mark; an empty place is `·`)
            self.assertTrue(w['first_named'], w)
            self.assertTrue(w['first_form'], w)
            self.assertFalse(w['first_true'], w)
            self.assertTrue(w['first_dot'], w)
            # (the first screen's box is the page's: the same width, the same left cell, the same mark and word on its top line)
            self.assertIsNotNone(w['first_box'], w)
            self.assertEqual(w['first_box'], w['page_box'], w)
            self.assertIn('다시 읽는 중', w['first_title'], w)
            self.assertIn('에 읽은 내용', w['first_title'], w)
            self.assertTrue(w['first_keys'], w)
            # (b) Fleet's page after the delay, the words gone.
            self.assertGreaterEqual(w['full_ms'], 800, w)
            self.assertNotIn('읽은 내용', w['full_title'], w)
            self.assertTrue(w['full_keys'], w)
            self.assertEqual(w['first_top'], w['full_top'], w)
            # (the page shows Fleet's stage boxes inside its frame, not the words that the route has no stage record)
            self.assertFalse(w['full_no_record'], w)
            for name in STAGES:
                self.assertTrue(any(name.startswith(box.rstrip('…')) and len(box.rstrip('…')) >= 4 for box in w['full_boxes']), (name, w))
            self.assertIn('execute', w['full_boxes'], w)
            self.assertEqual(len(w['full_boxes']), 6, w)
            self.assertEqual(w['full_marks'], {'done': 2, 'active': 1}, w)
            if width == '120':  # the page's limit: closed from the table, a press on the row opens the stage boxes first
                self.assertGreaterEqual(w['limit']['boxes'], 3, w)
                self.assertFalse(w['limit']['paged'] or w['limit']['fleet'], w)
            # (c) Enter: the table with the route unfolded and no frame; (d) Enter: the page again.
            self.assertIn('FLEET_R', w['table_unfolded'], w)
            self.assertNotIn('staged', w['table_unfolded'], w)
            if width == '196':  # (the table's route row reads `code(team·standard)` where its progress cell has the room; a narrower table shows the stage name)
                self.assertTrue(w['table_has_team'] and w['table_full_bracket'], w)
            self.assertIn('▶', w['table_unfolded'], w)
            self.assertFalse(w['table_paged'], w)
            self.assertTrue(w['again_paged'], w)
            # (e) Closed and pressed again: the page read last at once, with its words.
            self.assertLess(w['reopen']['ms'], 1000, w)
            self.assertTrue(w['reopen']['paged'] and w['reopen']['fleet'] and w['reopen']['stale'], w)
            # ←→ reaches the routes popup as the table.
            self.assertTrue(w['arrow']['table_keys'] and not w['arrow']['page_keys'] and not w['arrow']['paged'], w)
            # (f) The head: a table (never the page), with the table's key line.
            self.assertFalse(w['head']['paged'], w)
            self.assertFalse(w['head']['keys'], w)

    def test_enter_draws_the_first_route_page_during_a_read(self):
        for scenario in ('enter_ready', 'enter_pending'):
            with self.subTest(scenario=scenario):
                checks = self.run_case('claude', scenario, 1, staged=True)['checks']
                self.assertFalse(checks['table']['stage'], checks)
                self.assertGreater(checks['table']['top'], 2, checks)
                for state in ('first', 'waiting'):
                    frame = checks[state]
                    self.assertTrue(frame['stage'] and frame['reading'], checks)
                    self.assertFalse(frame['fleet'], checks)
                    self.assertEqual(frame['top'], 2, checks)
                self.assertTrue(checks['full']['stage'] and checks['full']['fleet'], checks)
                self.assertEqual(checks['full']['top'], 2, checks)
                self.assertEqual(checks['collections'], 1, checks)

    def test_a_route_with_no_stage_line_keeps_the_panels_box_first_and_opens_to_its_page(self):
        w = self.run_case('claude', 'row_page_unstaged', 3, delay=1)['checks']['widths']['160']
        self.assertLess(w['first_ms'], 1000, w)
        self.assertFalse(w['first_rule'], w)
        self.assertFalse(w['first_paged'], w)
        self.assertEqual(w['first_boxes'], 0, w)
        self.assertIn('다시 읽는 중', w['first_title'], w)
        self.assertTrue(w['full_keys'], w)
        self.assertEqual(w['first_top'], w['full_top'], w)
        self.assertNotIn('읽은 내용', w['full_title'], w)

    def test_the_gaps_of_the_aged_key_a_failed_reread_keys_pressed_under_it_and_a_held_backend_that_died(self):
        # Review of #1735 (yellow 1 and 4).
        c = self.run_case('claude', 'aged_gaps', 5, delay=1, staged=True)['checks']
        # (b) The route moved to stays the one unfolded while the re-read fails; the words change; a later key takes them away.
        self.assertEqual(c['fails']['route'], c['fails']['was'] + 1, c)
        self.assertTrue(c['fails']['reading'], c)
        self.assertIn('다시 읽지 못함', c['fails']['failed']['title'], c)
        self.assertEqual(c['fails']['failed']['route'], c['fails']['was'] + 1, c)
        self.assertEqual(c['fails']['after']['route'], c['fails']['was'] + 2, c)
        self.assertNotIn('읽은 내용', c['fails']['after']['title'], c)
        # (c) Two keys and Enter while the re-read is under way: the last route's page, one more collection, no words, at its top.
        self.assertTrue(c['keys']['paged'], c)
        self.assertTrue(c['keys']['route'], c)
        self.assertFalse(c['keys']['words'], c)
        self.assertEqual(c['keys']['collections'], 1, c)
        self.assertTrue(c['keys']['foot'].startswith('1/'), c)
        # (d) The held backend died: the next key is read by a fresh one, once, and nothing says it is read again.
        self.assertTrue(c['dead']['route'], c)
        self.assertEqual(c['dead']['collections'], 1, c)
        self.assertFalse(c['dead']['words'], c)

    def test_the_stage_boxes_the_panels_values_draw_for_a_routes_first_screen(self):
        script = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const names=['plan','plan-check','execute','impl-review','test','report'];
// The panel's value for a finished stage carries `gate: true` (a pass mark, `tools/fleet/session_panel.py` `stage_doer`); a gate in words is a string.
const stage=(n,i)=>({name:n,state:i<2?'done':i===2?'active':'pending',mark:i<2?'✓':i===2?'●':'',current:i===2,key:'stg'+(i%5)+'_on',
  ...(i<2?{verdict:'PASS',when:'30m',out:'artifacts/'+n+'.md',unit:'qa/'+n,gate:true}:{}),...(i===2?{harness:'claude',model:'Opus 5.5',unit:'dev/backend',when:'4m',gate:true}:{}),
  ...(i===3?{gate:'code-plan'}:{})});
const route=(id,extra={})=>({id,title:'제목 '+id,kind:'working',state:'working',elapsed:'23m',shape:'staged',kind_form:'team',stages:names.map(stage),
  chain:[{name:'frame',state:'done'},{name:'code',state:'active'}],...extra});
const model={styles:{dim:{dim:true},stg2_on:{foreground:150,bold:true}},routes:[route('r1'),route('r2',{shape:'one-shot-owner',stages:[{name:'one-shot',mark:'●'}]})]};
const text=r=>r.map(p=>p.text).join('');
function measure(s){return [...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);}
// Fleet's own cut (`grid.clip_cells`): a value that fits stays, else it ends in `…` within the room.
const clip=(v,room)=>{const s=String(v);if(s.length<=room)return s;return s.slice(0,Math.max(0,room-1))+'…';};
// Fleet's page lays the boxes in `stage_room = columns-10` (the page's frame and its legs' frame around), from the 5th cell (index 4);
// fewer than three stages stand at most 64 cells a stage.
const layout=(columns,n)=>{let room=Math.max(1,columns-10);if(n<3)room=Math.min(room,64*n);
  const per=Math.min(n,Math.max(3,Math.min(6,Math.floor((room+4)/40))));return {room,per,w:Math.floor((room-4*(per-1))/per)};};
const wordOf=s=>s.verdict||s.state;
const rightOf=(w,s)=>3+(w<34||!wordOf(s)?0:wordOf(s).length+1);
for(const columns of [192,116,76]){
  const b=view.routePageFirst({model,id:'r1',columns});
  assert(b,'a page at '+columns);
  const lines=b.rows.map(text);
  for(const row of b.rows)assert(row.every(p=>!('beat' in p)&&!('spin' in p)));
  for(const t of lines)assert(measure(t)<=columns,'a line of '+measure(t)+' cells at '+columns+': '+t);
  const {room,per,w}=layout(columns,6);
  const vroom=w-4-7;
  const shown=(n,i)=>clip(n,Math.max(0,w-6-rightOf(w,stage(n,i))));
  // (i) every stage box once, at most `per` boxes to a row, arrows between boxes and rows.
  const found=lines.flatMap(l=>[...l.matchAll(/┌─ (\S+) /g)].map(m=>m[1])).filter(n=>n!=='frame'&&n!=='code');
  assert.deepEqual([...found].sort(),names.map(shown).sort(),'six stages at '+columns);   // (a snake: an odd row reads leftward)
  for(const l of lines.filter(l=>/┌─ \S+ /.test(l)&&!/┌─ (frame|code) /.test(l)))assert([...l.matchAll(/┌─ \S+ /g)].length<=per,'row of '+l);
  assert(lines.some(l=>l.includes('▶')),'▶');assert(lines.some(l=>l.includes('▼')),'▼');assert(lines.some(l=>l.includes('◀')),'◀');
  // (ii) a value is in its own stage's box; a stage with none has an empty place (`·`, as Fleet's `out`/`unit`).
  const at=(n,i)=>lines.findIndex(l=>l.includes('┌─ '+shown(n,i)+' '));
  const boxOf=(n,i)=>{const top=at(n,i),left=lines[top].indexOf('┌─ '+shown(n,i)+' '),width=[...lines[top].slice(left)].findIndex(c=>c==='┐')+1,out=[];
    for(let y=top;y<lines.length&&!/^└/.test(lines[y].slice(left));y++)out.push([...lines[y]].slice(left,left+width).join(''));out.left=left;out.width=width;return out;};
  const first=boxOf('plan',0);
  // (iii) the box's width and its left cell are Fleet's (`_stage_grid` in `stage_room`, from cell 4)
  assert.equal(first.left,4,'the first box starts at cell 4 at '+columns);
  assert.equal(first.width,w,'the box is '+w+' wide at '+columns);
  for(const [n,i] of names.map((n,i)=>[n,i]))assert.equal(boxOf(n,i).width,w,n+' at '+columns);
  // (the status word stands in a box of 34 cells or more, as Fleet's does)
  const topOf=(n,i)=>lines[at(n,i)];
  assert.equal(/ PASS /.test(topOf('plan',0)),w>=34,'the word PASS at '+columns+' (box '+w+')');
  const done=boxOf('plan-check',1).join('\n'),running=boxOf('execute',2).join('\n'),todo=boxOf('test',4).join('\n');
  // (a value narrower than its place stands whole; a longer one ends in `…`, one cell before the right edge, as Fleet's does)
  const v1=clip('artifacts/plan-check.md',vroom),v2=clip('qa/plan-check',vroom);
  assert(done.includes(v1)&&done.includes(v2)&&done.includes('30m'),done);
  if(vroom<'artifacts/plan-check.md'.length)assert(v1.endsWith('…')&&done.includes(v1+' │'),'a cut value: '+done);
  assert(running.includes('claude')&&running.includes(clip('dev/backend',vroom))&&running.includes('4m'),running);
  // (a value fills at most up to one cell before the right edge: no line of a box that has a value touches it)
  for(const l of [...boxOf('plan',0),...boxOf('plan-check',1),...boxOf('execute',2)].slice(1,-1))assert(!/[^ ]│$/.test(l),'a value on the edge: '+l);
  const outLine=boxOf('test',4).find(l=>l.includes('out')),unitLine=boxOf('test',4).find(l=>l.includes('unit')),timeLine=boxOf('test',4).find(l=>l.includes('time'));
  // (an empty `out`/`unit` is `·`, as Fleet fills it)
  assert(/^│ out +· +│$/.test(outLine)&&/^│ unit +· +│$/.test(unitLine),outLine+unitLine);
  assert(timeLine.includes('·'),timeLine);
  assert(!todo.includes('claude'),'a harness a stage does not have');
  // (iv) the pass mark `gate: true` is not a word: no `true` anywhere, no gate line for it (the box's title mark already says ✓); a gate in words is drawn.
  for(const l of lines)assert(!/\btrue\b/.test(l),'a true at '+columns+': '+l);
  assert(!boxOf('plan',0).some(l=>l.includes('gate')),'a gate line for a pass mark');
  const gated=boxOf('impl-review',3).join('\n');
  assert(gated.includes('gate')&&gated.includes(clip('code-plan',vroom-3)),'a gate in words: '+gated);
  assert.deepEqual(b.routes,['r1','r2']);assert.deepEqual(b.hits,[]);
  // (x) 11-19: the head's word is the route's `kind_form` (`team`), never `staged`; where an older backend sends none, the shape's own word without `staged`.
  assert(/^╔═ .* · team ═+╗$/.test(lines[0])&&!lines[0].includes('staged'),'the head at '+columns+': '+lines[0]);
  const old=view.routePageFirst({model:{...model,routes:[route('o',{kind_form:undefined})]},id:'o',columns});
  assert(!old.rows.map(text).join('\n').includes('staged')&&!/team/.test(text(old.rows[0])),'an older backend at '+columns+': '+text(old.rows[0]));
  const mr=view.routePageFirst({model:{...model,routes:[route('m',{kind_form:undefined,shape:'map-reduce'})]},id:'m',columns});
  assert(/ · map-reduce ═/.test(text(mr.rows[0])),text(mr.rows[0]));       // (another shape's own word stays where no form is sent)
}
// (v) a word gate on a finished stage ends in the check mark.
{const g=view.routePageFirst({model:{...model,routes:[route('g',{stages:names.map((n,i)=>({...stage(n,i),...(i===1?{gate:'code-plan-review-long'}:{})}))})]},id:'g',columns:192});
 const l=g.rows.map(text).find(l=>l.includes('gate')&&l.includes('code-plan'));assert(l&&/code-plan-review-long ✓ +│/.test(l),String(l));}
// (vi) few stages stand at most 64 cells each: two stages at 192 columns are two boxes of 62 cells, from cell 4.
{const two=view.routePageFirst({model:{...model,routes:[route('t',{stages:names.slice(0,2).map(stage)})]},id:'t',columns:192});
 const ls=two.rows.map(text),top=ls.findIndex(l=>/┌─ plan /.test(l)),{w}=layout(192,2);
 assert.equal(w,62);assert.equal(ls[top].indexOf('┌─ plan '),4);assert.equal([...ls[top].slice(4)].findIndex(c=>c==='┐')+1,w);
 assert.equal(ls[top].indexOf('┌─ plan-check '),4+w+4);}
// (vii) a narrow page does not break: no exception, no line over the width (the box's width falls to a few cells).
for(const columns of [20,40,56]){
  const b=view.routePageFirst({model,id:'r1',columns});assert(b,'a page at '+columns);
  for(const r of b.rows)assert(measure(text(r))<=columns,'a line over '+columns+': '+text(r));
}
// (vii-b) 11-19: the label word of a content row stays inside its box too: a box's right rail `│` stands at its right cell on every content line
// (at 40 and 47 the label used to overwrite it).
for(const columns of [40,46,47,50,55]){
  const ls=view.routePageFirst({model,id:'r1',columns}).rows.map(text);
  let tops=0;
  ls.forEach((l,i)=>{for(const m of l.matchAll(/┌─/g)){const left=m.index,right=l.indexOf('┐',left);if(right<0)continue;tops++;
    for(let j=i+1;j<ls.length&&!/└/.test(ls[j].slice(left,left+1));j++)assert.equal(ls[j][right],'│','the right rail at '+columns+' row '+j+': '+ls[j]);}});
  assert(tops>0,'a box at '+columns);
}
// (viii) no stage line: the owner alone, a unit's route, no stages, an unknown route.
assert.equal(view.routePageFirst({model,id:'r2',columns:100}),null);
assert.equal(view.routePageFirst({model:{routes:[route('u',{unit_route:true})]},id:'u',columns:100}),null);
assert.equal(view.routePageFirst({model:{routes:[route('z',{stages:[]})]},id:'z',columns:100}),null);
assert.equal(view.routePageFirst({model,id:'nope',columns:100}),null);
// (ix) a name with control characters is cleaned.
const dirty=view.routePageFirst({model:{routes:[route('d',{title:'a\x1b[31mb',stages:names.map((n,i)=>stage(n+'\x07',i))})]},id:'d',columns:100});
assert(dirty.rows.every(r=>!/[\x00-\x1f\x7f-\x9f]/.test(text(r))));
// stages beyond the sixteen the panel carries: `+N`, at the boxes' left cell.
const more=view.routePageFirst({model:{routes:[route('m',{stage_more:3})]},id:'m',columns:100});
assert(more.rows.map(text).some(l=>l.trim()==='+3'));
console.log('ok');
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'utilities/session-shell')], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'ok')

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
