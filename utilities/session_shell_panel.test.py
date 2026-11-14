"""The approved work panel preserves facts, editor receipts and private settings."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import session_shell as shell


class PanelTest(unittest.TestCase):
    def test_mouse_opened_editor_owns_keys_during_peer_delivery(self):
        result = subprocess.run(["node", str(ROOT / "utilities/fixtures/session-shell-delivery-focus.cjs"), str(ROOT)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_route_evidence_pages_remain_readable_without_question_effects(self):
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const lines=['route','why','owner','test','review','CI','gate','result','next'].map(label=>[label,label+' EVIDENCE '.repeat(10),'dim']);
const model={routes:[{title:'Actual route',state:'working',stages:[{name:'inline',state:'active'}],visibility:{summary:'direct',lines}}]};
for(const [width,height] of [[37,21],[80,14]]){
 const parts=[];let pages=1;
 for(let page=0;page<pages;page++){
  const v=view.render({width,height,side:width<80,routeDetail:true,routePage:page,model,questions:()=>{throw Error('detail cannot submit');}});
  pages=v.routePages;parts.push(v.rows.flat().map(p=>p.text).join(''));
  assert(v.rows.length<=height);assert(v.hits.some(h=>h.action==='route-next'));
 }
 assert(pages>1);for(const [label] of lines)assert(parts.join('').includes(label+': '+label));
}
'''
        self.node(script, "panel-view.cjs")

    def test_values_left_unrefreshed_claim_no_route_state(self):
        # Spec rule 12: the host marks values it could not refresh for a while;
        # the route rows stay readable but nothing spins and the route box takes no
        # failure color, so an old state never reads as the current one. The frame
        # never takes a route's state at all (spec rule 25).
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const route=state=>({id:'r',title:'Exact route',state,kind:state,stages:[{name:'inline',st:'active'}],paint:{glyph:{text:'⠏'}}});
const styles={dim:{dim:true},g_dead:{foreground:217,bold:true}};
for(const state of ['working','done','dead']){
 const args=extra=>({width:40,height:24,side:true,model:{routes:[route(state)],route_count:1,styles,...extra},questions:()=>[],configuration:{},frame:3});
 const fresh=view.render(args({})),old=view.render(args({stale:true}));
 assert.equal(fresh.color,'dim');assert.equal(old.color,'dim');
 const box=v=>v.rows.flat().find(p=>p.text==='╭');
 assert.equal(box(fresh).foreground??undefined,state==='dead'?217:undefined,state);assert.equal(box(old).foreground??undefined,undefined,state);
 assert(old.rows.some(r=>r.map(p=>p.text).join('').includes('Exact route')));
 assert(!old.rows.flat().some(p=>p.spin||p.beat&&String(p.text).startsWith('route')));
 if(state==='working')assert(fresh.animated);
}
const asked=view.render({width:40,height:24,side:true,model:{answerable:true,stale:true},questions:()=>[],configuration:{}});
assert.equal(asked.color,'dim');
'''
        self.node(script, "panel-view.cjs")

    def test_frame_is_panel_only_neutral_and_keeps_model_route_question_at_80(self):
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
for(const width of [37,40,80]){
 const model={selected:[{text:'Native selected'},{text:' high'}],session_title:'Preserve the approved task',answerable:true,wait:'◑답 대기 1',route_count:1,
  routes:[{id:'r',title:'Exact route',state:'working',kind:'working',flow:'implement › verify ●',paint:{glyph:{text:'⠏'},harness:{text:'claude'}}}]};
 const v=view.render({width,height:width===80?12:30,side:width<80,model,sid:'exact',configuration:{},expandedKeys:['route:r'],
  questions:()=>[[{text:'Where should this result go?'}],[{text:'1 · Current project'}],[{text:'Ctrl+] answer'}]]});
 const text=v.rows.map(r=>r.map(s=>s.text).join(''));
 assert(text.some(s=>s.includes('Native selected high')));assert(text.some(s=>s.includes('Exact route')));
 assert(text.some(s=>s.includes('Where should')));assert(text.some(s=>s.includes('◑답 대기 1')));
 // An opened route row shows its stages joined by `›`, never `→`.
 if(width<80)assert(text.some(s=>s.includes('implement › verify ●')),text.join('\n'));
 assert(!text.some(s=>s.includes('→')));
 assert(text[0].startsWith('┌'));assert(text.at(-1).endsWith('┘'));
 assert(v.rows.every(r=>r.every(s=>s.background===undefined&&s.reverse!==true)));
 // The frame follows the session's own state (none given here), never the question (spec rule 25).
 assert.equal(v.color,'dim');assert(v.hits.some(h=>h.action==='settings'));
 assert(v.hits.some(h=>h.action==='route:r'));assert(v.hits.some(h=>h.action==='route'));
 assert(text.every(s=>s.length<=width));
}
const direct=view.render({width:40,height:24,side:true,model:{routes:[{title:'One step',state:'idle',stages:[{name:'inline'}]}]},questions:()=>[],configuration:{}});
const text=direct.rows.flat().map(s=>s.text).join('');assert(!text.includes('→'));assert.equal(direct.color,'dim');
const waiting=view.render({width:80,height:12,model:{},state:'blocked',questions:()=>[],configuration:{}});
assert.equal(waiting.color,'g_blocked'); // The frame is the state word's color: waiting, not a route failure.
'''
        self.node(script, "panel-view.cjs")

    def test_waiting_result_line_is_painted_without_the_next_input(self):
        # 2026-11-10: a continue that was not sent stayed unseen until the next input.
        # #1483 review: Claude's projection overwrote it with its native row's
        # segments, so the real projection -> renderer path is what is checked.
        import session_tidy as st
        from unittest import mock
        from fleet import session_panel
        shell.registry_root()
        native = [("⚠ ", "lvl_r"), ("NATIVE ROW", "lvl_r")]
        models = {}
        for number, harness in enumerate(("claude", "codex", "opencode")):
            sid = "00000000-0000-4000-8000-00000000148%d" % number
            seat = st.resolve_seat(harness, env={}, sid=sid)
            project = lambda: shell.panel_projection(harness, sid, columns=40, rows=30)["panel"]
            states = {"none": project()}
            with mock.patch.object(session_panel, "notice_line", return_value=list(native)):
                states["native"] = project()
            st.write_notice(seat, '[정리] 창은 비웠지만 "이어서해"를 넣지 않았습니다 ' + "이유와 다음 할 일. " * 8,
                            author_harness=harness, author_sid=sid, notice_id="panel-" + harness)
            states["new"] = project()
            with mock.patch.object(session_panel, "notice_line", return_value=list(native)):
                states["both"] = project()
            if harness == "claude":
                self.assertTrue(any("NATIVE ROW" in part["text"] for part in states["both"]["notice"]),
                                "Claude's native status row keeps its own segments")
            # This is the main-session hook for the seat above, not the runner's
            # inherited worker or pane environment.
            st.run_hook(harness, "prompt", sid, cwd=str(ROOT), prompt_text="next input", env={})
            states["resolved"] = project()
            models[harness] = states
        script = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]),models=JSON.parse(process.argv[2]);
const qs=['Q: where?','1 here','2 there','› draft','Tab next'];
const paint=(m,width,asking)=>view.render({width,height:width===80?14:24,side:width<80,fill:width<80,focused:asking,configuration:{},
 model:{...m,answerable:asking},questions:o=>asking?qs.slice(0,o.budget).map(text=>[{text}]):[]}).rows.map(r=>r.map(s=>s.text).join(''));
for(const [harness,states] of Object.entries(models))for(const width of [35,40,80])for(const asking of [false,true]){
 const id=harness+'/'+width+'/'+asking,none=paint(states.none,width,asking).join('\n');
 for(const s of ['native','resolved'])assert.equal(paint(states[s],width,asking).join('\n'),none,id+' '+s);
 for(const s of ['new','both']){
  const rows=paint(states[s],width,asking),text=rows.join('\n');
  assert(!text.includes('[object Object]')&&!text.includes('NATIVE ROW'),id+' '+s);
  // An open question comes first: its rows stay and the line waits.
  if(asking){assert.equal(text,none,id+' '+s);continue;}
  const first=rows.findIndex(l=>l.includes('⚠ [정리]'));
  // The notice ends at the next group: a blank row, a section rule or the answer wait.
  const end=rows.findIndex((l,i)=>i>first&&(/^│ +│$/.test(l)||l.includes('── ')||/◑(답| to answer)/.test(l)));
  assert(first>=0&&end>first&&text.includes('이어서해'),id+' '+s+'\n'+text);
  const lines=rows.slice(first,end);
  // Bottom: one row; side: at most three. A cut line says so with an ellipsis.
  assert(lines.length===(width===80?1:3)&&lines.at(-1).includes('…'),id+' '+s+' '+lines.length);
  assert.equal(rows.length,width===80?none.split('\n').length+1:rows.length,id);
 }
}
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"),
                                 json.dumps(models, ensure_ascii=False)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_top_groups_and_quota_stay_while_a_question_takes_the_rows(self):
        # Spec 2026-11-11 rule 6: with too few rows the question and the three top
        # groups stay; messages, opened details and then route rows give way.
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const route=(id,title)=>({id,title,state:'working',kind:'working',elapsed:'42m',flow:'plan ✓ › execute ●',paint:{glyph:{text:'⠏'},harness:{text:'codex'},ctx:{text:'72%'}}});
const model={head:[{text:'[e9] '},{text:'project'},{text:' (topic 🚧 3)'}],selected:[{text:'fixture'},{text:' high'}],
 context:[{text:'context '},{text:'━━━━──────'},{text:' 42%'}],wait:'◑답 대기 1',session_title:'SESSION_TITLE',
 jobs:[{text:'>_'},{text:' running: '},{text:'pytest'}],quota:[{text:'5h 23%'},{text:' │ '},{text:'7d 60%'}],route_count:6,
 routes:Array.from({length:6},(_,i)=>route('r'+i,'ROUTE_'+i)),messages:[{way:'←',who:'[e7]',text:'MESSAGE_ONE',at:1},{way:'→',who:'[73]',text:'MESSAGE_TWO',at:2}]};
const question=[[{text:'Q head'}],[{text:'1 · yes'}],[{text:'2 · no'}],[{text:'Ctrl+] answer'}]];
const paint=(width,height,side,q,open=[])=>view.render({width,height,side,model,sid:'exact',configuration:{},expandedKeys:open,
 questions:o=>q.slice(0,o.budget)}).rows.map(r=>r.map(s=>s.text).join(''));
for(const [width,height,side] of [[40,30,true],[80,20,false]]){
 const idle=paint(width,height,side,[]);
 for(const want of ['project','(topic 🚧 3)','fixture high',side?'CTX    ━━━━':'context ','42%','running: pytest','◑답 대기 1','SESSION_TITLE',side?'╭─ 6 ROUTE':'╭─ route 6','ROUTE_0',side?'│ MESSAGES':'│ messages','MESSAGE_TWO','5h 23% │ 7d 60%'])
  assert(idle.some(s=>s.includes(want)),width+' missing '+want+'\n'+idle.join('\n'));
 // Spec rule 23: every route has a row or is counted in the box's lower border; no `more` row.
 const listed=idle.filter(s=>/ROUTE_\d/.test(s)).length,below=Number((idle.join('\n').match(/ ↓(\d+) ─╯/)||[0,0])[1]);
 assert.equal(listed+below,6,idle.join('\n'));assert(!idle.join('').includes('more'),idle.join('\n'));assert(/^└─+┘$/.test(idle.at(-1)));
 assert(idle.at(-2).includes('7d 60%'),'quota is the last row');
}
for(const [width,side,low] of [[40,true,14],[80,false,10]])for(let height=low;height<=24;height+=2){
 const rows=paint(width,height,side,question,['route:r0']),text=rows.join('\n');
 assert(rows.length<=height,width+'x'+height);
 for(const row of question)assert(text.includes(row[0].text),'question row lost at '+width+'x'+height+'\n'+text);
 for(const want of ['project','fixture high','42%','◑답 대기 1','SESSION_TITLE'])assert(text.includes(want),width+'x'+height+' lost '+want+'\n'+text);
 // 11-11 night final layout: the route side gives way first (its opened detail
 // before its rows), and the messages fixed at the bottom go only last.
  // Spec rules 23, 31: messages fold to one row and fewer (the newest stays) before the
  // route box scrolls; the messages section goes only last.
 if(text.includes('plan ✓ › execute ●'))assert(text.includes('MESSAGE_TWO')&&text.includes('MESSAGE_ONE'),text);
 if(text.includes('ROUTE_0'))assert(text.includes('MESSAGE_TWO'),text);
}
const r=view.render({width:40,height:30,side:true,model,sid:'exact',configuration:{},questions:()=>[]});
assert(!r.hits.some(h=>h.action==='details'));
assert(r.hits.some(h=>h.action.startsWith('message:1:←:')));assert(!r.hits.some(h=>h.action==='fleet'));
'''
        self.node(script, "panel-view.cjs")

    def test_a_chosen_message_reads_to_its_end_and_is_the_one_chosen(self):
        # A token wider than a row runs on by cells instead of ending in `…`, and
        # two messages kept in the same second open their own bodies (RV-PR1517-R2 §2).
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const wide=c=>{const n=c.codePointAt(0);return n>=0x300&&n<=0x36f?0:n>0x2e7f?2:1;},cellWidth=s=>[...s].reduce((n,c)=>n+wide(c),0);
const draw=(messages,width,height,extra={})=>{const r=view.render({width,height,side:width<80,fill:false,model:{messages},cellWidth,questions:()=>[],configuration:{},...extra});
 return {...r,plain:r.rows.map(r=>r.map(p=>p.text).join(''))};};
// Spec rule 31: a message takes a head row and a body row, both open the same message.
const keyOf=(messages,i)=>[...new Set(draw(messages,40,40).hits.filter(h=>h.action.startsWith('message:')).map(h=>h.action))][i];
const read=(messages,key,width,height)=>{const first=draw(messages,width,height,{routeDetail:key}),out=[];
 for(let page=0;page<first.routePages;page++)out.push(...draw(messages,width,height,{routeDetail:key,routePage:page}).plain.slice(1,-1).filter(s=>!/(?:messages|MESSAGES) · |‹ \d+\/\d+ ›|[»▾]   ⚙/.test(s)));return out;};
// A row's text without its frame and indent: a body read across rows, end to end.
const flat=rows=>rows.map(s=>s.slice(1,-1)).join('').replace(/\s/g,'');
const bodies=[['url','https://example.invalid/'+'a'.repeat(290)+'END_1517','END_1517'],['cjk','한글'.repeat(150)+'끝1517','끝1517'],
 ['mixed','see '+'x'.repeat(120)+'한'.repeat(60)+' then END_1517','END_1517'],['marks','é'.repeat(150)+'END_1517','END_1517']];
for(const [name,body,end] of bodies){
 const messages=[{way:'←',who:'[e7]',text:body,at:1}],key=keyOf(messages,0);
 assert(draw(messages,40,30).plain.some(s=>s.includes('…')),name+': the folded preview keeps its ellipsis');
 for(const [width,height] of [[40,30],[80,14],[40,12]]){
  const open=draw(messages,width,height,{expandedKeys:[key]});
  for(const s of open.plain)assert(cellWidth(s)<=width,name+' '+width+'\n'+s);
  assert(!open.plain.some(s=>/[│ ]́/.test(s)),name+': a mark stays with its letter');
  assert(flat(open.plain).includes(end)||open.hits.some(h=>h.action==='read:'+key),name+' '+width+'x'+height+'\n'+open.plain.join('\n'));
  const pages=flat(read(messages,key,width,height));
  assert(pages.includes(body.replace(/\s/g,'')),name+' '+width+'x'+height+' pages lost text\n'+pages);
 }
}
// Same second: each row opens and reads its own body; identical records read the same body.
const one=(who,tag,at=7,way='→')=>({way,who,text:tag+' '+'word '.repeat(40)+tag+'_END',at});
for(const [name,messages,want] of [['same-way',[one('a','FIRST'),one('b','SECOND')],['FIRST_END','SECOND_END']],
 ['other-way',[one('a','FIRST'),one('b','SECOND',7,'←')],['FIRST_END','SECOND_END']],
 ['identical',[one('a','SAME'),one('a','SAME')],['SAME_END','SAME_END']],
 ['a-second-apart',[one('a','FIRST'),one('b','SECOND',8)],['FIRST_END','SECOND_END']]]){
 const keys=[0,1].map(i=>keyOf(messages,i));assert.notEqual(keys[0],keys[1],name);
 keys.forEach((key,i)=>{
  const other=want[1-i],page=read(messages,key,40,12).join('\n');
  assert(page.includes(want[i]),name+' '+i+'\n'+page);if(other!==want[i])assert(!page.includes(other),name+' '+i+'\n'+page);
  const open=draw(messages,40,40,{expandedKeys:[key]}).plain,rows=open.filter(s=>s.includes('_END'));
  assert.equal(rows.length,1,name+' '+i+'\n'+open.join('\n'));
  const link=draw(messages,40,12,{expandedKeys:[key]}).hits.find(h=>h.action.startsWith('read:'));
  if(link)assert.equal(link.action,'read:'+key,name);
 });
}
'''
        self.node(script, "panel-view.cjs")

    def test_a_chosen_message_stays_chosen_when_the_recent_list_moves(self):
        # A newer message pushing the first one out never opens the next one in its place
        # (RV-PR1517-R3 §1 ①): a kept message keeps its key; a message gone opens nothing.
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const one=(tag,at,way='→',who='peer')=>({way,who,at,text:tag+' '+'word '.repeat(40)+tag+'_END'});
const F=one('FIRST',7),S=one('SECOND',7),T=one('THIRD',8),U=one('FOURTH',9),X=one('INSERTED',7),A=one('SAME',7),A2=one('SAME',7);
const draw=(messages,extra={})=>{const r=view.render({width:40,height:40,side:true,fill:false,model:{messages},questions:()=>[],configuration:{},...extra});
 return {...r,plain:r.rows.map(r=>r.map(p=>p.text).join(''))};};
const keys=messages=>[...new Set(draw(messages).hits.filter(h=>h.action.startsWith('message:')).map(h=>h.action))];
const ends=rows=>['FIRST','SECOND','THIRD','FOURTH','INSERTED','SAME'].filter(t=>rows.some(s=>s.includes(t+'_END')));
const page=(messages,key)=>{const first=draw(messages,{routeDetail:key,height:10}),out=[];
 for(let p=0;p<first.routePages;p++)out.push(...draw(messages,{routeDetail:key,routePage:p,height:10}).plain);
 return {opened:first.plain.some(s=>s.includes('MESSAGES · ')),shown:ends(out),detail:first.routeDetail};};
for(const [name,before,after] of [['front-out',[F,S,T],[S,T,U]],['insert',[F,T,U],[X,F,T]],['identical',[A,A2,T],[A2,T,U]],['none',[F,S,T],[F,S,T]]]){
 const k=keys(before);
 before.forEach((m,i)=>{
  const want=m.text.split(' ')[0],stays=after.includes(m),p=page(after,k[i]),inline=ends(draw(after,{expandedKeys:[k[i]]}).plain);
  assert.equal(p.opened,stays,name+' '+i);
  if(stays){assert.deepEqual(p.shown,[want],name+' '+i);assert.deepEqual(inline,[want],name+' '+i);assert.equal(p.detail,k[i]);}
  else{assert.deepEqual(inline,[],name+' '+i);assert.equal(p.detail,false,name+' '+i);}
  if(stays)assert.equal(keys(after)[after.indexOf(m)],k[i],name+' '+i);
 });
}
// The panel head's `route N` is whole or left out at any width, never `route 1` for 12;
// beside the native area (spec rule 45 (a)) `N ROUTE`, never the count without its word.
for(const n of [1,12,123])for(let width=12;width<=40;width++)for(const side of [true,false]){
 const rows=view.render({width,height:12,side,model:{routes:[{id:'a',title:'ROW',state:'working',kind:'working',paint:{}}],route_total:n},questions:()=>[],configuration:{}}).rows.map(r=>r.map(p=>p.text).join(''));
 if(side)for(const row of rows){
  assert(!/\broute\b/.test(row),width+' '+row);
  for(const hit of row.matchAll(/─ (\d*) ?(R(?:O|OU|OUT|OUTE)?)?\b/g))if(hit[1]||hit[2])assert.equal(hit[1]+' '+hit[2],n+' ROUTE',width+' '+row);
 }
 else for(const row of rows)for(const hit of row.matchAll(/\br(?:o|ou|out|oute)?\b( \d+)?/g))assert.equal(hit[0],'route '+n,width+' '+row);
}
'''
        self.node(script, "panel-view.cjs")

    def test_shortening_keeps_the_observed_count_message_and_mcp(self):
        # Only the rows shown shrink: the whole route count, a message's recorded
        # end and OpenCode's MCP count stay readable (review RV-PR1517 §1).
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const route=(id,title)=>({id,title,state:'working',kind:'working',elapsed:'31m',paint:{glyph:{text:'⠏'},harness:{text:'codex'},ctx:{text:'37%'}}});
const base={head:[{text:'[52] project'}],selected:[{text:'fixture'},{text:' high'}],context:[{text:'context '},{text:'37%'}],
 jobs:[{text:'>_ running: JOB'}],quota:[{text:'5h 12%'}],session_title:'TITLE'};
const paint=(model,width,height,extra={})=>{const r=view.render({width,height,side:width<80,model,sid:'s',configuration:{},questions:()=>[],...extra});
 return {...r,plain:r.rows.map(r=>r.map(p=>p.text).join(''))};};
// Spec rule 23: twelve current routes all have rows; the head counts all twelve
// and nothing folds into `+N more` or opens Fleet.
const twelve={...base,routes:Array.from({length:12},(_,i)=>route('r'+i,'ROUTE_'+i)),route_count:12,route_total:12};
let r=paint(twelve,40,40);assert(r.plain.some(s=>s.includes('12 ROUTE')),r.plain.join('\n'));
assert(r.plain.some(s=>s.includes('ROUTE_11'))&&!r.plain.join('').includes('more'),r.plain.join('\n'));
// Fewer rows: the rows shown plus the border counts (`↑N` above, `↓N` below) are always the whole count.
const six={...base,routes:Array.from({length:6},(_,i)=>route('r'+i,'ROUTE_'+i)),route_count:6,route_total:6};
const hidden=(plain,arrow)=>Number((plain.join('\n').match(new RegExp(' '+arrow+'(\\d+) ─[╮╯]'))||[0,0])[1]);
for(let height=10;height<=22;height++){
 for(const model of [six,twelve])for(const scroll of [0,3,99]){
  r=paint(model,40,height,{routeScroll:scroll});const shown=new Set(r.hits.filter(h=>h.action.startsWith('route:')).map(h=>h.action)).size;
  assert(!r.plain.join('').includes('more')&&!r.hits.some(h=>h.action==='fleet'),height+'\n'+r.plain.join('\n'));
  if(r.routeArea){
   assert.equal(shown+hidden(r.plain,'↑')+hidden(r.plain,'↓'),model.route_total,height+'\n'+r.plain.join('\n'));
   assert.equal(hidden(r.plain,'↑'),r.routeArea.offset);assert(r.routeArea.offset<=r.routeArea.max);
  }
 }
}
// A producer total above the rows it sent is counted in the lower border.
r=paint({...six,route_total:9},40,40);assert(r.plain.some(s=>/ ↓3 ─╯/.test(s)),r.plain.join('\n'));
// While the box scrolls, a working route comes before waiting, stopped and finished ones.
const mixed={...base,route_count:4,routes:[{...route('d','DONE_R'),kind:'done'},{...route('x','DEAD_R'),kind:'dead'},{...route('b','WAIT_R'),kind:'blocked'},route('w','WORK_R')]};
for(let height=10;height<=16;height++){r=paint(mixed,40,height);if(r.routeArea?.max){const first=r.plain.find(s=>/_R/.test(s));assert(first.includes('WORK_R'),height+'\n'+r.plain.join('\n'));}}
// An opened message reads to its recorded end where rows allow, else counts the rest and reads on.
const long='START '+'word '.repeat(60)+'END_1517';
const msg={...base,routes:[],route_count:0,messages:[{way:'←',who:'[e7]',text:long,at:1}]};
const key=paint(msg,40,40).hits.find(h=>h.action.startsWith('message:')).action;
for(const [width,height] of [[40,30],[80,24]]){
 r=paint(msg,width,height,{expandedKeys:[key]});assert(r.plain.join('\n').includes('END_1517'),width+'\n'+r.plain.join('\n'));
}
r=paint(msg,40,14,{expandedKeys:[key]});const text=r.plain.join('\n');
assert(!text.includes('END_1517'));const rest=text.match(/\+(\d+) lines ›/);assert(rest,text);
assert(r.hits.some(h=>h.action==='read:'+key));
// The pages read the whole message from its start.
let pages=paint(msg,40,9,{routeDetail:key}),seen=[];
for(let page=0;page<pages.routePages;page++)seen.push(...paint(msg,40,9,{routeDetail:key,routePage:page}).plain);
assert(pages.routePages>1);assert(seen.join('\n').includes('START'));assert(seen.join('\n').includes('END_1517'));
assert(pages.plain.some(s=>s.includes('MESSAGES · ')));
// A message no longer kept falls back to the ordinary panel.
assert(!paint(msg,40,14,{routeDetail:'message:9:→'}).plain.some(s=>/messages · /i.test(s)));
// The repo's open work row and, on the evidence pages, the route owner and summary stay readable.
const work={...six,work:[{text:'◌ '},{text:'3 open · WORK_HEADLINE'}]};
for(const width of [40,80])assert(paint(work,width,30).plain.some(s=>s.includes('3 open · WORK_HEADLINE')),String(width));
const owned={...six,routes:[{...route('r0','ROUTE_0'),owner:'owner · codex (gpt·high)',visibility:{summary:'SUMMARY_TEXT',lines:[]}}]};
const detail=paint(owned,40,30,{routeDetail:true}).plain.join('\n');
assert(detail.includes('owner codex (gpt·high)')&&detail.includes('SUMMARY_TEXT'),detail);
// OpenCode's MCP count stays with the model and context at every height.
const mcp={...six,mcp:[{text:'MCP '},{text:'2/3'}]};
for(let height=10;height<=22;height++){
 for(const width of [40,80])assert(paint(mcp,width,height).plain.some(s=>s.includes('MCP 2/3')),width+'x'+height);
}
'''
        self.node(script, "panel-view.cjs")

    def test_the_natives_own_model_name_takes_the_route_rows_color_rule(self):
        # The head's MODEL row shows the model name read off the native's own screen when it
        # differs from Fleet's; it takes the route rows' color rule (its family's color, else the
        # harness's: `_model_key(model, harness=...)`), keyed by the backend so the panel keeps no
        # family table; until the key for a new name arrives, the harness's color. Effort as before.
        sys.path.insert(0, str(ROOT / "tools"))
        from unittest import mock
        import human_wait_state
        from fleet.palette import style_components
        from fleet.session_line import _model_key
        shell.registry_root()
        # (The answer-wait cache stays untouched: later projections read it as they found it.)
        quiet = mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, ""))
        quiet.start(); self.addCleanup(quiet.stop)
        for harness in ("claude", "codex", "opencode"):
            for name in ("claude-opus-5-5", "glm-5.2", "gpt-5.5"):
                part = shell.panel_projection(harness, "", columns=40, rows=30, native_model=name)["panel"]["native_model_part"]
                colors = 256 if "256color" in os.environ.get("TERM", "") or os.environ.get("COLORTERM") else 8
                want = style_components(_model_key(name, harness=harness), colors=colors, no_color="NO_COLOR" in os.environ)
                self.assertEqual({key: part[key] for key in want}, want, (harness, name))
                self.assertEqual(part["text"], name)
            self.assertNotIn("native_model_part", shell.panel_projection(harness, "", columns=40, rows=30, native_model="bad\x1bname")["panel"])
            self.assertNotIn("native_model_part", shell.panel_projection(harness, "", columns=40, rows=30)["panel"])
        self.assertEqual(_model_key("glm-5.2", harness="opencode"), "hb_opencode")
        script = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const styles={dim:{dim:true},loc_repo:{foreground:253},hb_claude:{foreground:116},hb_opencode:{foreground:110}};
const row=(harness,native,extra={})=>view.render({width:40,height:24,side:true,harness,native,model:{selected:[{text:'Fleet model'}],model_value:'fleet-model',styles,...extra},
 configuration:{},questions:()=>[]}).rows.find(r=>r.some(p=>p.text==='MODEL  '));
let r=row('opencode',{model:'glm-5.2',effort:'max'},{native_model_part:{text:'glm-5.2',foreground:110}});
assert.equal(r.find(p=>p.text==='glm-5.2').foreground,110);assert.equal(r.find(p=>p.text===' max').foreground??null,null);
r=row('claude',{model:'claude-opus-5-5'},{native_model_part:{text:'claude-opus-5-5',foreground:116,bold:false}});
assert.equal(r.find(p=>p.text==='claude-opus-5-5').foreground,116);
// A new name before its key arrives: the harness's color, never the plain white of before.
r=row('opencode',{model:'kimi-k3'},{native_model_part:{text:'glm-5.2',foreground:110}});
assert.equal(r.find(p=>p.text==='kimi-k3').foreground,110);assert.notEqual(r.find(p=>p.text==='kimi-k3').foreground,253);
"""
        self.node(script, "panel-view.cjs")

    def test_the_projection_carries_the_natives_own_turn_record_and_its_path(self):
        # Spec rule 26 and the 11-12 rule 6: Claude's REPL writes its own UI state into its
        # session record (busy / waiting / idle); the projection reads it with the session
        # (same exact process and session as the pane reader) and names the record, which
        # the host reads itself the moment its watched file changes. Codex: its rollout's
        # latest turn event. Display only.
        from unittest import mock
        import human_wait_state
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.collectors import codex
        shell.registry_root()
        sid = "00000000-0000-4000-8000-000000001112"
        me = os.getpid()
        start = open("/proc/self/stat").read().rsplit(") ", 1)[1].split()[19]
        # This process's own folder is the checkout: its experiment board is not this test's to read.
        from fleet import session_lab
        no_lab = mock.patch.object(session_lab, "read", return_value=(None, []))
        quiet = mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, ""))
        with tempfile.TemporaryDirectory() as home, quiet, no_lab, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            record = Path(home) / "sessions" / f"{me}.json"
            record.parent.mkdir()
            project = lambda: shell.panel_projection("claude", sid, native_pid=me, native_start=start, columns=40, rows=30)
            # Review R1: `waiting` is an answer wait only for the person's own reasons; a menu
            # or settings dialog (`dialog open`) and an unlisted or missing reason say nothing.
            for status, why, turn in (("busy", None, "working"), ("waiting", "permission prompt", "blocked"),
                                      ("waiting", "input needed", "blocked"), ("waiting", "dialog open", None),
                                      ("waiting", "a reason not listed", None), ("waiting", None, None),
                                      ("idle", None, "idle"), ("shell", None, None)):
                record.write_text(json.dumps({"pid": me, "procStart": start, "sessionId": sid, "status": status,
                                              **({"waitingFor": why} if why else {})}))
                result = project()
                self.assertEqual(result.get("native_turn"), turn, (status, why))
                self.assertEqual(result.get("turn_record"), str(record))
                self.assertIn(str(record), result["watch"])
            for other in ({"sessionId": "another"}, {"procStart": "1"}):
                record.write_text(json.dumps({"pid": me, "procStart": start, "sessionId": sid, "status": "busy", **other}))
                self.assertNotIn("native_turn", project(), other)
        with quiet, no_lab, mock.patch.object(codex, "exact_rollout_for_session_id", return_value=__file__), \
                mock.patch.object(codex, "_tail_token_count", return_value=None):
            for event, turn in (("task_started", "working"), ("task_complete", "idle"), ("turn_aborted", "idle")):
                with mock.patch.object(codex, "_latest_task_lifecycle", return_value=(event, "turn")):
                    result = shell.panel_projection("codex", sid, native_pid=me, native_start=start, columns=40, rows=30)
                self.assertEqual(result.get("native_turn"), turn, event)
                self.assertNotIn("turn_record", result)

    def test_presentation_keeps_the_producers_whole_route_count_and_mcp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet import session_panel
        rows = [{"id": "rt-" + str(i), "title": "ROUTE", "state": "working", "marker": "working"} for i in range(8)]
        values = {"harness": "opencode", "route_label": "route 12 ⠏12", "mcp": {"total": 3, "connected": 2, "failed": 1}}
        panel = session_panel.presentation(values, {"route_panels": {"opencode\0s": rows}}, "opencode", "s", 40)
        self.assertEqual((panel["route_count"], panel["route_total"]), (8, 12))
        self.assertEqual("".join(text for text, _key in panel["mcp"]), "MCP 2/3")
        # Spec rule 23: every current route reaches the panel, beyond the former eight rows.
        many = rows + [{"id": "rt-x" + str(i), "title": "ROUTE", "state": "working", "marker": "working"} for i in range(7)]
        panel = session_panel.presentation({"harness": "codex", "route_label": "route 15 ⠏15"}, {"route_panels": {"codex\0s": many}}, "codex", "s", 40)
        self.assertEqual((len(panel["routes"]), panel["route_count"], panel["route_total"]), (15, 15, 15))
        panel = session_panel.presentation({"harness": "codex"}, {"route_panels": {"codex\0s": rows[:2]}}, "codex", "s", 40)
        self.assertEqual((panel["route_count"], panel["route_total"], panel["mcp"]), (2, None, []))
        # A label an earlier release left in the shared cache still gives its whole count,
        # and is drawn without the hourglass it carried.
        from fleet import session_line
        old = {"harness": "claude", "route_label": "⏳ route 12 ⠏12"}
        panel = session_panel.presentation(old, {"route_panels": {"claude\0s": rows}}, "claude", "s", 40)
        self.assertEqual((panel["route_count"], panel["route_total"], panel["route_label"]), (8, 12, "route 12 ⠏12"))
        self.assertEqual(session_line.route_badge("⏳ route 12 ⠏12", rows, [("working", False)] * 12), "route 12 ⠏12")
        self.assertNotIn("⏳", "".join(text for row in session_line.status_lines(old, 80) for text, _key in row))

    def test_place_tells_the_boxes_apart_and_the_border_carries_the_state(self):
        # Spec rules 14-17 and 19 (2026-11-11 night final layout, draft C6).
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const p=t=>({text:t}),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const route=i=>({id:'r'+i,title:'ROUTE_'+i,state:'working',kind:'working',elapsed:'42m',paint:{glyph:p('⠏'),harness:p('codex'),ctx:p('72%')}});
const model={head:[p('[e9] '),p('project'),{text:' (topic 🚧 3)',dim:true}],selected:[p('fixture high')],context:[p('context 42%')],
 quota:[p('5h 23% │ 7d 60%')],route_count:3,routes:[0,1,2].map(route),styles:{dim:{dim:true},lvl_y:{foreground:186},g_spin:{foreground:230},g_work_off:{foreground:150,dim:true},g_blocked:{foreground:217},g_dead:{foreground:217,bold:true}},
 session_title:'SESSION_TITLE',now:'NOW_LINE',messages:[1,2,3].map(at=>({way:'←',who:'[e7]',text:'MESSAGE_'+at,at}))};
