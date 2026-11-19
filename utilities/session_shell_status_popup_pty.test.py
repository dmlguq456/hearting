"""11-15 (design 2): the status button and popup, the waiting blink and the popup's control filter over the real
host in a private PTY.

The panel's REPO row carries `현황 ›`; a press on the row opens the status popup (the table popup's frame,
width and keys): its title `현황 <campaign> · <age>`, Fleet's status lines for the project, the document with
its markdown table aligned, and the sections `현황 · QUESTIONS · ROUTE · MESSAGES` on its bottom line (←→ in
that order). PgDn reads it on. A change to the document (the existing file watch) reads the value and the
standing popup again; nothing else does: a hundred paints and the projections between them read nothing,
and the popup opened again from another section uses the rows it read. Without a document the button is
dim and its popup says so. The MESSAGES popup draws a request body's table aligned. A popup row never
carries a C1 control to the terminal. While a question waits, its red letters (and the folded strip's
`◑답 대기 N`) go between two reds on the breath's beat, and a beat writes only those cells.

Fleet's collection, the questions and the status value are fixtures (the value's read is counted); frames
are read back from a private headless terminal; nothing here reaches a real account, session or terminal.
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
pending='';said='READY_'+harness
def screen():
    print('\x1b[2J\x1b[H'+said+'\r\n'+'❯ '+pending[-60:]+'\r\n',end='',flush=True)
screen()
while True:
    data=os.read(0,65536)
    if not data:break
    pending+=data.decode(errors='replace')
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        if line=='quit':raise SystemExit
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
# collection (`vm.json`), the questions, the requests, and the status value (`session_status.read`, whose
# every call is noted in `reads.log`; its document and folder are this case's own).
BACKEND = r"""#!{python}
import json,os,sys,time
sys.path.insert(0,{utilities!r});sys.path.insert(0,{tools!r})
import session_shell as m
from fleet import session_tags, session_status
OWN,OTHER={sid!r},{other!r}
def tag(harness,sid):return {{OWN:'e9',OTHER:'12'}}.get(sid)
session_tags.assigned_tag=tag
def vm(harness,sid,kind):
    from fleet.explorer.view_model import ExplorerViewModel
    return ExplorerViewModel.from_mapping(json.load(open({vm!r})))
m._popup_view_model=vm
def questions(harness,sid,**_kw):
    return json.load(open({questions!r})).get(sid,[])
m.session_questions=questions
def requests(harness,sid,now,errors=None):
    record={{'id':'req-0001','state':'open','title':'표 본문 요청','requester':{{'harness':harness,'session_id':sid,'name':'e9'}},
            'recipient':{{'harness':'claude','session_id':OTHER,'name':'e12'}},'revisions':[{{'n':1,'state':'current','transfer_ref':'t1'}}],
            'reports':[],'updated':now}}
    body='요청 본문 첫 줄\n| 이름 | 값 |\n|:--|--:|\n| 가 | 12 |\n| 나나 | 3 |\n요청 끝 줄'
    return [{{'record':record,'side':'out','rows':{{}},'body':body}}],[]
m._request_views=requests
DOC,FOLDER={doc!r},{folder!r}
def read(cwd,routes=(),now=None):
    with open({reads!r},'a') as f:f.write(json.dumps([cwd,list(routes)])+'\n')
    if os.path.exists(DOC+'.none'):
        return {{'word':'현황','has_doc':False,'campaign':'panel-test','age':'','title':'현황','path':'',
                'slot':'campaigns/panel-test/현황.md','watch':[FOLDER+'/']}}
    return {{'word':'현황','has_doc':True,'campaign':'panel-test','age':'3m','title':'현황 panel-test · 3m','path':DOC,
            'slot':'','watch':[DOC,FOLDER+'/']}}
session_status.read=read
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
let chunks=null;
child.onData(d=>{term.write(d);if(chunks)chunks.push(d);});
const flush=()=>new Promise(r=>term.write('',r));
const cellsOf=y=>{const line=term.buffer.active.getLine(y),out=[];for(let x=0;x<cols;x++){const c=line?.getCell(x);out.push(!c?' ':c.getWidth()===0?'':c.getChars()||' ');}return out;};
const screen=async()=>{await flush();return Array.from({length:rows},(_,y)=>cellsOf(y).join('').trimEnd());};
const at=async needle=>{await flush();for(let y=0;y<rows;y++){const c=cellsOf(y),where=[];let text='';
  c.forEach((ch,x)=>{for(const _ of ch)where.push(x);text+=ch;});const i=text.indexOf(needle);if(i>=0)return {x:where[i],y};}return null;};
