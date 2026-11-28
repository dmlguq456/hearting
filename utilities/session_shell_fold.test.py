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
 // (a) The top border is the line alone right of the head; (a), (b) the last row inside ends `⚙   ▾`.
 assert(!rows[0].includes('⚙')&&rows[0].endsWith('─┐'),rows[0]);
 assert(rows.at(-2).endsWith('⚙   ▾ │')&&rows.filter(s=>s.includes('⚙')).length===1,rows.join('\n'));
 const fold=r.hits.find(h=>h.action==='fold'),gear=r.hits.find(h=>h.action==='settings');
 // The presses: `⚙` at panel column 33, `▾` at 37; the cells between them are neither's.
 assert.deepEqual([fold.row,gear.row],[18,18]);
 assert.deepEqual([fold.left,fold.right,gear.left,gear.right],[36,38,32,34]);
 assert(gear.left<=33&&gear.right>=33&&fold.left<=37&&fold.right>=37&&gear.right<35&&fold.left>35,JSON.stringify([fold,gear]));
 // Both marks dim, as the panel's labels.
 const last=r.rows.at(-2);assert(last.find(x=>x.text==='⚙').dim&&last.find(x=>x.text==='▾').dim);
}
// 5h/7d share the row: whole with room, their reset times going first when short.
assert(text(draw({}))[18].startsWith('│ 5h 12%(↻ 2h) │ 7d 66%(↻ 5d)'));
const narrow=text(v.render({width:32,height:20,side:true,model,state:'idle',questions:()=>[]}));
assert(narrow.at(-2).startsWith('│ 5h 12% │ 7d 66%')&&narrow.at(-2).endsWith('⚙   ▾ │'),narrow.at(-2));
// The row stays however short the rows run (the head's last rows go first, boxes stay closed).
for(let h=6;h<=14;h++){const rows=text(draw({height:h}));assert(rows.at(-2).endsWith('⚙   ▾ │'),h+'\n'+rows.join('\n'));
 assert.equal(rows.filter(s=>s.includes('╭')).length,rows.filter(s=>s.includes('╰')).length,h+'\n'+rows.join('\n'));}
