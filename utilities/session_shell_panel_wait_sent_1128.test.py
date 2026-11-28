"""11-28: the panel's questions read as the sent line does; a press on a sent line opens that answer's question.

The person (11-28, watching the panel): "그 이전에 답을 보낸것과 그 아래에 질문을 요구하는것이 서로 좀 사용자 입장에서
헷갈리는데? … 선지는 미리 보기를 날리고 대기중인 선지는 구분이 좀 되게끔 (앞에 답 대기를 붉은 색으로 쓴다던지해서 답 보냄과
표현 일관성을 맞추되 구분이 되게끔)" and "답 보냄의 내용을 누르면 그것과 관련된게 여전히 뜨게끔 해야지 … (그게 번거롭다면 그냥
내용만 확인하던가". Each wait is one row `◑ 답 대기 · <its first line>` beside `✓ 답 보냄 · <what was sent>`, three at most
with the hidden count; no choices row in the panel; a press on a sent line opens that question with the answer sent (a secret
stays `•••`), a press on a wait's row that wait. Outside the question box the panel is main's, byte for byte. These run the
real backend presentation, the real editor and the real panel paint with synthetic values.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "utilities")]
# The panel as it stood before this change (main at v4.81.4): what lies outside the question box stays this.
BASE = "0ea6458b3"

WAIT = ("[서브 호출 → unit route] 이 환경에서는 하위 에이전트를 직접 부를 수 없어 unit route로 돌려야 합니다. "
        "도감의 표지 색인 정리를 unit route로 넘길까요?")
CHOICES = ["unit route로 넘김", "이 창에서 직접", "나중에"]


def presented(panel, lang="ko"):
    """The backend's items for one sent answer, three waits and a sent secret, and the block's words."""
    from fleet.session_block import question_presentations
    import session_shell
    items = [{"id": "S4:t:timing", "revision": "b" * 64, "answerable": False, "request_state": "submitted",
              "sent": ["쉬는 때에만 넘김"], "questions": [{"id": "when", "question": "표지 색인 정리를 언제 넘길까요?\n지금 넘기면 끊깁니다.",
                                                      "choices": ["지금 바로", "쉬는 때에만 넘김"]}]},
             {"id": "S4:t:sub", "revision": "a" * 64, "answerable": True,
              "questions": [{"id": "call", "question": WAIT, "choices": CHOICES, "recommended": 0}]},
             {"id": "S2:rt:frame", "revision": "c" * 64, "answerable": True, "answer_kind": "decision",
              "questions": [{"id": "frame", "question": "[route 관문] 지도 장 — 연못 지도를 넣는 안으로 진행할까요?"}]},
             {"id": "S4:t:color", "revision": "d" * 64, "answerable": True,
              "questions": [{"id": "color", "question": "`docs/maps/` 구분선의 색을 지금처럼 둘까요?", "choices": ["옅은 잿빛", "한 단계 밝게"]}]},
             {"id": "S4:t:token", "revision": "e" * 64, "answerable": False, "request_state": "submitted", "sent": ["•••"],
              "questions": [{"id": "token", "question": "시험 기계의 사설 접속 토큰을 넣어 주세요.", "secret": True}]}]
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
        question_presentations(items, 76, brief_columns=panel - 4)
        labels = session_shell.block_labels(76)
    for item in items:
        for question in item["questions"]:
            question["display"] = {key: ([[{"text": t, "key": k} for t, k in line] for line in value]
                                         if key not in ("head", "brief_line") else [{"text": t, "key": k} for t, k in value])
                                   for key, value in question["display"].items()}
    return items, labels


class PanelWaitSentTest(unittest.TestCase):
    def node(self, script, *args):
        result = subprocess.run(["node", "-e", script, str(SHELL), *args], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_outside_the_question_box_the_panel_is_mains_byte_for_byte(self):
        # A panel with no wait and no sent answer, and every section outside the question box, are main's (`BASE`).
        base = subprocess.check_output(["git", "show", BASE + ":utilities/session-shell/panel-view.cjs"], cwd=ROOT, text=True, timeout=10)
        with tempfile.TemporaryDirectory() as scratch:
            source = Path(scratch) / "panel-view-base.cjs"
            source.write_text(base)
            self._outside_the_box(str(source))

    def _outside_the_box(self, source):
        self.node(r"""
