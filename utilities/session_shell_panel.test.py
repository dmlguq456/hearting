"""The approved work panel preserves facts, editor receipts and private settings."""
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
 assert(old.rows.flat().some(p=>p.text.includes('Exact route')));
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
 for(const want of ['[e9] project','(topic 🚧 3)','fixture high','context ','42%','running: pytest','◑답 대기 1','SESSION_TITLE','╭─ route 6','ROUTE_0','── messages','MESSAGE_TWO','5h 23% │ 7d 60%'])
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
 for(const want of ['[e9] project','fixture high','42%','◑답 대기 1','SESSION_TITLE'])assert(text.includes(want),width+'x'+height+' lost '+want+'\n'+text);
 // 11-11 night final layout: the route side gives way first (its opened detail
 // before its rows), and the messages fixed at the bottom go only last.
 if(text.includes('plan ✓ › execute ●'))assert(text.includes('MESSAGE_TWO'),text);
 if(text.includes('ROUTE_0'))assert(text.includes('MESSAGE_ONE'),text);
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
const keyOf=(messages,i)=>draw(messages,40,40).hits.filter(h=>h.action.startsWith('message:'))[i].action;
const read=(messages,key,width,height)=>{const first=draw(messages,width,height,{routeDetail:key}),out=[];
 for(let page=0;page<first.routePages;page++)out.push(...draw(messages,width,height,{routeDetail:key,routePage:page}).plain.slice(1,-1).filter(s=>!/messages · |‹ \d+\/\d+ ›/.test(s)));return out;};
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
const keys=messages=>draw(messages).hits.filter(h=>h.action.startsWith('message:')).map(h=>h.action);
const ends=rows=>['FIRST','SECOND','THIRD','FOURTH','INSERTED','SAME'].filter(t=>rows.some(s=>s.includes(t+'_END')));
const page=(messages,key)=>{const first=draw(messages,{routeDetail:key,height:10}),out=[];
 for(let p=0;p<first.routePages;p++)out.push(...draw(messages,{routeDetail:key,routePage:p,height:10}).plain);
 return {opened:first.plain.some(s=>s.includes('messages · ')),shown:ends(out),detail:first.routeDetail};};
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
// The panel head's `route N` is whole or left out at any width, never `route 1` for 12.
for(const n of [1,12,123])for(let width=12;width<=40;width++){
 const rows=view.render({width,height:12,side:true,model:{routes:[{id:'a',title:'ROW',state:'working',kind:'working',paint:{}}],route_total:n},questions:()=>[],configuration:{}}).rows.map(r=>r.map(p=>p.text).join(''));
 for(const row of rows)for(const hit of row.matchAll(/\br(?:o|ou|out|oute)?\b( \d+)?/g))assert.equal(hit[0],'route '+n,width+' '+row);
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
let r=paint(twelve,40,40);assert(r.plain.some(s=>s.includes('route 12')),r.plain.join('\n'));
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
assert(pages.plain.some(s=>s.includes('messages · ')));
// A message no longer kept falls back to the ordinary panel.
assert(!paint(msg,40,14,{routeDetail:'message:9:→'}).plain.some(s=>s.includes('messages · ')));
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

    def test_an_unknown_state_carries_the_natives_own_turn_record_for_display(self):
        # Spec rule 26: while the host has no lifecycle state (start, resume, /clear,
        # a switch) the projection carries the runtime's own turn record, the one the
        # pane reader uses; a known host state asks nothing.
        from unittest import mock
        import human_wait_state
        import terminal_surface
        shell.registry_root()
        sid = "00000000-0000-4000-8000-000000001112"
        calls = []
        def turn(harness, pid, session):
            calls.append((harness, pid, session))
            return "idle"
        # No answer-wait producer is asked for these reads (it would publish a cache
        # the other projection tests read).
        quiet = mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, ""))
        with quiet, mock.patch.object(terminal_surface, "native_turn", side_effect=turn):
            unknown = shell.panel_projection("claude", sid, native_pid=os.getpid(), native_start="0",
                                             columns=40, rows=30, state="unknown")
            known = shell.panel_projection("claude", sid, native_pid=os.getpid(), native_start="0",
                                           columns=40, rows=30, state="working")
            unbound = shell.panel_projection("claude", sid, columns=40, rows=30, state="unknown")
        self.assertEqual(unknown.get("native_turn"), "idle")
        self.assertNotIn("native_turn", known)
        self.assertNotIn("native_turn", unbound)
        self.assertEqual(calls, [("claude", os.getpid(), sid)])
        with mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, "")), \
                mock.patch.object(terminal_surface, "native_turn", return_value="unknown"):
            self.assertNotIn("native_turn", shell.panel_projection("codex", sid, native_pid=os.getpid(),
                                                                    native_start="0", columns=40, rows=30))

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
 messages:[1,2,3].map(at=>({way:'←',who:'[e7]',text:'MESSAGE_'+at,at}))};