const question=[[p('◑ Q_HEAD')],[p('Q_BODY')],[p('› A')],[p('2 B')],[p('↑↓ choose · Enter send · t text')]];
const paint=(o={})=>view.render({width:40,height:o.height||30,side:true,sid:'s',harness:'claude',configuration:o.cfg||{},model:{...model,...o.model},state:o.state,frame:o.frame,since:o.since,
 settings:o.settings,questions:q=>(o.q||[]).slice(0,q.budget)});
const text=v=>v.rows.map(r=>r.map(x=>x.text).join(''));
// Spec rule 29 (a): Fleet's session-row head on the left of the top border (glyph,
// number, harness); no state word; HEARTING only in settings. Spec rule 49 (a): no gear on the
// border; it stands at the right end of the last row inside, the fold button `»` before it.
for(const [state,want] of [['idle','┌ ● [e9] claude ─'],['working','┌ ⠙ [e9] claude ─'],['blocked','┌ ◑ [e9] claude ─'],['unknown','┌ [e9] claude ─'],[undefined,'┌ [e9] claude ─']]){
 const rows=text(paint({state,frame:1,model:{tag:'[e9]'}}));assert(rows[0].startsWith(want),state+' '+rows[0]);
 assert(!/running|idle|waiting/.test(rows[0]),rows[0]);
 assert(rows[0].endsWith('─┐')&&!rows[0].includes('⚙')&&!rows.join('').includes('Settings')&&!rows.join('').includes('HEARTING'),rows[0]);
 assert(rows.at(-2).endsWith('»   ⚙ │'),rows.at(-2));
}
// Spec rule 35 (a): a steward's `⚑` alone blinks in Fleet's steward pink, lit and dark on
// the shared beat; its number keeps the lit steward color on both beats (Fleet's session row).
for(const [frame,key] of [[0,'tag_steward'],[10,'tag_steward_dim']]){
 const v=paint({state:'idle',frame,model:{tag:'[e9]',steward:true,styles:{...model.styles,tag_steward:{foreground:219,bold:true},tag_steward_dim:{foreground:219,dim:true}}}});
 const flag=v.rows[0].find(x=>x.text==='⚑'),number=v.rows[0].find(x=>x.text==='e9');
 assert(flag.beat&&flag.foreground===219&&(key==='tag_steward'?flag.bold:flag.dim),frame+' '+JSON.stringify(flag));
 assert(!number.beat&&number.foreground===219&&number.bold&&!number.dim,frame+' '+JSON.stringify(number));
 assert(v.animated,'an idle steward still keeps the beat');
}
// Narrow: the steward mark goes first, then the harness, then the number; the glyph stays.
for(const [width,want] of [[20,'┌ ● [e9] claude ⚑ ─'],[19,'┌ ● [e9] claude ─'],[17,'┌ ● [e9] ─']]){
 const row=text(view.render({width,height:12,side:true,sid:'s',harness:'claude',configuration:{},model:{tag:'[e9]',steward:true},state:'idle',questions:()=>[]}))[0];
 assert(row.startsWith(want),width+' '+row);
}
const settings=paint({settings:{index:0,value:view.defaults}});
assert(!paint({state:'idle',model:{routes:[],route_count:0}}).animated,'an idle panel with nothing to blink keeps no beat');assert(text(settings)[0].startsWith('┌ HEARTING ─'),text(settings)[0]);
const hits=paint({state:'idle'}).hits,gear=hits.find(h=>h.action==='settings'),fold=hits.find(h=>h.action==='fold');
assert(gear.row===28&&gear.left<=37&&gear.right>=37&&fold.row===28&&fold.left<=33&&fold.right>=33&&fold.right<gear.left-1,JSON.stringify([gear,fold]));
// The number takes the same color as its glyph (Fleet's state key); the harness its Fleet hue.
const top=paint({state:'idle',model:{tag:'[e9]',styles:{...model.styles,hb_claude:{foreground:116}}}}).rows[0];
assert.equal(top.find(x=>x.text==='●').foreground,150);
assert.equal(top.find(x=>x.text==='e9').foreground,150);assert.equal(top.find(x=>x.text==='claude').foreground,116);
// Spec rule 25: the whole frame takes that word's color, running and waiting their
// session keys, idle and an unknown state the neutral line, in the same paint. Standing still
// (rule motion off) it keeps that color; moving (spec rule 47) a running frame is the neutral
// line with an arc whose head is that color, and a waiting one starts its breath at it.
for(const [state,want] of [['working',230],['submitting',230],['blocked',217],['idle',undefined],['unknown',undefined]]){
 const v=paint({state,cfg:{rule_motion:'off'}}),frame=v.rows.flatMap(r=>[r[0],r.at(-1)]);
 assert(frame.every(x=>(x.foreground??undefined)===want&&(want===undefined?x.dim===true:x.dim!==true)),state+' '+JSON.stringify(frame[0]));
 assert(!v.rows.flat().some(x=>x.text==='╭'&&x.foreground===217),state+' no route failure here');
 const moving=paint({state,frame:0,since:0}).rows.flatMap(r=>[r[0],r.at(-1)]),lines=paint({state,frame:0,since:0}).rows.flat().filter(x=>['frame','frame-state','orbit'].includes(x.role)&&/^[─┌┐└┘│]+$/.test(x.text));
 if(want===217)assert(lines.every(x=>x.foreground===217&&x.dim!==true),state+' '+JSON.stringify(moving[0]));
 else if(want)assert(lines.every(x=>x.role==='frame'&&x.dim===true||x.role==='orbit'&&view.FACES.dark.orbit.includes(x.foreground))&&lines.some(x=>x.foreground===want),state);
 else assert.deepEqual(moving,frame,state);
}
// A failed route turns only its own box line to the failure color; the frame stays the session's.
const failed=paint({state:'idle',model:{routes:[route(0),{...route(1),state:'dead',kind:'dead'}]}});
const box=failed.rows.flat().filter(x=>/^[╭╮╰╯]$/.test(x.text));
assert(box.length===4&&box.every(x=>x.foreground===217&&x.bold===false),JSON.stringify(box));
assert(failed.rows[0][0].foreground==null&&failed.rows[0][0].dim===true);
// Repo row, then Git on its own row; the route box high; messages (no box: two rules) fixed at the bottom.
// (32 rows: every structural row of spec rule 35 stands with three routes in their full form.)
let rows=text(paint({state:'idle',height:32}));
assert(rows[1].includes('project')&&!rows[1].includes('[e9]')&&!rows[1].includes('topic')&&rows[2].includes('(topic 🚧 3)'),rows.join('\n'));
// Spec rule 29 (b), 30 (a), 35 (b): the metadata block is two groups parted by one empty row
// ([repository, Git] / [model, context]), then a thin rule, then the session's content with
// its labels; an empty row parts the head from the route box.
assert(/^│ +│$/.test(rows[3])&&rows[4].includes('fixture high')&&rows[5].startsWith('│ CTX    42%')&&/^│ ─+ │$/.test(rows[6]),rows.join('\n'));
assert(rows[7].startsWith('│ TITLE  SESSION_TITLE')&&rows[8].startsWith('│ NOW    NOW_LINE')&&/^│ +│$/.test(rows[9])&&rows[10].includes('╭─ 3 ROUTE'),rows.join('\n'));
const r=rows.findIndex(s=>s.includes('╭─ 3 ROUTE ')),m=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(r>0&&m>r&&rows.slice(r,m).filter(s=>s.includes('ROUTE_')).length===3,rows.join('\n'));
// Spec rules 24, 38: beside the native area a folded route reads who (the glyph with its
// harness), then its context and time two cells in, then its title two cells in.
const t0=rows.findIndex(s=>s.includes('ROUTE_0'));
assert(/│ │ \S codex +│ │/.test(rows[t0-2])&&/│ │   72% +42m │ │/.test(rows[t0-1])&&rows[t0].includes('│ │   ROUTE_0'),rows.join('\n'));
// Spec rule 31: each message takes a head row and a body row (three messages: six rows).
assert(rows.at(-2).includes('7d 60%')&&/^│ ─+ │$/.test(rows.at(-3))&&m===rows.length-10&&!rows.join('').includes('╭─ messages'),rows.join('\n'));
assert(rows[m+1].includes('← [e7]')&&rows[m+2].includes('│   MESSAGE_1'),rows.join('\n'));
for(const s of rows)assert.equal(cells(s),40,s);
// A question box sits right above the messages in the answer-needed color; messages are dim, route plain.
const asked=paint({state:'working',q:question,model:{answerable:true}});rows=text(asked);
const qTop=rows.findIndex(s=>s.includes('─ ◑ Q_HEAD ')),mq=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
// Spec rule 34 (b): short of rows the messages fold to one row each before a route gives up its second row.
assert(qTop>r&&rows[mq-1].includes('╰')&&mq===rows.length-7&&rows.at(-2).includes('7d 60%')&&rows.includes('│ │   ROUTE_0                        │ │'),rows.join('\n'));
const border=(i,ch)=>asked.rows[i].find(x=>x.text.startsWith(ch));
const ra=rows.findIndex(s=>s.includes('╭─ 3 ROUTE '));
assert.equal(border(qTop,'╭').foreground,174);assert.equal(asked.rows[mq].find(x=>x.text.includes('MESSAGES')).dim,true);assert.notEqual(border(ra,'╭').dim,true);
for(const s of rows)assert.equal(cells(s),40,s);
// Short of rows (spec rule 23): two rows fold to one and messages fold to one row
// before the route box scrolls in place; messages go last.
const short=(h,q=question)=>text(view.render({width:40,height:h,side:true,sid:'s',configuration:{},model:{...model,answerable:true},state:'idle',questions:o=>q.slice(0,o.budget)}));
for(const q of [[],question])for(let h=12;h<=30;h++){
 const rows=short(h,q),all=rows.join('\n'),two=rows.some(s=>s.includes('│   ROUTE_'));
 // Spec rules 23, 31: fewer messages show from the oldest (their label counts them, spec rule
 // 58), before the route box scrolls; the newest message goes only with the section, last.
 const hidden=['MESSAGE_1','MESSAGE_2'].filter(m=>!all.includes(m)).length;
 if(all.includes('MESSAGE_3')&&hidden)assert(new RegExp('│ MESSAGES \\+'+hidden+' ').test(all),h+'\n'+all);
 if(all.includes('MESSAGE_1')&&all.includes('ROUTE_0'))assert(rows.filter(s=>s.includes('ROUTE_')).length===3&&!/[↑↓]\d/.test(all),h+'\n'+all);
 if(/[↑↓]\d/.test(all))assert(!two&&!all.includes('MESSAGE_2')&&!all.includes('MESSAGE_1'),h+'\n'+all);
 if(!all.includes('MESSAGE_3'))assert(!all.includes('╭─ 3 ROUTE'),h+' messages go last\n'+all);
}
// Every box is closed, whatever the height; an empty route list or message list draws no box.
for(let h=10;h<=30;h++)for(const q of [[],question]){const rows=text(view.render({width:40,height:h,side:true,sid:'s',configuration:{},model,state:'idle',questions:o=>q.slice(0,o.budget)}));
 assert.equal(rows.filter(s=>s.includes('╭')).length,rows.filter(s=>s.includes('╰')).length,h+'\n'+rows.join('\n'));}
rows=text(paint({model:{routes:[],route_count:0,routes_known:true,messages:[]}}));assert(!rows.join('').includes('╭'),rows.join('\n'));
// The groups a paint shows decide Claude's statusline rows: a long Git reading cut to the
// row still counts as painted; a group dropped for rows does not.
const longGit={...model,head:[p('[e9] '),p('project')],git:[{text:'(shell-box-git-1111-longer-name 🚧 12 ↑3 ↓1)',dim:true}],wait:'◑답 대기 1'};
for(const [w,side] of [[32,true],[40,true],[80,false]]){
 const v=view.render({width:w,height:30,side,sid:'s',configuration:{},model:longGit,state:'idle',questions:()=>[]});
 assert(text(v).some(s=>s.includes('(shell-box')&&(!side||s.includes('…'))),text(v).join('\n'));
 for(const g of ['head','git','context','quota','wait','route'])assert(v.groups.includes(g),w+' '+g+' '+v.groups);
}
const tight=view.render({width:40,height:12,side:true,sid:'s',configuration:{},model:{...longGit,answerable:true},state:'idle',questions:o=>question.slice(0,o.budget)});
assert(!tight.groups.includes('git')&&!text(tight).some(s=>s.includes('(shell-box')),tight.groups+'\n'+text(tight).join('\n'));
assert(!view.render({width:40,height:30,side:true,sid:'s',configuration:{},model,settings:{index:0,value:view.defaults},questions:()=>[]}).groups.length,'settings paint no group');
// An opened message folded with the rest keeps its first row and its read row, so its page stays reachable.
const long={way:'←',who:'[e7]',text:'LONG_BODY '+'word '.repeat(60),at:9},opened=[...model.messages.slice(1),long];
const keyOf=view.render({width:40,height:30,side:true,sid:'s',configuration:{},model:{...model,messages:opened},questions:()=>[]}).hits.find(h=>h.action.startsWith('message:')&&h.row>0&&true&&h.action.includes(':9:')).action;
for(const [w,h] of [[40,14],[40,16],[40,18],[40,19],[40,20],[40,21],[80,10],[80,12],[80,14]]){const v=view.render({width:w,height:h,side:w<80,sid:'s',configuration:{},model:{...model,messages:opened},expandedKeys:[keyOf],state:'idle',questions:()=>[]});
 const rows=text(v);if(!rows.some(s=>s.includes('LONG_BODY')))continue;
 assert(rows.some(s=>/\+\d+ lines ›/.test(s))&&v.hits.some(x=>x.action==='read:'+keyOf),w+'x'+h+'\n'+rows.join('\n'));}
'''
        self.node(script, "panel-view.cjs")

    def test_an_opened_route_shows_its_doers_now_and_last_finished_call(self):
        # Fleet's unfolded activity rows: the open call with its summary and age (whole),
        # or that no call is open, and the call it just finished on one row cut with `…`.
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const after='Edit · utilities/session-shell/'+'deep/'.repeat(20)+'host.cjs';
const route={id:'r',title:'PR 맞추기',state:'working',kind:'working',elapsed:'47m',tool:'Bash',
 target:'python3 tools/run-tests.py',tool_age:'3m',after_tool:after,paint:{glyph:{text:'⠏'},harness:{text:'claude'}}};
const paint=(r,width,side)=>view.render({width,height:30,side,model:{routes:[r],route_count:1,messages:[]},sid:'s',
 configuration:{},expandedKeys:['route:r'],questions:()=>[]}).rows.map(x=>x.map(p=>p.text).join(''));
for(const [width,side,call] of [[40,true,'>_ Bash · run-tests.py 3m'],[80,false,'>_ Bash · python3 tools/run-tests.py 3m']]){
 const rows=paint(route,width,side);
 // Spec rule 34 (a): short of cells the path gives way to its file name before the call folds.
 assert(rows.some(s=>s.includes(call)),rows.join('\n'));
 const line=rows.filter(x=>x.includes('after Edit'));
 assert.equal(line.length,1,rows.join('\n'));assert(line[0].includes('…'),line[0]);
}
const idle=paint({...route,tool:undefined,target:undefined,tool_age:undefined,idle_tool:true,after_tool:undefined},40,true);
assert(idle.some(x=>x.includes('>_ no tool')),idle.join('\n'));
'''
        self.node(script, "panel-view.cjs")

    def test_a_folded_one_step_route_adds_its_tool_only_where_the_row_has_room(self):
        # Supervisor 2026-11-12: the first row of a folded route may add Fleet's `>_ tool`
        # for a one-step route. Spec E9 rules 36 (a), 38: context and time take the row
        # below; short of cells effort goes, then the tool, then the harness.
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const P=(c)=>({glyph:{text:'⠋'},harness:{text:'claude'},model_text:{text:'Opus 5.5'},ctx:{text:c}});
const one={id:'a',title:'one step',state:'working',kind:'working',elapsed:'47m',tool:'python3',target:'tools/run-tests.py',paint:P('24%')};
const staged={...one,id:'b',title:'staged',flow:'plan ✓ › execute ● › review',stages:[{name:'plan'},{name:'execute'},{name:'review'}]};
const paint=(width)=>view.render({width,height:30,side:true,model:{routes:[one,staged],route_count:2,messages:[]},sid:'s',
 configuration:{},expandedKeys:[],questions:()=>[]}).rows.map(x=>x.map(p=>p.text).join(''));
const narrow=paint(34),wide=paint(40);
const at=wide.findIndex(x=>/⠋ claude Opus 5\.5 · >_ python3 +│/.test(x));
assert(at>0&&/│ │   24% +47m │ │/.test(wide[at+1])&&wide[at+2].includes('one step'),wide.join('\n'));
assert.equal(wide.filter(x=>x.includes('>_')).length,1,wide.join('\n'));
assert(!wide.some(x=>x.includes('tools/run-tests.py')),wide.join('\n'));
// A kind row that does not carry the tool (only a one-shot route's does) leaves it on the first row.
const kinded=view.render({width:40,height:30,side:true,model:{routes:[{...one,kind_word:'code'}],route_count:1,messages:[]},sid:'s',
 configuration:{},expandedKeys:[],questions:()=>[]}).rows.map(x=>x.map(p=>p.text).join(''));
