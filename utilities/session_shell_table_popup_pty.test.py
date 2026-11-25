"""11-15 (popup design B, E): the routes and MESSAGES popups over the real host in a private PTY.

For each harness's synthetic native: a press on a panel section opens its popup (Fleet's own table), a
press on a route's row in the panel's route box the routes popup with that route unfolded (the popup on
its own ground, a cell past each side line too, none left once it closes), ←→ move between sections
(QUESTIONS, ROUTE and MESSAGES where it has something: no LAB or SESSIONS, 11-15), ↑↓ unfold the next route, Enter opens a
route's page; the popup draws again when what it shows changes (an existing watch event, no timer),
stands again after a resize, opens from the one-row folded strip's routes, and stands in a 60×16
window, where the wheel reads it on. Keys and presses stay the popup's: the native's draft is
untouched. Frames are text read back from a private headless terminal; nothing here reaches a real
account, session or terminal (Fleet's collection is a fixture).
11-25 (four groups): the side panel's boxes read `ROUTE 2` / `QUESTIONS 1` (the count follows the name); the
folded strip (the bottom layout) and the popup's own title keep `2 ROUTE`. A group head row opens
with the frame's `├` (not `│`), so the draft read cuts at either.
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
OTHER = '00000000-0000-4000-8000-000000000012'

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


# The backend as installed, with fixture seams only where a real one would read this machine: Fleet's
# collection (a view model from `vm.json`, which the test rewrites), the shared numbers, the LAB board,
# the questions each session's board holds, and the answer API (it records each request it gets).
BACKEND = r"""#!{python}
import json,os,sys,time
sys.path.insert(0,{utilities!r});sys.path.insert(0,{tools!r})
import session_shell as m
from fleet import session_tags
OWN,OTHER={sid!r},{other!r}
def tag(harness,sid):return {{OWN:'e9',OTHER:'12'}}.get(sid)
session_tags.assigned_tag=tag
def vm(harness,sid,kind):
    from fleet.explorer.view_model import ExplorerViewModel
    while os.path.exists({vm!r}+'.hang'):time.sleep(0.2)
    return ExplorerViewModel.from_mapping(json.load(open({vm!r})))
m._popup_view_model=vm
def answered():
    try:return [json.loads(line) for line in open({answers!r})]
    except OSError:return []
def questions(harness,sid,**_kw):
    items=json.load(open({questions!r})).get(sid,[]);done={{a.get('question_id') for a in answered()}}
    for item in items:
        if item['id'] in done:item['request_state']='submitted'
    return items
def answer(binding,request):
    sent={{k:request[k] for k in ('question_id','revision','answers','decision','owner') if k in request}}
    with open({answers!r},'a') as f:f.write(json.dumps(sent,ensure_ascii=False)+'\n')
    return {{'ok':True,'state':'submitted'}}
m.session_questions=questions;m._block_answer=answer
raise SystemExit(m._backend())
"""

DRIVER = r"""
const fs=require('node:fs');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let cols=160,rows=40,raw='';
const term=new Terminal({cols,rows,allowProposedApi:true,scrollback:0});
term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const child=pty.spawn(cfg.python,[cfg.entry,cfg.harness,cfg.python,cfg.fake,cfg.harness,cfg.sid],{cols,rows,cwd:cfg.project,
  env:{...process.env,TERM:'xterm-256color',NODE_OPTIONS:'--require '+cfg.launcher}});
child.onData(d=>{term.write(d);raw+=d;});
const flush=()=>new Promise(r=>term.write('',r));
const cells=y=>{const line=term.buffer.active.getLine(y),out=[];for(let x=0;x<cols;x++){const c=line?.getCell(x);out.push(!c?' ':c.getWidth()===0?'':c.getChars()||' ');}return out;};
const screen=async()=>{await flush();return Array.from({length:rows},(_,y)=>cells(y).join('').trimEnd());};
const at=async needle=>{await flush();for(let y=0;y<rows;y++){const c=cells(y),where=[];let text='';
  c.forEach((ch,x)=>{for(const _ of ch)where.push(x);text+=ch;});const i=text.indexOf(needle);if(i>=0)return {x:where[i],y};}return null;};
