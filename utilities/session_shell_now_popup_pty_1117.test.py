"""11-17 (NOW popup): the NOW popup over the real host in a private PTY, for each harness's synthetic native.

A press on the panel's NOW row opens the popup (at once, with the NOW the panel holds on its own row until the list
arrives); the list is oldest first and stands on the current NOW; ↑↓ move the row stood on (a long list folds and
the prompt stood on unfolds); Enter on a prompt or NOW row reads that part of the conversation in the popup and
Enter again is back on that row; Enter on a route, question or message row opens that section's popup; ←→ move
between the sections; an unreadable part is said in words; an empty list is one line; 80 and 120 columns and the
folded strip. Nothing reaches the native: its draft stands, no press or key is sent to it. The timeline and the
conversation are fixtures (the backend's two reads are replaced); no real session, no model, no account.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import signal
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHOW_FRAMES = False
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools"), str(ROOT / "tools/fleet/tests")]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TABLE = load("now_popup_table_pty", ROOT / "utilities/session_shell_table_popup_pty.test.py")
NATIVE = load("now_popup_native", ROOT / "utilities/session_shell_popup.test.py")
SYNTH = load("now_popup_synth_pty", ROOT / "tools/fleet/tests/test_now_popup.py")

# The backend as installed, with the timeline layer's two reads replaced by fixtures (the list from `story`, the
# conversation from `story.passage`) and a file that holds the list's read back (`slow`).
BACKEND = r"""#!@PY@
import json,os,sys,time
sys.path.insert(0,@UTILITIES@);sys.path.insert(0,@TOOLS@)
import session_shell as m
from fleet.explorer import timeline as tl
STORY=@STORY@;SLOW=@SLOW@;SPAWNS=@SPAWNS@;HANG=@HANG@;LOGGED=[]
def rows(harness,sid,**kw):
    if not LOGGED:
        LOGGED.append(1);open(SPAWNS,'a').write('%d\n'%os.getpid())     # one line for each process that reads a list
    while os.path.exists(SLOW):time.sleep(0.05)
    if os.path.exists(HANG):
        os.unlink(HANG);time.sleep(60)      # this one read never answers (once)
    return json.load(open(STORY))
def passage(target,width):
    cfg=json.load(open(STORY+'.passage'))
    if not cfg.get('ok',True):return {'ok':False,'reason':cfg['reason'],'rows':[],'marks':[],'clipped':False,'span':None,'head':''}
    from fleet.explorer.transcript import dialogue_rows
    return {'ok':True,'reason':'','rows':dialogue_rows(tl._role_lines(cfg['text']),width,target['harness']),'marks':[],'clipped':False,'span':None,'head':''}
tl.rows=rows;tl.passage=passage
raise SystemExit(m._backend())
"""

DRIVER = r"""
const fs=require('node:fs');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let cols=cfg.cols,rows=cfg.rows,raw='';
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
const until=async(test,what,ms=20000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return s;await sleep(40);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>line.includes(word));
const press=async(x,y,ms=200)=>{child.write(`\x1b[<0;${x+1};${y+1}M\x1b[<0;${x+1};${y+1}m`);await sleep(ms);};
const key=async(bytes,ms=250)=>{child.write(bytes);await sleep(ms);};
const frames={},checks={};
const box=s=>{const top=s.findIndex(l=>l.includes('╭─')&&l.includes('✕ ─╮'));if(top<0)return null;
  const left=[...s[top]].indexOf('╭'),bottom=s.findIndex((l,i)=>i>top&&[...l][left]==='╰'&&l.includes('╯'));
  return {top,bottom,title:s[top],foot:s[bottom]||'',rows:s.slice(top,bottom+1),left};};
// The frame's width in cells (a wide character takes two; the screen's text has one char for it).
// Every row of the standing popup starts and ends on its frame (nothing ran over, nothing wrapped).
const aligned=async()=>{await flush();const b=box(await screen());if(!b)return false;const top=cells(b.top),l=top.indexOf('╭'),r=top.indexOf('╮');
  for(let y=b.top;y<=b.bottom;y++){const c=cells(y),a=c[l],z=c[r];if(!('╭│╰'.includes(a)&&'╮│╯'.includes(z)))return false;}return r>l;};