const ki=kinded.findIndex(x=>/⠋ claude Opus 5\.5 · >_ python3 +│/.test(x));
assert(ki>0&&kinded[ki+3].includes('│   code '),kinded.join('\n'));
// Narrower, the tool goes before the harness; context and time stay on their own row.
assert(narrow.some(x=>/⠋ claude Opus 5\.5 +│/.test(x))&&!narrow.some(x=>x.includes('>_'))&&narrow.some(x=>/│   24% +47m │/.test(x)),narrow.join('\n'));
'''
        self.node(script, "panel-view.cjs")

    def test_route_rows_follow_the_route_line_setting_and_open_in_place(self):
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const p=t=>({text:t});
const model={route_count:2,routes:[
 {id:'a',title:'셸 표기 통일',state:'working',kind:'working',elapsed:'42m',flow:'plan ✓ › execute ● › review',tool:'apply_patch',agents:1,
  paint:{glyph:p('⠏'),harness:p('codex'),model_text:p('gpt-6.1-sol'),effort:p('high'),ctx:p('72%'),gauge:[p('━━━━━━━'),p('───'),p(' '),p('72%')]}},
 {id:'b',title:'제목만 있는 route 이름이 아주 길어서 잘림',state:'dead',kind:'dead',elapsed:'1h 27m',paint:{glyph:p('✕'),harness:p('claude')}}],
 messages:[{way:'←',who:'[e7]',text:'#1506 병합 가능 — 추가 필수 수정 없음, CI 15/15. 검토서는 inputs/RV-PR1506-R2-1111.md',at:5}]};
const paint=(cfg,open=[],width=40,height=30)=>view.render({width,height,side:width<80,model,configuration:cfg,expandedKeys:open,questions:()=>[]}).rows.map(r=>r.map(s=>s.text).join(''));
const time=paint({}),byModel=paint({route_line:'model'});
const row=(rows,title)=>rows.find(s=>s.includes(title.slice(0,5)));
const at=(rows,title)=>rows.findIndex(s=>s.includes(title.slice(0,5)));
// Spec rules 24, 38: who (glyph, harness, model, effort), then context and time two cells
// in, then the title; the `route line` choice does not touch this full form.
for(const rows of [time,byModel]){const i=at(rows,'셸 표기');
 assert(/⠋ codex gpt-6\.1-sol high +│ │/.test(rows[i-2])&&/│ │   72% +42m │ │/.test(rows[i-1])&&!rows[i].includes('codex')&&rows[i].includes('│   셸 표기'),rows.join('\n'));}
// Spec rule 30 (b): percent and time stand in their own columns, the same for every route.
const ctxRows=time.filter(s=>/│ │   (72%)? +(42m|1h 27m) │ │/.test(s));
assert(ctxRows.length===2&&ctxRows.every(s=>s.indexOf(' │ │')===ctxRows[0].indexOf(' │ │')),ctxRows.join('\n'));
// Short of rows the context row folds into the first row (percent and time at the right,
// right-aligned in their columns), effort going first. (13 rows: spec rule 49 (b)'s last row stays.)
const low=paint({},[],40,13),lowRow=low[at(low,'셸 표기')-1];
assert(/⠋ codex gpt-6\.1-sol +72% +42m │/.test(lowRow)&&lowRow.indexOf('72%')+3===lowRow.indexOf('    42m'),low.join('\n'));
assert(low.some(s=>/✕ claude +1h 27m │/.test(s)),low.join('\n'));
// Narrower, spec rule 36 (a): the harness goes before the model; then the `route line`
// choice keeps its value (time: elapsed, model: context) and the other goes, whole.
const narrowTime=paint({},[],28,13),narrowModel=paint({route_line:'model'},[],28,13);
const lead=rows=>rows[at(rows,'셸 표기')-1];
assert(/⠋ gpt-6\.1-sol +42m │/.test(lead(narrowTime))&&!lead(narrowTime).includes('codex')&&!lead(narrowTime).includes('72%'),narrowTime.join('\n'));
assert(/⠋ gpt-6\.1-sol +72% │/.test(lead(narrowModel))&&!lead(narrowModel).includes('42m'),narrowModel.join('\n'));
// Spec rule 29 (c), 36 (c): a long title goes on to its second row (two cells in) instead
// of `…`; who and the time keep their rows.
const long=time.findIndex(s=>s.includes('제목만'));
assert(!time[long].includes('…')&&time[long+1].includes('│ │   ')&&time.slice(long,long+2).join('').includes('잘림')&&time[long-2].includes('claude')&&time[long-1].includes('1h 27m'),time.join('\n'));
// Every row of a folded route opens it in place.
const hitRows=view.render({width:40,height:30,side:true,model,configuration:{},questions:()=>[]}).hits.filter(h=>h.action==='route:a').map(h=>h.row);
assert.equal(hitRows.length,3);assert.deepEqual(hitRows,[hitRows[0],hitRows[0]+1,hitRows[0]+2]);
// Unreported context is left out, never `?`.
assert(!time.join('\n').includes('?'));
const opened=paint({},['route:a']),detail=opened.slice(opened.findIndex(s=>s.includes('셸 표기'))+1,opened.findIndex(s=>s.includes('제목만')));
// Spec rule 32: the doer's model and context are the folded first row's; the detail hangs behind its guide.
assert(opened.some(s=>s.includes('gpt-6.1-sol')&&s.includes('72%'))&&detail.slice(0,-2).every(s=>/┆|│ │ +│ │/.test(s)),detail.join('\n'));
assert(detail.some(s=>s.includes('plan ✓ › execute ● › review')));assert(detail.some(s=>s.includes('>_ apply_patch · +1 agent')));
const openedModel=paint({route_line:'model'},['route:a']),openedWide=paint({route_line:'model'},['route:a'],48);
// Spec rule 34 (a): the doer row lets its effort, then its harness go before it would fold.
assert(openedModel.some(s=>s.includes('┆ DOER   gpt-6.1-sol · 72%'))&&openedModel.some(s=>s.includes('42m')),openedModel.join('\n'));
assert(openedWide.some(s=>s.includes('┆ DOER   codex gpt-6.1-sol high · 72%')),openedWide.join('\n'));
// A message row opens to its whole retained text and closes again.
const messageKey=view.render({width:40,height:30,side:true,model,configuration:{},questions:()=>[]}).hits.find(h=>h.action.startsWith('message:')).action;
const message=paint({},[messageKey]);assert(message.join('').includes('RV-PR1506-R2-1111.md'));
// Folded, a message keeps its head and at most three body rows (spec rule 31), fewer when rows run short.
const folded=paint({}),head=folded.findIndex(s=>s.includes('← [e7]'));
assert(head>0&&folded.slice(head+1).filter(s=>/^│   \S/.test(s)).length<=3,folded.join('\n'));
assert(!view.render({width:40,height:16,side:true,model,configuration:{},questions:()=>[]}).rows.map(r=>r.map(x=>x.text).join('')).join('').includes('RV-PR1506-R2-1111.md'));
// Every row keeps the panel width with two-cell Hangul and emoji.
for(const rows of [time,byModel,opened,message,paint({},['route:a'],80)])for(const s of rows)assert([...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)<=80);
'''
        self.node(script, "panel-view.cjs")

    def test_a_route_folded_to_one_row_keeps_each_value_whole(self):
        # Review RV-PR1528 §2: 32 cells, the boxed one-row route cut `72%` to `7…`.
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const P=t=>({text:t}),route=i=>({id:'r'+i,title:'ROUTE_'+i+' 한글🙂',kind:'working',state:'working',elapsed:'1h 27m',
 paint:{glyph:P('⠏'),harness:P('codex'),model_text:P('gpt-6-sol'),ctx:P('72%'),gauge5:[P('━━━── 72%')]}});
const model={head:[P('[52] project')],git:[P('(long-branch-'+'x'.repeat(50)+' 🚧 3)')],selected:[P('fixture high')],context:[P('context 42%')],
 quota:[P('5h 12% │ 7d 66%')],route_count:3,route_total:3,routes:[1,2,3].map(route),messages:[1,2,3].map(at=>({at,way:'←',who:'[peer]',text:'MESSAGE_'+at}))};
let oneRow=0;
for(const width of [32,40])for(let height=10;height<=30;height++)for(const route_line of ['time','model'])for(const asking of [false,true]){
 const v=view.render({width,height,side:true,state:'idle',model:{...model,answerable:asking},configuration:{route_line},
  questions:o=>asking?[[P('Q')],[P('Question body')],[P('1 YES')],[P('2 NO')]].slice(0,o.budget):[]});
 const rows=v.rows.map(r=>r.map(p=>p.text).join(''));
 for(const hit of v.hits.filter(h=>h.action?.startsWith('route:'))){
  const s=rows[hit.row],label=width+'x'+height+' '+route_line+' '+asking+'\n'+rows.join('\n');
  if(s.includes('codex'))oneRow+=s.includes('ROUTE')?1:0;
  // A value is whole or absent: no cut number, harness, model or time.
  assert(!/7…|\d…|codex…|gpt[^ ]*…|1h…/.test(s),label);
  for(const m of s.matchAll(/72\S*/g))assert.equal(m[0],'72%',label);
 }
}
assert(oneRow>0,'the probe reaches the one-row form');
'''
        self.node(script, "panel-view.cjs")

    def test_an_opened_route_carries_what_fleet_unfolds_and_runs_on_the_shared_beat(self):
        # Spec rule 21: the kind and owner line, each stage with its doer, how it was run
        # and checked, the next step and why it stopped; whole values, read on a page when long.
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const p=t=>({text:t}),styles={g_work:{foreground:150,bold:true},g_work_off:{foreground:150,dim:true},g_idle:{foreground:186}};
const routes=[{id:'a',title:'ROUTE_A',kind:'working',elapsed:'42m',tool:'apply_patch',agents:1,
  owner:'owner · claude (Opus 5.5·high)',kind_text:'autopilot-code(dev·std)',profile:'balanced-deep',owner_ctx:'31%',age:'42m',legs:2,
  after:'before this leg: frame ✓ — outside this snapshot',
  stages:[{name:'plan',mark:'✓',verdict:'PASS',harness:'codex',model:'gpt-fixture',effort:'high',elapsed:'12m',when:'done 22:34',unit:'plan/code',gate:true},
   {name:'execute',mark:'●',current:true,round:2,harness:'codex',model:'gpt-fixture',effort:'high',elapsed:'30m',unit:'dev/backend',note:'NOTE_R2'},
   {name:'review',mark:'',verdict:'pending',unit:'review/code'}],
  workers:[{stage:'execute',harness:'codex',model:'gpt-fixture',ctx:'72%'},{stage:'execute',harness:'claude',model:'Sonnet 5.5',ctx:'12%'}],
  visibility:{summary:'staged · test ✓',next:'resume · execute',lines:[]},paint:{glyph:p('⠏'),harness:p('codex'),ctx:p('72%')}},
 {id:'b',title:'ROUTE_B',kind:'dead',reason:'worker claude · failed 3m',resume:'execute',stages:[{name:'execute',mark:'✕'}],paint:{glyph:p('✕'),harness:p('claude')}}];
const model={routes,route_count:2,styles};
const paint=(extra={})=>view.render({width:40,height:60,side:true,model,configuration:{},expandedKeys:['route:a','route:b'],questions:()=>[],...extra});
const rows=v=>v.rows.map(r=>r.map(s=>s.text).join(''));
// Spec rule 32: label rows and one row per stage in columns, behind a guide; every value kept.
const wide=rows(paint({width:70})),all=wide.join('\n'),flat=all.replace(/[│┆ \n]+/g,' ');
for(const word of ['OWNER claude (Opus 5.5·high) · 31% · balanced-deep','KIND autopilot-code(dev·std)','✓ plan PASS gpt-fixture 12m',
 '● execute R2 gpt-fixture 72% 30m','>_ apply_patch · +1 agent','NOTE NOTE_R2','└ 2 claude Sonnet 5.5 12%','· review INFO',
 'INFO 42m · 2 legs · staged · test ✓ · before this leg: frame ✓ — outside this snapshot · plan done 22:34 plan/code gate ✓ · execute dev/backend · review review/code',
 'NEXT resume · execute','✕ execute','WHY worker claude · failed 3m','RESUME execute'])
 assert(flat.includes(word),word+'\n'+all);
// One stage, one row: its columns stand one above the other (the time right-aligned).
const stageRows=wide.filter(s=>/┆ [✓●·] (plan|execute|review)/.test(s));
assert.equal(stageRows.length,3,all);assert.equal(new Set(stageRows.map(s=>s.indexOf('┆'))).size,1);
assert.equal(stageRows[0].indexOf('12m')+3,stageRows[1].indexOf('30m')+3,all);
// Narrow: whole columns go (context, then model) and no stage row folds.
const narrow=rows(paint());
for(const s of narrow)assert.equal([...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0),40,s);
assert(narrow.some(s=>/┆ ✓ plan +PASS +12m/.test(s))&&!narrow.join('').includes('gpt-fixture  '),narrow.join('\n'));
// An opened route's detail is parted from the next route by one empty row.
const gap=narrow.findIndex(s=>s.includes('┆ RESUME')||s.includes('┆ NEXT'));
assert(narrow.slice(gap+1).some(s=>/^│ │ +│ │$/.test(s)),narrow.join('\n'));
// Short of rows the detail keeps its first rows and a `+N lines ›` row that opens the route page.
const short=view.render({width:40,height:22,side:true,model,configuration:{},expandedKeys:['route:a'],questions:()=>[]});
assert(rows(short).some(s=>/\+\d+ lines ›/.test(s)),rows(short).join('\n'));
const more=short.hits.find(h=>h.action==='route'&&rows(short)[h.row]&&/lines ›/.test(rows(short)[h.row]));assert(more,'a press opens the route page');
const page=rows(view.render({width:40,height:80,side:true,model,configuration:{},routeDetail:true,questions:()=>[]})).join('\n').replace(/[│ \n]+/g,' ');
for(const word of ['owner claude (Opus 5.5·high)','dev/backend','NOTE_R2','resume execute'])assert(page.includes(word),word+'\n'+page);
// The page reads the owner's raw output (Fleet's attempts) only for its own route.
const attempts=[{route:'a',att:'att-owner',lines:['RAW_FIRST','RAW_LAST']},{route:'other',att:'x',lines:['RAW_OTHER']}];
const rawPage=rows(view.render({width:40,height:120,side:true,model,configuration:{},routeDetail:true,attempts,questions:()=>[]})).join('\n').replace(/[│ \n]+/g,' ');
for(const word of ['owner raw · attempt-view att-owner RAW_FIRST RAW_LAST'])assert(rawPage.includes(word),word+'\n'+rawPage);
assert(!rawPage.includes('RAW_OTHER'),rawPage);assert(!page.includes('owner raw'),page);
// One beat (Fleet's 100 ms frame and one-second blink): the working route's mark spins on the
// frame; the running stage and the open route count blink between g_work and g_work_off.
const at=frame=>paint({frame,state:'working'});
const a=at(0),b=at(3),c=at(13);
const mark=v=>v.rows.flat().find(x=>x.text.length===1&&'⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'.includes(x.text)&&x.role==='state:working'&&v.rows[0].indexOf(x)<0);
assert.notEqual(mark(a).text,mark(b).text);assert.equal(mark(a).text,view.SPIN[0]);assert.equal(mark(b).text,view.SPIN[3]);
const count=v=>v.rows.flat().find(x=>x.text==='2 ROUTE'),dot=v=>v.rows.flat().find(x=>x.text==='●'&&x.foreground===150);
assert.equal(count(a).bold,true);assert.equal(count(c).dim,true);assert.equal(dot(a).bold,true);assert.equal(dot(c).dim,true);
assert(a.animated&&a.spins.includes(0)&&a.spins.length>1,JSON.stringify(a.spins));
// Nothing runs: no beat, nothing to turn between native frames.
const idle=view.render({width:40,height:30,side:true,model:{routes:[{...routes[1]}],route_count:1,styles},state:'idle',configuration:{},questions:()=>[]});
assert.equal(idle.animated,false);assert.deepEqual(idle.spins,[]);
assert.equal(idle.rows.flat().find(x=>x.text==='1 ROUTE').foreground,186);
'''
        self.node(script, "panel-view.cjs")

    def test_session_status_leaves_out_absent_values_and_painted_fields(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.session_panel import session_status
        text = lambda rows: "\n".join("".join(t for t, _k in row) for row in rows)
        values = {"harness": "claude", "tag": "[23]", "state": "idle", "title": "Title", "branch": "topic",
                  "git": {"repo": 1, "operation": "none", "ahead": 2, "behind": 0, "extra_worktrees": 0,
                          "observation": "fresh", "observed_label": "21:52"},
                  "model": "fixture", "effort": "high", "ctx_pct": 42, "quota_windows": [("5h", 23, None)],
                  "route_label": "⏳ 1 route", "answer_wait": 1, "jobs": "pytest"}
        painted = text(session_status(values, 36))
        for want in ("(topic", "↑2", "running: pytest"):
            self.assertIn(want, painted)
        for already in ("fixture", "context", "5h", "route", "◑", "⎇", "21:52"):
            self.assertNotIn(already, painted)
        self.assertNotIn("?", text(session_status({"harness": "claude", "state": "idle"}, 36)))
        self.assertEqual(session_status({"harness": "claude", "state": "idle"}, 36), [])
        # Sessions are told apart by number and title; the panel paints no Codex thread ID.
        sid = "00000000-0000-4000-8000-000000000052"
        codex = text(session_status({**values, "harness": "codex", "native_sid": sid}, 36))
        self.assertIn("(topic", codex)
        self.assertNotIn(sid, codex)
        self.assertNotIn(sid[:8], codex)

    def test_title_and_jobs_stay_on_screen_while_a_question_takes_the_session_rows(self):
        # Review [52] missing-field probe, on the 2026-11-11 layout: a question at
        # 80x14, its focused editor, and OpenCode's 35-column sidebar keep the
        # session title and running jobs on screen. Spec rule 15 (2026-11-11 night):
        # the branch has its own row below the repo row and is the first to go.
        sys.path.insert(0, str(ROOT / "tools"))
        from fleet.session_panel import presentation
        values = {"harness": "claude", "tag": "[cb]", "state": "idle", "title": "REVIEW1463_TITLE",
                  "branch": "values-fixture*", "cwd": "/x/project", "model": "fixture", "effort": "high",
                  "ctx_pct": 15, "jobs": "REVIEW1463_JOB", "answer_wait": 1,
                  "git": {"repo": 1, "operation": "none", "ahead": 2, "behind": 1, "extra_worktrees": 3,
                          "observation": "fresh", "observed_label": "21:56:43"}}
        plain = lambda rows: [{"text": text} for text, _key in rows]
        models = {}
        for name, extra in (("short", {}), ("long", {"title": "REVIEW1463_TITLE_" + "A" * 80})):
            panel = presentation({**values, **extra}, {}, "claude", "sid", 80)
            models[name] = {**{key: plain(panel[key]) for key in ("head", "git", "selected", "context", "jobs", "quota")},
                            "wait": panel["wait"], "session_title": panel["session_title"]}
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]),models=JSON.parse(process.argv[2]);
const question=['◑ 질문 1 · 1/1 · 답변 필요','REVIEW1463: 결과를 어디에 둘까요?','1 현재 프로젝트 (추천)','2 다른 폴더','Ctrl+] · 답변 필요','more','more 2'].map(text=>[{text}]);
const routes=[{id:'r',title:'REVIEW1463_ROUTE',state:'blocked',kind:'blocked',paint:{glyph:{text:'◑'}}}];
const paint=(width,height,side,focused,model,n=question.length)=>view.render({width,height,side,fill:false,focused,sid:'s',configuration:{},
 model:{...model,answerable:true,routes,route_count:1},questions:o=>question.slice(0,Math.min(n,o.budget))})
 .rows.map(r=>r.map(p=>p.text).join(''));
