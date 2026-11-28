"""11-26 (the person: "질문 답변에서 줄바꿈이 안되네.. 내가 자유도 높이라고 했지 않나?"): the question popup's written answer
takes lines and the editing keys people use.

Enter alone still sends. A line break goes in with Ctrl+J (LF), Alt/Option+Enter (`ESC CR`/`ESC LF`), `\\` then
Enter, and a modified Enter the terminal reports (kitty `CSI 13;<mods>u`, modifyOtherKeys `CSI 27;<mods>;13~`, kitty
Ctrl+J `CSI 106;5u`). A paste's line breaks are line breaks. On a question answered only by a choice LF is still
Enter. Home/End go to the line's ends, the word and line keys edit, ↑↓ go between the answer's rows, and the answer
grows within the popup and follows the place. These run the real editor and the real popup paint with synthetic values.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "utilities")]

HARNESS = r"""
const assert=require('node:assert/strict'),path=require('node:path'),dir=process.argv[1];
const create=require(path.join(dir,'block-input.cjs')),view=require(path.join(dir,'panel-view.cjs'));
const labels=JSON.parse(process.argv[2]||'{}');
const cells=s=>[...s].reduce((n,c)=>n+(/\p{Mark}/u.test(c)?0:c.codePointAt(0)>=0x2e80?2:1),0);
function make(extra={},more={}){let sent;const b=create({cellWidth:cells,changed(){},action(){},answer:async x=>(sent=x,{state:'submitted'})});
 b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Synthetic question?',free_text:true,...extra}]},...(more.items||[])],'',{labels});b.enter();
 return {b,get sent(){return sent},rows:(columns=60,budget=20)=>b.detail({columns,budget}),
  answer:(columns=60,budget=20)=>b.detail({columns,budget}).filter(r=>r.some(p=>p.answerRow)).map(r=>r.map(p=>p.text).join('')),
  caret:(columns=60,budget=20)=>{const row=b.detail({columns,budget}).find(r=>r.some(p=>p.caret));return row?row.slice(0,row.findIndex(p=>p.caret)+1).map(p=>p.text).join(''):null;},
  async send(keys){for(const k of [].concat(keys)){b.input(Buffer.from(k));await new Promise(r=>setTimeout(r,30));}},
  async enter(){b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));return sent;}};}
const draft=x=>x.answer(200,40).map(r=>r.slice(2)).join('\n');
"""


class AnswerMultilineTest(unittest.TestCase):
    def node(self, script, *args):
        result = subprocess.run(["node", "-e", HARNESS + script, str(SHELL), *args],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_every_line_break_key_puts_a_break_at_the_place_and_enter_sends_the_lines(self):
        self.node(r"""
