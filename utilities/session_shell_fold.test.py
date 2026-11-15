"""Spec E9 rule 49: the gear inside the panel, folding and unfolding, folding by itself when narrow, and the folded strip under the native area."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"


class FoldTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", script, str(SHELL)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_gear_and_the_fold_button_stand_on_the_last_row_inside(self):
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const p=t=>({text:t});
const model={tag:'[e9]',repo:[p('hearting/')],selected:[p('Opus 5.5')],quota:[p('5h 12%'),p('(↻ 2h)'),p(' │ '),p('7d 66%'),p('(↻ 5d)')],
 route_total:2,route_count:2,routes:[{id:'a',title:'one',kind:'working',paint:{}},{id:'b',title:'two',kind:'idle',paint:{}}]};
const draw=o=>v.render({width:40,height:20,side:true,model,state:'idle',harness:'claude',questions:()=>[],...o});
const text=r=>r.rows.map(row=>row.map(x=>x.text).join(''));
for(const extra of [{},{routeDetail:true}]){
 const r=draw(extra),rows=text(r);
 // (a) The top border is the line alone right of the head; (a), (b) the last row inside ends `»   ⚙`.
 assert(!rows[0].includes('⚙')&&rows[0].endsWith('─┐'),rows[0]);
 assert(rows.at(-2).endsWith('»   ⚙ │')&&rows.filter(s=>s.includes('⚙')).length===1,rows.join('\n'));
 const fold=r.hits.find(h=>h.action==='fold'),gear=r.hits.find(h=>h.action==='settings');
 // The presses: `»` at panel column 33, `⚙` at 37; the cell between them is neither's.
 assert.deepEqual([fold.row,gear.row],[18,18]);
 assert(fold.left<=33&&fold.right>=33&&gear.left<=37&&gear.right>=37&&fold.right<35&&gear.left>35,JSON.stringify([fold,gear]));
 // Both marks dim, as the panel's labels.
 const last=r.rows.at(-2);assert(last.find(x=>x.text==='⚙').dim&&last.find(x=>x.text==='»').dim);
}
// 5h/7d share the row: whole with room, their reset times going first when short.
assert(text(draw({}))[18].startsWith('│ 5h 12%(↻ 2h) │ 7d 66%(↻ 5d)'));
const narrow=text(v.render({width:32,height:20,side:true,model,state:'idle',questions:()=>[]}));
assert(narrow.at(-2).startsWith('│ 5h 12% │ 7d 66%')&&narrow.at(-2).endsWith('»   ⚙ │'),narrow.at(-2));
// The row stays however short the rows run (the head's last rows go first, boxes stay closed).
for(let h=6;h<=14;h++){const rows=text(draw({height:h}));assert(rows.at(-2).endsWith('»   ⚙ │'),h+'\n'+rows.join('\n'));
 assert.equal(rows.filter(s=>s.includes('╭')).length,rows.filter(s=>s.includes('╰')).length,h+'\n'+rows.join('\n'));}
// The settings editor keeps its own last row (Esc), no fold button.
assert(!text(draw({settings:{index:0,value:v.defaults}})).join('').includes('»'));
// (e) The bottom layout: `▾   ⚙`; a left panel folds with `«`.
const bottom=text(v.render({width:80,height:14,side:false,model,state:'idle',configuration:{layout:'bottom'},questions:()=>[]}));
assert(bottom.at(-2).endsWith('▾   ⚙ │'),bottom.join('\n'));
assert(text(draw({configuration:{position:'left'}})).at(-2).endsWith('«   ⚙ │'));
// A side panel opened by hand where it cannot stand beside opens under the native area: `▾`
// there too (the host draws it with `side:false`; the configured layout stays `side`).
for(const position of ['right','left']){
 const under=v.render({width:90,height:14,side:false,model,state:'idle',configuration:{layout:'side',position},questions:()=>[]});
 assert(text(under).at(-2).endsWith('▾   ⚙ │')&&!text(under).join('').match(/[»«]/),text(under).join('\n'));
}
""")

    def test_the_last_row_keeps_the_model_quota_and_gives_way_in_order(self):
        # Correction 7 (2026-11-13): the last row reads `5h … │ 7d … │ fable 70%` (the model word in
        # its model color). Short of cells it gives way in this order: the reset times `(↻ …)`,
        # then 5h, then the model values, then 7d. The folded strip carries none of it.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const P=(t,o={})=>({text:t,...o}),sep=P(' │ ',{dim:true});
const quota=[P('5h ',{dim:true}),P('12%',{foreground:150}),P('(↻ 2h)',{dim:true}),sep,P('7d ',{dim:true}),P('66%',{foreground:186}),P('(↻ 5d)',{dim:true}),
 sep,P('fable ',{foreground:176}),P('70%',{foreground:186})];
