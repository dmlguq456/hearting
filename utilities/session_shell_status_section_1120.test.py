"""11-20 (panel STATUS section, REPO and SESSION groups, the MEM popup): the panel draws a `현황` section from the campaign document's lists, in two groups,
and MESSAGES and MEM go down first.

The person (11-20): "패널 공간에 현황은 중요하지. 기본적으로 5줄 이상 써서 보이는것도 중요할것 같다. 대신 MESSAGE도 최대 3개 정도로 다시 줄이고, 그리고 MESSAGE, MEM은 좀 더
세로길이 따라서 과감하게 표시 줄을 줄여버려 1~2줄 수준으로" and, right after: "REPO에 엮어서 구분을 하나 더 두는게 맞을듯" (`REPO ────` / `SESSION ────`).

1. Beside the native area the panel has two groups. REPO: its row (the group's head, `REPO   hearting · wt 12 ────`, no row more; the `separate` form puts
   a head row of its own over it) and, right under it, the STATUS section when the backend's value carries the document's lists (`status_doc.groups`); its
   head `현황 ──── ›` takes over the REPO row's button, so the button is drawn once. GIT and PEERS stand where they stood (under the section). SESSION: its head
   `SESSION ────` stands where the empty row between the two groups' metadata stood (it goes with that row), then MODEL, CTX, TITLE, NOW, QUESTIONS, the route
   box, MESSAGES, MEM. No lists (no document, a document with none of them, an older backend): no section, the button stays on the REPO row.
2. The groups of the section read in the order of the list the person liked: in progress, waiting for the person, ended lately, later; each its head row (name
   and count) and its titles under it, the tail of a title (` · #PR · 기록 N`) given up first. Five body rows at least while rows allow; short of rows the later
   groups stand as counts on one row and the group that waits for the person is the last to give a title up.
3. Short of rows MESSAGES (three at most) and MEM (one row a record) are the first to go down, before the STATUS section, the route box or the question give up a
   row. The MEM popup (`memoryBody`) reads each record whole in the MESSAGES popup's own frame.
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
const doc=groups=>({word:'현황',has_doc:true,...(groups?{groups}:{})});
function model(o={}){
  return {styles,colors:256,tag:'[e9]',repo_side:[t('hearting','loc_repo')],worktrees:[t('wt 12')],repo:[t('hearting','loc_repo')],git_side:[t('branch'),t(' ✓','lvl_g')],git:[t('branch')],
    selected:[t('Opus 5.5','hb_claude'),t(' xhigh')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],context:[t('ctx 41%','lvl_g')],quota:[t('5h ','dim'),t('23%','lvl_g')],
    session_title:'시험용 세션',now:'시험용 NOW',route_label:'route 2 ⠏2',route_count:2,route_total:2,routes:[route(1),route(2)],
    messages:MSGS.slice(-(o.messages??5)),memory_items:MEMS.slice(0,o.mem??3),status_doc:o.status,group_head:o.group_head,
    ...(o.peers?{peers:{groups:[{steward:{tag:'e9',key:'hb_claude',me:true},members:[{tag:'d4',key:'hb_codex'}]}]}}:{})};
}
const brief=()=>({count:1,answerable:true,choices:2,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')],[t('1 ','dim'),t('가','loc_repo'),t(' · ','dim'),t('2 ','dim'),t('나','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')]],line:'시험용 질문'});
const draw=(o={})=>view.render({width:o.width??44,height:o.height??60,side:o.side??true,model:model(o),state:'working',harness:'claude',frame:1000,since:990,
  brief:o.question?brief:undefined,sent:()=>[],cellWidth:cells,configuration:{side_width:o.width??44,rule_motion:'off',theme:'terminal',position:'right'}});
const lines=v=>v.rows.map(str);
const inside=v=>lines(v).map(l=>l.slice(2,-2));
const headAt=(rows,re)=>rows.findIndex(l=>re.test(l));
// The section's body: the rows under its head, up to the next group's or section's first row.
function body(v){const ls=inside(v),a=headAt(ls,/^현황 ─+ ›/);if(a<0)return null;let b=a+1;
  while(b<ls.length-2&&/^(  \S|하는 중|기다리는 것|기다림|최근에 끝난 것|나중에 할 것|끝난 일|예정)/.test(ls[b]))b++;return {at:a,rows:ls.slice(a+1,b)};}
const count=(v,re)=>inside(v).filter(l=>re.test(l)).length;
"""


