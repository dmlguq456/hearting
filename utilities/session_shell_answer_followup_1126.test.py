"""11-26 follow-up to the written answer of several lines (the person: "깔짝깔짝 고칠래 계속?"): what review left open.

A paste reads to the same text in the shell's question popup and in Fleet (an escape sequence is left out whole). A
paste whose end mark never comes is over a second after its last byte, and Ctrl+] leaves the popup even before that.
In a secret every line-break key sends, as Ctrl+J did before, and a pasted key keeps its inner line breaks but not the
ones it ends with. A draw failure that hands the terminal to the plain relay gives it back the native's own modes.
These run the real editor (and Fleet's real reader) with synthetic values.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "utilities")]

HARNESS = r"""
const assert=require('node:assert/strict'),path=require('node:path'),dir=process.argv[1];
const create=require(path.join(dir,'block-input.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/\p{Mark}/u.test(c)?0:c.codePointAt(0)>=0x2e80?2:1),0);
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function make(extra={}){let sent;const b=create({cellWidth:cells,changed(){},action(){},answer:async x=>(sent=x,{state:'submitted'})});
 b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Synthetic question?',free_text:true,...extra}]}],'',{labels:{}});b.enter();
 return {b,get sent(){return sent},
  draft(){return b.detail({columns:200,budget:60}).filter(r=>r.some(p=>p.answerRow)).map(r=>r.map(p=>p.text).join('').slice(2)).join('\n');},
  async send(keys){for(const k of [].concat(keys)){b.input(Buffer.isBuffer(k)?k:Buffer.from(k));await wait(30);}},
  async enter(){b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));return sent;}};}
"""


class FollowupTest(unittest.TestCase):
    def node(self, script, *args):
        result = subprocess.run(["node", "-e", HARNESS + script, str(SHELL), *args],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_paste_reads_to_the_same_text_in_the_popup_and_in_fleet(self):
        import base64
        from fleet.render_input import KEY_PASTE, _CursesInput

        class Curses:
            ERR = -1
            KEY_MOUSE = 409
            KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 259, 258, 260, 261
            KEY_PPAGE, KEY_NPAGE, KEY_HOME, KEY_END, KEY_DC = 339, 338, 262, 360, 330

        class Screen:
            def __init__(self, data):
                self.values = list(data)

            def timeout(self, delay):
                pass

            def getch(self):
                return self.values.pop(0) if self.values else Curses.ERR

        def fleet(body):
            reader, screen = _CursesInput(Curses, explorer_mode=True), Screen(b"\x1b[200~" + body + b"\x1b[201~")
            for _ in range(len(body) + 32):
                event = reader.read(screen)
                if event[0] == KEY_PASTE:
                    return event[1]
            self.fail("no paste key")

        cases = [b"a\x1b[31mb\x1b[0mc",                                     # a coloured log line
                 "가\x1b[1;32m나\x1b[m\r\n다\r라".encode(),                   # Korean, CR LF and CR
                 "x\x1bêy".encode(),                                    # Esc then a character of two bytes
                 b"\x1b\x1b[31mz",                                           # Esc, then a sequence
                 b"p\x1bOq\tr",                                              # Esc O (no sequence in a paste), a tab
                 b"t\x1b]0;title\x07u",                                      # an OSC: its Esc and BEL go, its text stays
                 "탭\t끝\x00\x07\x7f".encode(),                               # other controls are left out
                 b"s\x1b[" + b"1;" * 40 + b"m e",                            # a long parameter run, ended
                 b"o\x1b[" + b"9" * 70 + b"v"]                               # one past the popup's sequence limit
        want = {0: "abc", 1: "가나\n다\n라"}
        shell = json.loads(self.node(r"""
(async()=>{
 const out=[];
 for(const body of JSON.parse(process.argv[2])){const x=make();await x.send(['\x1b[200~',Buffer.from(body,'base64'),'\x1b[201~']);out.push((await x.enter()).answers.q[0]);}
 console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1)});
""", json.dumps([base64.b64encode(case).decode() for case in cases])))
        for index, body in enumerate(cases):
            with self.subTest(body=body):
                self.assertEqual(shell[index], fleet(body), "the two screens read the same paste alike")
                if index in want:
                    self.assertEqual(shell[index], want[index])

    def test_a_paste_that_never_ends_is_over_a_second_after_its_last_byte(self):
        self.node(r"""