const frameWidth=async()=>{await flush();const b=box(await screen());if(!b)return 0;const c=cells(b.top);return c.indexOf('╮')-c.indexOf('╭')+1;};
const popupHas=word=>s=>{const b=box(s);return !!b&&b.rows.some(l=>l.includes(word));};
const titled=word=>s=>{const b=box(s);return !!b&&b.title.includes(word);};
// The row stood on: the popup's cells on the chosen ground (the dark face's, `cfg.chosen`), or reversed.
const standing=async()=>{await flush();const s=await screen(),b=box(s);if(!b)return null;
  for(let y=b.top+1;y<b.bottom;y++){const line=term.buffer.active.getLine(y);let hit=false;
    for(let x=0;x<cols;x++){const c=line?.getCell(x);if(c&&c.getChars().trim()&&(c.isBgPalette()&&c.getBgColor()===cfg.chosen||cfg.reversed&&c.isInverse())){hit=true;break;}}
    if(hit)return s[y].replace(/^.*?│ /,'').replace(/ │.*$/,'').trim();}
  return '';};
const draft=async()=>{const t=(await screen()).find(l=>l.startsWith('❯ '));return t?t.slice(2).split('│')[0].trimEnd():null;};
const NOWWORD=cfg.now;
const openNow=async()=>{const p=await at(NOWWORD);if(!p)throw Error('no NOW row\n'+(await screen()).join('\n'));await press(p.x+1,p.y,300);
  return until(titled('NOW'),'the NOW popup');};
// Where the panel does not show its NOW row (a small window, the folded strip): Ctrl+] and ← reach it, as every popup is reached.
const openAny=async()=>{if(await at(NOWWORD))return openNow();
  await key('\x1d',500);await until(s=>!!box(s),'a popup by Ctrl+]');
  await toSection('NOW',600);
  return until(titled('NOW'),'NOW by a press on its name');};
const close=async()=>{await key('\x1b',300);await until(s=>!box(s),'closed');};
// Down until the row stood on has `word` (at most `max` presses).
const downTo=async(word,max=14)=>{for(let i=0;i<max;i++){const t=await standing();if(t&&t.includes(word))return t;await key('\x1b[B',300);}return (await standing())||'';};
// 11-22: the question popup's ←→ no longer change sections; a section is reached by a press on its name on the
// popup's bottom line.
const toSection=async(name,ms=500)=>{await flush();const s=await screen(),b=box(s);if(!b)throw Error('no popup to press in');
  const text=[],where=[];cells(b.bottom).forEach((ch,x)=>{for(const _ of ch){where.push(x);text.push(_);}});
  const i=text.join('').indexOf(name);if(i<0)throw Error('no '+name+' on the bottom line: '+b.foot);await press(where[i],b.bottom,ms);};