const model={tag:'[e9]',repo:[P('hearting/')],quota};
const last=w=>{const r=v.render({width:w,height:20,side:true,model,state:'idle',harness:'claude',questions:()=>[]});return r.rows.at(-2);};
const text=row=>row.map(x=>x.text).join('');
const want=[[60,'5h 12%(↻ 2h) │ 7d 66%(↻ 5d) │ fable 70%'],[40,'5h 12% │ 7d 66% │ fable 70%'],[32,'7d 66% │ fable 70%'],[26,'7d 66%'],[15,'']];
for(const [w,s] of want){const t=text(last(w));assert(t.startsWith('│ '+s+(s?' ':''))&&t.endsWith('»   ⚙ │')&&!t.includes('…'),w+' '+t);}
// The model word keeps its model color; nothing of the row is cut mid-value.
assert.equal(last(40).find(x=>x.text==='fable ').foreground,176);
// Folded, the strip's session row carries the same values, giving way in the same order after
// the reset times (spec rule 49 (c)).
const strip=w=>v.render({width:w,height:4,folded:true,model:{...model,tag:'[e9]'},state:'idle',questions:()=>[]}).rows.map(text)[1].trimEnd();
assert.equal(strip(120),'● [e9] hearting/ │ Checking │ 5h 12%(↻ 2h) │ 7d 66%(↻ 5d) │ fable 70%');
assert.equal(strip(60),'● [e9] hearting/ │ Checking │ 5h 12% │ 7d 66% │ fable 70%');
assert.equal(strip(48),'● [e9] │ Checking │ 5h 12% │ 7d 66% │ fable 70%');
assert.equal(strip(40),'● [e9] │ Checking │ 7d 66% │ fable 70%');
assert.equal(strip(30),'● [e9] │ Checking │ 7d 66%');
""")

    def test_the_folded_strip_stands_under_the_native_area(self):
        # 11-14 night ("접는게 하단창으로", "접는건 아래에 3~4줄 정도"): one folded form, the strip under
        # the native area: a thin rule with `▴`, then the session, what to do now, and the routes.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const styles={g_spin:{foreground:230},g_blocked:{foreground:217},g_work_off:{foreground:150,dim:true},dim:{dim:true},loc_repo:{foreground:252},
 g_idle:{foreground:186},g_dead:{foreground:210},lvl_r:{foreground:210},lvl_y:{foreground:186},tag_steward:{foreground:219,bold:true}};
const P=(t,k)=>({text:t,...(k?styles[k]||{}:{})});
const model={tag:'[e9]',styles,repo_side:[P('hearting','loc_repo')],selected:[P('Opus 5.5'),P(' '),P('xhigh')],context_side:[P('━━━━━━────'),P(' 58%')],
 quota:[P('5h ','dim'),P('12%'),P('(↻ 2h 0m)','dim'),P(' │ ','dim'),P('7d ','dim'),P('66%'),P('(↻ 5d 0h)','dim')],wait:'◑답 대기 1',
 route_label:'route 3 ⠏2 ✕1',route_count:3,route_total:3,messages_new:2,
 routes:[{title:'패널 자동 접힘 기준 60칸',kind:'working',paint:{glyph:P('⠸','g_spin')}},{title:'LAB 표 칸 정리',kind:'working',paint:{glyph:P('⠸','g_spin')}},
  {title:'휠 입력 시험',kind:'dead',paint:{glyph:P('✕','g_dead')}}],lab:{count:'2',counts:[[P('실패 1','lvl_r')]],strip_attention:true}};
// The question's summary (11-15): the strip's row of what to do carries its first line.
const ask=(choices=[])=>()=>({count:1,label:'QUESTIONS',rows:[[P('◑ ','g_blocked'),P('접힌 모양을 어느 안으로 할까요?','loc_repo')],[P('  ','dim'),...choices]],
 one:[[P('◑ ','g_blocked'),P('접힌 모양을 어느 안으로 할까요?','loc_repo')]],line:[P('접힌 모양을 어느 안으로 할까요?','loc_repo')],choices:choices.length?2:0});
const question=ask();
const strip=(o={},m={})=>v.render({width:120,height:4,folded:true,model:{...model,...m},state:'working',frame:3,harness:'claude',brief:question,...o});
const text=r=>r.rows.map(row=>row.map(x=>x.text).join(''));
let r=strip(),rows=text(r);
// Four rows the window's width, one placement for every harness (side or not, left or right).
assert.deepEqual(rows.map(s=>s.trimEnd()),['─'.repeat(118)+' ▴',
 '⠸ [e9] hearting │ Opus 5.5 xhigh │ CTX ━━━━━━──── 58% │ 5h 12%(↻ 2h 0m) │ 7d 66%(↻ 5d 0h)',
 '◑답 대기 1 · 접힌 모양을 어느 안으로 할까요?',
 '3 ROUTE ⠏2 ✕1 · ✕ 휠 입력 시험 │ 2 LAB 실패 1 │ MESSAGES ←2']);
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
assert(rows.every(s=>cells(s)===120),rows.join('\n'));
for(const o of [{side:true},{side:true,configuration:{position:'left'}},{harness:'codex'},{harness:'opencode'},{configuration:{layout:'bottom'}}])
 assert.deepEqual(text(strip(o)),rows,JSON.stringify(o));
// The rule takes the messages' line color, `▴` and the labels are dim (`ROUTE` too, as every other name
// tag: BC-FOLD-BAND-FOLLOWUPS-1115); no frame, gear, beat or moving rule: only the session's glyph turns.
// A press on the rule or the session row opens the panel; the row of what to do, carrying the question,
// opens the question popup; on the routes row each group opens its popup and the rest opens the panel (11-15).
assert.equal(r.rows[0][0].role,'messages');assert(r.rows[0].at(-1).text==='▴'&&r.rows[0].at(-1).dim);
for(const word of ['CTX ','MESSAGES','LAB','ROUTE'])assert(r.rows.flat().find(x=>x.text===word).dim===true,word);
assert.notEqual(r.rows.flat().find(x=>x.text==='ROUTE').foreground,styles.g_idle.foreground,'ROUTE is no longer the route-count yellow');
assert(!/[┌┐└┘⚙⚑]/.test(rows.join(''))&&r.motion===null&&!r.rows.flat().some(x=>x.beat||x.motion),rows.join('\n'));
assert.deepEqual([r.spins,r.animated],[[1],true]);
const whole=[0,1,2,3].map(row=>({row,left:0,right:119,action:row===2?'question':'unfold'}));
assert.deepEqual(r.hits.filter(h=>h.right===119),whole);
// Each group's own cells: `3 ROUTE ⠏2 ✕1 · ✕ 휠 입력 시험` (the count and the titles), `2 LAB 실패 1`, `MESSAGES ←2`.
const spans=r.hits.filter(h=>h.right!==119),row3=[...rows[3]].reduce((o,c)=>{for(let i=0;i<cells(c);i++)o.push(c);return o;},[]);
const at=(h)=>{let s='',seen=null;for(let x=h.left;x<=h.right;x++)if(row3[x]!==seen||cells(row3[x])<2){s+=row3[x];seen=row3[x];}return s;};
assert.deepEqual(spans.map(h=>[h.row,h.action]),[[3,'popup:ROUTE'],[3,'popup:LAB'],[3,'popup:MESSAGES']]);
assert(at(spans[0]).startsWith('3 ROUTE ⠏2 ✕1 · ✕ 휠')&&at(spans[1]).startsWith('2 LAB 실패')&&at(spans[2]).startsWith('MESSAGES ←2'),spans.map(at).join('|'));
// Where cells are left the question's choices follow on its row, whole, else their count alone (11-15).
const choices=[P('1 ','dim'),P('아래 두 줄','loc_repo'),P(' · 2 ','dim'),P('옆 띠','loc_repo')];
assert.equal(text(strip({brief:ask(choices)}))[2].trimEnd(),'◑답 대기 1 · 접힌 모양을 어느 안으로 할까요? · 1 아래 두 줄 · 2 옆 띠');
assert.equal(text(strip({width:56,brief:ask(choices)}))[2].trimEnd(),'◑답 대기 1 · 접힌 모양을 어느 안으로 할까요? · +2');
assert.equal(text(strip({width:48,brief:ask(choices)}))[2].trimEnd(),'◑답 대기 1 · 접힌 모양을 어느 안으로 할까요?');
// No question to answer here (a wait answered elsewhere): the row opens the panel as the rest do.
assert.equal(strip({brief:()=>null}).hits[2].action,'unfold');
// Only failed routes' titles; none failed, the running ones'.
const running=text(strip({},{route_label:'route 2 ⠏2',routes:model.routes.slice(0,2),route_count:2,route_total:2}))[3].trimEnd();
assert.equal(running,'2 ROUTE ⠏2 · ⠸ 패널 자동 접힘 기준 60칸 · ⠸ LAB 표 칸 정리 │ 2 LAB 실패 1 │ MESSAGES ←2');
// No answer wait: what to do is compaction imminent, then a tidy notice; nothing to do, no row: the
// routes row moves up and the last row stays empty.
assert.equal(text(strip({},{wait:'',warning:'압축 임박',tidy_notice:'새 판 대기'}))[2].trimEnd(),'⚠ 압축 임박 │ ⚠ 새 판 대기');
// A wait answered elsewhere says where, dim, after the wait (#1633's `wait_guide`); alone without a wait here.
assert.equal(text(strip({},{wait_guide:'1 QUESTIONS · 아래 입력창에 답'}))[2].trimEnd(),'◑답 대기 1 · 접힌 모양을 어느 안으로 할까요? │ 1 QUESTIONS · 아래 입력창에 답');
assert.equal(text(strip({},{wait:'',wait_guide:'1 QUESTIONS · 아래 입력창에 답'}))[2].trimEnd(),'1 QUESTIONS · 아래 입력창에 답');
assert(strip({},{wait:'',wait_guide:'1 QUESTIONS · 아래 입력창에 답'}).rows[2].find(x=>x.text.startsWith('1 QUESTIONS')).dim);
const calm=text(strip({},{wait:'',messages_new:0,lab:null}));
assert.deepEqual(calm.map(s=>s.trimEnd()).slice(2),['3 ROUTE ⠏2 ✕1 · ✕ 휠 입력 시험','']);
// The strip's height is the host's alone: the same four rows with and without a question.
assert.equal(calm.length,4);
// Every harness, state and color setting keeps the shapes; idle `●`, waiting `◑`, unknown no glyph.
for(const [state,lead] of [['idle','● [e9]'],['blocked','◑ [e9]'],['unknown','[e9]']])assert(text(strip({state}))[1].startsWith(lead+' hearting'),state);
assert.deepEqual(text(strip({},{no_color:true})),rows);assert.deepEqual(text(strip({configuration:{theme:'mono'}})),rows);
""")

    def test_each_strip_row_gives_way_from_its_end(self):
        # The most pressing first; short of cells whole items go from the end. The session row: the reset
        # times, the effort, the project (as draft 다), then 5h, the model windows, 7d, the model, CTX. What
        # to do: the question's first row is cut with `…` before the wait goes. The routes row (11-15): the
        # titles first (the first cut with `…` while it keeps four cells), then the groups from the last
        # (the new messages, LAB), the count last.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const P=t=>({text:t});
