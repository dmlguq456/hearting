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
// `idle`: the editor's stall time, short here (and `keyWait`, how long a key pressed in a paste waits for nothing to
// follow it); a stall is waited for by its state, a key's wait by more than it, never by a fixed second.
const KEY_WAIT=40;
function make(extra={},{idle=60,state='submitted',keyWait=KEY_WAIT}={}){let sent;const all=[];const b=create({cellWidth:cells,pasteIdle:idle,keyWait,changed(){},action(){},
  answer:async x=>(sent=x,all.push(x.answers.q[0]),{state,reason:'kept'})});
 b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Synthetic question?',free_text:true,...extra}]}],'',{labels:{}});b.enter();
 return {b,get sent(){return sent},all,
  async stall(){for(let i=0;i<400&&!b.stalled;i++)await wait(5);assert.equal(b.stalled,true,'the paste stalled');},
  draft(){return b.detail({columns:200,budget:60}).filter(r=>r.some(p=>p.answerRow)).map(r=>r.map(p=>p.text).join('').slice(2)).join('\n');},
  async send(keys){for(const k of [].concat(keys)){b.input(Buffer.isBuffer(k)?k:Buffer.from(k));await wait(30);}},
  // A key pressed: sent alone, then nothing for longer than a key's wait.
  async press(key){b.input(Buffer.from(key));await wait(KEY_WAIT+30);},
  async enter(){b.input(Buffer.from('\r'));await wait(KEY_WAIT+30);return sent;}};}
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
        elif step == "WAIT":                     # a key's wait, with nothing after it
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
 // Review 1861 (A): after a stall the paste's next byte can come alone, a CR: a byte within the key's wait makes it the
 // paste's line break, never Enter.
 {const x=make({},{state:'unknown'});await x.send(['\x1b[200~첫 줄']);await x.stall();
  x.b.input(Buffer.from('\r'));x.b.input(Buffer.from('둘째 줄\r셋째\x1b[201~'));await wait(KEY_WAIT+30);
  assert.deepEqual(x.all,[],'no half-written answer');assert.equal(x.draft(),'첫 줄\n둘째 줄\n셋째');}
 // Stalled, a pressed Enter (a read of one key, nothing after it for the wait) sends what came, once; the paste's bulk
 // after it is dropped, up to its end mark.
 {const x=make({},{state:'unknown'});await x.send(['\x1b[200~부분 글']);await x.stall();
  await x.press('\r');assert.deepEqual(x.all,['부분 글']);assert.equal(x.b.discarding,true);assert.equal(x.b.pasting,false);
  assert.equal(x.b.input(Buffer.from('나머지\r끝\x1b[2')).length,0);assert.equal(x.b.input(Buffer.from('01~')).length,0);
  assert.equal(x.b.discarding,false);assert.deepEqual(x.all,['부분 글'],'never twice');assert.equal(x.draft(),'부분 글');}
 // Review 1861 (B): while the bulk is dropped a read holding one key is the person's and goes on: typed keys (each
 // its own read), an arrow, a mouse or focus report, a character of several bytes; a new paste goes on whole and ends
 // the dropping. Only the bulk is dropped: several characters in one read (an input method's commit of `안녕` too).
 {const x=make();await x.send(['\x1b[200~끝 없는 붙여넣기']);await x.stall();await x.press('\x1b');assert.equal(x.b.focused,false);
  for(const key of ['h','e','\r','가','\x1b[A','\x1b[<0;10;5M','\x1b[I','\x1d'])assert.equal(x.b.swallow(Buffer.from(key)).toString(),key,JSON.stringify(key));
  assert.equal(x.b.swallow(Buffer.from('안녕')).length,0,'several characters in one read: the bulk');assert.equal(x.b.discarding,true);
  assert.equal(x.b.swallow(Buffer.from('\x1b[200~새 붙여넣기\x1b[201~')).toString(),'\x1b[200~새 붙여넣기\x1b[201~');assert.equal(x.b.discarding,false);}
 // The dropping ends a second after the last read it dropped, however the person types (their keys do not count it).
 {const x=make();await x.send(['\x1b[200~a']);await x.stall();await x.press('\x1b');const since=Date.now();
  while(x.b.discarding&&Date.now()-since<2000){x.b.swallow(Buffer.from('k'));await wait(15);}
  assert.equal(x.b.discarding,false);assert(Date.now()-since<60*4,'ended by its second, not by the person going quiet');
  const y=make({},{idle:200});await y.send(['\x1b[200~a']);await y.stall();await y.press('\x1b');await wait(110);
  assert.equal(y.b.discarding,true);y.b.swallow(Buffer.from('bulk of it'));
  await wait(120);assert.equal(y.b.discarding,true,'a dropped read counts the second again');}
 // Stalled, a pressed Esc closes after the wait; the paste's bulk is never the native's (given back from the end mark on).
 {const x=make();await x.send(['\x1b[200~로그 앞']);await x.stall();
  await x.press('\x1b');assert.equal(x.b.focused,false);assert.equal(x.b.discarding,true);
  assert.equal(x.b.swallow(Buffer.from('[31m빨강\x1b[0m 다음\r끝')).length,0);assert.equal(x.b.swallow(Buffer.from('\x1b[201~z')).toString(),'z');
  assert.equal(x.sent,undefined);x.b.enter();assert.equal(x.draft(),'로그 앞','the draft stays');}
 // Stalled, an Esc then `[` within the wait is an escape sequence of the paste (a late end mark too): the paste goes on.
 {const x=make();await x.send(['\x1b[200~ab']);await x.stall();x.b.input(Buffer.from('\x1b'));x.b.input(Buffer.from('[201~'));
  await wait(KEY_WAIT+30);assert.equal(x.b.focused,true);assert.equal(x.b.pasting,false);assert.equal(x.b.discarding,false);assert.equal(x.draft(),'ab');}
 // A paste that never ends does not hold the popup: stalled again and again, Esc pressed closes it.
 {const x=make();await x.send(['\x1b[200~끝 없음']);await x.stall();await x.send(['x']);assert.equal(x.b.stalled,false);await x.stall();
  await x.press('\x1b');assert.equal(x.b.focused,false);x.b.enter();assert.equal(x.draft(),'끝 없음x');}
 // Ctrl+] pressed (a read of one key, nothing after it) leaves, stalled or not; the paste's bulk after it is dropped.
 {const x=make();await x.send(['\x1b[200~앞 글']);assert.equal(x.b.stalled,false);
  await x.press('\x1d');assert.equal(x.b.focused,false);
  assert.equal(x.b.swallow(Buffer.from('뒤 글\r다음 줄\x1b[201~')).length,0);assert.equal(x.b.input(Buffer.from('z')).toString(),'z');}
 // The panel's mouse reader hands the start mark on alone, then the Ctrl+] of the same read: the bulk is dropped too.
 {const x=make();x.b.input(Buffer.from('\x1b[200~'));x.b.input(Buffer.from('\x1d'));await wait(KEY_WAIT+30);assert.equal(x.b.focused,false);
  assert.equal(x.b.swallow(Buffer.from('pasted\rtext\x1b[201~')).length,0);}
 // A Ctrl+] inside the pasted bytes is their control character: left out, the popup stands.
 {const x=make();assert.equal(x.b.input(Buffer.from('\x1b[200~ab\x1dcd')).length,0);assert.equal(x.b.focused,true);
  await x.send(['\x1b[201~']);assert.equal(x.draft(),'abcd');}
 // Not stalled (bytes keep coming), a lone Enter or Esc is the paste's own.
 {const x=make({},{idle:400});await x.send(['\x1b[200~a','\r','b','\x1b','c\x1b[201~']);assert.equal(x.sent,undefined);
  assert.equal(x.b.focused,true);assert.deepEqual((await x.enter()).answers.q,['a\nbc']);}
 // Review 1861 (🟡1): what a pressed key leaves out of the paste is what is not whole of it.
 {const x=make({},{state:'unknown'});await x.send(['\x1b[200~x\x1b[3']);await x.stall();await x.press('\r');assert.deepEqual(x.all,['x']);}
 {const x=make({},{state:'unknown'});await x.send([Buffer.concat([Buffer.from('\x1b[200~가'),Buffer.from('나').subarray(0,2)])]);await x.stall();
  await x.press('\r');assert.deepEqual(x.all,['가']);}
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
            ("E: Ctrl+] alone mid paste", ["\x1b[200~앞 글", "\x1d", "WAIT", "뒤 글\r다음 줄\x1b[201~", "REOPEN", "\r"], {}),
            ("stalled, Enter pressed", ["\x1b[200~부분 글", STALL, "\r", "WAIT", "나머지\r끝\x1b[201~"], {}),
            # Review 1861 (A): a CR alone then a byte within the wait is a line break; a CR alone, nothing after it, Enter.
            ("a lone CR, then the rest within the wait", ["\x1b[200~첫 줄", STALL, "\r", "둘째 줄\r셋째\x1b[201~", "\r"], {}),
            ("a lone CR, nothing after it", ["\x1b[200~첫 줄", STALL, "\r", "WAIT", "둘째 줄\r셋째\x1b[201~"], {}),
            ("secret, a lone CR, then the rest", ["\x1b[200~k1\nk2", STALL, "\r", "\nk3\n\x1b[201~", "\r"], {"secret": True}),
            # 🟡1: an escape sequence not ended, a character's first bytes, left out where a pressed key ends the paste.
            ("stalled in a sequence, Enter pressed", ["\x1b[200~x\x1b[3", STALL, "\r", "WAIT"], {}),
            ("stalled in a character, Enter pressed", [b"\x1b[200~\xea\xb0\x80\xeb\x82", STALL, "\r", "WAIT"], {}),
            # The rest of a paste that never ends is dropped until a second goes by with no byte (keys in it too).
            ("never ends, Esc", ["\x1b[200~끝 없음", STALL, "x", STALL, "\x1b", "WAIT", "QUIET", "REOPEN", "\r"], {}),
            ("CR LF cut by a stall", ["\x1b[200~a\r", STALL, "\nb\x1b[201~", "\r"], {}),
            ("a character cut by a stall", [b"\x1b[200~\xea\xb0", STALL, b"\x80\xeb\x82\x98\x1b[201~", "\r"], {}),
            ("a sequence cut by a stall", ["\x1b[200~x\x1b[3", STALL, "1my\x1b[201~", "\r"], {}),
            ("secret of several lines, stalled", ["\x1b[200~k1\nk2\n", STALL, "\r", "WAIT"], {"secret": True}),
            ("secret of one line, stalled", ["\x1b[200~token\n", STALL, "\r", "WAIT"], {"secret": True}),
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
   if(step==='STALL')await x.stall();else if(step==='WAIT')await wait(KEY_WAIT+30);else if(step==='REOPEN')x.b.enter();
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
        named = {name: shell[index] for index, (name, _steps, _extra) in enumerate(cases)}
        self.assertEqual(named["A: stalled, the rest has a CR"], ["첫 줄\n둘째 줄\n셋째 줄"])
        self.assertEqual(named["C: stalled, a lone Esc, the rest"], ["로그 앞"])
        self.assertEqual(named["stalled, Enter pressed"], ["부분 글"])
        self.assertEqual(named["a lone CR, then the rest within the wait"], ["첫 줄\n둘째 줄\n셋째"])
        self.assertEqual(named["a lone CR, nothing after it"], ["첫 줄"])
        self.assertEqual(named["secret, a lone CR, then the rest"], ["k1\nk2\nk3\n"], "its CR LF one line break")
        self.assertEqual(named["stalled in a sequence, Enter pressed"], ["x"])
        self.assertEqual(named["stalled in a character, Enter pressed"], ["가"])
        self.assertEqual(named["secret of several lines, stalled"], ["k1\nk2\n"])
        self.assertEqual(named["secret of one line, stalled"], ["token"])
        self.assertEqual(named["secret of several lines, in parts"], ["a\nb\n"])
        self.assertEqual(named["secret of one line, in parts"], ["abc"])

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


