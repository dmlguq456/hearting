"""11-26 follow-up to the written answer of several lines (the person: "깔짝깔짝 고칠래 계속?"): what review left open.

A paste reads to the same text in the shell's question popup and in Fleet (an escape sequence is left out whole). A
paste whose bytes stop for a second has stalled, not ended (review 1861): what comes after is still its text, so a
line break of it never sends a half-written answer; only a key pressed then (a read of its own: Enter, Esc, and
Ctrl+] at any time) ends it and is that key, and the rest of that paste is dropped, never typed and never the
native's. In a secret every line-break key sends, as Ctrl+J did before; a pasted key keeps its inner line breaks and,
pasted in several lines, one of those it ends with. A draw failure that hands the terminal to the plain relay gives it
back the native's own modes. These run the real editor (and Fleet's real reader and reducer) with synthetic values.
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
// `idle`: the editor's stall time, short here; a stall is waited for by its state, never by a fixed sleep.
function make(extra={},{idle=60,state='submitted'}={}){let sent;const all=[];const b=create({cellWidth:cells,pasteIdle:idle,changed(){},action(){},
  answer:async x=>(sent=x,all.push(x.answers.q[0]),{state,reason:'kept'})});
 b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Synthetic question?',free_text:true,...extra}]}],'',{labels:{}});b.enter();
 return {b,get sent(){return sent},all,
  async stall(){for(let i=0;i<400&&!b.stalled;i++)await wait(5);assert.equal(b.stalled,true,'the paste stalled');},
  draft(){return b.detail({columns:200,budget:60}).filter(r=>r.some(p=>p.answerRow)).map(r=>r.map(p=>p.text).join('').slice(2)).join('\n');},
  async send(keys){for(const k of [].concat(keys)){b.input(Buffer.isBuffer(k)?k:Buffer.from(k));await wait(30);}},
  async enter(){b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));return sent;}};}
"""