// The settings editor keeps its own last row (Esc), no fold button.
assert(!text(draw({settings:{index:0,value:v.defaults}})).join('').includes('▾'));
// (e) The bottom layout and a left panel: the same `⚙   ▾`.
const bottom=text(v.render({width:80,height:14,side:false,model,state:'idle',configuration:{layout:'bottom'},questions:()=>[]}));
assert(bottom.at(-2).endsWith('⚙   ▾ │'),bottom.join('\n'));
assert(text(draw({configuration:{position:'left'}})).at(-2).endsWith('⚙   ▾ │')&&!text(draw({configuration:{position:'left'}})).join('').match(/[»«]/));
// A side panel opened by hand where it cannot stand beside opens under the native area: `▾`
// there too (the host draws it with `side:false`; the configured layout stays `side`).
for(const position of ['right','left']){
 const under=v.render({width:90,height:14,side:false,model,state:'idle',configuration:{layout:'side',position},questions:()=>[]});
 assert(text(under).at(-2).endsWith('⚙   ▾ │')&&!text(under).join('').match(/[»«]/),text(under).join('\n'));
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
for(const [w,s] of want){const t=text(last(w));assert(t.startsWith('│ '+s+(s?' ':''))&&t.endsWith('⚙   ▾ │')&&!t.includes('…'),w+' '+t);}
// The model word keeps its model color; nothing of the row is cut mid-value.
assert.equal(last(40).find(x=>x.text==='fable ').foreground,176);
// Folded, the strip's one row carries the same values (spec rule 49 (c), 11-15 one row): short of cells
// the reset times go, then the model windows, then 7d, then 5h, then the model, then the project.
const strip=w=>v.render({width:w,height:1,folded:true,model:{...model,tag:'[e9]'},state:'idle',questions:()=>[]}).rows.map(text)[0].replace(/ +▴$/,'');
assert.equal(strip(120),'● [e9] hearting/ │ Checking │ 5h 12%(↻ 2h) │ 7d 66%(↻ 5d) │ fable 70%');
assert.equal(strip(60),'● [e9] hearting/ │ Checking │ 5h 12% │ 7d 66% │ fable 70%');
assert.equal(strip(52),'● [e9] hearting/ │ Checking │ 5h 12% │ 7d 66%');
assert.equal(strip(40),'● [e9] hearting/ │ Checking │ 5h 12%');
assert.equal(strip(30),'● [e9] hearting/ │ Checking');
assert.equal(strip(20),'● [e9] hearting/');
""")

    def test_the_folded_strip_is_one_row_under_the_native_area(self):
        # 11-15 ("최소로 접었을때 패널이 좀 못생겼어 ... 전체를 한줄로만 만들자"; spec rule 49 (c) changed): one row
        # under the native area, whatever the window's height and whatever it shows: the session │ the
        # answer wait in its red │ `N ROUTE` and its counts (no titles) │ `MESSAGES` and the sessions the
        # recent messages came from or went to (each way and session once, the newest first) … `▴`.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const styles={g_spin:{foreground:230},g_blocked:{foreground:217},g_work_off:{foreground:150,dim:true},dim:{dim:true},loc_repo:{foreground:252},
 g_idle:{foreground:186},g_dead:{foreground:210},lvl_r:{foreground:210},lvl_y:{foreground:186},lvl_g:{foreground:150},tag_steward:{foreground:219,bold:true}};
const P=(t,k)=>({text:t,...(k?styles[k]||{}:{})});
const message=(way,who)=>({way,who,text:'본문 '+who});
const model={tag:'[e9]',styles,repo_side:[P('hearting','loc_repo')],selected:[P('Opus 5.5'),P(' '),P('xhigh')],context_side:[P('━━━━━━'),P('────','dim'),P(' ','dim'),P('58%')],
 quota:[P('5h ','dim'),P('12%'),P('(↻ 2h 0m)','dim'),P(' │ ','dim'),P('7d ','dim'),P('66%'),P('(↻ 5d 0h)','dim')],wait:'◑답 대기 1',
 route_label:'route 3 ⠏2 ✕1',route_count:3,route_total:3,
 routes:[{title:'패널 자동 접힘 기준 60칸',kind:'working',paint:{glyph:P('⠸','g_spin')}},{title:'표 칸 정리',kind:'working',paint:{glyph:P('⠸','g_spin')}},
  {title:'휠 입력 시험',kind:'dead',paint:{glyph:P('✕','g_dead')}}],
 messages:[message('→','[73]'),message('←','[e7]'),message('←','[52]'),message('←','[e7]')],lab:{count:'2',counts:[[P('실패 1','lvl_r')]],strip_attention:true}};
const question=()=>({count:1,label:'QUESTIONS',answerable:true,waits:[{word:'답 대기',answerable:true,open:'S4:f@r',line:[P('접힌 모양을 어느 안으로 할까요?','loc_repo')]}]});
const strip=(o={},m={})=>v.render({width:160,height:1,folded:true,model:{...model,...m},state:'working',frame:3,harness:'claude',brief:question,...o});
const text=r=>r.rows.map(row=>row.map(x=>x.text).join(''));
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
let r=strip(),rows=text(r);
// One row the window's width, one placement for every harness (side or not, left or right).
assert.equal(rows.length,1);assert.equal(cells(rows[0]),160);
assert.equal(rows[0].replace(/ +▴$/,''),'⠸ [e9] hearting │ Opus 5.5 xhigh │ CTX ━━━━━━──── 58% │ 5h 12%(↻ 2h 0m) │ 7d 66%(↻ 5d 0h) │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES ← [e7] ← [52] → [73]');
assert(rows[0].endsWith(' ▴'));
for(const o of [{side:true},{side:true,configuration:{position:'left'}},{harness:'codex'},{harness:'opencode'},{configuration:{layout:'bottom'}}])
 assert.deepEqual(text(strip(o)),rows,JSON.stringify(o));
// No question words, no choices, no route titles, no LAB or SESSIONS on it.
for(const gone of ['접힌 모양','아래 두 줄','휠 입력','패널 자동','LAB','SESSIONS','─ ▴'])assert(!rows[0].includes(gone),gone);
// The wait in the answer wait's red, the name tags dim, `▴` dim; no frame, gear, beat or moving rule:
// only the session's glyph turns.
const parts=r.rows[0];
assert.equal(parts.find(x=>x.text==='◑답 대기 1').foreground,174);
for(const word of ['CTX ','MESSAGES','ROUTE'])assert(parts.find(x=>x.text===word).dim===true,word);
assert(parts.at(-1).text==='▴'&&parts.at(-1).dim);
// (11-15: "대기 중인건 점멸을 시키자": the wait's glyphs are the strip's only moving cells, its blink.)
assert(!/[┌┐└┘⚙⚑]/.test(rows[0])&&!parts.some(x=>x.beat||x.motion),rows[0]);
assert.equal(r.motion.cells.map(c=>c.ch).join(''),'◑답대기1');
assert.deepEqual([r.spins,r.animated],[[0],true]);
assert(parts.every(x=>x.background===undefined),'no ground anywhere on it');
// A press on the wait opens the question popup, on the routes the routes popup, on MESSAGES its popup;
// anywhere else (and `▴`) opens the panel.
const row=[...rows[0]].reduce((o,c)=>{for(let i=0;i<cells(c);i++)o.push(c);return o;},[]);
const at=h=>{let t='',seen=-1;for(let x=h.left;x<=h.right;x++){if(cells(row[x])>1&&x>0&&row[x-1]===row[x]&&seen===x-1){seen=x;continue;}t+=row[x];seen=x;}return t;};
const spans=r.hits.filter(h=>h.action!=='unfold');
assert.deepEqual(spans.map(h=>h.action),['question','popup:ROUTE','popup:MESSAGES']);
assert(at(spans[0]).startsWith('◑')&&at(spans[1])==='3 ROUTE ⠏2 ✕1'&&at(spans[2]).startsWith('MESSAGES ← [e7]'),spans.map(at).join('|'));
assert.deepEqual(r.hits.at(-1),{row:0,left:0,right:159,action:'unfold'});
// No wait: compaction imminent and a tidy notice stand in its place; nothing at all, no such group.
assert(text(strip({},{wait:'',warning:'압축 임박',tidy_notice:'새 판 대기'}))[0].includes('7d 66%(↻ 5d 0h) │ ⚠ 압축 임박 │ ⚠ 새 판 대기 │ 3 ROUTE'));
const calm=text(strip({},{wait:'',messages:[]}))[0].replace(/ +▴$/,'');
assert(calm.endsWith('7d 66%(↻ 5d 0h) │ 3 ROUTE ⠏2 ✕1'),calm);
// No routes and no messages: the session alone.
assert.equal(text(strip({},{wait:'',messages:[],routes:[],route_count:0,route_total:0,route_label:''}))[0].replace(/ +▴$/,''),
 '⠸ [e9] hearting │ Opus 5.5 xhigh │ CTX ━━━━━━──── 58% │ 5h 12%(↻ 2h 0m) │ 7d 66%(↻ 5d 0h)');
// A wait answered elsewhere is not on it (the panel and the popup say where).
assert(!text(strip({},{wait:'',wait_guide:'1 QUESTIONS · 아래 입력창에 답'}))[0].includes('QUESTIONS'));
// Every state keeps its glyph; no color and mono keep the words.
for(const [state,lead] of [['idle','● [e9]'],['blocked','◑ [e9]'],['unknown','[e9]']])assert(text(strip({state}))[0].startsWith(lead+' hearting'),state);
assert.deepEqual(text(strip({},{no_color:true})),rows);assert.deepEqual(text(strip({configuration:{theme:'mono'}})),rows);
""")

    def test_the_strip_gives_way_in_its_order(self):
        # 11-15: short of cells the reset times, the branch, the effort, the model windows, 7d, 5h, the
        # model, the messages' numbers (their count stays), MESSAGES, the project, the notices, the CTX
        # bar (its percent stays), the routes' counts go, and narrower than 44 cells `CTX`; the glyph and
        # number, the wait, `N ROUTE` and `▴` stay. Nothing wraps or runs past the window at 1-200 cells.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const P=t=>({text:t});