const model={tag:'[e9]',repo_side:[P('hearting')],selected:[P('Opus 5.5'),P(' '),P('xhigh')],context_side:[P('━━━━━━────'),P(' 58%')],
 quota:[P('5h '),P('12%'),P('(↻ 2h 0m)'),P(' │ '),P('7d '),P('66%'),P('(↻ 5d 0h)')],wait:'◑답 대기 1',
 route_label:'route 3 ⠏2 ✕1',route_count:3,messages_new:2,routes:[{title:'a',kind:'working'},{title:'b',kind:'working'},{title:'휠 입력 시험',kind:'dead',paint:{glyph:P('✕')}}],
 lab:{count:'2',counts:[[P('실패 1')]],strip_attention:true}};
const question=()=>({count:1,rows:[[P('◑ '),P('접힌 모양을 어느 안으로 할까요?')]],one:[[P('◑ '),P('접힌 모양을 어느 안으로 할까요?')]],line:[P('접힌 모양을 어느 안으로 할까요?')],choices:0});
const rows=w=>v.render({width:w,height:4,folded:true,model,state:'idle',harness:'claude',brief:question}).rows.map(r=>r.map(x=>x.text).join('').trimEnd());
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
// (A row keeps one blank cell at its right end: `w` columns hold `w - 1` cells.)
const session=[[90,'● [e9] hearting │ Opus 5.5 xhigh │ CTX ━━━━━━──── 58% │ 5h 12%(↻ 2h 0m) │ 7d 66%(↻ 5d 0h)'],
 [89,'● [e9] hearting │ Opus 5.5 xhigh │ CTX ━━━━━━──── 58% │ 5h 12% │ 7d 66%'],[71,'● [e9] hearting │ Opus 5.5 │ CTX ━━━━━━──── 58% │ 5h 12% │ 7d 66%'],
 [65,'● [e9] │ Opus 5.5 │ CTX ━━━━━━──── 58% │ 5h 12% │ 7d 66%'],[56,'● [e9] │ Opus 5.5 │ CTX ━━━━━━──── 58% │ 7d 66%'],
 [47,'● [e9] │ Opus 5.5 │ CTX ━━━━━━──── 58%'],[38,'● [e9] │ CTX ━━━━━━──── 58%'],[27,'● [e9]']];
