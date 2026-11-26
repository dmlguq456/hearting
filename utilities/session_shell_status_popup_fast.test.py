"""11-16 (the status popup opened empty: its read took 10 s): the popup comes up at once and is never left empty.

The popup used to read the button's value again (a scan of every campaign folder) and then Fleet's whole
collection before it drew one row; the host cut such a read at 12 s. Now:

1. The document's rows come first, from the value the button already read (`session_status.held`, a path only
   when it names a status document inside the folder's own artifact root), with no wait on Fleet. Fleet's
   lines come after and stand above them; what the person was reading keeps its place. Fleet's part holds no
   place of the table's: a popup opened again (or moved away and back) while it is out stands at once, and
   its rows fill the popup standing when they arrive, at the width and generation they were asked for; rows
   of another width are not kept as this one's.
2. A read that is late shows the reading mark; one that failed or ran out its time shows Fleet's own word
   (`fleet.output.unreadable`), dim; the next opening reads again. A document that cannot be read answers
   that failure (not a `?` taken for a document) and keeps nothing. A Fleet part that fails leaves the document.
3. The button's first read failing is asked again at the next projection event, one worker at a time.
4. A hundred paints read nothing.

Part A runs the backend on a synthetic artifact root of 400 campaign folders; part B runs the real host in a
private PTY on the same kind of root (the button's read and Fleet's collection are given a synthetic delay, as
a slow disk and a long collection are). Nothing here reaches a real account, session, artifact root or terminal.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import fcntl
import importlib.util
import json
import random
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGNS = 400
SID = '00000000-0000-4000-8000-0000000000e1'


def make_root(base, campaigns=CAMPAIGNS):
    """A synthetic artifact root: ``campaigns`` folders (every other one with a status document, one of them
    newest and carrying ``NEWEST_DOC_LINE``), a project folder in it, and the marker that makes the root the
    project's. Returns ``(project folder, the newest document)``."""
    base = Path(base)
    reports = base / '.agent_reports'
    (reports / 'campaigns').mkdir(parents=True)
    (base / 'proj').mkdir()
    (base / '.agent-workspace').write_text('')
    rng, now, newest = random.Random(7), time.time(), None
    for index in range(campaigns):
        folder = reports / 'campaigns' / f'2026-0{1 + index % 9}-{10 + index % 18}_camp-{index:03d}'
        (folder / '2026-01-01_cycle' / 'artifacts').mkdir(parents=True)
        if index % 2:
            continue
        doc = folder / '현황.md'
        doc.write_text(f'# 현황\n\n**한 줄**: 캠페인 {index}\n\n## 다음\n- 일 {index}\n' + '본문 줄\n' * 40)
        stamp = now - rng.randint(3600, 40 * 86400)
        os.utime(doc, (stamp, stamp))
    newest = reports / 'campaigns' / '2026-01-10_camp-000' / '현황.md'
    newest.write_text('# 현황\n\n**한 줄**: NEWEST_DOC_LINE\n\n## 다음\n- 다음 일\n\n' + ''.join(f'- 기록 줄 {n:02d}\n' for n in range(1, 61)))
    return base / 'proj', newest


