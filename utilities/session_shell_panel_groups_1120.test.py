"""11-20 (REPO and SESSION groups, the MEM popup): the panel shows no box and no list of the status document itself; its two groups; MESSAGES and MEM go down first.
(11-26 panel B: the REPO group is one row, the SESSION head a plain rule, a message one row; the assertions below follow that.)
(11-21: the REPO group ends in a STATUS part and a BACKLOG part, each a head and its first three items: `session_shell_repo_lists_1121.test.py`; the `STATUS ›` button
on the REPO row is gone, the bottom layout's first row ends in the two buttons.)

The person (11-20, on the installed box): "현황은 미리보기 없이 버튼만 만들자.. 팝업으로 띄워서 사용자가 직관적으로 목록을 볼 수 있게 하는 UI 작업에 집중해". Earlier the same
night: "REPO에 엮어서 구분을 하나 더 두는게 맞을듯" (`REPO ────` / `SESSION ────`) and "MESSAGE, MEM은 좀 더 세로길이 따라서 과감하게 표시 줄을 줄여버려 1~2줄 수준으로".

1. The panel draws no box and no preview of the status document: a value that carries the old lists (`groups`) draws none of them. With a document or without one the
   drawing is the same, letter for letter, and only the head word's brightness differs (dim without a document).
2. Beside the native area the panel has two groups. REPO: its row (the group's head, `REPO hearting · wt 12 ────`, no button; 11-22: GIT, PEERS, STATUS, BACKLOG and MODEL, CTX, TITLE, NOW stand one cell in), GIT, PEERS, then the STATUS and BACKLOG heads. SESSION: its head
   `SESSION ────` stands where the empty row between the two groups' metadata stood (it goes with that row), then MODEL, CTX, TITLE, NOW, QUESTIONS, the route box, MESSAGES, MEM.
3. Short of rows MESSAGES (three at most) and MEM (one row a record) are the first to go down, before the route box or the question give a row. The MEM popup
   (`memoryBody`) reads each record whole in the MESSAGES popup's own frame.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
NODE = shutil.which("node")

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},lvl_r:{foreground:217,bold:true},
  g_blocked:{foreground:217},g_spin:{foreground:230},hb_claude:{foreground:116}};
const t=(text,key='dim')=>({text,...(styles[key]||{dim:key==='dim'})});
const MSGS=[1,2,3,4,5].map(n=>({way:n%2?'←':'→',who:'[7'+n+']',harness:'claude',when:'09:0'+n,kind:'report',text:'시험용 메시지 '+n,body:'시험용 메시지 '+n,delivery:null}));
const MEMS=[1,2,3].map(n=>({kind:'decision',age:n+'m',text:'시험용 기억 '+n,tier:'durable',scope:'project',body:n===1?'시험용 본문 하나가 한 줄을 넘어 이어지는 긴 글이어서 팝업에서만 끝까지 읽힌다 BODY_END':''}));
const route=i=>({id:'rt-'+i,title:'시험용 작업 '+i,kind:'working',state:'working',shape:'one-shot-owner',kind_word:'code',elapsed:(10+i)+'m',tool:'Edit',role:'owner',
  paint:{glyph:t('⠏','g_spin'),harness:t('claude','hb_claude'),model_text:t('Opus 5.5','hb_claude'),effort:t('xhigh','dim'),ctx:t('52%','lvl_g'),gauge5:[t('━━━','lvl_g'),t('──'),t(' '),t('52%','lvl_g')]}});
const G=(kind,name,short,items,count)=>({kind,name,short,items,count:count??items.length});
const GROUPS=[G('running','하는 중','하는 중',['시험용 작업 가','시험용 작업 나','시험용 작업 다']),G('waiting','기다리는 것','기다림',['시험용 질문 둘','시험용 정하기']),
  G('done','최근에 끝난 것','끝난 일',['시험용 끝남 하나','시험용 끝남 둘','시험용 끝남 셋','시험용 끝남 넷']),G('next','나중에 할 것','예정',['시험용 예정 하나','시험용 예정 둘','시험용 예정 셋'])];
const doc=groups=>({word:'STATUS',has_doc:true,...(groups?{groups}:{})});
function model(o={}){
  return {styles,colors:256,tag:'[e9]',repo_side:[t('hearting','loc_repo')],worktrees:[t('wt 12')],repo:[t('hearting','loc_repo')],git_side:[t('branch'),t(' ✓','lvl_g')],git:[t('branch')],
    selected:[t('Opus 5.5','hb_claude'),t(' xhigh')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],context:[t('ctx 41%','lvl_g')],quota:[t('5h ','dim'),t('23%','lvl_g')],
    session_title:'시험용 세션',now:'시험용 NOW',route_label:o.routes?'route '+o.routes:'route 2 ⠏2',route_count:o.routes??2,route_total:o.routes??2,routes:[1,2,3].slice(0,o.routes??2).map(route),
    messages:MSGS.slice(-(o.messages??5)),memory_items:MEMS.slice(0,o.mem??3),status_doc:o.status,
    ...(o.peers?{peers:{groups:[{steward:{tag:'e9',key:'hb_claude',me:true},members:[{tag:'d4',key:'hb_codex'}]}]}}:{})};
}
const brief=()=>({count:1,answerable:true,choices:2,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')],[t('1 ','dim'),t('가','loc_repo'),t(' · ','dim'),t('2 ','dim'),t('나','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')]],line:'시험용 질문'});
const draw=(o={})=>view.render({width:o.width??44,height:o.height??60,side:o.side??true,model:model(o),state:'working',harness:'claude',frame:1000,since:990,
  brief:o.question?brief:undefined,sent:()=>[],cellWidth:cells,configuration:{side_width:o.width??44,rule_motion:'off',theme:'terminal',position:'right'}});
const lines=v=>v.rows.map(str);
const inside=v=>lines(v).map(l=>l.slice(2,-2));
const headAt=(rows,re)=>rows.findIndex(l=>re.test(l));
const count=(v,re)=>inside(v).filter(l=>re.test(l)).length;
"""


