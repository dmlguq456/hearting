"""11-15: the question popup over the real host in a private PTY, for each harness's synthetic native.

Press → popup → answer → closed; native output while it stands; a resize; opening it from the
folded strip; a 60×16 window; the moving frame beside it. Frames are text read back from a
private headless terminal; nothing here reaches a real account, session or terminal.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
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

# A native that reports its own start, paints `❯ <what is typed>` and answers each line with
# `DONE_<line>`. `later` prints twelve lines a little later (output while the popup stands);
# `busy`/`calm` report a turn's start and end (the moving frame). Codex and OpenCode read the
# mouse: every escape sequence they receive is shown as `NATIVE_ESC_…`. 11-15 (panel polish 3):
# `paint` adds a row in its own colors (bold red, a blue ground, inverse); `show` brings, a little
# later, every kind of output (lines one at a time, a scroll, the alternate screen in and out, one
# inverse cell poked in); `flood` a line every 2 ms for 1.5 s; a size change shows as `WINCH_N`.
FAKE_NATIVE = r"""
import json,os,select,signal,socket,sys,time,tty
harness,sid=sys.argv[1],sys.argv[2]
winch=[];signal.signal(signal.SIGWINCH,lambda *_:winch.append(1))
PAINT='\x1b[1;31mRED_TEXT\x1b[0m \x1b[44mBLUE_BG\x1b[0m \x1b[7mINVERSE\x1b[0m\r\n'
def event(kind):
    raw=open('/proc/self/stat').read();start=raw.rsplit(') ',1)[1].split()[19]
    req={'op':'event','event':kind,'harness':harness,'sid':sid,'pid':os.getpid(),'start':start,
         'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET']);c.sendall((json.dumps(req)+'\n').encode());c.recv(8192)
if harness=='opencode':
    # The TUI's own selection record (the plugin writes it): without it the observation of an OpenCode
    # session is empty (an identity it cannot prove), so no title, model or git stands in its panel.
    raw=open('/proc/self/stat').read();born=raw.rsplit(') ',1)[1].split()[19]
    folder=os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.join(os.environ['HOME'],'.local','state'),'hearting','tui-identity')
    os.makedirs(folder,exist_ok=True)
    open(os.path.join(folder,'%d-%s.json'%(os.getpid(),born)),'w').write(json.dumps({'schema':'hearting-tui-selection-v1','pid':os.getpid(),'start':born,'sessionID':sid}))
tty.setraw(0)
event('start')
pending='';said='READY_'+harness;later=[];seen=[];colored=False
def screen():
    print('\x1b[2J\x1b[H'+said+(' WINCH_%d'%len(winch) if winch else '')+'\r\n'+(PAINT if colored else '')+''.join(s+'\r\n' for s in seen[-14:])+'❯ '+pending.replace('\x1b','^')+'\r\n',end='',flush=True)
if harness!='claude':print('\x1b[?1000h\x1b[?1006h',end='',flush=True)
screen()
while True:
    wait=max(0,later[0][0]-time.monotonic()) if later else None
    ready,_,_=select.select([0],[],[],wait)
    while later and later[0][0]<=time.monotonic():
        item=later.pop(0)[1]
        if isinstance(item,tuple):print(item[1],end='',flush=True)
        else:seen.append(item);screen()
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
        if line=='paint':colored=True
        if line=='show':
            t=time.monotonic()+0.6
            later=[(t+i*0.03,'DRIP_%02d'%i) for i in range(8)]
            later+=[(t+0.3,('raw',''.join('SCROLL_%02d\r\n'%i for i in range(30)))),
                    (t+0.5,('raw','\x1b[?1049h\x1b[2J\x1b[H\x1b[1;33mALT_SCREEN\x1b[0m\r\n')),(t+0.8,('raw','\x1b[?1049l')),
                    (t+0.9,('raw','\x1b[3;3H\x1b[7;32mPOKE\x1b[0m'))]
        if line=='flood':
            t=time.monotonic()+0.6
            later=[(t+i*0.002,('raw','FLOOD_%04d '%i+'x'*60+'\r\n')) for i in range(750)]
        if line=='busy':event('prompt')
        if line=='calm':event('stop')
        said='DONE_'+line.replace('\x1b','^')
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