const assert=require('node:assert/strict'),Module=require('node:module'),fs=require('node:fs'),dir=process.argv[1];
const old=new Module(dir+'/panel-view.cjs');old.filename=dir+'/panel-view.cjs';old.paths=module.paths;old._compile(fs.readFileSync(process.argv[2],'utf8'),old.filename);
const was=old.exports,now=require(dir+'/panel-view.cjs');
const P=(t,k)=>({text:t,...(k==='dim'?{dim:true}:k==='red'?{foreground:217}:{})});
const model={tag:'[e9]',repo:[P('hearting')],selected:[P('Opus 5.5 xhigh')],session_title:'도감 표지와 지도',now:'그리는 중',
 route_label:'route 1 ⠏1',route_count:1,route_total:1,routes:[{id:'r1',title:'연못 지도 그리기',kind:'working',paint:{glyph:P('⠏')}}],
 messages:[{way:'←',who:'[e7]',text:'지도 초안을 올렸습니다',at:1},{way:'→',who:'[73]',text:'설치 확인 부탁',at:2}],
 memory_items:[{tier:'durable',kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'}],quota:[P('5h 18%'),P(' │ '),P('7d 64%')]};
const str=r=>r.map(p=>p.text).join('');
// (a) No wait and no sent answer: every row, every press the same.
for(const side of [true,false])for(const width of side?[36,44,50,60,69]:[100,140])for(const height of [8,12,18,24,30,40]){
 const args={width,height,side,model,state:'idle',harness:'claude',configuration:{},frame:0,brief:()=>null,sent:()=>[]};
 const a=now.render(args),b=was.render(args);
 assert.deepEqual(a.rows,b.rows,width+'x'+height+' '+side);assert.deepEqual(a.hits,b.hits,width+'x'+height+' '+side);
}
// (b) One sent answer and one wait, where nothing folds: the rows before the box, its top and bottom lines, and the
// rows after it are main's (main draws the same wait with its choices' row, which this change takes out).
const sent=()=>[Object.assign([P('✓ 답 보냄'),P(' · ','dim'),P('쉬는 때에만 넘김')],{open:'S4:s@r'})];
const fresh={count:1,label:'QUESTIONS',answerable:true,waits:[{word:'답 대기',answerable:true,open:'S4:w@r',line:[P('도감 표지에 어떤 이름을 붙일까요?')]}]};
const before={count:1,label:'QUESTIONS',answerable:true,choices:2,rows:[[P('◑ ','red'),P('도감 표지에 어떤 이름을 붙일까요?')],[P('  ','dim'),P('1 ','dim'),P('Dogwood'),P(' · ','dim'),P('2 ','dim'),P('들꽃')]],
 one:[[P('◑ ','red'),P('도감 표지에 어떤 이름을 붙일까요?')]],line:[P('도감 표지에 어떤 이름을 붙일까요?')]};
for(const width of [36,44,50,60,69]){
 const args=q=>({width,height:44,side:true,model:{...model,wait:'◑답 대기 1'},state:'idle',harness:'claude',configuration:{},frame:0,brief:()=>q,sent});
 const a=now.render(args(fresh)),b=was.render(args(before));
 const split=v=>{const rows=v.rows.map(str),top=rows.findIndex(s=>/╭─ 1 QUESTIONS/.test(s)),end=rows.findIndex((s,i)=>i>top&&/^│ ╰/.test(s));
  assert(top>0&&end>top,rows.join('\n'));return {top,end,above:v.rows.slice(0,top+1),bottom:v.rows[end],below:v.rows.slice(end+1).filter(r=>!/^│ +│$/.test(str(r)))};};
 const x=split(a),y=split(b);
 assert.deepEqual(x.above,y.above,width+' above');assert.deepEqual(x.bottom,y.bottom,width+' bottom line');assert.deepEqual(x.below,y.below,width+' below');
 // The box is one row shorter (no choices row); the rest of it: the sent line as it was, then the wait's row, then the action row.
 assert.equal(x.end-x.top,y.end-y.top-1,width);
 assert.deepEqual(a.rows[x.top+1],b.rows[y.top+1],width+' the sent line as it was');
}
""", source)

    def test_each_wait_is_one_row_beside_the_sent_lines_and_its_choices_are_the_popups(self):
        # The real backend's items, the real editor: `✓ 답 보냄 · …` lines, then `◑ 답 대기 · <first line>` rows (three at most),
        # the word being the folded strip's (`answer_wait_word`), whole at every width; no choice of any wait in the panel.
        from fleet.session_line import ANSWER_WAIT_WORDS
        for lang in ("ko", "en"):
            for width in (36, 44, 50, 60, 69):
                items, labels = presented(width, lang)
                self.assertEqual(labels["waiting"], ANSWER_WAIT_WORDS[lang])
                self.node(r"""
const assert=require('node:assert/strict'),dir=process.argv[1],view=require(dir+'/panel-view.cjs'),create=require(dir+'/block-input.cjs');
const items=JSON.parse(process.argv[2]),labels=JSON.parse(process.argv[3]),width=Number(process.argv[4]),lang=process.argv[5];
const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update(items,'',{labels,styles:{}});
const word=labels.waiting.replace(/^◑\s*/u,''),sentWord=labels.sent;
const r=view.render({width,height:40,side:true,model:{tag:'[e9]',wait:labels.waiting+' 3'},state:'idle',harness:'claude',configuration:{},brief:()=>b.summary(),sent:()=>b.sent()});
const rows=r.rows.map(x=>x.map(p=>p.text).join('')),all=rows.join('\n'),tag=width+' '+lang+'\n'+all;
const top=rows.findIndex(s=>/QUESTIONS/.test(s)),end=rows.findIndex((s,i)=>i>top&&/^│ ╰/.test(s)),box=rows.slice(top+1,end);
// Sent lines first, then the waits (`◑ <word> · …`), then the action row.
assert(box[0].includes(sentWord+' · 쉬는 때에만')&&box[1].includes(sentWord+' · •••'),tag);
for(const k of [2,3,4])assert(box[k].includes('◑ '+word+' ·'),tag);
assert(box[5].includes('↵'),tag);
// The words of each wait follow the word, cut before it; no choice of any wait stands anywhere in the panel.
assert(box[2].includes('· [서브')&&box[3].includes('· [route')&&box[4].includes('· `docs'),tag);
for(const c of ['unit route로 넘김','이 창에서 직접','나중에','옅은 잿빛','한 단계 밝게','approve','reject'])assert(!all.includes(c),c+'\n'+tag);
assert(!/│ (?:│ )? *1 \S/.test(all),tag);
// A press on each sent line and each wait's row opens that one; the rest of the box the first wait as before.
const act=i=>r.hits.filter(h=>h.row===i).map(h=>h.action);
assert.deepEqual([0,1,2,3,4].map(k=>act(top+1+k)[0]),['question:S4:t:timing@'+'b'.repeat(64),'question:S4:t:token@'+'e'.repeat(64),
 'question:S4:t:sub@'+'a'.repeat(64),'question:S2:rt:frame@'+'c'.repeat(64),'question:S4:t:color@'+'d'.repeat(64)]);
for(const i of [top,top+6,end])assert.deepEqual(act(i),['question'],String(i));
console.log('ok');
""", json.dumps(items, ensure_ascii=False), json.dumps(labels, ensure_ascii=False), str(width), lang)

    def test_a_press_on_a_sent_line_opens_its_question_and_a_wait_row_its_own(self):
        # The editor opens on the row pressed (`enter(<id@revision>)`): a sent answer's question to read, its words and the
        # answer sent; a wait's own question; nothing named, the question it would open on before. Board updates and Tab
        # leave the read-only view in place, and a press on a wait switches to that question.
        items, labels = presented(69)
        self.node(r"""
const assert=require('node:assert/strict'),dir=process.argv[1],create=require(dir+'/block-input.cjs');
const items=JSON.parse(process.argv[2]),labels=JSON.parse(process.argv[3]);
let answered=0,acted=0;
const b=create({changed(){},answer:async()=>(answered++,{state:'submitted'}),action(){acted++;}});b.update(items,'',{labels,styles:{}});
const words=()=>b.detail({budget:20,columns:72}).map(r=>r.map(p=>p.text).join('')).join('\n');
const key=i=>items[i].id+'@'+items[i].revision;
b.enter(key(0));let w=words();
assert(b.focused&&w.includes('표지 색인 정리를 언제 넘길까요?')&&w.includes(labels.sent+' · 쉬는 때에만 넘김'),w);
assert(!w.includes('[서브 호출')&&!w.includes('연못 지도'),w);
// Its key hint is for closing. Enter answers nothing.
b.input(Buffer.from('\r'));assert.equal(answered,0);
// A projection while it is read keeps it on screen.
b.update(JSON.parse(process.argv[2]),'',{labels,styles:{}});assert(b.focused&&words().includes('표지 색인 정리를 언제'),words());
b.input(Buffer.from('\t'));assert(words().includes('표지 색인 정리를 언제'),words());
// A wait's row opens on that wait, including from the sent view.
b.enter(key(3));w=words();assert(w.includes('`docs/maps/`')&&!w.includes('[서브 호출'),w);b.leave();
b.enter(key(2));w=words();assert(w.includes('[route 관문]'),w);b.leave();
// Nothing named (the head, the action row, Ctrl+]) opens where it did before: the question it stood on, the first wait
// in a fresh editor; so does a name the editor does not hold (a line already gone).
b.enter('');w=words();assert(w.includes('[route 관문]'),w);b.leave();
b.enter('S4:gone@'+'f'.repeat(64));w=words();assert(w.includes('[route 관문]'),w);b.leave();
{const c=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});c.update(JSON.parse(process.argv[2]),'',{labels,styles:{}});
 c.enter('');const x=c.detail({budget:20,columns:72}).map(r=>r.map(p=>p.text).join('')).join('\n');assert(x.includes('[서브 호출'),x);}
// Read while its line goes: it stays the answer pressed, even in a read containing Tab and Enter together.
b.enter(key(0));b.update(JSON.parse(process.argv[2]).filter((_,i)=>i!==0),'',{labels,styles:{}});w=words();
assert(b.focused&&w.includes('표지 색인 정리를 언제')&&w.includes(labels.sent+' · 쉬는 때에만 넘김')&&!w.includes('[서브 호출'),w);
assert.equal(b.input(Buffer.from('\t\r')).length,0);assert.equal(words(),w);
b.input(Buffer.from('\x1d'));assert(!b.focused);
b.enter('');w=words();assert(w.includes('[route 관문]')&&!w.includes('표지 색인 정리를 언제'),w);b.leave();
assert.equal(acted,0);assert.equal(answered,0);
console.log('ok');
""", json.dumps(items, ensure_ascii=False), json.dumps(labels, ensure_ascii=False))

    def test_a_secret_sent_answer_opens_hidden(self):
        # A secret answered in this window: its line and its popup say `•••`, never the secret nor its length; opening it
        # asks nothing of the backend (the editor's only call is the one answer).
        self.node(r"""
const assert=require('node:assert/strict'),dir=process.argv[1],create=require(dir+'/block-input.cjs');
const SECRET='hunter2-private-token';
let calls=0;const b=create({changed(){},answer:async()=>(calls++,{state:'submitted'}),action(){calls++;}});
const item={id:'S4:t:token',revision:'e'.repeat(64),answerable:true,questions:[{id:'token',question:'사설 접속 토큰을 넣어 주세요.',secret:true}]};
b.update([item],'',{labels:{sent:'✓ 답 보냄'},styles:{}});b.enter();b.input(Buffer.from(SECRET));b.input(Buffer.from('\r'));
(async()=>{await new Promise(r=>setImmediate(r));
 assert.equal(calls,1);
 // The backend's next board: the answer on its way, with no values (this window's own are what it holds).
 const sent={...item,answerable:false,request_state:'submitted'};b.update([sent],'',{labels:{sent:'✓ 답 보냄'},styles:{}});
 const line=b.sent().map(r=>r.map(p=>p.text).join(''));assert.deepEqual(line,['✓ 답 보냄 · •••']);
 b.enter(b.sent()[0].open);
 const all=b.detail({budget:20,columns:72}).map(r=>r.map(p=>p.text).join('')).join('\n');
 assert(all.includes('사설 접속 토큰')&&all.includes('✓ 답 보냄 · •••'),all);
 assert(!all.includes(SECRET)&&!all.includes('hunter')&&!all.includes('••••'),all);
 assert.equal(calls,1);
 console.log('ok');})().catch(e=>{console.error(e);process.exit(1);});
""")


def block_words(lang="ko"):
    """The block's words as the shell gives them (`block_labels`), in one language."""
    import session_shell
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
        return session_shell.block_labels(76)


# The editor's items for the two reviews of #1957.
EDITOR = r"""
const assert=require('node:assert/strict'),dir=process.argv[1],create=require(dir+'/block-input.cjs');
const labels=JSON.parse(process.argv[2]);
const S={id:'S4:t:timing',revision:'b'.repeat(64),answerable:false,request_state:'submitted',sent:['쉬는 때에만 넘김'],
 questions:[{id:'when',question:'표지 색인 정리를 언제 넘길까요?',choices:['지금 바로','쉬는 때에만 넘김']}]};
const W=(id,question)=>({id:'S4:t:'+id,revision:id[0].repeat(64),answerable:true,questions:[{id,question}]});
const A=W('alpha','알파 질문에 글로 답해 주세요.'),B=W('beta','베타 질문에 글로 답해 주세요.'),C=W('cover','표지 글을 적어 주세요.');
const firstChoice={...A,questions:[{...A.questions[0],choices:['첫 선지','둘째 선지']}]};
const key=item=>item.id+'@'+item.revision;
let answered=0;
const make=()=>create({changed(){},answer:async()=>(answered++,{state:'submitted'}),action(){}});
const put=(b,rows)=>b.update(JSON.parse(JSON.stringify(rows)),'',{labels,styles:{}});
const words=b=>b.detail({budget:20,columns:72}).map(r=>r.map(p=>p.text).join('')).join('\n');
const reading=(b,tag)=>{const w=words(b);assert(w.includes('표지 색인 정리를 언제')&&w.includes(labels.sent+' · 쉬는 때에만 넘김'),tag+'\n'+w);return w;};
const press=(b,keys)=>assert.equal(b.input(Buffer.from(keys)).length,0,JSON.stringify(keys)+' reached native input');
const close=b=>{press(b,'\x1d');assert(!b.focused);};
const escape=async b=>{press(b,'\x1b');await new Promise(r=>setTimeout(r,35));assert(!b.focused);};
const tick=()=>new Promise(r=>setImmediate(r));
"""

class SentAnswerReadTest(unittest.TestCase):
    """Both reviews' reproductions on the real editor."""

    def node(self, script):
        result = subprocess.run(["node", "-e", EDITOR + script, str(SHELL), json.dumps(block_words(), ensure_ascii=False)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_sent_answer_read_stays_the_one_pressed_and_a_paste_reaches_no_question(self):
        # Review 🔴1: S read, a paste starts, S leaves the board, the rest of the paste and its end mark (whole, or split
        # across two reads), then Enter twice: no question's draft holds a letter and nothing is answered; the popup still
        # shows S's words and its sent line. Close it to check each waiting question's draft.
        self.node(r"""
(async()=>{
 for(const end of [['\x1b[201~'],['\x1b[20','1~']]){
  answered=0;const b=make();put(b,[S,C,A]);b.enter(key(S));reading(b,'opened');
  press(b,'\x1b[200~FRONT-');put(b,[C,A]);
  press(b,'CROSSED-TAIL');for(const part of end)press(b,part);
  press(b,'\r');await tick();press(b,'\r');await tick();
  const tag=JSON.stringify(end);
  reading(b,tag);assert.equal(answered,0,tag);assert(!b.typing,tag);
  close(b);
  for(const wait of [C,A]){b.enter(key(wait));const w=words(b);assert(!/FRONT|CROSSED/.test(w),tag+'\n'+w);close(b);}
  assert.equal(answered,0,tag);
 }
 console.log('ok');})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_a_sent_answer_read_keeps_its_line_past_ten_minutes(self):
        # The panel keeps a pending delivery past ten minutes and expires a receipt at ten minutes from sending.
        # The view keeps the sent line even after the panel drops it.
        self.node(r"""
let T=1e12;Date.now=()=>T;
const b=make();put(b,[S,C]);
T+=600001;assert.equal(b.sent().length,1,'a pending delivery stays listed');
b.enter(b.sent()[0].open);reading(b,'opened while delivery was pending');
put(b,[{...S,request_state:'received'},C]);reading(b,'received after ten minutes');assert.equal(b.sent().length,0);
close(b);
const quick=make();put(quick,[S,C]);put(quick,[{...S,request_state:'applied'},C]);
assert.equal(quick.sent().length,1);quick.enter(quick.sent()[0].open);reading(quick,'opened after application');
T+=599999;assert.equal(quick.sent().length,1);
T+=1;reading(quick,'painted at ten minutes');assert.equal(quick.sent().length,0);
put(quick,[{...S,request_state:'applied'},C]);reading(quick,'the next board');assert.equal(quick.sent().length,0);
close(quick);quick.enter();const w=words(quick);assert(w.includes('표지 글을 적어'),w);close(quick);
console.log('ok');
""")

    def test_a_pressed_sent_answer_comes_before_a_changed_question_kept_with_its_draft(self):
        # Review 🔴2①: C typed to, C leaves the board (kept with its draft), the popup closed; a press on S's line shows S.
        # Closing the view and reopening with Ctrl+] shows C, its draft and its changed-question word.
        self.node(r"""
const b=make();put(b,[C,A,S]);b.enter('');b.input(Buffer.from('mydraft'));put(b,[A,S]);b.leave();
b.enter(key(S));const w=reading(b,'pressed');assert(!w.includes('mydraft')&&!w.includes('표지 글을 적어'),w);
close(b);b.enter();const back=words(b);
assert(back.includes('표지 글을 적어')&&back.includes('mydraft')&&back.includes(labels.changed),back);
console.log('ok');
""")

    def test_a_reopened_request_never_accepts_input_through_its_sent_view(self):
        # Review A. The second case presses a still-listed sent line backed by a local submitted state and an open
        # projection. When the first request returns open, the paste must still leave the open question's draft alone.
        self.node(r"""
(async()=>{
 for(const earlyOpen of [false,true]){
  const gate={...A,answer_kind:'decision'},calls=[];
  let finish;
  const b=create({changed(){},answer(fields){calls.push(fields);return new Promise(r=>{finish=r;});},action(){}});
  const question={id:'when',question:S.questions[0].question,
    ...(earlyOpen?{choices:['first draft'],free_text:true}:{})};
  const open={id:S.id,revision:S.revision,answerable:true,questions:[question]};
  put(b,[open,gate]);b.enter();press(b,earlyOpen?'1':'first draft');press(b,'\r');
  assert.equal(calls.length,1);assert.deepEqual(calls[0].answers,{when:['first draft']});
  press(b,'\t');assert(words(b).includes('알파 질문'),words(b));
  put(b,[gate,{...open,answerable:false,request_state:'submitted',sent:['first draft']}]);
  close(b);
  if(earlyOpen)put(b,[gate,open]);
  b.enter(b.sent()[0].open);
  const photo=words(b);assert(photo.includes(labels.sent+' · first draft'),photo);
  press(b,'\x1b[200~FRONT-');put(b,[gate]);put(b,[gate,open]);
  finish({state:'open',reason:'delivery refused'});await tick();
  press(b,'CROSSED-TAIL\x1b[201~\r');await tick();
  assert.equal(calls.length,1);assert(b.focused);assert(!b.typing);
  // A fallback title's i/N follows the list. The question, answer and key line belong to the view.
  assert.deepEqual(words(b).split('\n').slice(1),photo.split('\n').slice(1));
  close(b);b.enter(key(open));const draft=words(b);
  assert(!/FRONT|CROSSED-TAIL/.test(draft),draft);close(b);
 }
 console.log('ok');})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_a_sent_view_keeps_the_pressed_words_when_the_same_id_is_sent_again(self):
        self.node(r"""
for(const revision of [S.revision,'d'.repeat(64)]){
 const b=make();put(b,[A,S]);b.enter(b.sent()[0].open);const photo=reading(b,'pressed');
 put(b,[A]);
 const again={...S,revision,sent:['다른 보낸 답'],questions:[{...S.questions[0],question:'다시 올린 질문'}]};
 put(b,[A,again]);
 assert(b.sent()[0].some(part=>part.text==='다른 보낸 답'));
 press(b,'1ab\r');assert.equal(answered,0);assert.equal(words(b),photo);close(b);
}
console.log('ok');
""")

    def test_reading_keeps_the_locally_sent_record_through_other_questions(self):
        # The board carries no sent value for this answer. Its local value backs the panel's line when it returns.
        self.node(r"""
(async()=>{
 const b=make();
 put(b,[C]);b.enter();press(b,'내가 보낸 값\r');await tick();assert(!b.focused);assert.equal(answered,1);
 const sent={...C,answerable:false,request_state:'submitted'};
 put(b,[sent]);b.enter(b.sent()[0].open);
 const crowd=Array.from({length:12},(_,i)=>W('wait'+i,'다른 질문 '+i));put(b,crowd);
 press(b,'\x1b[200~FOREIGN\x1b[201~\r');await tick();
 const photo=words(b);assert(photo.includes('표지 글을 적어')&&photo.includes(labels.sent+' · 내가 보낸 값'),photo);
 assert(!b.typing&&b.focused);assert.equal(answered,1);
 put(b,[...crowd,sent]);assert.equal(b.sent()[0].map(part=>part.text).join(''),labels.sent+' · 내가 보낸 값');
 close(b);})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_tab_enter_and_paste_keep_the_sent_view_and_reach_no_native_input(self):
        self.node(r"""
(async()=>{
 for(const reads of [['\t'],['\t\r'],['\t','\x1b[200~FRONT-','TAIL\x1b[201~','\r'],['\x1b[Z','x1\r']]){
  const b=make();put(b,[firstChoice,B,C,S]);b.enter(key(C));b.enter(b.sent()[0].open);
  put(b,[firstChoice,B,S]);
  const photo=reading(b,'C left the board');
  for(const keys of reads)press(b,keys);
  await tick();assert.equal(answered,0);assert.equal(words(b),photo);assert(b.focused&&!b.typing);
  assert.equal(photo.split('\n').at(-1),labels.navigate.split(' · ').at(-1),photo);
  assert(!photo.includes('Tab'),photo);await escape(b);
 }
 console.log('ok');})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_board_updates_under_the_sent_view_use_the_normal_question_position(self):
        # The control follows the same board through the ordinary popup without pressing the sent line.
        self.node(r"""
(async()=>{
 for(const [start,draft,boards,question,changed] of [
   [C,'',[[A,B,S]],'베타 질문',true],
   [C,'my draft',[[A,B,S]],'표지 글을 적어',true],
   [C,'',[[S],[A,B,S]],'알파 질문',false],
   [B,'',[[C,A,B,S]],'베타 질문',false],
 ]){
  const [view,plain]=[make(),make()];
  for(const b of [view,plain]){put(b,[A,B,C,S]);b.enter(key(start));if(draft)press(b,draft);}
  view.enter(view.sent()[0].open);
  for(const board of boards){put(view,board);put(plain,board);}
  const photo=reading(view,'kept during the board updates');
  assert(!photo.includes(labels.changed)&&!photo.includes('my draft'),photo);
  for(const b of [view,plain]){await escape(b);b.enter();}
  const restored=words(view);assert.equal(restored,words(plain));
  assert(restored.includes(question),restored);assert.equal(restored.includes(labels.changed),changed,restored);
  if(draft)assert(restored.includes(draft),restored);
  for(const b of [view,plain])close(b);
 }
 assert.equal(answered,0);
 console.log('ok');})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_a_long_sent_question_can_still_be_scrolled(self):
        self.node(r"""
const lines=Array.from({length:18},(_,i)=>[{text:'질문 본문 '+(i+1)}]);
const sent={...S,questions:[{...S.questions[0],display:{body:lines}}]};
const b=make();put(b,[A,C,sent]);b.enter(key(C));b.enter(b.sent()[0].open);
const first=()=>b.detail({budget:8,columns:72})[1][0].text;
assert.equal(first(),'질문 본문 1');
press(b,'\x1b[B');assert.equal(first(),'질문 본문 2');
press(b,'\x1b[6~');assert.equal(first(),'질문 본문 6');
put(b,[A,sent]);assert.equal(first(),'질문 본문 6');
b.wheel(1);assert.equal(first(),'질문 본문 7');
press(b,'\x1b[5~\x1b[A');assert.equal(first(),'질문 본문 2');
assert.equal(answered,0);close(b);
console.log('ok');
""")


def styled_items(items, panel=50):
    """The backend's presentation of `items` at a panel's width, its parts as the editor receives them."""
    from fleet.session_block import question_presentations
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
        question_presentations(items, 76, brief_columns=panel - 4)
    for item in items:
        for question in item["questions"]:
            question["display"] = {key: ([[{"text": t, "key": k} for t, k in line] for line in value]
                                         if key not in ("head", "brief_line") else [{"text": t, "key": k} for t, k in value])
                                   for key, value in question["display"].items()}
    return items


PANEL = r"""
const assert=require('node:assert/strict'),dir=process.argv[1],view=require(dir+'/panel-view.cjs'),create=require(dir+'/block-input.cjs');
const items=JSON.parse(process.argv[2]),labels=JSON.parse(process.argv[3]),word=labels.waiting.replace(/^◑\s*/u,'');
const paint=(b,width,model)=>{const r=view.render({width,height:40,side:true,model:{tag:'[e9]',...model},state:'idle',harness:'claude',
 configuration:{},brief:()=>b.summary(),sent:()=>b.sent()});
 const rows=r.rows.map(x=>x.map(p=>p.text).join('')),top=rows.findIndex(s=>/QUESTIONS/.test(s)),end=rows.findIndex((s,i)=>i>top&&/^│ ╰/.test(s));
 return {r,rows,top,end,box:rows.slice(top+1,end),all:rows.join('\n')};};
"""


class WaitRowsFix1Test(unittest.TestCase):
    """Fix 1 of #1957: the waiting rows are the requests waiting here; their line is the first that draws."""

    def node(self, script, items):
        result = subprocess.run(["node", "-e", PANEL + script, str(SHELL), json.dumps(items, ensure_ascii=False),
                                 json.dumps(block_words(), ensure_ascii=False)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_display_reference_of_a_request_gone_has_no_row_and_no_count(self):
        # Review 🔴7: five waits, a display reference of a request that has left this session's waits (`retained-display-
        # no-longer-current`) and one wait answered elsewhere (the guide line): no row is the closed one's and the hidden
        # count is `+2`; the head's count, the popup's `i/N` and Tab's range stay main's (the closed one is in them). With
        # one wait before it, it has no row either. A question gone with its draft kept changes no row's word.
        waits = [{"id": f"S4:t:w{n}", "revision": str(n) * 64, "answerable": True,
                  "questions": [{"id": f"w{n}", "question": f"대기 {n}번 질문에 글로 답해 주세요."}]} for n in range(1, 6)]
        gone = {"id": "S4:t:gone", "revision": "9" * 64, "answerable": False, "reason": "retained-display-no-longer-current",
                "questions": [{"id": "gone", "question": "ZZ 이 세션의 대기를 떠난 질문"}]}
        self.node(r"""
const guide='1 QUESTIONS · 아래 창';
for(const width of [50,69]){
 const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update(items,'',{labels,styles:{}});
 const v=paint(b,width,{wait:labels.waiting+' 6',wait_guide:guide}),tag=width+'\n'+v.all;
 assert(/╭─ 6 QUESTIONS/.test(v.rows[v.top]),tag);assert.equal(b.summary().count,6,tag);
 assert(!v.all.includes('ZZ'),tag);assert.equal(b.summary().waits.length,5,tag);
 assert(v.box[0].includes('◑ '+word+' · 대기 1번')&&v.box[1].includes('대기 2번')&&v.box[2].includes('대기 3번'),tag);
 assert(/\+2 │$/.test(v.box[2].trimEnd().replace(/ │ │$/,' │')),tag);
 assert(!v.r.hits.some(h=>h.action==='question:S4:t:gone@'+'9'.repeat(64)),tag);
 // Main's `i/N` and Tab's range: 6 questions, the closed one the sixth.
 b.enter('');let w=b.detail({budget:20,columns:72}).map(r=>r.map(p=>p.text).join('')).join('\n');assert(w.includes('1/6'),w);
 for(let i=0;i<5;i++)b.input(Buffer.from('\t'));w=b.detail({budget:20,columns:72}).map(r=>r.map(p=>p.text).join('')).join('\n');
 assert(w.includes('6/6')&&w.includes('ZZ'),w);b.leave();
}
{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update([items[0],items.at(-1)],'',{labels,styles:{}});
 const v=paint(b,69,{wait:labels.waiting+' 1'});assert(!v.all.includes('ZZ'),v.all);assert.equal(b.summary().waits.length,1,v.all);}
// A question gone with a draft kept: every row still reads the waiting word.
{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update(items,'',{labels,styles:{}});
 b.enter('');b.input(Buffer.from('x'));b.update(items.slice(1),'',{labels,styles:{}});
 assert(b.summary().waits.every(w=>w.word===word),JSON.stringify(b.summary().waits.map(w=>w.word)));
 const v=paint(b,69,{wait:labels.waiting+' 5'});for(const k of [0,1,2])assert(v.box[k].includes('◑ '+word+' ·'),v.all);}
console.log('ok');
""", styled_items([*waits, gone]))

    def test_a_wait_row_reads_its_first_line_that_draws(self):
        # Backend and editor fallback skip lines left blank after removing controls.
        for text in (
            "\n\x00\x1b\x7f\n실제 질문 줄",
            "\n\x00 \x1b \x7f\x80\x9f\n실제 질문 줄",
            "\n\r\n\x00\r실제 질문 줄",
            "\n\u061c \u200f \u202e \u2069\n실제 질문 줄",
            "\n\x00\x85실제\t 질문\u3000줄",
        ):
            with self.subTest(text=text):
                raw = {"id": "S4:t:ctl", "revision": "c" * 64, "answerable": True,
                       "questions": [{"id": "ctl", "question": text}]}
                presented_items = styled_items([json.loads(json.dumps(raw))], 69)
                line = presented_items[0]["questions"][0]["display"]["brief_line"]
                self.assertEqual(line, [{"text": "실제 질문 줄", "key": "loc_repo"}])
                for items in (presented_items, [raw]):
                    self.node(r"""
const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update(items,'',{labels,styles:{}});
assert.equal(b.summary().waits[0].line.map(p=>p.text).join(''),'실제 질문 줄');
const v=paint(b,69,{wait:labels.waiting+' 1'});assert(v.box[0].includes('◑ '+word+' · 실제 질문 줄'),v.all);
console.log('ok');
""", items)


if __name__ == "__main__":
    unittest.main()