for(const [w,want] of session)assert.equal(rows(w)[1],want,String(w));
const todo=[[45,'◑답 대기 1 · 접힌 모양을 어느 안으로 할까요?'],[40,'◑답 대기 1 · 접힌 모양을 어느 안으로 …'],[21,'◑답 대기 1 · 접힌 …'],[15,'◑답 대기 1']];
for(const [w,want] of todo)assert.equal(rows(w)[2],want,String(w));
const routes=[[60,'3 ROUTE ⠏2 ✕1 · ✕ 휠 입력 시험 │ 2 LAB 실패 1 │ MESSAGES ←2'],[59,'3 ROUTE ⠏2 ✕1 · ✕ 휠 입력 시… │ 2 LAB 실패 1 │ MESSAGES ←2'],
 [48,'3 ROUTE ⠏2 ✕1 │ 2 LAB 실패 1 │ MESSAGES ←2'],[44,'3 ROUTE ⠏2 ✕1 │ 2 LAB 실패 1 │ MESSAGES ←2'],[36,'3 ROUTE ⠏2 ✕1 │ 2 LAB 실패 1'],
 [31,'3 ROUTE ⠏2 ✕1 │ 2 LAB 실패 1'],[24,'3 ROUTE ⠏2 ✕1 · ✕ 휠 …'],[15,'3 ROUTE ⠏2 ✕1'],[12,'3 ROUTE ⠏2…']];
for(const [w,want] of routes)assert.equal(rows(w)[3],want,String(w));
// No row runs past the window, at any width.
for(let w=1;w<=200;w++)for(const row of v.render({width:w,height:4,folded:true,model,state:'working',harness:'claude',brief:question}).rows)
 assert(cells(row.map(x=>x.text).join(''))===w,w+' '+row.map(x=>x.text).join(''));
