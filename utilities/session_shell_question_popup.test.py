"""11-15 (popup design A, F): the panel's question summary, the popup frame, the words and colors.

The panel no longer answers in place: its QUESTIONS section is a label row and a two-row summary,
and the question reads whole in a popup over the window (one frame for every popup). These run
the real backend presentation, the real editor and the real panel paint with synthetic values.
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

QUESTION = ("패널을 접었을 때의 모양을 어느 안으로 구현할까요?\n아래 두 줄(다)은 원래 화면이 폭을 다 돌려받고, 답할 질문의 첫 줄과 "
            "실패한 route가 글로 남습니다. 넓힌 옆 띠(가)는 규칙이 가장 적게 바뀌지만 원래 화면이 21칸을 잃습니다.\n"
            "고른 안만 따로 구현하고, 나머지는 시안으로 남깁니다.")
CHOICES = ["아래 두 줄 (다)", "넓힌 옆 띠 (가)", "아래 한 줄 (나)", "지금 그대로"]


def presented(popup, panel):
    """The backend's rows for three questions, as the projection sends them (Korean screen)."""
    from fleet.session_block import question_presentations
    items = [{"id": "q1", "revision": "a" * 64, "answerable": True,
              "questions": [{"id": "shape", "question": QUESTION, "choices": CHOICES, "recommended": 0}]},
             {"id": "q2", "revision": "b" * 64, "answerable": True,
              "questions": [{"id": "color", "question": "MESSAGES 구분선의 색을 지금 회색 그대로 둘까요, 한 단계 밝게 할까요? 글로 적어 주세요."}]},
             {"id": "q3", "revision": "c" * 64, "answerable": True,
              "questions": [{"id": "token", "question": "시험 기계의 사설 접속 토큰을 넣어 주세요.", "secret": True}]}]
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
        question_presentations(items, popup, brief_columns=panel - 4)
    for item in items:
        for question in item["questions"]:
            question["display"] = {key: ([[{"text": t, "key": k} for t, k in line] for line in value]
                                         if key not in ("head", "brief_line") else [{"text": t, "key": k} for t, k in value])
                                   for key, value in question["display"].items()}
    return items