const model={tag:'[e9]',repo_side:[P('hearting')],git_side:[P('main')],selected:[P('Opus 5.5'),P(' '),P('xhigh')],context_side:[P('━━━━━━'),P('────'),P(' '),P('58%')],
 quota:[P('5h '),P('12%'),P('(↻ 2h 0m)'),P(' │ '),P('7d '),P('66%'),P('(↻ 5d 0h)')],wait:'◑답 대기 1',
 route_label:'route 3 ⠏2 ✕1',route_count:3,routes:[{title:'a',kind:'working'},{title:'b',kind:'working'},{title:'휠 입력 시험',kind:'dead',paint:{glyph:P('✕')}}],
 messages:[{way:'→',who:'[73]'},{way:'←',who:'[e7]'},{way:'←',who:'[52]'},{way:'←',who:'[e7]'}]};
const row=w=>v.render({width:w,height:1,folded:true,model,state:'idle',harness:'claude'}).rows[0].map(x=>x.text).join('').replace(/ +▴$/,'');
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const want=[[200,'● [e9] hearting (main) │ Opus 5.5 xhigh │ CTX ━━━━━━──── 58% │ 5h 12%(↻ 2h 0m) │ 7d 66%(↻ 5d 0h) │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES ← [e7] ← [52] → [73]'],
 [140,'● [e9] hearting (main) │ Opus 5.5 xhigh │ CTX ━━━━━━──── 58% │ 5h 12% │ 7d 66% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES ← [e7] ← [52] +1'],
 [120,'● [e9] hearting │ Opus 5.5 │ CTX ━━━━━━──── 58% │ 5h 12% │ 7d 66% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES ← [e7] +2'],
 [110,'● [e9] hearting │ Opus 5.5 │ CTX ━━━━━━──── 58% │ 5h 12% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES ← [e7] +2'],
 [100,'● [e9] hearting │ Opus 5.5 │ CTX ━━━━━━──── 58% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES ← [e7] +2'],
 [96,'● [e9] hearting │ CTX ━━━━━━──── 58% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES ← [e7] ← [52] +1'],
 [85,'● [e9] hearting │ CTX ━━━━━━──── 58% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1 │ MESSAGES 3'],
 [75,'● [e9] hearting │ CTX ━━━━━━──── 58% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1'],
 [60,'● [e9] │ CTX ━━━━━━──── 58% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1'],
 [50,'● [e9] │ CTX 58% │ ◑답 대기 1 │ 3 ROUTE ⠏2 ✕1'],
 [44,'● [e9] │ CTX 58% │ ◑답 대기 1 │ 3 ROUTE'],
 [34,'● [e9] │ ◑답 대기 1 │ 3 ROUTE']];