@unittest.skipUnless(NODE, "needs node")
class StatusSectionTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_no_lists_no_section_and_the_button_stays_on_the_repo_row(self):
        self.node(r"""
for(const status of [undefined,doc(),doc([]),{word:'현황',has_doc:false},{word:'현황',has_doc:true,groups:[{kind:'nonsense',count:2,items:['x']}]}]){
  const v=draw({status}),ls=lines(v);
  assert(!ls.some(l=>/현황 ─/.test(l)),JSON.stringify(status));
  // (11-20: the REPO row is the group's head: `REPO   hearting · wt 12 ──── 현황 ›`.)
  if(status&&status.word){assert(/^│ REPO   hearting · wt 12 ─+ 현황 › │$/.test(ls[1]),ls[1]);assert(v.hits.some(h=>h.row===1&&h.action==='popup:STATUS'));}
  else assert(/^│ REPO   hearting · wt 12 ─+ │$/.test(ls[1]),ls[1]);
}
""")

    def test_the_section_stands_under_the_repo_row_the_button_moves_into_its_head_and_a_press_opens_the_popup(self):
        self.node(r"""
const v=draw({status:doc(GROUPS),question:true,peers:true,height:70}),ls=inside(v),all=lines(v);
// The REPO group: its row is the head (the rule runs to the end, no button), the STATUS section right under it, then GIT and PEERS where they stood.
assert(/^│ REPO   hearting · wt 12 ─+ │$/.test(all[1])&&!v.hits.some(h=>h.row===1),all[1]);
const s=headAt(ls,/^현황 ─+ ›$/),g=headAt(ls,/^GIT /),pe=headAt(ls,/^PEERS /),se=headAt(ls,/^SESSION ─+$/),mo=headAt(ls,/^MODEL /);
assert(s===2&&g>s&&pe>g&&se>pe&&mo===se+1,ls.join('\n'));   // (index 0 is the frame's top line, 1 the REPO row)
// The SESSION group: MODEL, CTX, TITLE, NOW, then QUESTIONS, the route box, MESSAGES, MEM.
const order=[/^MODEL /,/^CTX /,/^TITLE /,/^NOW /,/^╭─ 1 QUESTIONS/,/^╭─ 2 ROUTE/,/^MESSAGES/,/^MEM ─/].map(re=>headAt(ls,re));
assert(order.every((y,k)=>y>se&&(!k||y>order[k-1])),order+'\n'+ls.join('\n'));
// The head and every row of the section open the popup (and only they do); the REPO row, the SESSION head and PEERS open nothing.
const b=body(v);
for(let y=b.at;y<=b.at+b.rows.length;y++)assert(v.hits.some(h=>h.row===y&&h.action==='popup:STATUS'),'row '+y);
assert.equal(v.hits.filter(h=>h.action==='popup:STATUS').length,b.rows.length+1);
assert(!v.hits.some(h=>[1,g,pe,se].includes(h.row)&&h.action!=='fold'),JSON.stringify(v.hits));
// The group heads' look: the word in the title role, the rule dim in the messages role (a section head's).
const seRow=v.rows[se],word=seRow.find(p=>p.text==='SESSION');assert(word&&word.role==='title'&&word.dim===true&&seRow.some(p=>p.role==='messages'),JSON.stringify(seRow));
const repoRule=v.rows[1].find(p=>/^─+$/.test(p.text));assert(repoRule&&repoRule.role==='messages'&&repoRule.dim===true,JSON.stringify(v.rows[1]));
// The bottom layout has no group heads (its first row carries the repository and the session together): the section stays second among the sections.
{const w=draw({status:doc(GROUPS),side:false,width:100,height:14}),ws=lines(w);
 assert(ws.some(l=>/^│ 현황 ─+ › │$/.test(l)),ws.join('\n'));assert(!/현황|REPO|SESSION/.test(ws[1])&&!ws.some(l=>/^│ (REPO|SESSION)/.test(l)),ws.join('\n'));
 assert(body(w)&&body(w).rows.length>=5,ws.join('\n'));}
""")

    def test_the_separate_head_form_a_narrow_panel_and_the_heads_that_fold(self):
        self.node(r"""
// `separate`: a head row of its own over the REPO row, which keeps its value alone (under the label column).
{const ls=lines(draw({status:doc(GROUPS),group_head:'separate',height:70}));
 assert(/^│ REPO ─+ │$/.test(ls[1])&&/^│ {8}hearting · wt 12 +│$/.test(ls[2])&&/^│ 현황 ─+ › │$/.test(ls[3]),ls.join('\n'));}
// Narrower than 30 cells inside the REPO row keeps the word first (`REPO `), as the head of its group.
for(const width of [32,28,20]){const v=draw({status:doc(GROUPS),width,height:70}),ls=lines(v);
  assert(/^│ REPO hearting/.test(ls[1])&&ls.every(l=>cells(l)===width),width+'\n'+ls.join('\n'));}
// The merged head costs no row: the rows under it are the same with and without the section's group word, and the SESSION head takes the empty row's place.
// Short of rows the SESSION head goes with that empty row; the REPO head (the REPO row) never goes.
const seen=[];
for(let h=70;h>=12;h--){const ls=lines(draw({status:doc(GROUPS),height:h,peers:true}));
  seen.push([h,ls.some(l=>/^│ SESSION ─+ │$/.test(l)),/^│ REPO /.test(ls[1])]);}
assert(seen.every(r=>r[2]),JSON.stringify(seen));
assert(seen[0][1]===true&&seen.some(r=>r[1]===false),JSON.stringify(seen));
// The SESSION head is gone for every height shorter than the first one that lacks it, within the give-back rule (a height may take it back).
const firstGone=seen.find(r=>!r[1])[0];assert(firstGone>=24&&firstGone<=34,String(firstGone));
// A head row of its own is the first row to go (before any content row, even the empty ones): wherever the panel is short of rows the `separate` form is the
// merged form again, row for row, the REPO row keeping its word; with rows to spare it stands.
for(const h of [44,40,34,30,26,24,20]){const m=lines(draw({status:doc(GROUPS),height:h})),p=lines(draw({status:doc(GROUPS),height:h,group_head:'separate'}));
  assert.deepEqual(p,m,h+'\n'+p.join('\n'));}
assert(/^│ REPO ─+ │$/.test(lines(draw({status:doc(GROUPS),group_head:'separate',height:90}))[1]));
""")

    def test_groups_read_in_the_liked_order_each_with_a_head_row_and_its_titles(self):
        self.node(r"""
const v=draw({status:doc(GROUPS),height:70}),b=body(v);
const want=['하는 중 3','⠋ 시험용 작업 가','⠋ 시험용 작업 나','기다리는 것 2','◇ 시험용 질문 둘','◇ 시험용 정하기','최근에 끝난 것 4','✓ 시험용 끝남 하나','✓ 시험용 끝남 둘','나중에 할 것 3','· 시험용 예정 하나','· 시험용 예정 둘'];
assert.deepEqual(b.rows.map(l=>l.trim()),want,b.rows.join('\n'));
assert.equal(b.rows.length,12);   // STATUS_MAX: four heads and eight titles, two to a group
// Titles stand two cells in; the waiting count and glyph are the caution color, the others' count dim; the glyphs are the sián's.
const rowOf=text=>v.rows.find(r=>str(r).includes(text));
assert.equal(rowOf('기다리는 것 2').find(p=>p.text==='2').foreground,186);assert.equal(rowOf('하는 중 3').find(p=>p.text==='3').dim,true);
assert.equal(rowOf('◇ 시험용 질문').find(p=>p.text==='◇ ').foreground,186);assert.equal(rowOf('⠋ 시험용 작업 가').find(p=>p.text==='⠋ ').foreground,150);
assert(b.rows.filter(l=>/^  \S/.test(l)).length===8);
// A document with fewer titles stands as short as it is; a count past the titles read is still the count.
const few=body(draw({status:doc([G('running','하는 중','하는 중',['하나'],9),G('waiting','기다리는 것','기다림',[])]),height:70}));
assert.deepEqual(few.rows.map(l=>l.trim()),['하는 중 9','⠋ 하나','기다리는 것 0'],few.rows.join('\n'));
// A title as the document wrote it, `title · #PR · vX · 기록 N`: short of cells the tail goes first, piece by piece from its end (the tail dim), no `…`
// until the title itself is cut.
const tail=G('running','하는 중','하는 중',['시험용 작업 · #1801 · v4.60.1 · 기록 3']);
for(const [width,want] of [[60,'⠋ 시험용 작업 · #1801 · v4.60.1 · 기록 3'],[44,'⠋ 시험용 작업 · #1801 · v4.60.1'],[36,'⠋ 시험용 작업 · #1801'],[24,'⠋ 시험용 작업']]){
  const w=draw({status:doc([tail]),width,height:70}),r=body(w).rows[1].trim();
  assert.equal(r,want,width+'\n'+body(w).rows.join('\n'));assert(!r.includes('…'),r);}
{const w=draw({status:doc([G('running','하는 중','하는 중',['아주긴제목'.repeat(8)+' · #1'])]),width:28,height:70}),r=body(w).rows[1];
 assert(r.includes('…')&&!r.includes('#1'),r);
 const t=w.rows.find(row=>str(row).includes('#1')||false);}
{const w=draw({status:doc([tail]),width:44,height:70}),row=w.rows.find(r=>str(r).includes('#1801'));
 assert(row.find(p=>p.text.includes('#1801')).dim===true&&row.find(p=>p.text==='시험용 작업').dim!==true,JSON.stringify(row));}
""")

    def test_five_rows_at_least_then_the_later_groups_become_counts_and_the_waiting_ones_go_last(self):
        self.node(r"""
const seen=[];
for(let h=70;h>=8;h--){const b=body(draw({status:doc(GROUPS),height:h}));seen.push([h,b?b.rows.length:0,b?b.rows.filter(l=>/^  /.test(l)).length:0,b?b.rows.map(l=>l.trim()):[]]);}
// From 70 down to 24 rows (the shortest the person named) the section keeps five body rows at least.
for(const [h,n,,rows] of seen)if(h>=24)assert(n>=5,h+' '+n+'\n'+rows.join('\n'));
assert.equal(seen.find(s=>s[0]===70)[1],12);
// At 24 rows it is the five rows: in progress (head, one title), waiting (head, one title), the rest as one row of counts.
assert.deepEqual(seen.find(s=>s[0]===24)[3],['하는 중 3','⠋ 시험용 작업 가','기다리는 것 2','◇ 시험용 질문 둘','최근에 끝난 것 4 · 나중에 할 것 3']);
// The waiting group's title is the last to go: wherever any title stands, the waiting one does; counts stand on a row of their own.
for(const [h,,,rows] of seen){const titled=rows.some(l=>/^[⠋✓·] /.test(l));if(titled)assert(rows.some(l=>/^◇ /.test(l)),h+'\n'+rows.join('\n'));}
// Fewer rows still: one row of counts, then no section.
const last=seen.filter(s=>s[1]>0).at(-1);assert(last&&last[1]===1&&/하는 중 3/.test(last[3][0]),JSON.stringify(last));
""")

    def test_messages_and_mem_go_down_first_before_status_route_or_question_give_a_row(self):
        self.node(r"""
const full=draw({status:doc(GROUPS),height:120});
const routeBox=v=>{const ls=inside(v),a=headAt(ls,/^╭─ \d+ ROUTE/);return a<0?0:ls.findIndex((l,i)=>i>a&&/^╰/.test(l))-a;};
const msgRows=v=>count(v,/^[←→] /),memRows=v=>count(v,/^[◆◇·] /),statusRows=v=>{const b=body(v);return b?b.rows.length:0;};
const routeFull=routeBox(full),statusFull=statusRows(full);
assert.deepEqual([msgRows(full),memRows(full)],[3,3]);   // three messages at most, three records (one row each)
const table=[];
for(let h=60;h>=14;h--){const v=draw({status:doc(GROUPS),height:h,question:true});
  const row={h,msg:msgRows(v),mem:memRows(v),status:statusRows(v),route:routeBox(v),q:count(v,/QUESTIONS|시험용 질문\?/)};table.push(row);
  // Anything but MESSAGES and MEM giving a row (the status below five, a shorter route box, the question's rows) happens only with both at their floor.
  const another=row.status<Math.min(5,statusFull)||row.route<routeFull&&h<40;
  if(another)assert(row.msg<=1&&row.mem<=1,JSON.stringify(row)+'\n'+lines(v).join('\n'));
  assert(row.q>=2,JSON.stringify(row));
}
// MESSAGES and MEM stand at one or two rows before that: some height has them at two rows, and the shortest at one.
assert(table.some(r=>r.msg===2||r.mem===2)&&table.some(r=>r.msg===1&&r.mem===1),JSON.stringify(table));
// Their one-row forms: a message is its arrow, number and text; a record its mark, age, kind and headline (`…` where it goes on); `+N` counts the rest.
const small=inside(draw({status:doc(GROUPS),height:26,width:40}));
assert(small.some(l=>/^[←→] \[7\d\] 시험용 메시지/.test(l))&&small.some(l=>/^◆ \dm (decision )?시험용 기억/.test(l)),small.join('\n'));
assert(small.some(l=>/^MESSAGES \+\d+ ─+ ›$/.test(l)),small.join('\n'));
""")

    def test_the_wait_count_of_zero_and_a_narrow_panel_stay_in_their_row(self):
        self.node(r"""
const none=[G('running','하는 중','하는 중',['시험용 하나','시험용 둘']),G('waiting','기다리는 것','기다림',[]),G('done','최근에 끝난 것','끝난 일',['끝 하나','끝 둘']),G('next','나중에 할 것','예정',['예정 하나'])];
// Nothing waits: its count (0) is a row of counts, beside the groups that are counts too (the short names where the full ones do not fit).
const b=body(draw({status:doc(none),height:24,width:60})).rows.map(l=>l.trim());
assert(b.includes('기다리는 것 0 · 최근에 끝난 것 2 · 나중에 할 것 1'),b.join('\n'));
const c=body(draw({status:doc(none),height:24})).rows.map(l=>l.trim());
assert(c.includes('기다림 0 · 끝난 일 2 · 예정 1'),c.join('\n'));
// Narrow: the groups' short names, cut with `…` at the end where even those do not fit; every row exactly the panel's width.
for(const width of [36,28,20]){const v=draw({status:doc(GROUPS),width,height:24});
  assert(lines(v).every(l=>cells(l)===width),width+'\n'+lines(v).join('\n'));
  const rows=body(v).rows.map(l=>l.trim());
  if(width===28)assert(rows.includes('끝난 일 4 · 예정 3'),rows.join('\n'));}
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


if __name__ == "__main__":
    unittest.main()
