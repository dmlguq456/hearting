"""11-21 (the person: "버튼을 REPO의 아래에 각각을 두고 +? 숫자 띄우고 백로그랑 현황을 나열하자" / "각각 3줄 정도씩 목록으로 만들고 추가분을 +? 형식으로"):
the panel's STATUS part and BACKLOG part under the REPO group, and the BACKLOG popup.

1. Beside the native area the REPO group ends in two parts, each a section head (`STATUS +2 ──── ›`, a press opens that popup) and its first three items (one row each,
   `!` before what waits, `·` before the rest; a press on one opens the popup standing on it). `+N` on the head counts the items not shown, as MESSAGES' does (none for
   zero). No item: the head alone. No document: the word dim, nothing under it, still pressable. The `STATUS ›` button on the REPO row is gone.
2. Short of rows the BACKLOG items go first (3, 2, 1, none), then the order MESSAGES and MEM already had, then the STATUS items; NOW, QUESTIONS and the route box
   never give up a row for the new parts (they go on MESSAGES, MEM, the SESSION head, then Git, PEERS and the two heads together: a head never stands without Git).
3. The bottom layout has no list: its first row ends in `STATUS +N ›  BACKLOG +N ›` and the panel is no taller.
4. The two parts' items come with the status value (one read, the document read again only where it changed: `have`), and the panel's drawing reads no file.
5. The BACKLOG popup is the status popup's list screen (`status-list.cjs`) over `<artifact root>/backlog.md`; its groups are the document's `##` headings as written.
6. (From the review of #1793) a list dropped for its size leaves no stale style index; the document's rows are made only where the list is dropped.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
NODE = shutil.which("node")
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},lvl_r:{foreground:217,bold:true},
  g_blocked:{foreground:217},g_spin:{foreground:230},hb_claude:{foreground:116}};
const t=(text,key='dim')=>({text,...(styles[key]||{dim:key==='dim'})});
const MSGS=Array.from({length:7},(_,i)=>i+1).map(n=>({way:n%2?'←':'→',who:'[7'+n+']',harness:'claude',when:'09:0'+n,kind:'report',text:'시험용 메시지 '+n,body:'시험용 메시지 '+n,delivery:null}));
const MEMS=Array.from({length:5},(_,i)=>i+1).map(n=>({kind:'decision',age:n+'m',text:'시험용 기억 '+n+' 제목',tier:'durable',scope:'project',body:''}));
const route=i=>({id:'rt-'+i,title:'시험용 작업 '+i,kind:'working',state:'working',shape:'one-shot-owner',kind_word:'code',elapsed:(10+i)+'m',tool:'Edit',role:'owner',
  paint:{glyph:t('⠏','g_spin'),harness:t('claude','hb_claude'),model_text:t('Opus 5.5','hb_claude'),effort:t('xhigh','dim'),ctx:t('52%','lvl_g'),gauge5:[t('━━━','lvl_g'),t('──'),t(' '),t('52%','lvl_g')]}});
// The two parts' values as the host holds them: `{word, has_doc, items: [{mark, text, key}], total}`.
const SI=[{mark:'!',text:'시험용 정할 것 하나',key:'i:a1'},{mark:'',text:'시험용 하는 일 하나 · #101',key:'i:a2'},{mark:'',text:'시험용 하는 일 둘 — 제목이 길면 이렇게 잘린다 이렇게 길게 이어지는 시험용 제목입니다',key:'i:a3'}];
const BI=[{mark:'',text:'시험용 나중 일 하나',key:'i:b1'},{mark:'',text:'시험용 나중 일 둘',key:'i:b2'},{mark:'',text:'시험용 나중 일 셋',key:'i:b3'}];
const part=(word,list,n,total,has=true)=>({word,has_doc:has,items:list.slice(0,n),total});
const status=(n=3,total=5,has=true)=>part('STATUS',SI,n,total,has),backlog=(n=3,total=14,has=true)=>part('BACKLOG',BI,n,total,has);
function model(o={}){
  const routes=Array.from({length:o.routes??0},(_,i)=>route(i+1));
  return {styles,colors:256,tag:'[e9]',repo_side:[t('hearting','loc_repo')],worktrees:[t('wt 12')],repo:[t('hearting','loc_repo')],git_side:[t('branch'),t(' ✓','lvl_g')],git:[t('branch')],
    selected:[t('Opus 5.5','hb_claude'),t(' xhigh')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],context:[t('ctx 41%','lvl_g')],quota:[t('5h ','dim'),t('23%','lvl_g')],
    session_title:'시험용 세션',now:'시험용 NOW 한 줄',route_label:routes.length?'route '+routes.length:undefined,route_count:routes.length,route_total:routes.length,routes,
    messages:MSGS.slice(-(o.messages??0)),memory_items:MEMS.slice(0,o.mem??0),
    ...(o.status!==undefined?{status_doc:o.status}:{}),...(o.backlog!==undefined?{backlog_doc:o.backlog}:{}),
    ...(o.peers===false?{}:{peers:{groups:[{steward:{tag:'e9',key:'hb_claude',me:true},members:[{tag:'d4',key:'hb_codex'}]}]}})};
}
const brief=()=>({count:1,answerable:true,choices:2,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')],[t('1 ','dim'),t('가','loc_repo'),t(' · ','dim'),t('2 ','dim'),t('나','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')]],line:'시험용 질문'});
const draw=(o={})=>view.render({width:o.width??59,height:o.height??40,side:o.side??true,model:model(o),state:'working',harness:'claude',frame:1000,since:990,
  brief:o.question?brief:undefined,sent:()=>[],cellWidth:cells,configuration:{side_width:o.width??59,rule_motion:'off',theme:'terminal',position:'right'}});
const lines=v=>v.rows.map(str);
const inside=v=>lines(v).map(l=>l.slice(2,-2).trimEnd());
const count=(v,re)=>inside(v).filter(l=>re.test(l)).length;
"""


