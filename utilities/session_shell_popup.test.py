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
# mouse: every escape sequence they receive is shown as `NATIVE_ESC_…`.
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
    print('\x1b[2J\x1b[H'+said+'\r\n'+''.join(s+'\r\n' for s in seen[-14:])+'❯ '+pending.replace('\x1b','^')+'\r\n',end='',flush=True)
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
  env:{...process.env,TERM:'xterm-256color',NODE_OPTIONS:'--require '+cfg.launcher}});
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
(async()=>{try{
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
  // draft stays and the native receives nothing.
  await key('\x1bb',250);await key('\x1bf',250);
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

    def popup(self, harness, scenario=None):
        case = self.root / (harness + (scenario or '')); case.mkdir(mode=0o700)
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
                  'scenario': scenario, 'backend': str(backend), 'diagnostic': str(case / 'display-errors'),
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
        self.assertIn('  1 아래 두 줄 (다) (추천) · +3', panel)
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
