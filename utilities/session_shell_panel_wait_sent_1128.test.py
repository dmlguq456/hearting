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
        # answer sent; a wait's own question; nothing named, the question it would open on before. Tab from a sent answer goes
        # back to the waits; a projection while it is read keeps it; once its line has gone, the popup goes on as before.
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
// Its keys say where to go; Enter answers nothing (the answer has left the person's hands).
b.input(Buffer.from('\r'));assert.equal(answered,0);
// A projection while it is read keeps it on screen.
b.update(JSON.parse(process.argv[2]),'',{labels,styles:{}});assert(b.focused&&words().includes('표지 색인 정리를 언제'),words());
// Tab goes back to the question it stood over (the first wait).
b.input(Buffer.from('\t'));w=words();assert(w.includes('[서브 호출')&&!w.includes('표지 색인 정리를 언제'),w);
b.leave();
// A wait's row opens on that wait.
b.enter(key(3));w=words();assert(w.includes('`docs/maps/`')&&!w.includes('[서브 호출'),w);b.leave();
b.enter(key(2));w=words();assert(w.includes('[route 관문]'),w);b.leave();
// Nothing named (the head, the action row, Ctrl+]) opens where it did before: the question it stood on, the first wait
// in a fresh editor; so does a name the editor does not hold (a line already gone).
b.enter('');w=words();assert(w.includes('[route 관문]'),w);b.leave();
b.enter('S4:gone@'+'f'.repeat(64));w=words();assert(w.includes('[route 관문]'),w);b.leave();
{const c=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});c.update(JSON.parse(process.argv[2]),'',{labels,styles:{}});
 c.enter('');const x=c.detail({budget:20,columns:72}).map(r=>r.map(p=>p.text).join('')).join('\n');assert(x.includes('[서브 호출'),x);}
// Read while its line goes (received long ago, or gone from the board): the popup goes on, on the waits.
b.enter(key(0));b.update(JSON.parse(process.argv[2]).filter((_,i)=>i!==0),'',{labels,styles:{}});w=words();
assert(b.focused&&!w.includes('표지 색인 정리를 언제'),w);b.leave();
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


if __name__ == "__main__":
    unittest.main()