class QuestionPopupTest(unittest.TestCase):
    def node(self, script, *args):
        result = subprocess.run(["node", "-e", script, str(SHELL), *args], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_visible_marks_match_payloads_and_survive_navigation(self):
        baseline = subprocess.check_output(["git", "show", "773a57382:utilities/session-shell/block-input.cjs"],
                                           cwd=ROOT, text=True, timeout=10)
        self.node(r"""
const assert=require('node:assert/strict'),Module=require('node:module'),dir=process.argv[1];
const previous=new Module(dir+'/block-input.cjs');previous.filename=dir+'/block-input.cjs';previous.paths=module.paths;
previous._compile(process.argv[2],previous.filename);
const current=require(dir+'/block-input.cjs'),view=require(dir+'/panel-view.cjs');
const q={id:'q',question:'Synthetic question',recommended:1,free_text:true,choices:[1,2,3].map(n=>({value:'v'+n,label:'Label '+n}))};
function setup(create,extra={},kind){let sent;const b=create({changed(){},action(){},answer:async x=>(sent=x,{state:'submitted'})});
 const items=[{id:'synthetic',revision:'one',answerable:true,answer_kind:kind,questions:[{...q,...extra}]},{id:'other',revision:'one',answerable:true,questions:[{...q,id:'q2'}]}];
 b.update(items,'',{});b.enter();return {b,items,get sent(){return sent}};}
const rows=b=>b.detail({columns:92,budget:24});
const marked=b=>rows(b).filter(r=>r.some(p=>p.role==='choice-mark')).map(r=>Number(r.find(p=>Number.isInteger(p.choiceIndex))?.choiceIndex??r.map(p=>p.text).join('').match(/▶ (\d+)/)[1]-1)+1);
const words=b=>rows(b).map(r=>r.map(p=>p.text).join('')).join('\n');
function step(x,keys){if(keys==='click-write')x.b.write();else if(keys==='click-3')x.b.choose(2);else if(keys==='reopen'){x.b.leave();x.b.update(x.items,'',{});x.b.enter();}else x.b.input(Buffer.from(keys));}
const cases=[
 ['default',[],[2],['v2'],true],
 ['text',['가나'],[],['가나'],false],
 ['number',['3','가나'],[3],['v3','가나'],false],
 ['down',['\x1b[B','가나'],[3],['v3','가나'],true],
 ['toggle',['3','3','가나'],[],['가나'],true],
 ['up-retains',['가나','\x1b[A','1'],[1],['v1','가나'],false],
 ['click-write',['click-write','1가'],[],['1가'],false],
 ['erase',['가','\x7f'],[],null,false],
 ['tab',['3','\t','\t','\t','reopen'],[3],['v3'],false],
 ['default-number',['2','가나'],[2],['v2','가나'],false],
 ['default-down-to-row',['click-write'],[2],['v2'],true],
 ['last-default-down',['\x1b[B','가'],[3],['v3','가'],true],
 ['click-choice',['click-3','가나'],[3],['v3','가나'],true],
 ['paste-default',['\x1b[200~가나\x1b[201~'],[],['가나'],false],
 ['reopen-default',['reopen'],[2],['v2'],true],
 ['reopen-text',['가나','reopen'],[],['가나'],false],
 ['reopen-chosen',['3','가나','reopen'],[3],['v3','가나'],false]
];
(async()=>{for(const [name,keys,mark,payload,changed] of cases){
 const check=async create=>{const x=setup(create);for(const key of keys)step(x,key);
  assert.deepEqual(marked(x.b),mark,name+' mark');
  if(payload?.length===2)assert(words(x.b).includes('가'),name+' retains draft');
  x.b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));
  assert.deepEqual(x.sent?.answers.q??null,payload,name+' payload');if(payload===null)assert(words(x.b).includes('empty answer'));};
 await check(current);let oldPassed=true;try{await check(previous.exports);}catch{oldPassed=false;}
 if(changed)assert(!oldPassed,name+' must reject old implementation');
 console.log(JSON.stringify({name,mark,payload,current:'PASS',baseline:oldPassed?'PASS':'FAIL'}));
}
for(const extra of [{recommended:undefined},{recommended:2}]){const x=setup(current,extra);assert.deepEqual(marked(x.b),[extra.recommended===2?3:1]);x.b.input(Buffer.from('\x1b[B'));if(extra.recommended===2){assert.deepEqual(marked(x.b),[3]);x.b.input(Buffer.from('가'));assert.deepEqual(marked(x.b),[]);}}
const plain=setup(current,{choices:[]});assert.deepEqual(marked(plain.b),[]);plain.b.input(Buffer.from('가나\r'));await new Promise(r=>setImmediate(r));assert.deepEqual(plain.sent.answers.q,['가나']);
const choice=setup(current,{free_text:false});assert.deepEqual(marked(choice.b),[2]);choice.b.input(Buffer.from('t'));assert(!choice.b.typing);assert(!words(choice.b).includes('t text'));
const decision=setup(current,{},'decision');assert.deepEqual(marked(decision.b),[]);decision.b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));assert(!decision.sent);
const grouped=setup(current);grouped.items[0].questions.push({...q,id:'second'});grouped.items.splice(1);grouped.b.update(grouped.items,'',{});grouped.b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));assert(!grouped.sent);assert(words(grouped.b).includes('2/2'));grouped.b.input(Buffer.from('\r'));await new Promise(r=>setImmediate(r));assert.deepEqual(grouped.sent.answers,{q:['v2'],second:['v2']});
// Every painted choice and the answer row expose their own hit; the host uses the editor's explicit chooser.
const x=setup(current),all=rows(x.b),popup=view.popup({cols:100,rows:30,kind:'question',title:all[0],body:all.slice(1),sections:['QUESTIONS'],current:0});
assert.equal(popup.hits.filter(h=>h.action.startsWith('answer-choice:')).length,3);assert(popup.hits.some(h=>h.action==='answer-write'));
})().catch(e=>{console.error(e);process.exit(1)});
""", baseline)

    def test_free_text_guidance_uses_one_localized_row(self):
        from fleet.config import ui_text
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "en"}):
            labels = {name: ui_text("fleet.session_block." + name) for name in
                      ("choose_write", "writing_choices", "navigate_write", "send")}
        self.node(r"""
const assert=require('node:assert/strict'),create=require(process.argv[1]+'/block-input.cjs');
const b=create({changed(){},answer(){},action(){}}),labels=JSON.parse(process.argv[2]);
b.update([{id:'fixture',revision:'one',answerable:true,questions:[{id:'q',question:'Question',free_text:true,choices:['one','two']}]}],'',{labels});b.enter();
const last=()=>b.detail({budget:20,columns:92}).at(-1).map(p=>p.text).join('');
assert.equal(last(),'↑↓ choose · type to write · Enter send · Tab next · Esc/Ctrl+] back');
b.input(Buffer.from('hello'));assert.equal(last(),'Enter send · Ctrl+J newline · ↑ choices · Tab next · Esc/Ctrl+] back');
""", json.dumps(labels))

    def test_the_backend_sends_the_summary_and_the_count_leads_its_name(self):
        items = presented(76, 52)
        display = items[0]["questions"][0]["display"]
        text = lambda parts: "".join(part["text"] for part in parts)
        # 11-15 design F 12: the count leads `QUESTIONS`; F 1: one state word for one question.
        self.assertEqual(text(display["head"]), "◑ 3 QUESTIONS · 1/3 · 답 필요")
        # The popup's text wraps at its own width.
        self.assertTrue(all(sum(2 if ord(c) > 0x2e7f else 1 for c in text(row)) <= 72 for row in display["body"]))
        # 11-28 (the person: "선지는 미리 보기를 날리고"): the panel's row of a waiting question is its first line whole
        # (the panel cuts it); no summary rows of its choices or of its second row are sent any more.
        self.assertEqual(text(display["brief_line"]), "패널을 접었을 때의 모양을 어느 안으로 구현할까요?")
        self.assertFalse({"brief", "brief_one"} & set(display))
        self.assertEqual(text(items[1]["questions"][0]["display"]["brief_line"]),
                         "MESSAGES 구분선의 색을 지금 회색 그대로 둘까요, 한 단계 밝게 할까요? 글로 적어 주세요.")

    def test_a_long_question_leaves_the_routes_and_messages_in_a_20_row_window(self):
        # Design "지금 모습에서 본 것": in a 20-row window the question box pushed out ROUTE and MESSAGES.
        # Its summary is three rows: the routes and the messages stay, and a press on any of the
        # question's rows opens the popup. The same rows under the native area (bottom layout).
        # (11-15: no LAB section any more, whatever the backend sends.)
        out = self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const create=require(path.join(process.argv[1],'block-input.cjs'));