""")

    def test_the_strip_is_four_rows_and_fewer_in_a_short_window(self):
        # The strip's height follows the window's rows alone (never what it shows), so a question coming or
        # going never moves the native area: four rows from 24 window rows, three from 16, else two; the
        # routes row goes first, then the session row: what to do stays to the last, and a row with nothing on
        # it gives its place. A window of two rows keeps one strip row (its own `▴`).
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs')),v=require(path.join(process.argv[1],'panel-view.cjs'));
assert.deepEqual(d.FOLD_ROWS,[24,16]);
assert.deepEqual([60,40,24,23,16,15,12,6,3,2,1].map(d.foldRows),[4,4,4,3,3,2,2,2,2,1,1]);
const P=t=>({text:t});
const model={tag:'[e9]',repo_side:[P('hearting')],selected:[P('Opus 5.5')],context_side:[P('━━── 58%')],wait:'◑답 대기 1',route_label:'route 1 ⠏1',route_count:1,routes:[{title:'run',kind:'working'}]};
const draw=(height,m={})=>v.render({width:60,height,folded:true,model:{...model,...m},state:'idle',harness:'claude',brief:()=>({count:1,rows:[[P('◑ '),P('Q?')]],one:[[P('◑ '),P('Q?')]],line:[P('Q?')],choices:0})}).rows.map(r=>r.map(x=>x.text).join('').trimEnd());
assert.deepEqual(draw(4),['─'.repeat(58)+' ▴','● [e9] hearting │ Opus 5.5 │ CTX ━━── 58%','◑답 대기 1 · Q?','1 ROUTE ⠏1 · · run']);
assert.deepEqual(draw(3),['─'.repeat(58)+' ▴','● [e9] hearting │ Opus 5.5 │ CTX ━━── 58%','◑답 대기 1 · Q?']);
assert.deepEqual(draw(2),['─'.repeat(58)+' ▴','◑답 대기 1 · Q?']);
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0),one='◑답 대기 1 · Q?';
assert.deepEqual(draw(1),[one+' '.repeat(59-cells(one))+'▴']);
// Nothing to do: the session row, then the routes, take the places; the height stays.
assert.deepEqual(draw(3,{wait:''}),['─'.repeat(58)+' ▴','● [e9] hearting │ Opus 5.5 │ CTX ━━── 58%','1 ROUTE ⠏1 · · run']);
assert.deepEqual(draw(2,{wait:''}),['─'.repeat(58)+' ▴','● [e9] hearting │ Opus 5.5 │ CTX ━━── 58%']);
for(const h of [1,2,3,4])assert.equal(draw(h,{wait:''}).length,h);
""")

    def test_folded_the_host_gives_the_strip_its_rows_and_claudes_row_the_notice(self):
        # The host's own paths: folded, the panel's height is native-display's foldRows (the window's
        # rows alone) and the native keeps the rest at the whole width. Spec rule 49 (c), 11-14 night
        # ("같은 값을 두 번 보이지 않는다"): while the strip paints its session row, the row under Claude's
        # input keeps the notice (what to do now) as when the panel is open, so the status values show
        # once, in the strip; the strip carries no Git or job row. Strip rows with what to do alone
        # leave Claude its own status row, as an open panel short of rows does.
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const pick=(start,end)=>{const at=text.indexOf(start);assert(at>=0,start);return text.slice(at,text.indexOf(end,at));};
const code=pick('function geometry(){','function panelColumns(){')+pick('function nativeColumns(){','function panelRow(){')
 +pick('function maxHeight(){','\n')+pick('function height(){','function schedule()')+pick('function claudePanelPainted(){','// The blanked Claude row');
const d=require(path.join(process.argv[1],'native-display.cjs'));
const host=(cols,rows,expanded)=>{
 const c={nativeDisplay:d,cols,rows,expanded,foldChoice:null,displayConfiguration:{layout:'side',side_width:40,position:'right',bottom_rows:14},settings:undefined,dragging:false,
  block:{focused:false,leave(){}},fleetView:null,currentDelivery:null,panelContent:()=>Array(30).fill([]),
  harness:'claude',relay:false,ended:false,activated:true,sid:'s',paintedGroups:new Set(),
  panelModel:{head:[{text:'[e9] hearting'}],git:[{text:'(main)'}],context:[{text:'context 58%'}],quota:[{text:'5h 12%'}],jobs:[{text:'>_ job'}],wait:'◑ 1',route_count:2}};
 vm.createContext(c);vm.runInContext(code,c);return c;};