const until=async(test,what,ms=15000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return s;await sleep(40);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>line.includes(word));
const press=async(x,y,ms=200)=>{child.write(`\x1b[<0;${x+1};${y+1}M\x1b[<0;${x+1};${y+1}m`);await sleep(ms);};
const key=async(bytes,ms=250)=>{child.write(bytes);await sleep(ms);};
const box=s=>{const top=s.findIndex(l=>l.includes('╭─')&&l.includes('✕ ─╮'));if(top<0)return null;
  const left=[...s[top]].indexOf('╭'),bottom=s.findIndex((l,i)=>i>top&&[...l][left]==='╰'&&l.includes('╯'));
  return {top,bottom,title:s[top],foot:s[bottom]||'',rows:s.slice(top,bottom+1),left};};
const popupHas=word=>s=>{const b=box(s);return !!b&&b.rows.some(l=>l.includes(word));};
const reads=()=>{try{return fs.readFileSync(cfg.reads,'utf8').split('\n').filter(Boolean).length;}catch{return 0;}};
// The look of the cells of a row (screen columns from..to): foreground palette index or `d`/`x`, and dim.
const looks=(y,from,to)=>{const line=term.buffer.active.getLine(y),out=[];for(let x=from;x<to;x++){const c=line?.getCell(x);if(!c||c.getWidth()===0)continue;
  out.push({x,ch:c.getChars(),fg:c.isFgPalette()?c.getFgColor():c.isFgDefault()?'d':'x',dim:!!c.isDim(),inverse:!!c.isInverse()});}return out;};
