"""11-16 (panel polish 1116, 3 and 4): the waiting blink on Fleet's one-second beat and the head rule's light,
over the real host in a private PTY with the person's own settings (side, right, 50 cells, `terminal`, no
shade, `rule motion` `rule`, Claude) in a 170 x 46 window.

The person on v4.40.0: "점멸은 안하는데". The blink then went between 174 and 131 (an L* gap of 15) once
every 2.4 seconds on the breath's beat. Now the question to answer here (its red letters), and the folded
strip's `◑답 대기 N`, go between `FACES.*.waitBlink` (two reds an L* gap of 25 or more apart, both readable) on
Fleet's own beat (`render_cells._blink_phase`: lit in the even second, a change every second, on the clock's
second), whatever this session's state; a beat writes only those cells (and, while the session runs or waits,
the frame's or rule's own moving cells); with no answer waiting and nothing running, nothing is written.
While the session runs under `rule`, the head rule's light (`FACES.*.scan`) has its brightest cells as bright as
the body text and darkens in at least three steps toward the still rule.

The color bytes the terminal receives are read with the time they arrive; Fleet's collection and the
questions are fixtures; nothing here reaches a real account, session or terminal.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import fcntl
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
SID = '00000000-0000-4000-8000-0000000016d1'

FAKE_NATIVE = r"""
import json,os,socket,sys,time,tty
harness,sid=sys.argv[1],sys.argv[2]
def event(kind):
    raw=open('/proc/self/stat').read();start=raw.rsplit(') ',1)[1].split()[19]
    req={'op':'event','event':kind,'harness':harness,'sid':sid,'pid':os.getpid(),'start':start,
         'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET']);c.sendall((json.dumps(req)+'\n').encode());c.recv(8192)
tty.setraw(0)
event('start');event('stop')
pending='';said='READY_'+harness
def screen():
    print('\x1b[2J\x1b[H'+said+'\r\n'+'> '+pending[-60:]+'\r\n',end='',flush=True)
screen()
while True:
    data=os.read(0,65536)
    if not data:break
    pending+=data.decode(errors='replace')
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        if line=='quit':raise SystemExit
        if line.startswith('ev:'):event(line[3:])
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

BACKEND = r"""#!{python}
import json,os,sys
sys.path.insert(0,{utilities!r});sys.path.insert(0,{tools!r})
import session_shell as m
from fleet import session_tags
session_tags.assigned_tag=lambda harness,sid:{{{sid!r}:'e9'}}.get(sid)
def questions(harness,sid,**_kw):
    return json.load(open({questions!r})).get(sid,[])
m.session_questions=questions
raise SystemExit(m._backend())
"""