for(const [w,s] of want)assert.equal(row(w),s,String(w));
// No row runs past the window, at any width; the wait and `N ROUTE` stand whole from 34 cells.
for(let w=1;w<=200;w++){const r=v.render({width:w,height:1,folded:true,model,state:'working',harness:'claude'});
 const t=r.rows[0].map(x=>x.text).join('');assert(r.rows.length===1&&cells(t)===w&&t.endsWith('▴'),w+' '+t);
 if(w>=34)assert(t.includes('◑답 대기 1')&&t.includes('3 ROUTE')&&t.slice(1).startsWith(' [e9]'),w+' '+t);}
""")

    def test_the_strip_is_one_row_at_every_height(self):
        # 11-15: the strip's height is one row at every window height (never what it shows), so a question
        # coming or going never moves the native area and the native keeps every other row.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs')),v=require(path.join(process.argv[1],'panel-view.cjs'));
assert(!('FOLD_ROWS' in d));
assert.deepEqual([60,40,24,23,16,15,12,6,3,2,1].map(d.foldRows),[1,1,1,1,1,1,1,1,1,1,1]);
const P=t=>({text:t});
const model={tag:'[e9]',repo_side:[P('hearting')],selected:[P('Opus 5.5')],context_side:[P('━━── 58%')],wait:'◑답 대기 1',route_label:'route 1 ⠏1',route_count:1,routes:[{title:'run',kind:'working'}]};
const draw=(m={})=>v.render({width:80,height:1,folded:true,model:{...model,...m},state:'idle',harness:'claude',brief:()=>({count:1,waits:[{word:'답 대기',answerable:false,line:[P('Q?')]}]})}).rows.map(r=>r.map(x=>x.text).join('').replace(/ +▴$/,''));
assert.deepEqual(draw(),['● [e9] hearting │ Opus 5.5 │ CTX ━━── 58% │ ◑답 대기 1 │ 1 ROUTE ⠏1']);
assert.deepEqual(draw({wait:''}),['● [e9] hearting │ Opus 5.5 │ CTX ━━── 58% │ 1 ROUTE ⠏1']);
""")

    def test_folded_the_host_gives_the_strip_its_rows_and_claudes_row_the_notice(self):
        # The host's own paths: folded, the panel's height is native-display's foldRows (the window's
        # rows alone) and the native keeps the rest at the whole width. Spec rule 49 (c), 11-14 night
        # ("같은 값을 두 번 보이지 않는다"), 11-16 ("맨 아래줄이 비면이 아니고 항상 비우라고 그냥"): the row under
        # Claude's input is always the shell's one line (the working folder, `hearting v<version>`), folded or
        # open, whatever groups the panel paints; the panel's notice text no longer stands there. Hidden (Fleet
        # over the window), another harness or an open settings editor leave Claude's row.
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const pick=(start,end)=>{const at=text.indexOf(start);assert(at>=0,start);const to=text.indexOf(end,at);assert(to>at,end);return text.slice(at,to);};
const code=pick('function geometry(){','function panelColumns(){')+pick('function nativeColumns(){','function panelRow(){')
 +pick('function maxHeight(){','\n')+pick('function height(){','function schedule()')+pick('function claudePanelPainted(){','// The release this window runs')
 +pick('// The release this window runs','let claudeStatus=');