for(const [width,side] of [[52,true],[40,true],[90,false]]){
 const items=JSON.parse(process.argv[side?(width===52?2:3):4]);
 const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update(items,'',{labels:{questions:'QUESTIONS'},styles:{}});
 const P=t=>({text:t});
 const model={tag:'[e9]',repo:[P('hearting')],selected:[P('Opus 5.5 xhigh')],wait:'◑답 대기 3',answerable:true,session_title:'질문·route 팝업 시안',
  route_label:'route 4 ⠏2 ✕1',route_count:4,route_total:4,routes:[0,1,2,3].map(i=>({id:'r'+i,title:'ROUTE_'+i,kind:i?'working':'dead',paint:{glyph:P(i?'⠏':'✕')}})),
  lab:{key:'lab:grp_1',head:[P('4 LAB')],right:[P('실패 1 대기 1')],rows:[],strip:[P('!1')]},
  messages:[1,2,3].map(at=>({way:'←',who:'[e7]',text:'MESSAGE_'+at,at}))};
 // Both inputs a host ever gave: the editor's rows (before 11-15) and the summary (now).
 const r=v.render({width,height:20,side,model,state:'idle',harness:'claude',configuration:{},questions:o=>b.detail(o),brief:()=>b.summary(),sent:()=>b.sent()});
 const rows=r.rows.map(row=>row.map(p=>p.text).join('')),all=rows.join('\n');
 for(const want of [side?'4 ROUTE':'route 4','ROUTE_0','MESSAGE_3',side?'╭─ 3 QUESTIONS ─':'3 QUESTIONS','◑ to answer · 패널을'])assert(all.includes(want),width+' lost '+want+'\n'+all);
 assert(!/\bLAB\b/.test(all),all);
 assert(!/╭─ ▶ ◑|╭─ ◑/.test(all),'no question box in the panel\n'+all);
 assert(rows.filter(s=>/패널을|아래 두 줄|넓힌|지금 그대로|21칸/.test(s)).length<=2,all);
 // (11-16: beside the native area the section is a thin box, its name in the top line, the summary inside; the bottom layout
 // keeps the head row and its rows. A press on the head and on every row of either opens the popup.)
 // (11-17: the route box's zone rule no longer goes first, so a 20-row bottom layout may keep the section's thin box, as the side layout
 // does, and give the messages their one-row form instead; either form: the head row and its rows, or the box.)
 const q=rows.findIndex(s=>s.includes(side?'╭─ 3 QUESTIONS ─':'3 QUESTIONS')),boxed=side||rows[q].includes('╭'),last=boxed?rows.findIndex((s,i)=>i>q&&s.startsWith('│ ╰')):rows.findIndex(s=>s.includes('◑ to answer · 패널을'));
 assert(q>0&&last>q,all);
 // (11-28: a wait's row opens on that wait, `question:<id@revision>`; the rest of the box as before.)
 for(let i=q;i<=last;i++)assert(r.hits.filter(h=>/^question(:|$)/.test(h.action)).some(h=>h.row===i),i+JSON.stringify(r.hits));
 assert(r.hits.some(h=>h.action==='question:q1@'+'a'.repeat(64)),JSON.stringify(r.hits));
}
console.log('ok');
""", json.dumps(presented(76, 52), ensure_ascii=False), json.dumps(presented(76, 40), ensure_ascii=False),
            json.dumps(presented(76, 90), ensure_ascii=False))
        self.assertIn('ok', out)

    def test_one_popup_frame_stands_in_the_middle_above_the_strip(self):
        # Design 공통: the middle of the window (above the folded strip, never over it), a thin rounded
        # line (11-15: bright, on the popup's own ground) one empty cell from what it covers, the title and `✕` on the top line, the
        # sections on the bottom line (the current one bright), the keys on the row above it; 76
        # cells for text, the window less 4 under 80 columns, the whole window under 28; the
        # content's height, at most the rows above the strip less 2.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const P=t=>({text:t}),text=r=>r.rows.map(row=>row.map(p=>p.text).join(''));
const body=n=>[...Array(n)].map((_,i)=>[P('row '+i)]).concat([[P('↑↓ · Enter 보내기 · Tab 다음 · Esc/Ctrl+] 돌아가기')]]);
const pop=(cols,rows,band,n=3,title=[P('▶ ◑ 3 QUESTIONS · 1/3 · 답 필요')])=>v.popup({cols,rows,band,title,body:body(n),sections:['QUESTIONS','ROUTE'],current:0});
assert.deepEqual([160,120,90,80,79,60,28,27,20].map(c=>v.popupWidth(c)),[76,76,76,76,75,56,24,27,20]);
// 11-18: a table popup stands up to 116 cells, the routes popup alone up to 148; under 80 columns both are the window less 4.
assert.deepEqual([200,160,152,120,100,80,79,60,28,27].map(c=>v.popupWidth(c,'table')),[116,116,116,116,96,76,75,56,24,27]);
assert.deepEqual([240,200,160,120,100,80,79,60,28,27].map(c=>v.popupWidth(c,'route')),[196,196,156,116,96,76,75,56,24,27]);
// 11-18 ("그 질문 팝업도 가로 길이를 좀 늘려줘라"): the question popup stands up to 132 cells (`question`), the window less 4 where that is less, as before under 80 columns.
assert.deepEqual([240,200,160,136,132,120,100,80,79,60,28,27].map(c=>v.popupWidth(c,'question')),[132,132,132,132,128,116,96,76,75,56,24,27]);
// A popup asked for with no kind keeps the text width (the table and routes popups' and the question popup's are their own).
assert.deepEqual([240,160,80].map(c=>v.popupWidth(c,'text')),[76,76,76]);
// The question popup laid out by `popup({kind:'question'})`: 132 cells in a 160-wide window, the frame centred.
{const q=v.popup({cols:160,rows:40,band:0,kind:'question',title:[P('▶ ◑ 1 QUESTIONS')],body:body(3),sections:['QUESTIONS','ROUTE'],current:0});
 assert.deepEqual([q.frame.left,q.frame.width],[14,132]);}
let r=pop(160,40,0);assert.deepEqual(r.frame,{left:42,top:17,width:76,height:6});
assert.deepEqual([r.left,r.top,r.width,r.height],[41,16,78,8]);
const rows=text(r);
assert.equal(rows[0],' '.repeat(78));assert.equal(rows.at(-1),' '.repeat(78));
assert(/^ ╭─ ▶ ◑ 3 QUESTIONS · 1\/3 · 답 필요 ─+ ✕ ─╮ $/.test(rows[1]),rows[1]);
assert(/^ ╰─ QUESTIONS · ROUTE ─+╯ $/.test(rows.at(-2)),rows.at(-2));
assert(rows[rows.length-3].includes('Enter 보내기'),rows.join('\n'));
// 11-15: the thin line is bright (`popupLine`) on the popup's own ground (`popup`); the current section
// bright, the others the readable floor (`popupFloor`); `✕` and the names press.
const line=r.rows[1].find(p=>p.text.startsWith('╭'));assert.equal(line.foreground,v.FACES.dark.popupLine);assert.equal(line.background,v.FACES.dark.popup);
const names=r.rows.at(-2);assert.equal(names.find(p=>p.text==='QUESTIONS').bold,true);
const other=names.find(p=>p.text==='ROUTE');assert(other.dim!==true&&other.foreground===v.FACES.dark.popupFloor,JSON.stringify(other));
assert.deepEqual(r.hits.map(h=>h.action),['popup-close','popup:QUESTIONS','popup:ROUTE']);
const close=r.hits[0],cellAt=(s,x)=>{let at=0;for(const c of s){if(at===x)return c;at+=c.codePointAt(0)>0x2e7f?2:1;}return '';};
assert.equal(cellAt(rows[1],close.left-r.left),'✕');
// Folded: in the middle of the rows above the strip, never over it.
r=pop(90,24,4,24);assert(r.top+r.height<=20&&r.frame.width===76&&r.frame.left===7,JSON.stringify(r.frame));
assert.equal(r.frame.height,18,'at most the rows above the strip less 2');
// 60×16 with the strip: 56 cells at column 2; ten rows (sixteen less the strip's four, less two).
r=pop(60,16,4,20);assert.deepEqual(r.frame,{left:2,top:1,width:56,height:10});
// Narrower than 28: the whole window, no side margin.
r=pop(24,12,2);assert.deepEqual([r.frame.left,r.frame.width,r.left,r.width],[0,24,0,24]);
// A long title ends in `…` and the `✕` stays; every row is exactly its width.
r=pop(40,20,0,2,[P('▶ ◑ 3 QUESTIONS · 1/3 · '+'아주 긴 상태 낱말 '.repeat(4))]);
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
assert(text(r)[1].includes('…')&&text(r)[1].includes('✕ ─╮'),text(r)[1]);
assert(text(r).every(s=>cells(s)===r.width),text(r).join('\n'));
""")

    def test_the_folded_strip_opens_the_popup_from_its_row_of_what_to_do(self):
        out = self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const create=require(path.join(process.argv[1],'block-input.cjs'));