DRIVER = r"""
const fs=require('node:fs');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let cols=170,rows=46;
const term=new Terminal({cols,rows,allowProposedApi:true,scrollback:0});
term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const child=pty.spawn(cfg.python,[cfg.entry,cfg.harness,cfg.python,cfg.fake,cfg.harness,cfg.sid],{cols,rows,cwd:cfg.project,
  env:{...process.env,TERM:'xterm-256color',NODE_OPTIONS:'--require '+cfg.launcher}});
let chunks=null;
child.onData(d=>{term.write(d);if(chunks)chunks.push([Date.now(),d]);});
const flush=()=>new Promise(r=>term.write('',r));
const cellsOf=y=>{const line=term.buffer.active.getLine(y),out=[];for(let x=0;x<cols;x++){const c=line?.getCell(x);out.push(!c?' ':c.getWidth()===0?'':c.getChars()||' ');}return out;};
const screen=async()=>{await flush();return Array.from({length:rows},(_,y)=>cellsOf(y).join('').trimEnd());};
// The ceiling only ends a hang: each wait returns the moment its screen shows, so a loaded machine that needs
// 30 s to paint the panel is slow, not wrong (4 cores shared by 14 busy loops: 20 s was not enough, 60 s was).
const until=async(test,what,ms=60000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return s;await sleep(40);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>line.includes(word));
const record=async(name,ms)=>{await sleep(400);await flush();chunks=[];const t0=Date.now();await sleep(ms);await flush();
  const s=await screen();phases[name]={t0,chunks,screen:s};chunks=null;};
const type=async line=>{child.write(line+'\r');await sleep(500);};
const write=(file,value)=>{fs.writeFileSync(file+'.new',value,{mode:0o600});fs.renameSync(file+'.new',file);};
const phases={};
(async()=>{try{
  await until(s=>has('READY_'+cfg.harness)(s)&&has('QUESTIONS')(s)&&has(cfg.question)(s),'the panel with its question');
  await record('idle',5600);
  await type('ev:working');await until(has('DONE_ev:working'),'running');
  await record('working',5600);
  await type('ev:permission');await until(has('DONE_ev:permission'),'waiting');
  await record('blocked',5600);
  await type('ev:stop');await until(has('DONE_ev:stop'),'idle again');
  // No answer waiting, nothing running: nothing is written.
  write(cfg.questions,JSON.stringify({}));
  const cache=JSON.parse(fs.readFileSync(cfg.cache,'utf8'));cache.observed_at=Date.now()/1000;write(cfg.cache,JSON.stringify(cache));
  await until(s=>!has(cfg.question)(s),'the question gone');
  await record('quiet',3200);
  // The folded strip (90 columns), the question back: `◑답 대기 N` blinks.
  write(cfg.questions,fs.readFileSync(cfg.questions+'.keep','utf8'));
  cols=90;rows=24;term.resize(cols,rows);child.resize(cols,rows);
  await until(s=>s.at(-1).includes('▴')&&!s.some(l=>l.includes('QUESTIONS')),'the folded strip');
  {const cache=JSON.parse(fs.readFileSync(cfg.cache,'utf8'));cache.observed_at=Date.now()/1000;write(cfg.cache,JSON.stringify(cache));}
  await until(s=>/◑\S* ?\S+ \d/.test(s.at(-1))||s.at(-1).includes('◑'),'the strip with its wait');
  // The strip's own window: its first change can come two seconds in under load, and four changes at the
  // second still have to fit (the beat's own assertions are the same as the other phases').
  await record('strip',7600);
  fs.writeFileSync(cfg.result,JSON.stringify({phases}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x15quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""

FACES = r"""
const view=require(process.argv[1]);
console.log(JSON.stringify({dark:view.FACES.dark,light:view.FACES.light}));
"""


def cell_width(ch):
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 0 if unicodedata.combining(ch) else 1


def lightness(index):
    """CIE L* of a 256-color index (the xterm cube and gray ramp)."""
    if index >= 232:
        rgb = [8 + 10 * (index - 232)] * 3
    else:
        n = index - 16
        rgb = [0 if v == 0 else 55 + 40 * v for v in (n // 36, n // 6 % 6, n % 6)]
    lin = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return 116 * y ** (1 / 3) - 16 if y > 0.008856 else 903.3 * y


def writes(chunks):
    """Every glyph the chunks write: (time ms, row, column (1-based), glyph, 256-color foreground or None), and per
    chunk the cells and bytes it writes."""
    out, per = [], []
    row = col = None
    fg = None
    for at, chunk in chunks:
        cells = []
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
            out.append((at, row, col, token, fg)); cells.append((row, col))
            col += max(1, cell_width(token))
        per.append({'at': at, 'cells': cells, 'bytes': len(chunk.encode())})
    return out, per


class WaitBlinkPtyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hwb-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('shell_wait_blink_isolation', ROOT / 'tools/run-tests.py')
        runner = importlib.util.module_from_spec(spec); sys.modules[spec.name] = runner; spec.loader.exec_module(runner)
        cls.env = runner.build_isolated_env(cls.root, repo_root=ROOT)
        cls.env['CLAUDE_CONFIG_DIR'] = str(cls.root / 'native-claude')
        cls.env['FLEET_NOW_LANG'] = 'ko'
        cls.env.pop('COLORTERM', None); cls.env.pop('NO_COLOR', None)
        policy = Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME']) / '.config') / 'hearting/session-shell.json'
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({'enabled': True, 'layout': 'side', 'position': 'right', 'side_width': 50,
                                      'theme': 'terminal', 'shade': 'none', 'rule_motion': 'rule'}))
        policy.chmod(0o600)
        for name in ('npm-cache', 'node-gyp'):
            cls.env['npm_config_' + ('cache' if name == 'npm-cache' else 'devdir')] = str(cls.root / name)
        cls.env['npm_config_userconfig'] = str(cls.root / 'npmrc'); (cls.root / 'npmrc').write_text('')
        sys.path[:0] = [str(ROOT / 'tools/install'), str(ROOT / 'utilities')]
        import session_shell_install
        with mock.patch.dict(os.environ, cls.env, clear=True):
            assets = session_shell_install.prepare_assets()
        cls.env['NODE_PATH'] = str(assets / 'node_modules')
        cache = Path(cls.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True); cache.chmod(0o700)
        cls.lock = os.open(cache / '.projection.lock', os.O_CREAT | os.O_RDWR, 0o600)
        cls.addClassCleanup(os.close, cls.lock)
        fcntl.flock(cls.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cls.fake = cls.root / 'native.py'; cls.fake.write_text(FAKE_NATIVE)
        cls.launcher = cls.root / 'launcher.cjs'; cls.launcher.write_text(LAUNCHER)
        cls.driver = cls.root / 'driver.cjs'; cls.driver.write_text(DRIVER)
        faces = subprocess.run([cls.node, '-e', FACES, str(ROOT / 'utilities/session-shell/panel-view.cjs')],
                               capture_output=True, text=True, timeout=60, check=True)
        cls.faces = json.loads(faces.stdout)

    def run_shell(self):
        harness = 'claude'
        case = self.root / harness; case.mkdir(mode=0o700)
        project = case / 'project'; project.mkdir()
        cache = Path(self.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        key = harness + '\0' + SID
        target = cache / 'projection.json'
        value = {'schema_version': 3, 'route_row_form': 1, 'items': [], 'by_session': {}, 'total': 0, 'older': 0, 'complete': True,
                 'route_panels': {}, 'route_status': {}, 'route_sessions': {}, 'observed_at': time.time()}
        value['route_panels'][key] = [{'id': 'rt-b000000000001116', 'title': '패널 손질 일곱', 'state': 'idle',
                                       'worker_stage': 'one-shot', 'shape': 'one-shot-owner',
                                       'stages': [{'name': 'one-shot', 'st': 'pending'}], 'harness': 'claude',
                                       'model': 'Opus 5.5', 'effort': 'high'}]
        value['route_status'][key] = 'route 1'; value['route_sessions'][key] = time.time() + 3600
        target.write_text(json.dumps(value)); target.chmod(0o600)
        question = '점멸 확인 질문'
        questions = case / 'questions.json'
        body = json.dumps({SID: [{'id': 'S4:own', 'revision': 'a' * 64, 'answerable': True, 'questions': [
            {'id': 'shape', 'question': question + ' 본문', 'choices': ['첫째 안', '둘째 안']}]}]}, ensure_ascii=False)
        questions.write_text(body); (case / 'questions.json.keep').write_text(body)
        backend = case / 'backend'
        backend.write_text(BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'), tools=str(ROOT / 'tools'),
                                          sid=SID, questions=str(questions)))
        backend.chmod(0o700)
        config = {'harness': harness, 'sid': SID, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'cache': str(target),
                  'project': str(project), 'result': str(case / 'result.json'), 'questions': str(questions),
                  'question': question}
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
        return json.loads((case / 'result.json').read_text())['phases']

    def blink(self, phase, pair):
        """The blinking cells (both reds seen there), each change of their red with its time, and the beats."""
        glyphs, per = writes(phase['chunks'])
        tones = {}
        for at, row, col, ch, fg in glyphs:
            if fg in pair:
                tones.setdefault((row, col), []).append((at, fg))
        moving = {cell for cell, seen in tones.items() if len({fg for _at, fg in seen}) == 2}
        changes = []
        for cell in moving:
            last = None
            for at, fg in tones[cell]:
                if fg != last and last is not None:
                    changes.append((at, fg))
                last = fg
        beats = sorted({at for at, _fg in changes})
        return moving, beats, per

    def test_the_wait_blinks_on_fleets_beat_between_two_clearly_different_reds(self):
        phases = self.run_shell()
        dark = self.faces['dark']
        pair = set(dark['waitBlink'])
        report = {'waitBlink': dark['waitBlink'], 'L*': [round(lightness(v), 1) for v in dark['waitBlink']],
                  'light_waitBlink': self.faces['light']['waitBlink'],
                  'light_L*': [round(lightness(v), 1) for v in self.faces['light']['waitBlink']]}
        found = {}
        for name in ('idle', 'working', 'blocked', 'strip'):
            moving, beats, per = self.blink(phases[name], pair)
            gaps = [b - a for a, b in zip(beats, beats[1:])]
            blink_chunks = [p for p in per if p['cells'] and set(p['cells']) & moving]
            found[name] = {'cells': len(moving), 'changes_ms': [b - phases[name]['t0'] for b in beats], 'gaps_ms': gaps,
                           'second_offset_ms': [b % 1000 for b in beats],
                           'beat_bytes': [p['bytes'] for p in blink_chunks],
                           'beat_cells': [len(p['cells']) for p in blink_chunks],
                           'only_blink': all(set(p['cells']) <= moving for p in blink_chunks)}
        # The head rule's light while running: the colors its cells took, brightest first.
        glyphs, _per = writes(phases['working']['chunks'])
        rule_rows = {}
        for _at, row, col, ch, fg in glyphs:
            if ch == '─' and isinstance(fg, int):
                rule_rows.setdefault(row, set()).add(fg)
        rule = max(rule_rows.values(), key=len) if rule_rows else set()
        found['rule_light'] = sorted(rule, key=lambda v: -lightness(v))
        # The rule row as the terminal holds it after each write (its cells' colors), for the report.
        if rule_rows:
            y = max(rule_rows, key=lambda r: len(rule_rows[r]))
            held, frames = {}, []
            for at, row, col, ch, fg in glyphs:
                if row == y and ch == '─':
                    held[col] = fg
                    frames.append((at, dict(held)))
            seen, sampled = set(), []
            for at, cells in frames:
                if at in seen:
                    continue
                seen.add(at)
                lit = {c: v for c, v in cells.items() if v != dark['messages']}
                sampled.append([at - phases['working']['t0'], sorted(lit.items())])
            found['rule_frames'] = sampled[:40]
        _glyphs, quiet = writes(phases['quiet']['chunks'])
        found['quiet_writes'] = [p['bytes'] for p in quiet if p['cells']]
        print(json.dumps({'event': 'session-shell-wait-blink', **report, **found}, ensure_ascii=False), flush=True)
        # Two reds an L* gap of 25 or more apart, the darker still readable on a dark ground (L* 40 or more); the
        # light face's pair the same way round a light ground (its lighter one L* 60 or less).
        a, b = (lightness(v) for v in dark['waitBlink'])
        self.assertGreaterEqual(abs(a - b), 25, report)
        self.assertGreaterEqual(min(a, b), 40, report)
        a, b = (lightness(v) for v in self.faces['light']['waitBlink'])
        self.assertGreaterEqual(abs(a - b), 25, report)
        self.assertLessEqual(max(a, b), 60, report)
        for name in ('idle', 'working', 'blocked', 'strip'):
            got = found[name]
            self.assertGreaterEqual(got['cells'], 5 if name == 'strip' else 10, (name, got))
            # Fleet's beat: a change every second, on the clock's second (lit in the even one): four or more
            # changes in the window (5.6 seconds; the folded strip's 7.6, so a late first change still fits).
            self.assertGreaterEqual(len(got['gaps_ms']), 3, (name, got))
            self.assertTrue(all(850 <= g <= 1150 for g in got['gaps_ms']), (name, got))
            self.assertTrue(all(o < 150 for o in got['second_offset_ms']), (name, got))
            if name in ('idle', 'strip'):
                # Nothing else moves there: each beat writes the blinking cells alone, a few hundred bytes at most.
                self.assertTrue(got['only_blink'], (name, got))
                self.assertTrue(all(n <= 800 for n in got['beat_bytes']), (name, got))
        # No answer waiting and nothing running: nothing is written for three seconds.
        self.assertEqual(found['quiet_writes'], [], found['quiet_writes'])
        # The head rule's light: its brightest as bright as the body text, then four steps down to the still rule,
        # each an L* gap of 8 or more from the next (243 beside 101 was 5.8: a step that did not show).
        light = found['rule_light']
        self.assertTrue(light, found)
        self.assertGreaterEqual(lightness(light[0]), lightness(dark['body']) - 1, light)
        steps = [lightness(v) for v in light]
        self.assertGreaterEqual(len(steps), 5, light)
        self.assertTrue(all(a - b >= 8 for a, b in zip(steps, steps[1:])), [round(v, 1) for v in steps])


if __name__ == '__main__':
    unittest.main()