@unittest.skipUnless(NODE, "needs node")
class PanelListsTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_picture_under_the_repo_group_and_the_count_of_what_is_not_shown(self):
        self.node(r"""
const v=draw({status:status(),backlog:backlog(),routes:1,messages:3,mem:2,height:40}),ls=lines(v);
const want=[
'│ REPO   hearting · wt 12 ─────────────────────────────── │',
'│ GIT    branch ✓                                         │',
'│ PEERS  [e9] ⚑  ─ [d4]                                   │',
'│ STATUS +2 ─────────────────────────────────────────── › │',
'│ ! 시험용 정할 것 하나                                   │',
'│ · 시험용 하는 일 하나 · #101                            │',
'│ · 시험용 하는 일 둘 — 제목이 길면 이렇게 잘린다 이렇게… │',
'│ BACKLOG +11 ───────────────────────────────────────── › │',
'│ · 시험용 나중 일 하나                                   │',
'│ · 시험용 나중 일 둘                                     │',
'│ · 시험용 나중 일 셋                                     │',
'│ SESSION ─────────────────────────────────────────────── │'];
assert.deepEqual(ls.slice(1,13),want,ls.join('\n'));
assert(ls.every(l=>cells(l)===59));
// `+N` is the head's own text in the MESSAGES head's colour (`loc_repo`); the mark of what waits is the status list's own yellow.
const row=v.rows[4],plus=row.find(p=>p.text==='+2');assert(plus&&plus.foreground===253,JSON.stringify(row));
{const w=v.rows[5].find(p=>p.text==='! '),d=v.rows[6].find(p=>p.text==='· ');assert(w&&w.foreground===186&&d&&d.dim===true,JSON.stringify([v.rows[5],v.rows[6]]));}
// No `+N` where every item shows (the total is the three), and none for a total that was not told.
{const a=inside(draw({status:status(3,3),backlog:backlog(2,2),height:40}));
 assert(a.some(l=>/^STATUS ─+ ›$/.test(l))&&a.some(l=>/^BACKLOG ─+ ›$/.test(l)),a.join('\n'));}
{const a=inside(draw({status:{word:'STATUS',has_doc:true,items:SI},backlog:{word:'BACKLOG',has_doc:true},height:40}));
 assert(a.some(l=>/^STATUS ─+ ›$/.test(l))&&a.some(l=>/^BACKLOG ─+ ›$/.test(l))&&a.filter(l=>/^[!·] /.test(l)).length===3,a.join('\n'));}
// No item: the head alone (no empty row). A total of nothing and a total with no items shown (the host holds the count only) say `+N`.
{const a=inside(draw({status:status(0,0),backlog:backlog(0,0),height:40}));
 const at=a.findIndex(l=>/^STATUS/.test(l));assert(/^BACKLOG/.test(a[at+1])&&/^SESSION/.test(a[at+2]),a.join('\n'));}
{const a=inside(draw({status:status(0,7),backlog:backlog(0,9),height:40}));assert(a.some(l=>/^STATUS \+7 ─+ ›$/.test(l))&&a.some(l=>/^BACKLOG \+9 ─+ ›$/.test(l)),a.join('\n'));}
// The REPO row has no button now, and the heads stand between PEERS and SESSION.
{const a=inside(draw({status:status(),backlog:backlog(),height:40}));assert(!a[1].includes('›')&&/^REPO   hearting · wt 12 ─+$/.test(a[1]),a[1]);
 const at=n=>a.findIndex(l=>l.startsWith(n));assert(at('PEERS')<at('STATUS')&&at('STATUS')<at('BACKLOG')&&at('BACKLOG')<at('SESSION'));}
""")

    def test_a_missing_document_is_a_dim_word_and_no_root_has_no_part(self):
        self.node(r"""
// No document: the word plain dim (not the title role), `›` dim, no item, and a press still opens the popup.
{const v=draw({status:status(0,0,false),backlog:backlog(0,0,false),height:40}),a=inside(v);
 for(const [word,action] of [['STATUS','popup:STATUS'],['BACKLOG','popup:BACKLOG']]){
  const at=a.findIndex(l=>l.startsWith(word));assert(at>0);
  const w=v.rows[at].find(p=>p.text===word),arrow=v.rows[at].findLast(p=>p.text==='›');
  assert(w.dim===true&&w.role===undefined&&arrow.dim===true,JSON.stringify(v.rows[at]));
  assert(v.hits.some(h=>h.row===at&&h.action===action));}
 assert(!a.some(l=>/^[!·] /.test(l)));}
// With a document the word has the title role (as the other sections' heads).
{const v=draw({status:status(),backlog:backlog(),height:40});const at=inside(v).findIndex(l=>l.startsWith('STATUS'));assert.equal(v.rows[at].find(p=>p.text==='STATUS').role,'title');}
// Only one of the two parts has a value: only that one stands. No value at all (no artifact root): nothing, no press.
{const a=inside(draw({status:status(),height:40}));assert(a.some(l=>l.startsWith('STATUS'))&&!a.some(l=>l.startsWith('BACKLOG')));}
for(const status_doc of [undefined,null,{word:'',has_doc:true}]){const v=draw({status:status_doc,backlog:status_doc,height:40}),a=inside(v);
 assert(!a.some(l=>/^(STATUS|BACKLOG)/.test(l)),JSON.stringify(status_doc));assert(!v.hits.some(h=>/STATUS|BACKLOG/.test(h.action)));}
""")

    def test_presses_on_a_head_open_its_popup_and_on_an_item_open_it_standing_on_that_item(self):
        self.node(r"""
const v=draw({status:status(),backlog:backlog(),height:40}),a=inside(v);
const at=re=>a.findIndex(l=>re.test(l));
const sh=at(/^STATUS/),bh=at(/^BACKLOG/);
const acts=v.hits.filter(h=>/popup:(STATUS|BACKLOG)/.test(h.action)).map(h=>[h.row,h.action]);
assert.deepEqual(acts,[[sh,'popup:STATUS'],[sh+1,'popup:STATUS:i:a1'],[sh+2,'popup:STATUS:i:a2'],[sh+3,'popup:STATUS:i:a3'],
  [bh,'popup:BACKLOG'],[bh+1,'popup:BACKLOG:i:b1'],[bh+2,'popup:BACKLOG:i:b2'],[bh+3,'popup:BACKLOG:i:b3']],JSON.stringify(acts));
// A press stands for the whole row; no press on the REPO, GIT, PEERS or SESSION rows.
assert(v.hits.filter(h=>/popup:(STATUS|BACKLOG)/.test(h.action)).every(h=>h.left===2&&h.right===56));
assert(!v.hits.some(h=>h.row<sh&&/STATUS|BACKLOG/.test(h.action)));
// An item with no key opens the popup at its top: the head's own press.
{const w=draw({status:{...status(),items:[{mark:'',text:'시험용 키 없음'}]},backlog:backlog(0,0),height:40});
 assert(w.hits.some(h=>h.action==='popup:STATUS'&&inside(w)[h.row]==='· 시험용 키 없음'),JSON.stringify(w.hits));}
""")

    def test_narrow_panels_cut_the_head_and_the_items(self):
        self.node(r"""
for(const width of [59,44,40,36,32,30]){
  const v=draw({width,status:status(),backlog:backlog(),height:50}),ls=lines(v),a=inside(v);
  assert(ls.every(l=>cells(l)===width),width+'\n'+ls.join('\n'));
  const sh=a.findIndex(l=>/^STATUS/.test(l)),bh=a.findIndex(l=>/^BACKLOG/.test(l));
  assert(sh>0&&bh===sh+4,width+'\n'+ls.join('\n'));
  assert(/^STATUS( \+2)? ─+ ›$/.test(a[sh])&&/^BACKLOG( \+11)? ─+ ›$/.test(a[bh]),a[sh]+'|'+a[bh]);
  assert.equal(a[sh+1].slice(0,2),'! ');assert(a[sh+3].endsWith('…'),a[sh+3]);   // the long title is cut at the width
}
// The least a head is: `STATUS +2 ─ ›` (the rule and the count give way to nothing but a space and `›` before the word does).
{const v=draw({width:24,status:status(),backlog:backlog(),height:50}),a=inside(v);assert(a.some(l=>/^STATUS( \+2)? [─ ]*›$/.test(l)),a.join('\n'));}
// Hangul is two cells: a row never passes the panel.
{const v=draw({width:30,status:{...status(),items:[{mark:'!',text:'한글로만 이루어진 아주 긴 제목이라서 잘려야 합니다 한글로만',key:'i:x'}]},backlog:backlog(0,0),height:50});
 assert(lines(v).every(l=>cells(l)===30));}
""")

    # The one rule (11-22): the lists are the first to give way and the rest of the panel is what it is without them. The helper below draws a panel
    # (messages 0 is none) and sorts its rows by what each is, from the rows' own presses: `list` (a head or an item of STATUS or BACKLOG), `blank`, `mm-head`, `mm-item`
    # (MESSAGES' and MEM's rows under their head), the REPO row, and any other row.
    SORT = r"""
const drawN=o=>view.render({width:o.width,height:o.height,side:true,model:{...model(o),messages:MSGS.slice(7-(o.messages??0))},state:'working',harness:'claude',frame:1000,since:990,
  brief:o.question?brief:undefined,sent:()=>[],cellWidth:cells,configuration:{side_width:o.width,rule_motion:'off',theme:'terminal',position:'right'}});
function sort(v){
  const ins=lines(v).map(l=>l.slice(2,-2)),act=new Map();
  for(const h of v.hits){if(!act.has(h.row))act.set(h.row,[]);act.get(h.row).push(h.action);}
  const out=[];let prev='';
  for(let i=1;i<ins.length-1;i++){
    const first=(act.get(i)||[])[0]||'';let k='row';
    if(i===1)k='repo';
    else if(/^popup:(STATUS|BACKLOG)/.test(first))k='list';
    else if(/^popup:(MESSAGES|MEM)/.test(first)){const n=first.startsWith('popup:MESSAGES')?'MESSAGES':'MEM';k=prev===n?'mm-item':'mm-head:'+n;prev=n;}
    else if(!ins[i].trim())k='blank';
    if(!k.startsWith('mm-'))prev='';
    out.push({k,t:ins[i]});
  }
  return {lines:lines(v),rows:out};
}
// What the panel is apart from the lists, the empty rows and the rows under MESSAGES' and MEM's heads (their heads stand, their words are not compared: a count in them moves).
const rest=p=>p.rows.filter(r=>!['list','blank','mm-item'].includes(r.k)).map(r=>r.k==='repo'?'REPO':r.k.startsWith('mm-head')?r.k:r.t);
const items=(p,name)=>{let on=false,n=0;for(const r of p.rows){if(r.k==='list'){if(/^(STATUS|BACKLOG)\b/.test(r.t))on=r.t.startsWith(name);else if(on)n++;}else on=false;}return n;};
const mmRows=(p,name)=>{let on=false,n=0;for(const r of p.rows){if(r.k==='mm-head:'+name)on=true;else if(on&&r.k==='mm-item')n++;else on=false;}return n;};
"""

    def test_the_lists_never_move_the_rest_of_the_panel(self):
        # Every list state at every height from 6 to 60, at the four widths: with the lists up the panel, but for their rows, the empty rows and the rows under MESSAGES' and
        # MEM's heads, is the panel without them; where the heads are not up the whole panel but its REPO row is, byte for byte, and the REPO row ends in the two buttons.
        self.node(self.SORT + r"""
const lists=[[status(3,5),backlog(3,14)],[status(1,1),backlog(1,1)],[status(0,0),backlog(0,0)],[status(0,0,false),backlog(0,0,false)],[status(3,5),undefined]];   // the last: no BACKLOG value at all (the older host)
let cases=0,up=0,off=0;
for(const width of [59,44,40,30])for(let height=6;height<=60;height++)for(const question of [false,true])for(const routes of [0,3])for(const messages of [0,7])for(const mem of [0,5]){
  const peers=height%2===0,o={width,height,question,routes,messages,mem,peers},base=sort(drawN({...o,status:undefined,backlog:undefined}));
  lists.forEach(([s,b],n)=>{
    if(n>=3&&(height%3||width!==44))return;   // the mixed document states: a third of the heights
    const v=drawN({...o,status:s,backlog:b}),p=sort(v),where=JSON.stringify(o)+n+'\n'+p.lines.join('\n')+'\n'+base.lines.join('\n');
    assert.equal(p.lines.length,height,where);assert(p.lines.every(l=>cells(l)===width),where);cases++;
    const heads=p.rows.filter(r=>r.k==='list'&&/^(STATUS|BACKLOG)\b/.test(r.t)).length;
    if(heads){
      up++;assert.equal(heads,n===4?1:2,where);
      assert.deepEqual(rest(p),rest(base),where);
      assert(!v.hits.some(h=>h.row===1&&/^popup:(STATUS|BACKLOG)/.test(h.action)),'no buttons beside the heads\n'+where);
    }else{
      off++;
      assert.deepEqual(p.lines.filter((_,i)=>i!==1),base.lines.filter((_,i)=>i!==1),where);
      const press=new Set(v.hits.filter(h=>h.row===1).map(h=>h.action));
      assert(press.has('popup:STATUS')&&(n===4?!press.has('popup:BACKLOG'):press.has('popup:BACKLOG')),JSON.stringify([...press])+'\n'+where);
      assert(/›/.test(p.lines[1]),where);
    }
  });
}
assert(up>1000&&off>1000,[cases,up,off].join(' '));
""")

    def test_the_lists_give_way_first_and_in_their_order_as_the_height_falls(self):
        # Height by height: the BACKLOG items, then MESSAGES and MEM fold (each keeps its head and one row), then the STATUS items, then the two heads (the REPO row takes the buttons).
        self.node(self.SORT + r"""
const o={width:44,question:true,routes:3,messages:7,mem:5,peers:true};
const seen=[];
for(let h=70;h>=14;h--){
  const p=sort(drawN({...o,height:h,status:status(),backlog:backlog()})),base=sort(drawN({...o,height:h}));
  const r={h,b:items(p,'BACKLOG'),s:items(p,'STATUS'),heads:p.rows.filter(x=>x.k==='list'&&/^(STATUS|BACKLOG)\b/.test(x.t)).length,msg:mmRows(p,'MESSAGES'),mem:mmRows(p,'MEM'),
    msg0:mmRows(base,'MESSAGES'),mem0:mmRows(base,'MEM'),gap:p.rows.filter(x=>x.k==='blank').length};
  seen.push(r);const w=JSON.stringify(r)+'\n'+p.lines.join('\n');
  assert.deepEqual(rest(p),rest(base),w);
  if(r.heads){
    // BACKLOG items stand only on empty rows: where they stand nothing is folded.
    if(r.b>0)assert.deepEqual([r.msg,r.mem],[r.msg0,r.mem0],w);
    // STATUS items give way only once MESSAGES and MEM are down to one row each, and then BACKLOG has none.
    if(r.s<3)assert(r.b===0&&r.msg<=1&&r.mem<=1,w);
    // A BACKLOG item missing while nothing is folded and a row is empty: never.
    if(r.b<3&&r.msg===r.msg0&&r.mem===r.mem0)assert.equal(r.gap,0,w);
  }else{
    // The heads gone: MESSAGES and MEM are what they are without the lists.
    assert.deepEqual([r.b,r.s,r.msg,r.mem],[0,0,r.msg0,r.mem0],w);
  }
}
// Falling, each part only goes (BACKLOG items, STATUS items, the heads never come back as the height falls).
for(let i=1;i<seen.length;i++){const a=seen[i-1],b=seen[i];assert(b.b<=a.b&&b.s<=a.s&&b.heads<=a.heads,JSON.stringify([a,b]));}
// And the four steps all stand in this run, in this order.
const first=f=>seen.findIndex(f);
const at=[first(r=>r.heads&&r.b<3),first(r=>r.heads&&r.b===0&&(r.msg<r.msg0||r.mem<r.mem0)),first(r=>r.heads&&r.s<3),first(r=>!r.heads)];
assert(at.every(x=>x>0)&&at[0]<at[1]&&at[1]<at[2]&&at[2]<at[3],JSON.stringify({at,seen:seen.slice(0,3)}));
// The full panel stands whole and the same at a height with room to spare.
assert.deepEqual([seen[0].b,seen[0].s,seen[0].heads],[3,3,2]);
""")

    def test_the_repo_row_ends_in_the_two_buttons_where_the_heads_go_and_gives_way_as_the_bottom_row_does(self):
        self.node(self.SORT + r"""
const press=(v,a)=>v.hits.find(h=>h.row===1&&h.action===a);
for(const [w,want] of [[59,/^REPO   hearting · wt 12 +STATUS \+5 ›  BACKLOG \+14 ›$/],[44,/^REPO   hearting +STATUS ›  BACKLOG ›$/],[40,/^REPO   hearting +STATUS ›  BACKLOG ›$/],[30,/^REPO hearting · wt 12 ›  ›$/]]){
  const v=drawN({width:w,height:14,question:true,routes:3,messages:7,mem:5,peers:true,status:status(),backlog:backlog()}),p=sort(v);
  assert(!p.rows.some(r=>r.k==='list'),w+'\n'+p.lines.join('\n'));
  assert.match(p.rows[0].t.trimEnd(),want,w+' '+p.rows[0].t);
  assert(p.lines.every(l=>cells(l)===w));
  // A press on a button opens that popup; on the rest of the row, the status popup.
  const a=press(v,'popup:STATUS'),b=press(v,'popup:BACKLOG');
  assert(a&&b&&a.left===2&&b.right===w-3&&a.right+1===b.left,w+' '+JSON.stringify(v.hits.filter(h=>h.row===1)));
  const row=p.lines[1],cell=x=>[...row].reduce((s,c)=>s+(s.n<x?(s.n+=cells(c),c):''),{n:0,toString(){return ''}});
  assert(row.slice(0,2)==='│ '||true);
}
// The counts give way first, then the words, as the bottom layout's first row: the same pieces at widths that cut them.
{const at=(w,s,b)=>sort(drawN({width:w,height:14,question:true,routes:3,messages:7,mem:5,peers:true,status:status(3,s),backlog:backlog(3,b)})).rows[0].t.trimEnd();
 assert.match(at(59,5,14),/STATUS \+5 ›  BACKLOG \+14 ›$/);assert.match(at(50,5,14),/STATUS \+5 ›  BACKLOG \+14 ›$/);assert.match(at(44,5,14),/hearting +STATUS ›  BACKLOG ›$/);
 // Only the STATUS value (no backlog value): one button, the whole row is its press.
 const v=drawN({width:44,height:14,question:true,routes:3,messages:7,mem:5,peers:true,status:status(3,5)}),h=v.hits.filter(x=>x.row===1);
 assert.deepEqual(h.map(x=>x.action),['popup:STATUS']);assert.equal(h[0].right,44-3);}
// Where the heads stand there is no button on the row.
{const v=drawN({width:44,height:40,routes:1,messages:3,mem:2,peers:true,status:status(),backlog:backlog()});
 assert(!v.hits.some(h=>h.row===1),JSON.stringify(v.hits.filter(h=>h.row===1)));assert(!/›/.test(lines(v)[1]));}
""")

    def test_the_bottom_layout_has_two_buttons_and_no_list_and_is_no_taller(self):
        self.node(r"""
const two=(w,h,s,b,o={})=>draw({side:false,width:w,height:h,status:{word:'STATUS',has_doc:true,items:SI,total:s},backlog:{word:'BACKLOG',has_doc:true,items:BI,total:b},...o});
for(const w of [120,80,60]){const v=two(w,8,5,14),ls=lines(v);
  assert(/STATUS( \+5)? ›  BACKLOG( \+14)? › │$/.test(ls[1])||/ ›  › │$/.test(ls[1]),w+' '+ls[1]);
  assert(ls.every(l=>cells(l)===w));
  assert(!ls.some(l=>/^│ [!·] /.test(l)),ls.join('\n'));
  const [a,b]=['popup:STATUS','popup:BACKLOG'].map(x=>v.hits.find(h=>h.row===1&&h.action===x));
  assert(a&&b&&a.left===2&&b.right===w-3&&a.right+1===b.left,JSON.stringify(v.hits));
  // No taller than the panel without them, whatever the height.
  for(const h of [3,4,5,6,8,12]){assert.equal(lines(two(w,h,5,14)).length,lines(draw({side:false,width:w,height:h})).length);}}
// The counts go first, then the words: 120 and 80 whole, 60 the words only, 52 the arrows only.
assert.match(lines(two(120,8,5,14))[1],/ctx 41% +STATUS \+5 ›  BACKLOG \+14 › │$/);
assert.match(lines(two(80,8,5,14))[1],/ctx 41% +STATUS \+5 ›  BACKLOG \+14 › │$/);
assert.match(lines(two(60,8,5,14))[1],/ctx 41% +STATUS ›  BACKLOG › │$/);
assert.match(lines(two(52,8,5,14))[1],/ctx 41% +›  › │$/);
// A zero count is not written.
assert.match(lines(two(120,8,0,0))[1],/ctx 41% +STATUS ›  BACKLOG › │$/);
""")

    def test_a_backlog_longer_than_is_read_says_so_with_a_plus_after_its_count(self):
        self.node(r"""
const more=(n,total,has=true)=>({...backlog(n,total,has),more:true});
const heads=(b,o={})=>inside(draw({status:status(),backlog:b,height:40,...o})).filter(l=>/^BACKLOG/.test(l));
// `+N` is what the rows do not show, and the `+` after it that the file has more than was read.
assert(/^BACKLOG \+2997\+ ─+ ›$/.test(heads(more(3,3000))[0]),heads(more(3,3000))[0]);
assert(/^BACKLOG \+2997 ─+ ›$/.test(heads(backlog(3,3000))[0]));
// Nothing hidden by the rows and still more in the file: `+` alone.
assert(/^BACKLOG \+ ─+ ›$/.test(heads(more(3,3))[0]),heads(more(3,3))[0]);
// The items go first and the count grows with them, the `+` stays.
{const ls=inside(draw({status:status(),backlog:more(3,3000),routes:1,messages:3,mem:2,height:34}));
 assert(ls.some(l=>/^BACKLOG \+\d+\+ ─+ ›$/.test(l)),ls.join('\n'));}
// The bottom layout's button.
const two=(w,s,b)=>draw({side:false,width:w,height:8,status:{word:'STATUS',has_doc:true,items:SI,total:s},backlog:{...b,word:'BACKLOG',has_doc:true,items:[]}});
assert.match(lines(two(120,5,{total:2997,more:true}))[1],/STATUS \+5 ›  BACKLOG \+2997\+ › │$/);
assert.match(lines(two(120,5,{total:0,more:true}))[1],/STATUS \+5 ›  BACKLOG \+ › │$/);
assert.match(lines(two(120,5,{total:2997,more:false}))[1],/BACKLOG \+2997 › │$/);
""")

    def test_the_drawing_reads_no_file_and_asks_nothing(self):
        out = self.node(r"""
const calls=[];
for(const name of Object.getOwnPropertyNames(fs))if(typeof fs[name]==='function'&&/^(read|stat|lstat|open|access|exists|readdir|realpath)/.test(name)){
  const real=fs[name];fs[name]=function(...a){calls.push(name);return real.apply(this,a);};}
for(let i=0;i<60;i++)for(const side of [true,false])draw({side,width:side?59:100,height:side?34:8,status:status(),backlog:backlog(),routes:1,messages:3,mem:2});
console.log(JSON.stringify({file_reads:calls.length}));
""")
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {"file_reads": 0})