(async()=>{
 // No end mark: a second after the last byte the paste is over; what is typed then is typed, and Esc closes.
 {const x=make();await x.send(['\x1b[200~abc\rdef']);assert.equal(x.b.pasting,true);
  await x.send(['\x1b']);assert.equal(x.b.focused,true,'an Esc inside the paste is part of it');
  await wait(1100);assert.equal(x.b.pasting,false,'over a second after its last byte');
  await x.send(['g']);assert.equal(x.draft(),'abc\ndefg');
  x.b.input(Buffer.from('\x1b'));await wait(60);assert.equal(x.b.focused,false,'Esc closes the popup again');}
 // Ctrl+] pressed leaves at once, the paste under way ends with the popup; what is typed next is the native's.
 {const x=make();await x.send(['\x1b[200~abc']);assert.equal(x.b.pasting,true);
  assert.equal(x.b.input(Buffer.from('\x1d')).length,0);assert.equal(x.b.focused,false);assert.equal(x.b.pasting,false);
  assert.equal(x.b.input(Buffer.from('z')).toString(),'z','after it the keys are the native\'s');}
 // A Ctrl+] inside the pasted bytes is their control character: left out, the popup stands (none of it reaches the native).
 {const x=make();assert.equal(x.b.input(Buffer.from('\x1b[200~ab\x1dcd')).length,0);assert.equal(x.b.focused,true);
  await x.send(['\x1b[201~']);assert.equal(x.draft(),'abcd');}
 // A paste whose bytes keep coming is not cut: each byte gives it another second.
 {const x=make();await x.send(['\x1b[200~a']);await wait(600);await x.send(['b']);await wait(600);await x.send(['c']);
  assert.equal(x.b.pasting,true);await x.send(['\x1b[201~','d']);assert.equal(x.draft(),'abcd');}
 // An end mark cut by the second: what came of it is dropped, and the keys after it are keys.
 {const x=make();await x.send(['\x1b[200~ab\x1b[20']);await wait(1100);assert.equal(x.b.pasting,false);
  await x.send(['c']);assert.equal(x.draft(),'abc');}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_a_secret_takes_every_line_break_key_as_enter_and_a_pasted_key_keeps_its_inner_lines(self):
        self.node(r"""
(async()=>{
 for(const [name,key] of [['Ctrl+J','\n'],['Alt+Enter','\x1b\r'],['Alt+LF','\x1b\n'],['kitty Shift+Enter','\x1b[13;2u'],
   ['modifyOtherKeys Ctrl+Enter','\x1b[27;5;13~'],['kitty Ctrl+J','\x1b[106;5u'],['\\ then Enter','\\\r']]){
  const x=make({secret:true});await x.send(['pw',key]);await new Promise(r=>setImmediate(r));
  assert.deepEqual(x.sent?.answers.q,[name==='\\ then Enter'?'pw\\':'pw'],name+' sends the secret');
 }
 // A pasted key of several lines keeps them; the line breaks it ends with are left out (CR, LF, CR LF alike).
 {const x=make({secret:true});await x.send(['\x1b[200~-----BEGIN\r\nline1\nline2\r\n\r\n\x1b[201~']);
  assert.deepEqual((await x.enter()).answers.q,['-----BEGIN\nline1\nline2']);}
 // Typed after the paste, the held breaks were the paste's end: they stay left out.
 {const x=make({secret:true});await x.send(['\x1b[200~k1\nk2\n\x1b[201~','3']);assert.deepEqual((await x.enter()).answers.q,['k1\nk23']);}
 // Split reads and a paste whose end mark never comes: the same.
 {const x=make({secret:true});await x.send(['\x1b[200~a\r','\nb\n']);await wait(1100);assert.deepEqual((await x.enter()).answers.q,['a\nb']);}
 // Not a secret: a line-break key breaks the line and a paste goes in whole.
 {const x=make();await x.send(['a','\n','\x1b[200~b\n\x1b[201~']);assert.deepEqual((await x.enter()).answers.q,['a\nb\n']);}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_a_draw_failure_gives_the_terminal_back_the_natives_own_modes(self):
        # host.cjs rawRelay, run from its own source: what the host turned on for itself (bracketed paste while the
        # question popup had the keys, the panel's mouse) goes off; what the native asked for stays.
        script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const src=fs.readFileSync(process.argv[1]+'/host.cjs','utf8');
const a=src.indexOf('function rawRelay() {'),b=src.indexOf('// A display exception never owns',a);assert(a>=0&&b>a);
function relay(native,painted){
 const out=[];const context={relay:false,ended:false,activated:true,succeeding:false,fleetView:undefined,table:null,state:'idle',empty:true,
  heldStop(){},clearTimeout(){},frameTimer:undefined,projectionTimer:undefined,beatTimer:undefined,mouseInput:{flush(){}},displayVTs:new Set(),watchers:new Map(),cols:80,rows:24,child:{resize(){}},
  bypass(){throw Error('not active');},emit:t=>out.push(t),paintedModes:new Map(painted),
  terminal:{modes:{bracketedPasteMode:false,applicationCursorKeysMode:false,mouseTrackingMode:'none',sendFocusMode:false,...native},dispose(){}}};
 vm.createContext(context);vm.runInContext(src.slice(a,b)+'\nrawRelay();',context);
 assert.equal(context.relay,true);return out.join('');
}
const popup=[[2004,true],[1,false],[9,false],[1000,false],[1002,true],[1003,false],[1006,true],[1004,false],[25,true]];
// The native has bracketed paste off and no mouse: the popup's paste mode and the panel's mouse go off.
{const out=relay({},popup);for(const m of ['2004','1002','1006'])assert(out.includes('\x1b[?'+m+'l'),m+' off: '+JSON.stringify(out));
 assert(!/\x1b\[\?(?!25h)\d+h/.test(out),'nothing turned on (the cursor is shown as before): '+JSON.stringify(out));assert(out.endsWith('\x1b[?25h\x1b[2J\x1b[H'));}
// The native asked for bracketed paste and drag reporting itself: they stay, and nothing is sent for them.
{const out=relay({bracketedPasteMode:true,mouseTrackingMode:'drag'},popup);
 assert(!out.includes('2004')&&!out.includes('1002')&&!out.includes('1006'),JSON.stringify(out));}
// The native's any-motion reporting the panel replaced comes back, after the panel's mode goes off.
{const out=relay({mouseTrackingMode:'any',bracketedPasteMode:true},popup);
 assert(out.indexOf('\x1b[?1002l')>=0&&out.indexOf('\x1b[?1002l')<out.indexOf('\x1b[?1003h'),JSON.stringify(out));}
console.log('ok');
"""
        result = subprocess.run(["node", "-e", script, str(SHELL)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