const d=require(path.join(process.argv[1],'native-display.cjs'));
const host=(cols,rows,expanded)=>{
 const c={nativeDisplay:d,cols,rows,expanded,foldChoice:null,displayConfiguration:{layout:'side',side_width:40,position:'right',bottom_rows:14},settings:undefined,dragging:false,
  block:{focused:false,leave(){}},fleetView:null,currentDelivery:null,panelContent:()=>Array(30).fill([]),
  harness:'claude',relay:false,ended:false,activated:true,sid:'s',paintedGroups:new Set(),
  myRelease:'/home/u/.local/share/hearting/releases/v4.40.0/utilities/session-shell/host.cjs',cellWidth:t=>[...t].length,
  panelModel:{head:[{text:'[e9] hearting'}],git:[{text:'(main)'}],context:[{text:'context 58%'}],quota:[{text:'5h 12%'}],jobs:[{text:'>_ job'}],wait:'◑ 1',route_count:2,
   notice:[{text:'◑ 1 answer needed'}],footer_path:'~/work/project'}};
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
// 11-16: the row is the shell's line whatever the panel paints; folded to what to do alone it is still the line.
assert.equal(painted(false,['wait']),true);
assert.equal(painted(true,['head','context','quota','wait','route']),true);
assert.equal(painted(true,['head','git','context','quota','jobs','wait','route']),true);
assert.equal(painted(true,[]),true);
// Hidden (Fleet over the window), another harness, an open settings editor or no panel rows: Claude's row stays.
assert.equal(painted(false,['head'],{fleetView:{}}),false);
assert.equal(painted(false,['head'],{fleetView:null,harness:'codex'}),false);
assert.equal(painted(false,['head'],{harness:'claude',settings:{}}),false);
Object.assign(c,{settings:undefined});
// The line that takes the row: the working folder at the left, the release at the right, never the notice text.
const line=c.footerLine(60).replace(/\x1b\[[0-9;]*m/g,'');
assert.equal(line.length,60);
assert(line.startsWith('  ~/work/project')&&line.trimEnd().endsWith('hearting v4.40.0'),JSON.stringify(line));
assert(!line.includes('answer needed'),JSON.stringify(line));
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
// strip (rows - its one row), in every placement; under (cols).
const at=(cols,expanded,extra={})=>d.layout({cols,rows:40,expanded,panelRows:expanded?1:d.foldRows(40),configuration:{...configuration,...extra}});
assert.equal(at(180,true).nativeCols,138);assert.equal(at(102,true).nativeCols,60);
for(const extra of [{},{position:'left'},{layout:'bottom'}])for(const cols of [180,101]){
 const g=at(cols,false,extra);
 assert.deepEqual([g.side,g.folded,g.left,g.nativeCols,g.nativeRows,g.panelCols,g.nativeColumn,g.panelColumn,g.gap],[false,true,false,cols,39,cols,0,0,0],JSON.stringify(extra));
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


    def test_the_strip_carries_no_lab_or_sessions_group(self):
        # 11-15: the LAB and SESSIONS sections left the panel, and their groups the strip, whatever the
        # backend still sends.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const T=t=>({text:t});
const strip=(model={})=>v.render({width:120,height:1,folded:true,model:{tag:'[e9]',route_label:'route 2 ⠏2',route_count:0,...model},state:'idle',harness:'claude'});
const lab={count:'3',counts:[[T('실패 1')]],strip_attention:true},sessions={count:4,counts:[[T('◑1')]],rows:[]};
const with_=strip({lab,sessions}),without=strip();
assert.deepEqual(with_.rows,without.rows);assert.deepEqual(with_.hits,without.hits);
assert(!/LAB|SESSIONS/.test(with_.rows[0].map(x=>x.text).join('')));
assert(!with_.hits.some(h=>/LAB|SESSIONS/.test(h.action)));
""")

    def test_a_press_on_a_section_opens_its_popup_where_it_was_drawn(self):
        # 11-15 (popup design E): a press on a section's rows (MESSAGES here) opens that section's popup over
        # the window, nothing opens in place; a press on targets drawn for another placement is dropped
        # (review 1597 follow-up).
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function panelMouse('),text.indexOf('function displayReporter('))
 +text.slice(text.indexOf('function panelAction('),text.indexOf("let settingsTail=''"));
const g={side:true,folded:false,left:false,panelColumn:90,panelCols:40},place='true:true:false:false:40:130:30:false';
const opened=[];
const c={fleetView:null,paste:false,relay:false,dragging:false,settings:null,epoch:0,empty:true,SECTIONS:['QUESTIONS','ROUTE','MESSAGES'],
  geometry:()=>g,panelColumns:()=>40,panelRow:()=>0,height:()=>30,terminal:{modes:{mouseTrackingMode:'none'}},
  layoutQueued:false,panelHitsPlacement:place,placement:()=>place,layouts:0,queueLayout(){c.layouts++;},queueDisplay(){},
  openPopup:(name,open='')=>opened.push([name,open]),
  panelHits:[{row:16,left:2,right:37,action:'popup:MESSAGES'},{row:17,left:2,right:37,action:'popup:ROUTE:rt-1'}],routeArea:null};
vm.createContext(c);vm.runInContext(code,c);
assert.equal(c.panelMouse(0,101,17,false),true);assert.deepEqual(opened,[['MESSAGES','']]);
assert.equal(c.panelMouse(0,101,18,false),true);assert.deepEqual(opened,[['MESSAGES',''],['ROUTE','rt-1']]);
// A row with no target: nothing opens.
c.panelMouse(0,101,20,false);assert.equal(opened.length,2);
// Targets drawn for another placement: dropped.
c.panelHitsPlacement='old';c.panelMouse(0,101,17,false);assert.equal(opened.length,2);
assert.equal(c.layouts,0);
""")


if __name__ == "__main__":
    unittest.main()