# The backend as installed, with two fixture seams: the questions this session's board holds
# (an answered one reads as sent), and the answer API, which records each request it gets.
BACKEND = r"""#!{python}
import json,os,sys
sys.path.insert(0,{utilities!r})
import session_shell as m
def answered():
    try:return [json.loads(line) for line in open({answers!r})]
    except OSError:return []
def questions(harness,sid,**_kw):
    items=json.load(open({questions!r}));done={{a.get('question_id') for a in answered()}}
    for item in items:
        if item['id'] in done:item['request_state']='submitted'
    return items
def answer(binding,request):
    sent={{k:request[k] for k in ('question_id','revision','answers','decision') if k in request}}
    with open({answers!r},'a') as f:f.write(json.dumps(sent,ensure_ascii=False)+'\n')
    return {{'ok':True,'state':'submitted'}}
m.session_questions=questions;m._block_answer=answer
raise SystemExit(m._backend())
"""

DRIVER = r"""
const fs=require('node:fs'),path=require('node:path');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let cols=160,rows=40,raw='';
const term=new Terminal({cols,rows,allowProposedApi:true,scrollback:0});
term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const child=pty.spawn(cfg.python,[cfg.entry,cfg.harness,cfg.python,cfg.fake,cfg.harness,cfg.sid],{cols,rows,cwd:cfg.project,
  env:{...process.env,TERM:cfg.term||'xterm-256color',...(cfg.no_color?{NO_COLOR:'1'}:{}),NODE_OPTIONS:'--require '+cfg.launcher}});
child.onData(d=>{term.write(d);raw+=d;});
const flush=()=>new Promise(r=>term.write('',r));
// Each row as one character per cell (a wide character's second cell is empty).
const cells=y=>{const line=term.buffer.active.getLine(y),out=[];for(let x=0;x<cols;x++){const c=line?.getCell(x);out.push(!c?' ':c.getWidth()===0?'':c.getChars()||' ');}return out;};
const screen=async()=>{await flush();return Array.from({length:rows},(_,y)=>cells(y).join('').trimEnd());};
// The cell where `needle` starts (a wide character's two cells hold one character of text).
const at=async needle=>{await flush();for(let y=0;y<rows;y++){const c=cells(y),where=[];let text='';
  c.forEach((ch,x)=>{for(const _ of ch)where.push(x);text+=ch;});const i=text.indexOf(needle);if(i>=0)return {x:where[i],y};}return null;};
const until=async(test,what,ms=12000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return s;await sleep(30);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>line.includes(word));
const press=async(x,y)=>{child.write(`\x1b[<0;${x+1};${y+1}M\x1b[<0;${x+1};${y+1}m`);await sleep(120);};
const key=async(bytes,ms=150)=>{child.write(bytes);await sleep(ms);};
const frames={},checks={};
// The popup's rectangle on screen: its `╭` row and column to its `╯`.
const box=s=>{const top=s.findIndex(l=>l.includes('╭─ ')&&l.includes('✕ ─╮'));if(top<0)return null;
  const bottom=s.findIndex((l,i)=>i>top&&l.includes('╰─')&&l.includes('╯'));return {top,bottom};};
const region=async()=>{await flush();const out=[];for(let y=0;y<rows;y++){const c=cells(y),i=c.indexOf('╭'),j=c.indexOf('╮');if(i>=0&&j>i&&c.slice(i,j+1).join('').includes('✕')){
  let b=y;while(b<rows&&!cells(b).includes('╯'))b++;for(let k=y;k<=b;k++)out.push(cells(k).slice(i-1,j+2).join(''));return out;}}return null;};
const resize=async(c,r)=>{cols=c;rows=r;term.resize(c,r);child.resize(c,r);await sleep(400);};
// One synchronized frame draws the popup: the paint that brings its top line begins with ?2026h
// and ends with ?2026l around it.
const synchronized=()=>{const at=raw.indexOf('╭─ ');if(at<0)return false;const open=raw.lastIndexOf('\x1b[?2026h',at),shut=raw.lastIndexOf('\x1b[?2026l',at);
  return open>shut&&raw.indexOf('\x1b[?2026l',at)>at;};
// 11-15 (panel polish 3): every cell's look, and the cells outside the popup (its frame and the empty
// cell round it) that do not read as the one gray: a letter in another color, weight or ground, or
// a ground on an empty cell. Where nothing is veiled (`veil` 0) the native's own colors stand.
const VEIL=239;
const looks=()=>{const out=[];for(let y=0;y<rows;y++){const line=term.buffer.active.getLine(y),row=[];
  for(let x=0;x<cols;x++){const c=line?.getCell(x);row.push(!c?null:{ch:c.getChars(),fg:c.isFgDefault()?-1:c.getFgColor(),bg:c.isBgDefault()?-1:c.getBgColor(),
    bold:!!c.isBold(),dim:!!c.isDim(),inverse:!!c.isInverse()});}out.push(row);}return out;};
const layer=s=>{const top=s.findIndex(l=>l.includes('╭')&&l.includes('╮'));if(top<0)return null;let bottom=top;while(bottom+1<s.length&&!s[bottom].includes('╯'))bottom++;
  const c=cells(top),left=c.indexOf('╭'),right=c.indexOf('╮');return {top:top-1,bottom:bottom+1,left:left-1,right:right+1};};
const bright=async()=>{const s=await screen(),l=layer(s),all=looks(),bad=[];
  all.forEach((row,y)=>row.forEach((c,x)=>{if(!c||l&&y>=l.top&&y<=l.bottom&&x>=l.left&&x<=l.right)return;
    const blank=!c.ch.trim();
    if(c.bg!==-1||c.inverse||!blank&&(c.fg!==VEIL||c.bold||c.dim))bad.push([y,x,c]);}));
  return bad;};
// The look of the cell where `word` starts.
const lookAt=async word=>{const p=await at(word);return p?looks()[p.y][p.x]:null;};
// The colors of the panel's line cells (its outer frame and rules), the 40 columns at the right.
const frameLooks=()=>{const all=looks(),out=[];for(let y=0;y<rows;y++){const c=cells(y);for(let x=Math.max(0,cols-40);x<cols;x++)if('─│┌┐└┘'.includes(c[x]))out.push(all[y][x].fg);}return JSON.stringify(out);};
(async()=>{try{
  if(cfg.scenario==='veil'||cfg.scenario==='plain'){
    await until(s=>has('READY_'+cfg.harness)(s)&&has('2 QUESTIONS')(s),'the panel with its questions');
    await key('paint\r',300);await key('busy\r',600);
    let s=await until(has('RED_TEXT'),'the colored row');
    checks.red_before=await lookAt('RED_TEXT');checks.blue_before=await lookAt('BLUE_BG');
    const moving=new Set();for(let i=0;i<14;i++){moving.add(frameLooks());await sleep(150);}checks.moving_before=moving.size;
    // Output of every kind while it stands: sampled every 40 ms for 1.6 s.
    await key('show\r',100);await key('\x1d',150);s=await until(s=>!!box(s),'the popup');
    checks.bright_open=(await bright()).length;checks.bright_sample=JSON.stringify((await bright()).slice(0,3));
    {const all=looks(),b=box(s),x=cells(b.top).indexOf('╭');checks.red_open=await lookAt('RED_TEXT');checks.corner=all[b.top][x];}
    const still=new Set();let leaks=0,samples=0;const seen=new Set();
    for(let t=0;t<40;t++){const b=await bright();leaks+=b.length;samples++;still.add(frameLooks());
      for(const w of ['DRIP_07','SCROLL_29','ALT_SCREEN','POKE'])if(has(w)(await screen()))seen.add(w);await sleep(40);}
    checks.leaks=leaks;checks.samples=samples;checks.seen=[...seen].sort();checks.still=still.size;
    s=await screen();checks.native_input=(s.find(l=>l.startsWith('❯'))||'').slice(0,cols-42);checks.winch=s.some(l=>l.includes('WINCH_'));
    // No hue to take away (NO_COLOR, eight colors): the native's own colors stand, the frame is bright.
    if(cfg.scenario==='plain'){fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));return;}
    frames.open=s;
    // Closed: the native as it is now, in its own colors, and the panel's; nothing gray is left.
    await key('\x1b',300);s=await until(s=>!box(s),'closed');frames.closed=s;
    const after=looks();checks.gray_left=after.flat().filter(c=>c&&c.fg===VEIL).length;
    checks.poke_after=await lookAt('POKE');
    const turning=new Set();for(let i=0;i<14;i++){turning.add(frameLooks());await sleep(150);}checks.moving_after=turning.size;
    // While it stands the window changes: narrower, folded by itself, open again; the panel under the
    // native (opened by hand where it cannot stand beside) and back beside it.
    await key('\x1d',250);await until(s=>!!box(s),'the popup again');
    const sizes={};
    for(const [c,r,name] of [[120,30,'side'],[90,24,'strip'],[160,40,'open']]){await resize(c,r);s=await until(s=>!!box(s),'the popup at '+name);
      sizes[name]=(await bright()).length;frames['veil_'+name]=s;}
    await key('\x1b',300);await resize(80,24);await until(has('◑답 대기'),'the strip at 80');
    await key('\x1d',250);await key('p',600);s=await until(s=>s.some(l=>l.includes('QUESTIONS ─')),'the panel under the native');
    await key('\x1d',300);s=await until(s=>!!box(s),'the popup over the panel under the native');sizes.bottom=(await bright()).length;frames.veil_bottom=s;
    await resize(160,40);s=await until(s=>!!box(s),'the popup beside again');sizes.beside=(await bright()).length;
    checks.sizes=sizes;await key('\x1b',300);await until(s=>!box(s),'closed after the sizes');
    // The cost: frames (one synchronized paint each) and bytes while the native floods, closed and open.
    const flood=async open=>{await key('flood\r',100);if(open){await key('\x1d',150);await until(s=>!!box(s),'the popup for the flood');}
      await until(s=>s.some(l=>/FLOOD_\d{4}/.test(l)),'the flood',4000);const from=raw.length,began=Date.now();await sleep(1200);
      const part=raw.slice(from),took=(Date.now()-began)/1000;
      const out={seconds:took,frames:part.split('\x1b[?2026h').length-1,bytes:Buffer.byteLength(part),bright:open?(await bright()).length:0};
      await until(has('FLOOD_0749'),'the flood ending',4000);if(open){await key('\x1b',300);await until(s=>!box(s),'closed after the flood');}return out;};
    checks.flood_closed=await flood(false);checks.flood_open=await flood(true);
    fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));return;
  }
  if(cfg.scenario==='empty'){
    // No question waits: Ctrl+] opens a popup all the same, whose keys name `p` (the panel's fold):
    // a folded panel opens by the keyboard alone; `r` is the messages' popup.
    await until(has('READY_'+cfg.harness),'the native');
    await resize(90,24);await sleep(500);frames.folded=await screen();
    await key('\x1d',500);frames.popup=await screen();
    await key('p',600);frames.opened=await screen();
    await key('\x1d',400);await key('r',600);frames.read=await screen();
    await key('\x1b',300);frames.closed=await screen();
    fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));return;
  }
  await until(s=>has('READY_'+cfg.harness)(s)&&has('2 QUESTIONS')(s),'the panel with its questions');
  frames.closed=await screen();
  // Native output while the popup stands: the twelve lines arrive under it.
  await key('later\r',100);await key('\x1d',100);
  let s=await until(s=>!!box(s),'the popup by Ctrl+]');
  checks.synchronized=synchronized();
  const before=await region();
  s=await until(has('LATER_11'),'the native output under the popup');
  frames.output_open=s;checks.region_kept=JSON.stringify(before)===JSON.stringify(await region());
  checks.title=s[box(s).top];
  await key('\x1b',200);
  s=await until(s=>!box(s),'the popup closing on Esc');
  frames.output_closed=s;checks.later_after_close=s.filter(l=>/LATER_\d\d/.test(l)).length;
  // A press on the panel's QUESTIONS rows opens it; keys stay in it; Tab moves to 2/2.
  const label=await at('2 QUESTIONS ─');
  await press(label.x+2,label.y);
  s=await until(s=>!!box(s),'the popup by a press on QUESTIONS');
  frames.pressed=s;
  await key('abc');await key('\t');
  s=await until(s=>box(s)&&s[box(s).top].includes('2/2'),'Tab to 2/2');frames.tab=s;
  await key('\x1b[Z');s=await until(s=>box(s)&&s[box(s).top].includes('1/2'),'Shift+Tab back to 1/2');
  checks.leaked=(await screen()).some(l=>l.startsWith('❯ ')&&l.includes('abc'));
  // An answer goes once, for that question; the popup closes, the panel says what was sent, and
  // the lone Enter after it never submits the native's own draft.
  await key('\x1b',200);await key('draft1',150);await key('\x1d',200);await until(s=>!!box(s),'the popup again');
  await key('2',100);await key('\r',400);
  s=await until(s=>!box(s)&&has('✓')(s),'the answer sent and the popup closed');
  frames.answered=s;
  await key('\r',400);checks.enter_swallowed=!(await screen()).some(l=>l.includes('DONE_draft1'));
  await key('more\r',300);s=await until(has('DONE_draft1more'),'the native draft submitted whole');
  // The other question: a text answer in Korean and a paste, kept when the popup closes.
  await key('draft2',150);
  await key('\x1d',200);s=await until(s=>!!box(s),'the popup for the text question');
  await key('한글 답',100);
  // An Alt+letter (ESC then a letter) is no key the editor knows: dropped, the popup stands, the
  // draft stays and the native receives nothing; so for ESC then a control byte (^C, Tab).
  await key('\x1bb',250);await key('\x1bf',250);await key('\x1b\x03',250);await key('\x1b\t',250);
  s=await screen();checks.alt_kept=!!box(s)&&has('› 한글 답')(s);
  checks.alt_leaked=s.some(l=>/^❯ draft2\S/.test(l)||/^❯ (?!draft2)\S/.test(l))||s.some(l=>l.includes('NATIVE_ESC_'));
  // ESC then Enter (Shift+Enter, Alt+Enter): dropped like it; the popup stands, the draft stays, and the
  // Enter never submits the text standing in the native input.
  await key('\x1b\r',300);
  s=await screen();checks.escenter_kept=!!box(s)&&has('› 한글 답')(s);
  checks.escenter_leaked=s.some(l=>l.includes('DONE_draft2'))||!s.some(l=>l.includes('❯ draft2'));
  await key('\x1b[200~붙여넣기\x1b[201~',250);
  s=await until(has('› 한글 답붙여넣기'),'the typed answer');frames.typing=s;
  await key('\x1b',200);await until(s=>!box(s),'closed');
  await key('\x1d',250);s=await until(has('› 한글 답붙여넣기'),'the draft kept');frames.draft_kept=s;
  // The Esc that closes it, pressed again 0.2 s later, is the shell's; a letter after it, or an Esc
  // after 1.5 s, is the native's.
  await key('\x1b',200);await until(s=>!box(s),'closed again');
  await key('\x1b',200);s=await screen();checks.esc2_leaked=s.some(l=>/^❯ .*\^/.test(l));
  await key('\x1d',250);await until(s=>!!box(s),'the popup for the Esc and a letter');
  await key('\x1b',200);await key('x',200);s=await screen();checks.esc_x_kept=s.some(l=>l.includes('❯ draft2x'));
  // A key that starts with ESC, 0.2 s after the Esc that closed the popup, reaches the native whole: Alt+b is
  // `^b` in its input, and Alt+Enter (ESC CR) is two bytes, not a bare Enter submitting `draft2x^bz`.
  await key('\x1d',250);await until(s=>!!box(s),'the popup for Alt+b');await key('\x1b',200);await until(s=>!box(s),'closed for Alt+b');
  await key('\x1bb',250);s=await screen();checks.alt_after_close=s.some(l=>l.includes('❯ draft2x^b'));
  await key('z',150); // the host reads `ESC b` as an open key sequence until one more key ends it
  await key('\x1d',250);await until(s=>!!box(s),'the popup for Alt+Enter');await key('\x1b',200);await until(s=>!box(s),'closed for Alt+Enter');
  await key('\x1b\r',300);s=await screen();
  checks.escenter_after_close=s.some(l=>l.includes('DONE_draft2x^bz^'))&&!s.some(l=>/DONE_draft2x\^bz(?!\^)/.test(l));
  await key('\r',250);
  // The moving frame turns beside it (the popup covers the panel's edge at 120 columns).
  await key('\x1b',200);await key('busy\r',300);await resize(120,30);await key('\x1d',300);
  await until(s=>!!box(s),'the popup at 120 columns');
  const still=[];for(let i=0;i<6;i++){still.push(JSON.stringify(await region()));await sleep(200);}
  checks.motion_kept=new Set(still).size===1;frames.moving=await screen();
  await key('\x1b',200);await key('calm\r',300);
  // A resize while it stands: it stands again in the middle.
  await key('\x1d',250);await resize(140,36);s=await until(s=>!!box(s),'the popup after a resize');frames.resized=s;
  await key('\x1b',200);
  // The folded strip (the panel folds by itself at 90 columns): its row of what to do opens it,
  // above the strip, which stays.
  await resize(90,24);s=await until(has('◑답 대기'),'the folded strip');frames.strip=s;
  const todo=await at('◑답 대기');await press(todo.x+1,todo.y);
  s=await until(s=>!!box(s),'the popup from the strip');frames.strip_popup=s;
  checks.strip_kept=s.slice(-4).join('\n')===frames.strip.slice(-4).join('\n');
  // A press outside closes it and reaches nothing else.
  const native=raw.split('NATIVE_ESC_').length;
  await press(1,1);s=await until(s=>!box(s),'a press outside closing it');
  checks.outside_reached_native=(await screen()).some(l=>l.includes('NATIVE_ESC_'));
  // The drag that follows the press which closed it is the popup's too, up to the release.
  await key('\x1d',300);await until(s=>!!box(s),'the popup again for a drag');
  child.write('\x1b[<0;2;2M');await sleep(80);
  for(const x of [3,4,5])child.write(`\x1b[<32;${x};2M`);await sleep(80);
  child.write('\x1b[<0;5;2m');await sleep(250);
  s=await until(s=>!box(s),'the press closing it');
  checks.drag_reached_native=(await screen()).some(l=>l.includes('NATIVE_ESC_'));
  // A 60×16 window.
  await resize(60,16);await key('\x1d',300);s=await until(s=>!!box(s),'the popup at 60×16');frames.narrow=s;
  checks.narrow_width=Math.max(...s.map(l=>[...l].length));
  // An Esc 1.5 s after the Esc that closed it is the native's (last: it leaves an Esc in the native input).
  await key('\x1b',1500);await key('\x1b',250);s=await screen();checks.esc_late_native=s.some(l=>/^❯ .*\^/.test(l));
  fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""


class SessionShellPopupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hsp-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('shell_popup_isolation', ROOT / 'tools/run-tests.py')
        runner = importlib.util.module_from_spec(spec); sys.modules[spec.name] = runner; spec.loader.exec_module(runner)
        cls.env = runner.build_isolated_env(cls.root, repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR'] = str(cls.root / 'native-claude')
        cls.env['FLEET_NOW_LANG'] = 'ko'  # the words the design drew (11-15 design F)
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
        cls.fake = cls.root / 'native.py'; cls.fake.write_text(FAKE_NATIVE)
        cls.launcher = cls.root / 'launcher.cjs'; cls.launcher.write_text(LAUNCHER)
        cls.driver = cls.root / 'driver.cjs'; cls.driver.write_text(DRIVER)

    def popup(self, harness, scenario=None, term=None, no_color=False):
        case = self.root / (harness + (scenario or '') + (term or '') + ('-nc' if no_color else '')); case.mkdir(mode=0o700)
        project = case / 'project'; project.mkdir()
        sid = {'claude': '00000000-0000-4000-8000-0000000000c1', 'codex': '00000000-0000-4000-8000-0000000000c2',
               'opencode': 'ses_popupfixture'}[harness]
        cache = Path(self.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True); cache.chmod(0o700)
        key = harness + '\0' + sid
        target = cache / 'projection.json'
        value = json.loads(target.read_text()) if target.exists() else {
            'schema_version': 3, 'items': [], 'by_session': {}, 'total': 0, 'older': 0, 'complete': True,
            'route_panels': {}, 'route_status': {}, 'route_sessions': {}}
        value.update(observed_at=time.time())
        value['route_panels'][key] = []; value['route_status'][key] = ''; value['route_sessions'][key] = time.time() + 3600
        target.write_text(json.dumps(value)); target.chmod(0o600)
        if scenario in ('veil', 'plain'):
            # A session title, so the head rule stands (the default `rule motion` moves a light over it).
            sys.path.insert(0, str(ROOT / 'tools'))
            from fleet import titles
            with mock.patch.dict(os.environ, self.env, clear=True):
                title = Path(titles.sidecar_path(sid, harness))
            title.parent.mkdir(parents=True, exist_ok=True)
            title.write_text(json.dumps({'title': 'VEIL_TITLE', 'title_ts': time.time(), 'ts': time.time(), 'source': 'test'}))
        questions = case / 'questions.json'; answers = case / 'answers.jsonl'
        questions.write_text(json.dumps([] if scenario == 'empty' else [
            {'id': 'popup-choice', 'revision': 'a' * 64, 'answerable': True, 'questions': [
                {'id': 'shape', 'question': '패널을 접었을 때의 모양을 어느 안으로 구현할까요?\n아래 두 줄(다)은 원래 화면이 폭을 다 '
                 '돌려받고, 답할 질문의 첫 줄과 실패한 route가 글로 남습니다.',
                 'choices': ['아래 두 줄 (다)', '넓힌 옆 띠 (가)', '아래 한 줄 (나)', '지금 그대로'], 'recommended': 0}]},
            {'id': 'popup-text', 'revision': 'b' * 64, 'answerable': True, 'questions': [
                {'id': 'color', 'question': 'MESSAGES 구분선의 색을 지금 회색 그대로 둘까요, 한 단계 밝게 할까요? 글로 적어 주세요.'}]}],
            ensure_ascii=False))
        backend = case / 'backend'
        backend.write_text(BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'),
                                          questions=str(questions), answers=str(answers)))
        backend.chmod(0o700)
        config = {'harness': harness, 'sid': sid, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'scenario': scenario, 'term': term, 'no_color': no_color,
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'),
                  'project': str(project), 'result': str(case / 'result.json')}
        (case / 'config.json').write_text(json.dumps(config))
        env = dict(self.env, LOOP_CONFIG=str(case / 'config.json'))
        child = subprocess.Popen([self.node, str(self.driver)], env=env, cwd=project, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _out, err = child.communicate(timeout=120)
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
        result = self.popup(harness); frames, checks = result['frames'], result['checks']
        # Closed: the panel's questions section is its label row and the summary, no editor box.
        panel = '\n'.join(frames['closed'])
        self.assertIn('2 QUESTIONS ─', panel)
        self.assertIn('◑ 패널을 접었을 때의 모양을 어느 안…', panel)
        # 11-15 (panel polish 1): the question to answer here is one button: its choices are chips
        # (` 1 label `, `+N` for the rest) and `↵ 답하기 · Ctrl+]` stands under them at the right.
        self.assertIn('│   1 아래 두 줄 (다) (추천)  +3 ', panel)
        self.assertIn('↵ 답하기 · Ctrl+] │', panel)
        self.assertNotIn('╭─ ◑', panel)
        # Drawn in one synchronized frame; the native's output under it leaves it whole; closed,
        # the native as it is now shows where it stood.
        self.assertTrue(checks['synchronized'])
        self.assertTrue(checks['region_kept'])
        self.assertIn('› ◑ 2 QUESTIONS · 1/2 · 답 필요', checks['title'])
        self.assertEqual(checks['later_after_close'], 12, '\n'.join(frames['output_closed']))
        self.assertFalse(checks['leaked'])
        # Exactly one answer, for that question and with the chosen value.
        self.assertEqual(result['answers'], [{'question_id': 'popup-choice', 'revision': 'a' * 64,
                                              'answers': {'shape': ['넓힌 옆 띠 (가)']}}])
        self.assertIn('✓ 답 보냄 · 넓힌 옆 띠 (가)', '\n'.join(frames['answered']))
        self.assertTrue(checks['enter_swallowed'])
        self.assertIn('› 한글 답붙여넣기', '\n'.join(frames['draft_kept']))
        self.assertTrue(checks['motion_kept'])
        self.assertTrue(checks['strip_kept'], '\n'.join(frames['strip_popup']))
        self.assertFalse(checks['outside_reached_native'], '\n'.join(frames['strip_popup']))
        # The drag and release after the press that closed it never reach a native that reads the mouse.
        self.assertFalse(checks['drag_reached_native'])
        # An Alt+letter in a text answer: the popup stands, the draft stays, the native gets nothing.
        self.assertTrue(checks['alt_kept'])
        self.assertFalse(checks['alt_leaked'])
        # ESC then Enter: dropped, the popup stands, the native input's text is not submitted.
        self.assertTrue(checks['escenter_kept'])
        self.assertFalse(checks['escenter_leaked'])
        # The second Esc after the Esc that closed it is the shell's; a letter, or an Esc after 1.5 s, is the native's.
        self.assertFalse(checks['esc2_leaked'])
        self.assertTrue(checks['esc_x_kept'])
        # A key that starts with ESC right after an Esc close reaches the native whole (Alt+b, Alt+Enter).
        self.assertTrue(checks['alt_after_close'])
        self.assertTrue(checks['escenter_after_close'])
        self.assertTrue(checks['esc_late_native'])
        self.assertLessEqual(checks['narrow_width'], 60)
        narrow = frames['narrow']
        top = next(i for i, line in enumerate(narrow) if '✕ ─╮' in line)
        self.assertEqual(narrow[top].index('╭'), 2, '\n'.join(narrow))

    def no_question(self, harness):
        frames = self.popup(harness, 'empty')['frames']
        # Folded at 90 columns, with no question waiting Ctrl+] opens a popup whose keys name `p`; `p`
        # opens the panel (the keyboard's way to unfold it, 11-15) and closes the popup; `r` is the
        # messages' popup; the panel stays open after it.
        folded, popup, opened = ('\n'.join(frames[name]) for name in ('folded', 'popup', 'opened'))
        self.assertNotIn('REPO', folded)
        self.assertIn('p 패널 펼치기', popup)
        self.assertNotIn('REPO', popup)
        self.assertIn('REPO', opened)
        self.assertNotIn('╭─ ', opened)
        read = '\n'.join(frames['read'])
        self.assertIn('✉', read)
        self.assertIn('MESSAGES', read)
        self.assertIn('REPO', '\n'.join(frames['closed']))

    def veil(self, harness):
        # 11-15 (panel polish 3): while a popup stands, everything outside it (the native, the panel,
        # the strip) reads in one dark gray (256-color 239) without any other color, weight or ground;
        # what moves stands still meanwhile (under the default `rule motion`, the head rule's light). Output of every kind under it (lines one at a
        # time, a scroll, the alternate screen in and out, a poked inverse cell) never shows a bright
        # piece; nothing tells the native (no size change, no input). Closed, the screen as it is
        # now in its own colors, nothing gray left and the frame turning again. The window narrower,
        # folded by itself, open again, the panel under the native and back beside: veiled each time.
        # The cost: no more frames than without it while the native floods.
        result = self.popup(harness, 'veil'); frames, checks = result['frames'], result['checks']
        red, blue = checks['red_before'], checks['blue_before']
        self.assertTrue(red['bold'] and red['fg'] != 239 and blue['bg'] != -1, checks)
        self.assertGreater(checks['moving_before'], 1, checks)
        self.assertEqual(checks['bright_open'], 0, checks['bright_sample'])
        self.assertEqual(checks['leaks'], 0, checks)
        self.assertEqual(checks['seen'], ['ALT_SCREEN', 'DRIP_07', 'POKE', 'SCROLL_29'], checks)
        self.assertEqual(checks['still'], 1, checks)
        self.assertFalse(checks['winch'], checks)
        self.assertEqual(checks['native_input'].strip(), '❯', checks)
        self.assertEqual(checks['gray_left'], 0, '\n'.join(frames['closed']))
        poke = checks['poke_after']
        self.assertTrue(poke and poke['inverse'] and poke['fg'] == 2, checks)
        self.assertGreater(checks['moving_after'], 1, checks)
        self.assertEqual(checks['sizes'], {'side': 0, 'strip': 0, 'open': 0, 'bottom': 0, 'beside': 0},
                         '\n'.join(frames['veil_bottom']))
        closed, opened = checks['flood_closed'], checks['flood_open']
        self.assertEqual(opened['bright'], 0, checks)
        # One frame per 8 ms at most (the host's frame timer), and no more than it paints without the veil.
        for flood in (closed, opened):
            self.assertLessEqual(flood['frames'], flood['seconds'] / 0.008 + 5, checks)
        self.assertLessEqual(opened['frames'], closed['frames'] * 1.25 + 10, checks)
        print('VEIL ' + harness + ' ' + json.dumps({'flood_closed': closed, 'flood_open': opened}), flush=True)

    def plain(self, **paint):
        # Mono-like terminals (NO_COLOR, eight colors): nothing is veiled; the native keeps its own
        # colors while the popup stands, and the popup's frame is bright (not dim) instead.
        checks = self.popup('claude', 'plain', **paint)['checks']
        self.assertTrue(checks['red_open']['bold'], checks)
        self.assertFalse(checks['corner']['dim'], checks)
        self.assertGreater(checks['leaks'], 0, checks)

    def test_veil_claude(self):
        self.veil('claude')

    def test_veil_codex(self):
        self.veil('codex')

    def test_veil_opencode(self):
        self.veil('opencode')

    def test_no_veil_without_color(self):
        self.plain(no_color=True)

    def test_no_veil_in_eight_colors(self):
        self.plain(term='xterm')

    def test_claude(self):
        self.check('claude')

    def test_no_question_opens_a_folded_panel(self):
        self.no_question('claude')

    def test_codex(self):
        self.check('codex')

    def test_opencode(self):
        self.check('opencode')


if __name__ == '__main__':
    if '--frames' in sys.argv:
        sys.argv.remove('--frames'); SHOW_FRAMES = True
    unittest.main()