def fleet_answers(steps, *, secret=False):
    """What Fleet sends for the same bytes: its real reader (a stall is its clock moved on, so no sleep), the explorer's
    key names (``answer_key``, then ``_explorer_key_name``) and the reducer, on a question taking a written answer."""
    import time
    from fleet.render_explorer import _explorer_key_name
    from fleet.render_input import _CursesInput, answer_key
    from fleet.explorer.state import NavState, reduce

    class Curses:
        ERR = -1
        KEY_MOUSE = 409
        KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 259, 258, 260, 261
        KEY_PPAGE, KEY_NPAGE, KEY_HOME, KEY_END, KEY_DC = 339, 338, 262, 360, 330

    class Screen:
        def __init__(self, data=b""):
            self.values = list(data)

        def timeout(self, delay):
            pass

        def getch(self):
            return self.values.pop(0) if self.values else Curses.ERR

    ref = {"id": "S4:synthetic", "harness": "codex", "session_id": "synthetic-session",
           "request_id": "synthetic-request", "question_id": "q"}
    question = {"id": "q", "question": "Synthetic question?", "is_other": True, "choices": [],
                **({"is_secret": True} if secret else {})}
    envelope = {"kind": "question", "state": "open", "harness": "codex", "questions": [question],
                "support": {"supported": True}, "native_ref": {"transport": "hearting-message-answer"}}
    state = NavState(view="QUESTIONS", title="QUESTIONS", folds=(("qsel:QUESTIONS", ref["id"]),),
                     question_overlay={"ref": ref, "detail": envelope, "draft": None, "state": "open",
                                       "editing": False, "pending": False})
    state, _ = reduce(state, {"type": "click", "action": ("question.edit", "q")})
    reader, sent = _CursesInput(Curses, explorer_mode=True), []
    reader.alt_keys = True

    def feed(events):
        nonlocal state
        for code, payload in events:
            editing = bool(state.question_overlay.get("editing"))
            key = answer_key(code, payload, writable=True, editing=editing, cr_enter=True)
            if key is None:
                key = _explorer_key_name(code)
            if key:
                state, effects = reduce(state, {"type": "key", "key": key, "tabs": ["QUESTIONS"], "question_refs": [ref]})
                sent.extend(effect[2]["answers"]["q"][0] for effect in effects if effect[0] == "questions.submit")

    def drain(screen):
        events = []
        for _ in range(4096):
            event = reader.read(screen)
            if event[0] != Curses.ERR:
                events.append(event)
            elif not screen.values and not reader.ready:
                break
        return events

    for step in steps:
        if step in ("STALL", "QUIET"):
            reader.paste_since -= 2
            if reader.paste_key is not None:
                reader.paste_key_since -= 1
            feed(drain(Screen()))
        elif step == "WAIT":                     # an Esc's 25 ms
            if reader.paste_key is not None:
                reader.paste_key_since -= 1
            feed(drain(Screen()))
        elif step == "REOPEN":                   # the popup opened again; in Fleet the board is still there
            continue
        else:
            feed(drain(Screen(step if isinstance(step, bytes) else step.encode())))
    time.sleep(0)
    return sent


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

    def test_a_paste_that_stalls_goes_on_and_only_a_pressed_key_ends_it(self):
        self.node(r"""
(async()=>{
 const row=x=>x.b.detail({columns:60,budget:20}).at(-1).map(p=>p.text).join('');
 // A stall is no end: the rest is still the paste's text (its CR a line break, its Tab a tab, its escape sequence left
 // out); no half-written answer goes, and the popup stands. The key row says how out while it stands stalled.
 {const x=make({},{state:'unknown'});await x.send(['\x1b[200~첫 줄\r둘']);await x.stall();
  assert.equal(x.b.pasting,true);assert.equal(row(x),'paste stalled · Enter send · Esc/Ctrl+] back');
  await x.send(['째 줄\r셋째\t줄\x1b[31m빨강\x1b[0m\x1b[201~']);assert.deepEqual(x.all,[],'no half-written answer');
  assert.equal(x.b.focused,true);assert.equal(x.b.pasting,false);assert.notEqual(row(x),'paste stalled · Enter send · Esc/Ctrl+] back');
  assert.deepEqual((await x.enter()).answers.q,['첫 줄\n둘째 줄\n셋째\t줄빨강']);assert.equal(x.all.length,1);}
 // Stalled, a pressed Enter (a read of its own) sends what came, once; the rest of that paste is dropped, up to its end
 // mark; after it the keys are keys again.
 {const x=make({},{state:'unknown'});await x.send(['\x1b[200~부분 글']);await x.stall();
  await x.send(['\r']);assert.deepEqual(x.all,['부분 글']);assert.equal(x.b.discarding,true);assert.equal(x.b.pasting,true,'panel-mouse reads it as a paste');
  assert.equal(x.b.input(Buffer.from('나머지\r끝\x1b[2')).length,0);assert.equal(x.b.input(Buffer.from('01~')).length,0);
  assert.equal(x.b.discarding,false);assert.deepEqual(x.all,['부분 글'],'never twice');assert.equal(x.draft(),'부분 글');}
 // Stalled, a pressed Esc closes after its 25 ms; the rest of the paste is never the native's (given back from the end mark on).
 {const x=make();await x.send(['\x1b[200~로그 앞']);await x.stall();
  x.b.input(Buffer.from('\x1b'));await wait(60);assert.equal(x.b.focused,false);assert.equal(x.b.discarding,true);
  assert.equal(x.b.swallow(Buffer.from('[31m빨강\x1b[0m 다음\r끝')).length,0);assert.equal(x.b.swallow(Buffer.from('\x1b[201~z')).toString(),'z');
  assert.equal(x.sent,undefined);x.b.enter();assert.equal(x.draft(),'로그 앞','the draft stays');}
 // Stalled, an Esc then `[` within its 25 ms is an escape sequence of the paste (a late end mark too): the paste goes on.
 {const x=make();await x.send(['\x1b[200~ab']);await x.stall();x.b.input(Buffer.from('\x1b'));x.b.input(Buffer.from('[201~'));
  await wait(60);assert.equal(x.b.focused,true);assert.equal(x.b.pasting,false);assert.equal(x.b.discarding,false);assert.equal(x.draft(),'ab');}
 // A paste that never ends does not hold the popup: stalled again and again, Esc pressed closes it.
 {const x=make();await x.send(['\x1b[200~끝 없음']);await x.stall();await x.send(['x']);assert.equal(x.b.stalled,false);await x.stall();
  x.b.input(Buffer.from('\x1b'));await wait(60);assert.equal(x.b.focused,false);x.b.enter();assert.equal(x.draft(),'끝 없음x');}
 // Ctrl+] pressed (a read of its own) leaves at once, stalled or not; the rest of that paste is dropped, never the native's.
 {const x=make();await x.send(['\x1b[200~앞 글']);assert.equal(x.b.stalled,false);
  assert.equal(x.b.input(Buffer.from('\x1d')).length,0);assert.equal(x.b.focused,false);
  assert.equal(x.b.swallow(Buffer.from('뒤 글\r다음 줄\x1b[201~')).length,0);assert.equal(x.b.input(Buffer.from('z')).toString(),'z');}
 // The panel's mouse reader hands the start mark on alone, then the Ctrl+] of the same read: the rest is dropped too.
 {const x=make();x.b.input(Buffer.from('\x1b[200~'));x.b.input(Buffer.from('\x1d'));assert.equal(x.b.focused,false);
  assert.equal(x.b.swallow(Buffer.from('pasted\rtext\x1b[201~')).length,0);}
 // A second with no byte while dropping: the dropping ends, and what comes then is the native's.
 {const x=make();await x.send(['\x1b[200~a']);x.b.input(Buffer.from('\x1d'));assert.equal(x.b.discarding,true);
  for(let i=0;i<400&&x.b.discarding;i++)await wait(5);assert.equal(x.b.discarding,false);assert.equal(x.b.swallow(Buffer.from('q')).toString(),'q');}
 // A Ctrl+] inside the pasted bytes is their control character: left out, the popup stands.
 {const x=make();assert.equal(x.b.input(Buffer.from('\x1b[200~ab\x1dcd')).length,0);assert.equal(x.b.focused,true);
  await x.send(['\x1b[201~']);assert.equal(x.draft(),'abcd');}
 // Not stalled (bytes keep coming), a lone Enter or Esc is the paste's own.
 {const x=make({},{idle:400});await x.send(['\x1b[200~a','\r','b','\x1b','c\x1b[201~']);assert.equal(x.sent,undefined);
  assert.equal(x.b.focused,true);assert.deepEqual((await x.enter()).answers.q,['a\nbc']);}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_the_same_stalled_paste_gives_the_same_answer_on_both_screens(self):
        """The same bytes, cut where a paste stalls, through the popup and through Fleet's reader, key names and reducer."""
        import base64
        STALL = "STALL"
        cases = [
            ("A: stalled, the rest has a CR", ["\x1b[200~첫 줄\r둘", STALL, "째 줄\r셋째 줄\x1b[201~", "\r"], {}),
            ("B: stalled, the rest in two reads", ["\x1b[200~하나\r둘", STALL, "째\r", "셋째\r넷째\x1b[201~", "\r"], {}),
            ("C: stalled, a lone Esc, the rest", ["\x1b[200~로그 앞", STALL, "\x1b", "WAIT", "[31m빨강\x1b[0m 다음\r끝\x1b[201~", "REOPEN", "\r"], {}),
            ("E: Ctrl+] alone mid paste", ["\x1b[200~앞 글", "\x1d", "뒤 글\r다음 줄\x1b[201~", "REOPEN", "\r"], {}),
            ("stalled, Enter pressed", ["\x1b[200~부분 글", STALL, "\r", "나머지\r끝\x1b[201~"], {}),
            # The rest of a paste that never ends is dropped until a second goes by with no byte (keys in it too).
            ("never ends, Esc", ["\x1b[200~끝 없음", STALL, "x", STALL, "\x1b", "WAIT", "QUIET", "REOPEN", "\r"], {}),
            ("CR LF cut by a stall", ["\x1b[200~a\r", STALL, "\nb\x1b[201~", "\r"], {}),
            ("a character cut by a stall", [b"\x1b[200~\xea\xb0", STALL, b"\x80\xeb\x82\x98\x1b[201~", "\r"], {}),
            ("a sequence cut by a stall", ["\x1b[200~x\x1b[3", STALL, "1my\x1b[201~", "\r"], {}),
            ("secret of several lines, stalled", ["\x1b[200~k1\nk2\n", STALL, "\r"], {"secret": True}),
            ("secret of one line, stalled", ["\x1b[200~token\n", STALL, "\r"], {"secret": True}),
            ("secret of several lines, in parts", ["\x1b[200~a\n", STALL, "b\n", STALL, "\x1b[201~", "\r"], {"secret": True}),
            ("secret of one line, in parts", ["\x1b[200~ab", STALL, "c\n\n\x1b[201~", "\r"], {"secret": True}),
        ]
        encode = lambda step: base64.b64encode(step if isinstance(step, bytes) else step.encode()).decode()  # noqa: E731
        shell = json.loads(self.node(r"""
(async()=>{
 const out=[];
 for(const c of JSON.parse(process.argv[2])){
  const x=make(c.extra,{state:'unknown'});
  for(const step of c.steps){
   if(step==='STALL')await x.stall();else if(step==='WAIT')await wait(60);else if(step==='REOPEN')x.b.enter();
   else if(step==='QUIET'){for(let i=0;i<400&&x.b.discarding;i++)await wait(5);assert.equal(x.b.discarding,false);}
   else{const data=Buffer.from(step,'base64');(x.b.focused?x.b.input(data):x.b.swallow(data));await new Promise(r=>setImmediate(r));}
  }
  await wait(20);out.push(x.all);
 }
 console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1)});
""", json.dumps([{"steps": [s if s in (STALL, "WAIT", "REOPEN", "QUIET") else encode(s) for s in steps], "extra": extra}
                 for _name, steps, extra in cases])))
        for index, (name, steps, extra) in enumerate(cases):
            with self.subTest(case=name):
                fleet = fleet_answers(steps, secret=bool(extra.get("secret")))
                self.assertEqual(shell[index], fleet, "the two screens send the same")
                self.assertEqual(len(shell[index]), 1, "sent once, when Enter was pressed")
        self.assertEqual(shell[0], ["첫 줄\n둘째 줄\n셋째 줄"])
        self.assertEqual(shell[2], ["로그 앞"])
        self.assertEqual(shell[4], ["부분 글"])
        self.assertEqual(shell[9], ["k1\nk2\n"])
        self.assertEqual(shell[10], ["token"])
        self.assertEqual(shell[11], ["a\nb\n"])
        self.assertEqual(shell[12], ["abc"])

    def test_a_secret_takes_every_line_break_key_as_enter_and_a_pasted_key_keeps_its_lines(self):
        self.node(r"""
(async()=>{
 for(const [name,key] of [['Ctrl+J','\n'],['Alt+Enter','\x1b\r'],['Alt+LF','\x1b\n'],['kitty Shift+Enter','\x1b[13;2u'],
   ['modifyOtherKeys Ctrl+Enter','\x1b[27;5;13~'],['kitty Ctrl+J','\x1b[106;5u'],['\\ then Enter','\\\r']]){
  const x=make({secret:true});await x.send(['pw',key]);await new Promise(r=>setImmediate(r));
  assert.deepEqual(x.sent?.answers.q,[name==='\\ then Enter'?'pw\\':'pw'],name+' sends the secret');
 }
 // A pasted key of several lines keeps them and one of the line breaks it ends with (a private key needs its last one;
 // CR, LF, CR LF alike); a one-line secret keeps none.
 {const x=make({secret:true});await x.send(['\x1b[200~-----BEGIN\r\nline1\nline2\r\n\r\n\x1b[201~']);
  assert.deepEqual((await x.enter()).answers.q,['-----BEGIN\nline1\nline2\n']);}
 {const x=make({secret:true});await x.send(['\x1b[200~token\r\n\x1b[201~']);assert.deepEqual((await x.enter()).answers.q,['token']);}
 {const x=make({secret:true});await x.send(['\x1b[200~\n\n\x1b[201~']);assert.equal(x.draft(),'','line breaks alone put nothing in');}
 // Typed after the paste: the kept break was the paste's end.
 {const x=make({secret:true});await x.send(['\x1b[200~k1\nk2\n\x1b[201~','3']);assert.deepEqual((await x.enter()).answers.q,['k1\nk2\n3']);}
 // Split reads, a stall, then Enter pressed: the same rule.
 {const x=make({secret:true});await x.send(['\x1b[200~a\r','\nb\n']);await x.stall();assert.deepEqual((await x.enter()).answers.q,['a\nb\n']);}
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