(async()=>{
 for(const [name,key] of [['Ctrl+J','\n'],['Alt+Enter','\x1b\r'],['Alt+LF','\x1b\n'],['kitty Shift+Enter','\x1b[13;2u'],
   ['kitty Alt+Enter','\x1b[13;3u'],['kitty Ctrl+Enter','\x1b[13;5u'],['modifyOtherKeys Shift+Enter','\x1b[27;2;13~'],
   ['modifyOtherKeys Ctrl+Enter','\x1b[27;5;13~'],['kitty Ctrl+J','\x1b[106;5u']]){
  const x=make();await x.send(['첫째',key,'둘째']);
  assert.deepEqual(x.answer(),['› 첫째','  둘째'],name);assert.equal(x.b.focused,true,name);assert.equal(x.sent,undefined,name+' sends nothing');
  // At the place: in the middle of a line too.
  await x.send(['\x1b[A','\x1b[D',key]);assert.equal(draft(x),'첫\n째\n둘째',name+' in the middle');
  assert.deepEqual((await x.enter()).answers.q,['첫\n째\n둘째'],name+': Enter sends every line');
 }
 // `\` then Enter: the `\` before the place becomes the break; a `\` elsewhere is text; a secret keeps its `\`.
 {const x=make();await x.send(['a\\','\r','b']);assert.equal(draft(x),'a\nb');assert.deepEqual((await x.enter()).answers.q,['a\nb']);}
 {const x=make();await x.send(['a\\b','\r']);assert.deepEqual(x.sent.answers.q,['a\\b'],'a `\\` not before the place');}
 {const x=make({secret:true});await x.send(['pw\\','\r']);await new Promise(r=>setImmediate(r));assert.deepEqual(x.sent.answers.q,['pw\\'],'a secret sends its `\\`');}
 // An unmodified kitty Enter (`CSI 13;1u`) is no line break.
 {const x=make();await x.send(['a','\x1b[13;1u','b']);assert.equal(draft(x),'ab');}
 // Several break keys in a row make empty lines, and the answer keeps them.
 {const x=make();await x.send(['a','\n','\n','b']);assert.deepEqual(x.answer(),['› a','  ','  b']);assert.deepEqual((await x.enter()).answers.q,['a\n\nb']);}
 // A line break counts in the 4,096-byte limit like any character.
 {const x=make();await x.send(['a'.repeat(4096),'\n']);assert(x.rows().some(r=>r.some(p=>p.text.includes('4,096'))),'the limit is said');
  assert.deepEqual((await x.enter()).answers.q,['a'.repeat(4096)],'no break past the limit');}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_lf_stays_enter_on_a_question_answered_only_by_a_choice(self):
        # The path that took LF as Enter: a choice-only question (and Fleet's curses newline mode, tested there).
        self.node(r"""
(async()=>{
 const x=make({free_text:false,choices:['one','two'],recommended:1});
 x.b.input(Buffer.from('\n'));await new Promise(r=>setImmediate(r));assert.deepEqual(x.sent.answers.q,['two']);
 const d=make({free_text:false,choices:['one','two']});d.b.input(Buffer.from('\x1b\r'));await new Promise(r=>setTimeout(r,40));
 assert.equal(d.sent,undefined,'Alt+Enter on a choice is dropped');assert.equal(d.b.focused,true);
 // A written answer with choices: LF breaks the line once writing began, and before it starts the writing.
 const w=make({choices:['one','two']});await w.send(['note','\n','more']);assert.equal(draft(w),'note\nmore');
 assert.deepEqual((await w.enter()).answers.q,['note\nmore']);
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_a_paste_goes_in_whole_and_never_sends(self):
        self.node(r"""
(async()=>{
 for(const [name,body,want] of [['CR','가\r나\r다','가\n나\n다'],['CR LF','가\r\n나\r\n다','가\n나\n다'],['LF','가\n나\n다','가\n나\n다'],
   ['tab and controls (an escape sequence goes whole)','a\tb\x07c\x00d\x7fe\x1b[31mf','a\tbcdef'],['ending in a break','끝\r','끝\n']]){
  const x=make();await x.send(['\x1b[200~'+body+'\x1b[201~']);
  assert.equal(x.sent,undefined,name+': nothing sent');assert.equal(x.b.focused,true);
  assert.deepEqual((await x.enter()).answers.q,[want],name);
 }
 // A paste split across reads, its CR LF split too.
 {const x=make();await x.send(['\x1b[200~one\r','\ntwo\x1b[2','01~']);assert.deepEqual((await x.enter()).answers.q,['one\ntwo']);}
 // A paste in the middle of what was typed goes in at the place.
 {const x=make();await x.send(['ac','\x1b[D','\x1b[200~b1\rb2\x1b[201~']);assert.equal(draft(x),'ab1\nb2c');}
 // A paste on a question with choices starts the writing and keeps the picked choice.
 {const x=make({choices:['one','two']});await x.send(['2','\x1b[200~x\ry\x1b[201~']);assert.deepEqual((await x.enter()).answers.q,['two','x\ny']);}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_line_word_and_text_keys(self):
        self.node(r"""
(async()=>{
 const at=x=>{const c=x.caret(200,40);return c===null?null:c;};
 // Home/End and Ctrl+A/E: the ends of the line holding the place.
 for(const [home,end] of [['\x1b[H','\x1b[F'],['\x01','\x05'],['\x1b[1~','\x1b[4~'],['\x1bOH','\x1bOF']]){
  const x=make();await x.send(['one two','\n','three four',home,'X',end,'Y']);assert.equal(draft(x),'one two\nXthree fourY',JSON.stringify(home));
 }
 // Ctrl+Home/End: the ends of the text.
 {const x=make();await x.send(['ab','\n','cd','\x1b[1;5H','X','\x1b[1;5F','Y']);assert.equal(draft(x),'Xab\ncdY');}
 // Words: Alt/Ctrl+←→ and Alt+B/F step a run of letters and digits; Ctrl+W, Alt+Backspace delete the one before, Alt+D and
 // Ctrl/Alt+Delete the one after.
 for(const left of ['\x1b[1;3D','\x1b[1;5D','\x1bb']){const x=make();await x.send(['한글 낱말, 끝',left,'X']);assert.equal(draft(x),'한글 낱말, X끝',JSON.stringify(left));
  await x.send([left,left,'Y']);assert.equal(draft(x),'한글 Y낱말, X끝');}
 for(const right of ['\x1b[1;3C','\x1b[1;5C','\x1bf']){const x=make();await x.send(['ab cd','\x01',right,'X',right,'Y']);assert.equal(draft(x),'abX cdY',JSON.stringify(right));}
 for(const back of ['\x17','\x1b\x7f','\x1b\x08']){const x=make();await x.send(['keep this word',back]);assert.equal(draft(x),'keep this ',JSON.stringify(back));
  await x.send([back,back]);assert.equal(draft(x),'');}
 for(const del of ['\x1bd','\x1b[3;5~','\x1b[3;3~']){const x=make();await x.send(['one two three','\x01',del]);assert.equal(draft(x),' two three',JSON.stringify(del));}
 // Ctrl+U the line before the place, Ctrl+K the line after it; neither crosses a line break.
 {const x=make();await x.send(['first','\n','second half','\x1b[D','\x1b[D','\x1b[D','\x1b[D','\x15']);assert.equal(draft(x),'first\nhalf');
  await x.send(['\x0b']);assert.equal(draft(x),'first\n');await x.send(['\x0b','\x15']);assert.equal(draft(x),'first\n');}
 // Delete and Backspace cross a line break (joining the lines).
 {const x=make();await x.send(['ab','\n','cd','\x1b[A','\x1b[3~']);assert.equal(draft(x),'abcd');await x.send(['\n','\x1b[B','\x01','\x7f']);assert.equal(draft(x),'abcd');}
 // Word steps cross a line break; one character as the person sees it stays whole.
 {const x=make();await x.send(['a👍🏽 b','\n','c','\x1b[1;5D','\x1b[1;5D','X']);assert.equal(draft(x),'a👍🏽 Xb\nc');}
 {const x=make();await x.send(['가각́','\x1b[D','X']);assert.equal(draft(x),'가X각́');}
 // Not typing (no text): these keys do nothing and nothing goes on.
 {const x=make({choices:['one','two']});for(const k of ['\x17','\x15','\x0b','\x1b[1;5D','\x1bb','\x1bd']){assert.equal(x.b.input(Buffer.from(k)).length,0);}
  await new Promise(r=>setTimeout(r,40));assert.equal(x.b.focused,true);assert.equal(x.sent,undefined);}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_up_and_down_move_between_the_answers_rows(self):
        self.node(r"""
(async()=>{
 const shown=x=>x.caret(60,30);
 // Lines: the column is held while ↑↓ repeat (a short line between does not lose it).
 {const x=make();await x.send(['abcdef','\n','xy','\n','longer line']);
  await x.send(['\x1b[A']);assert.equal(shown(x),'  xy');await x.send(['\x1b[A']);assert.equal(shown(x),'› abcdef');
  await x.send(['\x1b[B','\x1b[B']);assert.equal(shown(x),'  longer line');
  await x.send(['\x01','\x1b[C','\x1b[A']);assert.equal(shown(x),'  x');}
 // Wide characters keep the cell column: two cells over `가`, one over `a`.
 {const x=make();await x.send(['가나다','\n','abcdef','\x01','\x1b[C','\x1b[C','\x1b[C','\x1b[C','\x1b[A']);assert.equal(shown(x),'› 가나');}
 // A wrapped line: ↑↓ go between its rows at the popup's width.
 {const x=make();await x.send(['0123456789'.repeat(12)]);const before=shown(x);await x.send(['\x1b[A']);const after=shown(x);
  assert.notEqual(before,after);assert(after.length<before.length+2,'one row up');await x.send(['X']);assert.equal(draft(x).replace(/\n/g,'').length,121);}
 // From the first row ↑ goes to the choices (the text kept); with no choices it reads the question, and ↓ on the last row reads on.
 {const x=make({choices:['one','two']});await x.send(['line one','\n','line two','\x1b[A','\x1b[A']);assert.equal(x.b.typing,false);assert.equal(draft(x),'line one\nline two');}
 {const x=make();await x.send(['a','\n','b','\x1b[B']);assert.equal(shown(x),'  b');}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_the_answer_grows_within_the_popup_and_follows_the_place(self):
        self.node(r"""
(async()=>{
 const lines=Array.from({length:30},(_,i)=>'줄 '+(i+1));
 for(const choices of [[],['one','two']])for(const budget of [24,12,8,4]){
  const x=make({choices,question:'Synthetic question? '.repeat(8)});await x.send(['\x1b[200~'+lines.join('\r')+'\x1b[201~']);
  const rows=x.rows(46,budget),text=rows.map(r=>r.map(p=>p.text).join(''));
  assert(rows.length<=budget,'within the popup '+budget);
  const answer=rows.map((r,i)=>[i,r]).filter(([,r])=>r.some(p=>p.answerRow)).map(([i])=>i);
  assert(answer.length>=1&&answer.every((v,i)=>!i||v===answer[i-1]+1),'one unbroken stretch');
  assert(text.some(t=>t.includes('줄 30')),'the place (the end) is in sight '+budget);
  if(budget>=12){
   assert(text[1].startsWith('Synthetic'),'the question keeps its first words');
   if(choices.length)assert(text.some(t=>t.includes('1  one'))&&text.some(t=>t.includes('2  two')),'the choices stay');
   assert(/Ctrl\+J/.test(text.at(-1)),'the key line stays: '+text.at(-1));
   assert(answer.length>1,'the answer has rows');
  }
  // ↑ follows the place up the rows shown, and Ctrl+Home brings the first line in.
  await x.send(['\x1b[1;5H']);const top=x.rows(46,budget).map(r=>r.map(p=>p.text).join(''));assert(top.some(t=>t.startsWith('› 줄 1')),'the start comes in '+budget);
  for(let i=0;i<29;i++)await x.send(['\x1b[B']);assert(x.rows(46,budget).map(r=>r.map(p=>p.text).join('')).some(t=>t.includes('줄 30')),'↓ brings the end back '+budget);
 }
 // Not focused, a long answer shows from its first row.
 {const x=make();await x.send(['\x1b[200~'+lines.join('\r')+'\x1b[201~']);x.b.leave();const text=x.rows(46,12).map(r=>r.map(p=>p.text).join(''));assert(text.some(t=>t.startsWith('› 줄 1')));}
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_key_line_names_the_break_key_in_both_languages_and_widths(self):
        import session_shell
        widths = (46, 48, 50, 54, 60, 64, 72, 100, 136)
        labels = {}
        for lang in ("en", "ko"):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
                # The backend's own words for each popup width (`session_shell.block_labels`, what the editor is sent).
                labels[lang] = {str(width): session_shell.block_labels(width) for width in widths}
        out = self.node(r"""
(async()=>{
 const all=labels,rows=[];
 for(const lang of ['en','ko'])for(const choices of [[],['one','two']])for(const popup of WIDTHS){
  const inner=popup-4,set=all[lang][String(popup)];
  let sent;const b=create({cellWidth:cells,changed(){},action(){},answer:async x=>(sent=x,{state:'submitted'})});
  b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Q?',free_text:true,choices}]}],'',{labels:set});b.enter();
  b.input(Buffer.from('글'));const last=b.detail({columns:inner,budget:20}).at(-1).map(p=>p.text).join('');
  // 11-26 follow-up, review 1861: while writing the row names Enter (it sends) and the line-break key at every
  // width from 46 cells; the closing key is shortened first (`뒤로`, then Esc alone under 50 cells).
  assert(cells(last)<=inner,last);assert(last.startsWith('Enter'),lang+' '+popup+': '+last);assert(last.includes('Ctrl+J'),lang+' '+popup+': '+last);
  assert(last.endsWith(lang==='en'?'back':popup<64?'뒤로':'돌아가기'),last);
  assert.equal(last.includes('Esc/Ctrl+]'),popup>=50,last);
  // `↑ choices` is named exactly where the row holding it (without `Tab next`) fits.
  if(choices.length){const back=set.navigate_write.split(' · ').at(-1),up=[...set.writing_choices.split(' · '),back].join(' · ');
   assert.equal(last.includes('↑'),cells(up)<=inner,popup+' '+last);}
  // A 50-cell popup (46 inside): the three keys, `Tab next` (and `↑ choices`) left out first.
  if(popup===50)assert.equal(last,lang==='en'?'Enter send · Ctrl+J newline · Esc/Ctrl+] back':'Enter 보내기 · Ctrl+J 줄바꿈 · Esc/Ctrl+] 뒤로');
  rows.push({lang,popup,choices:choices.length,keys:last});
 }
 console.log(JSON.stringify(rows));
})().catch(e=>{console.error(e);process.exit(1)});
""".replace("const all=labels,", "const all=" + json.dumps(labels, ensure_ascii=False) + ",")
                   .replace("WIDTHS", json.dumps(list(widths))))
        print("KEY_ROWS=" + out.strip(), flush=True)

    def test_a_sent_answer_of_several_lines_is_one_line_in_the_panel(self):
        self.node(r"""
(async()=>{
 const x=make();await x.send(['\x1b[200~1. 첫째\r2. 둘째\r3. 셋째\x1b[201~']);await x.enter();
 const lines=x.b.sent().map(r=>r.map(p=>p.text).join(''));assert.equal(lines.length,1);assert.equal(lines[0],'✓ answer sent · 1. 첫째 2. 둘째 3. 셋째');
})().catch(e=>{console.error(e);process.exit(1)});
""")

    def test_the_popup_takes_pastes_marked_while_it_has_the_keys(self):
        # host.cjs: bracketed paste stays on in the outer terminal while the question popup takes the keys, even if
        # the native turned it off, so a pasted line break never reaches the popup as Enter.
        source = (SHELL / "host.cjs").read_text(encoding="utf-8")
        self.assertIn("[2004,m.bracketedPasteMode||owning&&block.focused]", source)


class PrivateTmuxScenesTest(unittest.TestCase):
    """The real shell in a private tmux (54 columns: a 50-cell popup, Korean screen) with a synthetic native and a
    backend that records each answer: every line-break key, a multi-line paste, a long answer's rows, Korean."""

    def test_line_breaks_paste_and_a_long_answer_typed_in_the_popup(self):
        import importlib.util, re, shlex, shutil, signal, tempfile, time  # noqa: E401
        if not shutil.which("tmux"):
            self.skipTest("tmux unavailable")

        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        harness = load("am_harness", ROOT / "utilities/session_shell_question_input.test.py")
        fixture = load("am_backend", ROOT / "utilities/session_shell_wait_blink_pty.test.py")
        sid = harness.SID
        with tempfile.TemporaryDirectory(prefix="am-") as temp:
            root = Path(temp)
            runner = load("am_isolation", ROOT / "tools/run-tests.py")
            env = runner.build_isolated_env(root, repo_root=ROOT)
            env.update(FLEET_NOW_LANG="ko", CLAUDE_CONFIG_DIR=str(root / "native-config"))
            for key in ("cache", "devdir"):
                env["npm_config_" + key] = str(root / ("npm-" + key))
            env["npm_config_userconfig"] = str(root / "npmrc")
            (root / "npmrc").write_text("")
            sys.path[:0] = [str(ROOT / "tools/install"), str(ROOT / "utilities")]
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

            def screen(cursor=False):
                rows = tm("capture-pane", "-p", "-t", "qi:0.0").splitlines()
                if cursor:
                    x, y = map(int, tm("display-message", "-p", "-t", "qi:0.0", "#{cursor_x},#{cursor_y}").split(","))
                    while len(rows) <= y:
                        rows.append("")
                    line, used, at = rows[y], 0, 0
                    while at < len(line) and used < x:
                        used += harness.cells(line[at])
                        at += 1
                    rows[y] = line[:at] + "▌" + line[at:]
                return rows

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

            def popup(rows):
                top = next(i for i, row in enumerate(rows) if "╭─ ▶" in row)
                bottom = next(i for i, row in enumerate(rows) if i > top and "╰─ QUESTIONS" in row)
                return rows[top:bottom + 1]

            def ask(index, question, choices=(), item=None):
                q = dict(id="q", question=question, free_text=True, choices=list(choices))
                qp.write_text(json.dumps({sid: [dict(id=item or f"fixture-{index}", revision=f"{index + 1:064x}",
                                                     answerable=True, questions=[q])]}))
                waitfor(lambda rows: any(question[:8] in row for row in rows), "the question in the panel")
                send("\x1d")
                waitfor(lambda rows: any("╭" in row for row in rows), "the popup")

            try:
                waitfor(lambda rows: any("READY" in row for row in rows), "the native")
                # Scene 1: every line-break key, a paste with CR, CR LF and LF line breaks, Korean.
                # The native turns bracketed paste off first: the popup still takes the paste marked.
                send("\x02")
                mark = (case / "output").stat().st_size
                ask(0, "MESSAGES 구분선의 색을 정해 주세요. 글로 적어 주세요.")
                time.sleep(.4)
                self.assertIn(b"\x1b[?2004h", (case / "output").read_bytes()[mark:],
                              "the popup keeps pastes marked while the native has them off")
                for key in ("첫째 줄입니다", "\n", "둘째 줄", "\x1b\r", "셋째\\", "\r", "넷째",
                            "\x1b[200~가\r나\r\n다\x1b[201~", "\x1b[13;2u", "끝"):
                    send(key)
                rows = waitfor(lambda rows: any("끝" in row for row in rows), "the typed answer")
                picture = popup(screen(cursor=True))
                print("SCENE_KEYS=" + json.dumps(picture, ensure_ascii=False), flush=True)
                # 11-26 follow-up: a 50-cell popup names Enter while writing, in one row with the break key.
                self.assertTrue(any("│ Enter 보내기 · Ctrl+J 줄바꿈 · Esc/Ctrl+] 뒤로 │" in row for row in picture), picture)
                self.assertEqual(answers(), [], "no key sent it")
                send("\r")
                waitfor(lambda rows: any("synthetic recorded" in row for row in rows), "the recorded answer")
                self.assertEqual(answers(), [["첫째 줄입니다\n둘째 줄\n셋째\n넷째가\n나\n다\n끝"]])
                send("\x1d")
                # Scene 2: a long answer of thirty lines on a question with choices: the rows follow the place.
                ask(1, "여섯 가지를 한 번에 적어 주세요.", ["그대로", "고쳐서"])
                send("\x1b[200~" + "\r".join(f"{n}. 줄 {n}" for n in range(1, 31)) + "\x1b[201~", delay=.4)
                rows = waitfor(lambda rows: any("30. 줄 30" in row for row in rows), "the end of the long answer")
                end_picture = popup(screen(cursor=True))
                print("SCENE_LONG_END=" + json.dumps(end_picture, ensure_ascii=False), flush=True)
                self.assertTrue(any("1  그대로" in row for row in end_picture) and any("2  고쳐서" in row for row in end_picture),
                                "the choices stay in sight")
                for _ in range(3):
                    send("\x1b[A", delay=.08)
                send("\x1b[1;3D", delay=.08)
                send("+", delay=.3)
                print("SCENE_LONG_UP=" + json.dumps(popup(screen(cursor=True)), ensure_ascii=False), flush=True)
                middle = popup(screen())
                self.assertTrue(any("27. 줄 +27" in row for row in middle), middle)
                send("\x1b[1;5H", delay=.3)
                print("SCENE_LONG_START=" + json.dumps(popup(screen(cursor=True)), ensure_ascii=False), flush=True)
                start_picture = popup(screen())
                self.assertTrue(any("› 1. 줄 1" in row for row in start_picture), start_picture)
                send("\r")
                waitfor(lambda rows: any("synthetic recorded" in row for row in rows), "the long answer recorded")
                sent = answers()[-1]
                self.assertEqual(len(sent[-1].split("\n")), 30)
                self.assertIn("27. 줄 +27", sent[-1])
                send("\x1d")
                # Scene 3 (review 1861): a paste whose bytes stop for a second has stalled, not ended. The key row says
                # so and names the way out; what comes after is still the paste's text. Esc pressed then closes the
                # popup, Enter pressed sends what came, and the rest of that paste is never typed nor the native's.
                shown = lambda rows: any("╭─ ▶" in row for row in rows)  # noqa: E731
                stalled = lambda rows: any("붙여넣기 멈춤" in row for row in rows)  # noqa: E731

                def received():
                    path = case / "received"
                    return path.read_bytes() if path.exists() else b""
                ask(2, "끝나지 않는 붙여넣기 질문입니다.")
                native = len(received())
                send("\x1b[200~끝 표지가\r오지 않는다", delay=.1)
                waitfor(stalled, "the stalled paste's key row")
                picture = popup(screen(cursor=True))
                print("SCENE_STALLED=" + json.dumps(picture, ensure_ascii=False), flush=True)
                self.assertTrue(any("│ 붙여넣기 멈춤 · Enter 보내기 · Esc/Ctrl+] 뒤로 │" in row for row in picture), picture)
                send("!\t?", delay=.3)
                waitfor(lambda rows: any("오지 않는다!" in row for row in rows), "the rest of the paste as its text")
                self.assertTrue(shown(screen()), "a tab of the paste is no Tab key")
                waitfor(stalled, "stalled again")
                send("\x1b", delay=.5)
                waitfor(lambda rows: not shown(rows), "the popup closed by Esc pressed")
                send("\x1b[201~", delay=.3)
                self.assertEqual(received()[native:], b"", "the paste's late end mark is not the native's")
                self.assertEqual(answers(), [["첫째 줄입니다\n둘째 줄\n셋째\n넷째가\n나\n다\n끝"], sent])
                send("\x1d")
                waitfor(shown, "the popup again")
                send("\x1b[200~ 더", delay=.1)
                waitfor(stalled, "a stall again")
                send("\r")
                waitfor(lambda rows: any("synthetic recorded" in row for row in rows), "what came sent by Enter pressed")
                self.assertEqual(answers()[-1], ["끝 표지가\n오지 않는다!\t? 더"])
                send("나머지\r끝\x1b[201~", delay=.5)
                self.assertEqual(len(answers()), 3, "the rest of that paste sends nothing")
                self.assertFalse(any("나머지" in row for row in screen()))
                self.assertEqual(received()[native:], b"")
                send("\x1d", delay=.3)
                # The review's scenes (RV-PR1861 🔴1, 🟡1): bytes of one paste cut by a stall (or by a Ctrl+] byte read
                # alone). None sends a half-written answer or the same twice, and none reaches the native.
                scenes = (("A", "fixture-a", [("\x1b[200~첫 줄\r둘", 1.4), ("째 줄\r셋째 줄\x1b[201~", 1.0)], ["첫 줄\n둘째 줄\n셋째 줄"]),
                          ("B-submitted", "fixture-submitted-b",
                           [("\x1b[200~하나\r둘", 1.4), ("째\r", .6), ("셋째\r넷째\x1b[201~", 1.0)], ["하나\n둘째\n셋째\n넷째"]),
                          ("B-unknown", "fixture-b", [("\x1b[200~하나\r둘", 1.4), ("째\r", .6), ("셋째\r넷째\x1b[201~", 1.0)],
                           ["하나\n둘째\n셋째\n넷째"]),
                          ("C", "fixture-c", [("\x1b[200~로그 앞", 1.4), ("\x1b", .2), ("[31m빨강\x1b[0m 다음\r끝\x1b[201~", 1.0)], None),
                          ("E", "fixture-e", [("\x1b[200~앞 글", .3), ("\x1d", .3), ("뒤 글\r다음 줄\x1b[201~", 1.0)], None))
                for number, (name, item, steps, whole) in enumerate(scenes):
                    ask(10 + number, name + " 장면의 느린 붙여넣기 질문입니다.", item=item)
                    count, native = len(answers()), len(received())
                    for data, pause in steps:
                        send(data, delay=pause)
                    result = {"scene": name, "sent": answers()[count:], "native": received()[native:].decode("utf-8", "replace"),
                              "popup": shown(screen())}
                    self.assertEqual(result["sent"], [], name + ": no half-written answer")
                    self.assertEqual(result["native"], "", name + ": the paste is never the native's")
                    if whole is not None:
                        self.assertTrue(result["popup"], name)
                        send("\r", delay=1.0)    # the person's Enter: the whole paste, once
                        result["after_enter"] = answers()[count:]
                        self.assertEqual(result["after_enter"], [whole], name)
                    elif name == "E":
                        self.assertFalse(result["popup"], "Ctrl+] pressed closed it")
                    print("SCENE_REVIEW=" + json.dumps(result, ensure_ascii=False), flush=True)
                    if shown(screen()):
                        send("\x1d", delay=.3)
                    self.assertEqual(received()[native:], b"", name + ": nothing of it reached the native")
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