const items=JSON.parse(process.argv[2]);
const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});b.update(items,'',{labels:{},styles:{}});
const P=t=>({text:t});
const r=v.render({width:90,height:1,folded:true,model:{tag:'[e9]',wait:'◑답 대기 3',route_label:'route 1 ⠏1',route_count:1,routes:[{title:'run',kind:'working'}]},
 state:'idle',harness:'claude',questions:o=>b.detail(o),brief:()=>b.summary()});
const rows=r.rows.map(row=>row.map(p=>p.text).join('').replace(/ +▴$/,''));
// 11-15 (one row): the wait alone stands for the question; its words and choices are not on the strip.
assert.equal(rows.length,1);assert.equal(rows[0],'● [e9] │ Checking │ ◑답 대기 3 │ 1 ROUTE ⠏1');
// The wait opens the question popup, the routes the routes popup, the rest of it the panel.
assert.deepEqual(r.hits.map(h=>h.action),['question','popup:ROUTE','unfold']);
console.log('ok');
""", json.dumps(presented(76, 90), ensure_ascii=False))
        self.assertIn('ok', out)

    def test_a_lone_enter_right_after_an_answer_is_the_shells(self):
        # Rule 5 widened (11-15): after an answer the person is back at the native input; a lone
        # Esc or a lone Enter that follows at once was meant for the popup and never reaches it
        # (an Enter would submit what stands in the native input). Anything else goes on.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),create=require(path.join(process.argv[1],'block-input.cjs'));
(async()=>{
 for(const [key,taken] of [['\r',true],['\x1b',true],['\n',true],['a',false],['\r\r',false]]){
  const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Q?',choices:['a','b']}]}],'',{});
  b.enter();b.input(Buffer.from('1'));b.input(Buffer.from('\r'));await new Promise(r=>setTimeout(r,20));
  assert.equal(b.focused,false,'the answer closes the popup');
  assert.equal(b.answeredEscape(Buffer.from(key)),taken,JSON.stringify(key));
  assert.equal(b.answeredEscape(Buffer.from(key)),false,'once only');
 }
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_an_alt_letter_in_the_popup_is_dropped_and_a_lone_esc_still_closes_it(self):
        # ESC then a letter is the popup's, never the native's: the popup stands and nothing goes on. 11-26:
        # Alt+B/F step a word and Alt+Backspace deletes one in the written answer; any other Alt+letter is
        # dropped and the draft stays. A lone Esc (the host's 25 ms) closes it; so does Esc then Esc, and
        # nothing else that follows an Esc does.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),create=require(path.join(process.argv[1],'block-input.cjs'));
(async()=>{
 const make=()=>{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Q?'}]}],'',{});b.enter();return b;};
 for(const [alt,after] of [['\x1bb','dabc'],['\x1bf','abcd'],['\x1bx','abcd'],['\x1b\x7f','d'],['\x1b한','abcd']]){
  const b=make();b.input(Buffer.from('abc'));
  const rest=b.input(Buffer.from(alt));
  assert.equal(rest.length,0,'nothing goes on: '+JSON.stringify(alt));
  assert.equal(b.focused,true,'the popup stands: '+JSON.stringify(alt));
  b.input(Buffer.from('d'));
  const shown=b.detail({budget:6,columns:60}).find(row=>row.some(p=>p.answerRow))?.map(p=>p.text).join('');
  assert.equal(shown,'› '+after,'the draft after '+JSON.stringify(alt));
 }
 {const b=make();b.input(Buffer.from('\x1b'));b.input(Buffer.from('b'));assert.equal(b.focused,true,'split across reads');}
 {const b=make();b.input(Buffer.from('\x1b'));await new Promise(r=>setTimeout(r,60));assert.equal(b.focused,false,'a lone Esc closes it');}
 // One rule: after an Esc, in the same read or within the Esc window, only `[` and `O` (arrows, function
 // keys) belong to a key; any other byte is dropped, never the native's, and only a second Esc closes it.
 for(const combo of ['\x1b\x03','\x1b\x08','\x1b\t','\x1b\x1d','\x1b\x00']){
  const b=make();const rest=b.input(Buffer.from(combo));
  assert.equal(rest.length,0,'nothing goes on: '+JSON.stringify(combo));assert.equal(b.focused,true,'the popup stands: '+JSON.stringify(combo));
  const split=make();split.input(Buffer.from('\x1b'));assert.equal(split.input(Buffer.from(combo.slice(1))).length,0,'split: '+JSON.stringify(combo));
  assert.equal(split.focused,true,'split, the popup stands: '+JSON.stringify(combo));
 }
 {const b=make();const rest=b.input(Buffer.from('\x1b\x1b'));assert.equal(b.focused,false,'Esc then Esc closes it');assert.equal(rest.length,0,'and the second Esc is dropped');}
 {const b=make();b.input(Buffer.from('\x1b'));const rest=b.input(Buffer.from('\x1b'));assert.equal(b.focused,false,'split Esc, Esc closes it');assert.equal(rest.length,0);}
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_alt_enter_puts_a_line_break_and_the_popup_stands(self):
        # 11-26: Alt/Option+Enter (and a terminal's Shift+Enter set up to send it) arrives as ESC then CR (or
        # LF), in one read or split within the Esc window. In a written answer it puts a line break at the
        # place; on a question answered only by a choice it is dropped as before. Either way it never closes
        # the popup and never reaches the native (a CR there submits the native input's text). A lone Esc
        # still closes it.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),create=require(path.join(process.argv[1],'block-input.cjs'));
(async()=>{
 const make=(extra={})=>{let sent;const b=create({changed(){},answer:async x=>(sent=x,{state:'submitted'}),action(){}});
  b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Q?',...extra}]}],'',{});b.enter();return {b,get sent(){return sent}};};
 const answer=b=>b.detail({budget:12,columns:60}).filter(row=>row.some(p=>p.answerRow)).map(row=>row.map(p=>p.text).join(''));
 for(const combo of ['\x1b\r','\x1b\n']){
  const {b}=make();b.input(Buffer.from('abc'));
  const rest=b.input(Buffer.from(combo));
  assert.equal(rest.length,0,'nothing goes on: '+JSON.stringify(combo));
  assert.equal(b.focused,true,'the popup stands: '+JSON.stringify(combo));
  b.input(Buffer.from('d'));await new Promise(r=>setTimeout(r,60));
  assert.equal(b.focused,true,'no stale Esc timer closes it later');
  assert.deepEqual(answer(b),['› abc','  d'],'a line break at the place');
 }
 {const x=make();x.b.input(Buffer.from('abc'));x.b.input(Buffer.from('\x1b'));const rest=x.b.input(Buffer.from('\r'));
  assert.equal(rest.length,0,'split across reads');assert.equal(x.b.focused,true);await new Promise(r=>setTimeout(r,60));assert.equal(x.b.focused,true);
  x.b.input(Buffer.from('d\r'));await new Promise(r=>setImmediate(r));assert.deepEqual(x.sent.answers.q,['abc\nd'],'Enter sends the lines');}
 {const x=make({choices:['one','two'],free_text:false});const rest=x.b.input(Buffer.from('\x1b\r'));await new Promise(r=>setTimeout(r,60));
  assert.equal(rest.length,0);assert.equal(x.b.focused,true,'a choice-only question: dropped, the popup stands');assert.equal(x.sent,undefined,'nothing sent');}
 {const {b}=make();b.input(Buffer.from('\x1b'));await new Promise(r=>setTimeout(r,60));assert.equal(b.focused,false,'a lone Esc closes it');}
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_the_lone_esc_right_after_an_esc_close_is_the_shells_once(self):
        # The Esc that closes the popup is often pressed twice. The second lone Esc within a second
        # belongs to the shell (the native's would interrupt the turn or cancel its own prompt); any
        # other input, or an Esc after a second, goes on as typed. Other ways of closing are as before.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),create=require(path.join(process.argv[1],'block-input.cjs'));
(async()=>{
 const wait=ms=>new Promise(r=>setTimeout(r,ms));
 const make=()=>{const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
  b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Q?'}]}],'',{});b.enter();return b;};
 const escClosed=async()=>{const b=make();b.input(Buffer.from('\x1b'));await wait(60);assert.equal(b.focused,false);return b;};
 {const b=await escClosed();await wait(200);assert.equal(b.answeredEscape(Buffer.from('\x1b')),true,'Esc 0.2 s later is the shell\'s');
  assert.equal(b.answeredEscape(Buffer.from('\x1b')),false,'once only');}
 for(const key of ['x','\r','\n','\x1b[A','\x1b\x1b','\x1b[200~p\x1b[201~']){
  const b=await escClosed();assert.equal(b.answeredEscape(Buffer.from(key)),false,'goes on: '+JSON.stringify(key));
  assert.equal(b.answeredEscape(Buffer.from('\x1b')),false,'any other input ends the window');}
 {const b=await escClosed();await wait(1100);assert.equal(b.answeredEscape(Buffer.from('\x1b')),false,'after a second it is the native\'s');}
 {const b=make();b.leave();assert.equal(b.answeredEscape(Buffer.from('\x1b')),false,'Ctrl+] / ✕ / a press outside: as before');}
 {const b=make();b.input(Buffer.from('\x1d'));assert.equal(b.focused,false);assert.equal(b.answeredEscape(Buffer.from('\x1b')),false,'Ctrl+]: as before');}
 {const b=await escClosed();b.enter();b.leave();assert.equal(b.answeredEscape(Buffer.from('\x1b')),false,'a later close by another way ends it');}
 {const b=await escClosed();b.enter();assert.equal(b.answeredEscape(Buffer.from('\x1b')),false,'opening it again ends it');}
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_an_esc_key_after_an_esc_close_or_an_answer_reaches_the_native_whole(self):
        # panel-mouse used to tear the ESC off a key it did not know (`ESC b`, `ESC \r`, `ESC O A`) and
        # hand it on alone, where answeredEscape took it for the lone Esc. The key now goes whole (2, 2, 3,
        # 2 bytes); only a lone Esc flushed after 8 ms is taken. Mouse reports, CSI and pastes are cut as before.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path');
const create=require(path.join(process.argv[1],'block-input.cjs')),decoder=require(path.join(process.argv[1],'panel-mouse.cjs'));
(async()=>{
 const wait=ms=>new Promise(r=>setTimeout(r,ms));
 const rig=()=>{ // inputBytes' native path: what answeredEscape does not take goes to the native
  const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){}}),native=[],mouse=[];
  b.update([{id:'x',revision:'r',answerable:true,questions:[{id:'q',question:'Q?',choices:['a','b']}]}],'',{});
  const m=decoder({suppressed:()=>false,mouse:(...a)=>mouse.push(a.slice(0,4).join(',')),bytes:data=>{
   while(data.length){if(!b.focused&&b.answeredEscape(data)){data=data.subarray(1);continue;}native.push(data.toString('latin1'));return;}}});
  return {b,m,native,mouse};
 };
 const viaEsc=async r=>{r.b.enter();r.b.input(Buffer.from('\x1b'));await wait(60);assert.equal(r.b.focused,false);};
 const viaAnswer=async r=>{r.b.enter();r.b.input(Buffer.from('1'));r.b.input(Buffer.from('\r'));await wait(20);assert.equal(r.b.focused,false);};
 for(const [name,close] of [['Esc close',viaEsc],['an answer',viaAnswer]]){
  for(const [key,size] of [['\x1bb',2],['\x1b\r',2],['\x1bOA',3],['\x1b\x7f',2],['\x1b[A',3],['\x1b[1;5C',6]]){
   const r=rig();await close(r);await wait(200);r.m.feed(Buffer.from(key));await wait(30);
   assert.equal(r.native.join('').length,size,name+': '+JSON.stringify(key)+' reaches the native whole');
   assert.deepEqual(r.native,[key],name+': in one piece');
  }
  {const r=rig();await close(r);await wait(200);r.m.feed(Buffer.from('\x1b'));await wait(30);assert.deepEqual(r.native,[],name+': a lone Esc is the shell\'s');}
  {const r=rig();await close(r);await wait(200);r.m.feed(Buffer.from('\x1b'));await wait(3);r.m.feed(Buffer.from('b'));await wait(30);
   assert.deepEqual(r.native,['\x1bb'],name+': ESC and b split within a frame are one key');}
 }
 // Cut exactly as before: a mouse report, a CSI and a key behind another in one read.
 {const r=rig();r.m.feed(Buffer.from('\x1bb\x1b[<0;5;6M\x1b[Ax\x1b\x1bc'));await wait(30);
  assert.deepEqual(r.mouse,['0,5,6,false']);assert.deepEqual(r.native,['\x1bb','\x1b[A','x','\x1b','\x1bc']);}
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_a_question_replaced_with_nothing_typed_says_so_once_and_the_first_enter_only_clears_it(self):
        # A choice question being read goes (answered or cancelled elsewhere) and the next choice
        # question takes its place: the first Enter after never answers the new one unseen.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),create=require(path.join(process.argv[1],'block-input.cjs'));
(async()=>{
 const sent=[];
 const b=create({changed(){},answer:async r=>{sent.push(r.question_id);return {state:'submitted'};},action(){}});
 const ask=id=>({id,revision:id,answerable:true,questions:[{id:'q'+id,question:'Question '+id+'?',choices:['one','two']}]});
 const words=()=>b.detail({budget:8,columns:80}).map(row=>row.map(p=>p.text).join('')).join('\n');
 b.update([ask('A'),ask('B')],'',{});b.enter();assert(words().includes('Question A?'));
 b.update([ask('B')],'',{});                       // A is answered elsewhere; B stands in its place
 assert(words().includes('Question B?')&&words().includes('question changed'),words());
 b.update([ask('B')],'',{});assert.equal((words().match(/question changed/g)||[]).length,1,'once');
 b.input(Buffer.from('\r'));await new Promise(r=>setTimeout(r,20));
 assert.deepEqual(sent,[],'the first Enter sends nothing');
 assert(!words().includes('question changed'),'it clears the word');
 assert.equal(b.focused,true);
 b.input(Buffer.from('\r'));await new Promise(r=>setTimeout(r,20));
 assert.deepEqual(sent,['B'],'the next Enter answers B once');
 // A question that is simply read on (a later one arrives, this one stays) says nothing.
 const c=create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
 c.update([ask('A')],'',{});c.enter();c.update([ask('Z'),ask('A')],'',{});
 assert(!c.detail({budget:8,columns:80}).map(row=>row.map(p=>p.text).join('')).join('\n').includes('question changed'));
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_no_guidance_the_question_popup_draws_names_the_arrows_as_sections(self):
        # 11-22: in the question popup ←→ only move the place in the typed text, so none of its guidance
        # (the keys rows `navigate`, `navigate_write`, the writing hints, and `editor_help`, which a popup with no
        # question row to answer stands on) says `←→`, in either language.
        from fleet.config import ui_text
        keys = ("navigate", "navigate_write", "editor_help", "choose", "choose_write", "writing_choices", "send", "text",
                "more", "more_choose", "more_down", "more_page", "more_choices")
        for language in ("en", "ko"):
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
                for key in keys:
                    with self.subTest(language=language, key=key):
                        word = ui_text("fleet.session_block." + key)
                        self.assertTrue(word and word != "fleet.session_block." + key, word)
                        self.assertNotIn("←→", word)
                        self.assertNotIn("section" if language == "en" else "구획", word)

    def test_the_question_words_and_colors_are_one_set(self):
        # 11-15 design F 1-4, 12 (words) and 7-9 (colors): one word per meaning on the panel, the
        # popup, Fleet and the notices; `◑` Fleet's waiting red everywhere; `✓` Fleet's green on the
        # panel's route rows; a failed route's line the failure red of its glyph.
        from fleet.config import ui_text
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}):
            words = {key: ui_text(key) for key in (
                "fleet.session_block.open", "fleet.session_block.compact_open", "fleet.session_panel.answer",
                "wait.title", "fleet.session_block.submitted", "fleet.session_block.compact_submitted",
                "fleet.session_block.received", "fleet.session_block.send", "fleet.session_block.choose",
                "fleet.session_block.navigate", "fleet.session_block.editor_help")}
        self.assertEqual({words[k] for k in ("fleet.session_block.open", "fleet.session_block.compact_open",
                                            "fleet.session_panel.answer")}, {"답 필요"})
        self.assertEqual(words["wait.title"], "◑답 필요")
        self.assertEqual(words["fleet.session_block.submitted"], "보내는 중")
        self.assertEqual(words["fleet.session_block.compact_submitted"], "보내는 중")
        self.assertEqual(words["fleet.session_block.received"], "보냄 · 소비 전")
        self.assertEqual(words["fleet.session_block.send"], "Enter 보내기")
        self.assertEqual(words["fleet.session_block.choose"], "↑↓ · Enter 보내기")
        self.assertTrue(words["fleet.session_block.navigate"].endswith("돌아가기"))
        self.assertIn("돌아가기", words["fleet.session_block.editor_help"])
        from fleet.palette import style_components
        from fleet.render_cells import _GLYPH_KEY
        from fleet.explorer.style import normalize_style
        self.assertEqual(style_components(_GLYPH_KEY["done"])["foreground"], 150)
        self.assertEqual(style_components(normalize_style("blocked")[0])["foreground"],
                         style_components("g_blocked")["foreground"])
        from fleet.explorer import tables
        source = Path(tables.__file__).read_text()
        self.assertNotIn('"◑", "lvl_y"', source)
        self.assertNotIn('"lvl_y" if marker == "◑"', source)
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const styles={g_dead:{foreground:217,bold:true},dim:{dim:true}};
const r=v.render({width:40,height:20,side:true,model:{styles,route_count:1,routes:[{id:'d',title:'FAILED',kind:'dead',paint:{glyph:{text:'✕',foreground:217,bold:true}}}]},
 state:'idle',harness:'claude',configuration:{},background:'dark'});
// (11-15: the route box again) the failed route's box line is its glyph's red on the dark face.
const line=r.rows.flat().find(p=>p.role==='route-dead'&&p.text.startsWith('╭'));
assert.equal(line.foreground,217,'the failed route line is its glyph\'s red on the dark face');
const glyph=r.rows.flat().find(p=>p.text==='✕'&&p.role==='state:dead');
assert.equal(glyph.foreground,217,'the failed route keeps its glyph\'s red on the dark face');
""")


if __name__ == "__main__":
    unittest.main()