class BackendTest(unittest.TestCase):
    """Part A: the popup's backend read on a root of 400 campaign folders."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='hsf-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.project, cls.document = make_root(cls.temporary.name)
        sys.path[:0] = [str(ROOT / 'utilities'), str(ROOT / 'tools')]
        import session_shell
        from fleet import session_status
        cls.m, cls.status = session_shell, session_status

    def setUp(self):
        self.status.clear_cache()
        from fleet.collectors import project_status
        project_status.clear_cache()
        self.reads, self.vms, self.vm_delay, self.vm_fail = 0, 0, 0.0, False
        real = self.status.read

        def read(*args, **kwargs):
            self.reads += 1
            return real(*args, **kwargs)

        def vm(harness, sid, kind):
            self.vms += 1
            time.sleep(self.vm_delay)
            if self.vm_fail:
                raise RuntimeError('fleet collection failed')
            from fleet.explorer.view_model import ExplorerViewModel
            return ExplorerViewModel.from_mapping({
                'repos': [{'id': 'proj', 'name': 'proj', 'status': {'routes': 0, 'questions': 0, 'campaigns': [
                    {'campaign': 'camp-000', 'line': 'FLEET_STATUS_LINE'}]}}],
                'system': {}, 'routes': [], 'sessions': [{'tag': 'e1', 'repo_id': 'proj', 'harness': 'claude', 'state': 'idle'}]})
        for target, value in ((self.status, ('read', read)), (self.m, ('_popup_view_model', vm))):
            patch = mock.patch.object(target, value[0], value[1])
            patch.start(); self.addCleanup(patch.stop)
        from fleet import session_tags
        patch = mock.patch.object(session_tags, 'assigned_tag', lambda harness, sid: 'e1')
        patch.start(); self.addCleanup(patch.stop)
        self.binding = {'harness': 'claude', 'sid': SID}

    def popup(self, **extra):
        request = {'cwd': str(self.project), 'routes': [], **extra}
        began = time.monotonic()
        result = self.m._status_popup(self.binding, request, 100, 256, True)
        return result, time.monotonic() - began

    @staticmethod
    def text(result):
        return '\n'.join(''.join(run[0] for run in row) for row in result.get('rows', []))

    def button(self):
        value = self.status.read(str(self.project), [])
        self.reads = 0
        return value

    def test_the_value_is_the_newest_document_of_the_root(self):
        value = self.button()
        self.assertTrue(value['has_doc'], value)
        self.assertEqual(value['path'], str(self.document.resolve()))

    def test_document_rows_come_at_once_and_wait_on_nothing(self):
        """The popup's document part: under a second, with no collection and no second read of the root."""
        value = self.button()
        self.vm_delay = 1.5           # Fleet's collection is slow
        result, seconds = self.popup(status=value, part='doc')
        print(json.dumps({'event': 'status-popup-fast', 'case': 'doc-part', 'campaigns': CAMPAIGNS,
                          'seconds': round(seconds, 3), 'reads': self.reads, 'collections': self.vms}), flush=True)
        self.assertTrue(result['ok'], result)
        self.assertIn('NEWEST_DOC_LINE', self.text(result))
        self.assertLess(seconds, 1.0)
        self.assertEqual(self.vms, 0)
        self.assertEqual(self.reads, 0)

    def test_the_popup_without_a_held_value_reads_once(self):
        result, _seconds = self.popup(part='doc')
        self.assertTrue(result['ok'])
        self.assertEqual(self.reads, 1)
        self.assertIn('NEWEST_DOC_LINE', self.text(result))

    def test_fleet_part_is_apart_and_its_failure_leaves_the_document(self):
        value = self.button()
        fleet, _ = self.popup(status=value, part='fleet')
        self.assertTrue(fleet['ok'])
        self.assertIn('FLEET_STATUS_LINE', self.text(fleet))
        self.assertNotIn('title', fleet)
        self.assertEqual(self.reads, 0)           # Fleet's part has no use for the document's value
        self.vm_fail = True
        failed, _ = self.popup(status=value, part='fleet')
        self.assertFalse(failed['ok'])
        doc, _ = self.popup(status=value, part='doc')
        self.assertTrue(doc['ok'])
        self.assertIn('NEWEST_DOC_LINE', self.text(doc))

    def test_a_document_that_cannot_be_read_answers_failure(self):
        """``read_text`` giving nothing (an I/O error, a changed document): the doc part says so; no `?` row."""
        value = self.button()
        with mock.patch.object(self.status, 'read_text', return_value=(None, False)):
            result, _ = self.popup(status=value, part='doc')
        self.assertEqual(result, {'ok': False, 'reason': 'doc-unreadable'})
        again, _ = self.popup(status=value, part='doc')       # not kept: the next opening reads it
        self.assertTrue(again['ok'])
        self.assertIn('NEWEST_DOC_LINE', self.text(again))

    def test_the_parts_make_the_whole(self):
        value = self.button()
        whole, _ = self.popup(status=value)
        head, _ = self.popup(status=value, part='fleet')
        doc, _ = self.popup(status=value, part='doc')
        self.assertEqual(self.text(whole), self.text(head) + ('\n' if self.text(head) else '') + self.text(doc))
        self.assertEqual(doc['title'], whole['title'])

    def test_a_claim_names_no_place_outside_the_root(self):
        value = self.button()
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir(exist_ok=True)
        (outside / '현황.md').write_text('# OUTSIDE_SECRET\n')
        (self.project / 'notes.txt').write_text('NOT_A_STATUS_DOCUMENT')
        for path in (str(outside / '현황.md'), str(self.project / 'notes.txt'), '현황.md', value['path'] + '\0'):
            self.assertIsNone(self.status.held(str(self.project), dict(value, path=path)), path)
        self.assertIsNotNone(self.status.held(str(self.project), value))
        result, _ = self.popup(status=dict(value, path=str(outside / '현황.md')), part='doc')
        self.assertNotIn('OUTSIDE_SECRET', self.text(result))
        self.assertEqual(self.reads, 1)           # it read the root's own value instead

    def test_a_claim_on_a_campaign_page_is_held_like_the_older_names(self):
        """The button's value naming a campaign's ``campaign.md`` is taken as it is (the popup reads no more)."""
        value = self.button()
        claim = dict(value, path=str(Path(value['path']).parent / 'campaign.md'))
        held = self.status.held(str(self.project), claim)
        self.assertIsNotNone(held)
        self.assertEqual(held['path'], claim['path'])

    def test_a_request_that_names_no_part_is_refused(self):
        result, _ = self.popup(part='everything')
        self.assertEqual(result, {'ok': False, 'reason': 'invalid-status'})


# -- Part B: the real host, a private PTY -------------------------------------------------------------

FAKE_NATIVE = r"""
import json,os,socket,sys,time,tty
harness,sid=sys.argv[1],sys.argv[2]
raw=open('/proc/self/stat').read();start=raw.rsplit(') ',1)[1].split()[19]
req={'op':'event','event':'start','harness':harness,'sid':sid,'pid':os.getpid(),'start':start,
     'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
with socket.socket(socket.AF_UNIX) as c:
    c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET']);c.sendall((json.dumps(req)+'\n').encode());c.recv(8192)
tty.setraw(0)
print('\x1b[2J\x1b[HREADY_'+harness+'\r\n❯ \r\n',end='',flush=True)
while True:
    data=os.read(0,65536)
    if not data:break
    print('\x1b[2J\x1b[HREADY_'+harness+'\r\n❯ '+data.decode(errors='replace')[-40:]+'\r\n',end='',flush=True)
"""

LAUNCHER = r"""
const fs=require('node:fs');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
if(process.argv.some(arg=>arg.endsWith('/session-shell/host.cjs'))){
 process.env.HEARTING_SHELL_PYTHON=cfg.backend;
 process.on('uncaughtException',error=>fs.appendFileSync(cfg.diagnostic,String(error?.stack||error)+'\n'));
}
"""