const frames={},checks={};
// (Private, as the shared cache's reader requires.)
const write=(file,value)=>{fs.writeFileSync(file+'.new',value,{mode:0o600});fs.renameSync(file+'.new',file);};
(async()=>{try{
  let s=await until(s=>has('READY_'+cfg.harness)(s)&&has('현황 ›')(s)&&has('QUESTIONS')(s),'the panel with its status button');
  await sleep(600);s=await screen();frames.panel=s;
  const repo=await at('REPO'),button=await at('현황 ›'),head=await at('QUESTIONS');
  checks.button_row=repo&&button&&repo.y===button.y;
  // `›` stands in the column of the section heads' `›`: two cells inside the panel's right line. (11-16: the QUESTIONS
  // section is a thin box now, its `›` beside its name; the column is read from the panel's line on its top row.)
  const qrow=s[head.y],arrowAt=y=>{const c=cellsOf(y);for(let x=cols-1;x>=0;x--)if(c[x]==='›')return x;return -1;};
  const edgeAt=y=>{const c=cellsOf(y);for(let x=cols-1;x>=0;x--)if(c[x]==='│')return x;return -1;};
  checks.arrow_column=[arrowAt(button.y),edgeAt(head.y)-2];
  checks.button_look=looks(button.y,button.x,button.x+6).filter(c=>c.ch.trim()).map(c=>[c.ch,c.fg,c.dim]);
  // A hundred paints (the native echoes each key) and two projections (the shared cache changes): no read.
  const before=reads();checks.first_reads=before;
  for(let i=0;i<100;i++){child.write('a');await sleep(15);}
  await sleep(400);
  for(const title of ['표 하나','표 둘']){const cache=JSON.parse(fs.readFileSync(cfg.cache,'utf8'));cache.observed_at=Date.now()/1000;
    cache.route_panels[cfg.key][0].title=title;write(cfg.cache,JSON.stringify(cache));await until(has(title),'the route renamed '+title);}
  await sleep(400);checks.reads_after_paints=reads()-before;
  // A press on the REPO row: the status popup.
  const r0=reads();
  await press(repo.x+2,repo.y,300);
  // (The document stands first and Fleet's lines come above it a moment later: both are waited for.)
  s=await until(s=>!!box(s)&&box(s).title.includes('현황 panel-test · 3m')&&popupHas('┈')(s)&&popupHas('시험 마무리')(s),'the status popup');frames.popup=s;
  const b=box(s);checks.title=b.title;checks.foot=b.foot;checks.popup_reads=reads()-r0;
  checks.lines=b.rows.map(l=>l.slice(b.left));
  // PgDn reads it on (its place `i/N` moves), no read.
  const place=s=>{const m=(box(s)?box(s).rows.join('\n'):'').match(/(\d+)\/(\d+) · /);return m?[+m[1],+m[2]]:null;};
  checks.place=[place(s)];const r1=reads();
  await key('\x1b[6~',400);s=await screen();checks.place.push(place(s));frames.paged=s;
  await key('\x1b[5~',400);s=await screen();checks.place.push(place(s));
  // → the question popup; ← back: the rows it read, no read. ← from it: MESSAGES (the request's table aligned).
  await key('\x1b[C',400);s=await until(s=>!!box(s)&&box(s).title.includes('QUESTIONS'),'the question popup by →');frames.questions=s;
  checks.question_line=box(s).rows.map(l=>l.slice(box(s).left)).find(l=>l.includes('질문 본문'))||'';
  {const y=box(s).top+box(s).rows.findIndex(l=>l.includes('질문 본문'));checks.question_inverse=looks(y,0,cols).filter(c=>c.inverse).length;}
  await key('\x1b[D',400);s=await until(s=>!!box(s)&&box(s).title.includes('현황'),'back to the status popup');
  await key('\x1b[D',400);s=await until(s=>!!box(s)&&box(s).title.includes('MESSAGES'),'the MESSAGES popup by ← (round)');frames.messages=s;
  checks.messages=box(s).rows.map(l=>l.slice(box(s).left));
  await key('\x1b[C',400);s=await until(s=>!!box(s)&&box(s).title.includes('현황'),'the status popup by → (round)');
  await sleep(300);checks.reads_on_moves=reads()-r1;
  // The document changes: the existing watch reads the value and the standing popup again.
  const r2=reads();
  fs.writeFileSync(cfg.doc,fs.readFileSync(cfg.doc,'utf8').replace('단추와 팝업.','CHANGED_LINE_77 바뀐 줄.'));
  s=await until(popupHas('CHANGED_LINE_77'),'the popup read again at the watch event');frames.changed=s;
  await sleep(600);checks.reads_on_change=reads()-r2;
  await key('\x1b',400);await until(s=>!box(s),'closed');
  // No document: the button dim, and its popup says so.
  fs.writeFileSync(cfg.doc+'.none','');fs.writeFileSync(cfg.folder+'/touch','');
  await sleep(1800);s=await screen();
  {const p=await at('현황 ›');checks.dim_look=p?looks(p.y,p.x,p.x+6).filter(c=>c.ch.trim()).map(c=>[c.ch,c.fg,c.dim]):null;}
  {const p=await at('REPO');await press(p.x+2,p.y,300);}
  s=await until(s=>!!box(s)&&popupHas(cfg.no_status)(s),'the status popup without a document');frames.nodoc=s;
  checks.nodoc_title=box(s).title;
  await key('\x1b',400);await until(s=>!box(s),'closed');
  fs.unlinkSync(cfg.doc+'.none');fs.writeFileSync(cfg.folder+'/touch','2');await sleep(1800);
  // The blink: idle, a question waiting; over five seconds only its red letters are written, between two reds.
  const reds=()=>{const out=[];for(let y=0;y<rows;y++)for(const c of looks(y,cols-40,cols))if(c.fg===217||c.fg===95)out.push([y,c.x,c.fg]);return out;};
  await sleep(300);await flush();
  chunks=[];const a=reds();const t0=Date.now();
  const samples=[];for(let i=0;i<11;i++){await sleep(500);await flush();samples.push(reds());}
  checks.blink={chunks,first:a,samples,ms:Date.now()-t0};chunks=null;
  // The folded strip (90 columns): `◑답 대기 N` blinks; a beat writes only its cells.
  cols=90;rows=24;term.resize(cols,rows);child.resize(cols,rows);
  await until(s=>s.at(-1).includes('▴')&&!s.some(l=>l.includes('REPO')),'the folded strip');await sleep(600);await flush();
  const stripReds=()=>looks(rows-1,0,cols).filter(c=>c.fg===217||c.fg===95).map(c=>[c.x,c.fg]);
  chunks=[];const f=[];for(let i=0;i<11;i++){await sleep(500);await flush();f.push(stripReds());}
  checks.strip={chunks,samples:f,row:(await screen()).at(-1)};chunks=null;
  fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""


def route_row(rid, title, state, stage, st):
    return {'id': rid, 'title': title, 'state': state, 'worker_stage': stage, 'shape': 'one-shot-owner',
            'stages': [{'name': stage, 'st': st}], 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'high'}


def cell_width(ch):
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 0 if unicodedata.combining(ch) else 1


SPIN = set('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')


def written_cells(chunk, spin=False):
    """The screen cells (row, column; 1-based) a chunk writes glyphs to, after each cursor move (a spinner's
    glyph, which turns on the same beat, left out unless ``spin``)."""
    out, row, col = [], None, None
    for token in re.findall(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[78]|[^\x1b]', chunk):
        move = re.fullmatch(r'\x1b\[(\d+);(\d+)H', token)
        if move:
            row, col = int(move.group(1)), int(move.group(2))
        elif token.startswith('\x1b'):
            continue
        elif row is not None:
            if spin or token not in SPIN:
                out.append((row, col))
            col += max(1, cell_width(token))
    return out


class SessionShellStatusPopupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hss-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('shell_status_popup_isolation', ROOT / 'tools/run-tests.py')
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
        # The shared cache's producer would rewrite the seeded values (it has no real state here): its lock is held.
        cache = Path(cls.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True); cache.chmod(0o700)
        cls.lock = os.open(cache / '.projection.lock', os.O_CREAT | os.O_RDWR, 0o600)
        cls.addClassCleanup(os.close, cls.lock)
        fcntl.flock(cls.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cls.fake = cls.root / 'native.py'; cls.fake.write_text(FAKE_NATIVE)
        cls.launcher = cls.root / 'launcher.cjs'; cls.launcher.write_text(LAUNCHER)
        cls.driver = cls.root / 'driver.cjs'; cls.driver.write_text(DRIVER)
        catalog = json.loads((ROOT / 'tools/i18n/messages.json').read_text())['messages']
        cls.no_status = catalog['fleet.repo.no_status']['ko']

    def run_shell(self, harness):
        case = self.root / harness; case.mkdir(mode=0o700)
        project = case / 'project'; project.mkdir()
        folder = case / 'campaigns' / '2026-11-15_panel-test'; folder.mkdir(parents=True)
        doc = folder / '현황.md'
        doc.write_text('# panel-test 현황\n\n**한 줄**: 단추와 팝업.\n\n## 판\n\n| 이름 | 상태 | 비고 |\n|:--|:-:|--:|\n'
                       '| 현황 단추 | 완료 | 1 |\n| 점멸 | 진행 | 12 |\n\n## 기록\n'
                       + ''.join('- 기록 줄 %02d\n' % n for n in range(1, 61)))
        sid = {'claude': '00000000-0000-4000-8000-0000000000d1', 'codex': '00000000-0000-4000-8000-0000000000d2'}[harness]
        cache = Path(self.env['XDG_STATE_HOME']) / 'hearting/human-waits'
        key = harness + '\0' + sid
        target = cache / 'projection.json'
        value = json.loads(target.read_text()) if target.exists() else {
            'schema_version': 3, 'route_row_form': 1, 'items': [], 'by_session': {}, 'total': 0, 'older': 0, 'complete': True,
            'route_panels': {}, 'route_status': {}, 'route_sessions': {}}
        value.update(observed_at=time.time())
        value['route_panels'][key] = [route_row('rt-b000000000000001', '현황 팝업 구현', 'working', 'one-shot', 'active')]
        value['route_status'][key] = 'route 1 ⠏1'; value['route_sessions'][key] = time.time() + 3600
        target.write_text(json.dumps(value)); target.chmod(0o600)
        vm = case / 'vm.json'
        vm.write_text(json.dumps({'repos': [{'id': 'project', 'name': 'project',
                                             'status': {'routes': 1, 'questions': 1, 'campaigns': [{'campaign': 'panel-test', 'line': '단추와 팝업'}],
                                                        'next': '시험 마무리'}}],
                                  'system': {}, 'routes': [],
                                  'sessions': [{'tag': 'e9', 'repo_id': 'project', 'harness': harness, 'model': 'Opus 5.5', 'effort': 'high',
                                                'context': 46, 'window': 200000, 'state': 'idle', 'title': '이 창', 'age': '23m'}]},
                                 ensure_ascii=False))
        questions = case / 'questions.json'
        questions.write_text(json.dumps({sid: [{'id': 'S4:own', 'revision': 'a' * 64, 'answerable': True, 'questions': [
            {'id': 'shape', 'question': '질문 본문 \u009b7m뒤 글', 'choices': ['첫째 안', '둘째 안']}]}]}, ensure_ascii=False))
        reads = case / 'reads.log'
        backend = case / 'backend'
        backend.write_text(BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'), tools=str(ROOT / 'tools'),
                                          sid=sid, other=OTHER, vm=str(vm), questions=str(questions), doc=str(doc),
                                          folder=str(folder), reads=str(reads)))
        backend.chmod(0o700)
        config = {'harness': harness, 'sid': sid, 'key': key, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'cache': str(target),
                  'project': str(project), 'result': str(case / 'result.json'), 'reads': str(reads), 'doc': str(doc),
                  'folder': str(folder), 'no_status': self.no_status}
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
        result['reads'] = [json.loads(line) for line in reads.read_text().splitlines()] if reads.exists() else []
        result['project'] = str(project)
        return result

    def check(self, harness):
        result = self.run_shell(harness); frames, checks = result['frames'], result['checks']
        panel = '\n'.join(frames['panel'])
        # The button: on the REPO row, `›` in the section heads' column, the word in the body color.
        self.assertTrue(checks['button_row'], panel)
        arrow, heads = checks['arrow_column']
        self.assertEqual(arrow, heads, panel)
        self.assertEqual([c[0] for c in checks['button_look']], ['현', '황', '›'])
        self.assertEqual(checks['button_look'][0][1:], [253, False], checks['button_look'])
        self.assertTrue(checks['button_look'][2][2], checks['button_look'])
        # The value was read for this session's folder and its routes; a hundred paints and two projections read nothing.
        # (The first projection may come before the routes do: the route list's change reads it again.)
        self.assertTrue(all(read[0] == result['project'] for read in result['reads']), result['reads'])
        self.assertIn(['rt-b000000000000001'], [read[1] for read in result['reads']])
        self.assertGreaterEqual(checks['first_reads'], 1)
        self.assertEqual(checks['reads_after_paints'], 0, checks)
        # The popup: its title, Fleet's status lines, the document's table aligned, the sections in order.
        self.assertEqual(checks['popup_reads'], 1, checks)
        self.assertIn('현황 panel-test · 3m', checks['title'])
        self.assertIn('╰─ 현황 · QUESTIONS · ROUTE · MESSAGES ', checks['foot'])
        # (Each row's inside: between the popup's side lines; the panel stands to the right of it.)
        lines = [line.split('│')[1] if line.startswith('│') else line for line in checks['lines']]
        self.assertTrue(any('panel-test: 단추와 팝업' in line for line in lines), lines)
        self.assertTrue(any('next:' in line and '시험 마무리' in line for line in lines), lines)
        heads_row = next(line for line in lines if '이름' in line and '상태' in line)
        self.assertRegex(heads_row, r'이름 {2,}.*상태 {2,}비고')
        self.assertFalse(any('|:--' in line or '| 이름' in line for line in lines), lines)
        self.assertTrue(any(set(line.strip()) == {'┈'} for line in lines), lines)
        # PgDn reads on and PgUp back; moving between sections and back reads nothing.
        first, paged, back = checks['place']
        self.assertTrue(first and paged and back and paged[0] > first[0] and back[0] == first[0], checks['place'])
        self.assertEqual(checks['reads_on_moves'], 0, checks)
        # The MESSAGES popup: the request body's table aligned, its other lines as before.
        messages = [line.split('│')[1] if line.startswith('│') else line for line in checks['messages']]
        self.assertTrue(any('요청 본문 첫 줄' in line for line in messages), messages)
        self.assertTrue(any(re.search(r'이름 {2,}값', line) for line in messages), messages)
        self.assertFalse(any('|:--|' in line for line in messages), messages)
        # A C1 control in a question's text reaches the terminal as a space: the words after it stand, none reversed.
        self.assertIn('질문 본문  7m뒤 글', checks['question_line'])
        self.assertEqual(checks['question_inverse'], 0)
        # The watch event: the value and the standing popup read again (two reads), the new line shown.
        self.assertEqual(checks['reads_on_change'], 2, checks)
        # No document: the button dim; its popup says so under `현황`.
        self.assertTrue(checks['dim_look'] and all(c[2] for c in checks['dim_look']), checks['dim_look'])
        self.assertIn('╭─ 현황 ', checks['nodoc_title'])
        # The blink: the question's box lines (11-16: its top line, its walls and its bottom line, not the letters inside)
        # go between 217 and 95 together, once a second (the section head's count, a red that does not blink, keeps
        # 174); every beat writes only blinking cells, a few dozen at most (the box's lines are more cells than the letters
        # were: the bound stands at 80 where the letters', 60, was).
        blink = checks['blink']
        seen = {}
        for sample in [blink['first'], *blink['samples']]:
            for y, x, fg in sample:
                seen.setdefault((y + 1, x + 1), set()).add(fg)
        moving = {cell for cell, tones in seen.items() if tones == {217, 95}}
        self.assertGreaterEqual(len(moving), 10, seen)
        phases = {tuple(sorted({fg for y, x, fg in sample if (y + 1, x + 1) in moving})) for sample in blink['samples']}
        self.assertEqual(phases, {(217,), (95,)}, phases)
        # (The working route's count pulses on Fleet's same second, so a change rides the paint that rewrites that row:
        # the beats are the writes that touch a blinking cell, one a second, and on the blinking rows they write
        # nothing but the blinking cells.)
        beats = [chunk for chunk in blink['chunks'] if set(written_cells(chunk)) & moving]
        self.assertTrue(4 <= len(beats) <= 7, (len(beats), blink['ms']))
        blinking_rows = {row for row, _col in moving}
        for chunk in beats:
            cells = [cell for cell in written_cells(chunk) if cell[0] in blinking_rows]
            self.assertTrue(cells and set(cells) <= moving, sorted(set(cells) - moving)[:5])
        load = {'beats': len(beats), 'blinking_cells': len(moving),
                'cells_per_beat': [len([cell for cell in written_cells(c) if cell[0] in blinking_rows]) for c in beats],
                'bytes_per_beat': [len(c.encode()) for c in beats]}
        self.assertTrue(all(n <= 80 for n in load['cells_per_beat']), load)
        # The folded strip: `◑답 대기 N` alone moves, between the same two reds.
        strip = checks['strip']
        self.assertRegex(strip['row'], r'◑답 대기 \S+')
        seen = {}
        for sample in strip['samples']:
            for x, fg in sample:
                seen.setdefault(x + 1, set()).add(fg)
        wait = {x for x, tones in seen.items() if tones == {217, 95}}
        self.assertGreaterEqual(len(wait), 5, seen)
        stones = {tuple(sorted({fg for x, fg in sample if x + 1 in wait})) for sample in strip['samples']}
        self.assertEqual(stones, {(217,), (95,)}, stones)
        strip_beats = [chunk for chunk in strip['chunks'] if chunk.startswith('\x1b[?2026h\x1b7') and written_cells(chunk)]
        self.assertTrue(strip_beats)
        for chunk in strip_beats:
            cells = written_cells(chunk)
            self.assertTrue(cells and all(col in wait for _row, col in cells), cells)
        load.update(strip_beats=len(strip_beats), strip_cells_per_beat=[len(written_cells(c)) for c in strip_beats],
                    strip_bytes_per_beat=[len(c.encode()) for c in strip_beats])
        print(json.dumps({'event': 'session-shell-status-popup', 'harness': harness, **load}), flush=True)
        return result

    def test_claude(self):
        self.check('claude')

    def test_codex(self):
        self.check('codex')


if __name__ == '__main__':
    unittest.main()