const question=[[p('◑ Q_HEAD')],[p('Q_BODY')],[p('› A')],[p('2 B')],[p('↑↓ choose · Enter send · t text')]];
const paint=(o={})=>view.render({width:40,height:30,side:true,sid:'s',configuration:{},model:{...model,...o.model},state:o.state,frame:o.frame,
 settings:o.settings,questions:q=>(o.q||[]).slice(0,q.budget)});
const text=v=>v.rows.map(r=>r.map(x=>x.text).join(''));
// State on the left of the top border, the gear alone on the right; HEARTING only in settings.
for(const [state,want] of [['idle','┌ ● idle ─'],['working','┌ ⠙ running ─'],['blocked','┌ ◑ waiting ─'],['unknown','┌──'],[undefined,'┌──']]){
 const rows=text(paint({state,frame:1}));assert(rows[0].startsWith(want),state+' '+rows[0]);
 assert(rows[0].endsWith(' ⚙ ┐')&&!rows.join('').includes('Settings')&&!rows.join('').includes('HEARTING'),rows[0]);
}
const settings=paint({settings:{index:0,value:view.defaults}});assert(text(settings)[0].startsWith('┌ HEARTING ─'),text(settings)[0]);
const gear=paint({state:'idle'}).hits.find(h=>h.action==='settings');assert(gear.row===0&&gear.left<=35&&gear.right>=39,JSON.stringify(gear));
// The state word takes the same color as its glyph (Fleet's state key).
const top=paint({state:'idle'}).rows[0];assert.equal(top[2].foreground,150);assert.equal(top[3].foreground,150);
// Spec rule 25: the whole frame takes that word's color, running and waiting their
// session keys, idle and an unknown state the neutral line, in the same paint.
for(const [state,want] of [['working',230],['submitting',230],['blocked',217],['idle',undefined],['unknown',undefined]]){
 const v=paint({state}),frame=v.rows.flatMap(r=>[r[0],r.at(-1)]);
 assert(frame.every(x=>(x.foreground??undefined)===want&&(want===undefined?x.dim===true:x.dim!==true)),state+' '+JSON.stringify(frame[0]));
 assert(!v.rows.flat().some(x=>x.text==='╭'&&x.foreground===217),state+' no route failure here');
}
// A failed route turns only its own box line to the failure color; the frame stays the session's.
const failed=paint({state:'idle',model:{routes:[route(0),{...route(1),state:'dead',kind:'dead'}]}});
const box=failed.rows.flat().filter(x=>/^[╭╮╰╯]$/.test(x.text));
assert(box.length===4&&box.every(x=>x.foreground===217&&x.bold===false),JSON.stringify(box));
assert(failed.rows[0][0].foreground==null&&failed.rows[0][0].dim===true);
// Repo row, then Git on its own row; the route box high; messages (no box: two rules) fixed at the bottom.
let rows=text(paint({state:'idle'}));
assert(rows[1].includes('[e9] project')&&!rows[1].includes('topic')&&rows[2].includes('(topic 🚧 3)'),rows.join('\n'));
const r=rows.findIndex(s=>s.includes('╭─ route 3 ')),m=rows.findIndex(s=>s.includes('── messages '));
assert(r>0&&m>r&&rows.slice(r,m).filter(s=>s.includes('ROUTE_')).length===3,rows.join('\n'));
// Spec rule 24: a folded route takes two rows beside the native area: the glyph with
// harness, context and time, then its title two cells in.
const t0=rows.findIndex(s=>s.includes('ROUTE_0'));
assert(/│ │ \S codex 72% · 42m +│ │/.test(rows[t0-1])&&rows[t0].includes('│ │   ROUTE_0'),rows.join('\n'));
assert(rows.at(-2).includes('7d 60%')&&/^│ ─+ │$/.test(rows.at(-3))&&m===rows.length-7&&!rows.join('').includes('╭─ messages'),rows.join('\n'));
for(const s of rows)assert.equal(cells(s),40,s);
// A question box sits right above the messages in the answer-needed color; messages are dim, route plain.
const asked=paint({state:'working',q:question,model:{answerable:true}});rows=text(asked);
const qTop=rows.findIndex(s=>s.includes('─ ◑ Q_HEAD ')),mq=rows.findIndex(s=>s.includes('── messages '));
assert(qTop>r&&rows[mq-1].includes('╰')&&mq===rows.length-7&&rows.at(-2).includes('7d 60%'),rows.join('\n'));
const border=(i,ch)=>asked.rows[i].find(x=>x.text.startsWith(ch));
assert.equal(border(qTop,'╭').foreground,174);assert.equal(asked.rows[mq].find(x=>x.text.includes('messages')).dim,true);assert.notEqual(border(r,'╭').dim,true);
for(const s of rows)assert.equal(cells(s),40,s);
// Short of rows (spec rule 23): two rows fold to one and messages fold to one row
// before the route box scrolls in place; messages go last.
const short=(h,q=question)=>text(view.render({width:40,height:h,side:true,sid:'s',configuration:{},model:{...model,answerable:true},state:'idle',questions:o=>q.slice(0,o.budget)}));
for(const q of [[],question])for(let h=12;h<=30;h++){
 const rows=short(h,q),all=rows.join('\n'),two=rows.some(s=>s.includes('│   ROUTE_'));
 if(!all.includes('MESSAGE_2')&&all.includes('MESSAGE_1'))assert(!two&&/ \+2 ─ │/.test(all),h+'\n'+all);
 if(all.includes('MESSAGE_3')&&all.includes('ROUTE_0'))assert(rows.filter(s=>s.includes('ROUTE_')).length===3&&!/[↑↓]\d/.test(all),h+'\n'+all);
 if(/[↑↓]\d/.test(all))assert(!two&&!all.includes('MESSAGE_2'),h+'\n'+all);
 if(!all.includes('MESSAGE_1'))assert(!all.includes('╭─ route'),h+' messages go last\n'+all);
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
for(const [width,side] of [[40,true],[80,false]]){
 const rows=paint(route,width,side),flat=rows.join('').replace(/[│ ]/g,'');
 assert(flat.includes('>_Bash·python3tools/run-tests.py3m'),rows.join('\n'));
 const line=rows.filter(x=>x.includes('after Edit'));
 assert.equal(line.length,1,rows.join('\n'));assert(line[0].includes('…'),line[0]);
}
const idle=paint({...route,tool:undefined,target:undefined,tool_age:undefined,idle_tool:true,after_tool:undefined},40,true);
assert(idle.some(x=>x.includes('>_ no tool')),idle.join('\n'));
'''
        self.node(script, "panel-view.cjs")

    def test_a_folded_one_step_route_adds_its_tool_only_where_the_row_has_room(self):
        # Supervisor 2026-11-12: the value row of a folded route may add Fleet's `>_ tool`
        # for a one-step route, never pushing out harness, model, context or time (rules 19,
        # 24: the values lead, the tool before the time).
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const P=(c)=>({glyph:{text:'⠋'},harness:{text:'claude'},model_text:{text:'Opus 5.5'},ctx:{text:c}});
const one={id:'a',title:'one step',state:'working',kind:'working',elapsed:'47m',tool:'python3',target:'tools/run-tests.py',paint:P('24%')};
const staged={...one,id:'b',title:'staged',flow:'plan ✓ › execute ● › review',stages:[{name:'plan'},{name:'execute'},{name:'review'}]};
const paint=(width)=>view.render({width,height:30,side:true,model:{routes:[one,staged],route_count:2,messages:[]},sid:'s',
 configuration:{},expandedKeys:[],questions:()=>[]}).rows.map(x=>x.map(p=>p.text).join(''));
const narrow=paint(40),wide=paint(52);
assert(narrow.some(x=>x.includes('claude Opus 5.5 24% · 47m')),narrow.join('\n'));
assert(!narrow.some(x=>x.includes('>_')),narrow.join('\n'));
assert(wide.some(x=>x.includes('⠋ claude Opus 5.5 24% · >_ python3 · 47m')),wide.join('\n'));
assert.equal(wide.filter(x=>x.includes('>_')).length,1,wide.join('\n'));
assert(!wide.some(x=>x.includes('tools/run-tests.py')),wide.join('\n'));
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
const paint=(cfg,open=[],width=40)=>view.render({width,height:30,side:width<80,model,configuration:cfg,expandedKeys:open,questions:()=>[]}).rows.map(r=>r.map(s=>s.text).join(''));
const time=paint({}),byModel=paint({route_line:'model'});
const row=(rows,title)=>rows.find(s=>s.includes(title.slice(0,5)));
// Spec rule 24: two rows beside the native area: the glyph and the values, then the title.
const above=(rows,title)=>rows[rows.findIndex(s=>s.includes(title.slice(0,5)))-1];
for(const rows of [time,byModel])assert(above(rows,'셸 표기').includes('⠋ codex gpt-6.1-sol 72% · 42m')&&!row(rows,'셸 표기').includes('codex')
 &&row(rows,'셸 표기').includes('│   셸 표기'),rows.join('\n'));
// Effort goes first; with room it stays, in its place after the model.
assert(paint({},[],48).some(s=>s.includes('⠋ codex gpt-6.1-sol high 72% · 42m')),paint({},[],48).join('\n'));
// Short of cells the `route line` choice keeps its value and the other goes, whole.
const narrowTime=paint({},[],32),narrowModel=paint({route_line:'model'},[],32);
assert(above(narrowTime,'셸 표기').includes('codex 72% · 42m')&&!above(narrowTime,'셸 표기').includes('gpt'),narrowTime.join('\n'));
assert(above(narrowModel,'셸 표기').includes('codex gpt-6.1-sol 72%')&&!above(narrowModel,'셸 표기').includes('42m'),narrowModel.join('\n'));
// A long title is cut on its own row; harness and elapsed time keep theirs.
assert(row(time,'제목만').includes('…')&&above(time,'제목만').includes('claude')&&above(time,'제목만').includes('1h 27m'));
// Both rows open the route in place.
const hitRows=view.render({width:40,height:30,side:true,model,configuration:{},questions:()=>[]}).hits.filter(h=>h.action==='route:a').map(h=>h.row);
assert.equal(hitRows.length,2);assert.equal(hitRows[1],hitRows[0]+1);
// Unreported context is left out, never `?`.
assert(!time.join('\n').includes('?'));
const opened=paint({},['route:a']),detail=opened.slice(opened.findIndex(s=>s.includes('셸 표기'))+1,opened.findIndex(s=>s.includes('제목만')));
assert(detail.some(s=>s.includes('gpt-6.1-sol high')&&s.includes('72%')),detail.join('\n'));
assert(detail.some(s=>s.includes('plan ✓ › execute ● › review')));assert(detail.some(s=>s.includes('>_ apply_patch · +1 agent')));
const openedModel=paint({route_line:'model'},['route:a']);
assert(openedModel.some(s=>s.includes('high')&&s.includes('42m')),openedModel.join('\n'));
// A message row opens to its whole retained text and closes again.
const messageKey=view.render({width:40,height:30,side:true,model,configuration:{},questions:()=>[]}).hits.find(h=>h.action.startsWith('message:')).action;
const message=paint({},[messageKey]);assert(message.join('').includes('RV-PR1506-R2-1111.md'));
assert(!paint({}).join('').includes('RV-PR1506-R2-1111.md'));
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
const all=rows(paint()).join('\n'),flat=all.replace(/[│ \n]+/g,' ');
for(const word of ['kind autopilot-code(dev·std)','owner claude (Opus 5.5·high) balanced-deep 31%','✓ plan · PASS · codex gpt-fixture high · 12m · done 22:34 · plan/code · gate ✓',
 '● execute R2 · codex gpt-fixture high · 72% · 30m · dev/backend','autopilot-code(dev·std) · owner claude (Opus 5.5·high) balanced-deep 31% >_ apply_patch · +1 agent','NOTE_R2','└ execute · 2 claude Sonnet 5.5 12%',
 '· review · pending · review/code','42m · 2 legs · staged · test ✓','next resume · execute','✕ execute','worker claude · failed 3m','resume execute'])
 assert(flat.includes(word),word+'\n'+all);
for(const s of rows(paint()))assert.equal([...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0),40,s);
// Short of rows the detail keeps its first rows and a `+N lines ›` row that opens the route page.
const short=view.render({width:40,height:22,side:true,model,configuration:{},expandedKeys:['route:a'],questions:()=>[]});
assert(rows(short).some(s=>/\+\d+ lines ›/.test(s)),rows(short).join('\n'));
const more=short.hits.find(h=>h.action==='route'&&rows(short)[h.row]&&/lines ›/.test(rows(short)[h.row]));assert(more,'a press opens the route page');
const page=rows(view.render({width:40,height:80,side:true,model,configuration:{},routeDetail:true,questions:()=>[]})).join('\n').replace(/[│ \n]+/g,' ');
for(const word of ['owner claude (Opus 5.5·high)','dev/backend','NOTE_R2','resume execute'])assert(page.includes(word),word+'\n'+page);
// The band's note (Fleet's `detail_after`) follows its head, on the next rows when narrow.
assert(flat.includes('42m · 2 legs · staged · test ✓ · before this leg: frame ✓ — outside this snapshot'),all);
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
const count=v=>v.rows.flat().find(x=>x.text==='route 2'),dot=v=>v.rows.flat().find(x=>x.text==='●'&&x.foreground===150);
assert.equal(count(a).bold,true);assert.equal(count(c).dim,true);assert.equal(dot(a).bold,true);assert.equal(dot(c).dim,true);
assert(a.animated&&a.spins.includes(0)&&a.spins.length>1,JSON.stringify(a.spins));
// Nothing runs: no beat, nothing to turn between native frames.
const idle=view.render({width:40,height:30,side:true,model:{routes:[{...routes[1]}],route_count:1,styles},state:'idle',configuration:{},questions:()=>[]});
assert.equal(idle.animated,false);assert.deepEqual(idle.spins,[]);
assert.equal(idle.rows.flat().find(x=>x.text==='route 1').foreground,186);
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
 const branch=rows.findIndex(s=>s.includes('(values-fixture*'));
 if(width===35&&height===26)assert(branch>=0,'a roomy side panel keeps the Git row\n'+rows.join('\n'));
 // The Git row sits right below the repo row and never shares it.
 if(branch>=0)assert(rows[branch-1].includes('[cb] project')&&!rows[branch].includes('[cb]'),rows.join('\n'));
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

    def test_sent_messages_come_from_this_sessions_own_ledger_shard(self):
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": directory}):
            sid = "00000000-0000-4000-8000-0000000000e9"
            shard = shell._peer_message()._ledger_path(sid)
            shard.parent.mkdir(parents=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            rows = [{"ts": stamp, "from": {"session_id": sid}, "to": {"harness": "codex", "session_id": "x", "name": "n"},
                     "kind": "note", "summary": "SENT_" + str(n)} for n in range(5)]
            rows.append({"ts": stamp, "from": {"session_id": "other"}, "to": {}, "kind": "note", "summary": "FOREIGN"})
            shard.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot json\n")
            sent, watched = shell._sent_peer_messages(sid)
            self.assertEqual([row["summary"] for row in sent], ["SENT_2", "SENT_3", "SENT_4"])
            self.assertEqual(watched, [str(shard)])
            self.assertIs(shell._sent_peer_messages(sid)[0], sent)  # unchanged file: no reparse
            self.assertEqual(shell._sent_peer_messages("absent-session"), ([], []))

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
 assert(!rows(r).join('\n').includes('route 1'),rows(r).join('\n'));
 assert.deepEqual(r.spins,[]);assert.equal(r.animated,false,width+'x'+height);
}
// The same route with room to show it: its mark spins and the beat runs.
const shown=view.render({width:40,height:30,side:true,state:'idle',model:{routes:[route],route_count:1},questions:o=>q.slice(0,o.budget)});
assert(rows(shown).join('\n').includes('route 1'));assert.equal(shown.animated,true);assert(shown.spins.length>0);
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
 assert.equal(v.fields.at(-1),'side_min_columns');
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
 assert.equal(at('┌').foreground,f.frame,label);assert.equal(r.rows.flat().find(p=>p.text==='note').foreground,f.secondary,label);
 assert(parts.some(p=>p.text.includes('messages')&&p.foreground===f.title),label);
 if(theme!=='mono'){
  // The question's border, `◑` and `›` are rose; its words are body text.
  assert(parts.some(p=>p.text==='◑'&&p.foreground===f.accent)&&parts.some(p=>p.text==='›'&&p.foreground===f.accent),label);
  assert.equal(parts.find(p=>p.text==='Which?').foreground,f.body,label);
 }
 if(face==='light')assert(!parts.some(p=>[253,186,150,217].includes(p.foreground)),label);
}
// The light theme without a shade: the reviewed deep hues, secondary text without a dim.
const light=paint({theme:'light'}).rows.flat();
assert(!light.some(p=>[253,186,150,217,28,124,91].includes(p.foreground)));assert.equal(light.find(p=>p.text==='note').foreground,241);
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


if __name__ == "__main__":
    unittest.main()