# -- the backend: the two short lists, the backlog document and its popup -------------------------------------------------------

BACKLOG = """# 시험용 백로그
갱신 2026-11-21 11:02

## 2026-11-02_some-campaign
- 일 하나
  date: 2026-11-20
  source: 어딘가
  note: 맥락 하나
- 일 둘
  - 하위 항목은 일이 아니다

## 2026-10-01_other-campaign
- 다른 일

```
## 코드 울타리 안
- 세지 않는다
```

## Unassigned
- 정해지지 않은 일
"""

STATUS_PAGE = """<!-- hearting:campaign-document v1 -->
# 시험용 캠페인

갱신 2026-11-21 11:02

## 하는 중
- 하는 일 A
- 하는 일 B
- 하는 일 C
- 하는 일 D

## 기다리는 것
- 정할 것 하나
- 정할 것 둘

## 최근에 끝난 것
- 끝난 일
앞서 끝난 기록 3개는 접어 둠.
"""


class BackendTest(unittest.TestCase):
    def setUp(self):
        from fleet import session_status
        import session_shell
        self.status, self.shell = session_status, session_shell
        self._tmp = tempfile.TemporaryDirectory(prefix="rl")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        patch = mock.patch.object(session_status, "_root", lambda cwd, now: str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        self.cwd = str(self.root)

    def backlog(self, text=BACKLOG):
        path = self.root / "backlog.md"
        path.write_text(text, encoding="utf-8")
        return path

    def page(self, name="2026-11-02_some-campaign", text=STATUS_PAGE):
        folder = self.root / "campaigns" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "campaign.md").write_text(text, encoding="utf-8")
        return {"path": str(folder / "campaign.md"), "has_doc": True}

    # (rule 5 of the form) ##-headings are the groups as written; a top bullet is an item; indented lines are its detail; fences are not read.
    def test_the_backlog_is_read_by_its_headings_bullets_and_indented_lines(self):
        parsed = self.status.parse_backlog(BACKLOG.splitlines())
        self.assertEqual(parsed["title"], "시험용 백로그")
        self.assertEqual(parsed["updated"], (2026, 11, 21, 11, 2))
        self.assertEqual([(g["name"], [i["text"] for i in g["items"]]) for g in parsed["groups"]],
                         [("2026-11-02_some-campaign", ["일 하나", "일 둘"]), ("2026-10-01_other-campaign", ["다른 일"]), ("Unassigned", ["정해지지 않은 일"])])
        self.assertEqual(parsed["groups"][0]["items"][0]["detail"], ["date: 2026-11-20", "source: 어딘가", "note: 맥락 하나"])
        self.assertEqual(parsed["groups"][0]["items"][1]["detail"], ["- 하위 항목은 일이 아니다"])      # a sub-bullet is detail, not an item
        # A document of another form does not break: what is read is shown, and no headings or no bullets is no item.
        for body in ("", "그냥 글\n", "- 머리말 없는 글머리표\n", "## 머리말만\n", "# 제목만\n\n갱신 2026-11-21 11:02\n", "\x00\x01 ## \n- \n"):
            parsed = self.status.parse_backlog(body.splitlines())
            self.assertEqual(sum(len(g["items"]) for g in parsed["groups"]), 0, body)
        # The same heading twice: two groups in the document's order.
        parsed = self.status.parse_backlog("## A\n- 1\n## A\n- 2\n- 3\n".splitlines())
        self.assertEqual([[i["text"] for i in g["items"]] for g in parsed["groups"]], [["1"], ["2", "3"]])

    def test_the_panels_backlog_items_are_this_windows_campaign_first_then_the_rest_and_the_count_is_the_whole_documents(self):
        parsed = self.status.parse_backlog(BACKLOG.splitlines())
        items, total = self.status.panel_backlog_items(parsed, "2026-11-02_some-campaign")
        self.assertEqual([i["text"] for i in items], ["일 하나", "일 둘", "다른 일"])
        self.assertEqual(total, 4)
        items, total = self.status.panel_backlog_items(parsed, "2026-10-01_other-campaign")
        self.assertEqual([i["text"] for i in items], ["다른 일", "일 하나", "일 둘"])
        items, total = self.status.panel_backlog_items(parsed, "")                         # no campaign known: the document's order
        self.assertEqual([i["text"] for i in items], ["일 하나", "일 둘", "다른 일"])
        items, total = self.status.panel_backlog_items(parsed, "2026-11-02_some-campaign-2")    # the name is the folder's, letter for letter
        self.assertEqual([i["text"] for i in items], ["일 하나", "일 둘", "다른 일"])
        self.assertTrue(all(i["mark"] == "" and i["key"].startswith("i:") for i in items))

    def test_the_panels_status_items_are_the_list_screens_order_waiting_then_in_progress_with_its_marks_and_keys(self):
        lines = STATUS_PAGE.splitlines()
        parsed = self.status.parse_listing(lines)
        items, total = self.status.panel_items(parsed)
        self.assertEqual([(i["mark"], i["text"]) for i in items], [("!", "정할 것 하나"), ("!", "정할 것 둘"), ("", "하는 일 A")])
        self.assertEqual(total, 6)                                                         # waiting 2 + in progress 4; the finished and the folded count nothing
        # The keys are the list screen's own: a press on the item can stand on it there.
        entries = self.status.listing_entries(parsed, 80)
        have = {entry["key"] for entry in entries if entry.get("key")}
        self.assertTrue({i["key"] for i in items} <= have)
        # One reading: the same items as the list screen draws, in the order it draws them.
        drawn = [text_of(entry["rows"][0]) for entry in entries if entry.get("key", "").startswith("i:")]
        self.assertEqual([t.strip().lstrip("!").strip() for t in drawn[:3]], [i["text"] for i in items])

    def test_the_lists_are_read_with_the_value_and_a_document_that_did_not_change_is_not_read_again(self):
        view = self.page()
        self.backlog()
        got = self.status.panel_lists(self.cwd, view, {}, now=time.time())
        self.assertEqual(got["status"]["total"], 6)
        self.assertEqual(len(got["status"]["items"]), 3)
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["total"]), (True, 4))
        # Asked again with the signs it gave: nothing is read or parsed.
        have = {"status": got["status"]["sign"], "backlog": got["backlog"]["sign"]}
        with mock.patch.object(self.status, "read_text", side_effect=AssertionError("read")), \
                mock.patch.object(self.status, "read_backlog", side_effect=AssertionError("read")), \
                mock.patch.object(self.status, "parse_listing", side_effect=AssertionError("parsed")), \
                mock.patch.object(self.status, "parse_backlog", side_effect=AssertionError("parsed")):
            again = self.status.panel_lists(self.cwd, view, have, now=time.time())
        self.assertEqual(again, {"status": {"same": True}, "backlog": {"same": True}})
        # A change (modification time or size) reads it again; the sign is text (a time in nanoseconds does not pass JSON numbers).
        self.assertIsInstance(got["backlog"]["sign"], str)
        path = self.backlog(BACKLOG + "- 더\n")
        os.utime(path, (1_800_000_000, 1_800_000_000))
        self.assertNotEqual(self.status.panel_lists(self.cwd, view, have, now=time.time())["backlog"], {"same": True})

    def test_no_backlog_an_unreadable_one_and_a_strange_one_stand_as_no_document_and_never_raise(self):
        view = self.page()
        got = self.status.panel_lists(self.cwd, view, {})
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["items"], got["backlog"]["total"]), (False, [], 0))
        (self.root / "backlog.md").mkdir()                                                   # a folder of that name
        got = self.status.panel_lists(self.cwd, view, {})
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["total"]), (False, 0))
        (self.root / "backlog.md").rmdir()
        (self.root / "backlog.md").symlink_to(self.root / "elsewhere.md")                    # a link (and a dangling one)
        self.assertEqual(self.status.panel_lists(self.cwd, view, {})["backlog"]["has_doc"], False)
        (self.root / "backlog.md").unlink()
        (self.root / "backlog.md").write_bytes(b"\xff\xfe## \x00\n- \xc3\n")                    # broken text
        got = self.status.panel_lists(self.cwd, view, {})
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["total"]), (True, 0))
        with mock.patch.object(self.status, "read_backlog", side_effect=PermissionError("no")):      # whatever the read raises
            self.assertIsNone(self.status.panel_lists(self.cwd, view, {}))
        with mock.patch.object(self.status, "_root", lambda cwd, now: None):                              # no artifact root: no lists
            self.assertIsNone(self.status.panel_lists(self.cwd, view, {}))
        # A status page that is not the generated one has no items (the popup has no list screen for it either).
        hand = self.page("2026-11-03_hand", "# 손으로 쓴 글\n\n## 하는 중\n- 하나\n")
        self.assertEqual(self.status.panel_lists(self.cwd, hand, {})["status"]["items"], [])

    def test_the_status_read_carries_the_lists_and_asks_the_signs_back(self):
        value = {"word": "STATUS", "has_doc": True, "path": self.page()["path"], "title": "STATUS x", "slot": "", "watch": []}
        self.backlog()
        with mock.patch.object(self.status, "read", lambda cwd, routes=(), now=None: value):
            first = self.shell._panel_status({}, {"cwd": self.cwd, "routes": []})
            self.assertEqual((first["ok"], first["status"]), (True, value))
            self.assertEqual(first["lists"]["backlog"]["total"], 4)
            have = {"status": first["lists"]["status"]["sign"], "backlog": first["lists"]["backlog"]["sign"], "junk": 5}
            second = self.shell._panel_status({}, {"cwd": self.cwd, "routes": [], "have": have})
            self.assertEqual(second["lists"], {"status": {"same": True}, "backlog": {"same": True}})
            # No artifact root: no value and no lists.
        with mock.patch.object(self.status, "read", lambda cwd, routes=(), now=None: None):
            self.assertEqual(self.shell._panel_status({}, {"cwd": self.cwd, "routes": []}), {"ok": True, "status": None, "lists": None})

    def popup(self, part, own="2026-11-02_some-campaign", width=80, kind="backlog", open=""):
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        return self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": part, "own": own, "open": open}, width, 256, False, kind)

    def test_the_backlog_popup_is_the_status_screens_own_parts_over_the_document(self):
        self.backlog()
        answer = self.popup("list")
        self.assertEqual((answer["ok"], answer["kind"], answer["part"], answer["rows"], answer["has_doc"]), (True, "backlog", "list", [], True))
        self.assertTrue(answer["title"].startswith("BACKLOG"))
        entries = answer["list"]
        shown = [("".join(run[0] for run in e["rows"][0]), e.get("key", "")[:2]) for e in entries]
        self.assertEqual([t for t, _ in shown], ["시험용 백로그", "updated 11-21 11:02", "", "2026-11-02_some-campaign 2", "  일 하나", "  일 둘", "",
                                                  "  › 2026-10-01_other-campaign 1", "    다른 일", "  › Unassigned 1", "    정해지지 않은 일", "", "  full document ›"])
        self.assertEqual([k for _, k in shown], ["", "", "", "", "i:", "i:", "", "g:", "i:", "g:", "i:", "", "fu"])
        # The answer carries titles only: an item with indented lines under it is `lazy` (they are asked for when it is opened), and no entry holds them.
        self.assertTrue(all("more" not in e for e in entries))
        self.assertEqual([bool(e.get("lazy")) for e in entries if e.get("key", "").startswith("i:")], [True, True, False, False])
        self.assertNotIn("date: 2026-11-20", json.dumps(answer, ensure_ascii=False))
        # The lines come with the item's own ask.
        key = entries[4]["key"]
        got = self.popup("detail", open=key)
        self.assertEqual((got["ok"], got["part"], got["open"]), (True, "detail", key))
        self.assertEqual(["".join(run[0] for run in row) for row in got["rows"]], ["    date: 2026-11-20", "    source: 어딘가", "    note: 맥락 하나"])
        folded = [e for e in entries if e.get("key", "").startswith("i:") and e.get("when") == "open"][0]
        self.assertEqual(self.popup("detail", open=folded["key"])["rows"], [])                     # an item with no lines under it: none
        for bad in ("", "g:abc", "i:nothere", "x" * 80):
            self.assertFalse(self.popup("detail", open=bad)["ok"], bad)
        self.assertEqual(self.popup("detail", open=key, kind="status", own=""), {"ok": False, "reason": "invalid-status"})
        # Not the campaign of this window: every group is a folded line, in the document's order.
        other = [("".join(run[0] for run in e["rows"][0])) for e in self.popup("list", own="")["list"] if e.get("key", "").startswith("g:")]
        self.assertEqual(other, ["  › 2026-11-02_some-campaign 2", "  › 2026-10-01_other-campaign 1", "  › Unassigned 1"])
        # The document read whole is the other answer; the Fleet part is not the backlog's.
        whole = self.popup("doc")
        self.assertNotIn("list", whole)
        self.assertTrue(any("2026-11-02_some-campaign" in "".join(run[0] for run in row) for row in whole["rows"]))
        self.assertEqual(self.popup("fleet"), {"ok": False, "reason": "invalid-status"})
        # An older document is said to be old.
        os.utime(self.root / "backlog.md", (time.time() - 5 * 86400,) * 2)
        self.assertIn("old document", "".join(run[0] for run in self.popup("list")["list"][1]["rows"][0]))

    def test_the_backlog_popup_without_a_document_says_so_in_one_line(self):
        for part in ("list", "doc"):
            answer = self.popup(part)
            self.assertEqual((answer["ok"], answer["has_doc"], answer["title"]), (True, False, "BACKLOG"))
            self.assertNotIn("list", answer)
            self.assertEqual(["".join(run[0] for run in row) for row in answer["rows"]], ["No backlog document yet."])
        (self.root / "backlog.md").mkdir()
        self.assertEqual(self.popup("list")["has_doc"], False)

    def test_an_item_opens_to_its_indented_lines_in_the_status_lists_own_code_and_a_press_stands_on_it(self):
        self.backlog()
        answer = self.popup("list")
        detail = self.popup("detail", open=answer["list"][4]["key"])
        with tempfile.TemporaryDirectory(prefix="rlj") as folder:
            path, more = Path(folder) / "answer.json", Path(folder) / "detail.json"
            path.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
            more.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
            out = subprocess.run([NODE, "-e", r"""
const fs=require('node:fs'),path=require('node:path');
const list=require(path.join(process.argv[1],'status-list.cjs'));
const entries=list.entriesOf(JSON.parse(fs.readFileSync(process.argv[2],'utf8'))),text=r=>r.map(p=>p.text).join('');
const detail=list.rowsOf(JSON.parse(fs.readFileSync(process.argv[3],'utf8')));
const st={sel:'',fold:new Set(),items:new Set(),more:{}};
const lines=()=>list.layout(entries,st,{width:60}).rows.map(text);
const first=lines(),item=list.layout(entries,st).marks.find(m=>m.key.startsWith('i:')&&m.text);
// (the first item has lines under it: it can be opened; an item with none is not lazy and has nothing to open)
const plain=entries.find(e=>e.key&&e.key.startsWith('i:')&&!e.lazy);
const out={item_can_open:!!item,item_lazy:item.lazy,plain_can_open:!!(plain.text||plain.more||plain.lazy)};
out.pressed=list.press(st,list.layout(entries,st),item.key);
// until the lines come one dim `⠏` stands under the row; then they do
out.waiting=lines().slice(lines().findIndex(l=>l.startsWith('> '))+1)[0];
st.more[item.key]=detail;
const opened=lines();out.more=opened.length-first.length;out.shown=opened.filter(l=>/date:|source:|note:/.test(l));
const v=list.layout(entries,st,{width:60});out.tinted=v.rows.slice(v.at,v.at+v.span).every(r=>r.some(p=>p.tint));out.span=v.span;
out.hits=list.hits(v).filter(h=>h.action[1]===item.key).length;
out.press_again=list.press(st,list.layout(entries,st),item.key);out.closed=lines().length===first.length;
// A press from the panel stands on the item (the popup is opened with its key as the chosen line).
const sel={sel:item.key,fold:new Set(),items:new Set()};out.stands=list.layout(entries,sel).sel===item.key&&!list.layout(entries,sel).fellBack;
const gone={sel:'i:nothere',fold:new Set(),items:new Set()};out.fell=list.layout(entries,gone).fellBack;
// The second group is a folded line; pressing it opens its items.
const g=list.layout(entries,st).marks.find(m=>m.key.startsWith('g:'));out.group=list.press(st,list.layout(entries,st),g.key);
out.group_items=lines().filter(l=>/다른 일/.test(l)).length;
console.log(JSON.stringify(out));
""", str(SHELL), str(path), str(more)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got, {"item_can_open": True, "item_lazy": True, "plain_can_open": False, "pressed": "item", "waiting": "    ⠏", "more": 3,
                               "shown": ["    date: 2026-11-20", "    source: 어딘가", "    note: 맥락 하나"], "tinted": True, "span": 4, "hits": 4,
                               "press_again": "item", "closed": True, "stands": True, "fell": True, "group": "fold", "group_items": 1})

    # -- the entrance's counting rule, and a backlog far longer than the first one -------------------------------------------

    def test_a_task_is_counted_by_the_entrances_rule_and_the_updated_line_by_its_own(self):
        def items(body):
            return [(g["name"], [i["text"] for i in g["items"]]) for g in self.status.parse_backlog(body.splitlines())["groups"]]
        self.assertEqual(items("- 머리말 앞\n## A\n- 하나\n"), [("A", ["하나"])])                                     # a `- ` before the first `##` is none
        self.assertEqual(items("## A\n* 별\n+ 더하기\n1. 번호\n2) 번호\n- 하나\n  - 하위\n    - 더 하위\n"), [("A", ["하나"])])    # only `- `; no sub-bullet
        # A fence closes on the same character and the same or a longer length: a short one inside a longer one, and a wrong character, close nothing.
        self.assertEqual(items("## A\n````\n```\n- 안\n```\n- 아직 안\n````\n- 밖\n"), [("A", ["밖"])])
        self.assertEqual(items("## A\n~~~\n```\n- 안\n~~~~\n- 밖\n"), [("A", ["밖"])])
        self.assertEqual(items("## A\n- 앞\n```\n- 닫히지 않음\n## B\n- 끝까지\n"), [("A", ["앞"])])                 # never closed: to the end
        self.assertEqual(items("## A\n```python\n- 안\n```\n- 밖\n"), [("A", ["밖"])])
        self.assertEqual(items("```\n## 울타리 안 머리말\n```\n## A\n- 하나\n"), [("A", ["하나"])])
        self.assertEqual(items("## A\n- 하나\n# 다른 제목\n- 둘\n### 더 깊은\n- 셋\n"), [("A", ["하나", "둘", "셋"])])        # only `##` heads a group
        # The `갱신` line: before the first `##` only, and a real date and time only.
        def stamp(body):
            return self.status.parse_backlog(body.splitlines())["updated"]
        self.assertEqual(stamp("# T\n갱신 2026-11-21 11:02\n## A\n- 일\n"), (2026, 11, 21, 11, 2))
        self.assertIsNone(stamp("# T\n## A\n갱신 2026-11-21 11:02\n- 일\n"))                                            # after the first `##`
        self.assertIsNone(stamp("갱신 2026-13-45 99:99\n## A\n"))
        self.assertIsNone(stamp("갱신 2026-02-30 10:00\n## A\n"))
        self.assertIsNone(stamp("updated 2026-11-21 11:02\n## A\n"))                                                   # the status page's looser word is not this document's
        self.assertIsNone(stamp("```\n갱신 2026-11-21 11:02\n```\n## A\n"))
        self.assertEqual(stamp("갱신 2026-11-21 11:02\n갱신 2026-11-22 12:00\n## A\n"), (2026, 11, 21, 11, 2))          # the first one
        # The panel and the popup count the same.
        body = "# T\n- 앞\n## A\n- 하나\n* 둘\n```\n- 셋\n```\n- 넷\n"
        self.backlog(body)
        got = self.status.panel_lists(self.cwd, self.page(), {})["backlog"]
        self.assertEqual((got["total"], [i["text"] for i in got["items"]]), (2, ["하나", "넷"]))
        heads = [e for e in self.popup("list", own="A")["list"] if e.get("rows") and "".join(r[0] for r in e["rows"][0]).startswith("A ")]
        self.assertEqual("".join(r[0] for r in heads[0]["rows"][0]), "A 2")

    # What the artifact entrance (`utilities/artifact_entrance.py` `_backlog`, the PR that reads `core/ARTIFACT_META.md`'s rule) counts and reads for the same
    # texts: (name, text, its count, its `갱신` as `YYYY-MM-DD HH:MM` or ''). The expectations are written out (the entrance is not in this tree yet) from running its
    # function on these texts; the panel's reading (`parse_backlog`) and the popup's list must give the same count and the same date.
    ENTRANCE = [
        ('before_first_heading', '- 앞\n## A\n- 하나\n', 1, ''),
        ('no_heading_at_all', '# T\n- a\n- b\n', 0, ''),
        ('indented_heading', '# T\n - x\n  ## A\n- 앞이라 세지 않는다\n## B\n- 하나\n', 1, ''),
        ('only_indented_heading', '  ## A\n- 앞\n', 0, ''),
        ('heading_without_space', '##A\n- 하나\n## B\n- 둘\n', 1, ''),
        ('heading_alone', '##\n- 하나\n', 1, ''),
        ('heading_tab', '##\tA\n- 하나\n', 1, ''),
        ('deeper_headings_are_none', '### A\n- 앞\n## B\n- 하나\n### C\n- 둘\n#### D\n- 셋\n', 3, ''),
        ('star_plus_number_tab', '## A\n* 별\n+ 더하기\n1. 번호\n2) 번호\n-\t탭\n- 하나\n-하나\n', 1, ''),
        ('indented_and_sub_bullets', '## A\n- 하나\n  - 하위\n    - 더 하위\n  note: x\n\t- 탭 하위\n- 둘\n', 2, ''),
        ('short_fence_in_longer', '## A\n````\n```\n- 안\n```\n- 아직 안\n````\n- 밖\n', 1, ''),
        ('other_char_fence_in_fence', '## A\n~~~\n```\n- 안\n~~~~\n- 밖\n', 1, ''),
        ('unclosed_fence', '## A\n- 앞\n```\n- 안\n## B\n- 끝까지\n', 1, ''),
        ('fence_with_info', '## A\n```python\n- 안\n```\n- 밖\n', 1, ''),
        ('fence_with_backtick_in_info', '## A\n``` a`b\n- 안\n```\n- 밖\n', 1, ''),
        ('fence_closer_with_text', '## A\n```\n- 안\n``` text\n- 아직 안\n```\n- 밖\n', 1, ''),
        ('fence_closer_with_blanks', '## A\n```\n- 안\n```   \n- 밖\n', 1, ''),
        ('fence_indented_three_four', '## A\n   ```\n- 안\n   ```\n    ```\n- 밖\n', 1, ''),
        ('heading_in_fence', '```\n## 안\n```\n- 앞\n## A\n- 하나\n', 1, ''),
        ('rule_looking_line', '## A\n- - -\n- 하나\n', 2, ''),
        ('html_comment', '## A\n<!--\n- 주석 안\n- 또\n-->\n- 밖\n', 3, ''),
        ('empty_bullet', '## A\n- \n- 하나\n-\n', 2, ''),
        ('two_groups_same_heading', '## A\n- 1\n## A\n- 2\n- 3\n', 3, ''),
        ('updated_ok', '# T\n갱신 2026-11-21 11:02\n## A\n- 일\n', 1, '2026-11-21 11:02'),
        ('updated_wrong_date', '갱신 2026-13-45 99:99\n## A\n- 일\n', 1, ''),
        ('updated_feb30', '갱신 2026-02-30 10:00\n## A\n', 0, ''),
        ('updated_no_time', '갱신 2026-11-21\n## A\n- 일\n', 1, ''),
        ('updated_colon', '갱신: 2026-11-21 11:02\n## A\n', 0, ''),
        ('updated_fullwidth', '갱신 ２０２６-１１-２１ １１:０２\n## A\n', 0, ''),
        ('updated_surrounded', '   갱신 2026-11-21 23:10   \n## A\n- 일\n', 1, '2026-11-21 23:10'),
        ('updated_tab_inside', '갱신\t2026-11-21 11:02\n## A\n', 0, ''),
        ('updated_two_blanks', '갱신  2026-11-21 11:02\n## A\n', 0, ''),
        ('updated_with_T', '갱신 2026-11-21T11:02\n## A\n', 0, ''),
        ('updated_seconds', '갱신 2026-11-21 11:02:30\n## A\n', 0, ''),
        ('updated_after_heading', '## A\n갱신 2026-11-21 11:02\n- 일\n', 1, ''),
        ('updated_in_fence', '```\n갱신 2026-11-21 11:02\n```\n## A\n', 0, ''),
        ('updated_first_bad_second_good', '갱신 2026-13-45 99:99\n갱신 2026-11-22 12:00\n## A\n', 0, '2026-11-22 12:00'),
        ('updated_first_good_second_good', '갱신 2026-11-21 11:02\n갱신 2026-11-22 12:00\n## A\n', 0, '2026-11-21 11:02'),
        ('updated_in_title_block', '# 제목\n\n갱신 2026-11-21 11:02\n\n## A\n- 일\n', 1, '2026-11-21 11:02'),
        ('updated_english_word', 'updated 2026-11-21 11:02\n## A\n', 0, ''),
        ('updated_after_bad_prose', '설명 글\n갱신 2026-11-21 11:02\n## A\n', 0, '2026-11-21 11:02'),
        ('empty', '', 0, ''),
        ('crlf', '## A\r\n- 하나\r\n- 둘\r\n', 2, ''),
        ('no_final_newline', '## A\n- 하나', 1, ''),
        ('other_line_breaks', '## A\n- 하나\u2028- 둘\x0c- 셋\n', 3, ''),
    ]

    def test_the_count_and_the_date_are_the_entrances_on_the_same_texts(self):
        for name, text, count, updated in self.ENTRANCE:
            parsed = self.status.parse_backlog(text.splitlines())
            when = "%04d-%02d-%02d %02d:%02d" % parsed["updated"] if parsed["updated"] else ""
            self.assertEqual((self.status.backlog_total(parsed), when), (count, updated), name)
            self.backlog(text)                                                       # the file read the way the panel reads it, and the list the popup draws
            got = self.status.panel_lists(self.cwd, self.page(), {})["backlog"]
            self.assertEqual(got["total"], count, name)
            if parsed["groups"]:
                self.assertEqual(sum(int(e["rows"][0][-1][0].rsplit(" ", 1)[-1]) for e in self.popup("list", own="")["list"] if e.get("key", "").startswith("g:")),
                                 count, name)
        # 64 KiB: exactly it the entrance counts (8191 here), one byte over it has no count at all (`count` None: the line only). The panel is not so bounded
        # (it reads to 1 MiB and says `+` past that: an addition of the real backlog's size) and counts the same 8191.
        def sized(n):
            body, k = "## A\n", 0
            while len((body + "- %05d\n" % (k + 1)).encode()) <= n:
                k += 1
                body += "- %05d\n" % k
            return body + "x" * (n - len(body.encode()))
        for size, entrance in ((65536, 8191), (65537, None)):
            text = sized(size)
            self.assertEqual(len(text.encode()), size)
            self.backlog(text)
            got = self.status.panel_lists(self.cwd, self.page(), {})["backlog"]
            self.assertEqual((got["total"], got["more"]), (8191, False), size)

    def long_backlog(self, items, note=300, groups=1, title="시험용 나중 일"):
        out, per = ["# 시험용 백로그", "갱신 2026-11-21 11:02", ""], max(1, items // groups)
        n = 0
        for g in range(groups):
            out.append("## 2026-11-02_some-campaign" if g == 0 else "## 2026-10-%02d_other-%d" % (g % 28 + 1, g))
            for _ in range(per if g < groups - 1 else items - n):
                n += 1
                out += ["- %s %d — 제목이 한 줄로 이어지는 시험용 글입니다" % (title, n), "  date: 2026-11-%02d" % (n % 28 + 1), "  source: 시험용 출처 %d" % n,
                        "  note: " + ("시험용 맥락이 길게 이어집니다 " * 40)[:note]]
            out.append("")
        return "\n".join(out) + "\n"

    def test_a_long_backlog_stands_as_a_list_of_titles_with_its_whole_count(self):
        for count, size_at_least in ((110, 40_000), (500, 200_000)):
            body = self.long_backlog(count)
            self.assertGreater(len(body.encode()), size_at_least)
            self.backlog(body)
            got = self.status.panel_lists(self.cwd, self.page(), {})["backlog"]
            self.assertEqual((got["total"], len(got["items"]), got["more"]), (count, 3, False))
            answer = self.popup("list")
            self.assertIn("list", answer, count)                                          # the list stands: not the document read whole
            self.assertLessEqual(len(json.dumps({"id": 0, **answer}, ensure_ascii=False).encode()), self.shell.BACKLOG_ANSWER_BYTES)
            titles = [e for e in answer["list"] if e.get("key", "").startswith("i:")]
            self.assertEqual(len(titles), count)                                          # every title; no note in the answer
            self.assertNotIn("시험용 맥락이 길게", json.dumps(answer, ensure_ascii=False))
            self.assertEqual(sum(1 for e in titles if e.get("lazy")), count)
            self.assertNotIn("more in backlog.md", json.dumps(answer, ensure_ascii=False))

    def test_a_backlog_of_thousands_is_read_as_far_as_it_goes_and_says_there_is_more(self):
        # Titles alone pass the answer's size: the most that fit stand, then one line says how many more, and `full document ›` is there.
        self.backlog(self.long_backlog(4000, note=20))
        got = self.status.panel_lists(self.cwd, self.page(), {})["backlog"]
        self.assertEqual((got["total"], got["more"]), (4000, False))
        answer = self.popup("list")
        self.assertIn("list", answer)
        shown = ["".join(r[0] for r in e["rows"][0]) for e in answer["list"] if e.get("rows") and e["rows"][0]]
        self.assertEqual(shown[-1], "  full document ›")
        more = [t for t in shown if "more in backlog.md" in t]
        self.assertEqual(len(more), 1)
        drawn = sum(1 for e in answer["list"] if e.get("key", "").startswith("i:"))
        self.assertEqual(more[0].strip(), "… %d more in backlog.md" % (4000 - drawn))
        self.assertTrue(0 < drawn < 4000)
        self.assertLessEqual(len(json.dumps({"id": 0, **answer}, ensure_ascii=False).encode()), self.shell.BACKLOG_ANSWER_BYTES)
        self.assertLess(len(json.dumps({"id": 0, **answer}, ensure_ascii=False).encode()), 256 * 1024)       # what the host reads of an answer
        # Past what is read at all (a file longer than the read limit): what was read is counted and the panel says there is more (`+`).
        with mock.patch.object(self.status, "BACKLOG_READ_LIMIT", 100_000):
            self.backlog(self.long_backlog(2000, note=60))
            got = self.status.panel_lists(self.cwd, self.page(), {})["backlog"]
            self.assertTrue(got["more"] and 0 < got["total"] < 2000, got)
            self.assertTrue(got["has_doc"] and len(got["items"]) == 3)
            answer = self.popup("list")
            self.assertIn("list", answer)
            self.assertIn("the file is longer than is read here", json.dumps(answer, ensure_ascii=False))
            self.assertEqual(self.popup("doc")["has_doc"], True)

    def test_the_status_read_of_a_long_backlog_is_bounded(self):
        # A file of several MiB is read to the limit and no further.
        with mock.patch.object(self.status, "BACKLOG_READ_LIMIT", 50_000):
            self.backlog(self.long_backlog(3000, note=100))
            self.assertGreater((self.root / "backlog.md").stat().st_size, 500_000)
            lines, cut = self.status.read_backlog(str(self.root))
            self.assertTrue(cut)
            self.assertLessEqual(sum(len(line.encode()) + 1 for line in lines), 50_000)
            self.assertFalse(lines[-1].startswith("- 시험용 나중 일 3000"))

    # -- the review of #1793, the three it left -------------------------------------------------------------------------

    def test_a_list_dropped_for_its_size_leaves_no_stale_style_index(self):
        # A generated page with no bullet at all and so much text that the list and the document both pass the answer's limit: the list is dropped, and
        # the closing `…` row points at a style the answer has (it did not, once the styles the list added were dropped but their names were kept).
        body = STATUS_PAGE.split("## 하는 중")[0] + "## 기다리는 것\n" + "".join("note line %04d " % n + "z" * 60 + "\n" for n in range(780))
        view = self.page("2026-11-04_big", body)
        view = {**view, "title": "STATUS fixture", "word": "STATUS", "slot": "", "watch": [], "age": "", "campaign": "fixture"}
        os.utime(view["path"], (1_800_000_000, 1_800_000_000))
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        answers = {}
        for part in ("list", "doc"):
            with mock.patch.object(self.status, "held", return_value=view), mock.patch.object(self.shell, "_popup_view_model", side_effect=AssertionError("Fleet")):
                answers[part] = self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": part}, 100, 256, False)
        first = answers["list"]
        self.assertNotIn("list", first)                                                       # the list passed the limit: the document's screen
        for answer in answers.values():
            used = {run[1] for row in answer["rows"] for run in row}
            self.assertTrue(used and max(used) < len(answer["styles"]), (used, len(answer["styles"])))
        self.assertEqual({k: v for k, v in first.items() if k != "part"}, {k: v for k, v in answers["doc"].items() if k != "part"})
        self.assertEqual(first["styles"][first["rows"][-1][0][1]].get("dim"), True)           # the closing `…` is dim

    def test_the_documents_rows_are_made_only_where_the_list_is_dropped(self):
        view = {**self.page(), "word": "STATUS", "title": "STATUS x", "slot": "", "watch": [], "age": "", "campaign": "x"}
        with mock.patch.object(self.status, "_doc_rows_of", side_effect=AssertionError("rows were made for a list that stands")):
            rows, listing = self.status.popup_doc(view, 80, time.time(), want_list=True)
            self.assertTrue(callable(rows) and listing)
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        with mock.patch.object(self.status, "held", return_value=view), mock.patch.object(self.status, "_doc_rows_of", side_effect=AssertionError("made")):
            self.assertIn("list", self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": "list"}, 80, 256, False))
        # The full document asks without a list and gets its rows as before; a dropped list makes them once.
        rows, listing = self.status.popup_doc(view, 80, time.time())
        self.assertEqual((type(rows), listing), (list, None))
        calls = []
        real = self.status._doc_rows_of
        with mock.patch.object(self.status, "_doc_rows_of", lambda *a: calls.append(1) or real(*a)), \
                mock.patch.object(self.shell, "POPUP_ANSWER_BYTES", 300), mock.patch.object(self.status, "held", return_value=view):
            answer = self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": "list"}, 80, 256, False)
        self.assertEqual((len(calls), "list" in answer, bool(answer["rows"])), (1, False, True))


HOST = r"""
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const root=process.argv[1];
const src=fs.readFileSync(path.join(root,'host.cjs'),'utf8');
const cut=(from,to)=>{const a=src.indexOf(from),b=src.indexOf(to,a);if(a<0||b<0)throw new Error('marker '+from);return src.slice(a,b);};
const statusList=require(path.join(root,'status-list.cjs'));
const same=(a,b)=>assert.equal(JSON.stringify(a),JSON.stringify(b));   // (objects made inside a vm context are not the main context's)
"""


@unittest.skipUnless(NODE, "needs node")
class HostTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HOST + body, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_status_read_brings_the_two_lists_and_a_document_that_did_not_change_keeps_its_own(self):
        self.node(r"""(async()=>{
const asked=[];let answers=[];
const ctx={process,path,JSON,Set,Array,Number,String,Math,Promise,panelModel:{},table:null,selection:1,lastWatch:[],listed:k=>k==='STATUS'||k==='BACKLOG',
  readAction:(extra,op)=>{asked.push(JSON.parse(JSON.stringify(extra)));return Promise.resolve(answers.shift());},
  observeFiles:()=>false,queueProjection:()=>{},queueDisplay:()=>{},askTable:()=>{ctx.tableAsked=(ctx.tableAsked||0)+1;}};
vm.createContext(ctx);
vm.runInContext(cut('let statusValue=null,','// At a projection:')+'\nthis.api={askStatus,statusDoc,backlogDoc,ownFolder,get lists(){return panelLists;}};',ctx);
const api=ctx.api,settle=()=>new Promise(r=>setTimeout(r,10));
const item=(text,key,mark='')=>({text,key,mark});
const value={word:'STATUS',has_doc:true,path:'/x/2026-11-02_camp/campaign.md',title:'STATUS camp',watch:[]};
// No value yet: neither part.
assert.equal(api.statusDoc(),null);assert.equal(api.backlogDoc(),null);
answers=[{ok:true,status:value,lists:{status:{sign:'S1',items:[item('가','i:1','!'),item('나','i:2'),item('다','i:3'),item('라','i:4')],total:6},
  backlog:{sign:'B1',has_doc:true,items:[item('백','i:b')],total:9}}}];
ctx.askStatus();await settle();
same(asked[0].have,{});                                 // nothing held yet: nothing to send back
let s=api.statusDoc(),b=api.backlogDoc();
same([s.word,s.has_doc,s.items.length,s.total,s.items[0]],['STATUS',true,3,6,{mark:'!',text:'가',key:'i:1'}]);   // three at most
same([b.word,b.has_doc,b.items.length,b.total],['BACKLOG',true,1,9]);
assert.equal(api.ownFolder(),'2026-11-02_camp');
// The next read sends the signs back; "same" keeps what is held, a new sign replaces it.
answers=[{ok:true,status:value,lists:{status:{same:true},backlog:{same:true}}}];ctx.askStatus();await settle();
same(asked[1].have,{status:'S1',backlog:'B1'});
same([api.statusDoc().total,api.backlogDoc().total],[6,9]);
answers=[{ok:true,status:value,lists:{status:{same:true},backlog:{sign:'B2',has_doc:false,items:[],total:0}}}];ctx.askStatus();await settle();
same([api.statusDoc().total,api.backlogDoc().has_doc,api.backlogDoc().items.length],[6,false,0]);
// A read that failed keeps the lists; a value of no artifact root clears them.
answers=[{ok:false}];ctx.askStatus();await settle();assert.equal(api.statusDoc().total,6);
answers=[{ok:true,status:null,lists:null}];ctx.askStatus();await settle();
assert.equal(api.statusDoc(),null);assert.equal(api.backlogDoc(),null);
// A strange answer is cut to what the panel can use: not an array, a total smaller than the items, a total that is no number, items without text.
answers=[{ok:true,status:value,lists:{status:{sign:'S3',items:'x',total:'many'},backlog:{sign:'B3',has_doc:true,items:[item('a','k'),{text:5},null,item('b','k2')],total:1}}}];
ctx.askStatus();await settle();
same([api.statusDoc().items.length,api.statusDoc().total],[0,0]);
same([api.backlogDoc().items.map(i=>i.text),api.backlogDoc().total],[['a','b'],2]);
// The panel model got both values with the read (the drawing reads from it, nothing else).
assert(ctx.panelModel.status_doc&&ctx.panelModel.backlog_doc);
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_an_item_with_lines_under_it_asks_for_them_once_when_it_is_opened(self):
        self.node(r"""(async()=>{
const asked=[];let answer={ok:true,part:'detail',open:'i:x',styles:[{}],rows:[[['    note',0,8]]]};
const ctx={Set,Array,Number,String,Math,Promise,statusList,selection:1,table:null,statusRoutes:()=>[],statusHeld:()=>({}),ownFolder:()=>'2026-11-02_camp',
  POPUP_KIND:{BACKLOG:'backlog'},process,queueDisplay:()=>{},tableParts:r=>statusList.rowsOf(r),
  panelView:{},readAction:(extra)=>{asked.push(JSON.parse(JSON.stringify(extra)));return Promise.resolve(answer);}};
vm.createContext(ctx);
vm.runInContext(cut('function askDetail(t,key){','// PgUp/PgDn on the list screen')+'\nthis.askDetail=askDetail;',ctx);
const entry=(key,lazy)=>({rows:[[{text:'  '},{text:key}]],key,...(lazy?{lazy:true}:{})});
const settle=()=>new Promise(r=>setTimeout(r,10));
const t={kind:'BACKLOG',width:100,listing:[entry('i:x',true),entry('i:y',false)],items:new Set(['i:x','i:y']),more:{},moreAsking:new Set()};
ctx.table=t;
ctx.askDetail(t,'i:y');ctx.askDetail(t,'i:nothere');assert.equal(asked.length,0);              // an item with no lines under it, or none at all: no ask
ctx.askDetail(t,'i:x');ctx.askDetail(t,'i:x');assert.equal(asked.length,1);                      // one ask at a time
await settle();
const a=asked[0];assert.deepEqual([a.kind,a.part,a.open,a.own,a.columns],['backlog','detail','i:x','2026-11-02_camp',96]);
assert.equal(t.more['i:x'].map(r=>r.map(p=>p.text).join('')).join('|'),'    note');
ctx.askDetail(t,'i:x');assert.equal(asked.length,1);                                              // kept: not asked again
// Closed: not asked. An answer that cannot be read leaves nothing under the item; an answer for a list that was read again is dropped.
const u={kind:'BACKLOG',width:100,listing:[entry('i:x',true)],items:new Set(),more:{},moreAsking:new Set()};ctx.table=u;
ctx.askDetail(u,'i:x');assert.equal(asked.length,1);
u.items.add('i:x');answer={ok:false};ctx.askDetail(u,'i:x');await settle();assert.equal(JSON.stringify(u.more['i:x']),'[]');
const w={kind:'BACKLOG',width:100,listing:[entry('i:x',true)],items:new Set(['i:x']),more:{},moreAsking:new Set()};ctx.table=w;
answer={ok:true,part:'detail',styles:[{}],rows:[[['late',0,4]]]};ctx.askDetail(w,'i:x');w.listing=[entry('i:x',true)];await settle();assert.equal(w.more['i:x'],undefined);
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_page_keys_move_the_chosen_line_a_screen_on_and_stay_inside_the_list(self):
        self.node(r"""
const entries=[{rows:[[{text:'head'}]]},...Array.from({length:110},(_,i)=>({rows:[[{text:'  '},{text:'item '+i}]],key:'i:g:'+String(i).padStart(3,'0')})),{rows:[[{text:'  '},{text:'full'}]],key:'full'}];
const ctx={Set,Array,Number,String,Math,statusList,queueDisplay:()=>{}};vm.createContext(ctx);
vm.runInContext(cut('function moveLines(t,delta){','// The document read whole')+'\nthis.moveLines=moveLines;',ctx);
const t={listing:entries,sel:'',fold:new Set(),items:new Set(),reveal:false};
const at=()=>statusList.layout(entries,t).sel;
ctx.moveLines(t,20);assert.equal(at(),'i:g:020');assert.equal(t.reveal,true);                  // the first line stood on is item 0: a screen of 20 on
ctx.moveLines(t,20);assert.equal(at(),'i:g:040');
ctx.moveLines(t,-20);assert.equal(at(),'i:g:020');
for(let i=0;i<10;i++)ctx.moveLines(t,20);assert.equal(at(),'full');                          // the end stays
for(let i=0;i<10;i++)ctx.moveLines(t,-20);assert.equal(at(),'i:g:000');                       // and the top
// An opened item takes its rows: the screen is counted in rows, so the line lands where it should.
const more=[{rows:[[{text:'  '},{text:'a'}]],key:'i:a',more:[[{text:'x'}],[{text:'y'}]]},{rows:[[{text:'  '},{text:'b'}]],key:'i:b'},{rows:[[{text:'  '},{text:'c'}]],key:'i:c'}];
const u={listing:more,sel:'i:a',fold:new Set(),items:new Set(['i:a']),reveal:false};ctx.moveLines(u,4);assert.equal(statusList.layout(more,u).sel,'i:c');ctx.moveLines(u,-1);assert.equal(statusList.layout(more,u).sel,'i:b');
""")

    def test_a_press_on_an_item_opens_the_popup_standing_on_it_and_the_backlog_is_a_listed_kind(self):
        self.node(r"""
const calls=[];
const ctx={Set,Array,Number,String,Math,epoch:0,empty:false,actionLines:[],resetTableInput:()=>{},heldStop:()=>{},block:{focused:false,leave:()=>{},enter:()=>{}},
  table:null,statusPopup:null,statusGeneration:3,cols:100,expanded:false,routePopup:null,cellWidth:t=>[...t].length,panelModel:{},
  panelView:{popupWidth:()=>96,memoryBody:()=>({starts:[]})},tableKind:()=>'table',queueDisplay:()=>calls.push('display'),askTable:()=>calls.push('askTable'),
  blockAction:()=>{},rankedRoutes:()=>[],routeFirst:()=>null,listed:k=>k==='STATUS'||k==='BACKLOG'};
vm.createContext(ctx);
vm.runInContext(cut('function openPopup(name,open','// What a routes popup shows before')+'\nthis.openPopup=openPopup;',ctx);
ctx.openPopup('STATUS','i:abc:def');
assert.equal(ctx.table.kind,'STATUS');assert.equal(ctx.table.sel,'i:abc:def');assert.equal(ctx.table.listing,null);assert(calls.includes('askTable'));
// (with no item pressed the popup stands on its first line, as before)
ctx.openPopup('STATUS','');assert.equal(ctx.table.sel,'');
calls.length=0;ctx.openPopup('BACKLOG','i:1:2');
assert.equal(ctx.table.kind,'BACKLOG');assert.equal(ctx.table.sel,'i:1:2');assert(ctx.table.fold instanceof Set&&ctx.table.items instanceof Set);assert(calls.includes('askTable'));
// What a panel press names parses to that: `popup:STATUS:i:a:b` is the section STATUS and the key `i:a:b`.
const [,name,...rest]='popup:BACKLOG:i:a:b'.split(':');same([name,rest.join(':')],['BACKLOG','i:a:b']);
// The sections the host walks, and the popup kind the backend is asked for.
const sections=src.match(/const SECTIONS=\[([^\]]*)\]/)[1],kinds=src.match(/const POPUP_KIND=\{([^}]*)\}/)[1];
assert.equal(sections,"'STATUS','BACKLOG','NOW','QUESTIONS','ROUTE','MESSAGES','MEM'");assert(kinds.includes("BACKLOG:'backlog'"));
""")


def text_of(row):
    return "".join(part for part, _key in row)


if __name__ == "__main__":
    unittest.main()