@unittest.skipUnless(NODE, "needs node")
class PanelGroupsTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_panel_draws_no_box_and_no_preview_of_the_status_with_a_document_or_without_one(self):
        self.node(r"""
const both=(status,backlog)=>({status,backlog});
// No box, no old list, whatever the backend's value carries: a value with lists (`groups`, an older or a newer backend), a document with none, no document, no value at all.
for(const status of [undefined,doc(),doc(GROUPS),doc([]),{word:'STATUS',has_doc:false},{word:'STATUS',has_doc:false,groups:GROUPS}]){
  for(const side of [true,false]){
    const v=draw({status,side,width:side?44:100,height:side?60:14,peers:true}),ls=lines(v);
    assert(!ls.some(l=>/╭─ STATUS|하는 중|기다리는 것|⠋ 시험용 작업 가/.test(l)),JSON.stringify(status)+'\n'+ls.join('\n'));
    const presses=v.hits.filter(h=>h.action==='popup:STATUS');
    if(status&&status.word){assert.equal(presses.length,1,JSON.stringify(status));assert(side?/^ STATUS +›$/.test(inside(v)[presses[0].row]):/STATUS › │$/.test(ls[1]),ls.join('\n'));}
    else assert.equal(presses.length,0,JSON.stringify(status));
  }
}
// 11-26 (panel B): the REPO row is one row, `REPO   hearting (branch ✓)  wt 12`, with no button.
{const ls=lines(draw({status:doc(GROUPS),height:60}));assert(/^│  REPO   hearting \(branch ✓\)  wt 12 +│$/.test(ls[1]),ls[1]);assert(!draw({status:doc(GROUPS),height:60}).hits.some(h=>h.row===1));}
// The head's word: the section head's title role with a document, plain dim without one; `›` dim in both.
{const row=v=>{const at=inside(v).findIndex(l=>/^ STATUS/.test(l));return v.rows[at];};
 const a=draw({status:doc(GROUPS),height:60}),b=draw({status:{word:'STATUS',has_doc:false},height:60});
 const wa=row(a).find(p=>p.text==='STATUS'),wb=row(b).find(p=>p.text==='STATUS');
 assert.equal(wa.role,'title');assert.equal(wb.role,undefined);assert.equal(wb.dim,true);assert.equal(row(a).findLast(p=>p.text==='›').dim,true);}
// With a document and without one the drawing is the same letter for letter, rows and presses, and the cells differ in the head word's look only (side and bottom panels).
const strip=v=>JSON.stringify(v.rows.map(r=>r.map(p=>p.text==='STATUS'?{text:p.text}:p)));
let n=0;
for(const [side,widths,heights] of [[true,[59,44,36,32],[60,34,24,16]],[false,[100,80,60],[14,8,6]]])
  for(const width of widths)for(const height of heights)for(const question of [false,true])for(const routes of [0,2])for(const peers of [false,true]){
    const o={side,width,height,question,routes,peers,messages:5},a=draw({...o,status:doc(GROUPS)}),b=draw({...o,status:{word:'STATUS',has_doc:false}});
    assert.equal(lines(a).join('\n'),lines(b).join('\n'),JSON.stringify(o));assert.deepEqual(a.hits,b.hits,JSON.stringify(o)+' presses');
    assert.equal(strip(a),strip(b),JSON.stringify(o)+' cells');n++;}
assert(n>=200,String(n));
// The old lists the value carries change nothing at all: the drawing is the one of the value without them.
for(const side of [true,false]){const o={side,width:side?44:100,height:side?40:14,peers:true},a=draw({...o,status:doc(GROUPS)}),b=draw({...o,status:doc()});
  assert.equal(JSON.stringify(a.rows),JSON.stringify(b.rows));assert.deepEqual(a.hits,b.hits);}
""")

    def test_the_repo_group_and_the_session_group_stand_in_their_order(self):
        self.node(r"""
const v=draw({status:doc(),question:true,peers:true,height:70}),ls=inside(v),all=lines(v);
// 11-26 (panel B): the REPO group: its one row (repo, git words in parentheses, wt), no GIT row, then PEERS, the STATUS head, then the SESSION rule (no word).
const g=headAt(ls,/^ GIT /),pe=headAt(ls,/^ PEERS /),st=headAt(ls,/^ STATUS +›$/),se=headAt(ls,/^─+$/),mo=headAt(ls,/^ MODEL /);
assert(/^│  REPO   hearting \(branch ✓\)  wt 12 +│$/.test(all[1]),all[1]);
assert(g<0&&!ls.some(l=>/SESSION/.test(l)),ls.join('\n'));
assert(pe===2&&st===3&&se===4&&mo===se+1,ls.join('\n'));   // (index 0 is the frame's top line, 1 the REPO row)
// The SESSION group: MODEL, CTX, TITLE, NOW, then QUESTIONS, the route box, MESSAGES, MEM.
const order=[/^ MODEL /,/^ CTX /,/^ TITLE /,/^ NOW /,/^╭─ 1 QUESTIONS/,/^╭─ 2 ROUTE/,/^MESSAGES/,/^MEM +›$/].map(re=>headAt(ls,re));
assert(order.every((y,k)=>y>se&&(!k||y>order[k-1])),order+'\n'+ls.join('\n'));
// Only the STATUS head opens the status popup; GIT, PEERS and the SESSION head open nothing.
assert.deepEqual(v.hits.filter(h=>h.action==='popup:STATUS').map(h=>h.row),[st]);
assert(!v.hits.some(h=>[1,pe,se].includes(h.row)&&h.action!=='fold'),JSON.stringify(v.hits));
// 11-26 (panel B): the SESSION head is a plain rule in the messages role (no word); the REPO row has no rule and its label is dim, not the title role.
const seRow=v.rows[se];assert(seRow.some(p=>/^─+$/.test(p.text)&&p.role==='messages')&&!seRow.some(p=>p.text==='SESSION'),JSON.stringify(seRow));
const repoLabel=v.rows[1].find(p=>p.text.trim()==='REPO');assert(repoLabel&&repoLabel.dim===true&&repoLabel.role!=='title'&&!repoLabel.bold,JSON.stringify(v.rows[1]));
// The bottom layout has no group heads (its first row carries the repository and the session together): first row with the buttons, GIT, PEERS, the title and NOW row, the route box.
{const w=draw({status:doc(GROUPS),side:false,width:100,height:20,peers:true}),ws=lines(w);
 const at=re=>ws.findIndex(l=>re.test(l));
 const [r0,gi,pe2,ti,ro]=[/^│ hearting .* STATUS › │$/,/^│ branch /,/^│ PEERS /,/^│ 시험용 세션 │ NOW/,/^│ ╭─ route /].map(at);
 assert(r0===1&&gi===2&&pe2===3&&ti===4&&ro===5,ws.join('\n'));
 assert(!ws.some(l=>/^│ (REPO|SESSION)/.test(l)),ws.join('\n'));
 assert.deepEqual(w.hits.filter(h=>h.action==='popup:STATUS').map(h=>h.row),[1]);}
// 11-26 (panel B): REPO (one row), PEERS, the STATUS head, the SESSION rule, then MODEL.
{const ls=lines(draw({status:{word:'STATUS',has_doc:false},height:40,peers:true}));
 assert(/^│  REPO /.test(ls[1])&&/^│  PEERS /.test(ls[2])&&/^│  STATUS +› │$/.test(ls[3])&&/^│ ─+ │$/.test(ls[4])&&/^│  MODEL /.test(ls[5])&&!ls.some(l=>/ GIT /.test(l)),ls.join('\n'));}
""")

    def test_a_narrow_panel_and_the_session_head_that_folds(self):
        self.node(r"""
// 11-26 (panel B): narrower, the REPO row keeps its label first (`REPO` and one space, then the name).
for(const width of [32,28,20]){const v=draw({status:doc(),width,height:70}),ls=lines(v);
  // (fixes 1 and 2 of #1863: narrower the longer of the name and the branch is cut first, each down to one letter, the status whole: `REPO he… (b… ✓)`)
  assert((/^│  REPO +hearting/.test(ls[1])||width<=28&&/^│  REPO \S+… \((branch|\S+…) ✓\) │$/.test(ls[1]))&&ls.every(l=>cells(l)===width),width+'\n'+ls.join('\n'));}
// 11-26 (panel B): the SESSION head is a plain rule that costs no row of the old plan; short of rows it stands over MODEL's own row and goes once no spare row is left; the REPO row never goes.
const seen=[];
for(let h=70;h>=12;h--){const ls=lines(draw({status:doc(),height:h,peers:true}));
  seen.push([h,ls.some((l,k)=>/^│ ─+ │$/.test(l)&&/MODEL|Opus/.test(ls[k+1]||'')),/^│  REPO /.test(ls[1])]);}
assert(seen.every(r=>r[2]),JSON.stringify(seen));
assert(seen[0][1]===true&&seen.some(r=>r[1]===false),JSON.stringify(seen));
// The SESSION head is gone for every height shorter than the first one that lacks it, within the give-back rule (a height may take it back).
const firstGone=seen.find(r=>!r[1])[0];assert(firstGone>=14&&firstGone<=20,String(firstGone));
""")

    def test_messages_and_mem_go_down_first_before_the_route_box_or_the_question_give_a_row(self):
        self.node(r"""
const full=draw({status:doc(),height:120});
const routeBox=v=>{const ls=inside(v),a=headAt(ls,/^╭─ \d+ ROUTE/);return a<0?0:ls.findIndex((l,i)=>i>a&&/^╰/.test(l))-a;};
const msgRows=v=>count(v,/^ [←→] /),memRows=v=>count(v,/^ [◆◇·] /);
const routeFull=routeBox(full);
assert.deepEqual([msgRows(full),memRows(full)],[3,3]);   // three messages at most, three records (one row each)
const table=[];
for(let h=60;h>=14;h--){const v=draw({status:doc(),height:h,question:true});
  const row={h,msg:msgRows(v),mem:memRows(v),route:routeBox(v),q:count(v,/QUESTIONS|시험용 질문\?/)};table.push(row);
  // Anything but MESSAGES and MEM giving a row (a shorter route box, the question's rows) happens only with both at their floor.
  // (11-26, panel B: a message is one row now; the rows B's forms leave stand empty, they never unfold a message, so the floor holds as it did.)
  const another=row.route<routeFull&&h<40;
  if(another)assert(row.msg<=1&&row.mem<=1,JSON.stringify(row)+'\n'+lines(v).join('\n'));
  assert(row.q>=2,JSON.stringify(row));
}
// MESSAGES and MEM stand at one or two rows before that: some height has them at two rows, and the shortest at one.
assert(table.some(r=>r.msg===2||r.mem===2)&&table.some(r=>r.msg===1&&r.mem===1),JSON.stringify(table));
// Their one-row forms: a message is its arrow, number and text; a record its mark, age, kind and headline (`…` where it goes on); `+N` counts the rest.
const small=inside(draw({status:doc(),height:26,width:40}));
assert(small.some(l=>/^ [←→] \[7\d\] 시험용 메시지/.test(l))&&small.some(l=>/^ ◆ \dm (decision )?시험용 기억/.test(l)),small.join('\n'));
assert(small.some(l=>/^MESSAGES \+\d+ +›$/.test(l)),small.join('\n'));
""")

    def test_the_mem_popup_reads_each_record_whole_from_what_the_panel_holds(self):
        self.node(r"""
const items=[{kind:'decision',age:'10m',text:'제목 하나 '.repeat(8).trim(),tier:'durable',scope:'project',body:'본문 '.repeat(30).trim()},
 {kind:'lesson',age:'2h',text:'둘',tier:'working',scope:'global'},{kind:'fact',age:'1d',text:'머리글만으로 시작하는 기록이며 본문이 이어진다',tier:'',scope:'',body:'머리글만으로 시작하는 기록이며 본문이 이어진다 그리고 여기까지 읽힌다'},{text:'  '}];
const rows=view.memoryBody({model:{styles,memory_items:items},columns:40,cellWidth:cells}),ls=rows.map(str);
// Each record: its head (`◆ project · decision · 10m`: mark, scope, kind, age, dim), its headline whole, its body whole, a blank row between; no row wider than the popup.
assert.equal(ls[0],'◆ project · decision · 10m');assert(ls.slice(1,4).join(' ').replace(/\s+/g,' ').includes('제목 하나 제목 하나 제목 하나 제목 하나 제목 하나 제목 하나 제목 하나 제목 하나'),ls.join('\n'));
assert(ls.filter(l=>l.includes('본문')).length>=4&&ls.every(l=>cells(l)<=40),ls.join('\n'));
assert.deepEqual(rows.starts.length,3);assert.equal(ls[rows.starts[1]],'◇ global · lesson · 2h');assert.equal(ls[rows.starts[1]-1],'');
assert.equal(ls[rows.starts[2]],'· fact · 1d');
// A body that goes on from the headline is read once, whole (no headline row over it); the head is dim, the headline in the body color, the body dim.
assert.equal(ls.filter(l=>l.includes('머리글만으로')).length,1,ls.join('\n'));
assert(rows[0].every(p=>p.dim===true)&&rows[1].every(p=>p.dim!==true||!p.text.trim())&&rows[rows.starts[1]-1].length===0);
// Nothing the panel does not hold: a record with no text is none; no records, one dim dot; control characters become spaces.
assert.deepEqual(view.memoryBody({model:{styles,memory_items:[]},columns:40,cellWidth:cells}).map(str),['·']);
assert(!view.memoryBody({model:{styles,memory_items:[{text:'a\u001b[31mb',body:'c\u0007d',tier:'durable'}]},columns:40,cellWidth:cells}).map(str).join('').match(/[\u0000-\u001f]/));
""")

    def test_a_bottom_panel_keeps_its_order_with_a_document_or_without_one(self):
        self.node(r"""
// 100 x 12, three routes, five messages: the `messages` head and one message, the route box with its rows hidden counted, whatever the status value is.
for(const status of [undefined,{word:'STATUS',has_doc:false},doc([]),doc(GROUPS)]){
  const ws=lines(draw({status,side:false,width:100,height:12,routes:3,messages:5}));
  assert(ws.some(l=>/^│ messages \+4 ─+ › │$/.test(l))&&ws.some(l=>/^│ ← \[75\] /.test(l)),JSON.stringify(status)+'\n'+ws.join('\n'));
  assert(ws.some(l=>/^│ ╭─ route 3 /.test(l))&&ws.some(l=>/↓2 ─╯ │$/.test(l)),ws.join('\n'));}
// Short of rows the route box (the job that runs now) stays and the button stays on the first row: 100 x 8..14, one route.
for(let h=8;h<=14;h++){
  const v=draw({status:doc(GROUPS),side:false,width:100,height:h,routes:1}),ws=lines(v);
  assert(ws.length===h&&ws.every(l=>cells(l)===100),h+'\n'+ws.join('\n'));
  assert(ws.some(l=>/^│ ╭─ route /.test(l))&&/STATUS › │$/.test(ws[1])&&v.hits.some(x=>x.row===1&&x.action==='popup:STATUS'),h+'\n'+ws.join('\n'));}
""")


if __name__ == "__main__":
    unittest.main()
