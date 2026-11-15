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
        # Source-bound regression: visible shell focus must not leak keys into native delivery.
        # 11-15: a press opens the question popup (`block.input`) or a table popup (`tableInput`); while
        # one stands (`popupShown()`) it owns every key; the settings editor owns its keys as before.
        script = r"""
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=process.argv[1],src=fs.readFileSync(path.join(root,'utilities/session-shell/host.cjs'),'utf8');
function section(start,end){let a=src.indexOf(start),b=src.indexOf(end,a+start.length);assert(a>=0&&b>a,start);return src.slice(a,b);}
const functions=section('function openSettings(){',"let settingsTail=''")+section('function popupShown(',"// The sections with something")
 +section('function openPopup(',"// The panel's route order")+section('function panelMouse(', 'function displayReporter(')+section('function inputBytes(', '// The table popups');
function fixture(delivery){
 let held=[],saved=[],native=[],edited=[],tableKeys=[],focused=false;
 const context={Buffer,console,currentDelivery:delivery,settings:undefined,displayConfiguration:{layout:'side',side_width:40},
  panelView:require(path.join(root,'utilities/session-shell/panel-view.cjs')),cols:133,harness:'claude',epoch:0,empty:true,
  fleetView:undefined,paste:false,relay:false,dragging:false,heldBytes:0,sessionDetails:false,keyTail:'',actionLines:[],
  table:null,popupWatch:[],activated:true,ended:false,succeeding:false,expanded:true,
  panelHits:[{row:0,left:30,right:38,action:'settings'}],geometry:()=>({side:true,panelColumn:93}),panelRow:()=>0,height:()=>30,panelColumns:()=>40,
  terminal:{modes:{mouseTrackingMode:'none'}},queueLayout(){},queueDisplay(){},rebuildDisplay(){},observeInput(){},openFleet(){},
  // The press targets stand for the placement on screen (#1586 review follow-up 5); a hand fold choice.
  layoutQueued:false,panelHitsPlacement:'shown',placement:()=>'shown',foldByHand(){},
  source:(op,value)=>{saved.push({op,value});return Promise.resolve({ok:true,settings:value.settings});},
  settingsInput(){saved.push({op:'settings-input'});},
  tableInput(data){tableKeys.push(Buffer.from(data));return Buffer.alloc(0);},resetTableInput(){},
  inputOwner:{hold:b=>held.push(Buffer.from(b))},child:{write:b=>native.push(Buffer.from(b))},process:{stdin:{pause(){}}},
  block:{leave(){focused=false;},enter(){focused=true;},get focused(){return focused;},input(data){edited.push(Buffer.from(data));return Buffer.alloc(0);}}};
 vm.createContext(context);vm.runInContext(functions+'\nthis.mouse=panelMouse;this.bytes=inputBytes;',context);
 return {context,held,saved,native,edited,tableKeys};
}
let failures=0;
function check(name,fn){try{fn();console.log('PASS',name);}catch(error){failures++;console.log('ASSERT_FAIL',name,error.message);}}
check('normal settings click routes keys to its settings editor',()=>{
 const f=fixture(undefined);assert.equal(f.context.mouse(0,130,1,false),true);assert(f.context.settings);
 f.context.bytes(Buffer.from('\r'));assert.equal(f.saved[0]?.op,'settings-input');assert.equal(f.held.length,0);
});
check('peer delivery must not show a settings editor while sending its Enter to native hold',()=>{
 const f=fixture('fixture-peer-ref');assert.equal(f.context.mouse(0,130,1,false),true);assert(f.context.settings);
 f.context.bytes(Buffer.from('\r'));
 assert.equal(f.held.length,0,'Enter intended for the visible settings editor entered the native delivery hold queue');
 assert.equal(f.saved.length,1);
});
check('ordinary native typing remains held during peer delivery',()=>{
 const f=fixture('fixture-peer-ref');f.context.bytes(Buffer.from('native-draft'));
 assert.equal(Buffer.concat(f.held).toString(),'native-draft');assert.equal(f.saved.length,0);
});
check('mouse-opened question keeps its draft out of native hold',()=>{
 const f=fixture('fixture-peer-ref');f.context.panelAction('question');
 f.context.bytes(Buffer.from('question-draft'));assert.equal(Buffer.concat(f.edited).toString(),'question-draft');assert.equal(f.held.length,0);
 f.context.block.leave();f.context.bytes(Buffer.from('native-after'));
 assert.equal(Buffer.concat(f.held).toString(),'native-after');
});
check('a standing table popup owns every key and nothing reaches the native hold',()=>{
 const f=fixture('fixture-peer-ref');f.context.table={kind:'LAB',rows:[],hits:[],scroll:0};
 f.context.bytes(Buffer.from('popup-keys'));assert.equal(Buffer.concat(f.tableKeys).toString(),'popup-keys');
 assert.equal(f.held.length,0);assert.equal(f.native.length,0);assert.equal(f.edited.length,0);
 f.context.table=null;f.context.bytes(Buffer.from('native-after'));assert.equal(Buffer.concat(f.held).toString(),'native-after');
});
check('settings leave retains native bytes in their original order',()=>{
 const f=fixture('fixture-peer-ref');f.context.bytes(Buffer.from('first'));
 f.context.mouse(0,130,1,false);f.context.bytes(Buffer.from('\r'));f.context.cancelSettings();
 f.context.bytes(Buffer.from('second'));
 assert.deepEqual(f.held.map(b=>b.toString()),['first','second']);assert.equal(f.native.length,0);
});
check('display failure still holds native bytes even with stale shell focus',()=>{
 const f=fixture('fixture-peer-ref');f.context.mouse(0,130,1,false);f.context.block.enter();f.context.relay=true;
 f.context.bytes(Buffer.from('raw-draft'));assert.equal(Buffer.concat(f.held).toString(),'raw-draft');assert.equal(f.saved.length,0);
});
console.log(JSON.stringify({checks:7,assertionFailures:failures}));
process.exitCode=failures?1:0;
"""
        result = subprocess.run(["node", "-e", script, str(ROOT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
 // 11-15: there is no route box any more; the ROUTE head and the route's row carry the state.
 const rowOf=v=>v.rows.find(r=>r.map(p=>p.text).join('').includes('Exact route'));
 assert(rowOf(fresh)&&rowOf(old),state);
 assert(old.rows.some(r=>r.map(p=>p.text).join('').includes('1 ROUTE')),state);
 assert(!old.rows.flat().some(p=>p.text==='╭'),state);
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
 const v=view.render({width,height:width===80?12:30,side:width<80,model,sid:'exact',configuration:{},
  // 11-15: the question's summary (its first row, its choices on one row); the whole of it reads in the popup.
  brief:()=>({count:1,label:'QUESTIONS',rows:[[{text:'◑ '},{text:'Where should this result go?'}],[{text:'  '},{text:'1 Current project'}]],
   one:[[{text:'◑ '},{text:'Where should this result go?'}]],line:[{text:'Where should this result go?'}],choices:1})});
 const text=v.rows.map(r=>r.map(s=>s.text).join(''));
 assert(text.some(s=>s.includes('Native selected high')));assert(text.some(s=>s.includes('Exact route')));
 assert(text.some(s=>s.includes('Where should')));assert(text.some(s=>s.includes('1 QUESTIONS')));
 // Nothing opens in place any more (11-15), and no row shows `→`.
 assert(!text.some(s=>s.includes('→')));
 assert(text[0].startsWith('┌'));assert(text.at(-1).endsWith('┘'));
 assert(v.rows.every(r=>r.every(s=>s.background===undefined&&s.reverse!==true)));
 // The frame follows the session's own state (none given here), never the question (spec rule 25).
 assert.equal(v.color,'dim');assert(v.hits.some(h=>h.action==='settings'));
 assert(v.hits.some(h=>h.action==='popup:ROUTE:r'));assert(v.hits.some(h=>h.action==='popup:ROUTE'));assert(v.hits.some(h=>h.action==='question'));
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
        # Spec 2026-11-11 rule 6, 11-15: with too few rows the question and the three top
        # groups stay; MESSAGES rows and then the route rows give way (the lowest section first).
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const route=(id,title)=>({id,title,state:'working',kind:'working',elapsed:'42m',flow:'plan ✓ › execute ●',paint:{glyph:{text:'⠏'},harness:{text:'codex'},ctx:{text:'72%'}}});
const model={head:[{text:'[e9] '},{text:'project'},{text:' (topic 🚧 3)'}],selected:[{text:'fixture'},{text:' high'}],
 context:[{text:'context '},{text:'━━━━──────'},{text:' 42%'}],wait:'◑답 대기 1',session_title:'SESSION_TITLE',
 jobs:[{text:'>_'},{text:' running: '},{text:'pytest'}],quota:[{text:'5h 23%'},{text:' │ '},{text:'7d 60%'}],route_count:6,
 routes:Array.from({length:6},(_,i)=>route('r'+i,'ROUTE_'+i)),messages:[{way:'←',who:'[e7]',text:'MESSAGE_ONE',at:1},{way:'→',who:'[73]',text:'MESSAGE_TWO',at:2}]};
// 11-15: the question's summary under its head row; the whole of it reads in the popup.
const question={count:1,label:'QUESTIONS',rows:[[{text:'◑ '},{text:'Q head'}],[{text:'  '},{text:'1 yes · 2 no'}]],one:[[{text:'◑ '},{text:'Q head'}]],line:[{text:'Q head'}],choices:2};
const paint=(width,height,side,q)=>view.render({width,height,side,model,sid:'exact',configuration:{},
 brief:()=>q}).rows.map(r=>r.map(s=>s.text).join(''));
for(const [width,height,side] of [[40,30,true],[80,20,false]]){
 const idle=paint(width,height,side,null);
 for(const want of ['project','(topic 🚧 3)','fixture high',side?'CTX    ━━━━':'context ','42%','running: pytest','1 QUESTIONS','SESSION_TITLE',side?'6 ROUTE':'route 6','ROUTE_0',side?'MESSAGES':'messages','MESSAGE_TWO','5h 23% │ 7d 60%'])
  assert(idle.some(s=>s.includes(want)),width+' missing '+want+'\n'+idle.join('\n'));
 // 11-15: the ROUTE head carries the whole count and at most three routes have a row; no `more` row.
 const listed=idle.filter(s=>/ROUTE_\d/.test(s)).length;
 assert.equal(listed,3,idle.join('\n'));assert(!idle.join('').includes('more'),idle.join('\n'));assert(/^└─+┘$/.test(idle.at(-1)));
 assert(idle.at(-2).includes('7d 60%'),'quota is the last row');
}
for(const [width,side,low] of [[40,true,14],[80,false,10]])for(let height=low;height<=24;height+=2){
 const rows=paint(width,height,side,question),text=rows.join('\n');
 assert(rows.length<=height,width+'x'+height);
 for(const want of ['1 QUESTIONS','◑ Q head'])assert(text.includes(want),'question row lost at '+width+'x'+height+'\n'+text);
 for(const want of ['project','fixture high','42%','SESSION_TITLE'])assert(text.includes(want),width+'x'+height+' lost '+want+'\n'+text);
 // 11-15: the lowest section gives way first, so a MESSAGES row stands only where the route rows do.
 if(text.includes('MESSAGE_TWO'))assert(text.includes('ROUTE_0'),text);
}
const r=view.render({width:40,height:30,side:true,model,sid:'exact',configuration:{},questions:()=>[]});
assert(!r.hits.some(h=>h.action==='details'));
assert(r.hits.some(h=>h.action==='popup:MESSAGES'));assert(!r.hits.some(h=>h.action==='fleet'));
'''
        self.node(script, "panel-view.cjs")

    def test_shortening_keeps_the_observed_count_message_and_mcp(self):
        # Only the rows shown shrink: the whole route count, a message's row and
        # OpenCode's MCP count stay readable (review RV-PR1517 §1; 11-15 section form).
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const route=(id,title)=>({id,title,state:'working',kind:'working',elapsed:'31m',paint:{glyph:{text:'⠏'},harness:{text:'codex'},ctx:{text:'37%'}}});
const base={head:[{text:'[52] project'}],selected:[{text:'fixture'},{text:' high'}],context:[{text:'context '},{text:'37%'}],
 jobs:[{text:'>_ running: JOB'}],quota:[{text:'5h 12%'}],session_title:'TITLE'};
const paint=(model,width,height,extra={})=>{const r=view.render({width,height,side:width<80,model,sid:'s',configuration:{},questions:()=>[],...extra});
 return {...r,plain:r.rows.map(r=>r.map(p=>p.text).join(''))};};
// 11-15: twelve current routes: the head counts all twelve, three of them have rows,
// and nothing folds into `+N more` or opens Fleet.
const twelve={...base,routes:Array.from({length:12},(_,i)=>route('r'+i,'ROUTE_'+i)),route_count:12,route_total:12};
let r=paint(twelve,40,40);assert(r.plain.some(s=>s.includes('12 ROUTE')),r.plain.join('\n'));
assert.equal(r.plain.filter(s=>/ROUTE_\d/.test(s)).length,3,r.plain.join('\n'));assert(!r.plain.join('').includes('more'),r.plain.join('\n'));
// Fewer rows: the rows shown shrink, never the count in the head.
const six={...base,routes:Array.from({length:6},(_,i)=>route('r'+i,'ROUTE_'+i)),route_count:6,route_total:6};
for(let height=10;height<=22;height++){
 for(const [model,count] of [[six,'6'],[twelve,'12']]){
  r=paint(model,40,height);const rows=r.plain.filter(s=>/ROUTE_\d/.test(s)).length;
  assert(!r.plain.join('').includes('more')&&!r.hits.some(h=>h.action==='fleet'),height+'\n'+r.plain.join('\n'));
  assert(rows<=3&&r.hits.filter(h=>h.action.startsWith('popup:ROUTE:')).length===rows,height+'\n'+r.plain.join('\n'));
  if(rows)assert(r.plain.some(s=>s.includes(count+' ROUTE')),height+'\n'+r.plain.join('\n'));
 }
}
// A producer total above the rows it sent stays the head's count.
r=paint({...six,route_total:9},40,40);assert(r.plain.some(s=>s.includes('9 ROUTE')),r.plain.join('\n'));
// The rows read failed, then working, then waiting, then finished ones.
const mixed={...base,route_count:4,routes:[{...route('d','DONE_R'),kind:'done'},{...route('b','WAIT_R'),kind:'blocked'},route('w','WORK_R'),{...route('x','DEAD_R'),kind:'dead'}]};
r=paint(mixed,40,40);{const order=r.plain.filter(s=>/_R/.test(s)).map(s=>/(\w+)_R/.exec(s)[1]);assert.deepEqual(order.slice(0,3),['DEAD','WORK','WAIT'],r.plain.join('\n'));}
// A long message stays one row ending in `…`; the popup reads it whole (nothing opens in place).
const long='START '+'word '.repeat(60)+'END_1517';
const msg={...base,routes:[],route_count:0,messages:[{way:'←',who:'[e7]',text:long,at:1}]};
for(const [width,height] of [[40,30],[80,24]]){
 r=paint(msg,width,height);const rows=r.plain.filter(s=>s.includes('START'));
 assert.equal(rows.length,1,width+'\n'+r.plain.join('\n'));assert(rows[0].includes('…')&&!r.plain.join('').includes('END_1517'),rows[0]);
 assert(r.hits.some(h=>h.action==='popup:MESSAGES'));
}
// The repo's open work row stays readable.
const work={...six,work:[{text:'◌ '},{text:'3 open · WORK_HEADLINE'}]};
for(const width of [40,80])assert(paint(work,width,30).plain.some(s=>s.includes('3 open · WORK_HEADLINE')),String(width));
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
// 11-15: the question's summary under its label row; the whole of it reads in the popup.
const question={count:1,label:'QUESTIONS',rows:[[p('◑ '),p('Q_HEAD')],[p('  '),p('1 A · 2 B')]],one:[[p('◑ '),p('Q_HEAD')]],line:[p('Q_HEAD')],choices:2};
const paint=(o={})=>view.render({width:40,height:o.height||30,side:true,sid:'s',harness:'claude',configuration:o.cfg||{},model:{...model,...o.model},state:o.state,frame:o.frame,since:o.since,
 settings:o.settings,brief:()=>o.q||null});
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
// A failed route leaves the frame the session's own: no route box stands to take its failure color.
const failed=paint({state:'idle',model:{routes:[route(0),{...route(1),state:'dead',kind:'dead'}]}});
assert(!failed.rows.flat().some(x=>/^[╭╮╰╯]$/.test(x.text)),'no route box');
assert(failed.rows[0][0].foreground==null&&failed.rows[0][0].dim===true);
// 11-15: the repo row, then Git on its own row; the sections one head row each, from the top;
// the rows left over stand between them and MESSAGES, which keeps to the bottom, right above the quota row.
// (32 rows: every structural row of spec rule 35 stands with three routes in their full form.)
let rows=text(paint({state:'idle',height:32}));
assert(rows[1].includes('project')&&!rows[1].includes('[e9]')&&!rows[1].includes('topic')&&rows[2].includes('(topic 🚧 3)'),rows.join('\n'));
// Spec rule 29 (b), 30 (a), 35 (b): the metadata block is two groups parted by one empty row
// ([repository, Git] / [model, context]), then a thin rule, then the session's content with
// its labels; an empty row parts the head from the first section.
assert(/^│ +│$/.test(rows[3])&&rows[4].includes('fixture high')&&rows[5].startsWith('│ CTX    42%')&&/^│ ─+ │$/.test(rows[6]),rows.join('\n'));
assert(rows[7].startsWith('│ TITLE  SESSION_TITLE')&&rows[8].startsWith('│ NOW    NOW_LINE')&&/^│ +│$/.test(rows[9])&&/^│ 3 ROUTE ─+ › │$/.test(rows[10]),rows.join('\n'));
const r=rows.findIndex(s=>/^│ 3 ROUTE /.test(s)),m=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(r>0&&m>=r+4&&rows.slice(r,m).filter(s=>s.includes('ROUTE_')).length===3,rows.join('\n'));
// 11-15: a route row is its glyph, its title and its time at the right; no who/context rows, no box.
const t0=rows.findIndex(s=>s.includes('ROUTE_0'));
assert(/^│ \S ROUTE_0 +42m │$/.test(rows[t0])&&!rows.join('').includes('codex')&&!rows.join('').includes('72%'),rows.join('\n'));
// Each message takes one row (three messages: three rows), the head `MESSAGES` carries no count of rows shown.
assert(rows.at(-2).includes('7d 60%')&&m>=r+4&&/^│ MESSAGES ─+ › │$/.test(rows[m]),rows.join('\n'));
assert(rows[m+1].includes('← [e7] MESSAGE_1')&&rows[m+2].includes('← [e7] MESSAGE_2')&&rows[m+3].includes('← [e7] MESSAGE_3'),rows.join('\n'));
assert(m+4===rows.length-2&&rows.slice(r+4,m).every(s=>/^│ +│$/.test(s)),'MESSAGES keeps to the bottom, the spare rows stand above it\n'+rows.join('\n'));
for(const s of rows)assert.equal(cells(s),40,s);
// The questions section sits first, above the routes (11-15: no box): its head row, the count in the
// question's rose and the name tag dim with a thin rule to `›`, then the question's summary.
const asked=paint({state:'working',q:question,model:{answerable:true}});rows=text(asked);
const qTop=rows.findIndex(s=>/^│ 1 QUESTIONS ─+ › │$/.test(s)),ra=rows.findIndex(s=>/^│ 3 ROUTE /.test(s)),mq=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(qTop>0&&rows[qTop+1].startsWith('│ ◑ Q_HEAD')&&rows[qTop+2].startsWith('│   1 A · 2 B')&&ra===qTop+3&&mq>ra&&rows.at(-2).includes('7d 60%')
 &&rows.some(s=>/^│ \S ROUTE_0 +42m │$/.test(s)),rows.join('\n'));
assert.equal(asked.rows[qTop].find(x=>x.text==='1').foreground,174);assert.equal(asked.rows[qTop].find(x=>x.text==='QUESTIONS').dim,true);
assert.equal(asked.rows[qTop+1].find(x=>x.text==='◑').foreground,174);
assert.equal(asked.rows[mq].find(x=>x.text.includes('MESSAGES')).dim,true);

// A press on any of its rows opens the question popup.
assert.deepEqual(asked.hits.filter(h=>h.action==='question').map(h=>h.row),[qTop,qTop+1,qTop+2]);
for(const s of rows)assert.equal(cells(s),40,s);
// Short of rows (spec rule 23, 11-15): the lowest section gives its rows first, MESSAGES before
// ROUTE; nothing scrolls.
const short=(h,q=question)=>text(view.render({width:40,height:h,side:true,sid:'s',configuration:{},model:{...model,answerable:true},state:'idle',brief:()=>q}));
for(const q of [null,question])for(let h=12;h<=30;h++){
 const rows=short(h,q),all=rows.join('\n');
 assert(rows.length<=h&&!/[↑↓]\d/.test(all),h+'\n'+all);
 // Fewer messages show from the oldest (their head counts the rest, spec rule 58); the newest goes last.
 const hidden=['MESSAGE_1','MESSAGE_2'].filter(m=>!all.includes(m)).length;
 if(all.includes('MESSAGE_3')&&hidden)assert(new RegExp('│ MESSAGES \\+'+hidden+' ').test(all),h+'\n'+all);
 // The lowest section gives way first: a MESSAGES section stands only where a route row does.
 if(all.includes('MESSAGES'))assert(all.includes('ROUTE_0'),h+' messages go first\n'+all);
 if(all.includes('MESSAGE_1'))assert(rows.filter(s=>s.includes('ROUTE_')).length===3,h+'\n'+all);
}
// No box stands at any height; an empty route list or message list draws no head either.
for(let h=10;h<=30;h++)for(const q of [null,question]){const rows=text(view.render({width:40,height:h,side:true,sid:'s',configuration:{},model,state:'idle',brief:()=>q}));
 assert(!rows.join('').includes('╭')&&rows.length<=h,h+'\n'+rows.join('\n'));}
rows=text(paint({model:{routes:[],route_count:0,routes_known:true,messages:[]}}));assert(!rows.join('').includes('ROUTE')&&!rows.join('').includes('MESSAGES'),rows.join('\n'));
// The groups a paint shows decide Claude's statusline rows: a long Git reading cut to the
// row still counts as painted; a group dropped for rows does not.
const longGit={...model,head:[p('[e9] '),p('project')],git:[{text:'(shell-box-git-1111-longer-name 🚧 12 ↑3 ↓1)',dim:true}],wait:'◑답 대기 1'};
for(const [w,side] of [[32,true],[40,true],[80,false]]){
 const v=view.render({width:w,height:30,side,sid:'s',configuration:{},model:longGit,state:'idle',questions:()=>[]});
 assert(text(v).some(s=>s.includes('(shell-box')&&(!side||s.includes('…'))),text(v).join('\n'));
 for(const g of ['head','git','context','quota','wait','route'])assert(v.groups.includes(g),w+' '+g+' '+v.groups);
}
const tight=view.render({width:40,height:10,side:true,sid:'s',configuration:{},model:{...longGit,answerable:true},state:'idle',brief:()=>question});
assert(!tight.groups.includes('git')&&!text(tight).some(s=>s.includes('(shell-box')),tight.groups+'\n'+text(tight).join('\n'));
assert(!view.render({width:40,height:30,side:true,sid:'s',configuration:{},model,settings:{index:0,value:view.defaults},questions:()=>[]}).groups.length,'settings paint no group');
'''
        self.node(script, "panel-view.cjs")

    def test_route_rows_follow_the_route_line_setting(self):
        script = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const p=t=>({text:t});
const model={route_count:2,routes:[
 {id:'a',title:'셸 표기 통일',state:'working',kind:'working',elapsed:'42m',shape:'staged',stages:[{name:'plan',mark:'✓'},{name:'execute',mark:'●',current:true}],
  paint:{glyph:p('⠏'),harness:p('codex'),model_text:p('gpt-6.1-sol'),effort:p('high'),ctx:p('72%')}},
 {id:'b',title:'제목만 있는 route 이름이 아주 길어서 잘림',state:'dead',kind:'dead',elapsed:'1h 27m',shape:'one-shot-owner',stages:[{name:'one-shot',mark:'✕'}],paint:{glyph:p('✕'),harness:p('claude')}}]};
const paint=(cfg,width=48,height=30)=>view.render({width,height,side:width<80,model,configuration:cfg,questions:()=>[]});
const plain=v=>v.rows.map(r=>r.map(s=>s.text).join(''));
const time=plain(paint({})),byModel=plain(paint({route_line:'model'}));
const at=(rows,title)=>rows.findIndex(s=>s.includes(title.slice(0,5)));
// 11-15: one row each (glyph, title, PROGRESS, then the `route line` choice: AGE or the doer's model); the failed first.
assert(time.some(s=>/^│ 2 ROUTE ─+ › │$/.test(s)),time.join('\n'));
assert(at(time,'제목만')<at(time,'셸 표기'),'the failed route stands first\n'+time.join('\n'));
const row=(rows,title)=>rows[at(rows,title)];
assert(/^│ ⠋ 셸 표기 통일 +2\/2 execute ● +42m │$/.test(row(time,'셸 표기'))&&!row(time,'셸 표기').includes('gpt-6.1-sol'),time.join('\n'));
assert(/^│ ⠋ 셸 표기 통일 +2\/2 execute ● +gpt-6\.1-sol │$/.test(row(byModel,'셸 표기'))&&!row(byModel,'셸 표기').includes('42m'),byModel.join('\n'));
// A route without a model reading leaves the last value out, whole.
assert(!row(byModel,'제목만').includes('1h 27m'),byModel.join('\n'));
// Percent and codex stay out of the row: only the title, PROGRESS and the last value.
for(const rows of [time,byModel])assert(!rows.join('').includes('72%')&&!rows.join('').includes('codex')&&!rows.join('').includes('?'),rows.join('\n'));
// Short of cells (32) PROGRESS goes first and the last value stays whole.
for(const [cfg,last] of [[{},'42m'],[{route_line:'model'},'gpt-6.1-sol']]){
 const rows=plain(paint(cfg,32));assert(/^│ ⠋ 셸 표기 통일 +/.test(row(rows,'셸 표기'))&&row(rows,'셸 표기').includes(last)&&!row(rows,'셸 표기').includes('execute'),rows.join('\n'));}
// A long title is cut with `…` on its own row (no second row).
const long=row(time,'제목만');assert(long.includes('…')&&!time.join('').includes('잘림')&&/one-shot ✕/.test(long),time.join('\n'));
// Every route row opens its popup; nothing opens in place.
const hits=paint({}).hits;
assert(hits.some(h=>h.action==='popup:ROUTE:a')&&hits.some(h=>h.action==='popup:ROUTE:b')&&hits.some(h=>h.action==='popup:ROUTE')&&!hits.some(h=>/^route/.test(h.action)),JSON.stringify(hits));
assert.equal(hits.filter(h=>h.action==='popup:ROUTE:a').length,1);
// Every row keeps the panel width with two-cell Hangul and emoji.
for(const rows of [time,byModel,plain(paint({},80,12))])for(const s of rows)assert([...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)<=80);
"""
        self.node(script, "panel-view.cjs")

    def test_a_route_folded_to_one_row_keeps_each_value_whole(self):
        # Review RV-PR1528 §2: 32 cells, the boxed one-row route cut `72%` to `7…`.
        # 11-15: a route is one row (glyph, title, PROGRESS, AGE or model), ordered failed, running,
        # waiting, finished; PROGRESS and AGE stand whole or are left out (PROGRESS first), the title is cut with `…`.
        script = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const P=t=>({text:t}),route=(i,over={})=>({id:'r'+i,title:'ROUTE_'+i+' 한글🙂 긴 제목으로 잘림 확인',kind:'working',state:'working',elapsed:'1h 27m',shape:'staged',
 stages:[{name:'plan',mark:'✓'},{name:'execute',mark:'●',current:true}],
 paint:{glyph:P('⠏'),harness:P('codex'),model_text:P('gpt-6-sol'),ctx:P('72%')},...over});
const model={head:[P('[52] project')],git:[P('(long-branch-'+'x'.repeat(50)+' 🚧 3)')],selected:[P('fixture high')],context:[P('context 42%')],
 quota:[P('5h 12% │ 7d 66%')],route_count:4,route_total:4,
 routes:[route(1,{kind:'done',state:'done',outcome:'PASS',outcome_key:'lvl_g',paint:{glyph:P('✓')}}),route(2,{kind:'blocked',state:'blocked',paint:{glyph:P('●')}}),route(3),
  route(4,{kind:'dead',state:'dead',shape:'one-shot-owner',stages:[{name:'one-shot',mark:'✕'}],paint:{glyph:P('✕')}})],
 messages:[1,2,3].map(at=>({at,way:'←',who:'[peer]',text:'MESSAGE_'+at}))};
let withProgress=0,withoutProgress=0;
for(const width of [28,32,40,48])for(let height=10;height<=30;height++)for(const route_line of ['time','model'])for(const asking of [false,true]){
 const v=view.render({width,height,side:true,state:'idle',model:{...model,answerable:asking},configuration:{route_line},
  questions:o=>asking?[[P('Q')],[P('Question body')],[P('1 YES')],[P('2 NO')]].slice(0,o.budget):[]});
 const rows=v.rows.map(r=>r.map(p=>p.text).join(''));
 const label=width+'x'+height+' '+route_line+' '+asking+'\n'+rows.join('\n');
 const listed=v.hits.filter(h=>h.action?.startsWith('popup:ROUTE:'));
 assert(listed.length<=3,label);
 // Failed first, then running, waiting, finished.
 const order=listed.map(h=>h.action.slice('popup:ROUTE:'.length)),want=['r4','r3','r2','r1'];
 assert.deepEqual(order,want.slice(0,order.length),label);
 for(const hit of listed){
  const s=rows[hit.row];
  // A value is whole or absent: no cut PROGRESS, time, model or glyph; only the title ends in `…`.
  assert(!/execute…|one-shot…|gpt[^ ]*…|1h…|\d…/.test(s)&&!/ex…|exec…/.test(s),label);
  if(/\d\/2/.test(s))assert(/2\/2 execute ●/.test(s),label);
  if(s.includes('one-shot'))assert(/one-shot ✕/.test(s),label);
  if(s.includes('27m'))assert(s.includes('1h 27m'),label);
  if(s.includes('execute')||s.includes('one-shot'))withProgress++;else withoutProgress++;
  // PROGRESS goes before AGE: a row that kept PROGRESS kept its AGE.
  if((s.includes('execute')||s.includes('one-shot'))&&route_line==='time')assert(s.includes('1h 27m'),label);
 }
}
assert(withProgress>0&&withoutProgress>0,'the probe reaches both the full and the short form');
"""
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
// 11-15: the question's summary under its label row (the editor, focused or not, reads in the popup).
const question={count:1,label:'QUESTIONS',rows:[[{text:'◑ '},{text:'REVIEW1463: 결과를 어디에 둘까요?'}],[{text:'  '},{text:'1 현재 프로젝트 (추천) · 2 다른 폴더'}]],
 one:[[{text:'◑ '},{text:'REVIEW1463: 결과를 어디에 둘까요?'}]],line:[{text:'REVIEW1463: 결과를 어디에 둘까요?'}],choices:2};
const routes=[{id:'r',title:'REVIEW1463_ROUTE',state:'blocked',kind:'blocked',paint:{glyph:{text:'◑'}}}];
const paint=(width,height,side,focused,model)=>view.render({width,height,side,fill:false,focused,sid:'s',configuration:{},
 model:{...model,answerable:true,routes,route_count:1},brief:()=>question})
 .rows.map(r=>r.map(p=>p.text).join(''));
for(const [width,height,side,focused] of [[80,14,false,false],[80,14,false,true],[35,21,true,false],[35,26,true,false],[80,10,false,false]]){
 const rows=paint(width,height,side,focused,models.short);
 for(const want of ['REVIEW1463_TITLE','REVIEW1463_JOB','1 QUESTIONS','◑ REVIEW1463'])
  assert(rows.some(s=>s.includes(want)),`${width}x${height} focused=${focused} lost ${want}\n`+rows.join('\n'));
 const branch=rows.findIndex(s=>s.includes('(values-fix'));
 if(width===35&&height===26)assert(branch>=0,'a roomy side panel keeps the Git row\n'+rows.join('\n'));
 // The Git row sits right below the repo row and never shares it.
 // Spec rule 29: the number moves to the top border; the repo row keeps `project/`.
 if(branch>=0)assert(rows[branch-1].includes('project/')&&!rows.slice(1).some(s=>s.includes('[cb]')),rows.join('\n'));
 assert(rows.length<=height);
}
// A long title is shortened with an ellipsis; the answer wait (the QUESTIONS head's count) keeps its place.
const rows=paint(80,14,false,false,models.long);
assert(rows.some(s=>s.includes('REVIEW1463_TITLE_AAA')&&s.includes('…')),rows.join('\n'));assert(rows.some(s=>s.includes('1 QUESTIONS')),rows.join('\n'));
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
        # Spec E9 rule 34 (a), (e), 11-15: a route row lets PROGRESS go whole before its title is cut
        # (the title ends in `…`, never a cut value), and TITLE and NOW fold only between tokens.
        script = self.FIXTURE + r"""
const rows=draw([oneShot,staged,failed],40,76),all=rows.join('\n');
for(const s of rows)assert.equal(cells(s),40,s);
// One row per route: glyph, title, PROGRESS whole, AGE whole; the failed first.
const at=title=>rows.findIndex(s=>s.includes(title));
assert(at('PG-50')>0&&at('PG-50')<at('셸 상태')&&at('셸 상태')<at('세션 간'),all);
assert(/^│ ✕ PG-50 휠 입력 +2\/3 execute ✕ +54m │$/.test(rows[at('PG-50')]),all);
assert(/^│ ⠋ 셸 상태·표시… +one-shot ● +41m │$/.test(rows[at('셸 상태')]),all);
assert(/^│ ⠋ 세션 간 소통… +3\/7 execute ● +1h 0m │$/.test(rows[at('세션 간')]),all);
// Nothing opens in place: no owner, doer, tool or stage rows hang under a route.
for(const gone of ['OWNER','DOER','KIND','>_ Edit','┆','impl-review'])assert(!all.includes(gone),gone+'\n'+all);
// Narrower, the title is cut with `…` and PROGRESS goes whole before AGE; no value is cut apart.
for(const width of [28,32,36]){
 const narrow=draw([oneShot,staged,failed],width,76);
 for(const s of narrow){assert.equal(cells(s),width,s);assert(!/execut…|one-sho…|1h …|\d…|exec…/.test(s)&&!/ [a-z0-9]…/.test(s.replace(/[가-힣]+…/g,'')),s+'\n'+narrow.join('\n'));}
 const row=narrow.find(s=>s.includes('PG-50'));assert(row.includes('54m')||!row.includes('execute'),row);
}
// The same function breaks TITLE and NOW at word boundaries, as before.
const title=rows.findIndex(s=>s.includes('TITLE'));
assert.deepEqual(rows.slice(title,title+6).map(s=>s.slice(2,-1).trimEnd()),['TITLE  하팅 v4-open 감독 — 셸','       상태·표시 고치기와 Fleet 세션',
 '       줄 다듬기','NOW    검토 결과 3건을 기다리는','       중이며 CI 결과를 확인하고','       있음'],all);
// With room the whole values stand on one row each.
const wide=draw([oneShot,staged,failed],80,76,{side:false}).join('\n');
for(const want of ['PG-50 휠 입력','2/3 execute ✕','54m','3/7 execute ●','one-shot ●'])assert(wide.includes(want),want+'\n'+wide);
"""
        self.node(script, "panel-view.cjs")

    def test_route_rows_outlast_message_bodies_and_the_rule_outlasts_empty_rows(self):
        # Spec E9 rule 34 (b), (c), 11-15: empty rows, then a value's extra rows, then the sections' rows
        # from the lowest section up (MESSAGES before ROUTE) go first; the rule under the metadata
        # stays while TITLE or NOW takes more than one row.
        script = self.FIXTURE + r"""
const nine=[...Array(9)].map((_,i)=>{const r=[oneShot,staged,waiting][i%3];return {...r,id:'n'+i,title:r.title+' '+(i+1)};});
const routeRows=rows=>rows.filter(s=>/^│ \S (셸|세션|새 판)/.test(s)).length;
const section=rows=>{const m=rows.findIndex(s=>s.startsWith('│ MESSAGES'));return m<0?[]:rows.slice(m+1,rows.findIndex((s,i)=>i>m&&!/^│ [←→] /.test(s)));};
// 40×44: ROUTE keeps its three rows and MESSAGES its three (one row each, never two).
const r44=draw(nine,40,44),m44=section(r44);
assert(routeRows(r44)===3&&m44.length===3&&m44.every(s=>/^│ [←→] \[/.test(s)),r44.join('\n'));
// Rows to spare never grow a section past its three rows; they stay empty under the last section.
for(const h of [44,60,72]){const rows=draw(nine,40,h);assert(routeRows(rows)===3&&section(rows).length===3,h+'\n'+rows.join('\n'));}
for(let h=16;h<=72;h++){
 const rows=draw(nine,40,h),all=rows.join('\n'),body=section(rows);
 // The lowest section gives its rows first: MESSAGES never has more rows than ROUTE once ROUTE is short of its three.
 if(routeRows(rows)<3&&rows.some(s=>s.startsWith('│ MESSAGES')))assert(body.length<=routeRows(rows),h+'\n'+all);
 // A message is one row: never a body row under its head.
 assert(!rows.some(s=>/^│   \S/.test(s)),h+'\n'+all);
 // The rule under the metadata goes only once TITLE and NOW are one row each.
 const title=rows.findIndex(s=>s.startsWith('│ TITLE'));
 if(title>0&&/^│ {8}\S/.test(rows[title+1]))assert(/^│ [─━]+ │$/.test(rows[title-1]),h+'\n'+all);
}
// Empty rows go before the rule does: with room to spare the rule stands.
const tall=draw(nine,40,60);
assert(/^│ [─━]+ │$/.test(tall[tall.findIndex(s=>s.startsWith('│ TITLE'))-1]),tall.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_the_steward_number_holds_and_two_structural_empty_rows_part_the_groups(self):
        # Spec E9 rule 35: (a) a steward's number does not blink, only `⚑` changes between
        # beats; (b) one empty row parts [repository, Git] from [model, context]; (c) one
        # empty row parts the head from the first section, and the rows the sections leave
        # stay empty under the last one; (d) both are structural rows: they go after the
        # values on several rows and the sections' rows, the metadata one before the rule.
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
// (c) Six routes and eight messages at 40×58: the sections stand from the top, three rows at most each
// (`6 ROUTE`, `MESSAGES +5`), the one parting row above the first section, and the rows they leave stay
// empty under the last section (11-15: no route box, no message bodies).
const more=[...messages,message('←','[52]','08:10','두 번째 검토 — 줄이는 순서를 확인해 주세요, 본문이 길어서 둘째 줄까지 넘어감',6),
 message('→','[73]','08:20','PR 출판 요청 두 번째',7,'전달됨'),message('←','[e7]','08:30','설치 끝',8)];
const six=[...Array(6)].map((_,i)=>{const r=[oneShot,staged,waiting][i%3];return {...r,id:'s'+i,title:r.title+' '+(i+1)};});
const QUESTION={count:1,label:'QUESTIONS',rows:[[P('◑ '),P('Q_HEAD')],[P('  '),P('1 A · 2 B')]],one:[[P('◑ '),P('Q_HEAD')]],line:[P('Q_HEAD')],choices:2};
const paint=(routes,height,o={})=>view.render({width:40,height,side:true,fill:true,model:{...head,messages:o.messages||more,routes,route_count:routes.length,answerable:!!o.q,...(o.model||{})},
 state:'idle',configuration:{},brief:()=>o.q||null}).rows.map(r=>r.map(p=>p.text).join(''));
const rule=rows=>rows.findIndex(s=>s.startsWith('│ MESSAGES'));
const first=rows=>rows.findIndex(s=>/^│ (\d+ )?(QUESTIONS|ROUTE) /.test(s));
const full=paint(six,58),m=rule(full),q0=first(full);
assert(q0>0&&blank(full[q0-1])&&/^│ 1 QUESTIONS ─+ › │$/.test(full[q0])&&/^│ 6 ROUTE ─+ › │$/.test(full[q0+1])&&m>q0+5&&/^│ MESSAGES \+5 ─+ › │$/.test(full[m]),full.join('\n'));
// MESSAGES keeps to the panel's bottom (spec rule 14): its rows end right above the 5h/7d row, the spare rows
// stand between the routes and it.
assert(full.slice(m+1,m+4).every(s=>/^│ [←→] \[/.test(s))&&m+4===full.length-2&&full.slice(q0+5,m).every(blank)&&full.at(-2).includes('7d 66%'),full.join('\n'));
// With a question the summary stands between the QUESTIONS head and the routes (11-15: right above them).
const asked=paint([oneShot,staged],44,{q:QUESTION,messages}),q=asked.findIndex(s=>s.includes('1 QUESTIONS ─'));
assert(q>2&&blank(asked[q-1])&&asked[q+1].includes('◑ Q_HEAD')&&asked[q+2].includes('1 A · 2 B')&&/^│ 2 ROUTE /.test(asked[q+3])&&rule(asked)>=q+6,asked.join('\n'));
// No routes: no ROUTE head stands, and the parting row still stands above the first section.
const none=paint([],30,{model:{routes_known:true}});
assert(!none.join('').includes('ROUTE')&&blank(none[first(none)-1])&&first(none)>0&&rule(none)>first(none),none.join('\n'));
// (d) Short of rows: the parting row goes first, then values on several rows, then the sections' rows
// (the lowest section first), then the metadata row, then the rule under it.
const flags=h=>{const rows=paint(six,h),at=first(rows),git=rows.findIndex(s=>s.includes('(fix/')),title=rows.findIndex(s=>s.startsWith('│ TITLE'));
 return {spacer:at>0&&blank(rows[at-1]),meta:git>0&&blank(rows[git+1]),ruled:title>0&&/^│ ─+ │$/.test(rows[title-1]),multi:title>0&&/^│ {8}\S/.test(rows[title+1]),
  route:rows.filter(s=>/^│ \S (셸|세션|새)/.test(s)).length,messages:rows.filter(s=>/^│ [←→] \[/.test(s)).length};};
for(let h=14;h<=72;h++){
 const f=flags(h),all=paint(six,h).join('\n'),whole=f.route===3&&f.messages===3;
 // The parting row and the several-row values stand only while every section keeps its three rows.
 if(f.spacer||f.multi)assert(whole,h+' sections give way after the empty rows and multi-row values\n'+all);
 // The metadata row goes before the rule under it; neither goes while a value takes several rows.
 if(f.meta)assert(f.ruled,h+' the metadata row goes before the rule\n'+all);
 if(f.multi)assert(f.meta&&f.ruled,h+' values on several rows go before either row\n'+all);
 // The lowest section gives its rows first.
 assert(f.messages<=f.route||f.route===3,h+' MESSAGES gives way before ROUTE\n'+all);
}
// The steps at this fixture's heights: 24 gives up the parting row, 20 the several-row values,
// 15 the metadata row and 14 the rule (the sections give their rows between 20 and 15).
assert.deepEqual(flags(24),{spacer:false,meta:true,ruled:true,multi:true,route:3,messages:3});
assert.deepEqual(flags(20),{spacer:false,meta:true,ruled:true,multi:false,route:3,messages:3});
assert.deepEqual(flags(15),{spacer:false,meta:false,ruled:true,multi:false,route:1,messages:1});
assert.equal(flags(14).ruled,false);
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
// section under the messages, one row (11-15), its age at the right and its type left out; the head has no MEM row.
const title=r50.findIndex(s=>s.startsWith('│ TITLE')),mem=r50.findIndex(s=>/^│ MEM ─+ │$/.test(s)),msg=r50.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(title>0&&!r50.slice(0,msg).some(s=>s.includes('MEM'))&&mem>msg&&r50[mem+1].startsWith('│ ◆ ')&&!r50[mem+1].includes('decision')
 &&r50[mem+1].includes('…')&&/ 3m │$/.test(r50[mem+1])&&mem+2===r50.length-2&&!r50.join('').includes('지어낸 기억'),r50.join('\n'));
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

    def test_section_heads_are_upper_case_with_the_count_first(self):
        # Spec E9 rule 45, 11-15: beside the native area `2 ROUTE` (the count first, its word upper case,
        # the running pulse kept), `MESSAGES`, the producer's own label too, each with a thin rule to `›`;
        # the bottom layout keeps `route N`.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const owned={...oneShot,role:'owner',kind_word:'code'};
const at=(o={})=>view.render({width:o.width||40,height:60,side:o.side!==false,fill:o.side!==false,model:{...head,routes:[owned,staged],route_count:2,...(o.model||{})},
 state:'working',configuration:{},questions:()=>[],...(o.extra||{})});
const v=at(),rows=plain(v),all=rows.join('\n');
assert(rows.some(s=>/^│ 2 ROUTE ─+ › │$/.test(s))&&rows.some(s=>/^│ MESSAGES \+2 ─+ › │$/.test(s))&&!/\broute \d|messages/.test(all),all);
// The count keeps the running pulse; the word is the dim title.
const count=v.rows.flat().find(p=>p.text==='2'&&p.beat===true);assert(count,'the running count pulses');
const word=v.rows.flat().find(p=>p.text==='ROUTE');assert(word.dim===true&&word.role==='title',JSON.stringify(word));
// The producer's label alone (no rows yet): its count first too, its tally after the word.
const label=plain(at({model:{routes:[],route_count:0,route_label:'route 3 ⠏2'}}));
assert(label.some(s=>/^│ 3 ROUTE ⠏2 ─+ › │$/.test(s)),label.join('\n'));
// The bottom layout: `route N`, `messages`.
const bottom=plain(at({width:120,side:false})).join('\n');
assert(/│ route 2 ─+ › │/.test(bottom)&&/│ messages \+2 ─+ › │/.test(bottom)&&!/ROUTE|MESSAGES/.test(bottom),bottom);
assert(plain(at({width:120,side:false,model:{routes:[],route_count:0,route_label:'route 3 ⠏2'}})).some(s=>/^│ route 3 ⠏2 ─+ › │$/.test(s)));
"""
        self.node(script, "panel-view.cjs")

    def test_messages_and_mem_keep_to_the_panels_bottom(self):
        # Spec rule 14 (11-11, "메시지는 하단에 고정"; the 11-15 correction of #1638's "spare rows at the
        # bottom"): MESSAGES and MEM stand against the panel's bottom, right above the 5h/7d row; the spare
        # rows stand between the sections above (QUESTIONS, ROUTE, LAB, SESSIONS: their places and order as
        # before, from the top) and MESSAGES. Without MESSAGES, MEM; with neither, the spare rows are below.
        # The bottom layout keeps its label alone.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const blank=s=>/^│ +│$/.test(s);
const tall=(height,o={})=>plain(view.render({width:o.width||40,height,side:o.side!==false,fill:o.side!==false,model:{...head,routes:[oneShot,staged],route_count:2,...(o.model||{})},
 state:'idle',configuration:{},questions:()=>[]}));
for(const height of [50,70,90]){
 const rows=tall(height),r=rows.findIndex(s=>/^│ 2 ROUTE /.test(s)),m=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
 assert(r>0&&/^│ 1 QUESTIONS /.test(rows[r-1])&&blank(rows[r-2]),height+'\n'+rows.join('\n'));
 // MESSAGES keeps its rows (at most three) against the 5h/7d row; the blank rows stand above its head.
 const last=rows.findLastIndex(s=>/^│ [←→] /.test(s));
 assert(last===rows.length-3&&/⚙ │$/.test(rows.at(-2))&&m>r+3&&m+4===rows.length-2,height+'\n'+rows.join('\n'));
 assert(blank(rows[m-1])&&rows.slice(r+3,m).every(blank)&&rows.slice(r+1,r+3).every(s=>/^│ \S/.test(s)),height+'\n'+rows.join('\n'));
 // The press targets went with their rows.
 const v=view.render({width:40,height,side:true,fill:true,model:{...head,routes:[oneShot,staged],route_count:2},state:'idle',configuration:{},questions:()=>[]});
 assert(v.hits.some(h=>h.row===m&&h.action==='popup:MESSAGES')&&v.hits.some(h=>h.row===m+1&&h.action==='popup:MESSAGES'),JSON.stringify(v.hits));
}
// MEM under MESSAGES: its last row right above 5h/7d, the blank rows above MESSAGES.
{const rows=tall(50,{model:{memory_items:[{kind:'decision',age:'3m',text:'MEM_ONE'},{kind:'fact',age:'1h',text:'MEM_TWO'}]}}),
 m=rows.findIndex(s=>s.startsWith('│ MESSAGES')),e=rows.findIndex(s=>/^│ MEM ─+ │$/.test(s)),s=rows.findIndex(s=>s.includes('ROUTE_')||/^│ 2 ROUTE /.test(s));
 assert(m>0&&e>m&&/MEM_TWO/.test(rows[e+2])&&e+3===rows.length-2&&blank(rows[m-1]),rows.join('\n'));}
// No MESSAGES: MEM keeps to the bottom. Neither: the sections stand from the top, the blank rows are below.
{const mem=tall(50,{model:{messages:[],memory_items:[{kind:'decision',age:'3m',text:'MEM_ONE'}]}}),e=mem.findIndex(s=>/^│ MEM ─+ │$/.test(s));
 assert(e>0&&e+2===mem.length-2&&blank(mem[e-1]),mem.join('\n'));
 const none=tall(50,{model:{messages:[]}}),r=none.findIndex(s=>/^│ 2 ROUTE /.test(s));
 assert(r>0&&none.slice(r+3,-2).every(blank)&&!none.some(s=>/MEM|MESSAGES/.test(s)),none.join('\n'));}
// No overlap and no overflow at any height 5-60 and width 12-60, beside the native area and below it: the rows
// stay within the height, each as wide as the panel, and a press target stands on a row of its own section.
const mem=[{kind:'decision',age:'3m',text:'MEM_ONE'},{kind:'fact',age:'1h',text:'MEM_TWO'}];
const wide=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
for(const side of [true,false])for(let height=5;height<=60;height+=side?1:5)for(let width=12;width<=60;width+=4){
 const v=view.render({width,height,side,fill:side,model:{...head,routes:[oneShot,staged],route_count:2,memory_items:mem},state:'idle',configuration:{},questions:()=>[]});
 const rows=plain(v),label=[side,height,width].join(' ')+'\n'+rows.join('\n');
 assert(rows.length<=height&&rows.every(s=>wide(s)===width),label);
 assert(v.hits.every(h=>h.row>=0&&h.row<rows.length),label);
 const at=rows.findIndex(s=>s.startsWith('│ MESSAGES')||s.startsWith('│ messages')),m=rows.findIndex(s=>/^│ MEM /.test(s));
 if(side&&at>=0&&m>at)assert(rows.slice(at,m).filter(blank).length===0,label);
 for(const h of v.hits.filter(h=>h.action==='popup:MESSAGES'))assert(h.row>=at&&(m<0||h.row<m),label);
}
const bottom=tall(14,{side:false,width:120});
assert(bottom.some(s=>/^│ messages( \+\d+)? ─+ › │$/.test(s))&&!bottom.some(s=>/MESSAGES/.test(s)),bottom.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_messages_show_three_at_most_however_many_rows_are_left(self):
        # 11-14 night ("메시지 뜨는 최대 개수를 좀 줄이자. 세로폭이 아무리 길어도 말이야"): beside the native area
        # no more than three messages show whatever the height; the head counts the rest
        # (`MESSAGES +N`). 11-15: one row each; spare rows never lengthen a body.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const more=[...messages,message('←','[52]','08:10','두 번째 검토',6),message('→','[73]','08:20','PR 출판 요청 두 번째',7,'전달됨'),message('←','[e7]','08:30','설치 끝',8)];
const at=(list,height)=>plain(view.render({width:40,height,side:true,fill:true,model:{...head,messages:list,routes:[oneShot],route_count:1},state:'idle',configuration:{},questions:()=>[]}));
const heads=rows=>rows.filter(s=>/^│ [←→] \[/.test(s));
for(const list of [messages,more])for(const height of [60,90,140]){
 const rows=at(list,height),label=rows.find(s=>s.startsWith('│ MESSAGES'));
 assert.equal(heads(rows).length,3,height+'\n'+rows.join('\n'));
 assert(new RegExp('^│ MESSAGES \\+'+(list.length-3)+' ─+ › │$').test(label),label);
 // The newest three, oldest first.
 assert.deepEqual(heads(rows).map(s=>s.slice(4,8)),list.slice(-3).map(m=>m.who),rows.join('\n'));
}
// Three or fewer: all of them, and no count.
for(const n of [1,3]){const rows=at(messages.slice(0,n),90);assert.equal(heads(rows).length,n);assert(rows.some(s=>/^│ MESSAGES ─+ › │$/.test(s)),rows.join('\n'));}
// Spare rows never give a message a second row: a long body is one row ending in `…`.
const long=[...messages.slice(0,2),message('←','[52]','08:10','두 번째 검토 — 줄이는 순서를 확인해 주세요, 본문이 길어서 둘째 줄까지 넘어가고 셋째 줄까지 이어지는 긴 본문입니다',6)];
const rows=at(long,90),i=rows.findIndex(s=>s.startsWith('│ ← [52] 두 번째'));
assert(i>0&&rows[i].includes('…')&&!rows.some(s=>/^│   \S/.test(s)),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_each_section_keeps_two_or_three_rows_at_40_and_one_at_20(self):
        # 11-15 (popup design E): a side panel 44 wide with every section; at 40 rows QUESTIONS keeps its
        # summary's two rows (and the guide line), ROUTE, LAB, SESSIONS and MESSAGES three rows, MEM two;
        # at 20 each keeps one. The heads in order and the press actions of every head and row.
        script = r"""
const assert=require('node:assert/strict'),pv=require(process.argv[1]);
const p=(text,extra={})=>({text,...extra});
const model={tag:'[e9]',repo:[p('hearting/')],git_side:[p('popup-2 ✓')],selected:[p('Opus 5.5'),p(' xhigh')],context_side:[p('━━─── '),p('46%')],
 session_title:'질문·route 팝업',now:'그리는 중',wait:'◑답 대기 3',wait_guide:'1 QUESTIONS · 입력창에 답',route_label:'route 4 ⠏2 ✕1',route_count:4,route_total:4,
 routes:[{id:'a',title:'질문 팝업 구현 — 창 가운데 팝업',kind:'working',state:'working',shape:'staged',stages:[{name:'plan',mark:'✓'},{name:'execute',mark:'●',current:true}],elapsed:'23m',paint:{glyph:p('⠏')}},
  {id:'b',title:'휠 입력 시험',kind:'dead',state:'dead',shape:'one-shot-owner',stages:[{name:'one-shot',mark:'✕'}],elapsed:'54m',paint:{glyph:p('✕')}},
  {id:'c',title:'ROUTE 줄 누름 시험',kind:'working',shape:'one-shot-owner',stages:[],elapsed:'14m',paint:{glyph:p('⠏')}},
  {id:'d',title:'MESSAGES 구분선',kind:'done',state:'done',outcome:'PASS',outcome_key:'lvl_g',shape:'one-shot-owner',elapsed:'31m',paint:{glyph:p('✓')}}],
 lab:{count:'4',counts:[[p('실패 1')],[p('대기 1')]],goal:[p('SI-SDR 13 dB')],rows:[
  {name:p('긴 문맥'),cells:{state:p('실패'),progress:p('9/60 ep'),delta:p('-2.20 dB')}},{name:p('큰 학습률'),cells:{state:p('진행 중'),progress:p('36/60 ep'),delta:p('+0.5 dB')}},
  {name:p('작은 배치'),cells:{state:p('진행 중'),progress:p('?'),delta:p('-0.6 dB')}},{name:p('기준 모델'),cells:{state:p('대기'),progress:p('40/40 ep'),delta:p('·')}}]},
 sessions:{count:4,counts:[[p('◑1')],[p('⠏1')],[p('●2')]],rows:[
  {glyph:[p('◑')],tag:[p('['),p('e9'),p(']')],title:[p('own')],age:[p('1m')],own:true},
  {glyph:[p('◑')],tag:[p('['),p('12'),p(']')],title:[p('분리 모델 학습 비교')],age:[p('41m')]},
  {glyph:[p('⠏')],tag:[p('['),p('73'),p(']')],title:[p('v4.32.7 설치 확인')],age:[p('12m')]},
  {glyph:[p('●')],tag:[p('['),p('e7'),p(']')],title:[p('#1640 검토')],age:[p('1h')]},
  {glyph:[p('●')],tag:[p('['),p('52'),p(']')],title:[p('넷째 다른 세션')],age:[p('2h')]}]},
 messages:[{way:'←',who:'[e7]',text:'CI 초록, 병합해도 됩니다'},{way:'→',who:'[73]',text:'설치 확인 부탁'},{way:'←',who:'[12]',text:'답 기다립니다'},{way:'←',who:'[52]',text:'정리 끝'}],
 memory_items:[{kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'},{kind:'fact',age:'1h',text:'팝업은 Fleet 표'}],
 quota:[p('5h 18%'),p(' │ '),p('7d 64%')]};
const brief=()=>({count:2,label:'QUESTIONS',rows:[[p('◑ 패널을 접었을 때의 모양을 어느 안으로 구현할까요?')],[p('  1 아래 두 줄 (추천) · 2 넓힌 옆 띠 · +2')]],
 one:[[p('◑ 패널을 접었을 때의 모양을 어느 안으로')]],line:[p('패널을 접었을 때')],choices:4});
const paint=height=>pv.render({width:44,height,side:true,model,native:{},state:'working',sid:'s',harness:'claude',frame:0,brief,sent:()=>[]});
const HEADS=[['QUESTIONS',/^│ 3 QUESTIONS ─+ › │$/],['ROUTE',/^│ 4 ROUTE ⠏2 ✕1 ─+ › │$/],['LAB',/^│ 4 LAB ─+ 실패 1 대기 1 › │$/],['SESSIONS',/^│ 4 SESSIONS ◑1 ⠏1 ●2 ─+ › │$/],
 ['MESSAGES',/^│ MESSAGES \+\d+ ─+ › │$/],['MEM',/^│ MEM ─+ │$/]];
// The rows under each head, up to the next head (MEM: its `◆` rows).
const sections=rows=>{const at=HEADS.map(([name,re])=>[name,rows.findIndex(s=>re.test(s))]);
 assert(at.every(([,i])=>i>0),rows.join('\n'));assert(at.every(([,i],k)=>!k||i>at[k-1][1]),'order of the heads\n'+rows.join('\n'));
 const out={};at.forEach(([name,i],k)=>{const end=k+1<at.length?at[k+1][1]:rows.findIndex((s,j)=>j>i&&!/^│ ◆ /.test(s));const body=rows.slice(i+1,end);while(body.length&&/^│ +│$/.test(body.at(-1)))body.pop();out[name]={head:i,rows:body,from:i+1};});return out;};
for(const [height,rowsOf] of [[40,{QUESTIONS:2,ROUTE:3,LAB:3,SESSIONS:3,MESSAGES:3,MEM:2}],[20,{QUESTIONS:1,ROUTE:1,LAB:1,SESSIONS:1,MESSAGES:1,MEM:1}]]){
 const v=paint(height),rows=v.rows.map(r=>r.map(x=>x.text).join('')),sec=sections(rows),label=height+'\n'+rows.join('\n');
 assert(rows.length<=height&&rows.at(-2).includes('7d 64%'),label);
 // QUESTIONS: the summary's rows (the guide line stands under them while there is room), the others: their rows.
 const summary=sec.QUESTIONS.rows.filter(s=>!/입력창에 답/.test(s));
 assert.equal(summary.length,rowsOf.QUESTIONS,label);assert(summary[0].startsWith('│ ◑ 패널을'),label);
 assert.equal(sec.QUESTIONS.rows.some(s=>/입력창에 답/.test(s)),height===40,label);
 for(const name of ['ROUTE','LAB','SESSIONS','MESSAGES','MEM'])assert.equal(sec[name].rows.length,rowsOf[name],name+' '+label);
 // Press actions: the question's head and rows open it; every head and row of ROUTE/LAB/SESSIONS/MESSAGES its popup;
 // a route's and a session's own row opens with it unfolded; MEM opens nothing.
 const act=i=>v.hits.filter(h=>h.row===i).map(h=>h.action).filter(a=>a!=='fold'&&a!=='settings');
 for(let i=sec.QUESTIONS.head;i<=sec.QUESTIONS.head+sec.QUESTIONS.rows.length;i++)assert.deepEqual(act(i),['question'],'question '+i+label);
 assert.deepEqual(act(sec.ROUTE.head),['popup:ROUTE']);assert.deepEqual(act(sec.ROUTE.from),['popup:ROUTE:b']);
 if(height===40)assert.deepEqual([0,1,2].map(k=>act(sec.ROUTE.from+k)[0]),['popup:ROUTE:b','popup:ROUTE:a','popup:ROUTE:c'],label);
 assert.deepEqual(act(sec.LAB.head),['popup:LAB']);sec.LAB.rows.forEach((_,k)=>assert.deepEqual(act(sec.LAB.from+k),['popup:LAB']));
 assert.deepEqual(act(sec.SESSIONS.head),['popup:SESSIONS']);assert.deepEqual(act(sec.SESSIONS.from),['popup:SESSIONS:12']);
 if(height===40)assert.deepEqual([0,1,2].map(k=>act(sec.SESSIONS.from+k)[0]),['popup:SESSIONS:12','popup:SESSIONS:73','popup:SESSIONS:e7'],label);
 assert.deepEqual(act(sec.MESSAGES.head),['popup:MESSAGES']);sec.MESSAGES.rows.forEach((_,k)=>assert.deepEqual(act(sec.MESSAGES.from+k),['popup:MESSAGES']));
 assert.deepEqual(act(sec.MEM.head),[]);sec.MEM.rows.forEach((_,k)=>assert.deepEqual(act(sec.MEM.from+k),[]));
 // The routes read failed first; a LAB row whose PROGRESS is unknown shows Fleet's `?`.
 assert(sec.ROUTE.rows[0].includes('✕ 휠 입력 시험')&&/one-shot ✕/.test(sec.ROUTE.rows[0]),label);
 if(height===40){const unknown=sec.LAB.rows.find(s=>s.includes('작은 배치'));assert(unknown&&/ \? /.test(unknown),label);
  assert(sec.SESSIONS.rows.every(s=>!s.includes('[e9]')),'this window\'s own session is left out');
  assert.deepEqual(sec.MESSAGES.rows.map(s=>s.slice(2,10)),['→ [73] 설','← [12] 답','← [52] 정'].map(s=>s.slice(0,8)));}
 // MESSAGES and MEM keep to the bottom: MEM's last row is right above the 5h/7d row.
 assert(/^│ ◆ /.test(rows.at(-3))&&rows.at(-2).includes('7d 64%'),label);
}
// Taller than the sections need: the blank rows stand between SESSIONS and the MESSAGES head, not under MEM.
{const v=paint(48),rows=v.rows.map(r=>r.map(x=>x.text).join('')),sec=sections(rows),blank=s=>/^│ +│$/.test(s),label=rows.join('\n');
 const end=sec.SESSIONS.from+sec.SESSIONS.rows.length;
 assert(sec.MESSAGES.head-end>=1&&rows.slice(end,sec.MESSAGES.head).every(blank),label);
 assert(/^│ ◆ /.test(rows.at(-3))&&rows.at(-2).includes('7d 64%')&&sec.MEM.rows.length===2,label);
 // The press targets of the moved rows moved with them.
 const act=i=>v.hits.filter(h=>h.row===i).map(h=>h.action).filter(a=>a!=='fold'&&a!=='settings');
 assert.deepEqual(act(sec.MESSAGES.head),['popup:MESSAGES']);assert.deepEqual(act(sec.MESSAGES.from),['popup:MESSAGES']);
 assert.deepEqual(act(sec.SESSIONS.from),['popup:SESSIONS:12']);assert.deepEqual(act(end),[]);}
"""
        self.node(script, "panel-view.cjs")

    def test_a_low_panel_keeps_the_questions_head_and_first_row_from_five_rows(self):
        # PR #1648 review B: at 5 to 9 rows the panel kept only REPO/MODEL/CTX/TITLE/NOW and the question
        # went. QUESTIONS keeps its head and first row to the last (spec rule 34 (b)); the structural rows
        # above it (TITLE, NOW, ...) give their rows up first. Every height 5-60 at every width 12-60 keeps
        # its rows within the height, each as wide as the panel, and 5-9 rows (from width 20) show the question.
        script = r"""
const assert=require('node:assert/strict'),pv=require(process.argv[1]);
const p=(text,extra={})=>({text,...extra});
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const model={tag:'[e9]',repo:[p('hearting/')],git_side:[p('popup-2 ✓')],selected:[p('Opus 5.5'),p(' xhigh')],context_side:[p('━━─── '),p('46%')],
 session_title:'질문·route 팝업',now:'그리는 중',wait:'◑답 대기 3',wait_guide:'1 QUESTIONS · 입력창에 답',route_label:'route 1',route_count:1,route_total:1,
 routes:[{id:'a',title:'질문 팝업 구현',kind:'working',state:'working',shape:'one-shot-owner',stages:[],elapsed:'23m',paint:{glyph:p('⠏')}}],
 memory_items:[{kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'}],quota:[p('5h 18%'),p(' │ '),p('7d 64%')]};
const brief=()=>({count:3,label:'QUESTIONS',rows:[[p('◑ 패널을 접었을 때의 모양을 어느 안으로 구현할까요?')],[p('  1 아래 두 줄 (추천) · 2 넓힌 옆 띠 · +2')]],
 one:[[p('◑ 패널을 접었을 때의 모양을 어느 안으로')]],line:[p('패널을 접었을 때')],choices:4});
const paint=(height,width)=>pv.render({width,height,side:true,model,native:{},state:'working',sid:'s',harness:'claude',frame:0,brief,sent:()=>[]});
for(let h=5;h<=60;h++)for(let w=12;w<=60;w++){
 const v=paint(h,w),rows=v.rows.map(r=>r.map(x=>x.text).join('')),label=h+'x'+w+'\n'+rows.join('\n');
 assert(rows.length<=h&&rows.every(r=>cells(r)===w),label);
 if(w>=20&&h<=9){
  const head=rows.findIndex(r=>/^│ 3 QUESTIONS/.test(r));
  assert(head>=0&&/^│ ◑ /.test(rows[head+1]),label);
  // The press on the head or its row still opens the question popup.
  assert(v.hits.some(x=>x.row===head&&x.action==='question')&&v.hits.some(x=>x.row===head+1&&x.action==='question'),label+JSON.stringify(v.hits));
 }
}
// 5 rows: the head and first row between the borders and the fold row; 6 rows: REPO stands above them.
let rows=paint(5,44).rows.map(r=>r.map(x=>x.text).join(''));
assert(/^│ 3 QUESTIONS/.test(rows[1])&&/^│ ◑ /.test(rows[2])&&rows[3].endsWith('»   ⚙ │'),rows.join('\n'));
rows=paint(6,44).rows.map(r=>r.map(x=>x.text).join(''));
assert(/^│ REPO /.test(rows[1])&&/^│ 3 QUESTIONS/.test(rows[2]),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_the_question_to_answer_here_reads_as_one_button(self):
        # 11-15 (panel polish 1): in the open panel's QUESTIONS section the question a press answers
        # here, now, stands on the rose fill across the inside width: its first row bright, its
        # choices numbered chips, `↵ 답하기 · Ctrl+]` in rose at the right under them; a press on any
        # of its rows opens the question popup. The head row keeps the form of every other head. A
        # wait answered elsewhere and the answers on their way are never painted; nothing else in
        # the panel is. Short of rows the action row goes first, then the choices, the first row to
        # the last (heights 5-60 × widths 12-60, no row past the panel, none wider). The folded
        # strip's row of what to do takes the same fill. Mono, eight colors and NO_COLOR lead the
        # rows with a rose bar instead. The fill is one value per face (52 dark, 224 light).
        script = r"""
const assert=require('node:assert/strict'),pv=require(process.argv[1]);
const p=(text,extra={})=>({text,...extra});
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const styles={dim:{foreground:-1,dim:true},loc_repo:{foreground:253},g_blocked:{foreground:217},lvl_g:{foreground:150}};
const model={tag:'[e9]',repo:[p('hearting/')],selected:[p('Opus 5.5')],context_side:[p('━━─── '),p('46%')],wait:'◑답 대기 1',styles,
 labels:{answer_action:'↵ 답하기 · Ctrl+]'},session_title:'패널 손질',now:'그리는 중',route_label:'route 1',route_count:1,route_total:1,
 routes:[{id:'a',title:'휠 입력 시험',kind:'dead',state:'dead',shape:'one-shot-owner',stages:[],elapsed:'3m',paint:{glyph:p('✕')}}],
 messages:[{way:'←',who:'[73]',text:'설치 확인 끝났습니다'}],memory_items:[{kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'}],
 quota:[p('5h 18%'),p(' │ '),p('7d 64%')]};
const choice=(answerable=true)=>({count:1,label:'QUESTIONS',answerable,
 rows:[[p('◑ ',{foreground:217}),p('로그를 어느 쪽에 남길까요? 지금 둘 중 하나',{foreground:253})],
  [p('  ',styles.dim),p('1 ',styles.dim),p('파일',{foreground:253}),p(' · ',styles.dim),p('2 ',styles.dim),p('표준 출력',{foreground:253}),p(' (추천)',styles.dim),
   p(' · ',styles.dim),p('3 ',styles.dim),p('둘 다',{foreground:253})]],
 one:[[p('◑ ',{foreground:217}),p('로그를 어느 쪽에 남길까요?',{foreground:253})]],line:[p('로그를 어느 쪽에 남길까요?',{foreground:253})],choices:3});
const there={...choice(false),rows:[[p('◑ '),p('다른 세션의 질문')],[p('  '),p('그 창에서 답',styles.dim)]],choices:0};
const sent=()=>[[p('✓ 답 보냄',{foreground:150}),p(' · ',styles.dim),p('2 표준 출력',{foreground:253})]];
const paint=(o={})=>pv.render({width:o.width||44,height:o.height||30,side:o.folded?false:true,fill:!o.folded,folded:!!o.folded,
 model:{...model,...(o.model||{})},state:'idle',sid:'s',harness:'claude',frame:0,brief:()=>o.q,sent:o.sent||(()=>[]),configuration:o.cfg||{},background:o.background||''});
const str=r=>r.map(x=>x.text).join('');
const inside=r=>r.slice(2,-2);
const grounds=v=>v.rows.map(r=>r.some(x=>x.background!==undefined));
// (a) 256 colors: the head, then three painted rows; their every inside cell on the fill or a chip.
{const v=paint({q:choice(),sent}),rows=v.rows.map(str),head=rows.findIndex(r=>/^│ 1 QUESTIONS ─+ › │$/.test(r));
 assert(head>0,rows.join('\n'));assert.equal(rows[head+1],'│ ✓ 답 보냄 · 2 표준 출력                  │');
 const at=[head+2,head+3,head+4],painted=grounds(v);
 assert.deepEqual(painted.map((x,i)=>x?i:-1).filter(i=>i>=0),at,rows.join('\n'));
 for(const i of at){const parts=inside(v.rows[i]);
  assert.equal(parts.reduce((n,x)=>n+cells(x.text),0),40,rows[i]);
  assert(parts.every(x=>x.background===52||x.background===235),rows[i]+JSON.stringify(parts));}
 // The first row: `◑` rose, the words bright.
 const first=inside(v.rows[at[0]]);assert(str(first).startsWith('◑ 로그를 어느 쪽에 남길까요? 지금'),str(first));
 assert(first.find(x=>x.text==='◑').foreground===174&&first.filter(x=>/로그/.test(x.text)).every(x=>x.foreground===255),JSON.stringify(first));
 // The choices: numbered chips on the chip ground, one fill cell apart, the first under the words.
 const chips=[];let cur='';for(const x of inside(v.rows[at[1]])){if(x.background===235)cur+=x.text;else if(cur){chips.push(cur);cur='';}}if(cur)chips.push(cur);
 assert.deepEqual(chips,[' 1 파일 ',' 2 표준 출력 (추천) ',' 3 둘 다 '],rows[at[1]]);assert(rows[at[1]].startsWith('│   1 파일 '),rows[at[1]]);
 // The action at the right, in rose.
 assert(rows[at[2]].endsWith('↵ 답하기 · Ctrl+] │'),rows[at[2]]);
 assert(inside(v.rows[at[2]]).filter(x=>x.text.trim()).every(x=>x.foreground===174),JSON.stringify(v.rows[at[2]]));
 // A press on any painted row (and the head) opens the question popup.
 for(const i of [head,...at])assert(v.hits.some(h=>h.row===i&&h.action==='question'),String(i));
 // The sent line keeps its own colors, unpainted.
 assert(v.rows[head+1].find(x=>x.text==='✓ 답 보냄').foreground===150);}
// (b) A wait answered elsewhere, the sent line alone, no question: nothing painted, no action row.
for(const q of [there,null]){const v=paint({q,sent});assert(!grounds(v).some(Boolean),JSON.stringify(q));assert(!v.rows.map(str).join('').includes('↵'));}
// (c) Mono, eight colors, NO_COLOR: no ground anywhere, a rose bar leading each of the three rows.
for(const [cfg,m] of [[{theme:'mono'},{}],[{},{colors:8}],[{},{no_color:true}]]){
 const v=paint({q:choice(),cfg,model:m}),rows=v.rows.map(str);assert(!grounds(v).some(Boolean),JSON.stringify(cfg));
 const bars=rows.filter(r=>r.startsWith('│ ▌ '));assert.equal(bars.length,3,rows.join('\n'));
 assert(bars[0].startsWith('│ ▌ ◑ 로그를')&&bars[1].includes('1 파일 · 2 표준 출력')&&bars[2].endsWith('↵ 답하기 · Ctrl+] │'),bars.join('\n'));}
// (d) A light face and a shade keep the fill (the shade never covers it).
{const v=paint({q:choice(),cfg:{theme:'light'}}),r=v.rows.find(r=>str(r).includes('◑ 로그를'));
 assert(inside(r).every(x=>x.background===224)&&inside(r).filter(x=>/로그/.test(x.text)).every(x=>x.foreground===232),JSON.stringify(r));}
{const v=paint({q:choice(),cfg:{shade:'medium'},background:'dark'}),r=v.rows.find(r=>str(r).includes('◑ 로그를'));
 assert(inside(r).every(x=>x.background===52),JSON.stringify(r));}
// (e) Short of rows: the action row goes first, then the choices; the first row stays to the last.
// Every height 5-60 at every width 12-60 keeps its rows within the panel, each as wide.
const kinds=new Set();
for(let h=5;h<=60;h++)for(let w=12;w<=60;w++){
 const v=paint({q:choice(),height:h,width:w,sent}),rows=v.rows.map(str),label=h+'x'+w+'\n'+rows.join('\n');
 assert(rows.length<=h&&rows.every(r=>cells(r)===w),label);
 const painted=grounds(v).map((x,i)=>x?i:-1).filter(i=>i>=0),head=rows.findIndex(r=>/^│ 1 QUESTIONS/.test(r)||/^│ 1 Q/.test(r));
 if(w>=20)assert(painted.length>=1,label);
 if(!painted.length)continue;
 // One block, right under the head or the sent line, inside the frame.
 assert(painted.every((y,i)=>!i||y===painted[i-1]+1)&&painted[0]>0&&painted.at(-1)<rows.length-2,label);
 if(head>=0)assert(painted[0]===head+1||painted[0]===head+2,label);
 const action=rows[painted.at(-1)].includes('↵'),n=painted.length;
 assert(n===1&&!action||n===2&&!action||n===3&&action,label);
 kinds.add(n);
}
assert.deepEqual([...kinds].sort(),[1,2,3]);
// (f) The folded strip: its row of what to do on the fill across the whole width while it carries the
// question to answer here; a wait answered elsewhere leaves it plain; without color, the rose bar.
{const v=paint({q:choice(),folded:true,width:60,height:4}),i=v.rows.findIndex(r=>str(r).startsWith('◑답 대기 1'));
 assert(i>0&&v.rows[i].every(x=>x.background===52)&&v.rows[i].reduce((n,x)=>n+cells(x.text),0)===60,JSON.stringify(v.rows[i]));
 assert(grounds(v).filter(Boolean).length===1);assert(v.hits.some(h=>h.row===i&&h.action==='question'));}
{const v=paint({q:there,folded:true,width:60,height:4});assert(!grounds(v).some(Boolean));}
{const v=paint({q:choice(),folded:true,width:60,height:4,model:{colors:8}}),r=v.rows.map(str).find(r=>r.includes('◑답 대기'));
 assert(r.startsWith('▌ ◑답 대기 1 · 로그를')&&!grounds(v).some(Boolean),r);}
"""
        self.node(script, "panel-view.cjs")

    def test_the_veil_is_one_gray_per_face_and_none_where_no_hue_is_shown(self):
        # 11-15 (panel polish 3): the gray the host veils everything behind a standing popup with: 239 on
        # a dark or unknown face, 250 on a light one; none in mono, eight colors or NO_COLOR, where the
        # popup's frame line is bright (not dim) instead.
        script = r"""
const assert=require('node:assert/strict'),pv=require(process.argv[1]);
assert.equal(pv.veil({},{colors:256}),239);assert.equal(pv.veil({},{colors:1<<24,background:'dark'}),239);
assert.equal(pv.veil({theme:'light'},{colors:256}),250);assert.equal(pv.veil({},{colors:256,background:'light'}),250);
for(const [cfg,o] of [[{theme:'mono'},{colors:256}],[{},{colors:8}],[{},{colors:256,noColor:true}]])assert.equal(pv.veil(cfg,o),null,JSON.stringify([cfg,o]));
const styles={dim:{foreground:-1,dim:true}};
const lines=v=>v.rows.flat().filter(x=>/^[╭╮╰╯│─ ]+$/.test(x.text)&&/[╭╮╰╯│─]/.test(x.text));
const frame=(model,cfg={})=>lines(pv.popup({cols:100,rows:30,title:[{text:'T'}],body:[[{text:'x'}]],sections:['QUESTIONS'],model:{styles,...model},configuration:cfg}));
assert(frame({colors:256}).every(x=>x.dim===false&&x.foreground===undefined||x.dim===true||x.role==='messages'));
assert(frame({colors:256}).some(x=>x.dim===true));
for(const [m,cfg] of [[{colors:8},{}],[{colors:256,no_color:true},{}],[{colors:256},{theme:'mono'}]])
 assert(frame(m,cfg).every(x=>x.dim!==true),JSON.stringify([m,cfg]));
"""
        self.node(script, "panel-view.cjs")

    def test_mem_stands_under_the_messages_and_gives_way_first(self):
        # Spec E9 rule 46, 11-15: MEM leaves the head for its own section under the messages (its head
        # first and a thin rule on from it, as the messages' `MESSAGES`, no `›`: it opens no popup), two
        # records at most, one row each (`◆ kind headline   age`: the kind word stands in the row, there is no kind row; short of
        # cells the headline is cut first and then the kind word goes, the age stays); short of rows MEM
        # steps down to one record and then goes before any of the messages does.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const cellsOfRows=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const items=[{kind:'decision',age:'3m',text:'panel labels in dark grey, and the count leads its word so it reads as a count'},
 {kind:'lesson',age:'2h',text:'one fix per review finding'}];
const routes=[oneShot,staged,{...oneShot,id:'c'}];
const at=(o={})=>view.render({width:o.width||40,height:o.height||70,side:true,fill:true,model:{...head,routes,route_count:routes.length,memory_items:items,...(o.model||{})},
 state:'idle',configuration:{},questions:()=>[]});
const section=(rows,word)=>{const i=rows.findIndex(s=>new RegExp('^│ '+word+'( \\+\\d+)? ─+( ›)? │$').test(s));if(i<0)return null;let end=i+1;
 while(end<rows.length&&/^│ ◆ |^│ [←→] /.test(rows[end]))end++;return rows.slice(i+1,end);};
let v=at(),rows=plain(v);
const msg=rows.findIndex(s=>s.startsWith('│ MESSAGES')),mem=rows.findIndex(s=>/^│ MEM ─+ │$/.test(s));
assert(msg>0&&mem>msg&&!rows.slice(0,msg).some(s=>s.includes('MEM')),rows.join('\n'));
// Two records, one row each: the kind word after `◆`, the headline cut with `…`, its age at the right; no second row.
assert(/^│ ◆ decision panel labels in dark.*… +3m │$/.test(rows[mem+1])&&/^│ ◆ lesson one fix per review find… +2h │$/.test(rows[mem+2])
 &&!/^│ ◆ /.test(rows[mem+3]),rows.join('\n'));
// The kind word is dim as base's kind row was; short of cells the headline is cut first, then the kind word goes and the age stays.
assert(v.rows[mem+2].filter(p=>p.text==='lesson ').every(p=>p.dim===true)&&v.rows[mem+2].some(p=>p.text==='lesson '),JSON.stringify(v.rows[mem+2]));
for(const [w,re] of [[30,/^│ ◆ decision panel.*… +3m │$/],[22,/^│ ◆ decision .*… +3m │$/],[20,/^│ ◆ (?!decision).*… +3m │$/],[16,/^│ ◆ (?!decision).*… +3m │$/]]){
 // The kind word stays while the headline keeps four cells; short of that it goes first and the age stays.
 const r=plain(at({width:w})),m=r.findIndex(s=>/^│ MEM ─+ │$/.test(s));
 assert(re.test(r[m+1])&&r.every(s=>cellsOfRows(s)===w),w+'\n'+r.join('\n'));
}
// The head row's `◆` and age dim, the headline in the body color; the title as the messages' (dim, its word the title role).
assert(v.rows[mem+2].filter(p=>p.text.startsWith('one fix per review find')).length===1&&v.rows[mem+2].filter(p=>p.text.startsWith('one fix per review find')).every(p=>p.dim!==true)&&v.rows[mem+2].filter(p=>p.text==='2h'||p.text==='◆ ').every(p=>p.dim===true),JSON.stringify(v.rows[mem+2]));
const title=v.rows[mem].find(p=>p.text==='MEM'),mtitle=v.rows[msg].find(p=>p.text==='MESSAGES');
assert(title.role==='title'&&title.dim===true&&mtitle.role==='title'&&mtitle.dim===true);
// MEM opens no popup: no hit on its rows, no `›` on its head.
assert(!v.hits.some(h=>h.row>=mem&&h.row<=mem+2&&h.action!=='fold'&&h.action!=='settings'),JSON.stringify(v.hits));
// One record, none.
rows=plain(at({model:{memory_items:items.slice(1)}}));
const one=section(rows,'MEM');assert(one&&one.length===1&&one[0].startsWith('│ ◆ lesson one fix per review find'),rows.join('\n'));
assert(!plain(at({model:{memory_items:[]}})).join('').includes('MEM'));
// An age missing: the row keeps what is known; a record without a kind reads the same.
assert(plain(at({model:{memory_items:[{kind:'',age:'5m',text:'only an age'}]}})).some(s=>/^│ ◆ only an age +5m │$/.test(s)));
// No messages: MEM alone under its head.
rows=plain(at({model:{messages:[]}}));assert(!rows.join('').includes('MESSAGES')&&section(rows,'MEM').length===2,rows.join('\n'));
// Narrower than 30 cells inside: the same section.
rows=plain(at({width:32}));assert(section(rows,'MEM')?.[0].startsWith('│ ◆ decision panel labels'),rows.join('\n'));
// Short of rows (11-15: every section down to two rows and then one, the lowest first; then MEM goes
// whole, its records and then its head, before any message row or the MESSAGES head goes).
const memRows=rows=>rows.filter(s=>/^│ ◆ /.test(s)).length,memHead=rows=>rows.some(s=>/^│ MEM ─+ │$/.test(s));
const msgRows=rows=>rows.filter(s=>/^│ [←→] \[/.test(s)).length,msgHead=rows=>rows.some(s=>s.startsWith('│ MESSAGES'));
let oneRecord=0,noRecord=0,noHead=0,noMsgRow=0,noMsgHead=0;
for(let h=70;h>=8;h--){
 rows=plain(at({height:h}));
 assert(memRows(rows)<=2&&(!memHead(rows)||msgHead(rows)),h+'\n'+rows.join('\n'));
 if(memRows(rows)<2&&!oneRecord)oneRecord=h;if(!memRows(rows)&&!noRecord)noRecord=h;if(!memHead(rows)&&!noHead)noHead=h;
 if(!msgRows(rows)&&!noMsgRow)noMsgRow=h;if(!msgHead(rows)&&!noMsgHead)noMsgHead=h;
 // Spec rule 58: the messages not shown are counted right after their head (`MESSAGES +N`), with or without MEM.
 const shown=msgRows(rows),hidden=msgHead(rows)?head.messages.length-shown:0;
 if(hidden)assert(new RegExp('^│ MESSAGES \\+'+hidden+' ─+ › │$').test(rows.find(s=>s.startsWith('│ MESSAGES'))),h+'\n'+rows.join('\n'));
}
// (A later step that frees more rows than it needed gives them back to the earlier ones, so a state may come back one height lower.)
assert(oneRecord>noRecord&&noRecord>=noHead&&noHead>noMsgRow&&noMsgRow>noMsgHead&&noMsgHead>0,JSON.stringify({oneRecord,noRecord,noHead,noMsgRow,noMsgHead}));
// Spec rule 58 (11-14 night, B-가 with the label first): `MESSAGES` and `MEM` lead their rows (dim,
// the title role) and a thin rule in the same color runs on from each to the row's end (to `›` for
// MESSAGES); nothing stands before a head.
let lined=at();rows=plain(lined);
assert(rows.some(s=>/^│ MESSAGES \+2 ─+ › │$/.test(s))&&rows.some(s=>/^│ MEM ─+ │$/.test(s))&&!rows.some(s=>/── (MESSAGES|MEM)/.test(s)),rows.join('\n'));
const ruleOf=word=>lined.rows[rows.findIndex(s=>s.startsWith('│ '+word))].find(p=>/^─+$/.test(p.text));
const a=ruleOf('MESSAGES'),b=ruleOf('MEM');
assert(a&&b&&a.role==='messages'&&a.role===b.role&&a.dim===b.dim&&a.foreground===b.foreground,JSON.stringify([a,b]));
const cellsOf=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
for(const w of [32,40,48]){const r=plain(at({width:w}));assert(r.every(s=>cellsOf(s)===w),w+'\n'+r.join('\n'));}
"""
        self.node(script, "panel-view.cjs")

    # Draft A (chosen 11-13, "권장안으로 해. A으로"), 11-15 (popup design E): the LAB section of the experiment
    # board. The backend words it (tools/fleet/session_lab.py, Fleet's LAB-table cells); these build its shape:
    # key, count, clock, counts, goal, notes, at most three rows (name, cells {state, progress, delta}) and strip_attention.
    LAB = r"""
const T=(t,k)=>({text:t,...({dim:{dim:true},loc_repo:{foreground:252},lvl_r:{foreground:217},lvl_y:{foreground:186},g_spin:{foreground:230},lvl_g:{foreground:150},soft:{foreground:250}})[k]||{}});
const NAMES=['기본 분리','잔향 줄임','잡음 섞기','긴 구간 학습','작은 음성 보존'];
const labRow=(n,i,o={})=>({name:T(n,'soft'),cells:{state:T(o.state||'진행',o.stateKey||'g_spin'),progress:T(o.progress||(36-i)+'/60 ep','soft'),delta:T(o.delta||'+'+(0.4+i/10).toFixed(1)+' dB','lvl_g')}});
const lab=(o={})=>({key:'lab:grp_1',count:'5',clock:'20:40',revision:'rev',counts:[[T('진행 5','g_spin')]],goal:[T('잔향에서 기준보다 선명하게','soft')],notes:[],
 rows:NAMES.slice(0,3).map((n,i)=>labRow(n,i)),strip_attention:false,...o});
const failedLab=()=>lab({counts:[[T('실패 1','lvl_r')]],
 rows:[labRow('잡음 섞기',2,{state:'실패',stateKey:'lvl_r',progress:'34/60 ep',delta:'-2.2 dB'}),...[0,1].map(i=>labRow(NAMES[i],i))],strip_attention:true});
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const labStart=rows=>rows.findIndex(s=>/^│ (\d+ )?LAB( |$)/.test(s));
"""

    def test_lab_stands_under_the_routes_above_the_sessions_and_the_messages(self):
        # Draft A, 11-15: `5 LAB ─── 진행 5 ›` (the count leads its upper-case word, spec rule 45; the word a
        # dim title as MESSAGES and MEM; the most pressing counts at the right) and up to three condition
        # rows (CONDITION, STATE, PROGRESS, DIFF); under the routes and above the sessions and the messages
        # (QUESTIONS, ROUTE, LAB, SESSIONS, MESSAGES); a press on any of its rows (and only those) opens the LAB popup.
        script = self.FIXTURE + self.LAB + r"""
const sessions={count:3,counts:[[T('◑1')],[T('●2')]],rows:[1,2,3].map(i=>({glyph:[T('●')],tag:[T('['),T('7'+i),T(']')],title:[T('다른 세션 '+i)],age:[T(i+'m')]}))};
const q={count:1,label:'QUESTIONS',rows:[[P('◑ '),P('조건을 계속 비교할까요?')],[P('  '),P('1 계속 · 2 멈춤')]],one:[[P('◑ '),P('조건을 계속 비교할까요?')]],line:[P('조건을 계속 비교할까요?')],choices:2};
const at=(o={})=>view.render({width:o.width||40,height:o.height||60,side:true,fill:true,
 model:{...head,routes:[oneShot],route_count:1,lab:lab(o.lab||{}),sessions,...(o.model||{})},state:'idle',configuration:{},brief:()=>o.q||null});
for(const width of [40,32]){
 const v=at({width,q}),rows=plain(v),at0=labStart(rows),route=rows.findIndex(s=>/^│ 1 ROUTE /.test(s)),qa=rows.findIndex(s=>s.includes('1 QUESTIONS')),
  ses=rows.findIndex(s=>/^│ 3 SESSIONS /.test(s)),msg=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
 assert(qa>0&&qa<route&&route<at0&&at0<ses&&ses<msg,rows.join('\n'));
 // The head: count, word, then the counts at the right before `›`; the rows are CONDITION, STATE, PROGRESS, DIFF.
 assert(new RegExp('^│ 5 LAB ─+ 진행 5 › │$').test(rows[at0]),rows[at0]);
 // Short of cells (32) DIFF leaves, whole, and the name keeps its place.
 if(width===32)assert(/^│ 기본 분리 +진행 +36\/60 ep │$/.test(rows[at0+1])&&/^│ 잡음 섞기 +진행 +34\/60 ep │$/.test(rows[at0+3]),rows.join('\n'));
 else assert(/^│ 기본 분리 +진행 +36\/60 ep +\+0\.4 dB │$/.test(rows[at0+1])&&/^│ 잔향 줄임 +진행 +35\/60 ep +\+0\.5 dB │$/.test(rows[at0+2])&&/^│ 잡음 섞기 +진행 +34\/60 ep +\+0\.6 dB │$/.test(rows[at0+3]),rows.join('\n'));
 assert(/^│ 3 SESSIONS /.test(rows[at0+4]),rows.join('\n'));
 const word=v.rows[at0].find(p=>p.text==='LAB');assert(word.dim===true&&word.role==='title',JSON.stringify(word));
 // A press on the LAB rows (exactly those) opens the LAB popup; nothing opens in place.
 assert.deepEqual(v.hits.filter(h=>h.action==='popup:LAB').map(h=>h.row),[at0,at0+1,at0+2,at0+3],JSON.stringify(v.hits));
 assert(v.hits.filter(h=>h.action==='popup:LAB').every(h=>h.left===2&&h.right===width-3));
 assert(!v.hits.some(h=>/^lab:/.test(h.action)));
}
// No route: LAB right under the question; no ROUTE head.
let rows=plain(at({model:{routes:[],route_count:0}}));
assert(labStart(rows)>0&&!rows.join('').includes('ROUTE'),rows.join('\n'));
// A question: LAB keeps its place below the routes, the question stays above them.
rows=plain(at({q,height:44}));
const qa=rows.findIndex(s=>s.includes('조건을 계속')),la=labStart(rows),ra=rows.findIndex(s=>/^│ 1 ROUTE /.test(s));
assert(la>0&&qa>0&&qa<ra&&ra<la&&rows.some(s=>s.includes('1 QUESTIONS'))&&rows.some(s=>s.includes('1 계속 · 2 멈춤')),rows.join('\n'));
// A condition whose PROGRESS is unknown keeps Fleet's `?`.
rows=plain(at({lab:{rows:[labRow('큰 학습률',0,{progress:'?'})]}}));
assert(rows.some(s=>/^│ 큰 학습률 +진행 +\? +\+0\.4 dB │$/.test(s)),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_lab_gives_way_in_its_order_and_the_question_keeps_its_rows(self):
        # Draft A's order merged with the panel's (spec rule 34 (b), 46 (d), 11-15): short of rows every
        # section steps down to two rows and then to one, the lowest first (MEM, MESSAGES, SESSIONS,
        # LAB, ROUTE); later MEM goes, then each section's rows and head in the same order. The
        # question keeps its head and first row, the fold and settings row stay.
        script = self.FIXTURE + self.LAB + r"""
const items=[{kind:'decision',age:'3m',text:'평가 조건을 같게 유지'},{kind:'lesson',age:'2h',text:'one fix per review finding'}];
const sessions={count:3,counts:[[T('◑1')],[T('●2')]],rows:[1,2,3].map(i=>({glyph:[T('●')],tag:[T('['),T('7'+i),T(']')],title:[T('다른 세션 '+i)],age:[T(i+'m')]}))};
const q={count:1,label:'QUESTIONS',rows:[[P('◑ '),P('조건을 계속 비교할까요?')],[P('  '),P('1 계속 · 2 멈춤')]],one:[[P('◑ '),P('조건을 계속 비교할까요?')]],line:[P('조건을 계속 비교할까요?')],choices:2};
const labDraw=(height,o={})=>plain(view.render({width:40,height,side:true,fill:true,model:{...head,routes:[oneShot,staged,oneShot],route_count:3,memory_items:items,sessions,lab:lab(),...(o.model||{})},
 state:'idle',configuration:{},brief:()=>o.q||null}));
const n=(rows,re)=>rows.filter(s=>re.test(s)).length;
const state=rows=>{const a=labStart(rows);
 return {lab:a<0?-1:n(rows.slice(a+1),/^│ (기본|잔향|잡음)/),ses:rows.some(s=>/SESSIONS/.test(s))?n(rows,/^│ ● \[/):-1,msg:rows.some(s=>/^│ MESSAGES/.test(s))?n(rows,/^│ [←→] \[/):-1,
  route:rows.some(s=>/ ROUTE /.test(s))?n(rows,/^│ \S (셸|세션)/):-1,mem:rows.some(s=>/^│ MEM ─/.test(s))?n(rows,/^│ ◆/):-1};};
// The first height each section stands at a given row count or fewer.
const first={};const seen=[];
for(let h=90;h>=10;h--){
 const rows=labDraw(h),s=state(rows);seen.push(h+':'+JSON.stringify(s));
 for(const [k,v] of Object.entries(s))for(const at of [2,1,0,-1])if(v<=at&&first[k+at]===undefined&&!(k==='mem'&&at===2))first[k+at]=h;
 assert(rows.at(-2).endsWith('»   ⚙ │'),h+'\n'+rows.join('\n'));
 assert(rows.length<=h,h);
}
// (A later step that frees more rows than it needed gives them back to the earlier ones, so a
// state may come back one height lower; the order is that of the first height each step is taken.)
const lowest=['msg','ses','lab','route'];
for(const at of [2,1])for(let i=1;i<lowest.length;i++)assert(first[lowest[i-1]+at]>=first[lowest[i]+at],lowest[i-1]+at+' goes before '+lowest[i]+at+'\n'+JSON.stringify(first));
assert(first.mem1>=first.msg1,JSON.stringify(first));
// Down to two rows every section first, then to one, then (MEM first) to none, then the head goes.
for(const k of ['msg','ses','lab','route'])assert(first[k+2]>first[k+1]&&first[k+1]>first[k+0]&&first[k+0]>first[k+'-1']||k==='route'&&first[k+1]>first[k+0],k+JSON.stringify(first));
assert(first.mem0>first.msg0&&first.msg0>first.ses0&&first.ses0>first.lab0&&first.lab0>first.route0,JSON.stringify(first));
// LAB goes whole before ROUTE does: the routes outlast it.
assert(first['lab-1']>first['route0'],JSON.stringify(first));
// With a question: its head and first row stay at every height, the three rows while room allows.
const asked=rows=>rows.filter(s=>/1 QUESTIONS|조건을 계속|1 계속 · 2 멈춤/.test(s));
for(let h=40;h>=10;h--){const rows=labDraw(h,{q});
 assert(asked(rows).length>=2,h+'\n'+rows.join('\n'));
 if(h>=24)assert.equal(asked(rows).length,3,h+'\n'+rows.join('\n'));
 assert(rows.at(-2).endsWith('»   ⚙ │'),h);}
"""
        self.node(script, "panel-view.cjs")

    def test_lab_is_absent_without_a_board_and_an_unconfirmed_board_says_so(self):
        # No experiments and no problem: the backend sends no LAB and no row stands. A broken or
        # missing catalog with runs found, or runs without a name, never reads as "no experiments".
        # 11-15: without conditions the head's counts say so and the goal (or why there is none) and
        # the notes stand on one row each.
        script = self.FIXTURE + self.LAB + r"""
const labDraw=(model,o={})=>view.render({width:40,height:40,side:true,fill:true,model:{...head,routes:[oneShot],route_count:1,...model},state:'idle',configuration:{},
 questions:()=>[]});
for(const none of [null,undefined,'x',[]]){const rows=plain(labDraw({lab:none}));assert(!/\bLAB\b/.test(rows.join('\n'))&&labStart(rows)<0,rows.join('\n'));}
const unread=lab({count:'',counts:[[T('이름 없음 2','lvl_y')]],goal:[T('실험 정본 아직 없음; run은 id로만 보임','lvl_y')],rows:[],key:'lab:',strip_attention:true});
let rows=plain(labDraw({lab:unread})),a=labStart(rows);
// Without a group the head says so at the right and the second row says why, on one row (cut with `…`).
assert(/^│ LAB ─+ 이름 없음 2 › │$/.test(rows[a])&&rows[a+1].startsWith('│ 실험 정본 아직 없음;')&&rows[a+1].includes('…'),rows.join('\n'));
assert(!rows.some(s=>/^│ 조건 /.test(s))&&!rows.join('').includes('run-x'),rows.join('\n'));
// Another group's trouble is told, not hidden behind the selected one (a row the conditions leave).
rows=plain(labDraw({lab:lab({rows:[labRow('기본 분리',0)],notes:[[T('다른 묶음 잡음 실험도 확인 필요: 실패 1','lvl_y')]]})}));a=labStart(rows);
assert(rows[a+1].startsWith('│ 기본 분리')&&rows[a+2].startsWith('│ 다른 묶음 잡음 실험도'),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_an_experiment_failure_never_changes_the_frame_or_the_session_glyph(self):
        # The frame, the head's glyph and number, and the folded strip's rule and session row are the
        # session's own state (spec rule 25, 49 (c)); a failed or unconfirmed experiment stays inside
        # LAB (folded: its `5 LAB 실패 1` group at the end of the routes row).
        script = self.FIXTURE + self.LAB + r"""
const frame=v=>JSON.stringify(v.folded?[v.color,v.rows[0],v.rows[1]]:[v.color,v.rows[0],v.rows.map(r=>[r[0],r.at(-1)]),v.rows.at(-1)]);
for(const state of ['idle','working','blocked','unknown'])for(const folded of [false,true]){
 const labDraw=lab=>view.render({width:folded?120:40,height:folded?4:30,side:!folded,fill:!folded,folded,frame:3,model:{...head,tag:'[e9]',routes:[oneShot],route_count:1,lab},
  state,harness:'claude',configuration:{},questions:()=>[]});
 const none=labDraw(null),failed=labDraw(failedLab());
 assert.equal(frame(failed),frame(none),state+' '+folded);
 if(!folded)assert(plain(failed).some(s=>s.includes('실패 1')));
 else assert(plain(failed).some(s=>/ │ 5 LAB 실패 1 +$/.test(s))&&!plain(none).join('').includes('LAB')&&plain(failed)[1].includes('CTX ━━━━'),plain(failed).join('\n'));
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
        # Spec E9 rule 47 (11-14: the outer frame instead of the head rule; 11-15 panel polish 4:
        # scanners instead of one arc), rules 51, 52 and 25 as it retunes them. Running (and while an
        # answer is typed in), three short lights go round the outer frame clockwise together, evenly
        # spaced: each eleven cells whole, its middle the brightest (the running frame's vanilla) and
        # fading alike to both sides (230 → 187 → 144 → 101 → the neutral frame gray), once round
        # every 60 frames (6 s) whatever the size; a frame too short for three keeps fewer (the dark
        # stretch between two never shorter than one light); the cells of the head's words are passed
        # behind. Waiting, the whole frame breathes red every 48 frames (4.8 s), three shades in 256
        # colors and six in 24-bit, one at a time. Idle and an unknown state, rule motion off, mono,
        # NO_COLOR, eight colors and the settings screen: the still frame in the state's color. The
        # head rule is the plain thin rule in every state. Between frames only the frame's line cells
        # (and the spinner glyph) change; the host gets those cells, their paint at any frame and the
        # next frame they change at.
        script = self.FIXTURE + r"""
const styles={dim:{dim:true},g_spin:{foreground:230},g_blocked:{foreground:217},g_work_off:{foreground:150,dim:true},
 tag_steward:{foreground:219,bold:true},tag_steward_dim:{foreground:219,dim:true}};
const run=(o={})=>view.render({width:o.width||40,height:o.height||40,side:o.side!==false,fill:o.side!==false,harness:o.harness||'claude',
 model:{...head,tag:'[e9]',routes:[],route_count:0,messages:[],styles,...(o.model||{})},state:o.state,frame:o.frame||0,since:o.since,
 configuration:o.cfg||{},background:o.background||'',questions:()=>[],...(o.extra||{})});
const text=r=>r.map(p=>p.text).join('');
// The four values a person tunes stand at the top of panel-view.cjs.
assert.deepEqual(view.BORDER,{SCANNERS:3,HALF:5,LAP:60,PEAK:0,SHADES:4,BREATHE:48,BREATHE_STEPS:6,BREATHE_STEPS_256:3});
assert(/^'use strict';\n(?:\/\/.*\n)+const BORDER_SCANNERS=3,BORDER_HALF=5,BORDER_LAP=60,BORDER_PEAK=0;\n/.test(require('node:fs').readFileSync(process.argv[1],'utf8')));
assert.deepEqual(view.FACES.dark.orbit,[230,187,144,101]);assert.deepEqual(view.FACES.light.orbit,[22,65,108,151]);
assert.deepEqual(view.FACES.dark.breath,[217,210,174]);assert.deepEqual(view.FACES.light.breath,[88,124,131]);
// Every cell of a painted panel, by row and column, with the part it stands in.
const grid=v=>v.rows.map(r=>{const out=[];for(const p of r)for(const ch of p.text){out.push({ch,p});if(cells(ch)>1)out.push(null);}return out;});
// The frame's cells clockwise from the top-left corner, as [row, column].
const around=(W,H)=>{const out=[];for(let x=0;x<W;x++)out.push([0,x]);for(let y=1;y<H-1;y++)out.push([y,W-1]);
 for(let x=W-1;x>=0;x--)out.push([H-1,x]);for(let y=H-2;y>0;y--)out.push([y,0]);return out;};
// Each frame cell's look: 'text' (the head's words), 'base' (the neutral frame), or its shade 0..3.
const ramp=view.FACES.dark.orbit;
const looks=v=>{const g=grid(v),W=g[0].length,H=g.length;return around(W,H).map(([y,x])=>{const c=g[y][x];
 if(!'┌┐└┘─│'.includes(c.ch))return 'text';
 if(c.p.role==='frame')return 'base';
 assert(c.p.role==='orbit'&&ramp.includes(c.p.foreground)&&c.p.dim!==true,JSON.stringify(c.p));return ramp.indexOf(c.p.foreground);});};
// One light, end to end: the middle the brightest, fading alike to both sides.
const LIGHT=[3,2,2,1,0,0,0,1,2,2,3];
// The lights of a frame's looks: each maximal run of lit cells round the ring, its start, and
// whether the head's words cut it (a short run beside 'text').
const lightsOf=l=>{const n=l.length,lit=i=>typeof l[i%n]==='number';let from=l.findIndex((x,i)=>!lit(i));if(from<0)return [];
 const out=[];for(let k=1;k<=n;k++){const i=(from+k)%n;if(lit(i)&&!lit(i-1+n)){let j=i;const run=[];while(lit(j)){run.push(l[j%n]);j++;}
  out.push({at:i,run,cut:run.length<11&&(l[(i-1+n)%n]==='text'||l[j%n]==='text')});}}return out;};
// (a) Running: three lights, each the eleven-cell ramp, evenly spaced, moving on clockwise every frame
// by the same step (within a cell), round in exactly 60 frames for every size.
for(const [o,W,H] of [[{},40,40],[{width:60},60,40],[{side:false,width:100,height:14},100,0]]){
 const rowsOf=run({...o,state:'working'}).rows.length,P=2*W+2*((H||rowsOf)-2),mids=[];
 for(let f=0;f<=120;f++){
  const v=run({...o,state:'working',frame:f}),l=looks(v);assert.equal(l.length,P);
  // The glyphs stay the frame's own on every frame.
  const g=grid(v);assert(g[0][0].ch==='┌'&&g[0].at(-1).ch==='┐'&&g.at(-1)[0].ch==='└'&&g.at(-1).at(-1).ch==='┘',String(f));
  const lights=lightsOf(l),whole=lights.filter(x=>!x.cut);
  assert(lights.length<=3&&whole.length>=2,f+' '+l.join(''));
  for(const x of whole)assert.deepEqual(x.run,LIGHT,f+' '+l.join(''));
  // No lit cell outside the lights; a light the words cut is a piece of the ramp.
  for(const x of lights.filter(x=>x.cut))assert(x.run.length<11&&LIGHT.join(',').includes(x.run.join(',')),f+' '+l.join(''));
  mids.push(whole.length===3?whole.map(x=>(x.at+5)%P).sort((a,b)=>a-b):null);
 }
 // Evenly spaced: a third of the frame apart (within a cell).
 const full=mids.filter(Boolean);assert(full.length>30,String(W));
 for(const m of full)for(let k=0;k<3;k++){const gap=(m[(k+1)%3]-m[k]+P)%P;assert(Math.abs(gap-P/3)<=1,JSON.stringify(m)+' '+P);}
 // Moving on: the first middle steps P/60 (within a cell) a frame, never back; a lap is 60 frames.
 const steps=[];for(let f=0;f<120;f++)if(mids[f]&&mids[f+1]){const d=Math.min(...mids[f+1].map(b=>(b-mids[f][0]+P)%P));steps.push(d);}
 assert(steps.length>30&&steps.every(s=>s>=Math.floor(P/60)&&s<=Math.ceil(P/60)),JSON.stringify(steps));
 for(let f=0;f<60;f++)assert.deepEqual(run({...o,state:'working',frame:f}).rows,run({...o,state:'working',frame:f+60}).rows,String(f));
 // (b) Three passages a lap: each cell lights three times and goes back to the neutral frame between
 // (in 256 colors too: lights turning, never a blink); every shade shows along the way.
 const seq=Array.from({length:60},(_,f)=>looks(run({...o,state:'working',frame:f})));
 for(let i=0;i<P;i++){if(seq[0][i]==='text')continue;
  const s=seq.map(l=>l[i]),rises=s.filter((x,k)=>x!=='base'&&s[(k+59)%60]==='base').length;
  assert.equal(rises,3,i+' '+s.join(''));}
 assert.deepEqual([...new Set(seq.flat().filter(x=>typeof x==='number'))].sort(),[0,1,2,3]);
}
// A frame too short for three keeps fewer, the dark stretch between two at least one light long (11 cells).
for(const [W,H,n] of [[30,20,3],[20,4,2],[12,4,1],[8,4,1]]){
 const v=run({side:false,width:W,height:H,state:'working',frame:0,since:0}),l=looks(v),P=l.length,on=l.filter(x=>typeof x==='number').length;
 const lit=lightsOf(l.map(x=>x==='text'?'base':x));assert.equal(lit.length,n,W+'x'+H+' '+l.join(''));
 assert(on<=11*n,W+'x'+H);
 if(n>1)for(let k=0;k<n;k++){const a=lit[k],b=lit[(k+1)%n];assert((b.at-(a.at+a.run.length)+P)%P>=11,W+'x'+H+' '+l.join(''));}
}
// (c) The lights start with the state: at `since` the first one stands whole right after the head's words.
for(const since of [0,7,59,1000]){const v=run({state:'working',frame:since,since}),l=looks(v);
 const words=text(v.rows[0]).indexOf(' ─')+1;assert.equal(l[words-1],'text');assert.deepEqual(l.slice(words,words+11),LIGHT,since+' '+l.join(''));
 assert.deepEqual(looks(run({state:'working',frame:since+13,since})),looks(run({state:'working',frame:13})),'the same lights, '+since+' frames on');}
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
// 11-15: the editor reads in the popup over the window, at its width: a narrow window and 80 columns
// keep the draft and the keys in sight.
for(const [cols,rows] of [[40,16],[80,12],[60,10]]){
 const all=c.detail({budget:v.popupRoom(rows)+1,columns:v.popupWidth(cols)-4});
 const r=v.popup({cols,rows,title:all[0],body:all.slice(1),sections:['QUESTIONS']});
 const text=r.rows.map(row=>row.map(p=>p.text).join('')).join('\n');
 assert(text.includes('Draft answer'),text);assert(text.includes('Tab next'),text);
 assert(r.rows.every(row=>row.reduce((n,p)=>n+[...p.text].length,0)<=cols),text);
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
const q=()=>({count:1,label:'QUESTIONS',rows:[[{text:'◑ '},{text:'Which?'}],[{text:'  '},{text:'1 '},{text:'A',bold:true}]],one:[[{text:'◑ '},{text:'Which?'}]],line:[{text:'Which?'}],choices:1});
const paint=(cfg,background,extra={})=>v.render({width:40,height:24,side:true,model:{...model,...extra},configuration:cfg,background,brief:q});
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
  // The question's `◑` and its count (11-15: the section's label row) are rose; its words are body text.
  assert(parts.some(p=>p.text==='◑'&&p.foreground===f.accent)&&parts.some(p=>p.text==='1'&&p.role==='accent'&&p.foreground===f.accent),label);
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
    """R27, 11-15: sent and received requests stand as the first rows of MESSAGES (the popup reads
    them whole) and leave the question, the routes and the draft alone."""

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

    def test_the_route_head_counts_routes_only_and_requests_stand_as_messages_rows_without_an_hourglass(self):
        # 11-15: `N ROUTE │ M REQUEST` is gone: the ROUTE head carries the route count alone and the
        # requests are the first rows of MESSAGES (then the recent messages, three rows at most).
        self.node(r"""
for(const [width,height] of [[32,30],[37,30],[38,30],[80,24]]){
 const lines=text(render(width,height));
 assert(lines.some(s=>new RegExp(width<80?'^│ 1 ROUTE ─+ › │$':'^│ route 1 ─+ › │$').test(s)),lines.join('\n'));
 assert(!lines.join('').includes('REQUEST')&&!lines.join('').includes('request '),lines.join('\n'));
 // Three requests fill the three rows; the recent message is counted in the head (`+1`).
 assert(lines.some(s=>new RegExp(width<80?'^│ MESSAGES \\+1 ─+ › │$':'^│ messages \\+1 ─+ › │$').test(s)),lines.join('\n'));
 for(const mark of ['◂','→','✓'])assert(lines.some(s=>s.includes(mark)),mark+width);
 assert(!lines.join('').includes('⏳')&&!lines.join('').includes('⌛'));
 assert(lines.length<=height);
}
// Fewer requests leave rows to the messages: two requests and the message make the three rows.
const two=text(render(37,30,{},{...base,requests:requests.slice(0,2)}));
assert(two.some(s=>/^│ MESSAGES ─+ › │$/.test(s))&&two.some(s=>s.includes('MSG_KEPT')),two.join('\n'));
const none=text(render(37,30,{},{...base,requests:[]}));
assert(none.some(s=>s.includes('1 ROUTE'))&&!none.join('').includes('REQUEST')&&none.some(s=>s.includes('MSG_KEPT')));
// Spec rules 57, 58: the section names are dark gray name tags (as MEM), never the route count's state color;
// a press on the requests' rows opens the MESSAGES popup.
const styled={...base,routes:[{...routes[0],state:'idle',kind:'idle'}],styles:{dim:{dim:true},g_idle:{fg:186}}};
for(const width of [32,38]){
 const v=render(width,30,{},styled),parts=v.rows.flat();
 const route=parts.find(p=>p.text==='ROUTE'),messages=parts.find(p=>p.text==='MESSAGES');
 assert(route&&route.dim===true&&route.role==='title'&&messages&&messages.dim===true,JSON.stringify([route,messages]));
 const count=parts.find(p=>p.text==='1');assert(count&&count.dim!==true&&count.fg===undefined,JSON.stringify(count));
 const rows=text(v),at=rows.findIndex(s=>s.includes('◂'));
 assert(at>0&&v.hits.some(h=>h.row===at&&h.action==='popup:MESSAGES'),JSON.stringify(v.hits));
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

    def test_a_request_alone_still_draws_the_messages_section_and_a_long_url_title_is_cut(self):
        self.node(r"""
const lone=text(render(32,24,{},{...base,routes:[],route_count:0,messages:[],requests:[
 q('rq-dddddd','→','[52]','https://example.com/'+'a'.repeat(80),'전달됨','request-out')]}));
const head=lone.findIndex(s=>/^│ MESSAGES ─+ › │$/.test(s));
assert(head>0&&!lone.join('').includes('ROUTE')&&!lone.join('').includes('REQUEST'),lone.join('\n'));
const row=lone[head+1];
assert(row.includes('→')&&row.includes('[52]')&&row.includes('…')&&row.includes('전달됨'),row);
assert(lone.every(s=>[...s].length<=32));
""")


if __name__ == "__main__":
    unittest.main()