# The backend as installed. What a real one would read from this machine is a fixture (Fleet's collection, the
# questions, the requests); the status value is the real one over the case's synthetic root. `control/` holds
# the case's switches (a file's presence, or its number), `log` what each read did and when.
BACKEND = r"""#!{python}
import json,os,sys,time
sys.path.insert(0,{utilities!r});sys.path.insert(0,{tools!r})
import session_shell as m
from fleet import session_tags, session_status
OWN={sid!r}
session_tags.assigned_tag=lambda harness,sid:'e1' if sid==OWN else None
C={control!r}
def flag(name):return os.path.exists(C+'/'+name)
def number(name):
    try:return float(open(C+'/'+name).read())
    except (OSError,ValueError):return 0.0
def note(kind):
    with open(C+'/log','a') as f:f.write(json.dumps([kind,os.getpid(),time.time()])+'\n')
def vm(harness,sid,kind):
    note('vm')
    time.sleep(number('vm-delay'))
    if flag('vm-fail'):raise RuntimeError('fleet collection failed')
    from fleet.explorer.view_model import ExplorerViewModel
    return ExplorerViewModel.from_mapping(json.load(open({vm!r})))
m._popup_view_model=vm
m.session_questions=lambda harness,sid,**_kw:[]
m._request_views=lambda harness,sid,now,errors=None:([],[])
real_read=session_status.read
def read(cwd,routes=(),now=None):
    note('read-start')
    time.sleep(number('read-delay'))
    if flag('read-crash'):
        note('read-end');os._exit(3)
    try:return real_read(cwd,routes,now)
    finally:note('read-end')
session_status.read=read
real_text=session_status.read_text
def text(path):
    if flag('doc-fail'):return (None, False)
    return real_text(path)
session_status.read_text=text
# (11-26, panel B: with `messages` in the control folder this session has sent one message, so the panel's MESSAGES part stands)
real_sent=m._sent_peer_messages
def sent(sid):
    if flag('messages'):
        return ([{{'to':{{'harness':'codex','session_id':'peer-x','name':'peer'}},'kind':'report','summary':'BL_MSG_ONE 같이 볼 일',
                  'at':time.time()-60,'status':'received','message_id':'m-x'}}],[])
    return real_sent(sid)
m._sent_peer_messages=sent
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
const screen=async()=>{await flush();return Array.from({length:rows},(_,y)=>cellsOf(y).join('').trimEnd());};
const at=async needle=>{await flush();for(let y=0;y<rows;y++){const c=cellsOf(y),where=[];let text='';
  c.forEach((ch,x)=>{for(const _ of ch)where.push(x);text+=ch;});const m=needle instanceof RegExp?needle.exec(text):null,i=needle instanceof RegExp?(m?m.index:-1):text.indexOf(needle);if(i>=0)return {x:where[i],y};}return null;};
const until=async(test,what,ms=20000)=>{const began=Date.now();while(Date.now()-began<ms){const s=await screen();if(test(s))return {s,ms:Date.now()-began};await sleep(25);}
  throw Error('never '+what+'\n'+(await screen()).join('\n'));};
const has=word=>s=>s.some(line=>word instanceof RegExp?word.test(line):line.includes(word));
// 11-21: the STATUS head stands under the REPO group (` STATUS +N        ›`), the panel's only drawing of the status.
const hasButton=s=>s.some(line=>/STATUS( \+?\d+)? +›/.test(line));   // (11-26, panel B: beside the native area the head says the whole count, `STATUS 5 ›`)
const press=async(x,y,ms=100)=>{child.write(`\x1b[<0;${x+1};${y+1}M\x1b[<0;${x+1};${y+1}m`);await sleep(ms);};
const key=async(bytes,ms=250)=>{child.write(bytes);await sleep(ms);};
const box=s=>{const top=s.findIndex(l=>l.includes('╭─')&&l.includes('✕ ─╮'));if(top<0)return null;
  const left=[...s[top]].indexOf('╭'),bottom=s.findIndex((l,i)=>i>top&&[...l][left]==='╰'&&l.includes('╯'));
  return {top,bottom,left,rows:s.slice(top,bottom+1).map(l=>l.slice(left))};};
const popupHas=word=>s=>{const b=box(s);return !!b&&b.rows.some(l=>l.includes(word));};
const place=s=>{const m=(box(s)?box(s).rows.join('\n'):'').match(/(\d+)\/(\d+) · /);return m?[+m[1],+m[2]]:null;};
const body=s=>{const b=box(s);return b?b.rows.slice(1,-1).map(l=>l.replace(/^│\s?/,'').replace(/\s*│.*$/,'').trim()):[];};
const log=()=>{try{return fs.readFileSync(cfg.control+'/log','utf8').split('\n').filter(Boolean).map(l=>JSON.parse(l));}catch{return [];}};
const count=kind=>log().filter(e=>e[0]===kind).length;
const control=(name,value='')=>fs.writeFileSync(cfg.control+'/'+name,String(value));
const drop=name=>{try{fs.unlinkSync(cfg.control+'/'+name);}catch{}};
const write=(file,value)=>{fs.writeFileSync(file+'.new',value,{mode:0o600});fs.renameSync(file+'.new',file);};
const rename=async title=>{const cache=JSON.parse(fs.readFileSync(cfg.cache,'utf8'));cache.observed_at=Date.now()/1000;
  cache.route_panels[cfg.key][0].title=title;write(cfg.cache,JSON.stringify(cache));};
// 11-21: the press that opens the popup stands on the STATUS head under the REPO group.
const opened=async()=>{const spot=await at('STATUS');await press(spot.x+2,spot.y,0);};
const close=async()=>{await key('\x1b',300);await until(s=>!box(s),'closed');};
const edit=(from,to)=>fs.writeFileSync(cfg.doc,fs.readFileSync(cfg.doc,'utf8').replace(from,to));
const checks={};
(async()=>{try{
  if(cfg.scenario==='open'){
    // The button's read is slow (a scan of a slow disk); Fleet's collection is slower still.
    control('read-delay',1.5);control('vm-delay',4);
    // The button stands once a read has ended; wait for that (no read under way), not a fixed time.
    await until(hasButton,'the button');await until(()=>count('read-start')>=1&&count('read-end')>=count('read-start'),'the first read ended');
    const r0=count('read-start'),v0=count('vm');
    // 100 paints: no read. The count is taken once the typed letters stand on the panel (the paints reached it).
    for(let i=0;i<100;i++){child.write('a');await sleep(10);}
    await until(has(/❯ a+/),'the paints on the panel');checks.reads_after_paints=count('read-start')-r0;
    // The press: the document stands under a second; Fleet's lines are not there yet.
    const began=Date.now();await opened();
    let r=await until(popupHas('NEWEST_DOC_LINE'),'the document in the popup');
    checks.doc_ms=Date.now()-began;checks.fleet_early=popupHas('FLEET_STATUS_LINE')(r.s);
    checks.popup_reads=count('read-start')-r0;
    // Closed and opened again while Fleet's collection is still out: the document stands under a second
    // (the document part lets the table's place go when it ends) and the collection is not asked again.
    const r2=count('read-start');
    await until(()=>count('vm')>=v0+1,'Fleet\'s collection started');      // its start is in the log before the count
    const v3=count('vm');await close();
    const reopened=Date.now();await opened();
    await until(popupHas('NEWEST_DOC_LINE'),'the document on reopening');checks.reopen_doc_ms=Date.now()-reopened;
    checks.reopen_doc_reads=count('read-start')-r2;checks.reopen_doc_vm=count('vm')-v3;
    // Reading on while Fleet's part is out: the line at the top stays when Fleet's lines come in above it.
    await key('\x1b[6~',300);r=await until(s=>!!place(s),'the place');
    const topBefore=body(await screen())[0],placeBefore=place(await screen());
    r=await until(s=>{const p=place(s);return p&&placeBefore&&p[1]>placeBefore[1];},'Fleet\'s lines arrived',15000);
    checks.fleet_ms=Date.now()-began;checks.vm_started=count('vm')-v0;checks.top_before=topBefore;checks.top_after=body(r.s)[0];
    checks.place=[placeBefore,place(r.s)];
    await key('\x1b[5~',300);await key('\x1b[5~',300);
    checks.head_visible=popupHas('FLEET_STATUS_LINE')(await screen());
    checks.first_body=body(await screen())[0];
    await close();
    // Closed and opened again at once: the rows read last, no read.
    const r1=count('read-start'),v1=count('vm');await opened();
    await until(popupHas('FLEET_STATUS_LINE'),'the rows read last');await sleep(300);
    checks.reopen_reads=count('read-start')-r1;checks.reopen_vm=count('vm')-v1;
    await close();
    // Fleet's collection fails: the document stands, no lines above it; not kept, so the next opening reads again.
    control('vm-delay',0);control('vm-fail');
    edit('- 다음 일','- CHANGED_A 바뀐 줄');await sleep(2500);       // the watch event: the value is read again
    const v2=count('vm');await opened();
    await until(popupHas('CHANGED_A'),'the changed document');await sleep(500);
    checks.fail_head=popupHas('FLEET_STATUS_LINE')(await screen());checks.fail_vm=count('vm')-v2;
    await close();drop('vm-fail');
    await opened();r=await until(popupHas('FLEET_STATUS_LINE'),'Fleet\'s lines on the next opening');
    checks.next_vm=count('vm')-v2;
    await close();
    // The document's read fails: Fleet's word, dim, never an empty popup; the next opening reads it.
    edit('CHANGED_A','CHANGED_B');await sleep(2500);
    control('doc-fail');await opened();
    r=await until(popupHas(cfg.unreadable),'the unreadable word');checks.unread_frame=box(r.s).rows;
    await close();drop('doc-fail');await opened();
    r=await until(popupHas('CHANGED_B'),'the document on the next opening');checks.after_unread=true;
    await close();
  }
  if(cfg.scenario==='crash'){
    // The button's first read dies (a worker that exits): no button, then the next projection event asks again.
    control('read-delay',1.2);control('read-crash');
    await until(has('READY_'+cfg.harness),'the panel');
    // The first read's own end in the log (its worker exits right after it), not a fixed wait.
    await until(()=>count('read-end')>=1,'the first read ended');
    checks.button_after_crash=hasButton(await screen());checks.reads_failed=count('read-end');
    // Events while a read is under way: it asks again once it ends, never beside it.
    const started=count('read-start'),ended=count('read-end');
    for(const title of ['표 하나','표 둘','표 셋']){await rename(title);await until(has(title),'the route renamed '+title);await sleep(250);}
    // One of those events asked again, and that read ends in the log: wait for it, not a fixed wait.
    await until(()=>count('read-start')>started||count('read-end')>ended,'the events asked again');
    await until(()=>count('read-end')>=count('read-start'),'the read after the events ended');
    checks.reads_failing=count('read-start');
    const events=log().filter(e=>e[0]==='read-start'||e[0]==='read-end').sort((a,b)=>a[2]-b[2]);
    let live=0,most=0;for(const e of events){live+=e[0]==='read-start'?1:-1;most=Math.max(most,live);}
    checks.most_at_once=most;
    // The disk answers again: the next event gives the button.
    drop('read-crash');drop('read-delay');await rename('表 넷');
    await until(hasButton,'the button after the read works again');checks.button_after=true;
    // That read's end is in the log before the quiet stretch is counted (no read is left under way).
    await until(()=>count('read-end')>=count('read-start'),'the button read ended');
    const r0=count('read-start');await sleep(800);checks.reads_when_quiet=count('read-start')-r0;
  }
  if(cfg.scenario==='width'){
    // Fleet's lines are slow and the window narrows while they are out: rows laid out at the old width are
    // not kept as the new width's; the popup opened again stands with its document, then the new width's lines.
    control('read-delay',0);control('vm-delay',3);
    await until(hasButton,'the button');await sleep(300);
    await opened();await until(popupHas('NEWEST_DOC_LINE'),'the document');
    const v0=count('vm');
    cols=110;term.resize(cols,rows);child.resize(cols,rows);await sleep(300);
    // The old width's collection comes back and is not this width's: the popup asks once more for the new width.
    await until(()=>count('vm')>=v0+2,'the new width asked',15000);
    await close();
    const began=Date.now();await opened();
    await until(popupHas('NEWEST_DOC_LINE'),'the document on reopening');await sleep(600);
    checks.width_doc_ms=Date.now()-began;
    checks.width_old_lines=popupHas('W01')(await screen());     // the old width's rows must not stand
    await until(popupHas('W01'),'the new width\'s lines',15000);checks.width_new_lines=true;
    const v1=count('vm');await close();await opened();
    await until(popupHas('W01'),'the rows read last at this width');
    checks.width_reopen_vm=count('vm')-v1;
    await close();
  }
  if(cfg.scenario==='backlog'){
    // 11-21: the BACKLOG part under the REPO group (its head and its first three items, `+N` for the rest); a press on an item opens the BACKLOG popup
    // standing on it; the group of this window's campaign is open on top and the others are folded; an item with detail opens under its row.
    // 11-26 (panel B): the items are the ones the person ranked (`priority:`), three at most; the head `BACKLOG +N · Discuss n ›`.
    await until(s=>s.some(l=>/BACKLOG \+\d+ · Discuss \d+ +›/.test(l)),'the BACKLOG head');
    const panel=await screen();
    checks.head=panel.find(l=>/BACKLOG \+/.test(l));checks.items=panel.filter(l=>/BL_ITEM|BL_OTHER_ITEM/.test(l)).length;
    checks.ranked=panel.filter(l=>/BL_ITEM/.test(l)).map(l=>(/BL_ITEM_[A-Z]{3}/.exec(l)||[''])[0]);   // (a title is cut at the panel's width)
    checks.status_head=panel.some(l=>/STATUS( \+?\d+)? +›/.test(l));
    const spot=await at('BL_ITEM_TWO');await press(spot.x,spot.y,0);
    let r=await until(popupHas('BL_ITEM_ONE'),'the BACKLOG popup');
    const b=box(r.s);checks.title=b.rows[0];checks.band=b.rows.at(-1);
    checks.stands=body(r.s).some(l=>l.startsWith('> ')&&l.includes('BL_ITEM_TWO'));
    checks.other_folded=body(r.s).some(l=>l.includes('›')&&l.includes('TBD 2'));
    checks.other_hidden=!popupHas('BL_OTHER_ITEM')(r.s);
    await key('\x1b[A',250);await key('\r',400);r=await screen();
    checks.detail=popupHas('BL_DETAIL_NOTE')(r);
    await key('\r',300);r=await screen();checks.detail_closed=!popupHas('BL_DETAIL_NOTE')(r);
    for(let i=0;i<4;i++)await key('\x1b[B',200);await key('\r',400);r=await screen();
    checks.group_opened=popupHas('BL_OTHER_ITEM')(r);
    // PgDn/PgUp move the chosen line (the window follows): to the end of what there is, and back to the first line (11-26: the board's first group head).
    await key('\x1b[6~',300);r=await screen();checks.pgdn_end=body(r).some(l=>l.startsWith('> ')&&l.includes('full document'));
    await key('\x1b[5~',300);r=await screen();checks.pgup_top=body(r).some(l=>l.startsWith('> ')&&l.includes('FIX 4'));
    // 11-26 (scheme A): the board's own keys, typed as a person types them. `t` shows the Discuss items only, and Esc undoes that, not the popup.
    const chosen=s=>body(s).find(l=>l.startsWith('> '))||'';
    await key('t',400);r=await screen();
    checks.discuss=popupHas('state Discuss')(r)&&popupHas('showing 1 of 6')(r)&&body(r).some(l=>/Discuss +BL_ITEM_ONE/.test(l))&&!popupHas('BL_ITEM_TWO')(r);
    await key('\x1b',500);r=await screen();checks.esc_filter=!!box(r)&&popupHas('showing 6 ·')(r)&&!popupHas('state Discuss')(r);
    // `a`, `d`, `k`: grouped by area (case and blanks folded: `Panel` and `panel` are one group), by date, and by kind again.
    await key('a',400);r=await screen();checks.area=popupHas('[Area]')(r)&&popupHas('Panel 2')(r)&&popupHas('No area 4')(r);
    await key('d',400);r=await screen();checks.date=popupHas('[Date]')(r)&&popupHas('2026-11-20 1')(r)&&popupHas('No date 5')(r);
    await key('k',400);r=await screen();checks.kind=popupHas('[Kind]')(r)&&popupHas('FIX 4')(r);
    // `/` finds a title: the keys typed are the finder's (the `t` and `e` of "three" neither filter nor do anything else); Korean is typed whole.
    await key('/',300);for(const c of 'three')await key(c,120);r=await screen();checks.typing=popupHas('/ Title: three')(r);
    await key('\r',400);r=await screen();checks.find=popupHas('find Title "three"')(r)&&/BL_ITEM_THREE/.test(chosen(r))&&!popupHas('BL_ITEM_TWO')(r)&&!popupHas('state Discuss')(r);
    await key('\x1b',500);await key('/',300);await key('셋째',300);await key('\r',400);r=await screen();
    checks.find_korean=popupHas('find Title "셋째"')(r)&&/BL_ITEM_THREE/.test(chosen(r));
    // `/` then Tab finds an area's name.
    await key('\x1b',500);await key('/',300);await key('\t',200);for(const c of 'PANEL')await key(c,120);await key('\r',400);r=await screen();
    checks.find_area=popupHas('find Area "PANEL"')(r)&&popupHas('BL_ITEM_ONE')(r)&&popupHas('BL_ITEM_TWO')(r)&&!popupHas('BL_ITEM_THREE')(r);
    // Home and End: the first and the last item of the view (End opens the folded TBD group the last item is in).
    await key('\x1b',500);await key('\x1b[F',400);r=await screen();checks.end=/BL_LOOSE/.test(chosen(r));
    await key('\x1b[H',400);r=await screen();checks.home=/BL_ITEM_ONE/.test(chosen(r));
    await key('\x1b[C',400);r=await until(s=>{const x=box(s);return !!x&&x.rows[0].includes('ROUTE');},'the next section after BACKLOG');checks.next_section=true;
    await key('\x1b[D',400);r=await until(s=>{const x=box(s);return !!x&&x.rows[0].includes('BACKLOG');},'BACKLOG again by ←');checks.back=true;
    await close();
    // 11-26 (panel B): the presses of the new rows, in the real host. The BACKLOG head opens the board; the MESSAGES head (a message is one row,
    // its time at the right end) opens the MESSAGES popup; the STATUS head is one row and opens the status popup (`opened`, the other scenarios).
    {const spot=await at(/BACKLOG \+\d+/);await press(spot.x+2,spot.y,0);r=await until(popupHas('BL_ITEM_ONE'),'the board from the BACKLOG head');
     checks.backlog_head=box(r.s).rows[0].includes('BACKLOG');await close();}
    {const s=await screen();checks.message_row=s.find(l=>l.includes('BL_MSG_ONE'))||'';}
    {const spot=await at(/MESSAGES( \+\d+)? +›/);await press(spot.x+2,spot.y,0);r=await until(s=>{const x=box(s);return !!x&&x.rows[0].includes('MESSAGES');},'the MESSAGES popup from its head');
     checks.messages_head=popupHas('BL_MSG_ONE')(r.s);await close();}
    // Short of rows the two heads fold into the REPO row's buttons: the last `›` opens the board, the row's first cells the status popup.
    rows=16;term.resize(cols,rows);child.resize(cols,rows);
    const folded=/REPO .*›  (BACKLOG[^│]*)?› │/;   // (`STATUS +5 ›  BACKLOG +3 ›`, `STATUS ›  BACKLOG ›` or `›  ›`, as the row's own value leaves room)
    r=await until(s=>s.some(l=>folded.test(l)),'the buttons on the REPO row');
    {const y=r.s.findIndex(l=>folded.test(l)),line=r.s[y];checks.folded_row=line.slice(line.lastIndexOf('│',line.indexOf('REPO'))).trim();
     // (by cells, the folder's name and its Git words being the machine's: the last `›` of the row is BACKLOG's button, its label `REPO` lies in STATUS's press)
     await flush();const c=cellsOf(y),button={x:c.lastIndexOf('›'),y},name=await at(/REPO +/);
     await press(button.x,button.y,0);let o=await until(s=>{const x=box(s);return !!x&&x.rows[0].includes('BACKLOG')&&popupHas('6 total')(s);},'the board from the folded button');
     checks.folded_backlog=true;await close();   // (16 rows: the board's head lines; its items are below its short window)
     await press(name.x+1,name.y,0);o=await until(s=>{const x=box(s);return !!x&&x.rows[0].includes('STATUS');},'the status popup from the REPO row');
     checks.folded_status=true;await close();}
    rows=40;term.resize(cols,rows);child.resize(cols,rows);await sleep(300);
  }
  if(cfg.scenario==='mem'){
    // 11-20: the MEM section's records are one row each; a press on a record opens the MEM popup (the MESSAGES popup's frame and keys) on it.
    await until(has(/│ MEM +› │/),'the MEM section');
    const panel=await screen();
    checks.rows=panel.filter(l=>l.includes('MEMREC')).length;
    checks.row_cut=panel.some(l=>l.includes('MEMREC_ONE')&&l.includes('…'))&&!panel.some(l=>l.includes('MEMREC_ONE_END'));
    const spot=await at('MEMREC_TWO');await press(spot.x,spot.y,0);
    let r=await until(popupHas('MEM'),'the MEM popup');
    const b=box(r.s);checks.title=b.rows[0];checks.foot=b.rows.at(-1);
    checks.whole=popupHas('MEMREC_ONE_END')(r.s);checks.second=popupHas('MEMREC_TWO second record')(r.s);
    // (Both records fit the popup, so it stands at its top: the first row is the newest record's head.)
    checks.first_row=body(r.s)[0];
    await close();
    // The head opens it too; ←→ walk the sections (MEM is the last).
    const head=await at(/MEM +› │/);await press(head.x+2,head.y,0);
    r=await until(popupHas('MEMREC_ONE_END'),'the MEM popup from its head');checks.head_first=body(r.s)[0];
    await key('\x1b[D',300);r=await until(s=>{const b=box(s);return !!b&&b.rows[0].includes('MESSAGES')||!!b&&!b.rows[0].includes('MEM');},'a section before MEM');
    await key('\x1b[C',300);r=await until(popupHas('MEMREC_ONE_END'),'MEM again by →');checks.walk=true;
    await close();
  }
  fs.writeFileSync(cfg.result,JSON.stringify({checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('\x15');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""


def route_row(rid, title):
    return {'id': rid, 'title': title, 'state': 'working', 'worker_stage': 'one-shot', 'shape': 'one-shot-owner',
            'stages': [{'name': 'one-shot', 'st': 'active'}], 'harness': 'claude', 'model': 'Opus 5.5', 'effort': 'high'}


class PtyTest(unittest.TestCase):
    """Part B: the real host over a synthetic root."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        if not cls.node or not shutil.which('npm'):
            raise unittest.SkipTest('native PTY dependency builder needs node/npm')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hsfp-', dir='/tmp')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        spec = importlib.util.spec_from_file_location('shell_status_fast_isolation', ROOT / 'tools/run-tests.py')
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

    def run_case(self, scenario):
        case = self.root / scenario; case.mkdir(mode=0o700)
        control = case / 'control'; control.mkdir()
        project, doc = make_root(case / 'tree')
        key = 'claude\0' + SID
        target = Path(self.env['XDG_STATE_HOME']) / 'hearting/human-waits/projection.json'
        value = json.loads(target.read_text()) if target.exists() else {
            'schema_version': 3, 'items': [], 'by_session': {}, 'total': 0, 'older': 0, 'complete': True,
            'route_panels': {}, 'route_status': {}, 'route_sessions': {}}
        value.update(observed_at=time.time())
        value['route_panels'][key] = [route_row('rt-b000000000000001', '현황 팝업 구현')]
        value['route_status'][key] = 'route 1 ⠏1'; value['route_sessions'][key] = time.time() + 3600
        target.write_text(json.dumps(value)); target.chmod(0o600)
        vm = case / 'vm.json'
        # The width case's line is long enough to be clipped at the popup's width: its tail tells the width it was read at.
        line = ' '.join('W%02d' % n for n in range(1, 41)) if scenario == 'width' else 'FLEET_STATUS_LINE'
        vm.write_text(json.dumps({'repos': [{'id': 'proj', 'name': 'proj', 'status': {
            'routes': 1, 'questions': 0, 'campaigns': [{'campaign': 'camp-000', 'line': line}], 'next': '다음 일'}}],
            'system': {}, 'routes': [], 'sessions': [{'tag': 'e1', 'repo_id': 'proj', 'harness': 'claude', 'state': 'idle',
                                                      'title': '이 창'}]}, ensure_ascii=False))
        if scenario == 'mem':
            # 11-20: two saved memory records (global, so any folder reads them) in the case's own journal; the MEM section stands
            # from them, and its popup reads each whole (the snippet is the body the journal keeps).
            journal = Path(self.env['XDG_STATE_HOME']) / 'agent-memory/write-events.jsonl'
            journal.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - 300))
            journal.write_text('\n'.join(json.dumps({'ts': stamp, 'action': 'add', 'type': 'decision', 'tier': 'durable', 'scope': 'global',
                                                    'id': 'mem-%d' % n, 'snippet': snippet}) for n, snippet in enumerate((
                'MEMREC_ONE first record body line, which goes on and on so that the one row cuts it short of its end MEMREC_ONE_END',
                'MEMREC_TWO second record')) ) + '\n')
        if scenario == 'backlog':
            (control / 'messages').write_text('')
            # 11-21: a backlog document in the artifact root: this window's campaign group (the newest document's folder) first, another group folded.
            (case / 'tree' / '.agent_reports' / 'backlog.md').write_text(
                '# 시험용 백로그\n갱신 2026-11-21 11:02\n\n## 2026-02-11_camp-001\n- BL_OTHER_ITEM 다른 캠페인의 일\n\n'
                '## 2026-01-10_camp-000\n- BL_ITEM_ONE 첫째 일\n  kind: fix\n  date: 2026-11-20\n  state: discuss\n  area: Panel\n  priority: 2\n  note: BL_DETAIL_NOTE 맥락\n'
                '- BL_ITEM_TWO 둘째 일\n  kind: Fix\n  area:  panel \n  priority: 1\n- BL_ITEM_THREE 셋째 일\n  kind: fix\n  priority: 3\n- BL_ITEM_FOUR 넷째 일\n  kind: fix\n  priority: 4\n\n'
                '## Unassigned\n- BL_LOOSE 캠페인 없는 일\n', encoding='utf-8')
        backend = case / 'backend'
        backend.write_text(BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'), tools=str(ROOT / 'tools'),
                                          sid=SID, vm=str(vm), control=str(control)))
        backend.chmod(0o700)
        config = {'harness': 'claude', 'sid': SID, 'key': key, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'cache': str(target),
                  'project': str(project), 'result': str(case / 'result.json'), 'control': str(control),
                  'doc': str(doc), 'scenario': scenario, 'unreadable': self.unreadable}
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
        return json.loads((case / 'result.json').read_text())['checks']

    def test_the_popup_stands_at_once_and_is_never_empty(self):
        checks = self.run_case('open')
        print(json.dumps({'event': 'status-popup-fast-pty', 'campaigns': CAMPAIGNS, 'doc_ms': checks['doc_ms'],
                          'fleet_ms': checks['fleet_ms']}), flush=True)
        self.assertEqual(checks['reads_after_paints'], 0, checks)
        # The document under a second though the button's read takes 1.5 s and Fleet's collection 4 s;
        # Fleet's lines are not there yet, and the popup read nothing again.
        self.assertLess(checks['doc_ms'], 1000, checks)
        self.assertFalse(checks['fleet_early'], checks)
        self.assertEqual(checks['popup_reads'], 0, checks)
        self.assertEqual(checks['vm_started'], 1, checks)
        # Closed and opened again while Fleet's collection was still out: the document stood under a second,
        # and that collection was not asked again.
        self.assertLess(checks['reopen_doc_ms'], 1000, checks)
        self.assertEqual((checks['reopen_doc_reads'], checks['reopen_doc_vm']), (0, 0), checks)
        # Fleet's lines come in above it, and the line being read stays at the top of the popup.
        self.assertGreater(checks['fleet_ms'], 3000, checks)
        self.assertEqual(checks['top_before'], checks['top_after'], checks)
        self.assertGreater(checks['place'][1][1], checks['place'][0][1], checks)
        self.assertTrue(checks['head_visible'], checks)
        # Opened again at once: the rows read last, no read.
        self.assertEqual((checks['reopen_reads'], checks['reopen_vm']), (0, 0), checks)
        # Fleet's collection failed: the document stands, no lines above it, and the next opening reads again.
        self.assertFalse(checks['fail_head'], checks)
        self.assertEqual(checks['fail_vm'], 1, checks)
        self.assertEqual(checks['next_vm'], 2, checks)
        # The document's read failed: Fleet's word, dim, in the popup; the next opening reads it.
        self.assertTrue(any(self.unreadable in row for row in checks['unread_frame']), checks['unread_frame'])
        self.assertTrue(checks['after_unread'])

    def test_a_memory_record_is_one_row_and_a_press_opens_the_mem_popup_on_it(self):
        checks = self.run_case('mem')
        self.assertEqual(checks['rows'], 2, checks)                      # one row a record
        self.assertTrue(checks['row_cut'], checks)                        # the long record's row ends in `…`, its end is not there
        self.assertIn('MEM', checks['title'], checks)
        self.assertTrue(checks['whole'] and checks['second'], checks)     # the popup reads the record whole
        self.assertIn('◆ global · decision', checks['first_row'], checks)  # each record's head: the mark, its scope, its kind and its age
        self.assertIn('◆ global · decision', checks['head_first'], checks)
        self.assertTrue(checks['walk'], checks)

    def test_the_backlog_part_lists_three_items_and_its_popup_opens_on_the_one_pressed(self):
        checks = self.run_case('backlog')
        # 11-26 (panel B): the three the person ranked first, in that order (the fourth ranked and the unranked ones are `+N`); Discuss counts the document's.
        self.assertEqual(checks['items'], 3, checks)
        self.assertEqual(checks['ranked'], ['BL_ITEM_TWO', 'BL_ITEM_ONE', 'BL_ITEM_THR'], checks)
        self.assertRegex(checks['head'], r'BACKLOG \+3 · Discuss 1 +›', checks)        # six items in the document, three shown
        self.assertTrue(checks['status_head'], checks)
        self.assertIn('BACKLOG', checks['title'], checks)
        self.assertIn('╰─ STATUS · BACKLOG · ', checks['band'], checks)
        self.assertTrue(checks['stands'], checks)                                         # the popup stands on the item pressed
        self.assertTrue(checks['other_folded'] and checks['other_hidden'], checks)        # the FIX sort is open, the unsorted one folded
        self.assertTrue(checks['detail'] and checks['detail_closed'], checks)             # the indented lines under an item
        self.assertTrue(checks['group_opened'], checks)
        self.assertTrue(checks['pgdn_end'] and checks['pgup_top'], checks)                # PgDn/PgUp move the chosen line, not only the window
        self.assertTrue(checks['next_section'] and checks['back'], checks)
        # 11-26 (scheme A): the board's keys, in the real host over a pty
        for name in ('discuss', 'esc_filter', 'area', 'date', 'kind', 'typing', 'find', 'find_korean', 'find_area', 'end', 'home'):
            self.assertTrue(checks[name], (name, checks))
        # 11-26 (panel B): the BACKLOG head, the MESSAGES head (its message one row, the time at the right end) and the REPO row's folded buttons.
        for name in ('backlog_head', 'messages_head', 'folded_backlog', 'folded_status'):
            self.assertTrue(checks[name], (name, checks))
        self.assertRegex(checks['message_row'], r'│  → peer BL_MSG_ONE .* \d\d[:-]\d\d │', checks)
        self.assertRegex(checks['folded_row'], r'^│  REPO .*›  (BACKLOG[^│]*)?› │$', checks)

    def test_a_late_fleet_part_of_another_width_is_not_kept(self):
        checks = self.run_case('width')
        print(json.dumps({'event': 'status-popup-fast-width', 'campaigns': CAMPAIGNS,
                          'doc_ms': checks['width_doc_ms'], 'old_lines': checks['width_old_lines']}), flush=True)
        # Opened again at the new width before its own lines are in: the old width's rows do not stand.
        self.assertFalse(checks['width_old_lines'], checks)
        self.assertTrue(checks['width_new_lines'], checks)
        # The lines read at this width are kept: opened again, no collection.
        self.assertEqual(checks['width_reopen_vm'], 0, checks)

    def test_a_failed_first_read_is_asked_again_one_worker_at_a_time(self):
        checks = self.run_case('crash')
        self.assertFalse(checks['button_after_crash'], checks)
        self.assertGreaterEqual(checks['reads_failed'], 1, checks)
        self.assertEqual(checks['most_at_once'], 1, checks)
        # A failure is asked again at the events, and by nothing else: a quiet panel reads nothing.
        self.assertGreaterEqual(checks['reads_failing'], 2, checks)
        self.assertTrue(checks['button_after'], checks)
        self.assertEqual(checks['reads_when_quiet'], 0, checks)


if __name__ == '__main__':
    unittest.main()