for(const rows of [60,40,24,23,16,15,12,6]){
 const c=host(160,rows,false),strip=d.foldRows(rows);
 assert.deepEqual([c.height(),c.nativeRows(),c.nativeColumns(),c.geometry().side],[strip,rows-strip,160,false],String(rows));
}
// Folded beside or under, the native keeps every column.
for(const position of ['right','left']){const c=host(120,40,false);c.displayConfiguration.position=position;assert.equal(c.nativeColumns(),120);}
const c=host(160,40,false);
const painted=(expanded,groups,extra={})=>{Object.assign(c,{expanded,paintedGroups:new Set(groups)},extra);return c.claudePanelPainted();};
// Folded: the strip's session row takes the status row's place (no Git or job row needed).
assert.equal(painted(false,['head','context','quota','wait','route']),true);
assert.equal(painted(false,['head','context']),true);
// Folded to what to do alone: Claude keeps its own status row.
assert.equal(painted(false,['wait']),false);
// Open: every group the row carries, as before (#1635).
assert.equal(painted(true,['head','context','quota','wait','route']),false);
assert.equal(painted(true,['head','git','context','quota','jobs','wait','route']),true);
// Hidden (Fleet over the window) or another harness: Claude's row stays.
assert.equal(painted(false,['head'],{fleetView:{}}),false);
assert.equal(painted(false,['head'],{fleetView:null,harness:'codex'}),false);
""")

    def test_messages_new_counts_what_arrived_since_the_last_input(self):
        # The strip's `MESSAGES ←N`: kept messages received (`←`) after the person's last input in
        # this session; every kept one before any input; sent ones never.
        import importlib, sys as _sys
        _sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
        shell = importlib.import_module("session_shell")
        rows = [{"way": "←", "at": 100}, {"way": "→", "at": 150}, {"way": "←", "at": 200}, {"way": "←", "at": 300}, {"way": "←"}]
        self.assertEqual(shell.messages_since_input(rows, {"human": {"at": 150}}), 2)
        self.assertEqual(shell.messages_since_input(rows, {"human": {"at": 300}}), 0)
        self.assertEqual(shell.messages_since_input(rows, {"human": None}), 3)
        self.assertEqual(shell.messages_since_input(rows, {}), 3)
        self.assertEqual(shell.messages_since_input([], {"human": {"at": 1}}), 0)

    def test_one_judgment_decides_the_fold_and_the_native_width(self):
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs'));
const configuration={layout:'side',side_width:40,side_min_columns:100,position:'right'};
const fold=(cols,choice=null,pinned=false)=>d.fold({cols,choice,configuration,pinned});
// (d) The threshold is one constant: 60 native columns beside the open panel (cols - 40 - 2).
assert.equal(d.NATIVE_MIN,60);
assert.equal(fold(102).folded,false);assert.equal(fold(101).folded,true);
for(const width of [32,60,80])assert.equal(d.fold({cols:width+62,configuration:{...configuration,side_width:width}}).folded,false),
 assert.equal(d.fold({cols:width+61,configuration:{...configuration,side_width:width}}).folded,true);
// #1586 review follow-up 2: a panel folded by itself opens again only three columns past the
// threshold (63 native columns), so a window dragged across it does not jump back and forth; an
// open panel still folds under 60.
const now=(cols,expanded)=>d.fold({cols,configuration,expanded}).folded;
assert.equal(d.REOPEN_MARGIN,3);
assert.deepEqual([101,102,103,104,105].map(c=>now(c,false)),[true,true,true,true,false]);
assert.deepEqual([105,102,101].map(c=>now(c,true)),[false,false,true]);
// The old `side_min_columns` does not take part.
assert.equal(d.fold({cols:130,configuration:{...configuration,side_min_columns:1000}}).folded,false);
// A hand fold stays folded however wide; a hand unfold stays open however narrow.
assert.deepEqual(fold(300,{folded:true}),{folded:true,choice:{folded:true}});
for(const cols of [100,40,140])assert.deepEqual(fold(cols,{folded:false}),{folded:false,choice:{folded:false}});
// An open settings editor or question keeps an open panel open; the bottom layout never folds by itself.
assert.equal(fold(100,null,true).folded,false);
assert.equal(d.fold({cols:40,configuration:{...configuration,layout:'bottom'}}).folded,false);
assert.equal(d.fold({cols:40,choice:{folded:true},configuration:{...configuration,layout:'bottom'}}).folded,true);
// The sizes the native is told: open beside (cols - width - 2); folded, the whole width over the
// strip (rows - its rows), in every placement; under (cols).
const at=(cols,expanded,extra={})=>d.layout({cols,rows:40,expanded,panelRows:expanded?1:d.foldRows(40),configuration:{...configuration,...extra}});
assert.equal(at(180,true).nativeCols,138);assert.equal(at(102,true).nativeCols,60);
for(const extra of [{},{position:'left'},{layout:'bottom'}])for(const cols of [180,101]){
 const g=at(cols,false,extra);
 assert.deepEqual([g.side,g.folded,g.left,g.nativeCols,g.nativeRows,g.panelCols,g.nativeColumn,g.panelColumn,g.gap],[false,true,false,cols,36,cols,0,0,0],JSON.stringify(extra));
}
// Opened by hand where the panel cannot stand beside (native under 48), it opens under the native area.
assert.deepEqual([at(80,true).side,at(80,true).nativeCols],[false,80]);
""")

    def test_the_panel_folds_by_itself_only_under_sixty_native_columns(self):
        # 11-14 night ("자동으로 접히는 기준이 타이트하고"): the open panel folds by itself only where it would
        # leave the native area under 60 columns (it was 80), and opens again three columns past it.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs'));