const until=async(test,what,ms=15000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return s;await sleep(40);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>line.includes(word));
const press=async(x,y,ms=150)=>{child.write(`\x1b[<0;${x+1};${y+1}M\x1b[<0;${x+1};${y+1}m`);await sleep(ms);};
const wheel=async(x,y,down=true)=>{child.write(`\x1b[<${down?65:64};${x+1};${y+1}M`);await sleep(150);};
const key=async(bytes,ms=200)=>{child.write(bytes);await sleep(ms);};
const frames={},checks={};
// The popup's rows: its top line `╭─ … ✕ ─╮` to its bottom line; `title` the top line, `foot` the bottom one.
// (Its bottom line is the one under its own `╭`: the panel's route box beside it has a `╰─ … ╯` of its own.)
const box=s=>{const top=s.findIndex(l=>l.includes('╭─')&&l.includes('✕ ─╮'));if(top<0)return null;
  const left=[...s[top]].indexOf('╭'),bottom=s.findIndex((l,i)=>i>top&&[...l][left]==='╰'&&l.includes('╯'));
  return {top,bottom,title:s[top],foot:s[bottom]||'',rows:s.slice(top,bottom+1),left};};
// 11-22: the question popup's ←→ no longer change sections; a section is reached by a press on its name on the
// popup's bottom line (or on the panel's section head).
const toSection=async(name,ms=300)=>{await flush();const s=await screen(),b=box(s);if(!b)throw Error('no popup to press in');
  const text=[],where=[];cells(b.bottom).forEach((ch,x)=>{for(const _ of ch){where.push(x);text.push(_);}});
  const i=text.join('').indexOf(name);if(i<0)throw Error('no '+name+' on the bottom line: '+b.foot);await press(where[i],b.bottom,ms);};
const popupHas=word=>s=>{const b=box(s);return !!b&&b.rows.some(l=>l.includes(word));};
const unfolded=s=>{const b=box(s);return b?b.rows.find(l=>l.includes('▶'))||'':'';};
// 11-15 (panel polish 3): how many cells outside the standing popup (its frame and the empty cell
// round it) do not read as the one gray (another color, weight or ground; a ground on an empty cell).
const outside=async()=>{const s=await screen(),b=box(s);if(!b)return -1;const c=cells(b.top),left=c.indexOf('╭'),right=c.indexOf('╮');let n=0;
  for(let y=0;y<rows;y++){const line=term.buffer.active.getLine(y);for(let x=0;x<cols;x++){if(y>=b.top-1&&y<=b.bottom+1&&x>=left-1&&x<=right+1)continue;
    const cell=line?.getCell(x);if(!cell)continue;const blank=!cell.getChars().trim();
    if(!cell.isBgDefault()||cell.isInverse()||!blank&&(cell.isFgDefault()||cell.getFgColor()!==239||cell.isBold()||cell.isDim()))n++;}}return n;};
const veil={};
// 11-15 (the person: "팝업은 ... 배경 컬러도 다르고 폰트가 좀 더 밝아야"): the standing popup's frame cells and how
// many stand on the popup's ground (palette 235, the dark face's: the fixture's face is unknown) or read
// dim (a hue too), in the terminal's own color or in a gray fainter than the floor (palette 232-246); and anywhere
// on the screen, the cells on that ground (none once it is closed).
// (11-15: "스페이스바 한칸 정도 여백을 더") the cell each side of the frame on its rows is on the ground too
// (`margins`), and the cell past it is not (`beyond`).
const ground=async()=>{const s=await screen(),b=box(s);if(!b)return null;const c=cells(b.top),left=c.indexOf('╭'),right=c.indexOf('╮');let all=0,on=0,faint=0,margins=0,beyond=0;
  const grounded=cell=>cell&&cell.isBgPalette()&&cell.getBgColor()===235;
  for(let y=b.top;y<=b.bottom;y++){const line=term.buffer.active.getLine(y);for(let x=left;x<=right;x++){const cell=line?.getCell(x);if(!cell||cell.getWidth()===0)continue;all++;
    if(grounded(cell))on++;
    if(cell.getChars().trim()&&(cell.isDim()||cell.isFgDefault()||cell.isFgPalette()&&cell.getFgColor()>=232&&cell.getFgColor()<247))faint++;}
    for(const x of [left-1,right+1])if(grounded(line?.getCell(x)))margins++;for(const x of [left-2,right+2])if(grounded(line?.getCell(x)))beyond++;}
  return {all,on,faint,margins,beyond,rows:b.bottom-b.top+1};};
const grounded=async()=>{await flush();let n=0;for(let y=0;y<rows;y++){const line=term.buffer.active.getLine(y);for(let x=0;x<cols;x++){const cell=line?.getCell(x);
  if(cell&&cell.isBgPalette()&&cell.getBgColor()===235)n++;}}return n;};
