"""11-20: dragging over the native area of the shell's window selects text and copies it (OSC 52) again.

The shell asks its outer terminal to report the mouse whenever its panel stands, so the outer terminal no longer
selects text for a native that does not read the mouse itself. The shell now does it for that native: a drag over
the native area flips the cells it passes, the release writes the text to the outer terminal's clipboard (OSC 52)
from the shell's own copy of the screen, and the next input or screen change takes the flipped cells back. A native
that reads the mouse still gets every report; the panel's and the popups' presses and drags are untouched; a
clipboard write the native makes itself goes on to the outer terminal (a read never does).

Three layers: the text rules on a real headless terminal (wide characters, wrapped lines, blanks at a line's end,
a word, a line); the host's own mouse routing (the functions cut from `host.cjs`, with a real terminal and the
panel's and popups' neighbours replaced by recorders); the real host in a private PTY with a synthetic native, the
outer terminal being a headless one whose OSC 52 is recorded. `--frames` prints the frames the PTY runs read.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import base64
import importlib.util
import json
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
SHOW_FRAMES = False
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


POPUP = load("selection_popup_base", ROOT / "utilities/session_shell_popup.test.py")

LONG = " ".join("w%03d" % i for i in range(26))  # 129 cells: wraps in every layout below
# A sign-in address as a login screen shows it (fixture text, no account): once the terminal wraps it (URL_WRAPPED), and once
# the app itself cuts it into rows that each end by themselves (URL_SPLIT, cut after 90 characters).
URL_WRAPPED = ("https://claude.ai/oauth/authorize?code=true&client_id=FIXTURE-0000-0000&response_type=code&redirect_uri="
               "https%3A%2F%2Fexample.invalid%2Fcallback&scope=fixture%3Aone+fixture%3Atwo&state=abc123XYZ")
URL_SPLIT = URL_WRAPPED.replace("/oauth/", "/oauth-split/")
URL_CUT = 90

# A native that paints a screen of text (an ASCII row, a row with Hangul, a long row that wraps, a row with blanks
# after its text) and repaints only when something is typed. `osc` makes it write the clipboard itself, and ask to read it.
FAKE_NATIVE = r"""
import base64,json,os,select,socket,sys,time,tty
harness,sid=sys.argv[1],sys.argv[2]
LONG=%r
URL1=%r
URL2A=%r
URL2B=%r
def event(kind):
    raw=open('/proc/self/stat').read();start=raw.rsplit(') ',1)[1].split()[19]
    req={'op':'event','event':kind,'harness':harness,'sid':sid,'pid':os.getpid(),'start':start,
         'token':os.environ['HEARTING_SHELL_TOKEN'],'seq':time.monotonic_ns()//1000}
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(5);c.connect(os.environ['HEARTING_SHELL_SOCKET']);c.sendall((json.dumps(req)+'\n').encode());c.recv(8192)
tty.setraw(0)
event('start')
pending='';seen=[]
def screen():
    print('\x1b[2J\x1b[H'+'READY_'+harness+'\r\n'+'hello world path/to/file.txt\r\n'+'한글 가나다 mixed abc\r\n'+LONG+'\r\n'
          +'trail'+' '*12+'\r\n'+'last row\r\n'+URL1+'\r\n'+URL2A+'\r\n'+URL2B+'\r\n'+''.join(s+'\r\n' for s in seen[-3:])+'❯ '+pending.replace('\x1b','^')+'\r\n',end='',flush=True)
if harness!='claude':print('\x1b[?1000h\x1b[?1006h',end='',flush=True)
screen()
while True:
    data=os.read(0,65536)
    if not data:break
    text=data.decode(errors='replace')
    if '\x1b[<' in text:
        seen.append('NATIVE_ESC_'+text.replace('\x1b','^'));text=''.join(part.split('M',1)[-1].split('m',1)[-1] if part.startswith('[<') else part for part in text.split('\x1b'))
    pending+=text
    while '\r' in pending:
        line,pending=pending.split('\r',1)
        if line=='quit':raise SystemExit
        if line=='link':
            print('\x1b]8;;https://example.invalid/link\x1b\\LINKTEXT\x1b]8;;\x1b\\',end='',flush=True)
        if line=='osc':
            print('\x1b]52;c;'+base64.b64encode('from native 한글'.encode()).decode()+'\x07\x1b]52;c;?\x07\x1b]52;;bad base64!\x07',end='',flush=True)
        seen.append('DONE_'+line)
    screen()
""" % (LONG, URL_WRAPPED, URL_SPLIT[:URL_CUT], URL_SPLIT[URL_CUT:])

DRIVER = r"""
const fs=require('node:fs');
const pty=require('node-pty'),{Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const cfg=JSON.parse(fs.readFileSync(process.env.LOOP_CONFIG,'utf8'));
let cols=cfg.cols,rows=cfg.rows,raw='';
const term=new Terminal({cols,rows,allowProposedApi:true,scrollback:0});
term.loadAddon(new Unicode11Addon());term.unicode.activeVersion='11';
// What the outer terminal is asked to put on the clipboard (the outer terminal here is this headless one).
const osc=[],osc8=[];term.parser.registerOscHandler(52,d=>{osc.push(d);return true;});
term.parser.registerOscHandler(8,d=>{osc8.push(d);return true;});
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
// Cells drawn with inverse video: the set, and a frame that underlines the ones that are new since `base`.
const inverse=()=>{const out=new Set();for(let y=0;y<rows;y++){const line=term.buffer.active.getLine(y);for(let x=0;x<cols;x++){const c=line?.getCell(x);if(c&&c.isInverse())out.add(y+','+x);}}return out;};
const shot=async base=>{await flush();const s=await screen(),now=inverse(),out=[];
  s.forEach((text,y)=>{out.push(text);const c=cells(y);let marks='',any=false;
    for(let x=0;x<cols;x++){const on=now.has(y+','+x)&&!base.has(y+','+x);if(on)any=true;
      // a wide character's second cell has no text: keep the underline as wide as the character
      if(c[x]==='')continue;const wide=x+1<cols&&c[x+1]===''&&c[x]!=='';const next=wide&&now.has(y+','+(x+1))&&!base.has(y+','+(x+1));marks+=(on||next)?(wide?'^^':'^'):(wide?'  ':' ');}
    if(any)out.push(marks.trimEnd());});
  return out;};
const down=(p,code=0)=>child.write(`\x1b[<${code};${p.x+1};${p.y+1}M`);
const move=p=>child.write(`\x1b[<32;${p.x+1};${p.y+1}M`);
const up=p=>child.write(`\x1b[<0;${p.x+1};${p.y+1}m`);
const drag=async(from,to,via=[])=>{down(from);await sleep(60);for(const p of [...via,to]){move(p);await sleep(60);}await sleep(120);};
const finish=async to=>{up(to);await sleep(200);};
const click=async p=>{down(p);await sleep(30);up(p);await sleep(60);};
const texts=()=>osc.map(d=>d.startsWith('c;')?Buffer.from(d.slice(2),'base64').toString('utf8'):d);
const key=async(bytes,ms=250)=>{child.write(bytes);await sleep(ms);};
const frames={},checks={};
(async()=>{try{
  // The panel's first read of its facts may change its height (and with it the native area's rows): wait for it to settle.
  await until(s=>has('READY_'+cfg.harness)(s)&&has('last row')(s)&&has('⚙')(s),'the native screen and the panel');
  {let last='',since=Date.now();while(Date.now()-since<2000){const now=(await screen()).join('\n');if(now!==last){last=now;since=Date.now();}await sleep(100);}}
  const base=inverse();
  checks.popup0=has('✕ ─╮')(await screen());
  const w=await at('world'),f=await at('file'),han=await at('한글'),da=await at('다'),mixed=await at('mixed'),
        long=await at('w000'),trail=await at('trail'),path=await at('path/to');
  const end=(p,n)=>({x:p.x+n,y:p.y});
  if(cfg.scenario==='osc'){
    // The native writes the clipboard itself (and asks to read it, and writes a malformed one): what the outer terminal gets.
    await key('osc\r',500);checks.native_osc=texts();checks.native_osc_raw=osc.slice();
    await key('link\r',400);checks.osc8=osc8.slice();
    fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));return;
  }
  if(cfg.harness!=='claude'){
    // A native that reads the mouse: every report goes to it, nothing is selected or copied.
    await drag(w,end(f,3));await finish(end(f,3));
    const s=await screen();checks.native_esc=s.filter(l=>l.includes('NATIVE_ESC_')).length;
    checks.native_lines=s.filter(l=>l.includes('NATIVE_ESC_'));
    checks.osc=texts();checks.new_inverse=(await shot(base)).filter(l=>/^ *\^+$/.test(l)).length;
    frames.passthrough=await shot(base);
    fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));return;
  }
  // One row, up to the middle of a word: `world path/to/file`. Mid-drag the cells are flipped and nothing is copied yet.
  await drag(w,end(f,3));
  frames.drag=await shot(base);checks.during=texts();
  await finish(end(f,3));
  frames.done=await shot(base);checks.one_row=texts();
  checks.reached_native=(await screen()).some(l=>l.includes('NATIVE_ESC_'));
  // The next input takes the flipped cells back (and reaches the native).
  await key('x');frames.cleared=await shot(base);checks.after_key=[...inverse()].filter(c=>!base.has(c)).length;
  checks.draft=(await screen()).find(l=>l.includes('❯ '))||'';
  // A row with Hangul, taken from the second cell of `한` to the first cell of `다` (a wide character is whole).
  osc.length=0;
  await drag(end(han,1),da);await finish(da);frames.korean=await shot(base);checks.korean=texts();
  await click(trail); // a press that does not drag: nothing flipped, nothing copied
  checks.korean_after_click=texts();checks.click_inverse=[...inverse()].filter(c=>!base.has(c)).length;
  // Two rows: the first row's end is a line end.
  osc.length=0;
  await drag(w,end(mixed,4),[end(w,2)]);await finish(end(mixed,4));frames.two_rows=await shot(base);checks.two_rows=texts();
  // A long row wrapped onto the next: one line, however far across it goes.
  osc.length=0;
  await drag(long,{x:long.x+5,y:long.y+1});await finish({x:long.x+5,y:long.y+1});frames.wrapped=await shot(base);checks.wrapped=texts();
  // The blanks after a row's text are cut: from the text's start to the area's far right.
  osc.length=0;
  await drag(trail,{x:long.x+cfg.native_cols-1,y:trail.y});await finish({x:long.x+cfg.native_cols-1,y:trail.y});checks.trail=texts();
  // A word (two presses) and a line (three): the long line is both rows, joined.
  osc.length=0;
  await click(path);down(path);await sleep(40);up(path);await sleep(200);frames.word=await shot(base);checks.word=texts();
  osc.length=0;
  await key('x',200);await click(long);await click(long);down(long);await sleep(40);up(long);await sleep(200);frames.line=await shot(base);checks.line=texts();
  // The drag runs on over the panel (right) or from it (left): the end is the area's own edge, no popup opens, nothing leaks.
  if(cfg.reach!==null&&cfg.reach!==undefined){
    osc.length=0;await key('x',200);
    await drag(w,{x:cfg.reach,y:w.y});await finish({x:cfg.reach,y:w.y});checks.reach=texts();frames.reach=await shot(base);
    checks.popup_after=has('✕ ─╮')(await screen());
  }
  // A sign-in address the terminal wrapped is one line; one the app cut into rows that each end by themselves is copied as the
  // rows it is (no blank, and a line break where the app ended a row).
  osc.length=0;await key('\r',200);
  const u1=await at(cfg.url1.slice(0,30)),u1end={x:u1.x+cfg.url1.length-cfg.native_cols-1,y:u1.y+1};
  await drag(u1,u1end);frames.url_drag=await shot(base);await finish(u1end);frames.url_done=await shot(base);checks.url_wrapped=texts();
  osc.length=0;await key('x',200);
  const u2=await at(cfg.url2.slice(0,30)),u2end={x:u2.x+cfg.url2.length-cfg.url_cut-1,y:u2.y+1};
  await drag(u2,u2end);await finish(u2end);frames.url_split=await shot(base);checks.url_split=texts();
  // The native writes the clipboard itself: the write goes on, the read and the malformed one do not.
  osc.length=0;await key('\r',200);await key('osc\r',500);checks.native_osc=texts();checks.native_osc_raw=osc.slice();
  // A link the native draws (OSC 8): what reaches the outer terminal (measured, not asserted).
  await key('link\r',400);checks.osc8=osc8.slice();
  checks.native_esc_after=(await screen()).some(l=>l.includes('NATIVE_ESC_'));
  fs.writeFileSync(cfg.result,JSON.stringify({frames,checks}));
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{try{child.write('\x1b');child.write('quit\r');}catch{}await sleep(300);try{child.kill();}catch{}}
})();
"""

# The host's own mouse routing: the functions cut from host.cjs (the panel's, the popup's and the native's), with a real
# terminal and recorders for what they reach.
ROUTING = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {Terminal}=require('@xterm/headless');
const shell=process.argv[1],text=fs.readFileSync(path.join(shell,'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function panelMouse('),text.indexOf('function inputBytes('));
const flush=t=>new Promise(r=>t.write('',r));
async function world(options={}){
  const left=!!options.left,calls={native:[],emitted:[],actions:[],closed:0,opened:[],settings:[],scrolled:[],displays:0,bytes:[]};
  const real=new Terminal({cols:84,rows:20,allowProposedApi:true,scrollback:0});
  real.write('hello world path/to/file.txt\r\n한글 가나다 mixed abc\r\nthird');await flush(real);
  // The host reads `modes` fresh from its terminal each time; here the one thing it asks is held by the test.
  const t={cols:84,rows:20,get buffer(){return real.buffer;},modes:{mouseTrackingMode:'none'}};
  const g={side:true,folded:false,left,panelColumn:left?0:86,panelCols:44,nativeColumn:left?46:0},place='p';
  const c={fleetView:null,paste:false,relay:false,dragging:false,settings:options.settings||null,routeArea:options.routeArea||null,routeScroll:0,
    terminal:t,geometry:()=>g,panelColumns:()=>44,panelRow:()=>0,height:()=>20,cols:130,rows:20,
    layoutQueued:false,panelHitsPlacement:place,placement:()=>place,panelHits:[{row:3,left:2,right:20,action:'popup:MESSAGES'}],
    panelAction:a=>calls.actions.push(a),queueLayout(){},queueDisplay(){calls.displays++;},schedule(){},
    displayConfiguration:{side_width:44},foldByHand(){},source:()=>Promise.resolve({ok:true}),openSettings(){},
    panelView:{defaults:{}},popupLayer:options.popup||null,popupShown:()=>!!options.popup,closePopup(){calls.closed++;},
    openPopup:(...a)=>calls.opened.push(a),table:null,block:{wheel(){},leave(){}},POPUP_KIND:{},
    emit:v=>calls.emitted.push(v),child:{write:b=>calls.native.push(b.toString('latin1'))},observeInput(){},observeReport(){},
    currentDelivery:null,keyTail:'',inputBytes:b=>calls.bytes.push(b.toString('latin1')),inputOwner:{hold(){}},heldBytes:0,
    scrollRoutes:d=>calls.scrolled.push(d),Date,Buffer,pick:null,pressed:null,mouseInput:null,epoch:0,empty:true,settingsPaste:false,
    require:p=>p.startsWith('./')?require(path.join(shell,p)):require(p)};
  try{c.picking=require(path.join(shell,'selection.cjs'));}catch{} // (a host before the selection has none: the pinned cases still run)
  vm.createContext(c);vm.runInContext(code,c);
  const say=(code,x,y,release)=>c.mouseInput.feed(Buffer.from(`\x1b[<${code};${x};${y}${release?'m':'M'}`,'latin1'));
  const mode=m=>{t.modes.mouseTrackingMode=m;};
  return {c,t,calls,say,mode,g};
}
const copied=calls=>calls.emitted.map(s=>{const m=/^\x1b\]52;c;([A-Za-z0-9+/=]*)\x07$/.exec(s);return m?Buffer.from(m[1],'base64').toString('utf8'):s;});
module.exports={world,copied,assert};
"""


class SessionShellCopySelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        POPUP.SessionShellPopupTest.setUpClass.__func__(cls)
        cls.fake = cls.root / 'selection-native.py'; cls.fake.write_text(FAKE_NATIVE)
        cls.driver = cls.root / 'selection-driver.cjs'; cls.driver.write_text(DRIVER)
        (cls.root / 'routing.cjs').write_text(ROUTING)
        cls.policy = Path(cls.env.get('XDG_CONFIG_HOME') or Path(cls.env['HOME']) / '.config') / 'hearting/session-shell.json'

    def run_node(self, script):
        env = dict(self.env, ROUTING=str(self.root / 'routing.cjs'))
        result = subprocess.run([self.node, '-e', script, str(SHELL)], capture_output=True, text=True, timeout=60, env=env, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    # ---- the text rules ----

    def test_the_text_of_a_selection_follows_wide_characters_wrapping_and_line_ends(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),path=require('node:path');
const {Terminal}=require('@xterm/headless'),{Unicode11Addon}=require('@xterm/addon-unicode11');
const s=require(path.join(process.argv[1],'selection.cjs')),succession=require(path.join(process.argv[1],'succession.cjs'));
const make=async(cols,data)=>{const t=new Terminal({cols,rows:10,allowProposedApi:true,scrollback:0});t.loadAddon(new Unicode11Addon());t.unicode.activeVersion='11';
  await new Promise(r=>t.write(data,r));return t;};
const sel=(a,b,mode='cell')=>({a:{x:a[0],y:a[1]},b:{x:b[0],y:b[1]},mode});
(async()=>{
const t=await make(20,'hello world abc\r\n한글 가나다 mixed\r\nabcdefghijklmnopqrstuvwxyz0123\r\nend   \r\n');
assert.equal(s.textOf(t,sel([6,0],[8,0])),'wor');
// Hangul: each character takes two cells. An end on the second half of a wide character, a start on its second
// half, both take the character whole.
assert.equal(s.textOf(t,sel([0,1],[1,1])),'한');
assert.equal(s.textOf(t,sel([1,1],[3,1])),'한글');
assert.equal(s.textOf(t,sel([1,1],[6,1])),'한글 가');
assert.equal(s.textOf(t,sel([5,1],[9,1])),'가나다');
// A selection made backwards reads the same.
assert.equal(s.textOf(t,sel([9,1],[5,1])),'가나다');
// Two rows: the first row ends its line (the blanks after its text are cut), the second goes only as far as chosen.
assert.equal(s.textOf(t,sel([6,0],[5,1])),'world abc\n한글 가');
assert.equal(s.textOf(t,sel([0,0],[19,0])),'hello world abc');
// A row that wrapped onto the next joins it with no break and keeps its blanks; the last row's own end is cut.
assert.equal(s.textOf(t,sel([0,2],[5,3])),'abcdefghijklmnopqrstuvwxyz');
assert.equal(s.textOf(t,sel([18,2],[19,3])),'stuvwxyz0123');
assert.equal(s.textOf(t,sel([0,2],[19,3])),'abcdefghijklmnopqrstuvwxyz0123');
// `end   ` ends in blanks that are cut when the selection reaches past the text.
assert.equal(s.textOf(t,sel([0,4],[19,4])),'end');
assert.equal(s.textOf(t,sel([0,3],[19,4])),'uvwxyz0123\nend');
// Rows outside or blank: nothing.
assert.equal(s.textOf(t,sel([0,8],[19,8])),'');
// A word, a line (the rows a long line wrapped onto are one line).
assert.deepEqual(s.wordAt(t,7,0),{a:{x:6,y:0},b:{x:10,y:0}});
assert.deepEqual(s.wordAt(t,5,0),{a:{x:5,y:0},b:{x:5,y:0}},'a blank is a word of blanks');
assert.deepEqual(s.wordAt(t,3,1),{a:{x:0,y:1},b:{x:3,y:1}},'a wide character, either half, is one word');
assert.deepEqual(s.wordAt(t,2,1),{a:{x:0,y:1},b:{x:3,y:1}});
assert.deepEqual(s.lineAt(t,3),{a:{x:0,y:2},b:{x:19,y:3}});
assert.deepEqual(s.lineAt(t,2),{a:{x:0,y:2},b:{x:19,y:3}});
assert.deepEqual(s.lineAt(t,0),{a:{x:0,y:0},b:{x:19,y:0}});
// The cells a row flips: a wide character whole at each end; none outside the rows.
assert.deepEqual(s.rowSpan(t,sel([1,1],[6,1]),1),[0,6]);
assert.deepEqual(s.rowSpan(t,sel([1,1],[6,1]),0),null);
assert.deepEqual(s.rowSpan(t,sel([6,0],[5,1]),0),[6,19]);
assert.deepEqual(s.rowSpan(t,sel([6,0],[5,1]),1),[0,6],'the first half of 가 is taken with its second');
// The row as the outer terminal gets it: the chosen cells in the opposite video (an inverse cell turns plain).
const u=await make(20,'ab\x1b[7mcd\x1b[0mef');
const row=succession.serialize(u.buffer.active.getLine(0),6,0,[1,3]);
const cellsOf=(row)=>{const out=[];let inv=false;for(const m of row.matchAll(/\x1b\[([\d;]*)m|(.)/gu)){if(m[1]!==undefined){const p=m[1].split(';').map(Number);inv=p.includes(7);}else out.push(m[2]+(inv?'*':''));}return out.join('');};
assert.equal(cellsOf(row),'ab*cdef','b flips on; c and d, inverse themselves, flip off');
assert.equal(succession.serialize(u.buffer.active.getLine(0),6),succession.serialize(u.buffer.active.getLine(0),6,0,null),'no mark: the same bytes as before');
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_what_is_copied_is_osc_52_in_base64_and_a_native_write_is_filtered(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),path=require('node:path');
const s=require(path.join(process.argv[1],'selection.cjs'));
const b64=t=>Buffer.from(t).toString('base64');
assert.equal(s.copySequence('한글 abc'),'\x1b]52;c;'+b64('한글 abc')+'\x07');
assert.equal(s.copySequence(''),'');
// A long copy is cut on a character boundary, never in the middle of one.
const long=s.copySequence('한'.repeat(60000)),body=Buffer.from(long.slice(7,-1),'base64').toString('utf8');
assert(/^한+$/.test(body)&&Buffer.byteLength(body)<=s.MAX_COPY,Buffer.byteLength(body));
// A clipboard write by the native: only plain base64 with a real data part goes on, to the target it named.
assert.equal(s.nativeCopy('c;'+b64('hi')),'\x1b]52;c;'+b64('hi')+'\x07');
assert.equal(s.nativeCopy(';'+b64('hi')),'\x1b]52;c;'+b64('hi')+'\x07','an empty target is the clipboard');
assert.equal(s.nativeCopy('p;'+b64('hi')),'\x1b]52;p;'+b64('hi')+'\x07');
assert.equal(s.nativeCopy('x!;'+b64('hi')),'\x1b]52;c;'+b64('hi')+'\x07','an unknown target is the clipboard');
for(const bad of ['c;?','c;','c','c;not base64!','c;QUJD\x07\x1b]0;title','c;QUJD\x1b\\','c;'+'A'.repeat(1048577)])assert.equal(s.nativeCopy(bad),'',JSON.stringify(bad.slice(0,20)));
// Presses of the same cell in a short time count up to three and begin again; another cell or a pause starts over.
assert.equal(s.clicks(null,1,1,0),1);
assert.equal(s.clicks({x:1,y:1,time:0,count:1},1,1,200),2);
assert.equal(s.clicks({x:1,y:1,time:0,count:2},1,1,200),3);
assert.equal(s.clicks({x:1,y:1,time:0,count:3},1,1,200),1);
assert.equal(s.clicks({x:1,y:1,time:0,count:1},2,1,200),1);
assert.equal(s.clicks({x:1,y:1,time:0,count:1},1,1,2000),1);
""")

    # ---- the host's routing ----

    def routing(self, body):
        return self.run_node("const {world,copied,assert}=require(process.env.ROUTING);(async()=>{" + body
                         + "})().catch(e=>{console.error(e);process.exit(1);});")

    def test_a_drag_over_a_native_that_wants_no_mouse_is_selected_and_copied_by_the_shell(self):
        self.routing(r"""
const pause=()=>new Promise(r=>setTimeout(r,480)); // presses of one cell in a short time are a double press
for(const left of [false,true]){
  const w=await world({left}),off=left?46:0;
  // Press at cell 6 of row 0 (1-based x 7), drag, release at cell 10: `world`. Mid-drag nothing is copied; the cells are marked.
  w.say(0,off+7,1,false);w.say(32,off+9,1,false);
  assert.deepEqual(w.calls.emitted,[]);assert.equal(w.c.pick.dragged,true);assert.equal(w.c.pickMark(0).join(),'6,8');
  w.say(32,off+11,1,false);w.say(0,off+11,1,true);
  assert.deepEqual(w.calls.emitted.length,1);assert.deepEqual(copied(w.calls),['world']);
  assert.deepEqual(w.calls.native,[],'nothing of it reaches the native');assert.deepEqual(w.calls.actions,[]);
  // It stands until the next input; the next screen change or input takes it back.
  assert.equal(w.c.pick.live,false);assert.equal(w.c.pickMark(0).join(),'6,10');
  w.c.userInput(Buffer.from('a'));assert.equal(w.c.pick,null);
  // A drag that goes out past the area's edge ends on the edge (right: into the panel; left: into the panel on its left).
  const far=left?1:100;await pause();
  w.say(0,off+7,1,false);w.say(32,far,1,false);w.say(0,far,1,true);
  assert.deepEqual(copied(w.calls).slice(1),[left?'hello w':'world path/to/file.txt']);
  assert.deepEqual(w.calls.actions,[],'a drag over the panel presses nothing in it');
  // A press that does not drag: nothing is marked or copied, and it goes nowhere (as before).
  const n=w.calls.emitted.length;w.c.userInput(Buffer.from('a'));
  w.say(0,off+3,2,false);w.say(0,off+3,2,true);
  assert.equal(w.calls.emitted.length,n);assert.equal(w.c.pick,null);assert.deepEqual(w.calls.native,[]);
  // Back to the cell it began at: no selection either.
  w.say(0,off+4,2,false);w.say(32,off+6,2,false);w.say(32,off+4,2,false);w.say(0,off+4,2,true);
  assert.equal(w.calls.emitted.length,n);assert.equal(w.c.pick,null);
  // Two rows, Hangul whole (the first cell of `한` at 1-based x 1, its second at 2).
  await pause();
  w.say(0,off+7,1,false);w.say(32,off+2,2,false);w.say(0,off+2,2,true);
  assert.equal(copied(w.calls).at(-1),'world path/to/file.txt\n한');
  // A word (two presses), a line (three).
  w.c.userInput(Buffer.from('a'));
  // (the second press copies the word, as the third then copies the line)
  for(const [presses,want] of [[2,['path/to/file.txt']],[3,['path/to/file.txt','hello world path/to/file.txt']]]){
    const w2=await world({left});
    for(let i=0;i<presses;i++){w2.say(0,off+14,1,false);w2.say(0,off+14,1,true);}
    assert.deepEqual(copied(w2.calls),want,String(presses));
  }
}
""")

    def test_a_native_that_reads_the_mouse_still_gets_every_report(self):
        self.routing(r"""
for(const mode of ['x10','vt200','drag','any']){
  const w=await world({left:true});w.mode(mode);
  w.say(0,46+7,1,false);w.say(32,46+9,1,false);w.say(0,46+9,1,true);
  assert.deepEqual(w.calls.emitted,[]);assert.equal(w.c.pick,null);
  // A left panel moves the area: the native sees its own columns.
  assert.deepEqual(w.calls.native,['\x1b[<0;7;1M','\x1b[<32;9;1M','\x1b[<0;9;1m'],mode);
}
// A selection begun while the native wanted none ends when it asks for the mouse.
const w=await world();w.say(0,7,1,false);w.say(32,9,1,false);assert.equal(w.c.pick.dragged,true);
w.mode('drag');w.say(32,11,1,false);assert.equal(w.c.pick,null);assert.deepEqual(w.calls.native,['\x1b[<32;11;1M']);
""")

    def test_the_panels_presses_and_drags_and_the_popups_stay_as_they_were(self):
        # Pinned for the behaviour before this change too: the same assertions hold for a host without the selection.
        self.routing(r"""
// A press on a target of the panel acts, whatever the native wants; its release and motion are the panel's.
for(const mode of ['none','drag']){
  const w=await world();w.mode(mode);
  w.say(0,90,4,false);w.say(0,90,4,true);
  assert.deepEqual(w.calls.actions,['popup:MESSAGES'],mode);assert.deepEqual(w.calls.native,[]);assert.deepEqual(w.calls.emitted,[]);
  // The wheel and a drag over the panel go to no native.
  w.say(64,90,4,false);w.say(32,95,5,false);assert.deepEqual(w.calls.native,[]);
}
// The panel's boundary (the left edge of a right panel): a press there begins a width change, motion sets it, the release ends it.
{ const w=await world();
  w.say(0,87,5,false);assert.equal(w.c.dragging,true);
  w.say(32,100,5,false);assert.equal(w.c.displayConfiguration.side_width,32,'at least 32');
  w.say(32,70,6,false);assert.equal(w.c.displayConfiguration.side_width,61);
  w.say(0,70,6,true);assert.equal(w.c.dragging,false);
  assert.deepEqual(w.calls.emitted,[]);assert.deepEqual(w.calls.native,[]);
}
// A left panel's boundary is its right edge.
{ const w=await world({left:true});
  w.say(0,44,5,false);assert.equal(w.c.dragging,true);w.say(32,50,5,false);assert.equal(w.c.displayConfiguration.side_width,50);
  w.say(0,50,5,true);assert.equal(w.c.dragging,false);assert.deepEqual(w.calls.emitted,[]);
}
// The settings editor stands: a press in the native area is swallowed as before, and nothing is selected.
{ const w=await world({settings:{index:0,value:{}}});
  w.say(0,7,1,false);w.say(32,9,1,false);w.say(0,9,1,true);
  assert.deepEqual(w.calls.emitted,[]);assert.deepEqual(w.calls.native,[]);assert.equal(w.c.pick,null);
}
// A popup stands: a press outside closes it and goes nowhere; the drag and release that follow are its too.
{ const popup={frame:{top:5,left:10,width:30,height:8},hits:[{row:5,left:36,right:38,action:'popup-close'}]};
  const w=await world({popup});
  w.say(0,3,2,false);assert.equal(w.calls.closed,1);
  w.say(32,9,2,false);w.say(0,9,2,true);
  assert.deepEqual(w.calls.emitted,[]);assert.deepEqual(w.calls.native,[]);assert.equal(w.c.pick,null);assert.equal(w.calls.closed,1);
  // A press inside it is the popup's (nothing selected or sent).
  const v=await world({popup});v.say(0,20,8,false);v.say(32,24,8,false);v.say(0,24,8,true);
  assert.deepEqual(v.calls.emitted,[]);assert.deepEqual(v.calls.native,[]);assert.equal(v.c.pick,null);
  // The wheel over it reads on.
  const x=await world({popup});x.say(64,20,8,false);assert.deepEqual(x.calls.native,[]);
}
// The two cells between the native area and the panel: still nothing (as before).
{ const w=await world();
  w.say(0,85,3,false);w.say(32,86,3,false);w.say(0,86,3,true);
  assert.deepEqual(w.calls.emitted,[]);assert.deepEqual(w.calls.native,[]);assert.equal(w.c.pick,null);
}
// The wheel over the native area with no mouse wanted does nothing.
{ const w=await world();w.say(64,5,5,false);w.say(65,5,5,false);
  assert.deepEqual(w.calls.emitted,[]);assert.deepEqual(w.calls.native,[]);assert.equal(w.c.pick,null);
}
// A right or middle press in the native area does nothing either.
{ const w=await world();w.say(2,5,5,false);w.say(1,5,5,false);w.say(2,5,5,true);
  assert.deepEqual(w.calls.emitted,[]);assert.deepEqual(w.calls.native,[]);assert.equal(w.c.pick,null);
}
""")

    def test_the_native_own_clipboard_write_goes_on_and_a_read_does_not(self):
        self.run_node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {Terminal}=require('@xterm/headless');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const start=text.indexOf('terminal.parser.registerOscHandler(52,'),code=text.slice(start,text.indexOf('\n});',start)+4);
(async()=>{
  const t=new Terminal({cols:20,rows:5,allowProposedApi:true,scrollback:0}),emitted=[];
  const c={terminal:t,picking:require(path.join(process.argv[1],'selection.cjs')),emit:v=>emitted.push(v),relay:false,ended:false};
  vm.createContext(c);vm.runInContext(code,c);
  const w=d=>new Promise(r=>t.write(d,r));
  await w('\x1b]52;c;QUJD\x07');assert.deepEqual(emitted,['\x1b]52;c;QUJD\x07']);
  await w('\x1b]52;c;?\x07\x1b]52;c;\x07\x1b]52;;***\x07\x1b]52;c;QUJD\x1b\\');
  assert.deepEqual(emitted,['\x1b]52;c;QUJD\x07','\x1b]52;c;QUJD\x07'],'the read, the empty and the malformed ones go nowhere; ST ends one too');
  c.relay=true;await w('\x1b]52;c;QUJD\x07');assert.equal(emitted.length,2);
  // None of it is drawn in the screen.
  assert.equal(t.buffer.active.getLine(0).translateToString(true),'');
})().catch(e=>{console.error(e);process.exit(1);});
""")

    # ---- the real host in a private PTY ----

    def pty(self, harness, layout, position='right', cols=160, rows=40, reach=None, native_cols=114, scenario=None):
        self.policy.write_text(json.dumps({'enabled': True, 'layout': layout, 'side_width': 44, 'position': position, 'bottom_rows': 10}))
        self.policy.chmod(0o600)
        name = '-'.join((harness, layout, position) + ((scenario,) if scenario else ()))
        case = self.root / name; case.mkdir(mode=0o700)
        project = case / 'project'; project.mkdir()
        sid = {'claude': '00000000-0000-4000-8000-0000000000e1', 'codex': '00000000-0000-4000-8000-0000000000e2'}[harness]
        backend = case / 'backend'
        questions = case / 'questions.json'; questions.write_text('[]')
        backend.write_text(POPUP.BACKEND.format(python=sys.executable, utilities=str(ROOT / 'utilities'),
                                                questions=str(questions), answers=str(case / 'answers.jsonl')))
        backend.chmod(0o700)
        config = {'harness': harness, 'sid': sid, 'python': sys.executable, 'fake': str(self.fake),
                  'entry': str(ROOT / 'utilities/session-shell/entry.py'), 'launcher': str(self.launcher),
                  'backend': str(backend), 'diagnostic': str(case / 'display-errors'), 'project': str(project),
                  'result': str(case / 'result.json'), 'cols': cols, 'rows': rows, 'reach': reach, 'native_cols': native_cols,
                  'url1': URL_WRAPPED, 'url2': URL_SPLIT, 'url_cut': URL_CUT, 'scenario': scenario}
        (case / 'config.json').write_text(json.dumps(config))
        env = dict(self.env, LOOP_CONFIG=str(case / 'config.json'))
        child = subprocess.Popen([self.node, str(self.driver)], env=env, cwd=project, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            _out, err = child.communicate(timeout=150)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM); child.communicate(timeout=10); raise
        diagnostic = (case / 'display-errors').read_text() if (case / 'display-errors').exists() else ''
        self.assertEqual(child.returncode, 0, err + diagnostic)
        result = json.loads((case / 'result.json').read_text())
        if SHOW_FRAMES:
            print('FRAMES ' + name + ' ' + json.dumps(result, ensure_ascii=False), flush=True)
        return result

    def selected(self, harness, layout, position='right', native_cols=114, **kwargs):
        checks = self.pty(harness, layout, position, native_cols=native_cols, **kwargs)['checks']
        # Mid-drag nothing is copied; the release copies the one row; the cells flip while they stand and not after the next key.
        self.assertEqual(checks['during'], [])
        self.assertEqual(checks['one_row'], ['world path/to/file'])
        self.assertFalse(checks['reached_native'])
        self.assertEqual(checks['after_key'], 0)
        self.assertIn('❯ x', checks['draft'])
        self.assertEqual(checks['korean'], ['한글 가나다'])
        self.assertEqual(checks['korean_after_click'], ['한글 가나다'], 'a press that does not drag copies nothing')
        self.assertEqual(checks['click_inverse'], 0)
        self.assertEqual(checks['two_rows'], ['world path/to/file.txt\n한글 가나다 mixed'])
        self.assertEqual(checks['wrapped'], [LONG[:native_cols + 6]])
        self.assertEqual(checks['trail'], ['trail'])
        self.assertEqual(checks['word'], ['path/to/file.txt'])
        self.assertEqual(checks['line'], ['w000', LONG], 'the second press copies the word, the third the line')
        # The native's own clipboard write goes on; its read and the malformed write do not.
        self.assertEqual(checks['native_osc'], ['from native 한글'])
        # ...and its bytes go out as the native wrote them (target and data), nothing else.
        self.assertEqual(checks['native_osc_raw'], ['c;' + base64.b64encode('from native 한글'.encode()).decode()])
        # A sign-in address the terminal wrapped copies as one line; one the app cut by itself copies as the rows it is.
        self.assertEqual(checks['url_wrapped'], [URL_WRAPPED])
        self.assertEqual(checks['url_split'], [URL_SPLIT[:URL_CUT] + '\n' + URL_SPLIT[URL_CUT:]])
        self.assertFalse(checks['native_esc_after'])
        return checks

    def test_a_clipboard_write_by_the_native_reaches_the_outer_terminal_as_it_was_written(self):
        # The sign-in screen's own copy key writes the clipboard this way. Before 11-20 the shell's terminal copy ended it.
        checks = self.pty('claude', 'side', scenario='osc')['checks']
        self.assertEqual(checks['native_osc_raw'], ['c;' + base64.b64encode('from native 한글'.encode()).decode()],
                         'the write goes out with its target and data; the read and the malformed write do not')

    def test_claude_beside_a_panel_on_the_right(self):
        checks = self.selected('claude', 'side', 'right', reach=159)
        self.assertEqual(checks['reach'], ['world path/to/file.txt'])
        self.assertFalse(checks['popup_after'])

    def test_claude_beside_a_panel_on_the_left(self):
        checks = self.selected('claude', 'side', 'left', reach=0)
        self.assertEqual(checks['reach'], ['hello w'])
        self.assertFalse(checks['popup_after'])

    def test_claude_with_the_panel_under_the_native_area(self):
        self.selected('claude', 'bottom', cols=120, rows=34, native_cols=120)

    def test_a_native_that_reads_the_mouse_gets_the_drag_and_the_window_selects_nothing(self):
        checks = self.pty('codex', 'side')['checks']
        self.assertGreaterEqual(checks['native_esc'], 1)
        self.assertEqual(checks['osc'], [])
        self.assertEqual(checks['new_inverse'], 0)


if __name__ == '__main__':
    if '--frames' in sys.argv:
        sys.argv.remove('--frames'); SHOW_FRAMES = True
    unittest.main()