class PrivateTmuxEdgesTest(unittest.TestCase):
    """Review 1861, second round, in the real shell on a private tmux (synthetic native and backend, 54 columns): one
    `send-keys` is one read of the host. A paste that stalls and then brings a CR alone keeps it a line break (A); after
    a key ends a stalled paste the person's next input reaches where it goes, only the paste's bulk is dropped (B)."""

    def test_the_edges_of_a_stalled_paste(self):
        import importlib.util, shlex, shutil, signal, tempfile, time  # noqa: E401
        from unittest import mock
        if not shutil.which("tmux"):
            self.skipTest("tmux unavailable")

        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        harness = load("af_harness", ROOT / "utilities/session_shell_question_input.test.py")
        fixture = load("af_backend", ROOT / "utilities/session_shell_wait_blink_pty.test.py")
        sid = harness.SID
        with tempfile.TemporaryDirectory(prefix="af-") as temp:
            root = Path(temp)
            runner = load("af_isolation", ROOT / "tools/run-tests.py")
            env = runner.build_isolated_env(root, repo_root=ROOT)
            env.update(FLEET_NOW_LANG="ko", CLAUDE_CONFIG_DIR=str(root / "native-config"))
            for key in ("cache", "devdir"):
                env["npm_config_" + key] = str(root / ("npm-" + key))
            env["npm_config_userconfig"] = str(root / "npmrc")
            (root / "npmrc").write_text("")
            sys.path[:0] = [str(ROOT / "tools/install")]
            import session_shell_install
            with mock.patch.dict(os.environ, env, clear=True):
                session_shell_install.prepare_assets()
            policy = Path(env.get("XDG_CONFIG_HOME", str(Path(env["HOME"]) / ".config"))) / "hearting/session-shell.json"
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text(json.dumps(dict(enabled=True, layout="bottom", side_width=40, side_min_columns=132,
                                              shade="none", theme="dark", rule_motion="rule")))
            policy.chmod(0o600)
            case = root / "case"
            (case / "project").mkdir(parents=True)
            qp = case / "questions.json"
            qp.write_text("{}")
            backend = case / "backend"
            code = fixture.BACKEND.format(python=sys.executable, utilities=str(ROOT / "utilities"),
                                          tools=str(ROOT / "tools"), sid=sid, questions=str(qp))
            code = code.replace("raise SystemExit(m._backend())",
                "def answer(binding, request):\n    with open(" + repr(str(case / "answers")) +
                ", 'a') as log: log.write(json.dumps(request, ensure_ascii=False)+'\\n')\n" +
                "    if 'submitted' in str(request.get('question_id')): return {'ok': True, 'state': 'submitted'}\n" +
                "    return {'ok': True, 'state': 'unknown', 'reason': 'synthetic recorded'}\n" +
                "m._block_answer=answer\nraise SystemExit(m._backend())")
            backend.write_text(code)
            backend.chmod(0o700)
            cfg = dict(sid=sid, backend=str(backend), socket=str(case / "tmux.sock"), frames=str(case / "frames"),
                       output=str(case / "output"), received=str(case / "received"),
                       native_output=str(case / "native-output"), state=str(case / "state"))
            (case / "state").write_text("idle")
            (case / "config").write_text(json.dumps(cfg))
            (case / "native.py").write_text(harness.NATIVE)
            (case / "preload.cjs").write_text(harness.PRELOAD)
            e = dict(env, LOOP_CONFIG=str(case / "config"), NODE_OPTIONS="--require " + str(case / "preload.cjs"),
                     TERM="xterm-256color")
            sock = str(case / "tmux.sock")

            def tm(*args):
                return subprocess.check_output(["tmux", "-S", sock, "-f", "/dev/null", *args], env=e,
                                               stderr=subprocess.STDOUT, text=True, timeout=10)
            command = shlex.join([sys.executable, str(SHELL / "entry.py"), "claude", sys.executable, str(case / "native.py")])
            tm("new-session", "-d", "-s", "qi", "-x", "54", "-y", "30", "-c", str(case / "project"), command)
            server = int(tm("display-message", "-p", "#{pid}").strip())
            birth = Path(f"/proc/{server}/stat").read_text().rsplit(") ", 1)[1].split()[19]

            def screen():
                return tm("capture-pane", "-p", "-t", "qi:0.0").splitlines()

            def waitfor(predicate, what):
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    rows = screen()
                    if predicate(rows):
                        return rows
                    time.sleep(.08)
                self.fail("missing " + what + ":\n" + "\n".join(screen()))

            def send(data, delay=.15):
                raw = data if isinstance(data, bytes) else data.encode()
                tm("send-keys", "-t", "qi:0.0", "-H", *[f"{v:02x}" for v in raw])
                time.sleep(delay)

            def answers():
                path = case / "answers"
                return [json.loads(line)["answers"]["q"] for line in path.read_text().splitlines()] if path.exists() else []

            def received():
                path = case / "received"
                return path.read_bytes() if path.exists() else b""
            shown = lambda rows: any("╭─ ›" in row for row in rows)  # noqa: E731
            stalled = lambda rows: any("붙여넣기 멈춤" in row for row in rows)  # noqa: E731
            number = [0]

            def ask(name, submitted=False):
                number[0] += 1
                item = f"fixture-{'submitted-' if submitted else ''}{number[0]}"
                q = dict(id="q", question=f"{name} 장면 질문 {number[0]:02d}", free_text=True, choices=[])
                fresh = qp.with_name("questions.next")       # replaced whole: the backend never reads half of it
                fresh.write_text(json.dumps({sid: [dict(id=item, revision=f"{number[0]:064x}", answerable=True, questions=[q])]}))
                os.replace(fresh, qp)
                tag = f"장면 질문 {number[0]:02d}"
                waitfor(lambda rows: any(tag in row for row in rows), "the question in the panel")
                if not shown(screen()):
                    send("\x1d")
                def on_it(rows):
                    top = next((i for i, row in enumerate(rows) if "╭─ ›" in row), None)
                    return top is not None and any(tag in row for row in rows[top + 1:top + 3])
                waitfor(on_it, "the popup on it")
                return len(answers()), len(received())

            def closed():
                # The scene's draft is wiped (a question not confirmed keeps it for the next one) and the popup closed.
                time.sleep(1.2)                  # the dropping, if any, is over
                if not shown(screen()):
                    send("\x1d", delay=.5)
                if shown(screen()):
                    send("\x7f" * 150, delay=.4)
                    send("\x1d", delay=.4)
                waitfor(lambda rows: not shown(rows), "the popup closed")
            P, E = "\x1b[200~", "\x1b[201~"
            results = []
            try:
                waitfor(lambda rows: any("READY" in row for row in rows), "the native")
                send("\x02")                     # the native turns bracketed paste off (the popup forces it on)
                # A: a CR alone after the stall, the rest 50 ms on (within the key's wait): a line break.
                for submitted in (False, True):
                    count, native = ask("lone_cr", submitted)
                    send(P + "첫 줄", delay=.1)
                    waitfor(stalled, "the stall")
                    send("\r", delay=.05)
                    send("둘째 줄\r셋째" + E, delay=1.0)
                    sent, got = answers()[count:], received()[native:]
                    results.append(dict(scene="lone_cr", submitted=submitted, sent=sent, native=got.decode(), popup=shown(screen())))
                    self.assertEqual(sent, [], "no half-written answer")
                    self.assertEqual(got, b"")
                    send("\r", delay=1.0)
                    self.assertEqual(answers()[count:], [["첫 줄\n둘째 줄\n셋째"]], "the person's Enter: the whole paste, once")
                    closed()
                # The gap left: a CR alone after the stall with nothing after it for longer than the wait (0.3 s) is
                # Enter (what came is sent); the rest after it is dropped, never the native's.
                count, native = ask("lone_cr_late")
                send(P + "첫 줄", delay=.1)
                waitfor(stalled, "the stall")
                send("\r", delay=.3)
                send("둘째 줄\r셋째" + E, delay=1.0)
                results.append(dict(scene="lone_cr_late", sent=answers()[count:], native=received()[native:].decode(), popup=shown(screen())))
                self.assertLessEqual(len(answers()[count:]), 1)
                self.assertEqual(received()[native:], b"")
                closed()
                # B: a key ends a stalled paste; the person types to the native at once: every key reaches it.
                for name, key, submitted in (("type_after_esc", "\x1b", False), ("type_after_enter", "\r", True),
                                             ("type_after_ctrl", "\x1d", False)):
                    count, native = ask(name, submitted)
                    send(P + "끝 없는 붙여넣기", delay=.1)
                    waitfor(stalled, "the stall")
                    send(key, delay=.4)
                    waitfor(lambda rows: not shown(rows), "the popup closed by the key")
                    for char in "hello":
                        send(char, delay=.15)
                    send("\r", delay=.6)
                    got = received()[native:]
                    results.append(dict(scene=name, sent=answers()[count:], native=got.decode()))
                    self.assertEqual(got, b"hello\r", name)
                    self.assertEqual(answers()[count:], [["끝 없는 붙여넣기"]] if key == "\r" else [], name)
                    send("\x15", delay=.3)        # clear the native's line
                    closed()
                # Enter with a backend that did not confirm: the popup stays, and what is typed goes in it (not lost).
                count, native = ask("type_after_enter_unknown")
                send(P + "끝 없는 붙여넣기", delay=.1)
                waitfor(stalled, "the stall")
                send("\r", delay=.6)
                for char in "hello":
                    send(char, delay=.15)
                send("\r", delay=.8)
                results.append(dict(scene="type_after_enter_unknown", sent=answers()[count:], native=received()[native:].decode()))
                self.assertEqual(answers()[count:], [["끝 없는 붙여넣기"], ["끝 없는 붙여넣기hello"]])
                self.assertEqual(received()[native:], b"")
                closed()
                # A new paste after the key reaches the native whole; Ctrl+] opens the popup again.
                count, native = ask("paste_after_esc")
                send(P + "끝 없는 붙여넣기", delay=.1)
                waitfor(stalled, "the stall")
                send("\x1b", delay=.4)
                send(P + "새 붙여넣기" + E, delay=.3)
                send("\r", delay=.6)
                got = received()[native:]
                results.append(dict(scene="paste_after_esc", native=got.decode()))
                self.assertIn("새 붙여넣기".encode(), got)
                self.assertTrue(got.endswith(b"\r"))
                send("\x15", delay=.3)
                closed()
                count, native = ask("ctrl_after_esc")
                send(P + "끝 없는 붙여넣기", delay=.1)
                waitfor(stalled, "the stall")
                send("\x1b", delay=.4)
                waitfor(lambda rows: not shown(rows), "the popup closed by Esc")
                send("\x1d", delay=.4)
                waitfor(shown, "the popup opened again by Ctrl+] at once")
                closed()
                # Typing on for 3 s, a key each 0.15 s, right after the key: none lost.
                count, native = ask("type_for_3s")
                send(P + "끝 없는 붙여넣기", delay=.1)
                waitfor(stalled, "the stall")
                send("\x1b", delay=.3)
                typed = "abcdefghijklmnopqrst"
                for char in typed:
                    send(char, delay=.15)
                got = received()[native:]
                results.append(dict(scene="type_for_3s", native=got.decode()))
                self.assertEqual(got, typed.encode())
                send("\x15", delay=.3)            # clear the native's line
                closed()
                # An input method putting several characters in one read: within the dropping it is the paste's bulk.
                count, native = ask("ime_commit")
                send(P + "끝 없는 붙여넣기", delay=.1)
                waitfor(stalled, "the stall")
                send("\x1b", delay=.3)
                send("안녕", delay=.3)
                inside = received()[native:]
                time.sleep(1.2)
                send("안녕", delay=.4)
                got = received()[native:]
                results.append(dict(scene="ime_commit", within=inside.decode(), after=got.decode()))
                self.assertEqual(inside, b"", "within the dropping several characters in one read are its bulk")
                self.assertEqual(got, "안녕".encode(), "a second after it they reach the native")
                send("\x15", delay=.3)
                closed()
                # Still dropped: the bulk after the key (an Esc and its `[` 80 ms apart are the paste's sequence; the
                # start mark and a Ctrl+] in one read, the rest, then a key).
                count, native = ask("esc_split")
                send(P + "로그\r앞", delay=1.4)
                send("\x1b", delay=.08)
                send("[31m빨강\x1b[0m 다음\r끝" + E, delay=1.0)
                results.append(dict(scene="esc_split", sent=answers()[count:], native=received()[native:].decode(), popup=shown(screen())))
                self.assertEqual(answers()[count:], [])
                self.assertEqual(received()[native:], b"")
                closed()
                count, native = ask("start_ctrl")
                send(P + "\x1d", delay=.3)
                send("뒤 글\r다음" + E, delay=1.0)
                send("x", delay=.3)
                results.append(dict(scene="start_ctrl", sent=answers()[count:], native=received()[native:].decode()))
                self.assertEqual(answers()[count:], [])
                self.assertEqual(received()[native:], b"x")
                print("EDGES=" + json.dumps(results, ensure_ascii=False), flush=True)
                send("\x04")
            finally:
                try:
                    stat = Path(f"/proc/{server}/stat").read_text().rsplit(") ", 1)[1].split()
                    if stat[19] == birth:
                        os.kill(server, signal.SIGTERM)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