for(const width of [40,54]){
 const configuration={layout:'side',side_width:width,position:'right'};
 const folded=(cols,expanded=true)=>d.fold({cols,configuration,expanded}).folded;
 const line=width+2+60;  // 102 columns for a 40-column panel, 116 for a 54-column one
 // Open, the native keeps 60 columns beside it; one column fewer folds it.
 assert.deepEqual([line+20,line,line-1].map(c=>folded(c)),[false,false,true],String(width));
 assert.equal(d.layout({cols:line,rows:40,expanded:true,configuration}).nativeCols,60);
 // A window between the old line (80 native columns) and the new one stays open.
 assert.deepEqual([width+2+79,width+2+70].map(c=>folded(c)),[false,false],String(width));
 // Folded by itself, it opens again at 63 native columns and not before.
 assert.deepEqual([line,line+2,line+3].map(c=>folded(c,false)),[true,true,false],String(width));
}
""")

    def test_a_hand_choice_holds_at_every_width_and_only_no_choice_is_judged(self):
        # 11-14 night ("창 크기 조절할때마다 자동으로 접히는게 좀 번거롭네"): a panel opened by hand stays open
        # however the window is resized (under the native area where it cannot stand beside, beside
        # again when the width returns); folded by hand it stays folded; only without a hand choice
        # does the 60-column judgment fold and open it. The host's own fold path, resize by resize.
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function geometry(){'),text.indexOf('function panelColumns(){'));
const seedLine=text.split('\n').find(line=>line.includes('const choice=seed.foldChoice;'));
const host=(cols,expanded=true)=>{
 const c={nativeDisplay:require(path.join(process.argv[1],'native-display.cjs')),cols,rows:40,expanded,foldChoice:null,
  displayConfiguration:{layout:'side',side_width:40,position:'right'},settings:undefined,dragging:false,block:{focused:false,leave(){}}};
 vm.createContext(c);vm.runInContext(code,c);return c;};
// One window resize (host.cjs: applyLayout, then refold), then what stands: open beside, open under, or folded.
const resize=(c,cols)=>{c.cols=cols;c.refold();const g=c.geometry();return c.expanded?(g.side?'beside':'under'):'folded';};
const press=(c,folded)=>{c.foldByHand(folded);c.refold();};
// (1) Opened by hand where it had folded by itself: open at every width, narrower and wider, again and again.
let c=host(95);assert.equal(resize(c,95),'folded');press(c,false);assert.equal(resize(c,95),'beside');
const widths=[70,100,60,130,95,101,200,90,61,140];
assert.deepEqual(widths.map(w=>resize(c,w)),widths.map(w=>w-42>=48?'beside':'under'));
// (2) Folded by hand: folded however wide.
c=host(180);assert.equal(resize(c,180),'beside');press(c,true);
assert.deepEqual([300,400,120,180,90].map(w=>resize(c,w)),Array(5).fill('folded'));
// (3) No hand choice: the 60-column judgment folds it (a 40-column panel under 102 columns) and opens
// it again past the margin (105).
c=host(180);
assert.deepEqual([180,102,101,104,105,102,101,160].map(w=>resize(c,w)),['beside','beside','folded','folded','beside','beside','folded','beside']);
// (4) Opened by hand too narrow to stand beside (under 48 native columns): under the native area, and
// beside again, still open, once the width returns.
c=host(85,false);assert.equal(resize(c,85),'folded');press(c,false);
assert.deepEqual([85,95,80,180,89,90].map(w=>resize(c,w)),['under','beside','under','beside','under','beside']);
// A host hand-over from the earlier release carries `{folded:false,cols}`: it holds at every width too.
c=host(100);vm.runInContext('{const seed={foldChoice:{folded:false,cols:100}};'+seedLine.trim()+'}',c);
assert.deepEqual([100,70,140,60].map(w=>resize(c,w)),['beside','under','beside','under']);
""")

    def test_no_window_size_leaves_the_native_area_without_a_cell(self):
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs'));
// The eight-cell strip and its rules are gone: one folded form, under the native area.
assert(!('STRIP' in d)&&!('STRIP_NATIVE_MIN' in d));
for(const file of ['native-display.cjs','panel-view.cjs','host.cjs'])assert(!/\bSTRIP(_GAP|_NATIVE_MIN)?\b/.test(fs.readFileSync(path.join(process.argv[1],file),'utf8')),file);
// Folded, at every width 20-200 and height 6-60, in every placement: the native keeps every column and
// a row, and the strip (the host's foldRows) ends on the window's last row.
for(const extra of [{},{position:'left'},{layout:'bottom'}])for(let cols=20;cols<=200;cols++)for(let rows=6;rows<=60;rows++){
 const strip=d.foldRows(rows),g=d.layout({cols,rows,expanded:false,configuration:{layout:'side',side_width:40,...extra},panelRows:strip});
 const seen=JSON.stringify({cols,rows,extra,g});
 assert(!g.side&&g.folded&&g.nativeCols===cols&&g.panelCols===cols&&g.nativeRows>=1&&strip>=1,seen);
 assert.equal(g.nativeRows+strip,rows,seen);
}
// Every width 1-40 and height 1-12, open or folded, beside or under: the native keeps a cell and a row.
for(const extra of [{},{position:'left'},{layout:'bottom'}])for(const expanded of [true,false])
 for(let cols=1;cols<=40;cols++)for(let rows=1;rows<=12;rows++)for(const panelRows of [undefined,0,1,rows-1,rows,rows+3]){
  const g=d.layout({cols,rows,expanded,configuration:{layout:'side',side_width:40,...extra},panelRows});
  const seen=JSON.stringify({cols,rows,expanded,extra,panelRows,g});
  assert(g.nativeCols>=1&&g.nativeRows>=1,seen);
  assert.equal(g.side?g.nativeCols+g.gap+g.panelCols:g.nativeCols,cols,seen);
  assert.equal(g.nativeRows,g.side?rows:Math.max(1,rows-(panelRows??1)),seen);
 }
""")

    def test_a_press_on_the_targets_drawn_for_this_placement_counts_while_a_layout_is_queued(self):
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
// Review 1597 follow-up (11-13): a press made while a layout was queued was dropped even when the
// targets on screen were drawn for the very placement it was made in. The placement decides alone:
// the same placement counts; a press on targets drawn for another placement is still dropped.
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function panelMouse('),text.indexOf('function displayReporter('));
const g={side:true,folded:false,left:false,panelColumn:90,panelCols:40},place='true:true:false:false:40:130:30:false';
let calls=[];
const c={fleetView:null,paste:false,relay:false,dragging:false,settings:null,routeDetail:false,routeArea:null,
  geometry:()=>g,panelColumns:()=>40,panelRow:()=>0,height:()=>30,terminal:{modes:{mouseTrackingMode:'none'}},
  layoutQueued:false,panelHitsPlacement:place,placement:()=>place,
  panelHits:[{row:1,left:9,right:14,action:'question'}],panelAction:action=>calls.push(action)};
vm.createContext(c);vm.runInContext(code,c);
for(const [queued,stamp,expected] of [[false,place,['question']],[true,place,['question']],[false,'old',[]],[true,'old',[]]]){
  c.layoutQueued=queued;c.panelHitsPlacement=stamp;calls=[];
  assert.equal(c.panelMouse(0,101,2,false),true);
  assert.deepEqual(calls,expected,JSON.stringify({queued,stamp}));
}
""")


    def test_the_strip_carries_labs_attention_at_the_routes_row_and_opens_its_popup(self):
        # The strip under the native area (11-15, popup design E): only a LAB that needs a look (failed,
        # stuck, unconfirmed, unnamed runs, as the board counts them) stands on the routes row as its
        # count, its dark gray name tag and its first count (`3 LAB 실패 1`); a plain count draws nothing
        # there. Short of cells the groups go from the last (the new messages, then LAB). It never colors
        # the strip's rule or the session row, and a press on it opens the LAB popup.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const styles={dim:{dim:true},loc_repo:{foreground:252},lvl_r:{foreground:217},lvl_y:{foreground:186},g_spin:{foreground:230}};