for(const [width,height,side,focused,n] of [[80,14,false,false],[80,14,false,true],[80,14,false,true,5],[35,21,true,false],[35,26,true,false],[80,10,false,false]]){
 const rows=paint(width,height,side,focused,models.short,n);
 for(const want of ['REVIEW1463_TITLE','REVIEW1463_JOB',models.short.wait,'◑ 질문 1'])
  assert(rows.some(s=>s.includes(want)),`${width}x${height} focused=${focused} lost ${want}\n`+rows.join('\n'));
 const branch=rows.findIndex(s=>s.includes('(values-fix'));
 if(width===35&&height===26)assert(branch>=0,'a roomy side panel keeps the Git row\n'+rows.join('\n'));
 // The Git row sits right below the repo row and never shares it.
 // Spec rule 29: the number moves to the top border; the repo row keeps `project/`.
 if(branch>=0)assert(rows[branch-1].includes('project/')&&!rows.slice(1).some(s=>s.includes('[cb]')),rows.join('\n'));
 assert(rows.length<=height);
}
// A long title is shortened with an ellipsis; the answer wait keeps its place.
const rows=paint(80,14,false,false,models.long);
assert(rows.some(s=>s.includes(models.long.wait)&&s.includes('REVIEW1463_TITLE_AAA')&&s.includes('…')),rows.join('\n'));
assert(/^└─.*─┘$/.test(rows.at(-1))&&[...rows.at(-1)].length===80,rows.at(-1));
'''
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs"), json.dumps(models)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_route_line_setting_saves_through_the_existing_path_and_falls_back_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory}
            path = shell.policy_path(env)
            path.parent.mkdir()
            path.write_text(json.dumps({"enabled": True, "route_line": "sideways"}))
            path.chmod(0o600)
            self.assertTrue(shell.policy(env)[0])
            self.assertEqual(shell.display_policy(env)["route_line"], "time")
            path.write_text(json.dumps({"enabled": True}))
            desired = {**shell.DISPLAY_DEFAULTS, "route_line": "model"}
            self.assertEqual(shell.save_display_policy(desired, env)["route_line"], "model")
            self.assertEqual(shell.display_policy(env)["route_line"], "model")
            with self.assertRaises(ValueError):
                shell.save_display_policy({**desired, "route_line": "both"}, env)
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
const i=v.fields.indexOf('route_line');assert(i>=0);assert.equal(v.defaults.route_line,'time');
assert.equal(v.change(v.defaults,'route_line',1).route_line,'model');
const r=v.render({width:40,height:24,side:true,model:{labels:{route_line:'route line'}},settings:{index:i,value:{...v.defaults}},questions:()=>[]});
assert(r.rows.flat().map(p=>p.text).join('').includes('route line'));assert(r.hits.some(h=>h.action==='setting:'+i+':up'));
'''
        self.node(script, "panel-view.cjs")

    def test_rule_motion_setting_saves_through_the_existing_path_and_falls_back_alone(self):
        # Spec E9 rule 47 (e): `rule motion` (on by default, or off), saved like `route line`; an
        # unknown value falls back to `on` alone and never disables the shell.
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory}
            path = shell.policy_path(env)
            path.parent.mkdir()
            path.write_text(json.dumps({"enabled": True, "rule_motion": "sometimes", "route_line": "model"}))
            path.chmod(0o600)
            self.assertTrue(shell.policy(env)[0])
            self.assertEqual((shell.display_policy(env)["rule_motion"], shell.display_policy(env)["route_line"]), ("on", "model"))
            path.write_text(json.dumps({"enabled": True}))
            self.assertEqual(shell.DISPLAY_DEFAULTS["rule_motion"], "on")
            desired = {**shell.DISPLAY_DEFAULTS, "rule_motion": "off"}
            self.assertEqual(shell.save_display_policy(desired, env)["rule_motion"], "off")
            self.assertEqual(shell.display_policy(env)["rule_motion"], "off")
            self.assertIs(json.loads(path.read_text())["enabled"], True)
            with self.assertRaises(ValueError):
                shell.save_display_policy({**desired, "rule_motion": "fast"}, env)

    def test_sent_messages_come_from_this_sessions_own_ledger_shard(self):
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": directory}):
            sid = "00000000-0000-4000-8000-0000000000e9"
            shard = shell._peer_message()._ledger_path(sid)
            shard.parent.mkdir(parents=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            rows = [{"ts": stamp, "from": {"session_id": sid}, "to": {"harness": "codex", "session_id": "x", "name": "n"},
                     "kind": "note", "summary": "SENT_" + str(n), "message_id": "m" + str(n),
                     "delivery": {"status": "unknown"}} for n in range(10)]
            rows.append({"ts": stamp, "from": {"session_id": "other"}, "to": {}, "kind": "note", "summary": "FOREIGN"})
            # A later record of the same message updates its delivery state (spec E9 rule 31).
            rows.append({**rows[9], "delivery": {"status": "received"}})
            shard.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot json\n")
            sent, watched = shell._sent_peer_messages(sid)
            self.assertEqual([row["summary"] for row in sent], ["SENT_" + str(n) for n in range(2, 10)])
            self.assertEqual((sent[-1]["kind"], sent[-1]["status"], sent[0]["status"]), ("note", "received", "unknown"))
            self.assertEqual(watched, [str(shard)])
            self.assertIs(shell._sent_peer_messages(sid)[0], sent)  # unchanged file: no reparse
            self.assertEqual(shell._sent_peer_messages("absent-session"), ([], []))

    def test_requests_come_from_the_request_records_not_the_recent_message_tail(self):
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": directory}):
            import peer_request
            mine = {"harness": "claude", "session_id": "00000000-0000-4000-8000-0000000000e9", "name": "e9"}
            theirs = {"harness": "codex", "session_id": "00000000-0000-4000-8000-0000000000e7", "name": "e7"}
            request_id = peer_request.reserve(mine, theirs, title="검토 요청")
            peer_request.commit_open(request_id, "a" * 32, label="판1")
            views, watched = shell._request_views("claude", mine["session_id"], time.time())
            self.assertEqual([(v["side"], v["record"]["id"]) for v in views], [("out", request_id)])
            received, _ = shell._request_views("codex", theirs["session_id"], time.time())
            self.assertEqual([(v["side"], v["record"]["id"]) for v in received], [("in", request_id)])
            # The files whose change repaints the panel: the request index and the record.
            self.assertIn(str(peer_request.index_path()), watched)
            self.assertTrue(any(path.endswith(request_id + ".json") for path in watched))
            # No sent-message shard exists, yet the open request is listed.
            self.assertEqual(shell._sent_peer_messages(mine["session_id"]), ([], []))
            # A session with no requests shows nothing and still watches the index, so its
            # first request repaints the panel.
            self.assertEqual(shell._request_views("claude", "absent-session", time.time()),
                             ([], [str(peer_request.index_path())]))

    def test_z_a_requests_transfers_stand_in_the_request_line_not_in_the_message_lists(self):
        # R29: one line of work is counted once. The request, its report and its cancel notice are
        # shown by the route box's request line; ordinary messages stay in MESSAGES.
        import time
        from unittest import mock
        import peer_request
        shell.registry_root()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": directory}):
            for harness in ("claude", "codex", "opencode"):
                sid = "00000000-0000-4000-8000-0000000000e9"
                other = "00000000-0000-4000-8000-0000000000e7"
                peer = shell._peer_message()
                shard = peer._ledger_path(sid)
                shard.parent.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                record = lambda text, refs, n: {"ts": stamp, "from": {"session_id": sid},  # noqa: E731
                    "to": {"harness": harness, "session_id": other, "name": "n"}, "kind": "steer",
                    "summary": text, "message_id": "m%d" % n, "delivery": {"status": "queued"}, "refs": refs}
                rows = [record("ORDINARY_NOTE", [], 1),
                        record("[요청 rq-aaaaaa] REQUEST_LINE", ["peer-request:rq-aaaaaa:1:request"], 2),
                        record("[요청 취소 rq-aaaaaa] CANCEL_LINE", ["peer-request:rq-aaaaaa:1:cancel"], 3)]
                shard.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                shell._SENT_CACHE.clear()
                sealed = {"schema": peer_request.REF_SCHEMA, "id": "rq-bbbbbb", "revision": 1, "role": "report"}
                received = [{"source": "peer", "text": "RECEIVED_REPORT", "ref": "b" * 32, "at": time.time(),
                             "sender": "n", "sender_harness": harness, "sender_session_id": other},
                            {"source": "peer", "text": "RECEIVED_ORDINARY", "ref": "c" * 32, "at": time.time(),
                             "sender": "n", "sender_harness": harness, "sender_session_id": other}]
                transfers = {"b" * 32: {"request": sealed}, "c" * 32: {}}
                import session_tidy
                with mock.patch.object(session_tidy, "prompt_display", return_value={"received": received}), \
                        mock.patch.object(peer, "_read_transfer_record", side_effect=lambda ref, **k: transfers[ref]):
                    messages = shell.panel_projection(harness, sid, columns=60, rows=30)["panel"]["messages"]
                texts = [row["text"] for row in messages]
                self.assertEqual(sorted(texts), ["ORDINARY_NOTE", "RECEIVED_ORDINARY"], harness)

    def test_z_a_failed_request_read_is_kept_beside_the_panel_not_swallowed(self):
        from unittest import mock
        import peer_request
        errors = []
        with mock.patch.object(peer_request, "session_requests", side_effect=ValueError("peer-request-directory-symlink")):
            self.assertEqual(shell._request_views("claude", "a-session", 0, errors), ([], []))
        self.assertEqual(errors, ["ValueError: peer-request-directory-symlink"])

    def test_z_every_harness_lists_its_requests_in_the_panel(self):
        import tempfile as _t
        from unittest import mock
        import peer_request
        shell.registry_root()
        with _t.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": directory}):
            for harness in ("claude", "codex", "opencode"):
                mine = {"harness": harness, "session_id": "00000000-0000-4000-8000-0000000000a%d" % len(harness), "name": "a1"}
                theirs = {"harness": harness, "session_id": "00000000-0000-4000-8000-0000000000b%d" % len(harness), "name": "b1"}
                request_id = peer_request.reserve(mine, theirs, title="검토 제목")
                peer_request.commit_open(request_id, "a" * 32, label="판1")
                panel = shell.panel_projection(harness, mine["session_id"], columns=40, rows=30)["panel"]
                self.assertEqual([row["id"] for row in panel["requests"]], [request_id], harness)
                self.assertNotIn("request_error", panel)

    def test_native_model_is_observed_without_session_question_binding(self):
        script = r'''
const assert=require('node:assert/strict'),d=require(process.argv[1]);
const id='00000000-0000-4000-8000-000000000052';
let v=d.observe('codex',['  '+id+' · work · main · Context 42% used · gpt-6 high · 5h: 90% left · Weekly 80% left',' ? for shortcuts'],'');
assert.equal(v.model,'gpt-6');assert.equal(v.effort,'high');assert.equal(v.percent,42);
assert.equal(d.observe('codex',['Someone said model: made-up high'],'exact'),null);
v=d.observe('codex',['  main · Context 0% used · gpt-6 high','  ? for shortcuts'],'');
assert.equal(v.model,'gpt-6');assert.equal(v.effort,'high');assert.equal(v.sid,undefined);
assert.equal(d.observe('codex',['  main · gpt-6 high','normal conversation output'],''),null);
v=d.observe('claude',['📁 repo · ⎇ main · context ━━━━────── 42% · Opus high · 5h 10% · 7d 20%'],'');
assert.equal(v.model,'Opus');assert.equal(v.effort,'high');
'''
        self.node(script, "native-display.cjs")

    def test_parallel_stages_never_take_each_others_tool_and_the_beat_follows_the_painted_rows(self):
        # PR #1532 R2: the feed has one tool and sub-agent count for the route (its
        # doer), so it stands once on its own row as Fleet draws it, never on every
        # running stage. The beat runs only while a spinning or blinking part is painted.
        script = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const p=t=>({text:t});
const route={id:'a',title:'ROUTE_A',kind:'working',tool:'apply_patch',agents:1,kind_text:'code(dev·std)',
 stages:[{name:'execute',mark:'●',current:true,harness:'codex',model:'gpt-fixture',effort:'high'},
  {name:'review',mark:'●',current:true,harness:'claude',model:'claude-sonnet-5-5',effort:'med'}],
 workers:[{stage:'execute',harness:'codex',model:'gpt-fixture',ctx:'72%'},{stage:'review',harness:'claude',model:'Sonnet 5.5',ctx:'12%'}],
 paint:{glyph:p('⠏')}};
const rows=v=>v.rows.map(r=>r.map(s=>s.text).join(''));
const all=rows(view.render({width:80,height:40,side:false,model:{routes:[route],route_count:1},state:'idle',expandedKeys:['route:a'],questions:()=>[]}));
const stageRows=all.filter(s=>/[●] (execute|review)/.test(s));
assert.equal(stageRows.length,2,all.join('\n'));
for(const s of stageRows)assert(!s.includes('apply_patch')&&!s.includes('agent'),s);
assert.equal(all.filter(s=>s.includes('>_ apply_patch · +1 agent')).length,1,all.join('\n'));
// A working route the height (a question) dropped from the panel: nothing turns.
const q=[[p('Q_HEAD')],[p('BODY')],[p('1 YES')],[p('2 NO')],[p('choose')],[p('back')]];
for(const [width,height,side] of [[40,12,true],[80,12,false]]){
 const r=view.render({width,height,side,state:'idle',model:{routes:[route],route_count:1},questions:o=>q.slice(0,o.budget)});
 assert(!rows(r).join('\n').includes(side?'1 ROUTE':'route 1'),rows(r).join('\n'));
 assert.deepEqual(r.spins,[]);assert.equal(r.animated,false,width+'x'+height);
}
// The same route with room to show it: its mark spins and the beat runs.
const shown=view.render({width:40,height:30,side:true,state:'idle',model:{routes:[route],route_count:1},questions:o=>q.slice(0,o.budget)});
assert(rows(shown).join('\n').includes('1 ROUTE'));assert.equal(shown.animated,true);assert(shown.spins.length>0);
"""
        self.node(script, "panel-view.cjs")

    def test_lines_take_their_role_color_on_a_known_face_without_a_shade(self):
        # Spec rule 22: frame 244/243, route 245/243, messages 242/245, question 174/131 on a
        # known face whether or not it is shaded; the body and background stay the terminal's.
        script = r"""
const assert=require('node:assert/strict'),v=require(process.argv[1]);
const rows=[[{text:'body',foreground:253},{text:'frame',role:'frame',foreground:186},{text:'route',role:'route'},
 {text:'messages',role:'messages',dim:true},{text:'q',role:'accent-line'}]];
const want={dark:[244,245,242,174],light:[243,243,245,131]};
for(const background of ['dark','light'])for(const theme of ['terminal','light']){
 const face=theme==='light'?'light':background,out=v.appearance(rows,{theme,shade:'none'},{colors:256,background})[0];
 assert.deepEqual(out.slice(1).map(x=>x.foreground),want[face],background+'/'+theme);
 assert(out.every(x=>x.background===undefined&&!x.dim),background+'/'+theme);
}
// An unknown face keeps the terminal's own lines; mono keeps no hue; NO_COLOR returns the rows.
const unknown=v.appearance(rows,{theme:'terminal',shade:'none'},{colors:256,background:''})[0];
assert.deepEqual(unknown.slice(1,4).map(x=>x.foreground),[186,null,null]);
assert(v.appearance(rows,{theme:'mono',shade:'none'},{colors:256,background:'dark'})[0].slice(1,4).every(x=>x.foreground===null));
assert.equal(v.appearance(rows,{shade:'none'},{noColor:true,background:'dark'}),rows);
"""
        self.node(script, "panel-view.cjs")

    # Spec E9 rule 34: the narrow and short panel's finish. One fixture, the routes the
    # supervisor's screens drew: a one-shot route on a tool, a staged route on its third of
    # seven stages, a route waiting between stages and a failed one.
    FIXTURE = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const P=t=>({text:t}),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const seven=at=>['plan','plan-check','execute','impl-review','test','report','close'].map((name,i)=>i<at
 ?{name,mark:'✓',verdict:'PASS',harness:'claude',model:'Opus 5.5',elapsed:['12m','4m'][i]}
 :i===at?{name,mark:'●',current:true,harness:'claude',model:'Sonnet 5.5',elapsed:'1h',round:2}:{name,mark:''});
const owner='owner · claude (Opus 5.5·xhigh)';
const oneShot={id:'a',title:'셸 상태·표시 고치기',kind:'working',state:'working',elapsed:'41m',tool:'Edit',target:'utilities/session-shell/host.cjs',
 tool_age:'3m',shape:'one-shot-owner',stages:[{name:'one-shot',current:true,mark:'●'}],owner,owner_ctx:'38%',kind_text:'autopilot-code(dev·q)',
 owner_model:'Opus 5.5',visibility:{summary:'검증 ?'},paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh'),ctx:P('38%'),gauge5:[P('━━─── 38%')]}};
const staged={id:'b',title:'세션 간 소통 도구 단계 1',kind:'working',state:'idle',elapsed:'1h 0m',tool:'Edit',target:'utilities/peer-message.py',step:'execute ● 3/7',
 tool_age:'3m',shape:'staged',stages:seven(2),owner,owner_ctx:'22%',profile:'deep',kind_text:'autopilot-code(dev·std)',owner_model:'Opus 5.5',
 doer_worker:true,workers:[{stage:'execute',harness:'claude',model:'Sonnet 5.5',ctx:'12%'}],visibility:{summary:'검증 ?'},
 paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Sonnet 5.5'),effort:P('high'),ctx:P('12%'),gauge5:[P('━──── 12%')]}};
const waiting={id:'c',title:'새 판으로 자연스럽게 넘어가기',kind:'idle',state:'idle',elapsed:'7h 0m',shape:'staged',
 stages:[...['plan','plan-check','execute'].map(name=>({name,mark:'✓'})),...['host-run','test','report','close'].map(name=>({name,mark:''}))],
 paint:{glyph:P('●'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')}};
const failed={id:'e',title:'PG-50 휠 입력',kind:'dead',state:'dead',elapsed:'54m',shape:'staged',owner,resume:'execute',step:'execute ✕ 2/3',
 reason:'worker claude (Sonnet 5.5·high) · failed 3m',visibility:{summary:'검증 ?'},
 stages:[{name:'plan',mark:'✓',verdict:'PASS',model:'Opus 5.5',elapsed:'8m'},{name:'execute',mark:'✕',model:'Sonnet 5.5',elapsed:'46m',note:'checked by Sonnet 5.5'},{name:'test',mark:''}],
 paint:{glyph:P('✕'),harness:P('claude'),model_text:P('Sonnet 5.5'),effort:P('high')}};
const TITLE='하팅 v4-open 감독 — 셸 상태·표시 고치기와 Fleet 세션 줄 다듬기',NOW='검토 결과 3건을 기다리는 중이며 CI 결과를 확인하고 있음';
const message=(way,who,when,body,at,delivery)=>({way,who,harness:'claude',when,body,text:body,at,...(delivery?{delivery:[delivery,'dim']}:{})});
const messages=[message('→','[e8]','02:59','fact-verify 설치됨, 재개 명령 안내',1,'전달 안 됨'),message('→','[73]','04:00','#1549·#1505 한 묶음으로 출판 요청',2,'전달됨'),
 message('←','[52]','05:00','#1556 수정 후 병합, P2 한 건: 세션이 바뀐 뒤 DB 오류면 앞 값이 남음',3),message('←','[e7]','07:50','v4.23.0 설치 완료, 다음 판 준비',4),
 message('→','[e9]','07:58','검토 요청 — 셸 패널 시안 C7',5,'보냄')];
const head={repo:[P('hearting/')],git:[P('(fix/fleet-opencode-dup-sid 🚧 3)')],selected:[P('claude-fable-5-2 high')],context:[P('context ━━━━────── 41%')],
 quota:[P('5h 12% │ 7d 66%')],session_title:TITLE,now:NOW,wait:'◑답 대기 1',messages};
const draw=(routes,width,height,{side=true,open=[]}={})=>view.render({width,height,side,fill:side,model:{...head,routes,route_count:routes.length},state:'working',
 configuration:{},expandedKeys:open,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
'''

    def test_a_value_breaks_only_between_tokens_after_its_lesser_pieces_go(self):
        # Spec E9 rule 34 (a), (e): an opened route at 40 cells lets effort, then the harness
        # and brackets, then a kind's brackets go, the tool's path give way to its file name,
        # and folds only between tokens (`Sonnet 5.5` is one); stages not reached yet share one row.
        script = self.FIXTURE + r'''
const rows=draw([oneShot,staged,failed],40,76,{open:['route:a','route:b','route:e']}),all=rows.join('\n');
for(const s of rows)assert.equal(cells(s),40,s);
for(const want of ['┆ OWNER  Opus 5.5 · 38%','┆ KIND   autopilot-code(dev·q)','┆   >_ Edit · host.cjs 3m','┆ OWNER  Opus 5.5 · 22% · deep',
 '┆ DOER   Sonnet 5.5 · 12%','┆   >_ Edit · peer-message.py ','┆ OWNER  claude (Opus 5.5) ','┆ WHY    worker Sonnet 5.5 '])assert(all.includes(want),want+'\n'+all);
assert(rows.some(s=>/┆ KIND   autopilot-code +│/.test(s))&&!all.includes('(dev·st'),all);
// The reason's rest goes on at its own token, the `·` with the word after it.
assert(rows[rows.findIndex(s=>s.includes('┆ WHY'))+1].includes('┆        · failed 3m'),all);
// A model name keeps its version on its row (`checked by` / `Sonnet 5.5`).
const note=rows.findIndex(s=>s.includes('NOTE   checked by'));
assert(note>0&&rows[note+1].includes('┆          Sonnet 5.5'),all);
// No model name, version or path is cut apart.
for(const s of rows)assert(!/\((Opus|Sonnet) *│|┆ +5\.5|utilities\/sessio( |$)|sessio *│/.test(s),s+'\n'+all);
// Stages not reached yet: one dim row, broken before a `›`; only stages with values are table rows.
const at=rows.findIndex(s=>s.includes('┆ · impl-review › test'));
assert(at>0&&rows[at+1].includes('┆   › report › close'),all);
const own=rows.slice(rows.findIndex(s=>s.includes('세션 간 소통')),rows.findIndex(s=>s.includes('PG-50')));
assert.equal(own.filter(s=>/┆ [✓●·] /.test(s)).length,4,own.join('\n'));
assert(rows.some(s=>s.includes('┆ · test'))&&!all.includes('lines ›'),all);
// The same function breaks TITLE and NOW at word boundaries, as before.
const title=rows.findIndex(s=>s.includes('TITLE'));
assert.deepEqual(rows.slice(title,title+6).map(s=>s.slice(2,-1).trimEnd()),['TITLE  하팅 v4-open 감독 — 셸','       상태·표시 고치기와 Fleet 세션',
 '       줄 다듬기','NOW    검토 결과 3건을 기다리는','       중이며 CI 결과를 확인하고','       있음'],all);
// With room the whole values stand on one row each.
const wide=draw([oneShot,staged,failed],80,76,{open:['route:a','route:b','route:e']}).join('\n');
for(const want of ['┆ OWNER  claude (Opus 5.5·xhigh) · 38%','┆ KIND   autopilot-code(dev·std)','>_ Edit · utilities/session-shell/host.cjs 3m',
 '┆ WHY    worker claude (Sonnet 5.5·high) · failed 3m','┆ · impl-review › test › report › close'])assert(wide.includes(want),want+'\n'+wide);
'''
        self.node(script, "panel-view.cjs")

    def test_route_rows_outlast_message_bodies_and_the_rule_outlasts_empty_rows(self):
        # Spec E9 rule 34 (b), (c): empty rows, then the messages' extra rows, then details,
        # then one-row messages go before the routes' third and second rows; the messages
        # grow only into rows the routes leave in their three-row form; the rule under the
        # metadata stays while TITLE or NOW takes more than one row.
        script = self.FIXTURE + r'''
const nine=[...Array(9)].map((_,i)=>{const r=[oneShot,staged,waiting][i%3];return {...r,id:'n'+i,title:r.title+' '+(i+1)};});
const place=rows=>rows.filter(s=>/│ │   (one-shot|staged)/.test(s)).length,names=rows=>rows.filter(s=>/│ │   (셸|세션|새 판)/.test(s)).length;
const section=rows=>{const m=rows.findIndex(s=>s.startsWith('│ MESSAGES'));return m<0?[]:rows.slice(m+1,rows.findIndex((s,i)=>i>m&&/^│ ─+ │$/.test(s)));};
// 40×44: every route keeps its three rows and the three messages fold to one row each.
const r44=draw(nine,40,44),m44=section(r44);
assert(place(r44)===9&&names(r44)===9&&m44.length===3&&m44.every(s=>/^│ [←→] \[/.test(s)),r44.join('\n'));
// The messages fold to one row each before a route gives up its third row: from 40×44 on the routes keep three rows.
for(let h=44;h<=60;h++)assert.equal(place(draw(nine,40,h)),9,h+'\n'+draw(nine,40,h).join('\n'));
assert(place(draw(nine,40,43))<9);
// 40×70: rows to spare after the routes' whole form (spec rule 38: with their context rows)
// give the messages their two rows and more; at 40×60 the routes' whole form leaves none.
const r60=draw(nine,40,70),m60=section(r60),c60=draw(nine,40,60);
assert(place(r60)===9&&m60.length>6&&m60.filter(s=>/^│   \S/.test(s)).length>3,r60.join('\n'));
assert(place(c60)===9&&c60.filter(s=>/│ │   ━/.test(s)).length===6&&section(c60).length<=6,c60.join('\n'));
// 40×30: the routes fold to one row; the rows that frees go back to the messages' two-row form, none left empty.
const r30=draw(nine,40,30);
assert(place(r30)===0&&section(r30).length===6&&!r30.some(s=>/^│ +│$/.test(s)),r30.join('\n'));
for(let h=16;h<=72;h++){
 const rows=draw(nine,40,h),all=rows.join('\n'),body=section(rows);
 // Extra message rows only while every route stands in its three-row form.
 if(place(rows)<9)assert(body.filter(s=>/^│ [←→] /.test(s)).length<=3&&body.length<=6,h+'\n'+all);
 // The rule under the metadata goes only once TITLE and NOW are one row each.
 const title=rows.findIndex(s=>s.startsWith('│ TITLE'));
 if(title>0&&/^│ {8}\S/.test(rows[title+1]))assert(/^│ [─━]+ │$/.test(rows[title-1]),h+'\n'+all);
}
const tall=draw(nine,40,60);
assert(/^│ [─━]+ │$/.test(tall[tall.findIndex(s=>s.startsWith('│ TITLE'))-1]),tall.join('\n'));
'''
        self.node(script, "panel-view.cjs")

    def test_the_steward_number_holds_and_two_structural_empty_rows_part_the_groups(self):
        # Spec E9 rule 35: (a) a steward's number does not blink, only `⚑` changes between
        # beats; (b) one empty row parts [repository, Git] from [model, context]; (c) one
        # empty row parts the route box from the question box or the messages rule, and the
        # messages' extra rows leave it; (d) both are structural rows: they go after the
        # values on several rows, the route one first, before the rule under the metadata.
        script = self.FIXTURE + r'''
const blank=s=>/^│ +│$/.test(s);
// (a) An idle steward panel: between the two beats one cell changes, `⚑`, on every face.
const steward={...head,tag:'[e9]',steward:true,styles:{dim:{dim:true},tag_steward:{foreground:219,bold:true},tag_steward_dim:{foreground:219,dim:true}}};
const cellsOf=v=>v.rows.flatMap((r,y)=>r.flatMap(p=>[...String(p.text)].map(ch=>JSON.stringify([y,ch,p.foreground??null,p.background??null,!!p.bold,!!p.dim]))));
for(const [config,background,extra,number] of [[{theme:'terminal'},'dark',{},[219,true]],[{theme:'light'},'',{},[132,true]],[{theme:'terminal',shade:'medium'},'dark',{},[219,true]],
 [{theme:'terminal'},'',{},[219,true]],[{theme:'mono'},'dark',{},[null,false]],[{},'dark',{no_color:true},[219,false]]]){
 const at=frame=>view.render({width:40,height:30,side:true,fill:true,model:{...steward,...extra,routes:[waiting],route_count:1},state:'idle',frame,configuration:config,background,questions:()=>[]});
 const lit=at(0),dark=at(10),a=cellsOf(lit),b=cellsOf(dark);
 assert.equal(a.length,b.length);
 const changed=a.map((c,i)=>c===b[i]?null:[JSON.parse(c),JSON.parse(b[i])]).filter(Boolean);
 assert(changed.length===1&&changed[0][0][1]==='⚑'&&changed[0][0][4]===true&&changed[0][1][4]===false,JSON.stringify(config)+' '+JSON.stringify(changed));
 for(const v of [lit,dark]){const tag=v.rows[0].find(x=>x.text==='e9');
  assert(tag&&!tag.beat&&(tag.foreground??null)===number[0]&&!!tag.bold===number[1]&&!tag.dim,JSON.stringify(config)+' '+JSON.stringify(tag));}
}
// (b) The metadata's two groups: with Git, and without it (the row parts the repository from the model).
const r50=draw([oneShot,staged],40,50);
assert(r50[1].includes('hearting/')&&r50[2].includes('(fix/fleet')&&blank(r50[3])&&r50[4].includes('claude-fable-5-2 high')&&r50[5].startsWith('│ CTX    ━━━━'),r50.join('\n'));
const noGit=view.render({width:40,height:50,side:true,fill:true,model:{...head,git:[],routes:[oneShot],route_count:1},state:'working',configuration:{},questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
assert(noGit[1].includes('hearting/')&&blank(noGit[2])&&noGit[3].includes('claude-fable-5-2 high'),noGit.join('\n'));
// (c) Six routes and eight messages at 40×58 (spec rule 38: each route with its context
// row): three messages show (`MESSAGES +5`), their bodies on the rows they may take, and the
// rows they leave stay empty under the messages (11-14 night: the sections stand from the top,
// the one parting row under the route box and no more).
const more=[...messages,message('←','[52]','08:10','두 번째 검토 — 줄이는 순서를 확인해 주세요, 본문이 길어서 둘째 줄까지 넘어감',6),
 message('→','[73]','08:20','PR 출판 요청 두 번째',7,'전달됨'),message('←','[e7]','08:30','설치 끝',8)];
const six=[...Array(6)].map((_,i)=>{const r=[oneShot,staged,waiting][i%3];return {...r,id:'s'+i,title:r.title+' '+(i+1)};});
const QUESTION=[[P('◑ Q_HEAD')],[P('Q_BODY')],[P('› A')],[P('2 B')],[P('↑↓ choose · Enter send · t text')]];
const paint=(routes,height,o={})=>view.render({width:40,height,side:true,fill:true,model:{...head,messages:o.messages||more,routes,route_count:routes.length,answerable:!!o.q,...(o.model||{})},
 state:'idle',configuration:{},questions:o.q?(q=>o.q.slice(0,q.budget)):()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
const rule=rows=>rows.findIndex(s=>s.startsWith('│ MESSAGES'));
const full=paint(six,58),m=rule(full),box=full.findIndex(s=>s.includes('╰'));
const closing=full.findIndex((s,i)=>i>m&&/^│ ─+ │$/.test(s));
assert(box>0&&box===m-2&&blank(full[m-1])&&/^│ MESSAGES \+5 ─+ │$/.test(full[m])&&full.slice(m+1).filter(s=>/^│   \S/.test(s)).length>3
 &&closing>m&&closing<full.length-4&&full.slice(closing+1,-2).every(blank),full.join('\n'));
// With a question the row stands between the route box and the question box.
const asked=paint([oneShot,staged],44,{q:QUESTION,messages}),q=asked.findIndex(s=>s.includes('─ ◑ Q_HEAD'));
assert(q>2&&asked[q-2].includes('╰')&&blank(asked[q-1])&&asked[rule(asked)-1].includes('╰'),asked.join('\n'));
// No route box: the head's empty row stands above the messages.
const none=paint([],30,{model:{routes_known:true}});
assert(!none.join('').includes('╭')&&blank(none[rule(none)-1]),none.join('\n'));
// (d) Short of rows: values on several rows go before the route row, the route row before
// the metadata one, the metadata one before the rule under it; extra message rows only
// while all of them stand.
for(let h=18;h<=72;h++){
 const rows=paint(six,h),all=rows.join('\n'),at=rule(rows),git=rows.findIndex(s=>s.includes('(fix/')),title=rows.findIndex(s=>s.startsWith('│ TITLE'));
 const routeGap=at>1&&blank(rows[at-1]),metaGap=git>0&&blank(rows[git+1]),ruled=title>0&&/^│ ─+ │$/.test(rows[title-1]),multi=title>0&&/^│ {8}\S/.test(rows[title+1]);
 const body=at<0?[]:rows.slice(at+1,rows.findIndex((s,i)=>i>at&&/^│ ─+( \+\d+ ─)? │$/.test(s)));
 if(routeGap)assert(metaGap,h+' the route row goes first\n'+all);
 if(metaGap)assert(ruled,h+' the metadata row goes before the rule\n'+all);
 if(multi)assert(routeGap&&metaGap,h+' values on several rows go before either row\n'+all);
 if(body.filter(s=>/^│ [←→] /.test(s)).length>3||body.length>6)assert(routeGap&&metaGap&&multi,h+' extra message rows only after both\n'+all);
}
// The steps at this fixture's heights: 47 keeps both rows (TITLE and NOW one row each), 46
// only the metadata one, 45 neither (the rule stays).
const flags=h=>{const rows=paint(six,h),at=rule(rows),git=rows.findIndex(s=>s.includes('(fix/')),title=rows.findIndex(s=>s.startsWith('│ TITLE'));
 return [blank(rows[at-1]),blank(rows[git+1]),/^│ ─+ │$/.test(rows[title-1]),/^│ {8}\S/.test(rows[title+1])];};
assert.deepEqual([47,46,45].map(flags),[[true,true,true,false],[false,true,true,false],[false,false,true,false]]);
'''
        self.node(script, "panel-view.cjs")

    def test_a_folded_route_reads_who_context_what_and_kind(self):
        # Spec E9 rules 36 and 38: who (the recorded role in one column, harness, model,
        # effort) / context gauge and time / what (two rows at most) / Fleet's kind word with
        # the route's shape; the one-row form adds neither role nor kind.
        script = self.FIXTURE + r'''
const G=(pct,cells=8)=>{const f=Math.round(pct*cells/100);return [P('━'.repeat(f)),P('─'.repeat(cells-f)),P(' '),P(pct+'%')];};
const one={...oneShot,role:'owner',kind_word:'code',paint:{...oneShot.paint,gauge_row:G(38)}};
const stg={...staged,role:'worker',kind_word:'code',paint:{...staged.paint,gauge_row:G(12)}};
const wait={...waiting,role:'owner',kind_word:'code'};
const frame={id:'f',title:'셸 다듬기 방향 잡기',kind:'working',state:'working',elapsed:'3m',shape:'staged',role:'frame',kind_word:'frame',
 stages:[{name:'frame',mark:'●',current:true},{name:'frame-alternative',mark:'●'},{name:'frame-review',mark:''}],
 paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')}};
const four=[one,stg,wait,frame];
const box=rows=>rows.filter(s=>s.startsWith('│ │ '));
// (a) The role words stand in one column and the harness starts in one column after them.
const r50=draw(four,40,50),who=box(r50).filter(s=>/│ │ [⠋●] /.test(s));
assert.deepEqual(who.map(s=>s.slice(4,-4).trimEnd()),['⠋ owner  claude Opus 5.5 xhigh','⠋ worker claude Sonnet 5.5 high',
 '● owner  claude Opus 5.5 xhigh','⠋ frame  claude Opus 5.5 xhigh'],r50.join('\n'));
// An unrecorded role leaves its cells empty; nothing is made up.
const unnamed=box(draw([one,{...stg,role:undefined}],40,50));
assert(unnamed.some(s=>s.startsWith('│ │ ⠋       claude Sonnet 5.5 high'))&&!unnamed.join('').includes('worker'),unnamed.join('\n'));
// Short of cells effort goes, then the harness; the role and the model stay.
const r32=box(draw(four,32,50));
assert(r32.some(s=>s.startsWith('│ │ ⠋ worker Sonnet 5.5 '))&&r32.some(s=>s.startsWith('│ │ ⠋ owner  claude Opus 5.5 '))&&!r32.join('').includes('xhigh'),r32.join('\n'));
// (38) The context row: the gauge, the percent and the time each in one column; an unknown
// context leaves its cells empty and the time stands alone; with neither, no row.
const ctx=box(r50).filter(s=>/│ │   (━|─| +\d)/.test(s)&&/\d(m|h 0m) │ │$/.test(s));
assert.equal(ctx.length,4,r50.join('\n'));
assert(ctx[0].includes('━━━───── 38%')&&ctx[1].includes('━─────── 12%')&&!/[━─%]/.test(ctx[2].slice(4,-4))&&!/[━─%]/.test(ctx[3].slice(4,-4)),ctx.join('\n'));
assert(ctx[0].indexOf('%')===ctx[1].indexOf('%')&&ctx.every(s=>s.endsWith('m │ │')),ctx.join('\n'));
assert(!box(draw([one,{...frame,elapsed:''}],40,50)).some(s=>/^│ │ +│ │$/.test(s)),'no empty context row');
// The order: who, context, what, kind.
const at=box(r50).findIndex(s=>s.includes('owner  claude Opus 5.5 xhigh'));
assert(box(r50)[at+1].includes('38%')&&box(r50)[at+2].includes('셸 상태·표시 고치기')&&box(r50)[at+3].includes('code · one-shot'),r50.join('\n'));
// (b) Fleet's kind word leads the third row; the mode and intensity stay in the opened KIND.
for(const want of ['│   code · one-shot · Edit ','│   code · staged 3/7 · execute ● ','│   code · staged 4/7 · host-run ','│   frame · staged 1/3 · frame ● '])
 assert(r50.some(s=>s.includes(want)),want+'\n'+r50.join('\n'));
assert(!r50.join('').includes('(dev'),r50.join('\n'));
const r80=draw(four,80,50).join('\n');
assert(r80.includes('code · one-shot · Edit utilities/session-shell/host.cjs 3m')&&r80.includes('code · staged 3/7 · execute ● · owner Opus 5.5'),r80);
// The bottom layout's kind column: the kind leads it and goes first, before the tool.
const kindAt=w=>draw([one,stg,wait,{...failed,role:'owner',kind_word:'code'}],w,20,{side:false}).find(s=>s.startsWith('│ │ ')&&s.includes('세션 간'));
assert(kindAt(140).includes('│ code · staged 3/7 execute ● │ >_ Edit'),kindAt(140));
assert(!kindAt(100).includes('code ·')&&kindAt(100).includes('│ staged 3/7 execute ● │ >_ Edit'),kindAt(100));
// (c) A title takes two rows at most, the second ending in `…`; short of rows, one.
const long={...one,id:'l',title:'아주 긴 route 이름 — 셸 패널의 접힌 route 줄에서 이름이 세 줄을 넘으면 둘째 줄 끝에 줄임 표시가 붙는지 확인하는 이름'};
const lr=box(draw([one,long],40,50)),li=lr.findIndex(s=>s.includes('아주 긴'));
assert(lr[li+1].includes('…')&&lr[li+2].includes('code · one-shot')&&!lr.join('').includes('확인하는'),lr.join('\n'));
// (d) The one-row form: title, harness, percent and time; no role, no kind.
const nine=[...Array(9)].map((_,i)=>({...[one,stg,wait][i%3],id:'n'+i,title:[one,stg,wait][i%3].title+' '+(i+1)}));
const r30=box(draw(nine,40,30)),flat=r30.filter(s=>/│ │ [⠋●] \S/.test(s));
assert(flat.length>0&&flat.every(s=>!/owner|worker|code ·/.test(s))&&!r30.some(s=>/│ │   (━|code)/.test(s)),r30.join('\n'));
// Short of rows: a title's second row, then the context row folds into the first row
// (percent and time at the right, no gauge), then the kind row, then the one-row form.
const r44=box(draw(nine,40,44));
assert(!r44.some(s=>s.includes('━'))&&r44.filter(s=>/│ │ [⠋●] (owner|worker) .* \d+m │ │$/.test(s)).length===9&&r44.filter(s=>s.includes('│   code · ')).length===9,r44.join('\n'));
for(let h=16;h<=80;h++){
 const rows=box(draw(nine,40,h)),gauges=rows.filter(s=>/│ │   ━/.test(s)).length,kinds=rows.filter(s=>s.includes('│   code · ')).length;
 const second=rows.some(s=>/│ │   넘어가기/.test(s));
 if(gauges)assert(kinds===9,h+' the context row goes before the kind row\n'+rows.join('\n'));
 if(second)assert(gauges===6,h+' a title keeps its second row only while the context rows stand\n'+rows.join('\n'));
}
'''
        self.node(script, "panel-view.cjs")

    def test_the_repository_row_counts_worktrees_and_mem_reads_the_latest_saved_memory(self):
        # Spec E9 rules 37, 41: beside the native area `REPO   hearting · wt 12` and `GIT` without the
        # count. (MEM moved under the messages, spec rule 46; an older backend's row goes there.)
        # The bottom layout keeps its rows as they were.
        script = self.FIXTURE + r'''
const D=t=>({text:t,dim:true}),MEM=[D('3m '),D('decision '),P('셸 패널 route 줄 순서는 누가 / context·시간 / 무엇 / 종류로 정함, 지어낸 기억')];
const side={...head,repo_side:[P('hearting')],worktrees:[D('wt 12')],git_side:[D('fix/fleet-opencode-… ✓')],memory:MEM};
const paint=(routes,width,height,o={})=>view.render({width,height,side:o.side!==false,fill:o.side!==false,model:{...side,...(o.model||{}),routes,route_count:routes.length},
 state:'working',configuration:{},questions:()=>[]});
const text=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const v50=paint([oneShot,staged],40,50),r50=text(v50);
assert(r50[1].startsWith('│ REPO   hearting · wt 12 ')&&r50[2].startsWith('│ GIT    fix/fleet-opencode-… ✓ ')&&!r50.slice(1,3).join('').includes('🚧'),r50.join('\n'));
// The labels are TITLE's and NOW's: dim, seven cells; the name keeps its color, the rest is dim.
const labels=v50.rows.slice(1,3).map(r=>r.find(p=>/^(REPO|GIT) +$/.test(p.text)));
assert(labels.every(p=>p&&p.dim===true&&p.text.length===7),JSON.stringify(labels));
assert(v50.rows[1].find(p=>p.text==='hearting').dim!==true&&v50.rows[1].filter(p=>p.text===' · '||p.text==='wt 12').every(p=>p.dim===true),JSON.stringify(v50.rows[1]));
// Short of cells the branch gives way; the status and a warning stay whole (spec rule 30 (d)).
const tight=text(paint([oneShot],40,50,{model:{git_side:[D('fix/fleet-opencode-…'),D(' ↓3'),D(' ⚠️rebase')]}}));
assert(/^│ GIT    fix\/fleet-.*… ↓3 ⚠️rebase +│$/.test(tight[2]),tight.join('\n'));
// Narrower than 30 cells inside, the labels go first (as TITLE's and NOW's).
const narrow=text(paint([oneShot],32,50));
assert(narrow[1].startsWith('│ hearting · wt 12')&&narrow[2].startsWith('│ fix/fleet-opencode-… ✓')&&!narrow.join('').includes('REPO'),narrow.join('\n'));
assert(v50.groups.includes('head')&&v50.groups.includes('git'),v50.groups);
// Only the count was left in the parentheses: the repository row carries the Git group.
const bare=paint([oneShot],40,50,{model:{git_side:[]}});
assert(text(bare)[1].includes('wt 12')&&!text(bare).join('').includes('GIT')&&bare.groups.includes('git'),text(bare).join('\n'));
// Spec E9 rule 46 (e): an older backend's one MEM row (painted pieces only) stands in the MEM
// section under the messages, its age kept and its type left out; the head has no MEM row.
const title=r50.findIndex(s=>s.startsWith('│ TITLE')),mem=r50.findIndex(s=>/^│ MEM ─+ │$/.test(s)),msg=r50.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(title>0&&!r50.slice(0,msg).some(s=>s.includes('MEM'))&&mem>msg&&r50[mem+1].startsWith('│ ◆ 3m ')&&!r50[mem+1].includes('decision')
 &&r50[mem+2].startsWith('│   셸 패널')&&r50[mem+3].includes('…')&&!r50.join('').includes('지어낸 기억'),r50.join('\n'));
// No row, no MEM.
assert(!text(paint([oneShot],40,50,{model:{memory:[]}})).join('').includes('MEM'));
// The bottom layout: its rows as before (the count stays in the Git parentheses, no MEM).
const bottom=text(paint([oneShot,staged],140,16,{side:false}));
assert(bottom.some(s=>s.includes('hearting/ │'))&&bottom.some(s=>s.includes('(fix/fleet-opencode-dup-sid 🚧 3)'))&&!bottom.join('').includes('wt 12')
 &&!/REPO|GIT |MEM/.test(bottom.join('')),bottom.join('\n'));
'''
        self.node(script, "panel-view.cjs")

    def test_model_and_context_rows_take_the_model_and_ctx_labels(self):
        # Spec E9 rule 42: beside the native area `MODEL` and `CTX` start the model and context
        # rows as REPO's and GIT's; `CTX` stands for the word `context`; short of cells the model
        # name gives way so the effort stays whole; narrower than 30 cells inside all four labels
        # go and the rows are as they were. The bottom layout keeps its rows.
        script = self.FIXTURE + r'''
const D=t=>({text:t,dim:true}),gauge=[P('━━━━'),D('──────'),D(' '),P('41%')];
const side={...head,repo_side:[P('hearting')],worktrees:[D('wt 12')],git_side:[D('fix/fleet-opencode-… ✓')],
 selected:[P('claude-fable-5-2'),D(' '),P('high')],context:[D('context '),...gauge],context_side:gauge};
const paint=(width,o={})=>view.render({width,height:50,side:o.side!==false,fill:o.side!==false,model:{...side,...(o.model||{}),routes:[oneShot],route_count:1},
 native:o.native||{},state:'working',configuration:{},questions:()=>[]});
const text=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
// (a) 38 cells: four labels in one column, dim and seven cells; no word `context` beside them.
const v38=paint(38),r38=text(v38),meta=r38.slice(1,6);
assert(meta[0].startsWith('│ REPO   hearting · wt 12')&&meta[1].startsWith('│ GIT    fix/fleet-opencode-… ✓')&&/^│ +│$/.test(meta[2])
 &&meta[3].startsWith('│ MODEL  claude-fable-5-2 high ')&&meta[4].startsWith('│ CTX    ━━━━────── 41% '),r38.join('\n'));
const labels=[1,2,4,5].map(i=>v38.rows[i].find(p=>/^(REPO|GIT|MODEL|CTX) +$/.test(p.text)));
assert(labels.every(p=>p&&p.dim===true&&p.text.length===7),JSON.stringify(labels));
assert(!r38.slice(0,8).join('\n').includes('context'),r38.join('\n'));
assert(v38.groups.includes('context'),v38.groups);
// (b) A long model: the name gives way with `…`, the effort stays whole (as GIT's branch).
for(const o of [{model:{selected:[P('opencode-go/deepseek-v4.1-flash'),D(' '),P('max')]}},{native:{model:'opencode-go/deepseek-v4.1-flash',effort:'max'}}]){
 const row=text(paint(38,o))[4];
 assert(/^│ MODEL  opencode-go\/deepseek-v\S*… max │$/.test(row)&&cells(row)===38,row);
}
// (c) 30 cells inside keep the labels; 29 cells inside, all four go and `context` comes back.
assert(text(paint(34))[5].startsWith('│ CTX    ━━━━'),text(paint(34)).join('\n'));
const r33=text(paint(33));
assert(r33[1].startsWith('│ hearting · wt 12')&&r33[2].startsWith('│ fix/fleet-opencode-… ✓')&&r33[4].startsWith('│ claude-fable-5-2 high')
 &&r33[5].startsWith('│ context ━━━━────── 41%')&&!/REPO|GIT |MODEL|CTX/.test(r33.join('')),r33.join('\n'));
// (d) A model not known yet.
assert(text(paint(38,{model:{selected:[]}}))[4].startsWith('│ MODEL  Checking '),text(paint(38,{model:{selected:[]}})).join('\n'));
// (e) OpenCode's MCP count stays on the CTX row; the MODEL row carries none.
const mcp=text(paint(38,{model:{mcp:[D('MCP '),P('3/4')]}}));
assert(mcp[5].startsWith('│ CTX    ━━━━────── 41% │ MCP 3/4 ')&&!mcp[4].includes('MCP'),mcp.join('\n'));
// (f) An older backend without `context_side`: the word goes, never `CTX    context`.
for(const context of [[D('context '),...gauge],[P('context ━━━━────── 41%')]]){
 const old=text(paint(38,{model:{context,context_side:undefined}}));
 assert(old[5].startsWith('│ CTX    ━━━━────── 41% ')&&!old.join('').includes('context'),old.join('\n'));
}
// (g) The bottom layout keeps its rows, whatever the side fragments say.
const bottom=o=>text(paint(140,{side:false,...o}));
assert(bottom().some(s=>s.includes('claude-fable-5-2 high │ context ━━━━────── 41%'))&&!/MODEL|CTX/.test(bottom().join('')),bottom().join('\n'));
assert.deepEqual(bottom({model:{context_side:undefined}}),bottom());
'''
        self.node(script, "panel-view.cjs")

    def test_a_route_shows_its_owner_first_and_its_live_workers_in_a_box_under_it(self):
        # Spec E9 rules 43, 48, 57: beside the native area a route's first row is its owner (no
        # branch, no `│` guide); its stage workers running now stand in one rounded box under it,
        # two cells in (its left line under the owner's text, its right line where it was), each
        # worker's who in the box's top line (`╭─ … ─╮`, a later one's in the line between,
        # `├─ … ─┤`; N workers take 3N+1 rows), then `CTX` under the role word with the gauge
        # under the harness and its own time right after the percent, then its stage and tool. The
        # route's time follows the owner's percent (` · `), never at the right edge; the title
        # takes its rows whole. An older backend's row (no `workers_live`) keeps its one doer.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,e,pct,bar)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)]});
const solo={id:'s',title:'셸 상태·표시 고치기',kind:'working',state:'working',elapsed:'41m',shape:'one-shot-owner',kind_word:'code',role:'owner',tool:'Edit',
 stages:[{name:'one-shot',current:true,mark:'●'}],paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','38%','━━━')},workers_live:[]};
const worker=(h,m,e,pct,bar,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',...o,paint:{glyph:P(o.kind==='idle'?'●':'⠋'),...doer(h,m,e,pct,bar)}});
const one={id:'w',title:'속도 3단계',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'worker',doer_worker:true,
 owner_model:'Opus 5.5',paint:{glyph:P('⠋'),...doer('claude','Sonnet 5.5','high','22%','━━')},owner_paint:doer('claude','Opus 5.5','xhigh','52%','━━━━'),
 workers_live:[worker('claude','Sonnet 5.5','high','22%','━━',{tool:'Edit',target:'utilities/panel.py'})]};
const two={...one,id:'t',title:'검토 두 갈래',role:'owner',doer_worker:false,owner_paint:undefined,paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','61%','━━━━')},
 workers_live:[worker('codex','gpt-5.5','high','34%','━━',{tool:'Bash'}),worker('opencode','glm-5.2','max','71%','━━━━━',{kind:'idle',elapsed:'8m'})]};
const box=rows=>rows.slice(rows.findIndex(s=>/╭─ \d+ ROUTE/.test(s)),rows.findIndex(s=>s.startsWith('│ ╰'))+1);
const col=(row,word)=>[...row.slice(0,row.indexOf(word))].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
// No worker: owner / CTX with the route's time after its percent / title / kind; no inner box.
let b=box(draw([solo],40,60)),at=b.findIndex(s=>s.includes('⠋ owner claude Opus 5.5 xhigh'));
assert(at>0&&/^│ │   CTX   ━━━───── 38% · 41m +│ │$/.test(b[at+1])&&/^│ │   셸 상태·표시 고치기 +│ │$/.test(b[at+2])&&b[at+3].includes('│   code · one-shot · Edit'),b.join('\n'));
assert.equal(col(b[at+1],'CTX'),col(b[at],'owner'));assert.equal(col(b[at+1],'━'),col(b[at],'claude'));
assert(!/[└├]─|╭─ [⠋●]/.test(b.join('\n'))&&!b.some(s=>/^│ │ +│/.test(s))&&!b[at+2].includes('41m'),b.join('\n'));
// One worker: the owner's four rows (no guide), then the box two cells in: who in its top line
// (up to the corner rather than giving up a value), CTX with its time, stage and tool, bottom line.
b=box(draw([one],40,60));at=b.findIndex(s=>s.includes('⠋ owner  claude Opus 5.5 xhigh'));
for(const [i,want] of [[1,/^│ │   CTX    ━━━━──── 52% · 2h 10m +│ │$/],[2,/^│ │   속도 3단계 +│ │$/],[3,/^│ │   code · staged 3\/7 · execute ● +│ │$/],
 [4,/^│ │   ╭─ ⠋ worker claude Sonnet 5\.5╮ │ │$/],[5,/^│ │   │    CTX    ━━──── 22% · 12m │ │ │$/],[6,/^│ │   │    execute · Edit panel\.py │ │ │$/],[7,/^│ │   ╰─+╯ │ │$/]])
 assert(want.test(b[at+i]),i+'\n'+b.join('\n'));
// Spec rule 57 (c): the box's left line under the owner's text, its right line where the route's rows end.
assert.equal(col(b[at+4],'╭'),col(b[at+1],'CTX'));assert.equal(b[at+4].lastIndexOf('╮'),b[at+5].lastIndexOf('│ │ │')+0);
// (Six cells narrower inside the box: the tool's target shows where it fits, its file name first.)
assert(box(draw([one],52,60)).some(s=>/│    execute · Edit utilities\/panel\.py +│ │ │$/.test(s)),box(draw([one],52,60)).join('\n'));
assert.equal(col(b[at+5],'CTX'),col(b[at+4],'worker'));assert.equal(col(b[at+5],'━'),col(b[at+4],'claude'));
assert.equal(col(b[at+1],'CTX'),col(b[at],'owner'));assert(!b.join('').includes('owner Opus'),b.join('\n'));
const v1=view.render({width:40,height:60,side:true,model:{...head,routes:[one],route_count:1},configuration:{},questions:()=>[]});
const rowOf=re=>v1.rows.find(r=>re.test(r.map(p=>p.text).join('')));
const ownerRow=rowOf(/⠋ owner/),topLine=rowOf(/╭─ ⠋ worker/);
assert(ownerRow.find(p=>p.text==='owner ').dim&&ownerRow.some(p=>p.text==='Opus 5.5'),JSON.stringify(ownerRow));
assert(topLine.filter(p=>/^[╭╮─ ]*[╭╮─][╭╮─ ]*$/.test(p.text)&&p.text.trim()).every(p=>p.dim===true&&p.role==='messages')&&topLine.find(p=>p.text==='worker').dim,JSON.stringify(topLine));
// Every row of the box opens its route, as the owner's do.
const area=v1.hits.filter(h=>h.action==='route:w').map(h=>h.row),boxRows=v1.rows.map((r,i)=>/[╭│╰][─ ]/.test(r.map(p=>p.text).join('').slice(4,6))&&/worker|CTX|execute|╰─/.test(r.map(p=>p.text).join(''))?i:-1).filter(i=>i>=0);
for(const i of boxRows)assert(area.includes(i),i+' '+JSON.stringify(area));
// Two workers: one box, the second's who in the line between; 3N+1 rows.
b=box(draw([two],40,60));const top=b.findIndex(s=>/╭─ ⠋ worker codex gpt-5\.5 ─+╮/.test(s));
assert(top>0&&/^│ │   │    CTX    ━━.* 34% · 12m +│ │ │$/.test(b[top+1])&&/^│ │   │    execute · Bash +│ │ │$/.test(b[top+2])&&/^│ │   ├─ ● worker opencode glm-5\.2 ┤ │ │$/.test(b[top+3])
 &&/^│ │   │    CTX    ━━━━.* 71% · 8m +│ │ │$/.test(b[top+4])&&/^│ │   │    execute +│ │ │$/.test(b[top+5])&&/^│ │   ╰─+╯ │ │$/.test(b[top+6])&&b[top+7].startsWith('│ ╰'),b.join('\n'));
// No role recorded anywhere: no role column, `CTX` and one space.
b=box(draw([{...solo,role:undefined}],40,60));
assert(b.some(s=>s.includes('│ ⠋ claude Opus 5.5 xhigh'))&&b.some(s=>/^│ │   CTX ━━━───── 38% · 41m/.test(s)),b.join('\n'));
// An unknown context: no CTX label (owner or worker); the times stay in the value column.
b=box(draw([{...one,owner_paint:{harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')},workers_live:[{...worker('claude','Sonnet 5.5','high','22%','━━'),paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Sonnet 5.5')}}]}],40,60));
assert(!b.join('').includes('CTX')&&b.some(s=>/^│ │ {10}2h 10m +│ │$/.test(s))&&b.some(s=>/^│ │   │ {11}12m +│ │ │$/.test(s))&&!b.some(s=>/속도 3단계 +2h/.test(s)),b.join('\n'));
// A title on two rows: both rows the title's; the time on the CTX row.
b=box(draw([{...solo,title:'아주 긴 route 이름이 두 줄로 넘어가는 경우의 시간 자리 확인'}],40,60));at=b.findIndex(s=>s.includes('아주 긴'));
assert(!b[at].includes('41m')&&!b[at+1].includes('41m')&&b[at-1].includes('38% · 41m')&&b[at+2].includes('code · one-shot'),b.join('\n'));
// Hangul in the owner's title and the worker's target: every line keeps its cells.
for(const width of [32,34,38,40,44])for(const s of draw([{...one,title:'한글 제목이 길게 이어지는 경우',workers_live:[worker('claude','Sonnet 5.5','high','22%','━━',{tool:'Edit',target:'문서/한글 파일.md'})]},two],width,60))
 assert.equal(cells(s),width,width+' '+s);
// Short of cells (rule 48 (d)): in the box's top line the effort goes, then the harness; role and model stay.
assert(box(draw([one],48,60)).some(s=>/╭─ ⠋ worker claude Sonnet 5\.5 high ─+╮/.test(s)),box(draw([one],48,60)).join('\n'));
assert(box(draw([one],44,60)).some(s=>/╭─ ⠋ worker claude Sonnet 5\.5 ─+╮/.test(s)));
assert(box(draw([one],36,60)).some(s=>/╭─ ⠋ worker Sonnet 5\.5 ─+╮/.test(s)),box(draw([one],36,60)).join('\n'));
assert(box(draw([one],34,60)).some(s=>/╭─ ⠋ worker Sonnet 5\.5 ╮/.test(s)),box(draw([one],34,60)).join('\n'));
assert(box(draw([one],32,60)).some(s=>/╭─ ⠋ worker Sonnet …/.test(s)),box(draw([one],32,60)).join('\n'));
b=box(draw([{...one,workers_live:[worker('opencode','deepseek-v4.1-flash','max','22%','━━')]}],40,60));
assert(b.some(s=>/╭─ ⠋ worker deepseek-v4\.1-\S*… ─*╮/.test(s))&&b.some(s=>s.includes('⠋ owner  claude Opus 5.5 xhigh')),b.join('\n'));
assert(box(draw([{...one,workers_live:[worker('opencode','deepseek-v4.1-flash','max','22%','━━')]}],48,60)).some(s=>/╭─ ⠋ worker deepseek-v4\.1-flash ─+╮/.test(s)));
assert(box(draw([{...one,workers_live:[worker('opencode','deepseek-v4.1-flash','max','22%','━━')]}],52,60)).some(s=>/╭─ ⠋ worker opencode deepseek-v4\.1-flash ╮/.test(s)));
// An older backend's row: one doer, its context row keeps the time, no CTX, no box.
const older={...one,owner_paint:undefined,workers_live:undefined};
b=box(draw([older],60,60));
assert(!/CTX|╭─ [⠋●]|└─/.test(b.join(''))&&b.some(s=>/⠋ worker claude Sonnet 5\.5 high/.test(s))&&b.some(s=>/━━────── 22% +2h 10m │ │$/.test(s))
 &&b.some(s=>s.includes('owner Opus 5.5')),b.join('\n'));
// The bottom layout keeps its rows whatever the route carries.
assert.deepEqual(draw([solo,one,two],120,20,{side:false}),draw([{...solo,workers_live:undefined},older,{...two,workers_live:undefined}],120,20,{side:false}));
"""
        self.node(script, "panel-view.cjs")

    def test_a_route_box_carries_its_time_on_the_ctx_row_and_its_workers_two_cells_further_in(self):
        # Spec E9 rule 57: owners and workers keep their `CTX` row (label, bar, percent) with the
        # time right after the percent (` · `, dim), not at the right edge; the title and the
        # worker's stage row carry no time. The workers' box stands two cells further in. Short of
        # cells the bar shortens, then goes, then the time; the percent stays. Widths 32-48 with the
        # rows running short: every row keeps the panel's width, the steps of rule 48 (c) hold
        # (owners and workers fold together; folded, percent and time end the first row).
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,e,pct,bar)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)]});
const worker=(h,m,pct,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',tool:'Edit',...o,paint:{glyph:P('⠋'),...doer(h,m,'high',pct,'━━')}});
const solo={id:'s',title:'셸 상태·표시 고치기',kind:'working',state:'working',elapsed:'41m',shape:'one-shot-owner',kind_word:'code',role:'owner',tool:'Edit',
 stages:[{name:'one-shot',current:true,mark:'●'}],paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','38%','━━━')},workers_live:[]};
const one={id:'w',title:'한글 제목 — 속도 3단계의 남은 재계산',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'owner',
 paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','52%','━━━━')},workers_live:[worker('claude','Sonnet 5.5','22%',{target:'문서/한글 파일.md'})]};
const two={...one,id:'t',title:'검토 두 갈래',workers_live:[worker('codex','gpt-5.5','34%'),worker('opencode','glm-5.2','71%',{elapsed:'8m'})]};
const text=r=>r.map(p=>p.text).join('');
for(let width=32;width<=48;width++){
 for(let h=60;h>=12;h--){
  const v=view.render({width,height:h,side:true,fill:true,model:{...head,routes:[solo,one,two],route_count:3},state:'working',configuration:{},questions:()=>[]});
  const rows=v.rows.map(text),where=width+'x'+h+'\n'+rows.join('\n');
  for(const s of rows)assert.equal(cells(s),width,where);
  // (a), (b) The time follows a percent on a CTX row, never ends a title or a stage row.
  for(const s of rows.filter(s=>/^│ │ +(│ +)?CTX /.test(s))){if(/ · \d/.test(s))assert(/\d+% · (\d+h )?\d+m +(│ )?│ │$/.test(s),where);}
  assert(!rows.some(s=>/(고치기|재계산|두 갈래|execute · Edit|execute · Bash|execute) +(\d+h )?\d+m +(│ )?│ │$/.test(s)),where);
  // (c) The box two cells in: its left line under the owner's CTX label, its right line at the route's edge.
  const tops=rows.map((s,i)=>/^│ │   [╭├]─ /.test(s)?i:-1).filter(i=>i>=0);
  for(const i of tops)assert(/[╮┤] │ │$/.test(rows[i]),where);
  if(tops.length)assert(!rows.some(s=>/^│ │ [╭├]─ [⠋●]/.test(s)),where);
 }
}
// At 40 cells, the picture of rule 57.
const b=draw([one],40,60),at=b.findIndex(s=>s.includes('⠋ owner'));
for(const [i,want] of [[1,/^│ │   CTX    ━━━━──── 52% · 2h 10m +│ │$/],[2,/^│ │   한글 제목 — 속도 3단계의 남은 +│ │$/],[3,/^│ │   재계산 +│ │$/],
 [4,/^│ │   code · staged 3\/7 · execute ● +│ │$/],[5,/^│ │   ╭─ ⠋ worker claude Sonnet 5\.5╮ │ │$/],[6,/^│ │   │    CTX    ━━──── 22% · 12m │ │ │$/],[7,/^│ │   │    execute · Edit +│ │ │$/],[8,/^│ │   ╰─+╯ │ │$/]])
 assert(want.test(b[at+i]),i+'\n'+b.join('\n'));
// (d) Short of cells: the bar shortens, then goes; then the time; the percent stays.
const at32=draw([one],32,60);
assert(at32.some(s=>/^│ │   CTX    52% · 2h 10m +│ │$/.test(s))&&at32.some(s=>/^│ │   │    CTX    22% +│ │ │$/.test(s)),at32.join('\n'));
assert(draw([solo],32,60).some(s=>/^│ │   CTX   ━+─+ 38% · 41m │ │$/.test(s)&&s.replace(/[^━─]/g,'').length<8),draw([solo],32,60).join('\n'));
// Folded (rule 48 (c)): percent and time end the first row together, as before.
const folded=view.render({width:40,height:22,side:true,fill:true,model:{...head,routes:[one,two],route_count:2},state:'working',configuration:{},questions:()=>[]}).rows.map(text);
assert(folded.some(s=>/^│ │ ⠋ owner .* 52% +2h 10m │ │$/.test(s))&&!folded.some(s=>/^│ │ .*CTX/.test(s)),folded.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_a_four_cell_context_keeps_the_worker_box_line_at_the_narrowest_panel(self):
        # #1583 review follow-up (가): at 32 cells a four-cell percent (`100%`, or a token count
        # `250K` with no window) made the box's `CTX` row one cell too wide and its right `│`
        # fell off. Short of cells the row keeps `CTX` and the percent and lets the bar go, as
        # the owner's row does; every row of the box keeps the panel's width and its right line.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
// The gauge at the panel's width, as the backend lays it out (`max(5, min(10, (width-8)//4))` cells).
const gauge=(width,pct)=>{const n=Math.max(5,Math.min(10,Math.floor((width-8)/4)));
 if(!pct.endsWith('%'))return [D(pct)];const on=Math.round(n*parseInt(pct)/100);return [P('━'.repeat(on)),D('─'.repeat(n-on)),D(' '),P(pct)];};
for(let width=32;width<=36;width++)for(const pct of ['100%','250K','9%']){
 const route={id:'w',title:'속도 3단계',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'owner',
  paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh'),ctx:P('52%'),gauge_row:gauge(width,'52%')},
  workers_live:[{kind:'working',role:'worker',stage:'execute',elapsed:'12m',tool:'Edit',
   paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Sonnet 5.5'),effort:P('high'),ctx:P(pct),gauge_row:gauge(width,pct)}}]};
 const rows=draw([route],width,60),seen=width+' '+pct+'\n'+rows.join('\n');
 for(const s of rows)assert.equal(cells(s),width,seen);
 const top=rows.findIndex(s=>/^│ │   ╭─ ⠋ worker/.test(s)),bottom=rows.findIndex(s=>/^│ │   ╰─+╯ │ │$/.test(s));
 assert(top>0&&bottom>top+1,seen);
 for(const s of rows.slice(top+1,bottom))assert(/^│ │   │ .* │ │ │$/.test(s),seen);
 const ctx=rows.slice(top+1,bottom).find(s=>s.includes('CTX'));
 assert(ctx&&ctx.includes(pct),seen);
}
"""
        self.node(script, "panel-view.cjs")

    def test_the_git_arrows_keep_fleets_green_and_red_only_where_the_panel_shows_hue(self):
        # Spec E9 rule 50: the backend paints `↑N` in Fleet's `lvl_g` and `↓N` in its `lvl_r`
        # (marked `git-fresh`); the GIT row beside the native area and the bottom layout's head
        # keep those colors in 256 colors, and in mono or eight colors read as the rest of the Git
        # words, dim with no hue (rule 50 (d)). The words and cells stay.
        script = self.FIXTURE + r'''
const G=114,R=174,fresh=(t,o)=>({text:t,bold:false,dim:false,reverse:false,role:'git-fresh',...o});
const sideGit=[{text:'main',dim:true},fresh(' ↑2',{foreground:G}),fresh(' ↓5',{foreground:R,bold:true})];
const underGit=[{text:'(main',dim:true},fresh(' ↑2',{foreground:G}),fresh(' ↓5',{foreground:R,bold:true}),{text:' 🚧 3)',dim:true}];
const side=view.render({width:40,height:40,side:true,model:{...head,git_side:sideGit,routes:[],route_count:0},state:'idle',configuration:{},questions:()=>[]});
const under=view.render({width:100,height:14,side:false,model:{...head,git:underGit,routes:[],route_count:0},state:'idle',configuration:{layout:'bottom'},questions:()=>[]});
for(const [rows,re] of [[side.rows,/^│ GIT    main ↑2 ↓5 +│$/],[under.rows,/^│ \(main ↑2 ↓5 🚧 3\) +│$/]]){
 const row=rows.find(r=>re.test(r.map(p=>p.text).join('')));assert(row,rows.map(r=>r.map(p=>p.text).join('')).join('\n'));
 const look=(theme,colors)=>{const shown=view.appearance([row],{theme},{colors})[0];
  assert.equal(shown.map(p=>p.text).join(''),row.map(p=>p.text).join(''));
  return shown.filter(p=>/[↑↓]|main/.test(p.text)).map(p=>[p.text.trim(),p.foreground??null,p.bold===true,p.dim===true]);};
 const branch=look('terminal',256)[0];assert.deepEqual(branch.slice(1),[null,false,true]);
 assert.deepEqual(look('terminal',256).slice(1),[['↑2',G,false,false],['↓5',R,true,false]]);
 for(const [theme,colors] of [['mono',256],['terminal',8],['mono',8],['light',8]])
  assert.deepEqual(look(theme,colors),[branch,['↑2',null,false,true],['↓5',null,false,true]],theme+' '+colors);
 // NO_COLOR: the backend sent no color; the panel paints the parts as they came.
 assert.deepEqual(view.appearance([row],{},{noColor:true})[0],row);
}
'''
        self.node(script, "panel-view.cjs")

    def test_the_title_value_is_bold_in_the_sessions_harness_color(self):
        # Spec E9 rule 44: the TITLE row's value bold, in the session's harness color (Fleet's
        # `hb_*`), on every row it takes; the label stays dim. Without color (NO_COLOR) the weight
        # stays, as the steward's `⚑` (spec rule 35 (a); #1579 review follow-up (마)).
        script = self.FIXTURE + r'''
const styles={dim:{dim:true},loc_repo:{foreground:7},hb_claude:{foreground:116},hb_codex:{foreground:182},hb_opencode:{foreground:110},hb_other:{foreground:-1}};
const paint=(harness,o={})=>view.render({width:40,height:50,side:true,harness,model:{...head,routes:[],route_count:0,styles,...o},state:'working',configuration:{},questions:()=>[]}).rows;
const seen=new Set();
for(const harness of ['claude','codex','opencode']){
 const rows=paint(harness),at=rows.findIndex(r=>r.some(p=>p.text==='TITLE  '));
 const label=rows[at].find(p=>p.text==='TITLE  '),values=[rows[at],rows[at+1]].map(r=>r.filter(p=>p.text.trim()&&p.text!=='TITLE  '&&!p.role));
 assert(label.dim===true&&label.bold!==true,JSON.stringify(label));
 assert(values.every(v=>v.length&&v.every(p=>p.bold===true&&p.foreground===styles['hb_'+harness].foreground)),harness+JSON.stringify(values));
 seen.add(values[0][0].foreground);
 // NOW keeps its own color and weight.
 assert(rows.find(r=>r.some(p=>p.text==='NOW    ')).every(p=>p.bold!==true),harness);
}
assert.equal(seen.size,3);
const plain=paint('codex',{no_color:true}),title=plain.findIndex(r=>r.some(p=>p.text==='TITLE  ')),now=plain.findIndex(r=>r.some(p=>p.text==='NOW    '));
for(const [i,row] of plain.entries())for(const p of row)if(p.text.trim()&&p.text!=='TITLE  '&&!p.role)
 assert.equal(p.bold===true,i>=title&&i<now,i+' '+JSON.stringify(p));
'''
        self.node(script, "panel-view.cjs")

    def test_a_bottom_route_row_names_its_kind_before_its_tool(self):
        # Spec E9 rule 34 (d): after model and context, before the tool, the side layout's
        # and Fleet's words: `one-shot`, `staged 3/7 execute ●`, a failed `staged 2/3 execute ✕`;
        # short of cells the tool goes, then `staged`, then the whole column.
        script = self.FIXTURE + r'''
const four=[oneShot,staged,waiting,failed];
const row=(rows,title)=>rows.find(s=>s.startsWith('│ │ ')&&s.includes(title));
const r140=draw(four,140,20,{side:false});
for(const [title,kind] of [['셸 상태','one-shot'],['세션 간','staged 3/7 execute ●'],['새 판으로','staged 4/7 host-run'],['PG-50','staged 2/3 execute ✕']]){
 const s=row(r140,title);assert(s&&s.includes('│ '+kind),title+'\n'+r140.join('\n'));
 assert(s.indexOf('claude')<s.indexOf(kind),s);
 for(const s of r140)assert.equal(cells(s),140,s);
}
for(const title of ['셸 상태','세션 간']){const s=row(r140,title);assert(s.indexOf(s.includes('one-shot')?'one-shot':'staged')<s.indexOf('>_ Edit'),s);}
assert(!r140.some(s=>/execute ✕ 2\/3/.test(s)),r140.join('\n'));
// 80 cells: the tool goes first, then the `staged` word; the stage and its mark stay.
const r80=draw(four,80,20,{side:false});
assert(row(r80,'세션 간').includes('│ 3/7 execute ●')&&row(r80,'PG-50').includes('│ 2/3 execute ✕')&&row(r80,'셸 상태').includes('│ one-shot')
 &&!r80.join('').includes('staged')&&!r80.join('').includes('>_'),r80.join('\n'));
// Narrower still the whole column goes; the earlier columns stay.
const r64=draw(four,64,20,{side:false});
assert(!r64.join('').includes('one-shot')&&!r64.join('').includes('3/7')&&/claude Opus 5\.5 xhigh +│ ━━─── 38%/.test(row(r64,'셸')),r64.join('\n'));
// At every width the tool shows whole or not at all, and a route that names its kind never reads the older `execute ✕ 2/3`.
for(let width=56;width<=140;width+=2)for(const s of draw(four,width,20,{side:false}).filter(s=>s.startsWith('│ │ ')))
 assert(!/>_ ?\S*…|execute ✕ \d/.test(s)&&cells(s)===width,width+' '+s);
'''
        self.node(script, "panel-view.cjs")

    def test_section_heads_are_upper_case_with_the_count_first(self):
        # Spec E9 rule 45: beside the native area `2 ROUTE` (the count first, its word upper case,
        # the running pulse kept), `MESSAGES`, the producer's own label and the read pages' heads
        # too; role words and kind rows stay as they are; the bottom layout keeps `route N`.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const owned={...oneShot,role:'owner',kind_word:'code'};
const at=(o={})=>view.render({width:o.width||40,height:60,side:o.side!==false,fill:o.side!==false,model:{...head,routes:[owned,staged],route_count:2,...(o.model||{})},
 state:'working',configuration:{},questions:()=>[],...(o.extra||{})});
const v=at(),rows=plain(v),all=rows.join('\n');
assert(rows.some(s=>s.startsWith('│ ╭─ 2 ROUTE ─'))&&rows.some(s=>/^│ MESSAGES \+2 ─+ │$/.test(s))&&!/\broute \d|messages|── MESSAGES/.test(all),all);
const count=v.rows.flat().find(p=>p.text==='2 ROUTE');assert(count&&count.beat===true,JSON.stringify(count));
assert(all.includes('⠋ owner')&&all.includes('code · one-shot'),all);
// The producer's label alone (no rows yet): its count first too.
const label=plain(at({model:{routes:[],route_count:0,route_label:'route 3 ⠏2'}}));
assert(label.some(s=>s.includes('╭─ 3 ROUTE ⠏2 ─')),label.join('\n'));
// The read pages' heads.
assert(plain(at({extra:{routeDetail:true}}))[1].startsWith('│ ROUTE · Back'));
const key=v.hits.find(h=>h.action.startsWith('message:')).action;
assert(plain(at({extra:{routeDetail:key}}))[1].startsWith('│ MESSAGES · Back'));
// The bottom layout: `route N`, `messages`, its pages too (spec rule 45 (d)).
const bottom=plain(at({width:120,side:false})).join('\n');
assert(bottom.includes('╭─ route 2 ')&&bottom.includes('│ messages ')&&!bottom.includes('── messages')&&!/ROUTE|MESSAGES/.test(bottom),bottom);
assert(plain(at({width:120,side:false,extra:{routeDetail:true}}))[1].startsWith('│ route · Back'));
assert(plain(at({width:120,side:false,model:{routes:[],route_count:0,route_label:'route 3 ⠏2'}})).join('').includes('╭─ route 3 ⠏2 ─'));
"""
        self.node(script, "panel-view.cjs")

    def test_spare_rows_go_under_the_last_section(self):
        # 11-14 night: a tall side panel left a long run of empty rows between the ROUTE box and
        # MESSAGES. The sections stand from the top, one after another (one parting row under the
        # route box); the rows left over go under the last of them, right above the panel's last row.
        # The bottom layout keeps its label alone (no rule beside `messages`).
        script = self.FIXTURE + r'''
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const blank=s=>/^│ +│$/.test(s);
const tall=(height,o={})=>plain(view.render({width:o.width||40,height,side:o.side!==false,fill:o.side!==false,model:{...head,routes:[oneShot,staged],route_count:2},
 state:'idle',configuration:{},questions:()=>[]}));
for(const height of [50,70,90]){
 const rows=tall(height),box=rows.findIndex(s=>s.includes('╰')),m=rows.findIndex(s=>s.startsWith('│ MESSAGES')),closing=rows.findIndex((s,i)=>i>m&&/^│ ─+ │$/.test(s));
 assert(box>0&&m===box+2&&blank(rows[box+1])&&closing>m,height+'\n'+rows.join('\n'));
 const spare=rows.slice(closing+1,-2);
 assert(spare.length>0&&spare.every(blank)&&/⚙ │$/.test(rows.at(-2)),height+'\n'+rows.join('\n'));
}
const bottom=tall(14,{side:false,width:120});
assert(bottom.some(s=>/^│ messages( \+\d+)? +│$/.test(s))&&!bottom.some(s=>/messages[^│]*─/.test(s)),bottom.join('\n'));
'''
        self.node(script, "panel-view.cjs")

    def test_messages_show_three_at_most_however_many_rows_are_left(self):
        # 11-14 night ("메시지 뜨는 최대 개수를 좀 줄이자. 세로폭이 아무리 길어도 말이야"): beside the native area
        # no more than three messages show whatever the height; the label counts the rest
        # (`MESSAGES +N`). Spare rows still lengthen the bodies.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const more=[...messages,message('←','[52]','08:10','두 번째 검토',6),message('→','[73]','08:20','PR 출판 요청 두 번째',7,'전달됨'),message('←','[e7]','08:30','설치 끝',8)];
const at=(list,height)=>plain(view.render({width:40,height,side:true,fill:true,model:{...head,messages:list,routes:[oneShot],route_count:1},state:'idle',configuration:{},questions:()=>[]}));
const heads=rows=>rows.filter(s=>/^│ [←→] \[/.test(s));
for(const list of [messages,more])for(const height of [60,90,140]){
 const rows=at(list,height),label=rows.find(s=>s.startsWith('│ MESSAGES'));
 assert.equal(heads(rows).length,3,height+'\n'+rows.join('\n'));
 assert(new RegExp('^│ MESSAGES \\+'+(list.length-3)+' ─+ │$').test(label),label);
 // The newest three, oldest first.
 assert.deepEqual(heads(rows).map(s=>s.slice(4,8)),list.slice(-3).map(m=>m.who),rows.join('\n'));
}
// Three or fewer: all of them, and no count.
for(const n of [1,3]){const rows=at(messages.slice(0,n),90);assert.equal(heads(rows).length,n);assert(rows.some(s=>/^│ MESSAGES ─+ │$/.test(s)),rows.join('\n'));}
// Spare rows still give a body up to three rows.
const long=[...messages.slice(0,2),message('←','[52]','08:10','두 번째 검토 — 줄이는 순서를 확인해 주세요, 본문이 길어서 둘째 줄까지 넘어가고 셋째 줄까지 이어지는 긴 본문입니다',6)];
const rows=at(long,90),i=rows.findIndex(s=>s.startsWith('│ ← [52] claude · 08:10'));
assert(i>0&&rows.slice(i+1,i+4).every(s=>/^│   \S/.test(s)),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_mem_stands_under_the_messages_with_its_kind_and_gives_way_first(self):
        # Spec E9 rule 46: MEM leaves the head for its own section under the messages (spec rule 58:
        # its label first and a thin rule on from it, as the messages' `MESSAGES`; one closing rule ends both), each record `◆ kind · age` (dim) over its headline (body
        # color, two rows at most); two records at most; short of rows MEM goes before any of the
        # messages: two records, one, its headline on one row, none.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const items=[{kind:'decision',age:'3m',text:'panel labels in dark grey, and the count leads its word so it reads as a count'},
 {kind:'lesson',age:'2h',text:'one fix per review finding'}];
const routes=[oneShot,staged,{...oneShot,id:'c'}];
const at=(o={})=>view.render({width:o.width||40,height:o.height||70,side:true,fill:true,model:{...head,routes,route_count:routes.length,memory_items:items,...(o.model||{})},
 state:'idle',configuration:{},questions:()=>[]});
const section=(rows,word)=>{const i=rows.findIndex(s=>new RegExp('^│ '+word+'( \\+\\d+)? ─+ │$').test(s));if(i<0)return null;let end=i+1;
 while(end<rows.length&&!/^│ (MESSAGES|MEM)( \+\d+)? ─+ │$|^│ ─+ │$/.test(rows[end]))end++;return rows.slice(i+1,end);};
let v=at(),rows=plain(v);
const msg=rows.findIndex(s=>s.startsWith('│ MESSAGES')),mem=rows.findIndex(s=>/^│ MEM ─+ │$/.test(s));
assert(msg>0&&mem>msg&&!rows.slice(0,msg).some(s=>s.includes('MEM')),rows.join('\n'));
assert(rows[mem+1].startsWith('│ ◆ decision · 3m ')&&rows[mem+2].startsWith('│   panel labels in dark grey')&&rows[mem+3].startsWith('│   ')&&rows[mem+3].includes('…')
 &&rows[mem+4].startsWith('│ ◆ lesson · 2h ')&&rows[mem+5].startsWith('│   one fix per review finding')&&/^│ ─+ │$/.test(rows[mem+6]),rows.join('\n'));
// The head row dim, the headline in the body color; the title rule as the messages' (dim, its word the title role).
assert(v.rows[mem+1].filter(p=>p.text.trim()&&!p.role).every(p=>p.dim===true)&&v.rows[mem+2].filter(p=>p.text.trim()&&!p.role).every(p=>p.dim!==true),JSON.stringify(v.rows[mem+2]));
const title=v.rows[mem].find(p=>p.text==='MEM'),mtitle=v.rows[msg].find(p=>p.text==='MESSAGES');
assert(title.role==='title'&&title.dim===true&&mtitle.role==='title'&&mtitle.dim===true);
// One record, none.
rows=plain(at({model:{memory_items:items.slice(1)}}));
const one=section(rows,'MEM');assert(one&&one.length===2&&one[0].startsWith('│ ◆ lesson · 2h'),rows.join('\n'));
assert(!plain(at({model:{memory_items:[]}})).join('').includes('MEM'));
// A type or an age missing: the head keeps what is known.
assert(plain(at({model:{memory_items:[{kind:'',age:'5m',text:'only an age'}]}})).some(s=>s.startsWith('│ ◆ 5m ')));
// No messages: MEM alone under its rule, then the closing rule.
rows=plain(at({model:{messages:[]}}));assert(!rows.join('').includes('MESSAGES')&&section(rows,'MEM').length===5,rows.join('\n'));
// Narrower than 30 cells inside: the same section.
rows=plain(at({width:32}));assert(section(rows,'MEM')?.[0].startsWith('│ ◆ decision · 3m'),rows.join('\n'));
// Short of rows: MEM steps down before any of the messages does, in its own order.
const memState=rows=>{const m=section(rows,'MEM');if(!m)return 0;return m.filter(s=>s.startsWith('│ ◆')).length*10+m.filter(s=>s.startsWith('│   ')).length;};
const messagesWhole=rows=>{const m=section(rows,'MESSAGES')||[],heads=m.map((s,i)=>/^│ [←→] /.test(s)?i:-1).filter(i=>i>=0);
 return heads.length>=3&&heads.every(i=>/^│   \S/.test(m[i+1]||''));};
const seen=[];
for(let h=70;h>=12;h--){
 rows=plain(at({height:h}));const state=memState(rows);
 // (Rows a later step frees beyond its need may bring a MEM record back, as every step's.)
 if(!seen.includes(state))seen.push(state);
 if(state)assert(messagesWhole(rows),h+' MEM goes before the messages fold or go\n'+rows.join('\n'));
 // Spec rule 58: the messages not shown are counted right after their label (`MESSAGES +N`), with or without MEM.
 const msgRows=section(rows,'MESSAGES'),hidden=msgRows?head.messages.length-msgRows.filter(s=>/^│ [←→] /.test(s)).length:0;
 if(hidden)assert(new RegExp('^│ MESSAGES \\+'+hidden+' ─+ │$').test(rows.find(s=>s.startsWith('│ MESSAGES'))),h+'\n'+rows.join('\n'));
}
assert.deepEqual(seen,[23,12,11,0],JSON.stringify(seen));
// Spec rule 58 (11-14 night, B-가 with the label first): `MESSAGES` and `MEM` lead their rows (dim,
// the title role) and a thin rule in the lower rule's color runs on from each to the row's end;
// nothing stands before a label, and one closing rule ends both.
let lined=at();rows=plain(lined);
assert(rows.some(s=>/^│ MESSAGES \+2 ─+ │$/.test(s))&&rows.some(s=>/^│ MEM ─+ │$/.test(s))&&!rows.some(s=>/── (MESSAGES|MEM)/.test(s)),rows.join('\n'));
for(const word of ['MESSAGES','MEM']){
 const y=rows.findIndex(s=>s.startsWith('│ '+word)),line=lined.rows[y].find(p=>/^─+$/.test(p.text)),closing=lined.rows[rows.findIndex((s,i)=>i>y&&/^│ ─+ │$/.test(s))].find(p=>/^─+$/.test(p.text));
 assert(line&&line.role==='messages'&&line.role===closing.role&&line.dim===closing.dim&&line.foreground===closing.foreground,word+JSON.stringify([line,closing]));
}
assert.equal(rows.filter(s=>/^│ ─+ │$/.test(s)).length-rows.slice(0,rows.findIndex(s=>s.startsWith('│ MESSAGES'))).filter(s=>/^│ ─+ │$/.test(s)).length,1,rows.join('\n'));
const cellsOf=s=>[...s].reduce((n,c)=>n+(/[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
for(const w of [32,40,48]){const r=plain(at({width:w}));assert(r.every(s=>cellsOf(s)===w),w+'\n'+r.join('\n'));}
"""
        self.node(script, "panel-view.cjs")

    # Draft A (chosen 11-13, "권장안으로 해. A으로"): the LAB section of the experiment board. The
    # backend words it (tools/fleet/session_lab.py, Fleet's LAB-table cells); these build its shape.
    LAB = r'''
const T=(t,k)=>({text:t,...({dim:{dim:true},loc_repo:{foreground:252},lvl_r:{foreground:217},lvl_y:{foreground:186},g_spin:{foreground:230},lvl_g:{foreground:150},soft:{foreground:250}})[k]||{}});
const piece=(col,label,value,key='soft')=>({col,parts:label?[T(label+' ','dim'),T(value,key)]:[T(value,key)]});
const NAMES=['기본 분리','잔향 줄임','잡음 섞기','긴 구간 학습','작은 음성 보존'];
const labRow=(n,i,o={})=>{const latest=o.latest||(8.4+i/10).toFixed(1)+'@5',delta=o.delta||'+'+(0.4+i/10).toFixed(1)+' dB',state=o.state||'진행';
 return {name:T(n,'soft'),full:n,cells:{progress:T((36-i)+'/60 ep','soft'),latest:T(latest,'soft'),delta:T(delta,'lvl_g'),state:T(state,o.stateKey||'g_spin')},
  more:[piece('latest','최근',latest),piece('best','최고','8.7@4'),piece('delta','차이',delta,'lvl_g'),piece('state','',state,o.stateKey||'g_spin'),
   piece('expected','예상','21:10'),piece('since','진행뒤',o.since||'1m'),piece('observed','관측뒤','20s')],reason:o.reason?[T(o.reason,'dim')]:[]};};
const lab=(o={})=>({key:'lab:grp_1',count:'5',clock:'20:40',counts:[[T('진행 5','g_spin')]],goal:[T('잔향에서 기준보다 선명하게','soft')],
 headline:{left:[T('잔향 줄임','soft'),T(' 9.1 dB@5','soft')],right:[T('fix 8 dB','soft')]},notes:[],
 metric:[[T('si_sdr (dB) ↑','soft')],[T('기준 ','dim'),T('fix 8 dB','soft')]],header:{name:'조건',progress:'진행',latest:'최근',delta:'차이',state:'상태'},
 rows:NAMES.map((n,i)=>labRow(n,i)),more_rows:[],unlinked:[],strip:[T('5','loc_repo'),T(' LB','dim')],strip_attention:false,...o});
const failedLab=()=>lab({counts:[[T('실패 1','lvl_r')]],headline:{left:[T('잡음 섞기','soft'),T(' 실패','lvl_r')],right:[T('34/60 ep','soft')]},
 rows:NAMES.map((n,i)=>labRow(n,i,i===2?{state:'실패',stateKey:'lvl_r',reason:'실행 실패'}:{})),strip:[T('!1','lvl_r'),T('LB','dim')],strip_attention:true});
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const labStart=rows=>rows.findIndex(s=>/^│ (\d+ )?LAB( |$)/.test(s));
'''

    def test_lab_stands_under_the_routes_above_the_question_and_the_messages(self):
        # Draft A: `5 LAB 20:40` (the count leads its upper-case word, spec rule 45; the word a dim
        # title as MESSAGES and MEM), the most pressing count at the right, the goal and the one
        # condition to look at; under the route box (one parting row, spec rule 35 (c)) and above
        # the question and the messages; a press on any of its rows (and only those) opens it.
        script = self.FIXTURE + self.LAB + r"""
const items=[{kind:'decision',age:'3m',text:'평가 조건을 같게 유지'}];
const at=(o={})=>view.render({width:o.width||40,height:o.height||60,side:true,fill:true,
 model:{...head,routes:[oneShot],route_count:1,memory_items:items,lab:lab(o.lab||{}),...(o.model||{})},state:'idle',configuration:{},
 expandedKeys:o.open||[],questions:o.q?(q=>o.q.slice(0,q.budget)):()=>[]});
for(const width of [40,32]){
 const v=at({width}),rows=plain(v),at0=labStart(rows),box=rows.findIndex(s=>s.startsWith('│ ╰')),msg=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
 assert(box>0&&at0===box+2&&/^│ +│$/.test(rows[box+1])&&msg>at0,rows.join('\n'));
 assert(new RegExp('^│ 5 LAB 20:40 +진행 5 │$').test(rows[at0])&&rows[at0+1].startsWith('│ 잔향에서 기준보다 선명하게')
  &&/^│ 잔향 줄임 9\.1 dB@5 +fix 8 dB │$/.test(rows[at0+2])&&/^│ +│$/.test(rows[at0+3]),rows.join('\n'));
 const word=v.rows[at0].find(p=>p.text==='LAB');assert(word.dim===true&&word.role==='title',JSON.stringify(word));
 // A press on the LAB rows (exactly those) toggles `lab:grp_1`.
 assert.deepEqual(v.hits.filter(h=>h.action==='lab:grp_1').map(h=>h.row),[at0,at0+1,at0+2],JSON.stringify(v.hits));
 assert(v.hits.filter(h=>h.action==='lab:grp_1').every(h=>h.left===2&&h.right===width-3));
}
// No route box: LAB right under the head's parting row.
let rows=plain(at({model:{routes:[],route_count:0}}));
assert(/^│ +│$/.test(rows[labStart(rows)-1])&&!rows.join('').includes('ROUTE'),rows.join('\n'));
// A question: LAB keeps its place above it, the question stays above the messages.
const q=[[P('◑ 조건을 계속 비교할까요?')],[P('▸ 계속')],[P('  멈춤')],[P('↑↓ choose · Enter send')]];
rows=plain(at({q,height:44}));
const qa=rows.findIndex(s=>s.includes('조건을 계속')),la=labStart(rows),ma=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(la>0&&qa>la+3&&ma>qa&&rows.some(s=>s.includes('▸ 계속'))&&rows.some(s=>s.includes('Enter send')),rows.join('\n'));
assert.equal(la,labStart(plain(at({height:44}))),'LAB does not move when a question comes');
"""
        self.node(script, "panel-view.cjs")

    def test_lab_opens_in_place_as_a_condition_table_without_cutting_a_number(self):
        # Draft A opened: the metric and baseline, then one row per condition. 40 cells (36 inside):
        # name, progress, latest, difference, and under each the pieces its row does not show; 32
        # cells: name, progress, state, the rest under it. Each value whole; a column that does not
        # fit leaves; only a name ends in `…` (read whole on the row under it).
        script = self.FIXTURE + self.LAB + r"""
const labDraw=(width,model,open=['lab:grp_1'],height=80)=>view.render({width,height,side:true,fill:true,model:{...head,routes:[],route_count:0,messages:[],...model},
 state:'idle',configuration:{},expandedKeys:open,questions:()=>[]});
let rows=plain(labDraw(40,{lab:lab()})),a=labStart(rows);
assert(rows[a+2].startsWith('│ si_sdr (dB) ↑ · 기준 fix 8 dB')&&/^│ 조건 +진행 +최근 +차이 │$/.test(rows[a+3]),rows.join('\n'));
assert(/^│ 기본 분리 +36\/60 ep +8\.4@5 +\+0\.4 dB │$/.test(rows[a+4])&&rows[a+5]==='│   최고 8.7@4 · 진행 · 예상 21:10     │'&&rows[a+6].startsWith('│   진행뒤 1m · 관측뒤 20s'),rows.join('\n'));
// A cut name: `…` right after its last letter, the whole name on the next row.
const long=rows.findIndex(s=>s.startsWith('│ 작은 음성…'));assert(long>0&&rows[long+1].startsWith('│   작은 음성 보존'),rows.join('\n'));
rows=plain(labDraw(32,{lab:lab()}));a=labStart(rows);
assert(/^│ 조건 +진행 +상태 │$/.test(rows[a+4])&&rows[a+2].startsWith('│ si_sdr (dB) ↑')&&rows[a+3].startsWith('│ 기준 fix 8 dB'),rows.join('\n'));
assert(/^│ 기본 분리 +36\/60 ep +진행 │$/.test(rows[a+5])&&rows[a+6].startsWith('│   최근 8.4@5 · 최고 8.7@4')&&rows[a+7].startsWith('│   차이 +0.4 dB · 예상 21:10'),rows.join('\n'));
// A failed row says why under it; the head counts it.
rows=plain(labDraw(40,{lab:failedLab()}));a=labStart(rows);
assert(/^│ 5 LAB 20:40 +실패 1 │$/.test(rows[a])&&rows.some(s=>s.startsWith('│   실행 실패')),rows.join('\n'));
// Collapsed and opened: a press closes it again (the same key).
const v=labDraw(40,{lab:lab()}),vr=plain(v),va=labStart(vr),vend=vr.findIndex((s,i)=>i>va&&/^│ +│$/.test(s));
assert.deepEqual(v.hits.filter(h=>h.action==='lab:grp_1').map(h=>h.row),vr.map((_,i)=>i).slice(va,vend),vr.join('\n'));
// Long numbers: never cut, at any width; their column leaves whole (32's narrower form), the piece leaves when wider than a row.
const huge='12345678.90@1234567',diff='+12345658.91 dB';
const big=lab({rows:NAMES.map((n,i)=>labRow(n,i,i===1?{latest:huge,delta:diff}:{}))});
for(const width of [32,36,40,44,48,56,64,80]){
 const all=plain(labDraw(width,{lab:big}));
 for(const s of all)for(const value of [huge,diff,'36/60 ep','8.7@4','21:10'])
  for(let n=3;n<value.length;n++)assert(!s.includes(value.slice(0,n)+'…'),width+' cut '+value+'\n'+all.join('\n'));
 assert(all.join('\n').includes(huge)===(width-4-2>=huge.length+3),width+'\n'+all.join('\n'));
}
"""
        self.node(script, "panel-view.cjs")

    def test_lab_gives_way_in_its_order_and_the_question_keeps_its_rows(self):
        # Draft A's order merged with the panel's (spec rule 34 (b), 46 (d)): MEM first, then the
        # opened table loses its pieces and closes to the two-row summary, then (after the details,
        # values on several rows and structural rows) the head alone; the head outlasts the route
        # box and goes before any message. The question's rows, the fold and settings row stay.
        script = self.FIXTURE + self.LAB + r"""
const items=[{kind:'decision',age:'3m',text:'평가 조건을 같게 유지'},{kind:'lesson',age:'2h',text:'one fix per review finding'}];
const q=[[P('◑ 조건을 계속 비교할까요?')],[P('▸ 계속')],[P('  멈춤')],[P('↑↓ choose · Enter send')]];
const labDraw=(height,o={})=>plain(view.render({width:40,height,side:true,fill:true,model:{...head,routes:[oneShot],route_count:1,memory_items:items,lab:lab(),...(o.model||{})},
 state:'idle',configuration:{},expandedKeys:['lab:grp_1'],questions:o.q?(x=>o.q.slice(0,x.budget)):()=>[]}));
const state=rows=>{const a=labStart(rows),goal=a>=0&&rows[a+1]?.startsWith('│ 잔향에서');
 return {lab:a<0?0:!goal?1:rows.some(s=>/^│ 조건 /.test(s))?(rows.some(s=>s.startsWith('│   최고'))?4:3):2,
  mem:rows.some(s=>/^│ MEM ─+ │$/.test(s)),route:rows.some(s=>s.startsWith('│ ╭')),messages:rows.some(s=>s.startsWith('│ MESSAGES')),
  rule:rows.some((s,i)=>i<(a<0?rows.length:a)&&/^│ ─+ │$/.test(s))};};
const seen=[];let memGone=0,tableGone=0,ruleGone=0,oneAt=0,labGone=0,routeGone=0,messagesGone=0;
for(let h=90;h>=8;h--){
 const rows=labDraw(h),s=state(rows);
 seen.push(h+':'+JSON.stringify(s));
 if(!s.mem&&!memGone)memGone=h;if(s.lab<3&&!tableGone)tableGone=h;if(s.lab&&!s.rule&&!ruleGone)ruleGone=h;
 if(s.lab===1&&!oneAt)oneAt=h;if(!s.lab&&!labGone)labGone=h;if(!s.route&&!routeGone)routeGone=h;if(!s.messages&&!messagesGone)messagesGone=h;
 assert(rows.at(-2).endsWith('»   ⚙ │'),h+'\n'+rows.join('\n'));
}
// (A later step that frees more rows than it needed gives them back to the earlier ones, so a
// state may come back one height lower; the order is that of the first height each step is taken.)
assert(memGone>tableGone&&tableGone>oneAt&&ruleGone>=oneAt&&routeGone>=labGone&&labGone>messagesGone&&messagesGone>0,
 JSON.stringify({memGone,tableGone,ruleGone,oneAt,routeGone,labGone,messagesGone})+'\n'+seen.join('\n'));
// The one-row form: the count and the most pressing count stay, the clock goes first.
const one=labDraw(oneAt).find(s=>/^│ 5 LAB/.test(s));assert(/^│ 5 LAB( 20:40)? +진행 5 │$/.test(one),one);
// With a question: LAB takes none of its rows at any height (the same rows as without LAB, and
// all of them while there is room), and the fold row stays.
const asked=rows=>rows.filter(s=>/조건을 계속|▸ 계속|멈춤|Enter send/.test(s));
for(let h=40;h>=8;h--){const rows=labDraw(h,{q}),none=labDraw(h,{q,model:{lab:null}});
 assert.deepEqual(asked(rows),asked(none),h+'\n'+rows.join('\n'));
 if(h>=16)assert.equal(asked(rows).length,4,h+'\n'+rows.join('\n'));
 assert(rows.at(-2).endsWith('»   ⚙ │'),h);}
"""
        self.node(script, "panel-view.cjs")

    def test_lab_is_absent_without_a_board_and_an_unconfirmed_board_says_so(self):
        # No experiments and no problem: the backend sends no LAB and no row stands. A broken or
        # missing catalog with runs found, or runs without a name, never reads as "no experiments".
        script = self.FIXTURE + self.LAB + r"""
const labDraw=(model,o={})=>view.render({width:40,height:40,side:true,fill:true,model:{...head,routes:[oneShot],route_count:1,...model},state:'idle',configuration:{},
 expandedKeys:o.open||[],questions:()=>[]});
for(const none of [null,undefined,'x',[]]){const rows=plain(labDraw({lab:none}));assert(!/\bLAB\b/.test(rows.join('\n'))&&labStart(rows)<0,rows.join('\n'));}
const unread=lab({count:'',counts:[[T('이름 없음 2','lvl_y')]],goal:[T('실험 정본 아직 없음; run은 id로만 보임','lvl_y')],
 headline:{left:[T('이름 없는 run','lvl_y')],right:[T('2건','lvl_y')]},metric:[],rows:[],key:'lab:',
 unlinked:[T('이름 없는 실행 2건: run-x, run-y (정본에 등록 필요)','lvl_y')],strip:[T('?2','lvl_y'),T('LB','dim')],strip_attention:true});
let rows=plain(labDraw({lab:unread})),a=labStart(rows);
// Without a group the second row says why, whole on up to three rows (here two), then the unnamed runs.
assert(/^│ LAB 20:40 +이름 없음 2 │$/.test(rows[a])&&rows[a+1].startsWith('│ 실험 정본 아직 없음;')&&rows[a+2].startsWith('│ 보임 ')
 &&/^│ 이름 없는 run +2건 │$/.test(rows[a+3]),rows.join('\n'));
rows=plain(labDraw({lab:unread},{open:['lab:']}));a=labStart(rows);
assert(rows.slice(a).some(s=>s.startsWith('│ 이름 없는 실행 2건: run-x'))&&!rows.some(s=>/^│ 조건 /.test(s)),rows.join('\n'));
// Another group's trouble is told, not hidden behind the selected one.
rows=plain(labDraw({lab:lab({notes:[[T('다른 묶음 잡음 실험도 확인 필요: 실패 1','lvl_y')]]})}));a=labStart(rows);
assert(rows[a+3].startsWith('│ 다른 묶음 잡음 실험도'),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_an_experiment_failure_never_changes_the_frame_or_the_session_glyph(self):
        # The frame, the head's glyph and number, and the folded strip's rule and session row are the
        # session's own state (spec rule 25, 49 (c)); a failed or unconfirmed experiment stays inside
        # LAB (folded: its `!1 LAB` token at the end of the routes row).
        script = self.FIXTURE + self.LAB + r"""
const frame=v=>JSON.stringify(v.folded?[v.color,v.rows[0],v.rows[1]]:[v.color,v.rows[0],v.rows.map(r=>[r[0],r.at(-1)]),v.rows.at(-1)]);
for(const state of ['idle','working','blocked','unknown'])for(const folded of [false,true]){
 const labDraw=lab=>view.render({width:folded?120:40,height:folded?4:30,side:!folded,fill:!folded,folded,frame:3,model:{...head,tag:'[e9]',routes:[oneShot],route_count:1,lab},
  state,harness:'claude',configuration:{},questions:()=>[]});
 const none=labDraw(null),failed=labDraw(failedLab());
 assert.equal(frame(failed),frame(none),state+' '+folded);
 if(!folded)assert(plain(failed).some(s=>s.includes('실패 1')));
 else assert(plain(failed).some(s=>/ │ !1 LAB +$/.test(s))&&!plain(none).join('').includes('LAB')&&plain(failed)[1].includes('CTX ━━━━'),plain(failed).join('\n'));
}
"""
        self.node(script, "panel-view.cjs")

    def test_lab_reads_the_same_in_every_harness(self):
        script = self.FIXTURE + self.LAB + r"""
for(const open of [[],['lab:grp_1']])for(const folded of [false,true]){
 const labDraw=harness=>plain(view.render({width:folded?120:40,height:folded?4:50,side:!folded,fill:!folded,folded,model:{...head,lab:failedLab()},state:'idle',harness,
  configuration:{},expandedKeys:open,questions:()=>[]})).filter(s=>!s.startsWith('┌'));
 const claude=labDraw('claude');
 for(const harness of ['codex','opencode'])assert.deepEqual(labDraw(harness),claude,harness);
}
"""
        self.node(script, "panel-view.cjs")

    def test_the_outer_frame_carries_the_sessions_moving_state(self):
        # Spec E9 rule 47 (11-14: the outer frame instead of the head rule), rules 51, 52 and 25 as
        # it retunes them. Running (and while an answer is typed in), a bright arc goes round the
        # outer frame clockwise without rest: its head the brightest (the running frame's vanilla),
        # its tail four shades down into the neutral frame gray, a quarter of the frame's cells
        # long (it grows with the panel), once round every 60 frames (6 s) whatever the size; the
        # cells of the head's words are passed behind. Waiting, the whole frame breathes red every
        # 48 frames (4.8 s), three shades in 256 colors and six in 24-bit, one at a time. Idle and
        # an unknown state, rule motion off, mono, NO_COLOR, eight colors and the settings screen:
        # the still frame in the state's color. The head rule is the plain thin rule in every
        # state. Between frames only the frame's line cells (and the spinner glyph) change; the
        # host gets those cells, their paint at any frame and the next frame they change at.
        script = self.FIXTURE + r"""
const styles={dim:{dim:true},g_spin:{foreground:230},g_blocked:{foreground:217},g_work_off:{foreground:150,dim:true},
 tag_steward:{foreground:219,bold:true},tag_steward_dim:{foreground:219,dim:true}};
const run=(o={})=>view.render({width:o.width||40,height:o.height||40,side:o.side!==false,fill:o.side!==false,harness:o.harness||'claude',
 model:{...head,tag:'[e9]',routes:[],route_count:0,messages:[],styles,...(o.model||{})},state:o.state,frame:o.frame||0,since:o.since,
 configuration:o.cfg||{},background:o.background||'',questions:()=>[],...(o.extra||{})});
const text=r=>r.map(p=>p.text).join('');
assert.deepEqual(view.BORDER,{LAP:60,ARC:1/4,SHADES:4,BREATHE:48,BREATHE_STEPS:6,BREATHE_STEPS_256:3});
assert.deepEqual(view.FACES.dark.orbit,[230,187,144,101]);assert.deepEqual(view.FACES.light.orbit,[22,65,108,151]);
assert.deepEqual(view.FACES.dark.breath,[217,210,174]);assert.deepEqual(view.FACES.light.breath,[88,124,131]);
// Every cell of a painted panel, by row and column, with the part it stands in.
const grid=v=>v.rows.map(r=>{const out=[];for(const p of r)for(const ch of p.text){out.push({ch,p});if(cells(ch)>1)out.push(null);}return out;});
// The frame's cells clockwise from the top-left corner, as [row, column].
const around=(W,H)=>{const out=[];for(let x=0;x<W;x++)out.push([0,x]);for(let y=1;y<H-1;y++)out.push([y,W-1]);
 for(let x=W-1;x>=0;x--)out.push([H-1,x]);for(let y=H-2;y>0;y--)out.push([y,0]);return out;};
// Each frame cell's look: 'text' (the head's words), 'base' (the neutral frame), or its arc shade 0..3.
const ramp=view.FACES.dark.orbit;
const looks=v=>{const g=grid(v),W=g[0].length,H=g.length;return around(W,H).map(([y,x])=>{const c=g[y][x];
 if(!'┌┐└┘─│'.includes(c.ch))return 'text';
 if(c.p.role==='frame')return 'base';
 assert(c.p.role==='orbit'&&ramp.includes(c.p.foreground)&&c.p.dim!==true,JSON.stringify(c.p));return ramp.indexOf(c.p.foreground);});};
const lit=l=>l.map((x,i)=>typeof x==='number'?i:-1).filter(i=>i>=0);
// The arc's head: a brightest cell whose next cell clockwise is no arc cell (null while it is behind the head's words).
const headOf=l=>{const n=l.length,h=l.findIndex((x,i)=>x===0&&l[(i+1)%n]==='base');return h<0?null:h;};
// (a) Running: every frame shows one arc, a quarter of the frame long, its head the brightest and
// four shades down to its tail; behind it the neutral frame. One lap is 60 frames for every size.
for(const [o,W,H] of [[{},40,40],[{width:60},60,40],[{side:false,width:100,height:14},100,0]]){
 const rowsOf=run({...o,state:'working'}).rows.length,P=2*W+2*((H||rowsOf)-2),arc=Math.round(P/4),heads=[];
 for(let f=0;f<=120;f++){
  const v=run({...o,state:'working',frame:f}),l=looks(v);assert.equal(l.length,P);
  // The glyphs stay the frame's own on every frame.
  const g=grid(v);assert(g[0][0].ch==='┌'&&g[0].at(-1).ch==='┐'&&g.at(-1)[0].ch==='└'&&g.at(-1).at(-1).ch==='┘',String(f));
  const on=lit(l),h=headOf(l);heads.push(h);
  if(h!==null&&!l.slice(0).some((x,i)=>x==='text'&&((h-i+P)%P)<arc)){
   // Walking back from the head: shades 0, 1, 2, 3 in order, each a quarter of the arc (within a cell), then the neutral frame.
   const back=Array.from({length:arc+1},(_,k)=>l[(h-k+P)%P]);
   assert.equal(back[arc],'base',f+' '+back.join(''));
   for(let k=1;k<arc;k++)assert(back[k]>=back[k-1]&&back[k]-back[k-1]<=1,f+' '+back.join(''));
   for(let s=0;s<4;s++){const n=back.filter(x=>x===s).length;assert(Math.abs(n-arc/4)<=1,f+' '+s+' '+back.join(''));}
   assert.equal(on.length,arc,f+' '+P);
  }
  assert(on.length<=arc&&on.length>=arc-15,f+' '+on.length);
 }
 // The head moves on clockwise every frame by the same step (within a cell), never back, and
 // comes round to where it was after exactly 60 frames; half a lap later it is half the frame on.
 const steps=[];for(let f=0;f<120;f++)if(heads[f]!==null&&heads[f+1]!==null)steps.push((heads[f+1]-heads[f]+P)%P);
 assert(steps.length>60&&steps.every(s=>s>=Math.floor(P/60)&&s<=Math.ceil(P/60)),JSON.stringify(steps));
 for(let f=0;f<60;f++)assert.deepEqual(run({...o,state:'working',frame:f}).rows,run({...o,state:'working',frame:f+60}).rows,String(f));
 const half=[0,10,20].find(f=>heads[f]!==null&&heads[f+30]!==null);assert(Math.abs((heads[half+30]-heads[half]+P)%P-P/2)<=1,String(half));
 // (b) One passage a lap: each cell lights once, brightest first, then steps down to the neutral
 // frame, in 256 colors too (a turning arc, never a blink).
 const seq=Array.from({length:60},(_,f)=>looks(run({...o,state:'working',frame:f})));
 for(let i=0;i<P;i++){if(seq[0][i]==='text')continue;
  const s=seq.map(l=>l[i]);const from=s.findIndex((x,k)=>x==='base'&&s[(k+1)%60]!=='base');
  const turn=[...s.slice(from+1),...s.slice(0,from+1)];const end=turn.indexOf('base');
  assert(end>0&&turn.slice(end).every(x=>x==='base'),i+' '+s.join(''));
  for(let k=1;k<end;k++)assert(turn[k]>=turn[k-1],i+' '+s.join(''));
  assert(turn[0]===0&&turn[end-1]===3,i+' '+s.join(''));}
}
// (c) The arc starts with the state: at `since` its head is the first line cell right of the head's words.
for(const since of [0,7,59,1000]){const v=run({state:'working',frame:since,since}),l=looks(v);
 const words=text(v.rows[0]).indexOf(' ─')+1;assert.equal(headOf(l),words,since+' '+text(v.rows[0]));
 assert.deepEqual(looks(run({state:'working',frame:since+13,since})),looks(run({state:'working',frame:13})),'the same arc, '+since+' frames on');}
// (d) The host's cells: the frame's line cells (never the head's words), their paint at any frame as painted, the next change.
const v3=run({state:'working',frame:3});
assert(v3.animated&&!v3.blinks&&v3.motion&&v3.motion.cells.length===156-text(v3.rows[0]).indexOf(' ─'));
{const g=grid(v3);for(const c of v3.motion.cells)assert.equal(g[c.row][c.col].ch,c.ch,JSON.stringify(c));
 assert(!v3.motion.cells.some(c=>c.row===0&&c.col>0&&c.col<text(v3.rows[0]).indexOf(' ─')+1));
 for(const f of [3,17,30,59]){const g=grid(run({state:'working',frame:f}));
  v3.motion.at(f).forEach((p,i)=>{const c=v3.motion.cells[i],q=g[c.row][c.col].p;assert(p.text===c.ch&&p.foreground===q.foreground&&!!p.dim===!!q.dim,f+' '+i);});}}
for(const f of [0,3,59,60,1000])assert.equal(v3.motion.next(f),f+1);
// A small frame (under 60 cells) wakes only on the frames its head moves, once a cell.
{const s=run({side:false,width:20,height:4,state:'working',frame:0});const P=2*20+2*2;assert(s.motion);
 let t=0,wakes=0;while(t<60){const n=s.motion.next(t);assert(n>t);for(let k=t+1;k<n;k++)assert.deepEqual(s.motion.at(k),s.motion.at(t),String(k));t=n;wakes++;}
 assert.equal(wakes,P);}
// (e) Between two frames only the frame's line cells and the spinner glyph change; the head rule never moves.
const ruleRow=v=>v.rows.findIndex((r,i)=>i>0&&/^│ ─+ │$/.test(text(r)));
for(const state of ['working','submitting','blocked']){
 const a=run({state,frame:5}),b=run({state,frame:state==='blocked'?17:6}),ga=grid(a),gb=grid(b),mc=new Set(a.motion.cells.map(c=>c.row+','+c.col));
 ga.forEach((r,y)=>r.forEach((c,x)=>{if(!c)return;if(mc.has(y+','+x)||c.p.spin)return;assert.deepEqual(c,gb[y][x],state+' '+y+','+x);}));
 const i=ruleRow(a);assert(i>0,state);
 for(const v of [a,b]){const parts=v.rows[i].slice(2,-2);
  assert(parts.length===1&&parts[0].text==='─'.repeat(36)&&parts[0].role==='messages'&&parts[0].dim===true&&!parts[0].motion&&!parts[0].tone,state+' '+JSON.stringify(parts));}
}
// (f) Waiting: the whole frame breathes, one red at a time on every line cell; three shades in
// 256 colors, six in 24-bit; it starts at its brightest (the frame's waiting red) and the host
// wakes only at its shade changes.
for(const [colors,count] of [[256,3],[1<<24,6]]){
 const seq=[],seen=[];
 for(let f=0;f<48;f++){const v=run({state:'blocked',frame:f,since:0,model:{colors}}),g=grid(v);
  const tones=v.motion.cells.map(c=>g[c.row][c.col].p.foreground);
  assert(tones.every(t=>t===tones[0])&&v.motion.cells.every(c=>g[c.row][c.col].p.role==='orbit'),f+' '+colors);
  seq.push(g[0][0].p.step);if(!seen.includes(tones[0]))seen.push(tones[0]);}
 assert.equal(seen.length,count,colors+' '+JSON.stringify(seen));assert.equal(seen[0],colors>256?'#ffafaf':217);
 if(colors===256)assert.deepEqual(seen,view.FACES.dark.breath);
 for(let f=1;f<=48;f++)assert(Math.abs(seq[f%48]-seq[f-1])<=1,colors+' '+seq.join(','));
 assert.equal(seq[0],0);assert.equal(Math.max(...seq),count-1);
 const runs=[];let n=1;for(let f=1;f<48;f++){if(seq[f]===seq[f-1])n++;else{runs.push(n);n=1;}}
 assert(Math.max(...runs.slice(1))-Math.min(...runs.slice(1))<=1,colors+' '+JSON.stringify(runs));
 const w=run({state:'blocked',frame:0,since:0,model:{colors}});let t=0,wakes=0;while(t<48){t=w.motion.next(t);if(t<=48)wakes++;}
 assert.equal(wakes,2*(count-1),colors+' wakes');
 assert.deepEqual(run({state:'blocked',frame:5,since:0,model:{colors}}).rows,run({state:'blocked',frame:53,since:0,model:{colors}}).rows);
}
// The steward `⚑` keeps its own blink.
for(const [f,on] of [[0,true],[10,false]]){const v=run({state:'blocked',frame:f,model:{steward:true}});
 assert.equal(v.rows[0].find(p=>p.text==='⚑').bold===true,on);assert(v.blinks);}
// (g) Still: idle and an unknown state keep the neutral frame; rule motion off, mono, NO_COLOR,
// eight colors and the settings screen keep the frame in the state's color (running 230, waiting
// 217 where color shows). Nothing moves, nothing keeps the beat for the frame.
const frameOf=v=>{const g=grid(v);return around(g[0].length,g.length).map(([y,x])=>g[y][x]).filter(c=>'┌┐└┘─│'.includes(c.ch));};
for(const state of ['idle','unknown',undefined]){
 const v=run({state,frame:7});assert.equal(v.motion,null);assert.equal(v.animated,false,String(state));
 assert(frameOf(v).every(c=>c.p.role==='frame'&&c.p.dim===true),String(state));
 assert.deepEqual(v.rows,run({state,frame:31}).rows);}
for(const [cfg,model,extra] of [[{rule_motion:'off'},{}],[{theme:'mono'},{}],[{},{no_color:true}],[{},{colors:8}],[{},{},{settings:{index:0,value:{...view.defaults}}}]]){
 for(const [state,want] of [['working',230],['blocked',217]]){
  const a=run({state,frame:7,cfg,model,extra}),b=run({state,frame:37,cfg,model,extra});
  assert.equal(a.motion,null,JSON.stringify([cfg,model,state]));assert.deepEqual(a.rows,b.rows,JSON.stringify([cfg,model,state]));
  assert(!a.rows.flat().some(p=>p.role==='orbit'||p.tone),JSON.stringify(cfg));
  if(cfg.rule_motion||model.colors)assert(frameOf(a).every(c=>c.p.foreground===want),JSON.stringify([cfg,model,state,frameOf(a)[3]]));
 }}
// (h) A known face takes its ramps: the dark face's vanilla arc over its frame gray (244), the light
// face's deep green over 243; a waiting frame starts at the face's waiting red.
for(const [o,face] of [[{background:'dark'},'dark'],[{cfg:{theme:'light'}},'light']]){
 const v=run({...o,state:'working',frame:8}),cs=frameOf(v);
 assert(cs.filter(c=>c.p.role==='frame').every(c=>c.p.foreground===view.FACES[face].frame&&c.p.dim===false),face);
 const arc=cs.filter(c=>c.p.role==='orbit');assert(arc.length>20&&arc.every(c=>view.FACES[face].orbit.includes(c.p.foreground))&&arc.some(c=>c.p.foreground===view.FACES[face].orbit[0]),face);
 assert.equal(frameOf(run({...o,state:'blocked',frame:0,since:0}))[0].p.foreground,view.FACES[face].breath[0],face);
}
// (i) Claude, Codex and OpenCode draw the same frame on the same frame (one renderer, one beat):
// the same content and breath; the same arc, each starting right after its own head's words.
const startOf=v=>text(v.rows[0]).indexOf(' ─')+1;
for(const [state,f] of [['working',5],['blocked',10]]){const all=['claude','codex','opencode'].map(harness=>run({harness,state,frame:f}));
 for(const v of all.slice(1)){
  assert.deepEqual(v.rows.slice(1,-1).map(r=>r.slice(1,-1)),all[0].rows.slice(1,-1).map(r=>r.slice(1,-1)),state);
  if(state==='blocked'){assert.deepEqual(v.rows.slice(1),all[0].rows.slice(1),state);continue;}
  const d=startOf(v)-startOf(all[0]),a=looks(v),b=looks(all[0]),P=a.length;assert(d!==0);
  a.forEach((x,i)=>{const y=b[((i-d)%P+P)%P];if(x!=='text'&&y!=='text')assert.equal(x,y,state+' '+i);});}}
// (j) Short of rows (the rule goes, spec rule 34 (c)) the frame keeps moving; the bottom layout moves alike.
for(let h=30;h>=8;h-=4){const v=run({state:'working',frame:7,height:h});assert(v.motion&&v.motion.cells.length===2*40+2*(h-2)-text(v.rows[0]).indexOf(' ─'),String(h));}
{const b=run({side:false,width:100,height:14,state:'working',frame:7});assert(b.motion&&b.motion.cells.some(c=>c.row===b.rows.length-1)&&b.motion.cells.some(c=>c.col===99));}
// The setting: on by default, `rule_motion` beside `route_line`, `on`/`off`.
assert.equal(view.defaults.rule_motion,'on');assert.deepEqual(view.options.rule_motion,['on','off']);
assert.equal(view.change(view.defaults,'rule_motion',1).rule_motion,'off');
const i=view.fields.indexOf('rule_motion');assert(i>view.fields.indexOf('route_line'));
const settings=view.render({width:40,height:24,side:true,model:{labels:{rule_motion:'rule motion'}},settings:{index:i,value:{...view.defaults}},questions:()=>[]});
assert(text(settings.rows.flat()).includes('rule motion  ‹ on ›')&&settings.hits.some(h=>h.action==='setting:'+i+':up'));
"""
        self.node(script, "panel-view.cjs")

    def test_owners_and_their_workers_fold_together_and_every_route_takes_the_same_step(self):
        # Spec E9 rule 48 (c) (it replaces the #1579 review follow-up (가)): short of rows (1) every
        # owner's context folds into its first row and, in the same step, its workers take the
        # one-row form two cells in (percent and time in the owners' columns), (2) the workers go
        # and the kind row counts them, (3) the routes take one row. Never an owner in its whole
        # form with a worker on one row, or the reverse; every route at the same step. The
        # reviewer's case: 38 cells, heights 23-29, one staged route with a worker and three without.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,e,pct,bar)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)]});
const solo={id:'s',title:'셸 상태·표시 고치기',kind:'working',state:'working',elapsed:'41m',shape:'one-shot-owner',kind_word:'code',role:'owner',tool:'Edit',
 stages:[{name:'one-shot',current:true,mark:'●'}],paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','38%','━━━')},workers_live:[]};
const worker=(h,m,pct,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',tool:'Edit',...o,paint:{glyph:P('⠋'),...doer(h,m,'high',pct,'━━')}});
const one={id:'w',title:'속도 3단계 — 남은 재계산',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'worker',doer_worker:true,
 paint:{glyph:P('⠋'),...doer('claude','Sonnet 5.5','high','22%','━━')},owner_paint:doer('claude','Opus 5.5','xhigh','52%','━━━━'),workers_live:[worker('claude','Sonnet 5.5','22%')]};
const two={...one,id:'t',title:'검토 두 갈래',workers_live:[worker('codex','gpt-5.5','34%'),worker('opencode','glm-5.2','71%',{elapsed:'8m'})]};
const reviewer=[one,...[1,2,3].map(i=>({...solo,id:'s'+i}))];
const order=['box','one','gone','flat'];let checked=0;
for(const [width,routes,model] of [[38,reviewer,{}],[38,reviewer,{messages:[],wait:'',now:''}],[40,reviewer,{}],[40,[two,solo],{}],[34,[one,two],{}],[32,[one],{}]]){
 const seen=[];
 for(let h=40;h>=12;h--){
  const rows=view.render({width,height:h,side:true,fill:true,model:{...head,...model,routes,route_count:routes.length},state:'working',configuration:{},questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
  const all=rows.join('\n'),ctx=rows.filter(s=>/^│ │   CTX /.test(s)).length,owners=rows.filter(s=>/^│ │ [⠋●] owner /.test(s)).length;
  const inBox=rows.some(s=>/^│ │   [╭├]─ [⠋●] worker/.test(s)),oneRow=rows.filter(s=>/^│ │   [⠋●] worker /.test(s)),counted=/· worker \d/.test(all);
  const state=inBox?'box':oneRow.length?'one':counted?'gone':owners?'':'flat';
  const where=width+'x'+h+'\n'+all;
  // Never a middle form: the box only with every owner's context row, the one-row workers only without any.
  if(inBox)assert(ctx>0&&ctx===owners&&!oneRow.length&&!counted,where);
  if(oneRow.length)assert(ctx===0&&!inBox&&!counted,where);
  if(counted)assert(ctx===0&&!inBox&&!oneRow.length,where);
  if(ctx)assert.equal(ctx,owners,where);
  // The one-row worker: two cells in, its percent and time in the owners' columns.
  for(const s of oneRow){const owner=rows.find(r=>/^│ │ [⠋●] owner .*%/.test(r));if(owner&&/%/.test(s))assert.equal(s.lastIndexOf('%'),owner.lastIndexOf('%'),where);}
  if(state&&!seen.includes(state))seen.push(state);checked++;
 }
 assert.deepEqual(seen,order.filter(x=>seen.includes(x)),width+' '+JSON.stringify(seen));
 assert(seen.includes('box')&&seen.includes('one'),width+' '+JSON.stringify(seen));
}
assert(checked>0);
"""
        self.node(script, "panel-view.cjs")

    def node(self, script, name):
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell" / name)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mouse_packets_do_not_rewrite_typing_or_pasted_mouse_text(self):
        script = r'''
const assert=require('node:assert/strict'),decode=require(process.argv[1]);let output='',events=[],paste=false;
const d=decode({bytes:b=>{output+=b.toString();if(output.endsWith('\x1b[200~'))paste=true;},
 mouse:(...event)=>events.push(event.slice(0,4)),suppressed:()=>paste});
d.feed(Buffer.from('draft'));assert.equal(output,'draft');
d.feed(Buffer.from('\x1b'));d.feed(Buffer.from('[<0;'));d.feed(Buffer.from('95;1Mtail'));
assert.equal(output,'drafttail');assert.deepEqual(events,[[0,95,1,false]]);
d.feed(Buffer.from('\x1b[200~'));d.feed(Buffer.from('literal\x1b[<0;95;1M\x1b[201~'));
assert.equal(events.length,1);assert(output.includes('literal\x1b[<0;95;1M'));
paste=false;d.feed(Buffer.from('\x1b'));d.flush();assert(output.endsWith('\x1b'));
'''
        self.node(script, "panel-mouse.cjs")

    def test_settings_controls_render_and_edit_the_same_width_in_every_harness(self):
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
for(const harness of ['claude','codex','opencode']){
 const settings={index:1,value:{...v.defaults}};
 const r=v.render({width:40,height:24,side:true,harness,settings,questions:()=>[]});
 assert(r.rows.flat().map(s=>s.text).join('').includes('Hearting shell settings'));
 assert(r.hits.some(h=>h.action==='apply'));assert(r.hits.some(h=>h.action==='cancel'));assert(r.hits.some(h=>h.action==='defaults'));
 const width=v.fields.indexOf('side_width');
 assert(r.hits.some(h=>h.action==='setting:'+width+':up'));
 for(const key of ['position','theme','shade'])assert(r.hits.some(h=>h.action==='setting:'+v.fields.indexOf(key)+':up'),key);
 // Spec rule 49 (d): the fold judgment is one constant; the editor has no threshold.
 assert(!v.fields.includes('side_min_columns')&&v.defaults.side_min_columns===100);
}
'''
        self.node(script, "panel-view.cjs")

    def test_editing_budget_keeps_the_draft_visible_in_a_narrow_side_panel_and_80_columns(self):
        script = r'''
const assert=require('node:assert/strict'),path=require('node:path'),v=require(process.argv[1]);
const c=require(path.join(path.dirname(process.argv[1]),'block-input.cjs'))({changed(){},answer(){throw Error('no submit');},action(){}});
c.update([{id:'exact',revision:'one',answerable:true,questions:[{id:'q',question:'Where should this result go?',choices:['Here','Another folder'],display:{head:[{text:'Question · answer needed'}],body:[[{text:'Where should this result go?'}]],options:[[{text:'1 Here · 2 Another folder'}]],source:[[{text:'this session'}]]}}]}]);
c.enter();c.input(Buffer.from('tDraft answer'));
const model={answerable:true,title:'Confirmed task',routes:[{title:'Confirmed task',state:'working',stage:'review',stages:[{name:'prepare'},{name:'review'}],reason:'Review this result together'}]};
for(const [width,height,side] of [[37,21,true],[80,12,false]]){
 const r=v.render({width,height,side,focused:true,model,configuration:{},questions:o=>c.detail(o)});
 const text=r.rows.map(row=>row.map(p=>p.text).join('')).join('\n');
 assert(text.includes('Draft answer'),text);assert(text.includes('Tab next'),text);
}
'''
        self.node(script, "panel-view.cjs")

    def test_settings_apply_preserves_enablement_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory}
            path = shell.policy_path(env)
            path.parent.mkdir()
            original = b'{ "enabled": true, "layout": "bottom" }\n'
            path.write_bytes(original)
            path.chmod(0o600)
            desired = {**shell.DISPLAY_DEFAULTS, "side_width": 48}
            self.assertEqual(shell.save_display_policy(desired, env), desired)
            self.assertIs(json.loads(path.read_text())["enabled"], True)
            self.assertEqual(next(path.parent.glob("*.backup-*")).read_bytes(), original)
            self.assertEqual(shell.display_policy(env)["side_width"], 48)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                shell.save_display_policy({**desired, "model": "not-shell-config"}, env)
            self.assertEqual(path.read_bytes(), before)

    def test_appearance_settings_fall_back_per_key_and_never_disable_the_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory}
            path = shell.policy_path(env)
            path.parent.mkdir()
            path.write_text(json.dumps({"enabled": True, "side_width": 50, "position": "top",
                                        "theme": 7, "shade": "medium"}))
            path.chmod(0o600)
            self.assertTrue(shell.policy(env)[0])
            value = shell.display_policy(env)
            self.assertEqual((value["side_width"], value["position"], value["theme"], value["shade"]),
                             (50, "right", "terminal", "medium"))
            path.write_text(json.dumps({"enabled": True}))
            desired = {**shell.DISPLAY_DEFAULTS, "position": "left", "theme": "light", "shade": "light"}
            self.assertEqual(shell.save_display_policy(desired, env), desired)
            self.assertEqual({key: shell.display_policy(env)[key] for key in shell.APPEARANCE},
                             {"position": "left", "theme": "light", "shade": "light"})
            before = path.read_bytes()
            for key, bad in (("position", "top"), ("theme", "neon"), ("shade", "dark")):
                with self.assertRaises(ValueError):
                    shell.save_display_policy({**desired, key: bad}, env)
            self.assertEqual(path.read_bytes(), before)

    def test_the_session_state_keeps_fleet_session_keys_on_every_face(self):
        # Review RV-PR1528 §4: the session's running is g_spin and idle g_work_off;
        # g_work/g_idle (150/186 on a shade) belong to the route and stage marks.
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
const styles={g_spin:{foreground:230},g_work_off:{foreground:150,dim:true},g_blocked:{foreground:174},g_work:{foreground:150,bold:true},g_idle:{foreground:186}};
const model={styles,colors:256,route_count:1,routes:[{id:'r',title:'R',state:'working',kind:'working',paint:{glyph:{text:'⠏',foreground:150}}}]};
const head=(state,cfg,background)=>v.render({width:40,height:16,side:true,state,model,configuration:cfg,background,questions:()=>[]}).rows[0].slice(1,4);
for(const [cfg,background,spin,idle] of [[{},'',230,150],[{shade:'medium'},'dark',230,150],[{theme:'light'},'',22,22],[{theme:'light',shade:'light'},'',22,22]]){
 const label=JSON.stringify([cfg,background]);
 assert.equal(head('working',cfg,background)[1].foreground,spin,label);assert.equal(head('idle',cfg,background)[1].foreground,idle,label);
 assert.notEqual(head('idle',cfg,background)[1].foreground,v.FACES.dark.idle,label);
}
// The route's own mark keeps the route state color on a shade.
const r=v.render({width:40,height:16,side:true,state:'idle',model,configuration:{shade:'medium'},background:'dark',questions:()=>[]});
assert(r.rows.slice(1).flat().some(x=>x.role==='state:working'&&x.foreground===v.FACES.dark.working));
'''
        self.node(script, "panel-view.cjs")

    def test_theme_and_shade_keep_glyphs_and_leave_the_terminal_theme_unchanged(self):
        # Spec rule 22 (the reviewed set A, the question rose one step deeper).
        script = r'''
const assert=require('node:assert/strict'),v=require(process.argv[1]);
const styles={dim:{foreground:-1,dim:true},loc_repo:{foreground:253},lvl_y:{foreground:186},g_work:{foreground:150,bold:true},lvl_g:{foreground:150},g_dead:{foreground:217,bold:true}};
const model={styles,colors:256,title:'Task',answerable:true,routes:[{id:'t',title:'Task',state:'working',kind:'working',paint:{glyph:{text:'⠏'}},stages:[{name:'execute',current:true,mark:'●'}]}],
 route_count:1,routes_known:true,messages:[{way:'←',who:'[e7]',text:'note',at:1}]};
const q=()=>[[{text:'◑ questions 1'}],[{text:'Which?'}],[{text:'›'},{text:' A',bold:true}]];
const paint=(cfg,background,extra={})=>v.render({width:40,height:24,side:true,model:{...model,...extra},configuration:cfg,background,questions:q});
const text=r=>r.rows.map(row=>row.map(p=>p.text).join('')).join('\n');
const base=paint({});
assert.deepEqual(paint({theme:'terminal',shade:'none'}).rows,base.rows);
const F=v.FACES;
for(const background of ['','dark','light'])for(const theme of ['terminal','light','mono'])for(const shade of ['none','light','medium']){
 const r=paint({theme,shade},background),parts=r.rows.flat(),label=[background,theme,shade].join('/');
 // Words and glyphs never change with color; only paint does.
 assert.equal(text(r),text(base),label);
 const face=theme==='light'?'light':background;
 // A shade needs a known face: `terminal` and `mono` on an unknown background keep the terminal's own.
 if(shade==='none'||!face){assert(parts.every(p=>p.background===undefined),label);continue;}
 const f=F[face];
 assert(parts.every(p=>p.background===f[shade]&&Number.isInteger(p.foreground)),label);
 // Nothing is dimmed on top of a shade: secondary text, messages and lines take their own color.
 assert(!parts.some(p=>p.dim&&[f.body,f.secondary,f.frame,f.route,f.messages,f.title].includes(p.foreground)),label);
 const at=ch=>parts.find(p=>p.text.startsWith(ch));
 // Spec rule 31: a message's text is body text; its head's own values are secondary.
 assert.equal(at('┌').foreground,f.frame,label);assert.equal(r.rows.flat().find(p=>p.text==='note').foreground,f.body,label);
 assert(parts.some(p=>p.text.includes('MESSAGES')&&p.foreground===f.title),label);
 if(theme!=='mono'){
  // The question's border, `◑` and `›` are rose; its words are body text.
  assert(parts.some(p=>p.text==='◑'&&p.foreground===f.accent)&&parts.some(p=>p.text==='›'&&p.foreground===f.accent),label);
  assert.equal(parts.find(p=>p.text==='Which?').foreground,f.body,label);
 }
 if(face==='light')assert(!parts.some(p=>[253,186,150,217].includes(p.foreground)),label);
}
// The light theme without a shade: the reviewed deep hues, secondary text without a dim.
const light=paint({theme:'light'}).rows.flat();
assert(!light.some(p=>[253,186,150,217,28,124,91].includes(p.foreground)));assert.equal(light.find(p=>p.text==='note').foreground,237);
// The question rose on the terminal's own background too; mono keeps it by weight.
assert(base.rows.flat().some(p=>p.text==='◑'&&p.foreground===174));
assert(paint({theme:'mono'}).rows.flat().find(p=>p.text==='◑').bold===true);
// Shade needs 256 colors and is off under NO_COLOR; an unknown theme is the default.
for(const extra of [{colors:8},{no_color:true}])assert(paint({shade:'medium'},'dark',extra).rows.flat().every(p=>p.background===undefined));
assert.deepEqual(paint({theme:'neon'}).rows,base.rows);
// Every harness, OpenCode included, gets the chosen paint on the common panel.
for(const harness of ['claude','codex','opencode'])
 assert(v.render({width:40,height:12,side:true,model,harness,background:'dark',configuration:{theme:'mono',shade:'medium'},questions:()=>[]}).rows.flat().every(p=>p.background===235),harness);
'''
        self.node(script, "panel-view.cjs")

    def test_position_places_one_side_panel_left_or_right_for_every_harness(self):
        script = r'''
const assert=require('node:assert/strict'),d=require(process.argv[1]);
const cfg={layout:'side',side_width:40,side_min_columns:100};
const right=d.layout({cols:133,rows:30,configuration:cfg});
assert.deepEqual([right.side,right.left,right.panelColumn,right.nativeColumn,right.nativeCols],[true,false,93,0,91]);
const left=d.layout({cols:133,rows:30,configuration:{...cfg,position:'left'}});
assert.deepEqual([left.side,left.left,left.panelColumn,left.nativeColumn,left.nativeCols],[true,true,0,42,91]);
for(const position of ['top',undefined,'right'])assert.equal(d.layout({cols:133,rows:30,configuration:{...cfg,position}}).left,false);
// Narrow windows keep the bottom panel; position does not apply.
assert.equal(d.layout({cols:80,rows:24,configuration:{...cfg,position:'left'}}).left,false);
'''
        self.node(script, "native-display.cjs")


class RequestPanelTest(unittest.TestCase):
    """R27 in the route box: sent and received requests stand beside the routes, open in
    place, read whole on their own page, and leave the question, messages and draft alone."""

    HEAD = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const q=(id,glyph,who,title,status,kind,age='3m')=>({id,way:glyph==='◂'?'◂':'→',glyph,who,title,status,age,kind,
 details:[['','e9 → e7 · '+id,'dim'],['판','2 head d2d92ec · 1 대체됨','loc_repo'],['전달','전달됨','dim'],['일','열림','loc_repo'],
  ['변화','3m','dim'],['보고','1 일부 · 1(대체됨) · '+'긴결과 '.repeat(6),'dim']],
 body:['BODY_NONCE 첫 줄','둘째 줄','https://example.com/'+'x'.repeat(60)],body_label:'본문'});
const requests=[q('rq-aaaaaa','◂','[e7]','같은 시각 요청 A 한글 제목','열림','request-in'),
 q('rq-bbbbbb','→','[52]','같은 시각 요청 B','전달됨','request-out'),q('rq-cccccc','✓','[e1]','끝난 일','최종 답','request-done')];
const routes=[{id:'rt-a',title:'Actual route',state:'working',kind:'working',paint:{glyph:{text:'⠏'}}}];
const base={route_count:1,routes,requests,messages:[{way:'←',who:'[e7]',text:'MSG_KEPT',when:'1m',kind:'steer',harness:'claude'}],styles:{dim:{dim:true}}};
const text=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const render=(width,height,extra={},model=base)=>view.render({width,height,side:width<80,model,sid:'x',configuration:{},questions:()=>[],...extra});
"""

    def node(self, body):
        result = subprocess.run(["node", "-e", self.HEAD + body, str(ROOT / "utilities/session-shell/panel-view.cjs")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_title_counts_routes_then_requests_and_no_hourglass_stands_anywhere(self):
        self.node(r"""
for(const [width,height] of [[32,30],[37,30],[38,30],[80,24]]){
 const lines=text(render(width,height));
 assert(lines.some(s=>s.includes(width<80?'1 ROUTE │ 3 REQUEST':'route 1 │ request 3')),lines.join('\n'));
 for(const mark of ['◂','→','✓'])assert(lines.some(s=>s.includes(mark)),mark+width);
 assert(!lines.join('').includes('⏳')&&!lines.join('').includes('⌛'));
 assert(lines.some(s=>s.includes('MSG_KEPT')),'messages stay at '+width);
 assert(lines.length<=height);
}
const none=text(render(37,30,{},{...base,requests:[]}));
assert(none.some(s=>s.includes('1 ROUTE'))&&!none.join('').includes('REQUEST'));
// Spec rules 57, 58: `REQUEST` is a dark gray name tag (as MESSAGES and MEM), never the route
// count's state color.
const styled={...base,routes:[{...routes[0],state:'idle',kind:'idle'}],styles:{dim:{dim:true},g_idle:{fg:186}}};
for(const width of [32,38]){
 const parts=render(width,30,{},styled).rows.flat();
 const tag=parts.find(p=>/^\d+ REQUEST$/.test(p.text));
 assert(tag&&tag.dim===true&&tag.fg===undefined,JSON.stringify(tag));
 const count=parts.find(p=>/^\d+ ROUTE$/.test(p.text));
 assert(count&&count.fg===186,JSON.stringify(count));
}
""")

    def test_a_long_status_cuts_the_title_first_and_keeps_the_other_session(self):
        # r3: `◂ 검토 제목 1c… · partial answer` lost its `[6e]`; the title is what gives way.
        self.node(r"""
for(const width of [32,37,38])for(const status of ['partial answer','final answer','open'])for(const title of ['검토 제목 1c9036d9','review title 1c9036d9'])for(const [glyph,kind] of [['◂','request-in'],['✓','request-done']]){
 const model={...base,requests:[q('rq-aaaaaa',glyph,'[6e]',title,status,kind)]};
 const lines=text(render(width,30,{},model));
 const row=lines.find(s=>s.includes(glyph)&&s.includes('· '+status));
 assert(row,'the row stands: '+width+status+'\n'+lines.join('\n'));
 assert(row.includes('[6e]'),'the other session stays: '+row);
 if(width>=37||status==='open')assert(row.includes('…')||row.includes(title),'the title is cut with an ellipsis: '+row);
}
// A title that fits stays whole beside the session and the status.
const whole=text(render(38,30,{},{...base,requests:[q('rq-aaaaaa','◂','[6e]','짧은 일','final answer','request-in')]}));
assert(whole.some(s=>s.includes('[6e] 짧은 일 · final answer')),whole.join('\n'));
""")

    def test_a_request_opens_in_place_whole_then_its_overflow_leads_to_the_read_page(self):
        self.node(r"""
for(const width of [32,37,38]){
 const open=render(width,40,{expandedKeys:['request:rq-aaaaaa']});
 const lines=text(open).join('\n');
 assert(lines.includes('rq-aaaaaa'),'the number is in the detail');
 assert(lines.includes('+ 본문 ›'));
 assert(open.hits.some(h=>h.action==='read:request:rq-aaaaaa'));
 // Two requests of the same time keep distinct, whole numbers when both open.
 const both=text(render(width,60,{expandedKeys:['request:rq-aaaaaa','request:rq-bbbbbb']})).join('\n');
 assert(both.includes('rq-aaaaaa')&&both.includes('rq-bbbbbb'));
 // Short of rows the detail keeps whole items and a `+N lines ›` row to the page.
 const short=render(width,22,{expandedKeys:['request:rq-aaaaaa']});
 assert(short.hits.some(h=>h.action==='read:request:rq-aaaaaa'));
 assert(text(short).some(s=>s.includes('MSG_KEPT')),'the messages are kept');
}
""")

    def test_the_read_page_carries_every_value_and_the_body_and_back_returns(self):
        self.node(r"""
for(const [width,height] of [[32,26],[38,26],[80,24]]){
 const parts=[];let pages=1;
 for(let page=0;page<pages;page++){
  const v=render(width,height,{routeDetail:'request:rq-aaaaaa',routePage:page});
  pages=v.routePages;parts.push(text(v).join('\n'));assert(v.rows.length<=height);
  assert(text(v)[1].toUpperCase().includes('REQUEST')||text(v)[0].toUpperCase().includes('REQUEST'));
 }
 const all=parts.join('\n').replace(/[│┌┐└┘\s]/g,'');
 for(const needle of ['rq-aaaaaa','BODY_NONCE','1(대체됨)','전달됨','둘째줄'])assert(all.includes(needle),needle+' at '+width);
}
// A request that no longer exists closes its page instead of showing nothing.
const gone=render(37,24,{routeDetail:'request:rq-zzzzzz'});
assert.equal(gone.routeDetail,false);
""")

    def test_the_question_box_and_messages_keep_their_rows_when_requests_open(self):
        self.node(r"""
const asking={...base,answerable:true};
for(const [width,height] of [[37,26],[80,24]]){
 const v=render(width,height,{expandedKeys:['request:rq-aaaaaa','request:rq-bbbbbb'],
  questions:()=>[[{text:'◑ Where should this go?'}],[{text:'1 · Current project'}],[{text:'Ctrl+] answer'}]]},asking);
 const lines=text(v);
 assert(lines.some(s=>s.includes('Where should this go?')),'question at '+width);
 assert(lines.some(s=>s.includes('Current project')));
 assert(lines.some(s=>s.includes('MSG_KEPT')),'messages at '+width);
 assert(lines.length<=height);
}
""")

    def test_short_of_rows_the_box_scrolls_and_puts_work_to_answer_first(self):
        self.node(r"""
const many=Array.from({length:9},(_,i)=>q('rq-00000'+i,i===5?'◂':'→','[e'+i+']','일 '+i,'전달됨',i===5?'request-in':'request-out'));
const v=render(37,16,{},{...base,requests:many});
const lines=text(v);
assert(lines.some(s=>s.includes('↓')||s.includes('↑')),'the border counts the rows it hides');
assert(v.hits.some(h=>h.action==='request:rq-000005'),'the received work to answer is among the first shown');
assert(lines.some(s=>s.includes('MSG_KEPT')));
const wheel=render(37,16,{routeScroll:1},{...base,requests:many});
assert.notDeepEqual(text(wheel),lines);
""")

    def test_a_request_alone_still_draws_the_box_and_a_long_url_title_is_cut_whole(self):
        self.node(r"""
const lone=text(render(32,24,{},{...base,routes:[],route_count:0,requests:[
 q('rq-dddddd','→','[52]','https://example.com/'+'a'.repeat(80),'전달됨','request-out')]}));
assert(lone.some(s=>s.includes('1 REQUEST')),lone.join('\n'));
const row=lone.find(s=>s.includes('→'));
assert(row.includes('…')&&row.includes('전달됨'),row);
assert(lone.every(s=>[...s].length<=32));
""")


if __name__ == "__main__":
    unittest.main()