(async()=>{try{
  await until(s=>has('READY_'+cfg.harness)(s)&&has(NOWWORD)(s),'the panel with its NOW');
  await key('draft9',300);
  frames.panel=await screen();
  const p0=await at(NOWWORD);checks.panel_row=(await screen())[p0.y];checks.mark=checks.panel_row.includes('›');
  if(cfg.scenario==='keys'){
    // 11-17 review, the folded list's ↑↓: one backend answers while the popup stands, nothing is left behind, and a held
    // key steps over every row.
    const out={};
    const spawned=()=>fs.existsSync(cfg.spawns)?fs.readFileSync(cfg.spawns,'utf8').split('\n').filter(Boolean).map(Number):[];
    // The backends that read a list and are still running (the host's own projection backend is another matter).
    const isAlive=pid=>{try{process.kill(pid,0);return true;}catch{return false;}};
    const live=()=>spawned().filter(isAlive).length;
    const boxText=async()=>{await flush();const b=box(await screen());return b?b.rows.join('\n'):'';};
    out.base=live();
    await openNow();await until(popupHas('지금'),'the list');await sleep(300);
    out.open={spawned:spawned().length,live:live()};
    // A press at a time: the popup is drawn again with the list read for the new row, from the one backend.
    const first=spawned().length,lat=[];
    for(let i=0;i<17;i++){
      const t0=Date.now();let prev=await boxText(),last=t0,states=1;child.write('\x1b[A');
      while(Date.now()-t0<600){await sleep(5);const now=await boxText();if(now!==prev){prev=now;last=Date.now();states++;}}
      lat.push([last-t0,states]);
    }
    out.single={spawned:spawned().length-first,lat,live:live()};
    // A key held (30 a second): every row is stepped on, none is stepped over, and reads never run side by side.
    await close();await openNow();await until(popupHas('지금'),'the list again');await sleep(300);
    out.held_start=await standing();let most=live();
    for(let i=0;i<cfg.held;i++){child.write('\x1b[A');await sleep(33);if(i%6===5)most=Math.max(most,live());}
    await sleep(2500);out.held={final:await standing(),most,live:live()};
    // Every key was the popup's: the draft stands, nothing was sent.
    out.draft_now=await draft();
    // A backend that dies under the popup: the next key still moves the row, and nothing is left behind.
    let alive=spawned().filter(isAlive);
    out.alive_before_kill=alive.length;
    if(alive.length)try{process.kill(alive.at(-1),'SIGKILL');}catch{}
    await sleep(300);
    let before=await standing();child.write('\x1b[A');await sleep(1500);
    out.killed={moved:(await standing())!==before,live:live()};
    // A backend that does not answer: the read is made the one-shot way, the silent one is ended, nothing is left.
    fs.writeFileSync(cfg.hang,'');before=await standing();child.write('\x1b[A');await sleep(6500);
    out.hung={moved:(await standing())!==before,live:live(),hang_left:fs.existsSync(cfg.hang)};
    before=await standing();child.write('\x1b[A');await sleep(1200);
    out.after_hung={moved:(await standing())!==before,live:live()};
    // Closing it, another section, the section again, closing: none leaves a backend running.
    await close();await sleep(500);out.closed=live();
    await openNow();await until(popupHas('지금'),'the list once more');await sleep(300);out.reopened=live();
    await key('\x1b[C',600);await until(titled('QUESTIONS'),'QUESTIONS');await sleep(400);out.next_section=live();
    await toSection('NOW',600);await until(titled('NOW'),'NOW again');await sleep(500);out.back=live();
    await key('\x1d',500);await sleep(500);out.ctrl=live();
    const end=await screen();out.draft=end.some(l=>l.startsWith('❯ draft9'));out.submitted=end.some(l=>l.includes('DONE_draft9'));out.native_mouse=raw.includes('NATIVE_ESC_');
    fs.writeFileSync(cfg.result,JSON.stringify({frames,checks:out}));return;
  }
  if(cfg.layout==='bottom'){
    // The bottom layout: the NOW shares the title's row; a press on it opens the popup.
    let s=await openNow();frames.bottom=s;checks.bottom_title=box(s).title;await close();
    s=await screen();checks.draft_bottom=s.some(l=>l.startsWith('❯ draft9'));
    fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));return;
  }
  // 1. At once: the list's read is held back; the frame stands with the NOW the panel holds on its own row.
  fs.writeFileSync(cfg.slow,'');
  const began=Date.now();let p=await at(NOWWORD);await press(p.x+1,p.y,0);
  let s=await until(titled('NOW'),'the frame at once');checks.frame_ms=Date.now()-began;frames.placeholder=s;
  checks.placeholder=box(s).rows.some(l=>l.includes(NOWWORD))&&!box(s).rows.some(l=>l.includes('지금'));
  fs.unlinkSync(cfg.slow);
  s=await until(popupHas('지금'),'the list');frames.list=s;
  checks.title=box(s).title;checks.foot=box(s).foot;
  checks.keys=box(s).rows.filter(l=>l.includes('Enter')).join(' | ');
  checks.start=await standing();
  // 2. Folded (the long fixture does not fit): `+N 접음`; ↑ moves the row stood on, ↓ back; the prompt's NOWs unfold.
  checks.folded=box(s).rows.some(l=>/\+\d+ 접음/.test(l));
  const before=await standing();await key('\x1b[A',600);
  checks.up_moved=(await standing())!==before;
  for(let i=0;i<3;i++)await key('\x1b[A',500);
  frames.up=await screen();checks.up_after=await standing();
  // PgDn / PgUp move a page of rows.
  await key('\x1b[6~',700);checks.page_down=await standing();await key('\x1b[5~',700);checks.page_up=await standing();
  // 3. Enter on a NOW row reads that part here; Enter again is back on the same row. Nothing goes to the native.
  await close();await openNow();await until(popupHas('지금'),'the list again at the start');
  const nowRow=await standing();checks.on_now=nowRow;
  await key('\r',300);s=await until(popupHas('PASSAGE_BODY'),'the part read');frames.read=s;
  checks.read_title=box(s).title;checks.read_keys=box(s).rows.filter(l=>l.includes('Enter')).join(' | ');
  await key('\r',400);s=await until(popupHas('지금'),'the list again');frames.back=s;
  checks.back_on=await standing();checks.round_trip=checks.back_on===nowRow;
  // A prompt row reads too.
  for(let i=0;i<14;i++){const t=await standing();if(t&&t.includes('>'))break;await key('\x1b[A',400);}
  checks.on_prompt=await standing();
  await key('\r',300);s=await until(popupHas('PASSAGE_BODY'),'a prompt read');await key('\r',400);await until(s=>!!box(s)&&box(s).rows.some(l=>/\d\d:\d\d/.test(l)),'the list after a prompt');
  // 4. A part that cannot be read is one line in words.
  fs.writeFileSync(cfg.story+'.passage',JSON.stringify({ok:false,reason:'no-transcript'}));
  await key('\r',300);s=await until(popupHas('대화 기록을 찾지 못했습니다'),'the reason in words');frames.reason=s;
  await key('\r',400);await until(s=>!!box(s)&&box(s).rows.some(l=>/\d\d:\d\d/.test(l)),'back from the reason');
  fs.writeFileSync(cfg.story+'.passage',JSON.stringify({ok:true,text:cfg.passage}));
  // 5. An event row's Enter opens that section's popup.
  checks.sections={};
  for(const [word,title] of [['route','ROUTE'],['질문','QUESTIONS'],['같이 봐','MESSAGES']]){
    await close();await openNow();
    let found=await downTo(word,16);
    if(!found.includes(word)){for(let i=0;i<30&&!(await standing()||'').includes(word);i++)await key('\x1b[A',250);found=await standing();}
    await key('\r',700);s=await until(s=>!!box(s)&&box(s).title.includes(title),'the '+title+' popup');
    checks.sections[word]={on:found,title:box(s).title};frames['to_'+word]=s;
  }
  await close();
  // 6. ←→ between the sections: NOW sits before QUESTIONS; Ctrl+] opens what it opened before (not NOW).
  await openNow();await key('\x1b[C',700);s=await until(titled('QUESTIONS'),'QUESTIONS by →');checks.next_foot=box(s).foot;
  await toSection('NOW',700);s=await until(titled('NOW'),'NOW by a press on its name');
  await close();
  await key('\x1d',500);s=await until(s=>!!box(s),'a popup by Ctrl+]');checks.ctrl_title=box(s).title;await close();
  // 7. Keys and presses stayed the popup's.
  s=await screen();checks.draft=s.some(l=>l.startsWith('❯ draft9'));checks.submitted=s.some(l=>l.includes('DONE_draft9'));
  checks.native_mouse=raw.includes('NATIVE_ESC_');
  // 8. An empty list (a new session): one line, Enter does nothing.
  fs.writeFileSync(cfg.story,'[]');
  await openNow();s=await until(popupHas('아직 남은 기록이 없습니다'),'the empty list');frames.empty=s;
  await key('\r',400);checks.empty_enter=popupHas('아직 남은 기록이 없습니다')(await screen());
  await close();
  fs.writeFileSync(cfg.story,fs.readFileSync(cfg.story+'.full','utf8'));
  // 9. 80 and 120 columns: the popup's inside is 72 and 112 cells; a long NOW is cut with `…` at 80.
  for(const [c,r] of [[80,36],[120,36]]){
    cols=c;rows=r;term.resize(c,r);child.resize(c,r);await sleep(700);
    await sleep(300);
    s=await openAny();s=await until(popupHas('지금'),'the list at '+c);frames['w'+c]=s;
    checks['inner'+c]=(await frameWidth())-4;checks['cut'+c]=box(s).rows.some(l=>l.includes('…'));
    checks['aligned'+c]=await aligned();
    await close();
  }
  // 10. The folded strip (90×24): Ctrl+] then ← reaches NOW.
  cols=90;rows=24;term.resize(90,24);child.resize(90,24);await sleep(700);
  s=await until(s=>s.at(-1).includes('▴'),'the folded strip');
  s=await openAny();checks.strip_now=!!box(s)&&box(s).title.includes('NOW');frames.strip=s;
  await close();
  // 11. 60×16 and 160×60: no overflow, still open.
  for(const [c,r] of [[60,16],[160,60]]){
    cols=c;rows=r;term.resize(c,r);child.resize(c,r);await sleep(700);
    await openAny();
    s=await until(titled('NOW'),'NOW at '+c+'x'+r);await sleep(800);s=await screen();
    checks['size'+c]={aligned:await aligned(),stands:!!box(s),height:s.length};
    await close();
  }
  s=await screen();checks.draft_end=s.some(l=>l.startsWith('❯ draft9'));checks.native_mouse_end=raw.includes('NATIVE_ESC_');
  fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""

NOW_TEXT = "그리는 중이다"


def story(harness, sid):
    """A long session (it must fold in a 36-row window), the last NOW current, then a route, a question and a message."""
    rows = SYNTH.synth(prompts=9, nows=3, harness=harness, sid=sid, events=False)
    for row in rows:
        if row["kind"] == "now":
            row["current"] = False
    last = [row for row in rows if row["kind"] == "now"][-1]
    last["current"] = True
    # The 120-column window shows it whole; the 80-column one cuts it (its time and `지금` stay).
    last["text"] = NOW_TEXT + " " + "긴 요약 " * 9
    at = last["at"]
    rows += [
        {"key": "route:rt-x:start", "at": at + 20, "kind": "route", "sub": "start", "route_id": "rt-x", "text": "시험 route",
         "capability": "code", "intensity": "quick", "state": "open", "harness": harness, "sid": sid, "generation": 0,
         "open": {"kind": "popup", "popup": "routes", "id": "rt-x"}},
        {"key": "question:q9:asked", "at": at + 30, "kind": "question", "sub": "asked", "id": "q9", "source": "S4",
         "text": "어느 쪽으로 할까요?", "state": "", "harness": harness, "sid": sid, "generation": 0,
         "open": {"kind": "popup", "popup": "questions", "id": "q9"}},
        {"key": "message:m9:received", "at": at + 40, "kind": "message", "sub": "received", "who": "[e4]",
         "message_kind": "steer", "delivery": None, "id": "m9", "text": "같이 봐 주세요", "harness": harness, "sid": sid,
         "generation": 0, "open": {"kind": "popup", "popup": "messages", "id": "m9"}}]
    return rows


class NowPopupPtyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TABLE.SessionShellTablePopupTest.setUpClass.__func__(cls)
        cls.fake.write_text(NATIVE.FAKE_NATIVE)
        cls.driver.write_text(DRIVER)

    def run_shell(self, harness, layout="side", cols=160, rows=40, scenario=''):
        case = self.root / (harness + layout + scenario); case.mkdir(mode=0o700)
        project = case / 'project'; project.mkdir()
        sid = {'claude': '00000000-0000-4000-8000-0000000000d1', 'codex': '00000000-0000-4000-8000-0000000000d2',
               'opencode': 'ses_nowpopupfixture'}[harness]
        policy = Path(self.env.get('XDG_CONFIG_HOME') or Path(self.env['HOME']) / '.config') / 'hearting/session-shell.json'
        policy.write_text(json.dumps({'enabled': True, 'layout': layout, 'side_width': 40, 'side_min_columns': 132, 'bottom_rows': 9}))
        policy.chmod(0o600)
        # The panel's NOW: a fresh summary in this session's title record (the isolated state).
        with mock.patch.dict(os.environ, self.env, clear=True):
            from fleet import titles
            titles.write(sid, '이 창 제목', summary=NOW_TEXT, harness=harness, source='refresher')
        full = case / 'story.json'
        full.write_text(json.dumps(story(harness, sid), ensure_ascii=False))
        (case / 'story.json.full').write_text(full.read_text())
        passage = "[user] 이 대목의 프롬프트\n[assistant] PASSAGE_BODY 이 대목에서 한 일을 적은 답이다\n"
        (case / 'story.json.passage').write_text(json.dumps({'ok': True, 'text': passage}, ensure_ascii=False))
        backend = case / 'backend'
        backend.write_text(BACKEND.replace('@PY@', sys.executable).replace('@UTILITIES@', repr(str(ROOT / 'utilities')))
                           .replace('@TOOLS@', repr(str(ROOT / 'tools'))).replace('@STORY@', repr(str(full)))
                           .replace('@SLOW@', repr(str(case / 'slow'))).replace('@SPAWNS@', repr(str(case / 'spawns')))
                           .replace('@HANG@', repr(str(case / 'hang'))))
        backend.chmod(0o700)
        config = {'harness': harness, 'sid': sid, 'python': sys.executable, 'fake': str(self.fake), 'layout': layout,
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'project': str(project),
                  'result': str(case / 'result.json'), 'story': str(full), 'slow': str(case / 'slow'), 'now': NOW_TEXT,
                  'passage': passage, 'cols': cols, 'rows': rows, 'reversed': False, 'scenario': scenario, 'held': 24,
                  'spawns': str(case / 'spawns'), 'hang': str(case / 'hang'),
                  # the dark face's chosen ground, as panel-view has it (the popup's colors are not this test's to fix)
                  'chosen': int(subprocess.run([self.node, '-p', "require(process.argv[1]).FACES.dark.chosen", str(ROOT / 'utilities/session-shell/panel-view.cjs')], capture_output=True, text=True, check=True).stdout)}
        (case / 'config.json').write_text(json.dumps(config))
        env = dict(self.env, LOOP_CONFIG=str(case / 'config.json'))
        child = subprocess.Popen([self.node, str(self.driver)], env=env, cwd=project, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _out, err = child.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM); child.communicate(timeout=10); raise
        diagnostic = (case / 'display-errors').read_text() if (case / 'display-errors').exists() else ''
        self.assertEqual(child.returncode, 0, err + diagnostic)
        result = json.loads((case / 'result.json').read_text())
        if SHOW_FRAMES:  # `--frames`: the frames this run read, for a person to look at
            print('FRAMES ' + harness + ' ' + json.dumps(result, ensure_ascii=False), flush=True)
        return result

    def check(self, harness):
        result = self.run_shell(harness)
        frames, checks = result['frames'], result['checks']
        text = lambda name: '\n'.join(frames[name])  # noqa: E731
        # The opener: the panel's NOW row ends in `›`.
        self.assertTrue(checks['mark'], checks['panel_row'])
        self.assertIn('›', checks['panel_row'])
        # At once and not empty: the frame stood while the list was held back, with the panel's own NOW on a row.
        self.assertTrue(checks['placeholder'], text('placeholder'))
        # The list: the title counts, the sections line names NOW before QUESTIONS, the keys line is the NOW's.
        self.assertIn('NOW · ', checks['title'])
        self.assertRegex(checks['foot'], r'NOW · QUESTIONS')
        self.assertIn('↑↓ · Enter 그 대목 읽기', checks['keys'])
        self.assertIn(NOW_TEXT, checks['start'])
        self.assertTrue(checks['start'].endswith('지금'), checks['start'])
        self.assertTrue(checks['folded'], text('list'))
        # ↑ moved the row stood on, PgUp/PgDn a page of rows.
        self.assertTrue(checks['up_moved'] and checks['up_after'] != checks['start'], checks)
        self.assertNotEqual(checks['page_down'], checks['page_up'])
        # Enter: that part read in the popup (its own keys line), Enter again on the same row.
        self.assertIn('PASSAGE_BODY', text('read'))
        self.assertIn('Enter 목록으로 돌아가기', checks['read_keys'])
        self.assertTrue(checks['round_trip'], (checks['on_now'], checks['back_on']))
        self.assertIn('>', checks['on_prompt'])
        self.assertIn('대화 기록을 찾지 못했습니다', text('reason'))
        # An event row opens its section's popup.
        for word, title in (('route', 'ROUTE'), ('질문', 'QUESTIONS'), ('같이 봐', 'MESSAGES')):
            self.assertIn(word, checks['sections'][word]['on'], checks['sections'])
            self.assertIn(title, checks['sections'][word]['title'])
        # ←→ keep working; Ctrl+] opens what it always opened.
        self.assertRegex(checks['next_foot'], r'NOW · QUESTIONS')
        self.assertNotIn('NOW', checks['ctrl_title'])
        # An empty list is one line and Enter does nothing there.
        self.assertTrue(checks['empty_enter'])
        # 80 and 120 columns: the popup's inside is 72 and 112 cells.
        self.assertEqual((checks['inner80'], checks['inner120']), (72, 112))
        self.assertTrue(checks['cut80'], text('w80'))
        self.assertFalse(checks['cut120'], text('w120'))
        self.assertTrue(checks['aligned80'] and checks['aligned120'])
        self.assertTrue(checks['strip_now'], text('strip'))
        for key, (c, r) in (('size60', (60, 16)), ('size160', (160, 60))):
            self.assertTrue(checks[key]['stands'], key)
            self.assertTrue(checks[key]['aligned'], key)
        # Nothing reached the native.
        self.assertTrue(checks['draft'] and checks['draft_end'])
        self.assertFalse(checks['submitted'] or checks['native_mouse'] or checks['native_mouse_end'])
        return result

    def test_claude(self):
        self.check('claude')

    def test_codex(self):
        self.check('codex')

    def test_opencode(self):
        self.check('opencode')

    def leftovers(self):
        """Processes of this test's backend wrappers still alive (any case), by their command line."""
        out = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    cmd = (entry / 'cmdline').read_bytes().decode(errors='replace')
                except OSError:
                    continue
                if str(self.root) in cmd and '/backend' in cmd and str(os.getpid()) != entry.name:
                    out.append((entry.name, cmd[:120]))
        return out

    def test_the_folded_lists_keys_ask_one_backend_that_ends_with_the_popup(self):
        """11-17 review: ↑↓ in a folded list start no process of their own and draw in 150 ms; a held key steps on every
        row; the backend ends with the popup (closed, another section, the window ended) and when it dies or is silent."""
        result = self.run_shell('claude', scenario='keys')
        c = result['checks']
        # One backend read the list on opening and none was started for the keys that followed (17 single, 24 held).
        self.assertEqual(c['open']['spawned'], 1, c['open'])
        self.assertEqual(c['single']['spawned'], 0, c['single'])
        self.assertEqual(c['open']['live'], c['base'] + 1, c['open'])
        self.assertEqual(c['single']['live'], c['base'] + 1, c['single'])
        # The key to the list drawn for the new row: milliseconds (150 or less), measured on the presses that change the rows
        # (those that step into another prompt's group: it opens and the one left folds).
        crossed = sorted(ms for ms, states in c['single']['lat'] if states >= 2)
        self.assertGreaterEqual(len(crossed), 3, c['single'])
        median = crossed[len(crossed) // 2]
        print('NOW-POPUP-LATENCY key->drawn ms (presses that read the list again):', crossed, 'median', median, flush=True)
        self.assertLessEqual(median, 150, crossed)
        # A held key: the row stood on is the 24th selectable row above where it began, none stepped over; one read at a time.
        selectable = [row for row in story('claude', 'x') if row['kind'] in ('prompt', 'now', 'route', 'question', 'message')]
        at = next(i for i, row in enumerate(selectable) if row['kind'] == 'now' and row['text'].split()[0] in c['held_start'] and row.get('current'))
        self.assertIn(selectable[at - 24]['text'], c['held']['final'], (c['held'], selectable[at - 24]['text']))
        self.assertLessEqual(c['held']['most'], c['base'] + 1, c['held'])
        self.assertEqual(c['draft_now'], 'draft9')
        # A backend killed under the popup: the next key still moves the row.
        self.assertEqual(c['alive_before_kill'], 1, c)
        self.assertTrue(c['killed']['moved'], c['killed'])
        self.assertLessEqual(c['killed']['live'], c['base'] + 1)
        # A silent backend: ended after its wait, the read made the one-shot way, nothing left, and the popup reads that way on.
        self.assertTrue(c['hung']['moved'] and not c['hung']['hang_left'], c['hung'])
        self.assertEqual(c['hung']['live'], c['base'], c['hung'])
        self.assertTrue(c['after_hung']['moved'] and c['after_hung']['live'] == c['base'], c['after_hung'])
        # Closing, another section and Ctrl+] leave no backend; opening it again starts one.
        b = c['base']
        self.assertEqual((c['closed'], c['reopened'], c['next_section'], c['back'], c['ctrl']), (b, b + 1, b, b + 1, b), c)
        # Every key was the popup's: nothing reached the native.
        self.assertTrue(c['draft'] and not c['submitted'] and not c['native_mouse'])
        self.assertEqual(self.leftovers(), [], 'a backend outlived the window')


    def test_bottom_layout_opens_from_the_row_it_shares_with_the_title(self):
        result = self.run_shell('claude', layout='bottom', cols=120, rows=40)
        checks = result['checks']
        self.assertTrue(checks['mark'], checks['panel_row'])
        self.assertIn('NOW', checks['bottom_title'])
        self.assertTrue(checks['draft_bottom'])


if __name__ == '__main__':
    if '--frames' in sys.argv:
        sys.argv.remove('--frames'); SHOW_FRAMES = True
    unittest.main()