const T=(t,k)=>({text:t,...(styles[k]||{})});
const lab=(word,key,attention)=>({count:'3',counts:[[T(word,key)]],strip_attention:attention});
const strip=(model={},o={})=>v.render({width:60,height:4,folded:true,model:{tag:'[e9]',styles,route_label:'route 2 ⠏2',route_count:0,...model},state:'idle',harness:'claude',questions:()=>[],...o});
const text=r=>r.rows.map(row=>row.map(x=>x.text).join('').trimEnd());
for(const [l,want] of [[lab('실패 1','lvl_r',true),'2 ROUTE ⠏2 │ 3 LAB 실패 1'],[lab('미확인 1','lvl_y',true),'2 ROUTE ⠏2 │ 3 LAB 미확인 1'],
  [lab('진행 중 2','g_spin',false),'2 ROUTE ⠏2']])
 assert.equal(text(strip({lab:l}))[2],want,JSON.stringify(l));
// The count keeps the board's color, `LAB` the name tag's dim.
const row=strip({lab:lab('실패 1','lvl_r',true)}).rows[2];assert(row.find(x=>x.text==='실패 1').foreground===217&&row.find(x=>x.text==='LAB').dim);
// Short of cells the new messages go first, then LAB.
assert.equal(text(strip({lab:lab('실패 1','lvl_r',true),messages_new:3},{width:40}))[2],'2 ROUTE ⠏2 │ 3 LAB 실패 1 │ MESSAGES ←3');
assert.equal(text(strip({lab:lab('실패 1','lvl_r',true),messages_new:3},{width:30}))[2],'2 ROUTE ⠏2 │ 3 LAB 실패 1');
assert.equal(text(strip({lab:lab('실패 1','lvl_r',true),messages_new:3},{width:20}))[2],'2 ROUTE ⠏2');
// A press on it opens the LAB popup; on the route count, the routes popup.
const hits=strip({lab:lab('실패 1','lvl_r',true)}).hits.filter(h=>h.row===2&&h.action!=='unfold');
assert.deepEqual(hits.map(h=>[h.left,h.right,h.action]),[[0,9,'popup:ROUTE'],[13,24,'popup:LAB']]);
// No board, no LAB; the rule and the session row are the session's own whatever the board says.
assert(!text(strip({lab:null})).join('').includes('LAB'));
for(const state of ['idle','working','blocked'])
 assert.deepEqual(strip({lab:lab('실패 3','lvl_r',true)},{state,frame:2}).rows.slice(0,2),strip({},{state,frame:2}).rows.slice(0,2),state);
for(const harness of ['codex','opencode'])assert.deepEqual(text(strip({lab:lab('미확인 2','lvl_y',true)},{harness})),text(strip({lab:lab('미확인 2','lvl_y',true)})));
""")

    def test_a_press_on_a_section_opens_its_popup_where_it_was_drawn(self):
        # 11-15 (popup design E): a press on a section's rows (LAB here) opens that section's popup over
        # the window, nothing opens in place; a press on targets drawn for another placement is dropped
        # (review 1597 follow-up).
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function panelMouse('),text.indexOf('function displayReporter('))
 +text.slice(text.indexOf('function panelAction('),text.indexOf("let settingsTail=''"));
const g={side:true,folded:false,left:false,panelColumn:90,panelCols:40},place='true:true:false:false:40:130:30:false';
const opened=[];
const c={fleetView:null,paste:false,relay:false,dragging:false,settings:null,epoch:0,empty:true,SECTIONS:['QUESTIONS','ROUTE','LAB','SESSIONS','MESSAGES'],
  geometry:()=>g,panelColumns:()=>40,panelRow:()=>0,height:()=>30,terminal:{modes:{mouseTrackingMode:'none'}},
  layoutQueued:false,panelHitsPlacement:place,placement:()=>place,layouts:0,queueLayout(){c.layouts++;},queueDisplay(){},
  openPopup:(name,open='')=>opened.push([name,open]),
  panelHits:[{row:16,left:2,right:37,action:'popup:LAB'},{row:17,left:2,right:37,action:'popup:ROUTE:rt-1'}]};
vm.createContext(c);vm.runInContext(code,c);
assert.equal(c.panelMouse(0,101,17,false),true);assert.deepEqual(opened,[['LAB','']]);
assert.equal(c.panelMouse(0,101,18,false),true);assert.deepEqual(opened,[['LAB',''],['ROUTE','rt-1']]);
// A row with no target: nothing opens.
c.panelMouse(0,101,20,false);assert.equal(opened.length,2);
// Targets drawn for another placement: dropped.
c.panelHitsPlacement='old';c.panelMouse(0,101,17,false);assert.equal(opened.length,2);
assert.equal(c.layouts,0);
""")


if __name__ == "__main__":
    unittest.main()