// (Private, as the shared cache's reader requires.)
const write=(file,value)=>{fs.writeFileSync(file+'.new',JSON.stringify(value),{mode:0o600});fs.renameSync(file+'.new',file);};
(async()=>{try{
  await until(s=>has('READY_'+cfg.harness)(s)&&has('ROUTE 2')(s)&&has('QUESTIONS')(s),'the panel with its sections');
  await key('draft9',300);
  frames.closed=await screen();
  // 11-15 (the route box again): a press on a route's own row in the panel's route box opens the routes
  // popup with that route unfolded (here the working one, not the most pressing one); the popup stands on
  // its own ground with bright words, and closed it leaves no ground behind.
  {const at0=await at('질문 팝업 구현');await press(at0.x+2,at0.y);
   const t=await until(s=>unfolded(s).includes('질문 팝업'),'the pressed route unfolded');frames.route_row=t;
   checks.ground=await ground();await key('\x1b',300);await until(s=>!box(s),'closed after the route row');checks.ground_left=await grounded();}
  // A press on the ROUTE section's head: Fleet's routes table, the most pressing route unfolded.
  let p=await at('ROUTE 2');await press(p.x+2,p.y);
  let s=await until(popupHas('PROGRESS'),'the routes popup');
  s=await until(s=>unfolded(s).includes('휠 입력'),'the failed route unfolded');frames.route=s;veil.route=await outside();
  checks.route_title=box(s).title;checks.route_foot=box(s).foot;
  // ↑↓ unfold the route before or after it in Fleet's order; Enter its page (Fleet's route page); Enter again the table.
  await key('\x1b[A',300);s=await until(s=>unfolded(s).includes('질문 팝업'),'the route before it unfolded');frames.route_down=s;
  await key('\r',300);s=await until(s=>!!box(s)&&box(s).rows.some(l=>l.includes('╔')),'the route page');frames.route_page=s;veil.route_page=await outside();
  await key('\r',300);s=await until(s=>!!box(s)&&!box(s).rows.some(l=>l.includes('╔'))&&popupHas('PROGRESS')(s),'the table again');
  // While it stands the route is renamed: the projection's route rows change (the existing watch on the
  // shared cache) and the popup reads Fleet's table again.
  const model=JSON.parse(fs.readFileSync(cfg.vm,'utf8'));model.routes[0].title='RENAMED_ROUTE 이름 바뀜';write(cfg.vm,model);
  const cache=JSON.parse(fs.readFileSync(cfg.cache,'utf8'));cache.observed_at=Date.now()/1000;
  cache.route_panels[cfg.key][0].title='RENAMED_ROUTE 이름 바뀜';write(cfg.cache,cache);
  s=await until(popupHas('RENAMED_ROUTE'),'the routes popup drawn again');frames.route_changed=s;
  // → the next section with something in it (11-15: the LAB and SESSIONS popups are gone; no message here):
  // the question popup; ← back.
  await key('\x1b[C',300);s=await until(s=>!!box(s)&&box(s).title.includes('QUESTIONS'),'the question popup by →');frames.next=s;checks.next_foot=box(s).foot;veil.next=await outside();
  await toSection('ROUTE');s=await until(popupHas('PROGRESS'),'back to the routes');veil.back=await outside();
  await key('\x1b',300);s=await until(s=>!box(s),'closed after the sections');
  // Nothing of it reached the native: its draft stands, never submitted.
  s=await screen();checks.draft=s.some(l=>l.startsWith('❯ draft9'));checks.submitted=s.some(l=>l.includes('DONE_draft9'));
  checks.native_mouse=raw.includes('NATIVE_ESC_');
  // A resize while a popup stands: it stands again in the middle, Fleet's table read at the new width.
  await key('\x1d',300);await until(s=>!!box(s),'the question popup by Ctrl+]');
  await toSection('ROUTE');await until(popupHas('PROGRESS'),'the routes popup by a press on its name');
  await resize(120,30);s=await until(s=>!!box(s)&&popupHas('ROUTE')(s),'the popup after a resize');frames.resized=s;veil.resized=await outside();
  checks.resized_width=[...box(s).title.trimEnd()].length-box(s).left;checks.resized_left=box(s).left;
  await key('\x1b',300);await until(s=>!box(s),'closed');
  // The folded strip (90 columns, one row): its routes group opens the routes popup; the strip stays.
  await resize(90,24);s=await until(s=>s.at(-1).includes('2 ROUTE')&&!s.some(l=>l.includes('REPO')),'the folded strip');frames.strip=s;
  checks.strip_one_row=!/LAB|SESSIONS|QUESTIONS ─/.test(s.join('\n'))&&s.at(-1).includes('▴');
  p=await at('2 ROUTE');await press(p.x,p.y,300);s=await until(popupHas('PROGRESS'),'the routes popup from the strip');frames.strip_route=s;
  checks.strip_kept=s.slice(-1).join('\n')===frames.strip.slice(-1).join('\n');veil.strip_route=await outside();
  await key('\x1b',300);await until(s=>!box(s),'closed');
  // 60×16: the question popup and the routes popup stand in it; the wheel over them reads them on.
  await resize(60,16);await key('\x1d',400);s=await until(s=>!!box(s),'the question popup at 60×16');frames.narrow_question=s;
  const before=box(s).rows.join('\n');const b=box(s);await wheel(b.left+4,b.top+2);await wheel(b.left+4,b.top+2);
  s=await screen();checks.wheel_question=!!box(s)&&box(s).rows.join('\n')!==before;
  await toSection('ROUTE');s=await until(popupHas('ROUTE'),'the routes popup at 60×16');
  s=await until(s=>!!box(s)&&box(s).rows.some(l=>/│ \d+\/\d+ · /.test(l)),'a routes popup longer than its rows');frames.narrow_route=s;
  const page=box(s).rows.join('\n');await wheel(box(s).left+4,box(s).top+2);await wheel(box(s).left+4,box(s).top+2);
  s=await screen();checks.wheel_table=!!box(s)&&box(s).rows.join('\n')!==page;frames.narrow_wheel=s;
  checks.narrow_width=Math.max(...s.map(l=>[...l].length));veil.narrow_route=await outside();checks.veil=veil;
  await key('\x1b',300);s=await until(s=>!box(s),'closed at 60×16');
  checks.native_mouse_end=raw.includes('NATIVE_ESC_');
  s=await screen();checks.draft_end=s.some(l=>l.startsWith('❯ draft9'));checks.submitted_end=s.some(l=>l.includes('DONE_draft9'));
  // The Esc rule (#1641's fixes, the question popup's editor) is every popup's: on a popup, ESC then a
  // control byte (Tab, ^C) is dropped and the popup stands; ESC ESC closes it and the second is dropped;
  // a lone Esc closes it and a lone Esc within a second after is the shell's. Nothing reaches the native
  // (it shows every control byte it gets); after that second a lone Esc and a letter reach it.
  await resize(160,40);
  // The native's input row, its own cells only (the panel beside it starts at its frame `│`).
  const draft=async()=>{const t=(await screen()).find(l=>l.startsWith('❯ '));return t?t.slice(2).split(/[│├]/)[0].trimEnd():null;};
  const kinds=['QUESTIONS','ROUTE'];
  const open=async kind=>{await key('\x1d',400);await until(s=>!!box(s),'a popup by Ctrl+]');
    if(kind==='ROUTE')await toSection('ROUTE',400);
    return until(s=>!!box(s)&&box(s).title.includes(kind),'the '+kind+' popup');};
  checks.esc={};
  for(const kind of kinds){
    const r={};
    await open(kind);
    await key('\x1b\t',300);s=await screen();r.esc_tab_stands=!!box(s)&&box(s).title.includes(kind);
    await key('\x1b\x03',300);s=await screen();r.esc_ctrl_c_stands=!!box(s)&&box(s).title.includes(kind);
    await key('\x1b\x1b',300);s=await screen();r.esc_esc_closes=!box(s);
    r.after_esc_esc=await draft();
    await open(kind);
    await key('\x1b',200);s=await screen();r.lone_esc_closes=!box(s);
    await key('\x1b',300);s=await screen();r.second_esc_stays=!box(s);
    r.after_second=await draft();
    checks.esc[kind]=r;
  }
  await sleep(1200);await key('\x1b',300);await key('Q',300);checks.esc_after_a_second=await draft();
  // The popup's input state is the popup's. A paste's end mark (`ESC[201~`) may arrive split at any of its
  // five joints: the keys live (→ moves the section, Esc closes) and the pasted text goes nowhere (no `p r w f`,
  // no Enter of it reaches the native). A paste that never ends, and a key's front, end with the popup: Esc,
  // Ctrl+] and a press outside close it, and the next popup is normal.
  await key('z',300);  // (the `ESC Q` the native took above leaves the host's key reader mid-key; a letter ends it)
  const base=await draft();checks.paste={base};
  const END='\x1b[201~',kept=async()=>(await draft())===base&&!raw.includes('NATIVE_ESC_');
  for(const cut of [1,2,3,4,5]){
    await open('ROUTE');
    await key('\x1b[200~prwf hello\rworld\x1b[A'+END.slice(0,cut),150);await key(END.slice(cut),150);
    await key('\x1b[C',300);s=await screen();
    const r={moved:!!box(s)&&box(s).title.includes('QUESTIONS'),draft:await kept()};
    await key('\x1b',300);s=await screen();r.closed=!box(s);checks.paste['split'+cut]=r;
  }
  // Never ended: the three ways out, each then a normal popup (→ moves it, Esc closes it).
  const fresh=async name=>{await open('ROUTE');await key('\x1b[C',300);const t=await screen();const ok=!!box(t)&&box(t).title.includes('QUESTIONS');
    await key('\x1b',300);return ok&&!box(await screen());};
  for(const way of ['ctrl','esc','press','tail']){
    await open('ROUTE');
    await key(way==='tail'?'\x1b[200~abc\x1b[2':'\x1b[200~abc prwf',200);
    if(way==='ctrl')await key('\x1d',300);
    else if(way==='esc')await key('\x1b',500);
    else if(way==='press')await press(2,1,300);
    else await key('\x1d',300);
    s=await screen();const r={closed:!box(s)};
    r.next=await fresh();r.draft=await kept();checks.paste['never_'+way]=r;
  }
  // A paste's front and a key's front, left standing when the popup closes by Esc, are gone with it: the
  // next popup's `01~` and → are keys of its own.
  await open('ROUTE');await key('\x1b[200~abc\x1b[',200);await key('\x1b',500);s=await screen();
  checks.paste.front_closed=!box(s);await open('ROUTE');await key('01~',200);await key('\x1b[C',300);s=await screen();
  checks.paste.front_next=!!box(s)&&box(s).title.includes('QUESTIONS');await key('\x1b',300);checks.paste.front_draft=await kept();
  // ↑↓ follow: a popup longer than its rows keeps the unfolded row in view, up and down (60×16).
  await resize(60,16);await key('\x1d',400);await until(s=>!!box(s),'a popup at 60×16');await toSection('ROUTE',400);
  s=await until(popupHas('ROUTE'),'the routes popup at 60×16');
  // (11-18: a table read before stands at once with words after its title; the keys below are for the one read now.)
  await until(s=>unfolded(s)!==''&&!/에 읽은 내용/.test(box(s).title),'the unfolded route in view, read now');
  const place=s=>{const m=(box(s)?box(s).rows.join('\n'):'').match(/(\d+)\/(\d+) · /);return m?[+m[1],+m[2]]:null;};
  checks.follow={down:[],up:[],place:[]};
  for(let i=0;i<14;i++){await key('\x1b[B',350);s=await screen();checks.follow.down.push(unfolded(s)!=='');checks.follow.place.push(place(s));}
  for(let i=0;i<14;i++){await key('\x1b[A',350);s=await screen();checks.follow.up.push(unfolded(s)!=='');checks.follow.place.push(place(s));}
  await key('\x1b',300);await resize(160,40);
  // Children: the popup opened and closed 100 times leaves no read behind, and a read that hangs ends at 12 s.
  const reads=()=>{const kids=new Map();for(const id of fs.readdirSync('/proc').filter(n=>/^\d+$/.test(n))){try{
    const st=fs.readFileSync('/proc/'+id+'/stat','utf8'),after=st.slice(st.lastIndexOf(')')+2).split(' ');
    kids.set(+id,{ppid:+after[1],cmd:fs.readFileSync('/proc/'+id+'/cmdline','utf8')});}catch{}}
    const live=new Set([child.pid]);for(let again=true;again;){again=false;for(const [id,k] of kids)if(!live.has(id)&&live.has(k.ppid)){live.add(id);again=true;}}
    return [...live].filter(id=>kids.get(id)&&kids.get(id).cmd.includes(cfg.backend)).length;};
  const idle=reads();checks.children={idle};
  {const head=await at('ROUTE 2');for(let i=0;i<100;i++){await press(head.x+2,head.y,60);child.write('\x1b');await sleep(60);}}
  await sleep(2500);checks.children.after_100=reads();
  if(cfg.harness==='claude'){
    fs.writeFileSync(cfg.vm+'.hang','');{const head=await at('ROUTE 2');await press(head.x+2,head.y,500);}
    checks.children.hanging=reads();await sleep(13500);checks.children.ended=reads();
    s=await screen();checks.children.stood=!!box(s);
    fs.unlinkSync(cfg.vm+'.hang');await key('\x1b',300);
  }
  fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
async function resize(c,r){cols=c;rows=r;term.resize(c,r);child.resize(c,r);await sleep(500);}
"""


def route_row(rid, title, state, stage, st):
    return {'id': rid, 'title': title, 'state': state, 'worker_stage': stage, 'shape': 'one-shot-owner',
            'stages': [{'name': stage, 'st': st}], 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'high'}


def fleet_route(rid, title, state, stage, st, worker=None):
    return {'id': rid, 'session_tag': 'e9', 'repo': 'project', 'title': title, 'state': state, 'harness': 'claude',
            'model': 'Opus 5.5', 'effort': 'high', 'owner_gauge': [40, 0.2], 'age': '23m', 'capability': 'autopilot-code',
            'mode': 'dev', 'intensity': 'quick', 'stages': [{'name': stage, 'st': st}], 'worker_stage': stage,
            **({'worker': worker} if worker else {})}


class SessionShellTablePopupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hst-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('shell_table_popup_isolation', ROOT / 'tools/run-tests.py')
        runner = importlib.util.module_from_spec(spec); sys.modules[spec.name] = runner; spec.loader.exec_module(runner)
        cls.env = runner.build_isolated_env(cls.root, repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR'] = str(cls.root / 'native-claude')
        cls.env['FLEET_NOW_LANG'] = 'ko'
        policy = Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME']) / '.config') / 'hearting/session-shell.json'
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({'enabled': True, 'layout': 'side', 'side_width': 40, 'side_min_columns': 132}))
        policy.chmod(0o600)
        for name in ('npm-cache', 'node-gyp'):
            cls.env['npm_config_' + ('cache' if name == 'npm-cache' else 'devdir')] = str(cls.root / name)
        cls.env['npm_config_userconfig'] = str(cls.root / 'npmrc'); (cls.root / 'npmrc').write_text('')
        sys.path[:0] = [str(ROOT / 'tools/install'), str(ROOT / 'utilities')]
        import session_shell_install
        with mock.patch.dict(os.environ, cls.env, clear=True):
            assets = session_shell_install.prepare_assets()
        cls.env['NODE_PATH'] = str(assets / 'node_modules')
        # The shared cache's producer would rewrite the seeded values from this fixture's real state (it
        # has none): its lock is held for the class, so the seeded cache stays (as the succession test does).
        cache = Path(cls.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True); cache.chmod(0o700)
        cls.lock = os.open(cache / '.projection.lock', os.O_CREAT | os.O_RDWR, 0o600)
        cls.addClassCleanup(os.close, cls.lock)
        fcntl.flock(cls.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cls.fake = cls.root / 'native.py'; cls.fake.write_text(FAKE_NATIVE)
        cls.launcher = cls.root / 'launcher.cjs'; cls.launcher.write_text(LAUNCHER)
        cls.driver = cls.root / 'driver.cjs'; cls.driver.write_text(DRIVER)

    def run_shell(self, harness):
        case = self.root / harness; case.mkdir(mode=0o700)
        project = case / 'project'; project.mkdir()
        sid = {'claude': '00000000-0000-4000-8000-0000000000c1', 'codex': '00000000-0000-4000-8000-0000000000c2',
               'opencode': 'ses_tablepopupfixture'}[harness]
        cache = Path(self.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True); cache.chmod(0o700)
        key = harness + '\0' + sid
        target = cache / 'projection.json'
        value = json.loads(target.read_text()) if target.exists() else {
            'schema_version': 3, 'items': [], 'by_session': {}, 'total': 0, 'older': 0, 'complete': True,
            'route_panels': {}, 'route_status': {}, 'route_sessions': {}}
        value.update(observed_at=time.time())
        # Two routes of this session (one failed) and another session's wait for the person.
        value['route_panels'][key] = [route_row('rt-a000000000000001', '질문 팝업 구현', 'working', 'one-shot', 'active'),
                                      route_row('rt-a000000000000002', '휠 입력 시험', 'dead', 'one-shot', 'failed')]
        value['route_status'][key] = 'route 2 ⠏1 ✕1'; value['route_sessions'][key] = time.time() + 3600
        value['items'] = [item for item in value['items'] if item.get('id') != 'S4:other'] + [
            {'id': 'S4:other', 'source': 'S4', 'since': time.time(), 'owner': {'harness': 'claude', 'session_id': OTHER}}]
        target.write_text(json.dumps(value)); target.chmod(0o600)
        vm = case / 'vm.json'
        vm.write_text(json.dumps({'repos': [{'id': 'project', 'name': 'project'}, {'id': 'speech-lab', 'name': 'speech-lab'}], 'system': {},
            'routes': [fleet_route('rt-a000000000000001', '질문 팝업 구현 — 창 가운데 팝업', 'working', 'one-shot', 'active'),
                       fleet_route('rt-a000000000000002', '휠 입력 시험', 'dead', 'one-shot', 'failed',
                                   ['owner', [['BLOCKED: 사설 PTY를 열지 못함 — 휠 시험을 돌리지 못했다', 'lvl_r']], [['dead', 'g_dead']]])]
                      + [fleet_route('rt-a00000000000%04d' % n, '지난 route %d' % n, 'done', 'one-shot', 'done') for n in range(10, 22)],
            'sessions': [
                {'tag': 'e9', 'repo_id': 'project', 'harness': harness, 'model': 'Opus 5.5', 'effort': 'high', 'context': 46,
                 'window': 200000, 'state': 'working', 'title': '이 창', 'now': '그리는 중', 'age': '23m'},
                {'tag': '12', 'repo_id': 'speech-lab', 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'high', 'context': 58,
                 'window': 200000, 'state': 'blocked', 'answer_wait_marker': '◑', 'title': '분리 모델 학습 비교', 'now': '답 대기', 'age': '41m'},
                {'tag': '73', 'repo_id': 'project', 'harness': 'codex', 'model': 'gpt-5.5', 'effort': 'high', 'context': 33,
                 'window': 400000, 'state': 'idle', 'title': 'v4.32.7 설치 확인', 'now': '', 'age': '1h'}]}, ensure_ascii=False))
        now = time.time()
        iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(t)))  # noqa: E731
        cond = lambda cid, name, run, reached, anomalies=(): {  # noqa: E731
            'id': cid, 'display_name': name, 'run_status': run, 'anomalies': list(anomalies),
            'current_run': {'run_id': 'r-' + cid, 'started_at': iso(now - 7200)} if run else None,
            'progress': {'reached': reached, 'total': 60, 'unit': 'epoch'}, 'latest': None, 'best': None,
            'comparison': {'state': 'no-data'}, 'observed_at': iso(now - 60), 'observed_age_seconds': 60}
        lab = case / 'lab.json'
        lab.write_text(json.dumps({'schema': 'experiment-status/v1', 'generated_at': iso(now - 60),
            'catalog': {'status': 'ok', 'path': 'experiments/catalog.yaml', 'revision': 1, 'updated_at': iso(now - 3600)},
            'groups': [{'id': 'sep', 'display_name': '분리 모델 비교', 'goal': 'SI-SDR 13 dB 넘기기',
                        'metric': {'key': 'si_sdr', 'unit': 'dB', 'direction': 'max'}, 'target': {'kind': 'condition', 'condition': 'c0'},
                        'conditions': [cond('c0', '기준 모델', None, 40), cond('c3', '긴 문맥', 'running', 9, ('status-failed',))]}],
            'unlinked_runs': {'run_ids': [], 'truncated': 0}, 'warnings': []}, ensure_ascii=False))
        questions = case / 'questions.json'; answers = case / 'answers.jsonl'
        questions.write_text(json.dumps({
            sid: [{'id': 'S4:own', 'revision': 'a' * 64, 'answerable': True, 'questions': [
                {'id': 'shape', 'question': '이 창의 질문입니다. ' + ' '.join('긴 본문 %d줄' % n for n in range(1, 40)),
                 'choices': ['첫째 안', '둘째 안']}]}],
            OTHER: [{'id': 'S4:other', 'revision': 'c' * 64, 'answerable': True, 'questions': [
                {'id': 'lr', 'question': '학습률을 얼마로 할까요?', 'choices': ['1e-4', '3e-4']}]}]}, ensure_ascii=False))
        backend = case / 'backend'
        backend.write_text(BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'), tools=str(ROOT / 'tools'),
                                          sid=sid, other=OTHER, vm=str(vm), lab=str(lab), questions=str(questions), answers=str(answers)))
        backend.chmod(0o700)
        config = {'harness': harness, 'sid': sid, 'key': key, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'vm': str(vm), 'cache': str(target),
                  'project': str(project), 'result': str(case / 'result.json')}
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
        result['answers'] = [json.loads(line) for line in answers.read_text().splitlines()] if answers.exists() else []
        if SHOW_FRAMES:  # `--frames`: the frames this run read, for a person to look at
            print('FRAMES ' + harness + ' ' + json.dumps(result, ensure_ascii=False), flush=True)
        return result

    def check(self, harness):
        result = self.run_shell(harness); frames, checks = result['frames'], result['checks']
        text = lambda name: '\n'.join(frames[name])  # noqa: E731
        # The panel: each section its head and rows (11-15, design E).
        panel = text('closed')
        for head in ('QUESTIONS 1', '╭─ ROUTE 2 '):
            self.assertIn(head, panel)
        # (11-15: no LAB and no SESSIONS section, whatever the backend sends.)
        self.assertNotRegex(panel, r'\bLAB\b|SESSIONS|분리 모델')
        # The route box again (11-15): a press on a route's own row opened the popup with it unfolded; the
        # popup's every frame cell stood on its ground, no word in it faint; closed, no ground was left.
        self.assertIn('질문 팝업', text('route_row'))
        ground = checks['ground']
        self.assertTrue(ground['all'] > 200 and ground['on'] == ground['all'] and ground['faint'] == 0, ground)
        self.assertEqual((ground['margins'], ground['beyond']), (2 * ground['rows'], 0), ground)
        self.assertEqual(checks['ground_left'], 0)
        # The routes popup: Fleet's table, its title the section's head, the sections on its bottom line.
        # 11-15 (panel polish 3): behind every popup, through ←→, a page, a resize, the strip and 60×16,
        # everything outside it reads in the one gray.
        self.assertEqual(checks['veil'], {name: 0 for name in ('route', 'route_page', 'next', 'back',
                                                               'resized', 'strip_route', 'narrow_route')}, checks['veil'])
        self.assertIn('2 ROUTE ⠏1 ✕1', checks['route_title'])
        for name in ('QUESTIONS', 'ROUTE'):
            self.assertIn(name, checks['route_foot'])
        self.assertNotRegex(checks['route_foot'] + checks['next_foot'], r'LAB|SESSIONS')
        self.assertIn('BLOCKED:', text('route'))
        self.assertIn('╔', text('route_page'))
        self.assertIn('RENAMED_ROUTE', text('route_changed'))
        # No answer goes from these popups (the other session's question is no longer opened here).
        self.assertEqual(result['answers'], [])
        # Nothing reached the native: its draft stands unsubmitted; no press reached a native that reads the mouse.
        self.assertTrue(checks['draft'] and checks['draft_end'])
        self.assertFalse(checks['submitted'] or checks['submitted_end'])
        self.assertFalse(checks['native_mouse'] or checks['native_mouse_end'])
        self.assertTrue(checks['strip_kept'], text('strip_route'))
        self.assertTrue(checks['strip_one_row'], text('strip'))
        self.assertIn('PROGRESS', text('strip_route'))
        self.assertLessEqual(checks['narrow_width'], 60)
        self.assertTrue(checks['wheel_question'], text('narrow_question'))
        self.assertTrue(checks['wheel_table'], text('narrow_wheel'))
        # The Esc rule is the same in every popup (11-15, #1641's fixes): nothing reached the native.
        for kind in ('QUESTIONS', 'ROUTE'):
            self.assertEqual(checks['esc'][kind], {'esc_tab_stands': True, 'esc_ctrl_c_stands': True, 'esc_esc_closes': True,
                                                   'after_esc_esc': 'draft9', 'lone_esc_closes': True, 'second_esc_stays': True,
                                                   'after_second': 'draft9'}, kind)
        # A second after the close, a lone Esc and a letter are the native's again.
        self.assertEqual(checks['esc_after_a_second'], 'draft9^[Q')
        # A paste's end mark split at any joint leaves the keys alive; the pasted text goes nowhere.
        paste = checks['paste']
        for cut in range(1, 6):
            self.assertEqual(paste['split%d' % cut], {'moved': True, 'draft': True, 'closed': True}, 'split at %d' % cut)
        for way in ('ctrl', 'esc', 'press', 'tail'):
            self.assertEqual(paste['never_' + way], {'closed': True, 'next': True, 'draft': True}, way)
        self.assertTrue(paste['front_closed'] and paste['front_next'] and paste['front_draft'], paste)
        # ↑↓ keep the unfolded row in view; its place reads `i/N` with i inside N.
        follow = checks['follow']
        self.assertTrue(all(follow['down']) and all(follow['up']), follow)
        self.assertTrue(all(place and 1 <= place[0] <= place[1] for place in follow['place']), follow)
        self.assertGreater(len({place[0] for place in follow['place']}), 3, follow)
        # No read is left behind by 100 openings; a read that hangs (Claude's run) is ended at its 12 seconds.
        children = checks['children']
        self.assertEqual(children['after_100'], children['idle'], children)
        if harness == 'claude':
            self.assertGreater(children['hanging'], children['idle'], children)
            self.assertEqual(children['ended'], children['idle'], children)
            self.assertTrue(children['stood'], children)
        return result

    def test_claude(self):
        self.check('claude')

    def test_codex(self):
        self.check('codex')

    def test_opencode(self):
        self.check('opencode')


if __name__ == '__main__':
    if '--frames' in sys.argv:
        sys.argv.remove('--frames'); SHOW_FRAMES = True
    unittest.main()
