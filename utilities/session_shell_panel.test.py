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
  terminal:{modes:{mouseTrackingMode:'none'}},heldStop(){},queueLayout(){},queueDisplay(){},rebuildDisplay(){},observeInput(){},openFleet(){},
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
 // (11-15: the route box stands again, its line in the failure color only for a current failure.)
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
const assert=require('node:assert/strict'),view=require(process.argv[1]),models=JSON.parse(require('node:fs').readFileSync(0,'utf8'));   // (stdin: the models weigh near an argument's 128 KiB limit)
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
  // The notice ends at the next group: a blank row, a section rule, a box's top line (the QUESTIONS box, 11-16) or the answer wait.
  const end=rows.findIndex((l,i)=>i>first&&(/^│ +│$/.test(l)||l.includes('── ')||/^│ ╭─/.test(l)||/◑(답| to answer)/.test(l)));
  assert(first>=0&&end>first&&text.includes('이어서해'),id+' '+s+'\n'+text);
  const lines=rows.slice(first,end);
  // Bottom: one row; side: at most three. A cut line says so with an ellipsis.
  assert(lines.length===(width===80?1:3)&&lines.at(-1).includes('…'),id+' '+s+' '+lines.length);
  assert.equal(rows.length,width===80?none.split('\n').length+1:rows.length,id);
 }
}
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "utilities/session-shell/panel-view.cjs")],
                                input=json.dumps(models, ensure_ascii=False), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_top_groups_and_quota_stay_while_a_question_takes_the_rows(self):
        # Spec 2026-11-11 rule 6: with too few rows the question and the three top
        # groups stay; the messages' rows, then the route box's rows give way (11-15: the box again; 11-20: the messages go first).
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
 // Spec rule 23: every route has a row or is counted in the box's lower border; no `more` row.
 const listed=idle.filter(s=>/ROUTE_\d/.test(s)).length,below=Number((idle.join('\n').match(/ ↓(\d+) ─╯/)||[0,0])[1]);
 assert.equal(listed+below,6,idle.join('\n'));assert(!idle.join('').includes('more'),idle.join('\n'));assert(/^└─+┘$/.test(idle.at(-1)));
 assert(idle.at(-2).includes('7d 60%'),'quota is the last row');
}
for(const [width,side,low] of [[40,true,14],[80,false,10]])for(let height=low;height<=24;height+=2){
 const rows=paint(width,height,side,question),text=rows.join('\n');
 assert(rows.length<=height,width+'x'+height);
 for(const want of ['1 QUESTIONS','◑ Q head'])assert(text.includes(want),'question row lost at '+width+'x'+height+'\n'+text);
 for(const want of ['project','fixture high','42%','SESSION_TITLE'])assert(text.includes(want),width+'x'+height+' lost '+want+'\n'+text);
 // Spec rules 23, 31 (v4.36.4's order, the route box again): the messages fold to one row (the newest
 // stays) before the route box scrolls; 11-20: the messages section goes before the route box does.
 if(text.includes('MESSAGE_TWO'))assert(text.includes('ROUTE_0'),text);
 if(text.includes('MESSAGE_ONE'))assert(!/[↑↓]\d/.test(text),text);
}
const r=view.render({width:40,height:30,side:true,model,sid:'exact',configuration:{},questions:()=>[]});
assert(!r.hits.some(h=>h.action==='details'));
assert(r.hits.some(h=>h.action==='popup:MESSAGES'));assert(!r.hits.some(h=>h.action==='fleet'));
'''
        self.node(script, "panel-view.cjs")

    def test_shortening_keeps_the_observed_count_message_and_mcp(self):
        # Only the rows shown shrink: the whole route count, a message's row and
        # OpenCode's MCP count stay readable (review RV-PR1517 §1; 11-15 the route box again).
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
const hidden=(plain,arrow)=>Number((plain.join('\n').match(new RegExp(' '+arrow+'(\\d+) (?:─|›─)[╮╯]'))||[0,0])[1]);
for(let height=10;height<=22;height++){
 for(const model of [six,twelve])for(const scroll of [0,3,99]){
  r=paint(model,40,height,{routeScroll:scroll});const shown=new Set(r.hits.filter(h=>h.action.startsWith('popup:ROUTE:')).map(h=>h.action)).size;
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
        quiet = mock.patch.object(human_wait_state, "cached_statusline", return_value=(None, ""))
        with tempfile.TemporaryDirectory() as home, quiet, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
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
        with quiet, mock.patch.object(codex, "exact_rollout_for_session_id", return_value=__file__), \
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
const route=i=>({id:'r'+i,title:'ROUTE_'+i,state:'working',kind:'working',elapsed:'42m',paint:{glyph:p('⠏'),harness:p('codex'),ctx:p('72%'),now:'DOING_'+i}});
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
// border; it stands at the right end of the last row inside, the fold button `▾` at its right end.
for(const [state,want] of [['idle','┌ ● [e9] claude ─'],['working','┌ ⠙ [e9] claude ─'],['blocked','┌ ◑ [e9] claude ─'],['unknown','┌ [e9] claude ─'],[undefined,'┌ [e9] claude ─']]){
 const rows=text(paint({state,frame:1,model:{tag:'[e9]'}}));assert(rows[0].startsWith(want),state+' '+rows[0]);
 assert(!/running|idle|waiting/.test(rows[0]),rows[0]);
 assert(rows[0].endsWith('─┐')&&!rows[0].includes('⚙')&&!rows.join('').includes('Settings')&&!rows.join('').includes('HEARTING'),rows[0]);
 assert(rows.at(-2).endsWith('⚙   ▾ │'),rows.at(-2));
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
// Narrow: (11-18: two cells stand after the flag) the steward mark goes first, then the harness, then the number; the glyph stays.
for(const [width,want] of [[21,'┌ ● [e9] claude ⚑  ─'],[20,'┌ ● [e9] claude ─'],[17,'┌ ● [e9] ─']]){
 const row=text(view.render({width,height:12,side:true,sid:'s',harness:'claude',configuration:{},model:{tag:'[e9]',steward:true},state:'idle',questions:()=>[]}))[0];
 assert(row.startsWith(want),width+' '+row);
}
const settings=paint({settings:{index:0,value:view.defaults}});
assert(!paint({state:'idle',model:{routes:[],route_count:0}}).animated,'an idle panel with nothing to blink keeps no beat');assert(text(settings)[0].startsWith('┌ HEARTING ─'),text(settings)[0]);
const hits=paint({state:'idle'}).hits,gear=hits.find(h=>h.action==='settings'),fold=hits.find(h=>h.action==='fold');
assert(gear.row===28&&gear.left<=33&&gear.right>=33&&fold.row===28&&fold.left<=37&&fold.right>=37&&gear.right<fold.left-1,JSON.stringify([gear,fold]));
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
 // (11-15: the arc is the `border` value's; under the default `rule` a running frame stands lit, one step below.)
 const moving=paint({state,frame:0,since:0,cfg:{rule_motion:'border'}}).rows.flatMap(r=>[r[0],r.at(-1)]),lines=paint({state,frame:0,since:0,cfg:{rule_motion:'border'}}).rows.flat().filter(x=>['frame','frame-state','orbit'].includes(x.role)&&/^[─┌┐└┘│]+$/.test(x.text));
 const lit=paint({state,frame:0,since:0}).rows.flat().filter(x=>!x.rule&&['frame','frame-state','orbit'].includes(x.role)&&/^[─┌┐└┘│]+$/.test(x.text));
 const R=view.FACES.dark.ramp.claude.orbit;   // (11-18: this window is Claude's: a running session's moving cells are its harness's ramp)
 if(want===230)assert(lit.every(x=>x.role==='orbit'&&x.foreground===R[1]&&x.dim!==true),state+' lit');
 if(want===217)assert(lines.every(x=>x.foreground===217&&x.dim!==true),state+' '+JSON.stringify(moving[0]));
 else if(want)assert(lines.every(x=>x.role==='frame'&&x.dim===true||x.role==='orbit'&&R.includes(x.foreground))&&lines.some(x=>x.foreground===(want===230?R[0]:want)),state);
 else assert.deepEqual(moving,frame,state);
}
// A failed route turns only its own box line to the failure color; the frame stays the session's.
const failed=paint({state:'idle',model:{routes:[route(0),{...route(1),state:'dead',kind:'dead'}]}});
const box=failed.rows.flat().filter(x=>/^[╭╮╰╯]$/.test(x.text));
assert(box.length===4&&box.every(x=>x.foreground===217&&x.bold===false),JSON.stringify(box));
assert(failed.rows[0][0].foreground==null&&failed.rows[0][0].dim===true);
// Repo row, then Git on its own row; the route box high; MESSAGES (no box) keeps to the bottom, right
// above the quota row, the rows left over standing between the box and it (11-15).
// (35 rows: every structural row of spec rule 35 stands with three routes in their full form, each now four rows: who, CTX row, NOW and title.)
let rows=text(paint({state:'idle',height:35}));
assert(rows[1].includes('project')&&!rows[1].includes('[e9]')&&!rows[1].includes('topic')&&rows[2].includes('(topic 🚧 3)'),rows.join('\n'));
// Spec rule 29 (b), 30 (a), 35 (b): the metadata block is two groups parted by one empty row
// ([repository, Git] / [model, context]), then a thin rule, then the session's content with
// its labels; an empty row parts the head from the route box. (11-20: the SESSION group's head `SESSION ────` stands where the empty row between the
// two groups stood.)
// (11-22: the line between CTX and TITLE is gone, and GIT, MODEL, CTX, TITLE, NOW stand one cell in.)
assert(/^│ SESSION ─+ │$/.test(rows[3])&&rows[4].includes('fixture high')&&rows[5].startsWith('│  CTX    42%')&&!rows.some(s=>/^│ ─+ │$/.test(s)&&rows.indexOf(s)<rows.length-3),rows.join('\n'));
assert(rows[6].startsWith('│  TITLE  SESSION_TITLE')&&rows[7].startsWith('│  NOW    NOW_LINE')&&/^│ +│$/.test(rows[8])&&rows[9].includes('╭─ 3 ROUTE'),rows.join('\n'));
const r=rows.findIndex(s=>s.includes('╭─ 3 ROUTE ')),m=rows.findIndex(s=>s.startsWith('│ MESSAGES')),rb=rows.findIndex(s=>s.startsWith('│ ╰'));
assert(r>0&&m>rb&&rb>r&&rows.slice(r,rb).filter(s=>s.includes('ROUTE_')).length===3,rows.join('\n'));
// Spec rules 24, 38 (11-18, route row 5; the CTX row under the who row, then the NOW row): beside the native area a route
// reads its title (with the route's mark; this fixture names no kind), then who two cells in (the glyph with its harness: no
// context, no time), then its CTX row (`CTX 72%`, this fixture has no gauge) four cells in, then its NOW four cells in.
const t0=rows.findIndex(s=>s.includes('ROUTE_0'));
assert(/^│ │ \S ROUTE_0 +│ │$/.test(rows[t0])&&/^│ │   \S codex +│ │$/.test(rows[t0+1])&&/^│ │ {5}CTX 72% +│ │$/.test(rows[t0+2])&&/^│ │ {5}DOING_0 +│ │$/.test(rows[t0+3]),rows.join('\n'));
// Each message takes two rows (a head `← [e7]`, the text two cells in), the head `MESSAGES` carries no count of rows shown.
assert(rows.at(-2).includes('7d 60%')&&/^│ MESSAGES +› │$/.test(rows[m]),rows.join('\n'));
assert(rows[m+1].startsWith('│  ← [e7]')&&rows[m+2].startsWith('│   MESSAGE_1')&&rows[m+3].startsWith('│  ← [e7]')&&rows[m+4].startsWith('│   MESSAGE_2')&&rows[m+5].startsWith('│  ← [e7]')&&rows[m+6].startsWith('│   MESSAGE_3'),rows.join('\n'));
assert(m+7===rows.length-3&&/^│ ─+ │$/.test(rows[m+7])&&rows.slice(rb+1,m).every(s=>/^│ +│$/.test(s)),'MESSAGES keeps to the bottom, the spare rows stand above it\n'+rows.join('\n'));
for(const s of rows)assert.equal(cells(s),40,s);
// The questions section sits first, above the route box (11-15; 11-16 a thin rounded box like the route's): its top
// line carries the count in the question's rose and the name tag dim, `›` at its end, then the question's summary
// in the rows inside; messages are dim, the route box plain. (No question answerable here: the box's line is dim.)
const asked=paint({state:'working',q:question,model:{answerable:true},height:39});rows=text(asked);
const qTop=rows.findIndex(s=>/^│ ╭─ 1 QUESTIONS ─+ ›─╮ │$/.test(s)),ra=rows.findIndex(s=>s.includes('╭─ 3 ROUTE ')),mq=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(qTop>0&&rows[qTop+1].startsWith('│ │ ◑ Q_HEAD')&&rows[qTop+2].startsWith('│ │   1 A · 2 B')&&/^│ ╰─+╯ │$/.test(rows[qTop+3])&&ra===qTop+4&&mq>ra&&rows.at(-2).includes('7d 60%')
 &&rows.some(s=>/^│ │ \S ROUTE_0 {24}│ │$/.test(s)),rows.join('\n'));
const border=(i,ch)=>asked.rows[i].find(x=>x.text.startsWith(ch));
assert.equal(asked.rows[qTop].find(x=>x.text==='1').foreground,174);assert.equal(asked.rows[qTop].find(x=>x.text==='QUESTIONS').dim,true);
assert.equal(asked.rows[qTop+1].find(x=>x.text==='◑').foreground,174);
assert.equal(asked.rows[mq].find(x=>x.text.includes('MESSAGES')).dim,true);assert.notEqual(border(ra,'╭').dim,true);

// A press on any of its rows opens the question popup.
assert.deepEqual(asked.hits.filter(h=>h.action==='question').map(h=>h.row),[qTop,qTop+1,qTop+2,qTop+3]);
assert(asked.rows[qTop].filter(x=>/^[╭─╮ ]+$/.test(x.text)&&x.text.trim()).every(x=>x.dim===true&&x.role==='messages'),'a question not answered here leaves its box line dim');
for(const s of rows)assert.equal(cells(s),40,s);
// Short of rows (spec rule 23): two rows fold to one and messages fold to one row
// before the route box scrolls in place; 11-20: the messages go first (before the route box does), the newest one last of them.
const short=(h,q=question)=>text(view.render({width:40,height:h,side:true,sid:'s',configuration:{},model:{...model,answerable:true},state:'idle',brief:()=>q}));
for(const q of [null,question])for(let h=12;h<=30;h++){
 const rows=short(h,q),all=rows.join('\n'),two=rows.some(s=>/^│ │   \S codex/.test(s));   // (a route in more than one row: its who under its title)
 assert(rows.length<=h,h+'\n'+all);
 // Fewer messages show from the oldest (their head counts the rest, spec rule 58), before the
 // route box scrolls; the newest message goes only with the section.
 const hidden=['MESSAGE_1','MESSAGE_2'].filter(m=>!all.includes(m)).length;
 if(all.includes('MESSAGE_3')&&hidden)assert(new RegExp('│ MESSAGES \\+'+hidden+' ').test(all),h+'\n'+all);
 if(all.includes('MESSAGE_1')&&all.includes('ROUTE_0'))assert(rows.filter(s=>s.includes('ROUTE_')).length===3&&!/[↑↓]\d/.test(all),h+'\n'+all);
 if(/[↑↓]\d/.test(all))assert(!two&&!all.includes('MESSAGE_2')&&!all.includes('MESSAGE_1'),h+'\n'+all);
 // (The route box that went leaves rows over, which a section that fits there takes back: the newest message only, never an older one.)
 if(!all.includes('╭─ 3 ROUTE'))assert(!all.includes('MESSAGE_1')&&!all.includes('MESSAGE_2'),h+' the messages go before the route box\n'+all);
}
// Every box is closed, whatever the height; an empty route list or message list draws no box (nor head).
for(let h=10;h<=30;h++)for(const q of [null,question]){const rows=text(view.render({width:40,height:h,side:true,sid:'s',configuration:{},model,state:'idle',brief:()=>q}));
 assert.equal(rows.filter(s=>s.includes('╭')).length,rows.filter(s=>s.includes('╰')).length,h+'\n'+rows.join('\n'));assert(rows.length<=h,h);}
rows=text(paint({model:{routes:[],route_count:0,routes_known:true,messages:[]}}));assert(!rows.join('').includes('╭')&&!rows.join('').includes('ROUTE')&&!rows.join('').includes('MESSAGES'),rows.join('\n'));
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

    def test_route_rows_follow_the_route_line_setting_and_open_their_popup(self):
        # Restored 11-15 from v4.36.4 (`..._and_open_in_place`): a press on any row of a folded route opens the route popup with it
        # unfolded. 11-18: an owner has no time of its own (the route's stands on its first line, where a route has a kind word), so
        # the `route line` choice (time or model, kept when the one-row form is short of cells) no longer changes an owner's row:
        # both read its percent beside its model.
        script = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const p=t=>({text:t});
const model={route_count:2,routes:[
 {id:'a',title:'셸 표기 통일',state:'working',kind:'working',elapsed:'42m',flow:'plan ✓ › execute ● › review',tool:'apply_patch',agents:1,
  paint:{glyph:p('⠏'),harness:p('codex'),model_text:p('gpt-6.1-sol'),effort:p('high'),ctx:p('72%'),now:'패치 적용 중',gauge:[p('━━━━━━━'),p('───'),p(' '),p('72%')]}},
 {id:'b',title:'제목만 있는 route 이름이 아주 길어서 잘림',state:'dead',kind:'dead',elapsed:'1h 27m',paint:{glyph:p('✕'),harness:p('claude')}}],
 messages:[{way:'←',who:'[e7]',text:'#1506 병합 가능 — 추가 필수 수정 없음, CI 15/15. 검토서는 inputs/RV-PR1506-R2-1111.md',at:5}]};
const paint=(cfg,width=40,height=30)=>view.render({width,height,side:width<80,model,configuration:cfg,questions:()=>[]}).rows.map(r=>r.map(s=>s.text).join(''));
const time=paint({}),byModel=paint({route_line:'model'});
const at=(rows,title)=>rows.findIndex(s=>s.includes(title.slice(0,5)));
// Spec rules 24, 38 (11-18, route row 5; the CTX row with its gauge under the who row, the NOW under it): the title (with the route's
// mark), then who two cells in (glyph, harness, model, effort: no context, no time), then its CTX row four cells in (`CTX`, the backend's
// five-cell gauge and the percent), then its NOW four cells in; the `route line` choice does not touch this full form.
for(const rows of [time,byModel]){const i=at(rows,'셸 표기');
 assert(/^│ │ \S 셸 표기 통일 +│ │$/.test(rows[i])&&!rows[i].includes('codex')&&/^│ │   ⠋ codex gpt-6\.1-sol high +│ │$/.test(rows[i+1])&&/^│ │ {5}CTX ━+─+ 72% +│ │$/.test(rows[i+2])&&/^│ │ {5}패치 적용 중 +│ │$/.test(rows[i+3]),rows.join('\n'));
 assert(rows.filter(s=>s.includes('━')).every(s=>/^│ │ {5}CTX /.test(s)),'the gauge stands on the CTX row alone\n'+rows.join('\n'));}
// Where the row has room nothing joins the who row: no time on an owner (a route with no context has no CTX row either).
const roomy=paint({},60);
assert(roomy.some(s=>/^│ │   ⠋ codex gpt-6\.1-sol high +│ │$/.test(s))&&roomy.some(s=>/^│ │ {5}CTX ━+─+ 72% +│ │$/.test(s))&&roomy.some(s=>/^│ │   ✕ claude +│ │$/.test(s))&&!roomy.some(s=>/42m|1h 27m/.test(s)),roomy.join('\n'));
// Short of rows the CTX row folds into the who row (its percent at the right edge, no time: an owner has none; at 40 cells the effort fits
// beside it). (13 rows: spec rule 49 (b)'s last row stays.)
const low=paint({},40,13),lowRow=low[at(low,'셸 표기')+1];
assert(/⠋ codex gpt-6\.1-sol high +72% │/.test(lowRow)&&!lowRow.includes('42m')&&!low.some(s=>s.includes('CTX')),low.join('\n'));
assert(low.some(s=>/✕ claude +│ │$/.test(s))&&!low.some(s=>s.includes('1h 27m')),low.join('\n'));
// Narrower, spec rule 36 (a): the harness goes before the model, the percent stays beside it whole. The `route line` choice (kept for a
// time column that only a worker fills) does not change an owner's row: both settings read alike.
// (30 cells: the owner's row stands two cells in, so the percent still fits its row at 30, not at 26.)
const narrowTime=paint({},30,13),narrowModel=paint({route_line:'model'},30,13);
const lead=rows=>rows[at(rows,'셸 표기')+1];
assert(/⠋ gpt-6\.1-sol +72% │/.test(lead(narrowTime))&&!lead(narrowTime).includes('codex')&&!lead(narrowTime).includes('42m'),narrowTime.join('\n'));
assert.equal(lead(narrowModel),lead(narrowTime),narrowModel.join('\n'));
// Spec rule 29 (c), 36 (c): a long title goes on to its second row (two cells in) instead
// of `…`; who keeps its row under the title (a route with no context draws no `CTX` row, and no time stands on it).
const long=time.findIndex(s=>s.includes('제목만'));
assert(!time[long].includes('…')&&time[long+1].includes('│ │   ')&&time.slice(long,long+2).join('').includes('잘림')&&time[long+2].includes('claude')&&!time[long+2].includes('1h 27m')&&!time[long+2].includes('CTX')&&time[long+3].startsWith('│ ╰'),time.join('\n'));
// Every row of a folded route opens the route popup with it unfolded (11-15: nothing opens in place).
const v=view.render({width:40,height:30,side:true,model,configuration:{},questions:()=>[]});
const hitRows=v.hits.filter(h=>h.action==='popup:ROUTE:a').map(h=>h.row);
assert.equal(hitRows.length,4);assert.deepEqual(hitRows,[hitRows[0],hitRows[0]+1,hitRows[0]+2,hitRows[0]+3]);   // (title, who, CTX row, NOW)
assert(v.hits.some(h=>h.action==='popup:ROUTE')&&!v.hits.some(h=>/^(route|request|message)[:-]/.test(h.action)),JSON.stringify(v.hits));
// Unreported context is left out, never `?`.
assert(!time.join('\n').includes('?'));
// Nothing hangs under a route: no detail guide, no stage flow, no tool row.
for(const rows of [time,byModel])assert(!/┆|plan ✓ › execute|apply_patch/.test(rows.join('\n')),rows.join('\n'));
// Every row keeps the panel width with two-cell Hangul and emoji.
for(const rows of [time,byModel,paint({},80)])for(const s of rows)assert([...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)<=80);
"""
        self.node(script, "panel-view.cjs")

    def test_a_folded_one_step_route_adds_its_tool_only_where_the_row_has_room(self):
        # Supervisor 2026-11-12 had a one-step route's owner row add its open tool; 11-17 (the person: CTX beside the model, the NOW
        # under it) took the tool off the owner row for good, and 11-18 (the person: the CTX row under the first row) moved the
        # context down: the owner's first row reads who only (mark, role, harness, model), under it a `CTX 24%` row two cells further
        # in, then the session's NOW; the owner never carries a time (the route's time stands at the right end of its first line),
        # at any width, and the tool and its file name are drawn nowhere in the side layout.
        script = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const P=(c)=>({glyph:{text:'⠋'},harness:{text:'claude'},model_text:{text:'Opus 5.5'},ctx:{text:c},now:'running the tests'});
const one={id:'a',title:'one step',state:'working',kind:'working',elapsed:'47m',tool:'python3',target:'tools/run-tests.py',paint:P('24%')};
const staged={...one,id:'b',title:'staged',flow:'plan ✓ › execute ● › review',stages:[{name:'plan'},{name:'execute'},{name:'review'}]};
const paint=(width)=>view.render({width,height:30,side:true,model:{routes:[one,staged],route_count:2,messages:[]},sid:'s',
 configuration:{},expandedKeys:[],questions:()=>[]}).rows.map(x=>x.map(p=>p.text).join(''));
const narrow=paint(34),wide=paint(40),roomy=paint(52);
// The owner's blocks: who (title above it), the CTX row two cells further in, then the NOW under it.
const at=wide.findIndex(x=>/│ │   ⠋ claude Opus 5\.5 +│/.test(x));
assert(at>0&&/│ │     CTX 24% +│ │/.test(wide[at+1])&&/│ │     running the tests +│ │/.test(wide[at+2])&&wide[at-1].includes('│ │ ⠋ one step'),wide.join('\n'));
// No tool and no file name on any row, at any width: the owner row no longer carries them.
for(const rows of [narrow,wide,roomy])assert(!rows.some(x=>x.includes('python3')||x.includes('>_')||x.includes('run-tests.py')),rows.join('\n'));
// An owner shows no time at any width (the route's is on its first line): with room the CTX row still reads the percent alone.
for(const rows of [narrow,wide,roomy])assert(!rows.some(x=>/47m/.test(x)),rows.join('\n'));
assert(roomy.some(x=>/│ │   ⠋ claude Opus 5\.5 +│/.test(x))&&roomy.some(x=>/│ │     CTX 24% +│ │/.test(x))&&roomy.some(x=>/│ │     running the tests +│/.test(x)),roomy.join('\n'));
// A route with a kind word has a first line of its own above the title with its time at the right end; the owner's who, its CTX row and its NOW follow.
const kinded=view.render({width:40,height:30,side:true,model:{routes:[{...one,kind_word:'code'}],route_count:1,messages:[]},sid:'s',
 configuration:{},expandedKeys:[],questions:()=>[]}).rows.map(x=>x.map(p=>p.text).join(''));
const ki=kinded.findIndex(x=>/│ │   ⠋ claude Opus 5\.5 +│/.test(x));
assert(ki>2&&/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +47m │ │$/.test(kinded[ki-2])&&kinded[ki-1].includes('│   one step')&&/│ │     CTX 24% +│ │/.test(kinded[ki+1])&&kinded[ki+2].includes('│     running the tests'),kinded.join('\n'));
// Narrower the CTX row stays on its own row (the word `CTX` and the percent), and the NOW keeps its row; no tool, no time.
assert(narrow.some(x=>/│ │   ⠋ claude Opus 5\.5 +│/.test(x))&&narrow.some(x=>/│ │     CTX 24% +│ │/.test(x))&&narrow.some(x=>/│ │     running the tests +│/.test(x)),narrow.join('\n'));
'''
        self.node(script, "panel-view.cjs")


    def test_a_folded_route_reads_who_context_what_and_kind(self):
        # Spec E9 rules 36 and 38, 11-18 (the person: the CTX row with its gauge under the first row, the NOW on two rows): the kind
        # word (bold) and the route's time at the right end on the first line / what (two rows at most, two cells in) / who (the
        # recorded role in the route's own column, harness, model, effort: no CTX, no time) / its CTX row (four cells in: `CTX`, the
        # gauge and the percent; none for a session with no context and no time of its own) / the session's NOW (four in; none, no
        # row) / the stage line (four in); the one-row form adds neither role nor kind.
        script = self.FIXTURE + r'''
const one={...oneShot,role:'owner',kind_word:'code',paint:{...oneShot.paint,now:'editing host.cjs'}};
const stg={...staged,role:'worker',kind_word:'code',paint:{...staged.paint,now:'editing peer-message.py'}};
const wait={...waiting,role:'owner',kind_word:'code'};
const frame={id:'f',title:'셸 다듬기 방향 잡기',kind:'working',state:'working',elapsed:'3m',shape:'staged',role:'frame',kind_word:'frame',
 stages:[{name:'frame',mark:'●',current:true},{name:'frame-alternative',mark:'●'},{name:'frame-review',mark:''}],
 paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh'),now:'framing'}};
const four=[one,stg,wait,frame];
const box=rows=>rows.filter(s=>s.startsWith('│ │ ')&&!/^│ │ ─ (direct|dispatch|request) ─+ │ │$/.test(s));// (11-17: the zone rules `─ dispatch ───` are no route's row)
// (a) The who row stands two cells in: its mark, the role word (the route's own column), then the harness, model and effort
// (the one-step route's open tool is not on the row, nor `CTX`, nor a time: the context is the row under it, the time is the route's
// and stands on the first line).
const r50=draw(four,48,50),who=box(r50).filter(s=>/│ │   [⠋●] /.test(s));
const whoWant=['  ⠋ owner claude Opus 5.5 xhigh','  ⠋ worker claude Sonnet 5.5 high','  ● owner claude Opus 5.5 xhigh','  ⠋ frame claude Opus 5.5 xhigh'];
assert.deepEqual(who.map(s=>s.slice(4,-4).trimEnd()),whoWant,r50.join('\n'));
// Wider, the who rows stay the same (no time joins them) and each CTX row is `CTX`, the gauge and the percent, four cells in; a session
// with no context (the waiting owner, the frame leg) and no time of its own has no CTX row.
const r64=box(draw(four,64,50)),ctxRows=rows=>rows.filter(s=>/^│ │     CTX /.test(s)).map(s=>s.slice(4,-4).trimEnd());
assert.deepEqual(r64.filter(s=>/│ │   [⠋●] /.test(s)).map(s=>s.slice(4,-4).trimEnd()),whoWant,r64.join('\n'));
assert.deepEqual(ctxRows(r64),['    CTX ━━─── 38%','    CTX ━──── 12%'],r64.join('\n'));
assert.deepEqual(ctxRows(box(r50)),['    CTX ━━─── 38%','    CTX ━──── 12%'],r50.join('\n'));
assert(!r64.some(s=>/ · \d+h? ?\d*m\b/.test(s)),'a session with no time of its own shows none\n'+r64.join('\n'));
// An unrecorded role leaves no column; nothing is made up.
const unnamed=box(draw([one,{...stg,role:undefined}],48,50));
assert(unnamed.some(s=>s.startsWith('│ │   ⠋ claude Sonnet 5.5 high'))&&!unnamed.join('').includes('worker'),unnamed.join('\n'));
// Short of cells effort goes, then the harness; the role and the model stay.
const r32=box(draw(four,32,50));
assert(r32.some(s=>s.startsWith('│ │   ⠋ worker Sonnet 5.5'))&&r32.some(s=>s.startsWith('│ │   ⠋ owner Opus 5.5'))&&!r32.join('').includes('xhigh'),r32.join('\n'));
// (38) The gauge stands on the CTX row only (never on a who row); an unknown context leaves no `CTX` row and no time stands in its place;
// the NOW is a row of its own, only where the session has one (none: no row, never an empty one).
assert(!box(r50).some(s=>/[━]/.test(s)&&/│ │   [⠋●] /.test(s)),r50.join('\n'));
const waitAt=box(r50).findIndex(s=>s.includes('● owner'));
assert(!/CTX/.test(box(r50)[waitAt])&&!/CTX/.test(box(r50)[waitAt+1])&&box(r50)[waitAt-2].includes('4/7: host-run'),'an unknown context draws no CTX\n'+r50.join('\n'));
assert(!box(draw([one,{...frame,elapsed:''}],48,50)).some(s=>/^│ │ +│ │$/.test(s)),'no empty row');
const noNow=box(draw([{...frame,paint:{...frame.paint,now:''}}],48,50));
assert(box(draw([frame],48,50)).some(s=>/^│ │     framing +│ │$/.test(s))&&!noNow.some(s=>/^│ │     \S/.test(s)&&!/\d\/\d/.test(s))&&!noNow.some(s=>/^│ │ +│ │$/.test(s)),'a session with no NOW has no NOW row\n'+noNow.join('\n'));
// The order: the kind word, what, who, its CTX row, its NOW, then the stage line.
const at=box(r50).findIndex(s=>s.includes('⠋ owner claude Opus 5.5 xhigh'));
assert(/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +41m │ │$/.test(box(r50)[at-2])&&box(r50)[at-1].includes('│ │   셸 상태·표시 고치기')&&/^│ │     CTX ━━─── 38% +│ │$/.test(box(r50)[at+1])&&/^│ │     editing host\.cjs +│ │$/.test(box(r50)[at+2]),r50.join('\n'));
assert(box(r50).some((s,i)=>s.includes('worker claude Sonnet 5.5 high')&&/^│ │     CTX ━──── 12% +│ │$/.test(box(r50)[i+1])&&/^│ │     editing peer-message\.py +│ │$/.test(box(r50)[i+2])&&!/^│ │     staged/.test(box(r50)[i+3])&&box(r50).some(t=>t.includes('● code · 3/7: execute'))),r50.join('\n'));   // (the stage is on the first line)
// (b) Fleet's kind word leads the first line (bold, in the body color) and, with it, the stage (`code · 3/7: execute`: where the
// width allows, no stage row stands apart); the mode and intensity stay in the opened KIND. A route with no stage line (solo) has none: its NOW ends it.
for(const want of ['│ │ ● code ','│ │ ● frame ','│ │ ● code · 3/7: execute','│ │ ● code · 4/7: host-run','│ │ ● frame · 1/3: frame'])
 assert(r50.some(s=>s.includes(want)),want+'\n'+r50.join('\n'));
assert(!r50.join('').includes('(dev')&&!r50.some(s=>/one-shot/.test(s)),r50.join('\n'));
const r80=draw(four,80,50).join('\n');
assert(r80.includes('⠋ owner claude Opus 5.5 xhigh')&&r80.includes('CTX ━━─── 38%')&&!r80.includes('CTX ━━─── 38% · 41m')&&r80.includes('editing host.cjs')&&!r80.includes('· Edit')&&r80.includes('3/7: execute · owner Opus 5.5'),r80);
// The bottom layout's kind column: the kind leads it (`code · 3/7: execute`) and goes first, before the tool.
const kindAt=w=>draw([one,stg,wait,{...failed,role:'owner',kind_word:'code'}],w,20,{side:false}).find(s=>s.startsWith('│ │ ')&&s.includes('세션 간'));
assert(/│ code · 3\/7: execute +(\d+h )?\d+m │ │$/.test(kindAt(140))&&!kindAt(140).includes('>_'),kindAt(140));   // (11-18: no command column)
assert(/│ code · 3\/7: execute +(\d+h )?\d+m │ │$/.test(kindAt(100))&&/│ code · 3\/7: execute +(\d+h )?\d+m │ │$/.test(kindAt(90))&&!kindAt(84).includes('code ·')&&/│ 3\/7: execute +(\d+h )?\d+m │ │$/.test(kindAt(84)),kindAt(84)+'\n'+kindAt(100));   // (11-19: with no `staged ` the kind stays to 87 cells)
// (c) A title takes two rows at most, the second ending in `…`; short of rows, one.
const long={...one,id:'l',title:'아주 긴 route 이름 — 셸 패널의 접힌 route 줄에서 이름이 세 줄을 넘으면 둘째 줄 끝에 줄임 표시가 붙는지 확인하는 이름'};
const lr=box(draw([one,long],40,50)),li=lr.findIndex(s=>s.includes('아주 긴'));
assert(lr[li+1].includes('…')&&lr[li+2].includes('│ │   ⠋ owner ')&&!lr.join('').includes('확인하는'),lr.join('\n'));
// (d) The one-row form: mark, title, harness, percent and time; no role, no kind line.
const nine=[...Array(9)].map((_,i)=>({...[one,stg,wait][i%3],id:'n'+i,title:[one,stg,wait][i%3].title+' '+(i+1)}));
const r30=box(draw(nine,40,30)),flat=r30.filter(s=>/│ │ [⠋●] \S/.test(s));
assert(flat.length>0&&flat.every(s=>!/owner|worker|code/.test(s))&&!r30.some(s=>/^│ │   /.test(s)),r30.join('\n'));
// Short of rows: a title's second row, then the NOW rows go and the owner's row takes the one-row fold
// (the percent at the right edge, and no time: a session with no time of its own has none; the routes' times stand on their first lines),
// then the stage line, then the one-row form.
const r44=box(draw(nine,40,50));
assert(!r44.some(s=>s.includes('━'))&&r44.filter(s=>/│ │   [⠋●] (owner|worker) .* \d+% │ │$/.test(s)).length===6&&!r44.some(s=>/│ │   [⠋●] .* \d+m │ │$/.test(s))&&r44.filter(s=>/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +(\d+h )?\d+m │ │$/.test(s)).length===9&&!r44.some(s=>/^│ │ +staged/.test(s)),r44.join('\n'));   // (11-18: the stage text is on the first line, never a row apart)
for(let h=16;h<=80;h++){
 const rows=box(draw(nine,40,h)),nows=rows.filter(s=>/│ │     editing /.test(s)).length,stages=rows.filter(s=>/│ │ .*\d\/\d/.test(s)).length;
 // (11-18, round 5: a stage name the first line has no room for stands on a stage row under the owner, the first of the steps after the NOW rows to go.)
 const apart=rows.filter(s=>/^│ │ {4,5}\d/.test(s)).length;
 const second=rows.some(s=>/^│ │   \d +│ │$/.test(s));
 // (11-19: with no `staged ` the stage's name fits on the first line at this width: its six first lines carry the stage, none stands on a row apart)
 if(nows)assert(stages===6&&apart===0,h+' the NOW rows go before the stage line\n'+rows.join('\n'));
 if(second)assert(nows===6,h+' a title keeps its second row only while the NOW rows stand\n'+rows.join('\n'));
}
'''
        self.node(script, "panel-view.cjs")


    def test_a_route_shows_its_owner_first_and_its_live_workers_in_a_box_under_it(self):
        # Spec E9 rules 43, 48, 57, 11-18 (the person: the CTX row with its gauge under the first row, the NOW on two rows, the
        # stage's workers in a box): beside the native area a route's rows are its kind and time (first line), its title, its owner
        # (two cells in, no branch: mark, role, harness, model, effort), the owner's CTX row (`CTX`, the gauge, the percent; no time),
        # the owner's NOW (four in; none, no row) and the stage box: the workers running now stand in one box under the owner whose
        # top line is the stage text (`┌ 3/7: execute ──┐`, the stage row apart is not drawn), each worker's who row, its CTX
        # row with its own time (`CTX ━━ 22% · 12m`) and its NOW two cells inside the box's side. The open tool and the stage name are
        # not drawn on a worker, nor the tool on the owner. The title takes its rows whole. An older backend's row (no
        # `workers_live`) keeps its one doer.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,e,pct,bar,now)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)],...(now?{now}:{})});
const solo={id:'s',title:'셸 상태·표시 고치기',kind:'working',state:'working',elapsed:'41m',shape:'one-shot-owner',kind_word:'code',role:'owner',tool:'Edit',
 stages:[{name:'one-shot',current:true,mark:'●'}],paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','38%','━━━','editing host.cjs')},workers_live:[]};
const worker=(h,m,e,pct,bar,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',...o,paint:{glyph:P(o.kind==='idle'?'●':'⠋'),...doer(h,m,e,pct,bar,o.now)}});
const one={id:'w',title:'속도 3단계',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'worker',doer_worker:true,
 owner_model:'Opus 5.5',paint:{glyph:P('⠋'),...doer('claude','Sonnet 5.5','high','22%','━━','writing the worker side')},owner_paint:doer('claude','Opus 5.5','xhigh','52%','━━━━','waiting for the worker'),
 workers_live:[worker('claude','Sonnet 5.5','high','22%','━━',{tool:'Edit',target:'utilities/panel.py',now:'editing utilities/panel.py'})]};
const two={...one,id:'t',title:'검토 두 갈래',role:'owner',doer_worker:false,owner_paint:undefined,paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','61%','━━━━','collecting reviews')},
 workers_live:[worker('codex','gpt-5.5','high','34%','━━',{tool:'Bash',now:'running the codex review'}),worker('opencode','glm-5.2','max','71%','━━━━━',{kind:'idle',elapsed:'8m',now:'waiting for the owner'})]};
const box=rows=>{const a=rows.findIndex(s=>/╭─ \d+ ROUTE/.test(s));return rows.slice(a,a+rows.slice(a).findIndex(s=>s.startsWith('│ ╰'))+1).filter(s=>!/^│ │ ─ (direct|dispatch|request) ─+ │ │$/.test(s));};// (11-17: without the zone rules `─ dispatch ───`)
const col=(row,word)=>[...row.slice(0,row.indexOf(word))].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
// No worker: the kind / title / owner (who only) / its CTX row / its NOW; no stage box, no `↳`, no time on the owner.
let b=box(draw([solo],40,60)),at=b.findIndex(s=>s.includes('⠋ owner claude Opus 5.5 xhigh'));
assert(at>0&&/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +41m │ │$/.test(b[at-2])&&/^│ │   셸 상태·표시 고치기 +│ │$/.test(b[at-1])&&/^│ │     CTX ━━━─+ 38% +│ │$/.test(b[at+1])&&/^│ │     editing host\.cjs +│ │$/.test(b[at+2])&&b.length===at+4,b.join('\n'));
assert.equal(col(b[at+1],'CTX'),col(b[at],'owner'));assert.equal(col(b[at+2],'editing'),col(b[at],'owner'));
assert(!/[└├]─|╭─ [⠋●]|↳|Edit /.test(b.join('\n'))&&!b.some(s=>/^│ │ +│/.test(s))&&!b[at-1].includes('41m')&&!/CTX|━|41m/.test(b[at]),b.join('\n'));
// Where the row has room nothing joins the owner's rows: its CTX row has no time (the route's stands on the first line).
b=box(draw([solo],64,60));at=b.findIndex(s=>s.includes('⠋ owner claude Opus 5.5 xhigh'));
assert(at>0&&/^│ │     CTX ━━━─+ 38% +│ │$/.test(b[at+1])&&/^│ │     editing host\.cjs +│ │$/.test(b[at+2])&&!/41m/.test(b[at]+b[at+1])&&/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +41m │ │$/.test(b[at-2]),b.join('\n'));
// One worker: the kind, the title and the owner's rows (no guide), its CTX row and NOW, then the stage box two cells in: the stage text on its top
// line, the worker's who, its CTX row with its own time and its NOW (here on two rows, wrapped between words) inside the box's side. The
// owner stands as a dim `●`.
b=box(draw([one],40,60));at=b.findIndex(s=>s.includes('● owner  claude Opus 5.5 xhigh'));
for(const [i,want] of [[-2,/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +(\d+h )?\d+m │ │$/],[-1,/^│ │   속도 3단계 +│ │$/],[1,/^│ │     CTX ━━━━─+ 52% +│ │$/],[2,/^│ │     waiting for the worker +│ │$/],
 [3,/^│ │   ┌ 3\/7: execute ─+┐ │ │$/],[4,/^│ │   │ ⠋ worker claude Sonnet 5\.5 +│ │ │$/],[5,/^│ │   │   CTX ━━─+ 22% · 12m +│ │ │$/],[6,/^│ │   │   editing +│ │ │$/],[7,/^│ │   │   utilities\/panel\.py +│ │ │$/],
 [8,/^│ │   └─+┘ │ │$/],[9,/^│ ╰─+╯ │$/]])
 assert(want.test(b[at+i]),i+'\n'+b.join('\n'));
// Spec rule 57 (c): the box's corner stands under the owner's mark, the worker's mark two cells in, its CTX row and NOW under its role word.
assert.equal(col(b[at+3],'┌'),col(b[at],'●'));assert.equal(col(b[at+4],'⠋'),col(b[at+3],'┌')+2);
assert.equal(col(b[at+5],'CTX'),col(b[at+4],'worker'));assert.equal(col(b[at+6],'editing'),col(b[at+4],'worker'));
assert.equal(col(b[at+1],'CTX'),col(b[at],'owner'));assert.equal(col(b[at+2],'waiting'),col(b[at],'owner'));
// (Where the row has room the whole NOW shows on one row, and no tool, no target and no stage name stand on the worker's rows.)
assert(box(draw([one],52,60)).some(s=>/^│ │   │   editing utilities\/panel\.py +│ │ │$/.test(s)),box(draw([one],52,60)).join('\n'));
assert(!/execute · |Edit |Bash/.test(box(draw([one],52,60)).join('\n')),box(draw([one],52,60)).join('\n'));
assert(!b.join('').includes('owner Opus'),b.join('\n'));
const v1=view.render({width:40,height:60,side:true,model:{...head,routes:[one],route_count:1},configuration:{},questions:()=>[]});
const rowOf=re=>v1.rows.find(r=>re.test(r.map(p=>p.text).join('')));
const ownerRow=rowOf(/● owner/),workerRow=rowOf(/│ ⠋ worker/);
assert(ownerRow.find(p=>p.text==='●').dim&&ownerRow.find(p=>p.text==='owner ').dim&&ownerRow.some(p=>p.text==='Opus 5.5'),JSON.stringify(ownerRow));
assert(workerRow.find(p=>p.text==='worker').dim&&workerRow.some(p=>p.text==='Sonnet 5.5'),JSON.stringify(workerRow));
// A NOW is bright while its session works or waits on an answer, dim otherwise (the route's kind, a worker's own kind); its words are
// drawn one by one, so the first word stands for the row.
assert.equal(rowOf(/waiting for the worker/).find(p=>p.text==='waiting').dim,false);
assert.equal(rowOf(/^│ │   │   editing/).find(p=>p.text==='editing').dim,false);
const idleRoute=view.render({width:40,height:60,side:true,model:{...head,routes:[{...solo,kind:'idle',state:'idle'}],route_count:1},configuration:{},questions:()=>[]});
assert.equal(idleRoute.rows.find(r=>/editing host/.test(r.map(p=>p.text).join(''))).find(p=>p.text==='editing').dim,true);
// Every row of the route opens it (11-15: the routes popup with it unfolded), the worker's and the box's as the owner's.
const area=v1.hits.filter(h=>h.action==='popup:ROUTE:w').map(h=>h.row),routeRows=v1.rows.map((r,i)=>/^│ │ /.test(r.map(p=>p.text).join(''))&&!/^│ │ ─ dispatch ─+ │ │$/.test(r.map(p=>p.text).join(''))?i:-1).filter(i=>i>=0);// (11-17: the zone rule `─ dispatch ───` opens nothing)
assert.equal(routeRows.length,11,JSON.stringify(routeRows));
for(const i of routeRows)assert(area.includes(i),i+' '+JSON.stringify(area));
// Two workers: each its who, its CTX row and its NOW in the same box; the box's bottom line, then the route box's.
b=box(draw([two],40,60));const top=b.findIndex(s=>/^│ │   │ ⠋ worker codex gpt-5\.5 +│ │ │$/.test(s));
assert(top>0&&/^│ │   │   CTX ━━─+ 34% · 12m +│ │ │$/.test(b[top+1])&&/^│ │   │   running the codex review +│ │ │$/.test(b[top+2])&&/^│ │   │ ● worker opencode glm-5\.2 +│ │ │$/.test(b[top+3])
 &&/^│ │   │   CTX ━+─+ 71% · 8m +│ │ │$/.test(b[top+4])&&/^│ │   │   waiting for the owner +│ │ │$/.test(b[top+5])&&/^│ │   └─+┘ │ │$/.test(b[top+6])&&b[top+7].startsWith('│ ╰'),b.join('\n'));
// A worker's NOW is dim when the worker is not working (its own kind), bright while it works.
const v2=view.render({width:40,height:60,side:true,model:{...head,routes:[two],route_count:1},configuration:{},questions:()=>[]});
const nowOf=(re,word)=>v2.rows.find(r=>re.test(r.map(p=>p.text).join(''))).find(p=>p.text===word);
assert.equal(nowOf(/running the codex review/,'running').dim,false);assert.equal(nowOf(/waiting for the owner/,'waiting').dim,true);
// No role recorded anywhere: no role column, one space before the model.
b=box(draw([{...solo,role:undefined}],40,60));
assert(b.some(s=>s.includes('│ │   ⠋ claude Opus 5.5 xhigh'))&&b.some(s=>/^│ │     CTX ━━━─+ 38% +│ │$/.test(s))&&b.some(s=>/^│ │     editing host\.cjs +│ │$/.test(s)),b.join('\n'));
// An unknown context: no CTX label (owner or worker). The owner, with no context and no time of its own, has no CTX row at all; a worker's
// own time ends its first row (no CTX row of only a time) where it has one (56 cells).
const noCtx=[{...one,owner_paint:{harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')},workers_live:[{...worker('claude','Sonnet 5.5','high','22%','━━'),paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Sonnet 5.5')}}]}];
b=box(draw(noCtx,56,60));at=b.findIndex(s=>s.includes('● owner'));
assert(!b.join('').includes('CTX')&&/^│ │   ● owner  claude Opus 5\.5 xhigh +│ │$/.test(b[at])&&/^│ │   ┌ 3\/7: execute ─+┐ │ │$/.test(b[at+1])&&b.some(s=>/^│ │   │ ⠋ worker claude Sonnet 5\.5 +12m +│ │ │$/.test(s))&&!b.some(s=>/^│ │   │   12m +│ │ │$/.test(s))&&!b.some(s=>/owner.*2h/.test(s)&&!/code/.test(s)),b.join('\n'));
// A session with no NOW has no NOW row (none empty): the owner's CTX row, then the stage box straight under it.
b=box(draw([{...one,owner_paint:{...one.owner_paint,now:''}}],40,60));at=b.findIndex(s=>s.includes('● owner'));
assert(/^│ │     CTX /.test(b[at+1])&&/^│ │   ┌ 3\/7: execute /.test(b[at+2])&&!b.some(s=>/^│ │ +│ │$/.test(s)),b.join('\n'));
// A title on two rows: both rows the title's; the time on the first line, never on the owner's rows, its CTX row and NOW under it.
b=box(draw([{...solo,title:'아주 긴 route 이름이 두 줄로 넘어가는 경우의 시간 자리 확인'}],40,60));at=b.findIndex(s=>s.includes('아주 긴'));
assert(!b[at].includes('41m')&&!b[at+1].includes('41m')&&/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +41m │ │$/.test(b[at-1])&&b[at+2].includes('⠋ owner')&&b[at+3].includes('CTX ━')&&b[at+4].includes('editing host.cjs'),b.join('\n'));
b=box(draw([{...solo,title:'아주 긴 route 이름이 두 줄로 넘어가는 경우의 시간 자리 확인'}],64,60));at=b.findIndex(s=>s.includes('아주 긴'));
assert(!b[at].includes('41m')&&!b[at+1].includes('41m')&&b[at+2].includes('⠋ owner')&&!b[at+2].includes('CTX')&&b[at+3].includes('CTX ━━━')&&!b[at+3].includes('41m'),b.join('\n'));
// Hangul in the owner's title and the worker's NOW: every line keeps its cells, in each form of the box.
for(const configuration of [{},{worker_box:'open'},{worker_box:'none'}])for(const width of [32,34,38,40,44])
 for(const s of view.render({width,height:60,side:true,fill:true,model:{...head,routes:[{...one,title:'한글 제목이 길게 이어지는 경우',workers_live:[worker('claude','Sonnet 5.5','high','22%','━━',{tool:'Edit',now:'문서/한글 파일 고치는 중'})]},two],route_count:2},state:'working',configuration,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join('')))
  assert.equal(cells(s),width,width+' '+s);
// Short of cells (rule 48 (d)): the worker's who row (inside the box) loses its effort, then its harness; role and model stay and a model longer
// than the row is cut with `…`. The CTX row under it has its percent whole and its time.
const whoAt=w=>box(draw([one],w,60)).find(s=>/│ [⠋●] worker/.test(s));
assert(/│ │   │ ⠋ worker claude Sonnet 5\.5 high +│ │ │$/.test(whoAt(52)),box(draw([one],52,60)).join('\n'));
assert(/│ │   │ ⠋ worker claude Sonnet 5\.5 +│ │ │$/.test(whoAt(44))&&/│ │   │ ⠋ worker claude Sonnet 5\.5 +│ │ │$/.test(whoAt(40)),box(draw([one],44,60)).join('\n'));
assert(/│ │   │ ⠋ worker Sonnet 5\.5 +│ │ │$/.test(whoAt(38))&&/│ │   │ ⠋ worker Sonnet 5\.5 +│ │$/.test(whoAt(36))&&/│ │   │ ⠋ worker Sonnet 5\.5 +│ │$/.test(whoAt(32)),box(draw([one],36,60)).join('\n'));
assert(box(draw([one],32,60)).some(s=>/^│ │   │   CTX 22% · 12m +│ │$/.test(s))&&box(draw([one],36,60)).some(s=>/^│ │   │   CTX ━━─+ 22% · 12m +│ │$/.test(s)),box(draw([one],32,60)).join('\n'));
// A NOW longer than its row goes on a second row at a word boundary, the last row ending in `…` where it still goes on: no row is wider than the box.
b=box(draw([one],34,60));
assert(b.some(s=>/^│ │   │   editing +│ │$/.test(s))&&b.some(s=>/^│ │   │   utilities\/panel\.py +│ │$/.test(s)),b.join('\n'));
b=box(draw([{...one,workers_live:[worker('claude','Sonnet 5.5','high','22%','━━',{now:'editing utilities/panel.py and then running every test of the suite twice'})]}],40,60));
assert(b.some(s=>/^│ │   │   utilities\/panel\.py and… +│ │ │$/.test(s))&&b.filter(s=>/^│ │   │   \S/.test(s)&&!/CTX/.test(s)).length===2,b.join('\n'));
b=box(draw([{...one,workers_live:[worker('opencode','deepseek-v4.1-flash','max','22%','━━')]}],40,60));
assert(b.some(s=>/│ │   │ ⠋ worker deepseek-v4\.1-\S*… +│ │ │$/.test(s))&&b.some(s=>s.includes('● owner  claude Opus 5.5')),b.join('\n'));
assert(box(draw([{...one,workers_live:[worker('opencode','deepseek-v4.1-flash','max','22%','━━')]}],48,60)).some(s=>/│ │   │ ⠋ worker deepseek-v4\.1-flash +│ │ │$/.test(s)));
// (Room for the harness and the effort too.)
assert(box(draw([{...one,workers_live:[worker('opencode','deepseek-v4.1-flash','max','22%','━━')]}],64,60)).some(s=>/│ │   │ ⠋ worker opencode deepseek-v4\.1-flash max +│ │ │$/.test(s)),box(draw([{...one,workers_live:[worker('opencode','deepseek-v4.1-flash','max','22%','━━')]}],64,60)).join('\n'));
// The same worker in the `none` form hangs by a `↳` four cells in under the stage row, its CTX row and NOW eight in (no box is drawn).
const flat=view.render({width:40,height:60,side:true,fill:true,model:{...head,routes:[one],route_count:1},state:'working',configuration:{worker_box:'none'},questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
b=box(flat);at=b.findIndex(s=>s.includes('● owner'));
for(const [i,want] of [[3,/^│ │     3\/7: execute +│ │$/],[4,/^│ │     ↳ ⠋ worker claude Sonnet 5\.5 +│ │$/],[5,/^│ │         CTX ━━─+ 22% · 12m +│ │$/],[6,/^│ │         editing +│ │$/],[7,/^│ │         utilities\/panel\.py +│ │$/],[8,/^│ ╰─+╯ │$/]])
 assert(want.test(b[at+i]),i+'\n'+b.join('\n'));
assert.equal(col(b[at+3],'3/7'),col(b[at+2],'waiting'));assert.equal(col(b[at+4],'↳'),col(b[at+2],'waiting'));assert.equal(col(b[at+6],'editing'),col(b[at+4],'worker'));
// An older backend's row: one doer, its who row, its CTX row (no time: the route's is on the first line), its NOW, the stage row; no `↳`, no box.
const older={...one,owner_paint:undefined,workers_live:undefined};
b=box(draw([older],60,60));
assert(!/↳|└─|┌/.test(b.join(''))&&b.some(s=>/⠋ worker claude Sonnet 5\.5 high +│ │$/.test(s))&&b.some(s=>/^│ │     CTX ━━─+ 22% +│ │$/.test(s))&&b.some(s=>/^│ │     writing the worker side +│ │$/.test(s))
 &&b.some(s=>s.includes('owner Opus 5.5')),b.join('\n'));
// The bottom layout keeps its rows whatever the route carries.
// (11-18: under the native area a route's row reads its owner, as the NOW under it does, so what a worker holds changes nothing there.)
assert.deepEqual(draw([solo,one,two],120,20,{side:false}),draw([{...solo,workers_live:undefined},{...older,owner_paint:one.owner_paint},{...two,workers_live:undefined}],120,20,{side:false}));
"""
        self.node(script, "panel-view.cjs")


    def test_a_route_box_carries_its_time_on_the_ctx_row_and_its_workers_two_cells_further_in(self):
        # Spec E9 rule 57, 11-18 (the person: the CTX row with its gauge under the first row; the stage's workers in a box): a
        # session is its who row (mark, role, harness, model, effort), under it its CTX row two cells further in (`CTX`, the gauge,
        # the percent), then its NOW. The time stands on that CTX row (`CTX ━━ 22% · 12m`) only when it is the session's own, a
        # worker's; an owner never shows one (the route's time stands at the right end of the route's first line), and neither a
        # title, a NOW nor a stage row carries one. The workers stand in the stage box two cells in under the owner (`closed`, the
        # default); `worker_box: none` keeps the `↳` form, four cells in, their rows eight in. Short of cells the CTX row loses its
        # gauge's width, the word `CTX` and then the time; widths 32-48 with the rows running short: every row keeps the panel's
        # width, the steps of rule 48 (c) hold (the box closes, the NOW's second row, the CTX row folds into the who row, the NOW
        # goes; folded, the percent ends the row).
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,e,pct,bar,now)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)],...(now?{now}:{})});
const worker=(h,m,pct,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',tool:'Edit',...o,paint:{glyph:P('⠋'),...doer(h,m,'high',pct,'━━','worker step')}});
const solo={id:'s',title:'셸 상태·표시 고치기',kind:'working',state:'working',elapsed:'41m',shape:'one-shot-owner',kind_word:'code',role:'owner',tool:'Edit',
 stages:[{name:'one-shot',current:true,mark:'●'}],paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','38%','━━━','solo step')},workers_live:[]};
const one={id:'w',title:'한글 제목 — 속도 3단계의 남은 재계산',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'owner',
 paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','52%','━━━━','owner step')},workers_live:[worker('claude','Sonnet 5.5','22%',{target:'문서/한글 파일.md'})]};
const two={...one,id:'t',title:'검토 두 갈래',workers_live:[worker('codex','gpt-5.5','34%'),worker('opencode','glm-5.2','71%',{elapsed:'8m'})]};
const text=r=>r.map(p=>p.text).join('');
for(const none of [false,true])for(let width=32;width<=48;width++){
 for(let h=60;h>=12;h--){
  const v=view.render({width,height:h,side:true,fill:true,model:{...head,routes:[solo,one,two],route_count:3},state:'working',configuration:none?{worker_box:'none'}:{},questions:()=>[]});
  const rows=v.rows.map(text),where=(none?'none ':'box ')+width+'x'+h+'\n'+rows.join('\n');
  for(const s of rows)assert.equal(cells(s),width,where);
  // (a), (b) The time follows a percent after `CTX`, on a worker's CTX row alone (an owner has none, 11-18); it never ends a who row,
  // a title, a NOW or a stage row.
  for(const s of rows.filter(s=>/^│ │ +\S.* CTX /.test(s))){if(/ · \d/.test(s))assert(/CTX .*\d+[%K] · (\d+h )?\d+m +(│ )?│ │$/.test(s)&&!/^│ │ {5}CTX /.test(s),where);}
  assert(!rows.some(s=>/(고치기|재계산|두 갈래|solo step|owner step|worker step|execute) +(\d+h )?\d+m +│ │$/.test(s)),where);
  assert(!rows.some(s=>/^│ │ +[⠋●] (owner|frame|worker) .* (\d+h )?\d+m +(│ )?│ │$/.test(s)&&/owner/.test(s)),'an owner shows no time of its own\n'+where);
  // (c) The owner's NOW stands four cells in, under its CTX row. A worker stands in the stage box two cells in (its mark, then its CTX
  // row and NOW inside the box's side), or, in the `none` form, hangs four cells in by a `↳`, its NOW eight in. The stage text is
  // the top line of the box (`┌ 3/7: execute`) or, in the `none` form and where the workers went, a row four in.
  for(const s of rows.filter(s=>/^│ │ +(owner|solo) step/.test(s)))assert(/^│ │ {5}(owner|solo) step/.test(s),where);
  for(const s of rows.filter(s=>/^│ │ +(staged )?\d+\/\d+: /.test(s)))assert(/^│ │ {5}(staged )?\d+\/\d+: /.test(s),where);
  if(none){
   for(const s of rows.filter(s=>s.includes('↳')))assert(/^│ │     ↳ [⠋●] /.test(s),where);
   for(const s of rows.filter(s=>/^│ │ +worker step/.test(s)))assert(/^│ │ {9}worker step/.test(s),where);
   assert(!rows.some(s=>/^│ │   [┌│└]/.test(s)),where);
  }else{
   for(const s of rows.filter(s=>/^│ │ .*┌/.test(s)))assert(/^│ │   ┌ (staged )?\d+\/\d+: /.test(s),where);
   for(const s of rows.filter(s=>/^│ │ +│ +worker step/.test(s)))assert(/^│ │   │ {3}worker step/.test(s),where);
   for(const s of rows.filter(s=>/^│ │ +│ +[⠋●] worker/.test(s)))assert(/^│ │   │ [⠋●] worker/.test(s),where);
   assert(!rows.some(s=>s.includes('↳')),where);
  }
  // The gauge stands on a CTX row only, and that row has the word first, four cells in for an owner.
  for(const s of rows.filter(s=>/^│ │ /.test(s)&&/━/.test(s.slice(2,-2))))assert(/^│ │ +(│ +)?CTX /.test(s),where);
  for(const s of rows.filter(s=>/^│ │ +CTX /.test(s)))assert(/^│ │ {5}CTX /.test(s)||none&&/^│ │ {9}CTX /.test(s),where);
  assert(!rows.some(s=>/[╭├]─ [⠋●]/.test(s)),where);
 }
}
// At 40 cells, the picture of rule 57 (the stage box closed): the owner's who, its CTX row two cells further in with no time, its NOW,
// then the stage box with the worker's who, CTX row (with its own time) and NOW; every right `│` of the box in one column.
const b=draw([one],40,60),at=b.findIndex(s=>s.includes('● owner'));
for(const [i,want] of [[-3,/^│ │ ● code · 3\/7 +2h 10m │ │$/],[-2,/^│ │   한글 제목 — 속도 3단계의 남은 +│ │$/],[-1,/^│ │   재계산 +│ │$/],[0,/^│ │   ● owner  claude Opus 5\.5 xhigh +│ │$/],[1,/^│ │     CTX ━━━━──── 52% +│ │$/],[2,/^│ │     owner step +│ │$/],
 [3,/^│ │   ┌ 3\/7: execute ─+┐ │ │$/],[4,/^│ │   │ ⠋ worker claude Sonnet 5\.5 +│ │ │$/],[5,/^│ │   │   CTX ━━─+ 22% · 12m +│ │ │$/],[6,/^│ │   │   worker step +│ │ │$/],[7,/^│ │   └─+┘ │ │$/],[8,/^│ ╰─+╯ │$/]])
 assert(want.test(b[at+i]),i+'\n'+b.join('\n'));
// The `none` form is the older one: the stage row four cells in, the worker hanging by a `↳` four cells in, its CTX row and NOW eight in.
const old=draw([one],40,60).length&&view.render({width:40,height:60,side:true,fill:true,model:{...head,routes:[one],route_count:1},state:'working',configuration:{worker_box:'none'},questions:()=>[]}).rows.map(text);
const oat=old.findIndex(s=>s.includes('● owner'));
for(const [i,want] of [[1,/^│ │     CTX ━━━━──── 52% +│ │$/],[2,/^│ │     owner step +│ │$/],[3,/^│ │     3\/7: execute +│ │$/],[4,/^│ │     ↳ ⠋ worker claude Sonnet 5\.5 +│ │$/],[5,/^│ │         CTX ━━─+ 22% · 12m +│ │$/],[6,/^│ │         worker step +│ │$/],[7,/^│ ╰─+╯ │$/]])
 assert(want.test(old[oat+i]),i+'\n'+old.join('\n'));
// With room, the owner's CTX row has no time and `CTX ━━ 22% · 12m` stands under the worker's who.
const wide=draw([one],72,60);
assert(wide.some(s=>/^│ │   ● owner  claude Opus 5\.5 xhigh +│ │$/.test(s))&&wide.some(s=>/^│ │     CTX ━━━━──── 52% +│ │$/.test(s))&&wide.some(s=>/^│ │   │ ⠋ worker claude Sonnet 5\.5 high +│ │ │$/.test(s))&&wide.some(s=>/^│ │   │   CTX ━━─+ 22% · 12m +│ │ │$/.test(s)),wide.join('\n'));
// (d) Short of cells the who row loses its effort, then its harness (the role and the model stay); it carries neither the context nor
// a time at any width, the CTX row under it keeps its percent whole.
const ownerRows=w=>{const rows=draw([solo],w,60),i=rows.findIndex(s=>/^│ │   ⠋ /.test(s));return [rows[i],rows[i+1]];};
for(let w=32;w<=72;w++){const [row,ctx]=ownerRows(w);
 assert(!/CTX|41m|38%/.test(row)&&row.includes('Opus 5.5'),w+' '+row);      // the model stays, nothing of the context or the time
 if(!row.includes('xhigh')||!row.includes('claude'))assert(row.includes('Opus 5.5'),w+' '+row);
 if(!row.includes('claude'))assert(!row.includes('xhigh'),w+' '+row);   // the harness goes after the effort
 assert(/^│ │     CTX ━━━─+ 38% +│ │$/.test(ctx)&&!ctx.includes('41m'),w+' '+ctx);
}
assert(ownerRows(32)[0].includes('⠋ owner Opus 5.5')&&ownerRows(38)[0].includes('⠋ owner claude Opus 5.5')&&!ownerRows(38)[0].includes('xhigh')&&ownerRows(48)[0].includes('⠋ owner claude Opus 5.5 xhigh'),[32,38,48].map(w=>ownerRows(w)[0]).join('\n'));
const at32=draw([one],32,60);
assert(at32.some(s=>/^│ │   ● owner  Opus 5\.5 +│ │$/.test(s))&&at32.some(s=>/^│ │     CTX ━━━━──── 52% +│ │$/.test(s))&&at32.some(s=>/^│ │   │ ⠋ worker Sonnet 5\.5 +│ │$/.test(s))&&at32.some(s=>/^│ │   │   CTX .*22%.* · 12m +│ │$/.test(s))&&!at32.some(s=>/owner|worker/.test(s)&&s.includes('2h 10m')),at32.join('\n'));   // (the time stands on the first line)
// Folded (rule 48 (c)): the NOW rows are gone and the CTX row has folded into the who row, the percent ending it (no time: an owner has none).
const folded=view.render({width:40,height:26,side:true,fill:true,model:{...head,routes:[one,two],route_count:2},state:'working',configuration:{},questions:()=>[]}).rows.map(text);
assert(folded.some(s=>/^│ │   [⠋●] owner .* 52% +│ │$/.test(s))&&!folded.some(s=>/^│ │ .*CTX/.test(s))&&!folded.some(s=>/owner step|worker step/.test(s))&&!folded.some(s=>/^│ │   [⠋●] owner .*(\d+h )?\d+m +│ │$/.test(s)),folded.join('\n'));
"""
        self.node(script, "panel-view.cjs")


    def test_a_four_cell_context_keeps_the_worker_box_line_at_the_narrowest_panel(self):
        # #1583 review follow-up (가): at 32 cells a four-cell percent (`100%`, or a token count
        # `250K` with no window) made the worker's `CTX` row one cell too wide and its route's right `│`
        # fell off. 11-18 (the person: the CTX row under the first row, the workers in a box): the worker's rows are its who, its
        # CTX row (`CTX`, the gauge, the percent, ` · 12m`) and its NOW, in the stage box under the owner (the default `closed`
        # gives way to the open one where the text inside is under 24 cells, as at these widths; `none` keeps the `↳` form); short
        # of cells the CTX row lets the time, the gauge's width, then the word `CTX` go, and a percent that does not fit goes whole
        # (never a cut `10…`); every row of the worker keeps the panel's width and its route's right line.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
// The gauge at the panel's width, as the backend lays it out (`max(5, min(10, (width-8)//4))` cells).
const gauge=(width,pct)=>{const n=Math.max(5,Math.min(10,Math.floor((width-8)/4)));
 if(!pct.endsWith('%'))return [D(pct)];const on=Math.round(n*parseInt(pct)/100);return [P('━'.repeat(on)),D('─'.repeat(n-on)),D(' '),P(pct)];};
// `top` is the worker's who row, then its CTX row and its NOW row; `ctx` and `now` are where they start in each form.
for(const [name,configuration,top,ctx,now] of [['box',{},/^│ │   │ ⠋ worker/,/^│ │   │   CTX /,/^│ │   │   running the/],
 ['none',{worker_box:'none'},/^│ │     ↳ ⠋ worker/,/^│ │         CTX /,/^│ │         running the/]])
for(let width=32;width<=38;width++)for(const pct of ['100%','250K','9%']){
 const route={id:'w',title:'속도 3단계',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'owner',
  paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh'),ctx:P('52%'),gauge_row:gauge(width,'52%')},
  workers_live:[{kind:'working',role:'worker',stage:'execute',elapsed:'12m',tool:'Edit',
   paint:{glyph:P('⠋'),harness:P('claude'),model_text:P('Sonnet 5.5'),effort:P('high'),ctx:P(pct),gauge_row:gauge(width,pct),now:'running the tests'}}]};
 const rows=view.render({width,height:60,side:true,fill:true,model:{...head,routes:[route],route_count:1},state:'working',configuration,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join('')),seen=name+' '+width+' '+pct+'\n'+rows.join('\n');
 for(const s of rows)assert.equal(cells(s),width,seen);
 const at=rows.findIndex(s=>top.test(s)),bottom=rows.findIndex(s=>/^│ ╰─+╯ │$/.test(s));
 assert(at>0&&bottom>at+2&&ctx.test(rows[at+1])&&now.test(rows[at+2]),seen);
 for(const s of rows.slice(at,bottom))assert(/^│ │ .* │ │$/.test(s),seen);
 // The percent stands whole on the worker's CTX row or not at all, never cut (`10…`); the who row carries none.
 assert((rows[at+1].match(/\d+[%K]/g)||[]).every(g=>g===pct)&&!/\d+[%K]…|[1-9]0…/.test(rows[at+1])&&!/\d+[%K]/.test(rows[at]),seen);
 if(width>=36&&pct==='9%')assert(rows[at+1].includes('9%'),seen);
}
"""
        self.node(script, "panel-view.cjs")


    def test_a_session_now_wraps_to_two_rows_and_the_second_ends_in_an_ellipsis(self):
        # 11-18 (the person: "NOW를 두줄까지는 허용치를 늘려주고 (owner, worker 둘다)"): an owner's and a worker's NOW take up to two rows,
        # wrapped between words (Hangul too); where it goes on past the second row that row ends in `…`; a NOW that fits takes one
        # row, none takes none. Whatever the width no row is wider than the panel, and a closed box keeps its right line.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,pct,now)=>({harness:P(h),model_text:P(m),ctx:P(pct),gauge_row:[P('━━'),D('────'),D(' '),P(pct)],...(now?{now}:{})});
const LONG='한글로 적은 아주 긴 작업 설명이 두 줄을 넘어서 세 줄째로 이어지는 경우 finally done',SHORT='짧은 작업 설명';
const route=(ownerNow,workerNow)=>({id:'w',title:'속도',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'owner',
 paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','52%',ownerNow)},workers_live:[{kind:'working',role:'worker',stage:'execute',elapsed:'12m',paint:{glyph:P('⠋'),...doer('codex','gpt-5.5','34%',workerNow)}}]});
const plain=(routes,width,configuration={})=>view.render({width,height:80,side:true,fill:true,model:{...head,routes,route_count:routes.length},state:'working',configuration,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
// The rows of a session's NOW: from the row under its CTX row to the next row that is not a NOW row (the stage row, the next who row, the box's line, the route's end).
const nowOf=(rows,from)=>{const i=rows.findIndex((s,k)=>k>=from&&/CTX /.test(s)),out=[];
 for(let k=i+1;k<rows.length&&/^│ │ +(│ +)?\S/.test(rows[k])&&!/^│ │ +(│ +)?(CTX |[⠋●✕] |[┌└↳]|\d+\/\d+: |staged )/.test(rows[k]);k++)out.push(rows[k]);return out;};
const words=s=>s.replace(/[│ ]+$/,'').replace(/^[│ ]+/,'').trim();
for(const width of [30,32,34,36,40,44,52]){
 for(const configuration of [{},{worker_box:'open'},{worker_box:'none'}]){
  const rows=plain([route(LONG,LONG)],width,configuration),where=width+' '+JSON.stringify(configuration)+'\n'+rows.join('\n');
  for(const s of rows)assert.equal(cells(s),width,where);
  const owner=nowOf(rows,rows.findIndex(s=>/⠋ owner|● owner/.test(s))),worker=nowOf(rows,rows.findIndex(s=>/worker/.test(s)));
  for(const [name,list] of [['owner',owner],['worker',worker]]){
   // At most two rows; this NOW is longer than two rows at every width here, so exactly two, the second ending in `…`, the first not.
   assert.equal(list.length,2,name+' '+where);
   const [first,second]=list.map(words);
   assert(second.endsWith('…')&&!first.endsWith('…'),name+' '+where);
   // The first row breaks between words: its words are the NOW's first words, whole.
   assert(LONG.startsWith(first+' ')&&LONG.split(' ').length>first.split(' ').length,name+' '+where);
  }
 }
}
// A NOW that fits one row takes one row and ends in no `…`; a NOW of exactly two rows keeps both whole; none takes no row.
for(const width of [36,44]){
 const rows=plain([route(SHORT,SHORT)],width),where=width+'\n'+rows.join('\n');
 assert(nowOf(rows,rows.findIndex(s=>/owner/.test(s))).length===1&&!rows.some(s=>s.includes('…')&&/설명/.test(s)),where);
 const none=plain([route('',undefined)],width),at=none.findIndex(s=>/owner/.test(s));
 assert(/CTX /.test(none[at+1])&&/^│ │   ┌ /.test(none[at+2]),'no NOW, no NOW row\n'+none.join('\n'));
}
{const rows=plain([route('가나다라 마바사아 자차카타 파하',SHORT)],32),list=nowOf(rows,rows.findIndex(s=>/owner/.test(s)));
 assert.equal(list.length,2,rows.join('\n'));assert(!list.some(s=>s.includes('…'))&&words(list[0]).startsWith('가나다라')&&words(list[1]).endsWith('파하'),rows.join('\n'));}
"""
        self.node(script, "panel-view.cjs")

    def test_the_worker_box_is_closed_open_or_none_as_the_setting_says(self):
        # 11-18 (the person: "박스 여부는 내가 골라가면서 볼 수 있게 그냥 옵션에 좀 넣어줘"): `worker_box` is `closed` (the default and what
        # an unknown value reads as), `open` or `none`. A route with workers and a stage line puts the workers of the stage in a box
        # under the owner, the stage text on its top line (`┌ 3/7: execute ──┐`): closed it has all four borders, open the top
        # line and the left side only, `none` the stage row and the `↳` indent as before. The closed box's right line stands in one
        # column for every row whatever its text (Hangul two cells, ASCII one); where its text would be under 24 cells wide it is open.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,e,pct,now)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P('━━'),D('────'),D(' '),P(pct)],...(now?{now}:{})});
const worker=(h,m,pct,now,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',...o,paint:{glyph:P('⠋'),...doer(h,m,'high',pct,now)}});
const route={id:'w',title:'한글 제목 — 속도 3단계의 남은 재계산',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'owner',
 paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','52%','owner 한글 step')},
 workers_live:[worker('claude','Sonnet 5.5','22%','문서/한글 파일 editing'),worker('codex','gpt-5.5','34%','ASCII only now'),worker('opencode','glm-5.2','71%','혼합 mixed 설명 text',{elapsed:'8m'})]};
const plain=(width,configuration)=>view.render({width,height:80,side:true,fill:true,model:{...head,routes:[route],route_count:1},state:'working',configuration,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
// The route box's rows from the stage text (the box's top line, or the stage row in the `none` form) to the end of the workers.
const part=rows=>{const a=rows.findIndex(s=>/^│ │ +(┌ )?(staged )?\d+\/\d+: /.test(s)),b=rows.findIndex(s=>/^│ ╰/.test(s));return rows.slice(a,b);};
// The column of a box's right line: the cells before the last `│` of the box, with the route's `│ │` closing the row.
const edge=s=>cells(s.replace(/ │ │$/,'').replace(/[│┘┐]$/,''));
for(const width of [38,40,44,52,60]){
 const closed=part(plain(width,{})),where=width+'\n'+closed.join('\n');
 // The default is `closed`, and so is a value the setting does not know.
 assert.deepEqual(plain(width,{worker_box:'bogus'}),plain(width,{}),where);assert.deepEqual(plain(width,{worker_box:'closed'}),plain(width,{}),where);
 assert(/^│ │   ┌ 3\/7: execute ─+┐ │ │$/.test(closed[0])&&/^│ │   └─+┘ │ │$/.test(closed.at(-1)),where);
 // Every row of the box (the top, the workers' rows with Hangul and ASCII, the bottom) puts its right line in the one column.
 assert.equal(closed.filter(s=>/^│ │   │ [⠋●] worker/.test(s)).length,3,where);
 for(const s of closed.slice(1))assert(/^│ │   [│└]/.test(s),where);
 assert.equal(new Set(closed.map(edge)).size,1,'one right-line column\n'+where);
 for(const s of plain(width,{}))assert.equal(cells(s),width,where);
 // `open` has the top line and the left side, no right line and no bottom line; its text starts where the closed box's does, and has the
 // room of the right line besides.
 const open=part(plain(width,{worker_box:'open'})),owhere=width+' open\n'+open.join('\n');
 assert(/^│ │   ┌ 3\/7: execute ─+ │ │$/.test(open[0])&&!open.some(s=>/[┐┘└]/.test(s))&&open.slice(1).every(s=>/^│ │   │ /.test(s)),owhere);
 // `none` is the older form: the stage row four cells in, the workers hanging by `↳`, no border line at all.
 const flat=part(plain(width,{worker_box:'none'})),nwhere=width+' none\n'+flat.join('\n');
 assert(/^│ │ {5}3\/7: execute +│ │$/.test(flat[0])&&flat.filter(s=>/^│ │ {5}↳ [⠋●] worker/.test(s)).length===3&&!flat.some(s=>/[┌┐└┘]|^│ │   │/.test(s)),nwhere);
}
// Where the text inside would be under 24 cells wide (here under 38 cells in all) the closed box gives way to the open one.
for(const width of [32,34,36]){const rows=plain(width,{}),box=part(rows);
 assert(/^│ │   ┌ /.test(box[0])&&!box.some(s=>/[┐┘└]/.test(s))&&box.slice(1).every(s=>/^│ │   │ /.test(s)),width+'\n'+rows.join('\n'));
 for(const s of rows)assert.equal(cells(s),width,s);}
// A route with no workers draws no box. Its stage text is on the first line, `3/7: execute` where the width has room for it (the
// name goes first short of cells, then the whole text); where the name is not on the first line the stage row `3/7: execute` stands
// under the owner (11-18, round 5), and where it is, none does.
for(const [width,first,apart] of [[40,/^│ │ ● code · 3\/7: execute +2h 10m │ │$/,false],[36,/^│ │ ● code · 3\/7: execute +2h 10m │ │$/,false],[52,/^│ │ ● code · 3\/7: execute +2h 10m │ │$/,false],[32,/^│ │ ● code · 3\/7 +2h 10m │ │$/,true]]){
 const rows=view.render({width,height:80,side:true,fill:true,model:{...head,routes:[{...route,workers_live:[]}],route_count:1},state:'working',configuration:{},questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
 const lines=rows.filter(s=>/^│ │ {5}3\/7: execute +│ │$/.test(s));
 assert(rows.some(s=>first.test(s))&&!rows.some(s=>/^│ │ +[┌│└]/.test(s))&&lines.length===(apart?1:0),width+'\n'+rows.join('\n'));}
// Several stages at once (review C2): the workers stand in the box of their own stage, each box titled with that stage's position and name.
{const parallel={...route,stages:[...seven(2).slice(0,2),{name:'eval-asr',mark:'●',current:true},{name:'eval-sep',mark:'●',current:true},...seven(2).slice(4)],
  workers_live:[worker('claude','Sonnet 5.5','22%','ASR step',{stage:'eval-asr'}),worker('opencode','glm-5.2','9%','SEP step',{stage:'eval-sep'}),worker('codex','gpt-5.5','34%','ASR two',{stage:'eval-asr'})]};
 for(const configuration of [{},{worker_box:'open'},{worker_box:'none'}]){
  const rows=view.render({width:44,height:80,side:true,fill:true,model:{...head,routes:[parallel],route_count:1},state:'working',configuration,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join('')),where=JSON.stringify(configuration)+'\n'+rows.join('\n');
  const asr=rows.findIndex(s=>s.includes('3/7: eval-asr')),sep=rows.findIndex(s=>s.includes('4/7: eval-sep'));
  assert(asr>0&&sep>asr,where);
  const between=(a,b)=>rows.slice(a,b).filter(s=>/worker/.test(s)).map(s=>s.replace(/[^\w. -]/g,' ').replace(/\s+/g,' ').trim());
  assert(between(asr,sep).length===2&&between(asr,sep)[0].includes('claude Sonnet 5.5')&&between(asr,sep)[1].includes('codex gpt-5.5'),where);
  assert.equal(rows.slice(sep).filter(s=>/worker opencode glm-5\.2/.test(s)).length,1,where);
  assert.equal(rows.filter(s=>/worker/.test(s)).length,3,where);
 }}
"""
        self.node(script, "panel-view.cjs")

    def test_an_owner_shows_no_time_and_a_worker_shows_its_own_on_its_ctx_row(self):
        # 11-18 (the person: the route's time at the right end of its first line): the route's elapsed time stands only there. An owner
        # has no time of its own, at any width, so neither its who row nor its CTX row carries one; a worker that has its own
        # (`elapsed`, `12m`) shows it after the percent on its CTX row (`CTX ━━ 22% · 12m`), a worker with none shows none.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,pct)=>({harness:P(h),model_text:P(m),ctx:P(pct),gauge_row:[P('━━'),D('────'),D(' '),P(pct)],now:'step'});
const route=workers=>({id:'w',title:'속도',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'owner',
 paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','52%')},workers_live:workers});
const timed={kind:'working',role:'worker',stage:'execute',elapsed:'12m',paint:{glyph:P('⠋'),...doer('codex','gpt-5.5','34%')}};
const untimed={kind:'working',role:'worker',stage:'execute',paint:{glyph:P('⠋'),...doer('opencode','glm-5.2','71%')}};
const plain=(routes,width,configuration={})=>view.render({width,height:80,side:true,fill:true,model:{...head,routes,route_count:routes.length},state:'working',configuration,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
for(const configuration of [{},{worker_box:'none'}])for(const width of [34,40,52,72]){
 const rows=plain([route([timed,untimed])],width,configuration),where=width+' '+JSON.stringify(configuration)+'\n'+rows.join('\n');
 const at=rows.findIndex(s=>/● owner/.test(s)),first=rows.findIndex(s=>/● code/.test(s));
 // The route's time ends its first line and stands nowhere else but the workers' own.
 assert(/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +2h 10m │ │$/.test(rows[first])&&rows.filter(s=>s.includes('2h 10m')).length===1,where);
 // The owner: its who row and its CTX row, no time.
 assert(/^│ │   ● owner /.test(rows[at])&&/^│ │ {5}CTX /.test(rows[at+1])&&!/\d+[hm]\b/.test(rows[at]+rows[at+1].replace(/^│ │ +CTX [━─ ]*\d+% /,'')),where);
 // The worker with its own time: `CTX ━━ 34% · 12m` (the gauge may go before the time in a narrow box); the one with none: the percent alone.
 const timedCtx=rows.find(s=>/CTX .*34%/.test(s)),untimedCtx=rows.find(s=>/CTX .*71%/.test(s));
 assert(/34% · 12m +(│ )?│ │$/.test(timedCtx)&&/71% +(│ )?│ │$/.test(untimedCtx)&&!/ · /.test(untimedCtx),where);
 assert.equal(rows.filter(s=>/ · \d+m/.test(s)&&/CTX /.test(s)).length,1,where);
 for(const s of rows)assert.equal(cells(s),width,where);
}
// A worker's time shows `· 12m` on its CTX row even where the percent has no gauge; an owner still has none at any width.
for(let width=32;width<=72;width++){
 const rows=plain([route([timed])],width),where=width+'\n'+rows.join('\n'),at=rows.findIndex(s=>/● owner/.test(s));
 assert(rows.some(s=>/CTX .*34% · 12m/.test(s))&&!/ · \d/.test(rows[at+1]),where);
}
"""
        self.node(script, "panel-view.cjs")


    def test_a_bottom_route_row_names_its_kind_and_has_no_command_column(self):
        # Spec E9 rule 34 (d), 11-17 (route row 5): after model and context, the side layout's and Fleet's words: the kind with
        # its intensity (`lab(quick)`, `code(standard) · 3/7: execute`), a failed `2/3: execute ✕`; short of
        # cells the kind word goes, then `staged`, then the whole column. 11-18: the command column (`>_ Edit`) is gone.
        script = self.FIXTURE + r'''
const four=[{...oneShot,kind_word:'lab',intensity:'quick'},{...staged,kind_word:'code',intensity:'standard'},waiting,failed];
const row=(rows,title)=>rows.find(s=>s.startsWith('│ │ ')&&s.includes(title));
const r140=draw(four,140,20,{side:false});
for(const [title,kind] of [['셸 상태','lab(quick)'],['세션 간','code(standard) · 3/7: execute'],['새 판으로','4/7: host-run'],['PG-50','2/3: execute ✕']]){
 const s=row(r140,title);assert(s&&s.includes('│ '+kind),title+'\n'+r140.join('\n'));
 assert(s.indexOf('claude')<s.indexOf(kind),s);
 for(const s of r140)assert.equal(cells(s),140,s);
}
assert(!r140.some(s=>/execute ✕ 2\/3/.test(s))&&!r140.some(s=>s.includes('>_')),r140.join('\n'));
// 100 cells (11-19: `staged ` is no longer written before the position, so the kind still stands here): the kind and the stage stay, no command.
const r100=draw(four,100,20,{side:false});
assert(row(r100,'세션 간').includes('│ code(standard) · 3/7: execute')&&!r100.join('').includes('>_'),r100.join('\n'));
// 90 cells: the kind word goes first (its bracket with it); the stage and its mark stay.
const r90=draw(four,90,20,{side:false});
assert(row(r90,'세션 간').includes('│ 3/7: execute')&&!r90.join('').includes('code(')&&!r90.join('').includes('lab(')&&!r90.join('').includes('>_'),r90.join('\n'));
// 80 cells: the stage and its mark stay (`staged` is never written).
const r80=draw(four,80,20,{side:false});
assert(row(r80,'세션 간').includes('│ 3/7: execute')&&row(r80,'PG-50').includes('│ 2/3: execute ✕')&&!r80.join('').includes('staged'),r80.join('\n'));
// Narrower still the whole column goes; the earlier columns stay.
const r64=draw(four,64,20,{side:false});
assert(!r64.join('').includes('lab(')&&!r64.join('').includes('3/7')&&/claude Opus 5\.5 xhigh +│ ━━─── 38%/.test(row(r64,'셸')),r64.join('\n'));
// At every width a route never reads the older `execute ✕ 2/3`, and no row carries a command.
for(let width=56;width<=140;width+=2)for(const s of draw(four,width,20,{side:false}).filter(s=>s.startsWith('│ │ ')))
 assert(!/>_|execute ✕ \d/.test(s)&&cells(s)===width,width+' '+s);
'''
        self.node(script, "panel-view.cjs")


    def test_a_solo_route_at_standard_reads_code_solo_standard_while_its_review_runs_and_while_it_is_fixed(self):
        # 11-19 (route form 3b-1, the middle cell): one owner makes the work and starts its review. The panel draws it
        # with the words it already has (the form `solo` and the intensity `standard` in the bracket, the stage line's
        # `1/2: impl-review` / `2/2: one-shot` -- the unit name on the worker line and the work order are the next piece);
        # it never writes `staged`, throws, or leaves a row empty, at the widths the panel is drawn in.
        script = self.FIXTURE + r'''
const cell=state=>({...oneShot,id:'s1',title:'공유 계약 고치기',kind_word:'code',kind_field:'code',kind_form:'solo',intensity:'standard',
 role:'owner',elapsed:'58m',shape:'one-shot-owner',owner_paint:oneShot.paint,
 stages:state==='review'?[{name:'impl-review',mark:'●',current:true,harness:'claude',model:'Opus 5.5',elapsed:'6m'},{name:'one-shot',mark:'',elapsed:'41m'}]
  :[{name:'impl-review',mark:'✓',verdict:'FAIL',harness:'claude',model:'Opus 5.5',elapsed:'9m'},{name:'one-shot',mark:'●',current:true,elapsed:'44m'}]});
for(const state of ['review','fix'])for(const width of [100,74,44,36,28]){
 const rows=draw([cell(state)],width,28,{side:width<=44}),text=rows.join('\n');
 for(const row of rows)assert.equal(cells(row),width,state+' '+width+'\n'+text);
 assert(!/staged|undefined|\bNaN\b/.test(text),state+' '+width+'\n'+text);
 if(width!==74)assert(/code\(solo(·standard)?\)/.test(text),state+' '+width+'\n'+text);   // (the bottom row drops its kind word below 90 cells, as every route's does)
 if(width===44||width===36)assert(/● code\(solo·standard\)/.test(text),state+' '+width+'\n'+text);
 if(width===28)assert(/● code\(solo\)/.test(text),state+' '+width+'\n'+text);   // narrower the intensity goes first
}
'''
        self.node(script, "panel-view.cjs")

    def test_a_solo_route_at_strong_and_thorough_reads_its_form_and_intensity_with_its_review_legs_in_boxes(self):
        # S2: `solo·strong` and `solo·thorough` draw by the rules the panel already has: the form and the intensity in the
        # bracket, the stage line by node order, and one box for each review leg that has a worker (the rule `staged` routes
        # use). Nothing new is drawn for them: never `staged`, never an empty or overlong row, at the widths the panel is drawn in.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const legWorker=(stage,h,m,e,elapsed)=>({kind:'working',role:'worker',stage,elapsed,paint:{glyph:P('⠋'),harness:P(h),model_text:P(m),effort:P(e)}});
const cell=(intensity,legs,state)=>{const names=[...legs,'one-shot'];
 return {...oneShot,id:'s1',title:'공유 계약 고치기',kind_word:'code',kind_field:'code',kind_form:'solo',intensity,role:'owner',elapsed:'52m',shape:'one-shot-owner',
  owner_paint:oneShot.paint,
  stages:names.map((name,i)=>state==='review'?{name,mark:i<legs.length?'●':'',current:i===0}:state==='fix'?{name,mark:i<legs.length?'✓':'●',current:i===legs.length}:{name,mark:''}),
  workers_live:state==='review'?legs.map((leg,i)=>legWorker(leg,i===1?'codex':'claude',i===1?'gpt-5.5':'Opus 5.5','xhigh','9m')):[]};};
const legsOf={strong:['impl-review','impl-review-alternative'],thorough:['impl-review','impl-review-alternative','impl-review-failure-mode']};
for(const intensity of ['strong','thorough'])for(const state of ['review','fix'])for(const width of [100,74,44,36,28]){
 const rows=draw([cell(intensity,legsOf[intensity],state)],width,40,{side:width<=44}),text=rows.join('\n');
 for(const row of rows)assert.equal(cells(row),width,intensity+' '+state+' '+width+'\n'+text);
 assert(!/staged|undefined|\bNaN\b/.test(text),intensity+' '+state+' '+width+'\n'+text);
 if(width!==74)assert(new RegExp('code\\(solo(·'+intensity+')?\\)').test(text),intensity+' '+state+' '+width+'\n'+text);
 if(width===44)assert(new RegExp('● code\\(solo·'+intensity+'\\)').test(text),intensity+' '+state+' '+width+'\n'+text);
 if(width===28)assert(/● code\(solo\)/.test(text),intensity+' '+state+' '+width+'\n'+text);   // narrower the intensity goes first
 if(state==='review'&&width===44){   // one box for each leg, each with its own worker
  const legs=legsOf[intensity];
  for(const [i,leg] of legs.entries())assert(text.includes((i+1)+'/'+(legs.length+1)+': '+leg),leg+'\n'+text);
  assert.equal(rows.filter(r=>r.includes('⠋ worker')).length,legs.length,text);
 }
}
"""
        self.node(script, "panel-view.cjs")

    def test_a_unit_route_leads_with_its_field_and_its_worker_line_names_the_unit(self):
        # 11-18 (the person: "Unit에서는 무슨 작업인지가 안보이잖아"): a unit route (one worker called with no owner) reads its
        # field on the first line and, in the bracket, its form alone `(unit)` (11-20; 11-19 had `(unit·quick)`, before that `code` with no
        # bracket: a unit has no intensity on screen, the payload still carries it) and the unit's short name stands in the role word's place on its worker line; a route with an owner keeps `owner`
        # and its form and intensity bracket (`one-shot·quick`). An older route that records no field keeps `unit` with no bracket.
        # The name goes whole, never cut, where the row has no room for it.
        script = self.FIXTURE + r'''
const unit={...oneShot,id:'u',title:'PR 독립 검토',kind_word:'code',kind_field:'code',kind_form:'unit',intensity:'quick',unit_route:true,unit_word:'code-review',role:'worker',elapsed:'17m',
 stages:[],paint:{...oneShot.paint,model_text:P('Opus 5.5'),ctx:P('14%'),gauge_row:[P('━───── 14%')]}};
const own={...oneShot,kind_word:'code',kind_form:'solo',intensity:'quick',role:'owner',owner_paint:oneShot.paint};
const first=(rows,re)=>rows.find(s=>s.startsWith('│ │ ')&&re.test(s));
const w44=draw([unit,own],44,60);
assert(first(w44,/● code\(unit\) +17m │ │$/),w44.join('\n'));
assert(!w44.some(s=>/quick/.test(s)&&/unit/.test(s)),w44.join('\n'));          // (no intensity on a unit's line, whatever the record holds)
assert(first(w44,/● code\(solo·quick\)/),w44.join('\n'));            // the route with an owner reads its form and intensity too
assert(w44.filter(s=>/unit/.test(s)).length===1,w44.join('\n'));          // the `(unit)` of the first line is the one `unit` word
const w32=draw([unit],32,60);
assert(first(w32,/● code\(unit\) +17m │ │$/),w32.join('\n'));
const w28=draw([unit],28,60);
assert(first(w28,/● code\(unit\) +17m │ │$/),w28.join('\n'));         // narrow: the same (nothing left to go first)
// The worker line: the unit's name where a role word stands, then harness, model, effort; the owner route keeps `owner`.
assert(w44.some(s=>/⠋ code-review claude Opus 5\.5/.test(s)),w44.join('\n'));
assert(w44.some(s=>/⠋ owner claude/.test(s)),w44.join('\n'));
assert(!w44.some(s=>/worker/.test(s)),w44.join('\n'));
// A name too long for the row goes whole, not cut: the row is the bare harness and model.
const longName={...unit,unit_word:'research-survey-with-a-very-long-name'};
const w26=draw([longName],26,60);
assert(!w26.some(s=>/research-s/.test(s))&&w26.some(s=>/⠋ .*Opus 5\.5/.test(s)),w26.join('\n'));
// An older route that records no field keeps `unit`, with no bracket (not `unit(unit)`) and no name on the first line.
const old=draw([{...unit,kind_word:'unit',kind_field:undefined,kind_form:undefined}],44,60);       // (the backend sends such a route no form)
assert(first(old,/● unit +17m │ │$/)&&old.some(s=>/⠋ code-review claude/.test(s)),old.join('\n'));
// Another field: `docs` / `research` read as themselves (only five hues, the rest the body color).
for(const field of ['docs','research','plan'])assert(first(draw([{...unit,kind_word:field,kind_field:field==='plan'?undefined:field}],44,60),new RegExp('● '+field+'\\(unit\\) +17m')),field);
// An ended route's first line reads the same; its `success · title` line is the one it had.
const ended=draw([{...unit,kind:'done',state:'done',outcome:'success'}],44,60);
assert(first(ended,/ code\(unit\) +17m/)&&ended.some(s=>/success · PR 독립 검토/.test(s)),ended.join('\n'));
// The bottom layout: the kind cell is the field, the unit's name leads the doer column.
const bottom=draw([unit],140,20,{side:false});
assert(bottom.some(s=>/code-review claude Opus 5\.5 xhigh .*│ code\(unit\) +17m/.test(s)),bottom.join('\n'));
assert(!bottom.some(s=>/unit·|quick/.test(s)&&/code-review/.test(s)),bottom.join('\n'));
// the inner row (a unit the route's worker called): its row is the one it was, no bracket
const inner=draw([{...unit,called:[{kind:'working',unit_word:'fact-verify',elapsed:'3m',title:'check',kind_word:'code',kind_field:'code',paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')}}]}],44,60);
assert(first(inner,/● code\(unit\) +17m │ │$/)&&inner.some(s=>/^│ │     ⠋ fact-verify claude Opus 5\.5 3m │ │$/.test(s)),inner.join('\n'));
const innerBottom=draw([{...unit,called:[{kind:'working',unit_word:'fact-verify',elapsed:'3m',title:'check',kind_word:'code',kind_field:'code',paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')}}]}],100,30,{side:false});
assert(innerBottom.some(s=>/fact-verify claude Opus 5\.5 xhigh +│ +│ code +3m │ │$/.test(s)),innerBottom.join('\n'));
'''
        self.node(script, "panel-view.cjs")


    def test_the_bracket_holds_the_form_and_the_intensity_and_the_whole_bracket_follows_the_intensitys_color_and_weight(self):
        # 11-20: a `unit` route's bracket is its form alone, `code(unit)`, in the field's plain weight (a form-only bracket's: not dim, which reads as
        # `quick`, not bold); the form-and-intensity brackets of `solo`, `team`, a frame and an older record, and the exact lines below, are as they were.
        # 11-19 (the person: "unit, one-shot, staged을 괄호로 넣어야하나 싶은데" / "unit도 어떤 모델을 부르냐에 따라서 강도는 정해질 수 있는 거잖아" /
        # "강도도 표기해 항상"): the bracket of a route in the dispatch zone holds the backend's form word (`kind_form`) and its intensity after a `·`,
        # always (`code(team·standard)`); a route with no intensity reads the form alone; one with no form (a frame, an older record) its
        # intensity alone; an older unit route its bare `unit`; a called unit's row has none. The whole bracket wears the intensity's color and
        # weight (a dim one where the route has no field hue), also where the tail is folded away. The stage line does not lead with `staged` again.
        script = self.FIXTURE + r'''
const styles={dim:{dim:true},kind_code:{foreground:69},kindq_code:{foreground:69,dim:true},kinds_code:{foreground:69},kindb_code:{foreground:69,bold:true},
 kind_lab:{foreground:214},kindq_lab:{foreground:214,dim:true},kinds_lab:{foreground:214},kindb_lab:{foreground:214,bold:true}};
const rendered=(routes,width,side=true,extra={})=>view.render({width,height:60,side,fill:side,colors:256,model:{...head,styles,routes,route_count:routes.length,...extra},state:'working',
 configuration:{},expandedKeys:[],questions:()=>[]}).rows;
const texts=rows=>rows.map(r=>r.map(p=>p.text).join(''));
const find=(rows,t)=>rows.flat().filter(p=>p.text===t);
const own0={...oneShot,role:'owner',owner_paint:oneShot.paint};
const unit={...own0,id:'u',kind_word:'code',kind_field:'code',kind_form:'unit',intensity:'quick',unit_route:true,unit_word:'code-review',role:'worker',elapsed:'17m',stages:[]};
const quick={...own0,id:'q',kind_word:'code',kind_field:'code',kind_form:'solo',intensity:'quick'};
const std={...staged,id:'s',kind_word:'code',kind_field:'code',kind_form:'team',intensity:'standard',role:'owner',chain:[{name:'frame',state:'done'},{name:'code',state:'active'}]};
const strong={...staged,id:'l',kind_word:'lab',kind_field:'lab',kind_form:'team',intensity:'strong',role:'owner'};
const none={...staged,id:'n',kind_word:'code',kind_field:'code',kind_form:'team',intensity:'',role:'owner'};
const old={...unit,kind_word:'unit',kind_field:undefined,kind_form:undefined,id:'o'};
const all=[unit,quick,std,old,strong,none];
for(const [width,side] of [[44,true],[36,true],[28,true],[160,false],[130,false]]){
  const rows=rendered(all,width,side),text=texts(rows),tag=width+(side?' side':' bottom')+'\n'+text.join('\n');
  const has=re=>text.some(s=>re.test(s));
  if(width>=36||!side){
    assert(has(/code\(unit\)/)&&has(/code\(solo·quick\)/)&&has(/lab\(team·strong\)/)&&has(/code\(team\)/),tag);
    assert(side||has(/code\(team·standard\)/),tag);              // (beside the native area the chain and the stage take the room: the tail folds)
    assert(!has(/staged/)&&has(/ \d\/\d: /),tag);                 // (the stage line never leads with `staged`)
    assert(!has(/unit\(/)&&!has(/\(quick\)|\(standard\)|\(strong\)|unit·/),tag);
  }
  assert(has(/(● |│ )unit(?![(·])/),tag);                                  // (the older unit route: bare `unit`)
  // color: the whole bracket follows the intensity (quick dim, standard plain, strong bold) in the field's hue
  const key=(t,fg,dim,bold)=>find(rows,t).forEach(p=>assert(p.foreground===fg&&!!p.dim===dim&&!!p.bold===bold&&p.role==='kind',tag+JSON.stringify(p)));
  if(width>=36||!side){key('(unit)',69,false,false);key('(solo·quick)',69,true,false);if(!side)key('(team·standard)',69,false,false);key('(team·strong)',214,false,true);}
}
// the tail folds first, the colour stays that of the intensity; the form goes after the chain
const row28=texts(rendered([std],28)).find(s=>/● frame|● code/.test(s));
assert(/● code\(team\)/.test(row28)||/● frame ✓ → code\(team\)/.test(row28),row28);
for(const [routes,word,fg,dim,bold] of [[[unit],'(unit)',69,false,false],[[strong],'(team)',214,false,true]]){
  const got=[22,24,26,28,30,32,34,36].flatMap(w=>find(rendered(routes,w),word));
  assert(got.length&&got.every(p=>p.foreground===fg&&!!p.dim===dim&&!!p.bold===bold),word+JSON.stringify(got));}
// as the width narrows the bracket only ever loses its tail, then itself (never the other way round)
const stateOf=s=>/code\(team·standard\)/.test(s)?0:/code\(team\)/.test(s)?1:/● code/.test(s)||/→ code/.test(s)?2:3;
let last=0;
for(let w=60;w>=20;w--){const line=texts(rendered([std],w)).find(s=>/● (frame|code)/.test(s));if(!line)continue;const st=stateOf(line);assert(st>=last,w+' '+line);last=st;}
assert(last>=1);
// beneath the native area the kind column folds the intensity first, then the bracket (`code · 3/6: execute`), then the kind; `staged` is never written
const kindState=line=>/code\(team·standard\) · \d\/\d: /.test(line)?0:/code\(team\) · \d\/\d: /.test(line)?1:/code · \d\/\d: /.test(line)?2:/ \d\/\d: /.test(line)?3:-1;
const seen=[];
for(let w=170;w>=60;w--){const line=texts(rendered([{...std,chain:undefined},quick],w,false)).find(s=>/\d\/\d: /.test(s));if(line)seen.push(kindState(line));}
assert(seen.every(v=>v>=0)&&seen.every((v,i)=>i===0||v>=seen[i-1])&&seen.includes(0)&&seen.includes(1),JSON.stringify(seen));
// mono / NO_COLOR / under 256 colors: dim brackets, the same text
for(const extra of [{no_color:true},{colors:16}]){
  const rows=rendered([unit,std],44,true,extra);
  assert(texts(rows).some(s=>/code\(unit\)/.test(s)),JSON.stringify(extra));
  assert(find(rows,'(unit)').every(p=>p.dim===true&&!p.foreground),JSON.stringify(extra));
  assert(find(rows,'(team·standard)').every(p=>!p.foreground),JSON.stringify(extra));
}
// 11-20: every other form's first line is exactly what it was (the unit is the only route whose bracket lost its intensity), at every width
for(const [width,side,want] of [[44,true,[[quick,/^│ │ ● code\(solo·quick\) +41m │ │$/],[std,/^│ │ ● frame ✓ → code\(team\) · \d\/\d +1h 0m │ │$/]]],[28,true,[[quick,/^│ │ ● code\(solo\) +41m │ │$/],[strong,/^│ │ ● lab\(team\) +1h 0m │ │$/]]],
  [160,false,[[quick,/│ code\(solo·quick\) +41m │ │$/],[strong,/│ lab\(team·strong\) · \d\/\d: \w+ +1h 0m │ │$/]]]]){
  const text=texts(rendered([quick,std,strong,none],width,side));
  for(const [route,re] of want)assert(text.some(s=>re.test(s)),width+' '+re+'\n'+text.join('\n'));
}
const frameRow={...own0,id:'f',kind_word:'frame',kind_field:undefined,kind_form:undefined,intensity:'standard',shape:'framed',role:'owner'};
const directRow={...own0,id:'d',kind_word:'code',kind_field:'code',kind_form:undefined,intensity:'direct',shape:'direct',zone:'direct',role:'owner'};
{const text=texts(rendered([frameRow,directRow],44));
 assert(text.some(s=>/● frame\(standard\)/.test(s))&&text.some(s=>/● code +\d+m/.test(s)&&!/\(/.test(s)),text.join('\n'));}
// a unit a worker called (under the route): no bracket on its row
const caller={...staged,id:'c',kind_word:'code',kind_field:'code',kind_form:'team',intensity:'standard',role:'owner',workers_live:[{kind:'working',role:'worker',stage:'execute',elapsed:'12m',
 paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Sonnet 5.5')},called:[{kind:'working',unit_word:'code-review',elapsed:'3m',title:'review',kind_word:'code',kind_field:'code',
 paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')}}]}]};
const under=texts(rendered([caller],130,false));
assert(under.some(s=>/code-review/.test(s))&&!under.some(s=>/code-review.*\((unit|quick|one-shot)/.test(s)),under.join('\n'));
// a `direct` route has no form; a route with no form keeps its intensity bracket
const direct=texts(rendered([{...own0,kind_word:'code',kind_field:'code',kind_form:'solo',intensity:'quick',zone:'direct',shape:'direct'}],44));
assert(!direct.some(s=>/one-shot/.test(s)),direct.join('\n'));
const plain=texts(rendered([{...own0,kind_word:'code',kind_field:'code',intensity:'standard'}],44));
assert(plain.some(s=>/● code\(standard\)/.test(s)),plain.join('\n'));
'''
        self.node(script, "panel-view.cjs")


    def test_a_unit_routes_narrow_worker_line_gives_up_its_values_and_keeps_its_name_last(self):
        # 11-19 (independent review of #1736): the effort goes first, then the harness, then the model, and the unit's name is the last
        # word on the line; a narrower line never shows a word a wider one lacks. Only a name with no room goes (whole), and the line is
        # then drawn as one with no name is. Under the route, a unit route among other routes keeps every row's columns at the same cells and costs no other route a word.
        script = self.FIXTURE + r'''
const unit={...oneShot,id:'u',title:'PR 독립 검토',kind_word:'code',kind_field:'code',unit_route:true,unit_word:'code-review',role:'worker',elapsed:'17m',
 stages:[],paint:{...oneShot.paint,model_text:P('Opus 5.5'),ctx:P('14%'),gauge_row:[P('━───── 14%')]}};
const words=rows=>{const row=rows.find(s=>/[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]/.test(s)&&s.includes('code-review'));return row?row.replace(/[│╭╮╰╯─]/g,' ').trim().split(/\s+/).filter(w=>!/^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●]$/.test(w)):null;};
for(const rows of [60,22]){
  let wider=null;
  for(let width=60;width>=23;width--){   // (below 23 even the name has no room: it goes whole, see the long name below)
    const got=words(draw([unit],width,rows));
    assert(got&&got.includes('code-review'),'name stays at '+width+'x'+rows+': '+JSON.stringify(got));
    if(wider)for(const w of got)assert(wider.includes(w),'width '+width+'x'+rows+' shows `'+w+'` that '+(width+1)+' lacks: '+JSON.stringify(got)+' / '+JSON.stringify(wider));
    wider=got;
  }
}
// The order the values go in, at the widths where each form stands.
const seen=new Set();
for(let width=60;width>=23;width--){const got=words(draw([unit],width,60));seen.add(got.filter(w=>w!=='code-review').join(' '));}
for(const form of ['claude Opus 5.5 xhigh','claude Opus 5.5','Opus 5.5',''])assert(seen.has(form),'a form is missing: `'+form+'`: '+[...seen].join(' | '));
// A name with no room goes whole, and the line is then the bare harness and model as before.
const longName={...unit,unit_word:'research-survey-with-a-very-long-name'};
const narrow=draw([longName],26,60);
assert(!narrow.some(s=>/research-s/.test(s))&&narrow.some(s=>/⠋ .*Opus 5\.5/.test(s)),narrow.join('\n'));
// Under the route (`side:false`), a unit route among other routes: its doer cell never borrows from its own title, so every route row
// of the box has its columns at the same cells; the cell folds like the narrow worker line (effort, harness, model) with the name
// last, and stands whole where the box has room to spare; no other route's row loses a word or a letter of its title for it.
const own={...oneShot,id:'o',title:'Fix the governor cap',kind_word:'code',intensity:'quick',role:'owner',owner_paint:oneShot.paint};
const stagedRoute={...staged,kind_word:'code',intensity:'standard'};
const rowsOf=(routes,width)=>draw(routes,width,20,{side:false}).filter(s=>/^│ │ [⠋●✓] /.test(s));
const barsOf=row=>{const out=[];let at=0;for(const c of row){if(c==='│')out.push(at);at+=cells(c);}return out.slice(2);};   // cells, not characters (Korean titles)
const wordsOf=row=>row.replace(/[│╭╮╰╯─]/g,' ').trim().split(/\s+/);
const doerOf=row=>row.split('│')[3].trim();
const noName={...unit,unit_word:''};   // the same route as the main branch draws it: no name in front of its doer cell
for(const [label,mixed,rest] of [['owner+unit',[own,unit],[own]],['owner+unit+staged',[own,unit,stagedRoute],[own,stagedRoute]]]){
  let wider=null;
  for(let width=200;width>=40;width--){
    const rows=rowsOf(mixed,width),without=rowsOf(rest,width),plain=rowsOf(mixed.map(r=>r===unit?noName:r),width);
    assert.equal(rows.length,mixed.length,label+' '+width);
    // 1. one set of columns: the title│doer and the doer│gauge bars stand at the same cells on every route row.
    // (Widths 48 and 49 of the box with a staged route are left out: the staged row's own doer cell is clipped there by the row's
    // end, on main as well, before this change; it is not a unit route's doing.)
    if(!(rest.length>1&&width>=48&&width<=49))for(const k of [0,1])assert.equal(new Set(rows.map(row=>barsOf(row)[k])).size,1,label+' bar '+k+' at '+width+'\n'+rows.join('\n'));
    // 2. the unit route costs the other routes nothing: their words and their title letters are what they are without it.
    const others=mixed.map((r,i)=>r===unit?null:rows[i]).filter(Boolean);
    others.forEach((row,i)=>assert.deepEqual(wordsOf(row),wordsOf(without[i]),label+' '+width+'\n'+rows.join('\n')+'\n--\n'+without.join('\n')));
    // 3. the unit route's title is whole wherever the title is whole with no name in front of its doer cell (the main branch's row).
    const at=mixed.indexOf(unit);
    if(plain[at].includes('PR 독립 검토'))assert(rows[at].includes('PR 독립 검토'),label+' title cut at '+width+'\n'+rows.join('\n'));
    // 4. the name shows at every width where the column holds it (the column is 21 cells wide in these boxes; below 49 cells the doer
    // column is no longer drawn), and a narrower row never shows a word a wider one lacks.
    if(width>=49){
      const cell=doerOf(rows[at]),got=cell.split(/\s+/);
      assert(got.includes('code-review'),label+' name missing at '+width+'\n'+rows.join('\n'));
      if(wider)for(const w of got)assert(wider.includes(w),label+' width '+width+' shows `'+w+'` that '+(width+1)+' lacks: `'+cell+'`');
      wider=got;
    }
  }
  // 5. the whole cell (`code-review claude Opus 5.5 xhigh`) shows from some width on and at every width above it, 200 included.
  const full=[];for(let width=49;width<=200;width++)full.push(doerOf(rowsOf(mixed,width)[mixed.indexOf(unit)])==='code-review claude Opus 5.5 xhigh');
  const first=full.indexOf(true);
  assert(first>=0&&first+49<=140&&full.slice(first).every(Boolean),label+' whole cell from '+(first+49)+': '+full.map(x=>x?1:0).join(''));
  assert.equal(doerOf(rowsOf(mixed,60)[mixed.indexOf(unit)]),'code-review Opus 5.5','shared column of 21 cells: the effort and the harness go, the name stays');
}
// 11-19 (follow-up to the #1736 close check): a box whose titles are all short, with a staged route whose stage cell has two forms (the kind
// with its stage word, then the stage word alone). The whole unit cell shows from some width on and at every width above it, 200 included,
// whether the unit cell is the widest of the box (`code-review claude Opus 5.5 xhigh`) or a shorter one (`fact-verify codex gpt-5.5 high`);
// the two bars stand at the same cells on every route row all the way.
const shortOwn={...own,title:'Fix cap'},shortStaged={...stagedRoute,title:'단계 1'},shortUnit={...unit,title:'PR 검토'};
const factUnit={...shortUnit,unit_word:'fact-verify',paint:{...unit.paint,harness:P('codex'),model_text:P('gpt-5.5'),effort:P('high')}};
for(const [label,u,whole] of [['short titles',shortUnit,'code-review claude Opus 5.5 xhigh'],['short titles, a shorter unit cell',factUnit,'fact-verify codex gpt-5.5 high']]){
  const box=[shortOwn,u,shortStaged],shown=[];
  for(let width=60;width<=200;width++){
    const rows=rowsOf(box,width);
    assert.equal(rows.length,3,label+' '+width);
    for(const k of [0,1])assert.equal(new Set(rows.map(row=>barsOf(row)[k])).size,1,label+' bar '+k+' at '+width+'\n'+rows.join('\n'));
    shown.push(doerOf(rows[1])===whole);
  }
  const first=shown.indexOf(true);
  assert(first>=0&&first+60<=115&&shown.slice(first).every(Boolean),label+' whole cell from '+(first+60)+': '+shown.map(x=>x?1:0).join(''));
}
// A unit route that records no harness: the doer column is as wide as the cell drawn (one blank between the name and the model, none left
// over after the cell), alone in its box and beside another route.
const noHarness={...unit,paint:{...unit.paint,harness:undefined}};
for(const [label,box,at] of [['alone',[noHarness],0],['beside an owner',[own,noHarness],1]])
  for(const width of [120,140,200]){
    const rows=rowsOf(box,width);
    assert.equal(rows[at].split('│')[3],' code-review Opus 5.5 xhigh ',label+' '+width+'\n'+rows.join('\n'));
    assert.equal(new Set(rows.map(row=>barsOf(row)[1])).size,1,label+' bars at '+width+'\n'+rows.join('\n'));
  }
'''
        self.node(script, "panel-view.cjs")


    def test_owners_and_their_workers_fold_together_and_every_route_takes_the_same_step(self):
        # Spec E9 rule 48 (c) (it replaces the #1579 review follow-up (가)), 11-18: short of rows the steps of the route box are, in
        # order: the worker box's closing border, every session's second NOW row, then in one step every session's CTX row folds into
        # its who row (the old one-row form: the percent, and a worker's time, at the right edge) the workers taking the one-row form in
        # their box too, then every first NOW row, then the workers go and the stage row counts them (`· worker 2`), the stage row
        # goes, the owner's row stands alone, the routes take one row. Never an owner in its whole form with a worker folded, or the
        # reverse; every route at the same step. (A row given back goes to the most important item, so the box may close again once
        # the NOW rows are gone: the border is not a step of its own in the order.) The same holds for `worker_box: none`. The
        # reviewer's case: 38 cells, one staged route with a worker and three without.
        script = self.FIXTURE + r"""
const D=t=>({text:t,dim:true});
const doer=(h,m,e,pct,bar,now)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)],...(now?{now}:{})});
const solo={id:'s',title:'셸 상태·표시 고치기',kind:'working',state:'working',elapsed:'41m',shape:'one-shot-owner',kind_word:'code',role:'owner',tool:'Edit',
 stages:[{name:'one-shot',current:true,mark:'●'}],paint:{glyph:P('⠋'),...doer('claude','Opus 5.5','xhigh','38%','━━━','solo step')},workers_live:[]};
const worker=(h,m,pct,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',tool:'Edit',...o,paint:{glyph:P('⠋'),...doer(h,m,'high',pct,'━━','worker step')}});
const one={id:'w',title:'속도 3단계 — 남은 재계산',kind:'working',state:'working',elapsed:'2h 10m',shape:'staged',stages:seven(2),kind_word:'code',role:'worker',doer_worker:true,
 paint:{glyph:P('⠋'),...doer('claude','Sonnet 5.5','high','22%','━━','owner step')},owner_paint:doer('claude','Opus 5.5','xhigh','52%','━━━━','owner step'),workers_live:[worker('claude','Sonnet 5.5','22%')]};
const two={...one,id:'t',title:'검토 두 갈래',workers_live:[worker('codex','gpt-5.5','34%'),worker('opencode','glm-5.2','71%',{elapsed:'8m'})]};
const reviewer=[one,...[1,2,3].map(i=>({...solo,id:'s'+i}))];
const order=['whole','fold','nonow','counted','stage','owner','flat'];let checked=0;
for(const configuration of [{},{worker_box:'none'}])
for(const [width,routes,model] of [[38,reviewer,{}],[38,reviewer,{messages:[],wait:'',now:''}],[40,reviewer,{}],[40,[two,solo],{}],[34,[one,two],{}],[32,[one],{}]]){
 const seen=[],boxes=new Set();
 for(let h=90;h>=12;h--){
  const rows=view.render({width,height:h,side:true,fill:true,model:{...head,...model,routes,route_count:routes.length},state:'working',configuration,questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
  // (11-18: the owner's lines stand two cells in, its CTX row and NOW four; a worker stands in the stage box two cells in or, with
  // `worker_box: none`, hangs by `↳` four cells in, its own rows eight.) The steps: the whole form (the CTX rows and the NOW rows), the
  // CTX rows folded into the who rows, the NOW rows gone, the workers gone with the stage line counting them, the stage line
  // alone, the owner's row alone, then the routes in one row.
  const all=rows.join('\n'),count=re=>rows.filter(s=>re.test(s)).length,none=!!configuration.worker_box;
  const ctx=count(/^│ │ +(│ +)?CTX /),nows=count(/^│ │ +(│ +)?(owner|solo|worker) step/),owners=count(/^│ │   [⠋●] owner /),ownerPct=count(/^│ │   [⠋●] owner .*\d+%/);
  const workers=count(none?/^│ │ {5}↳ [⠋●] worker/:/^│ │   │ [⠋●] worker/),stageRows=count(/^│ │ {5}(staged )?\d+\/\d+: /)+count(/^│ │   ┌ (staged )?\d+\/\d+: /);
  const counted=/· worker \d/.test(all),sessions=owners+workers;
  const state=nows&&ctx?'whole':nows?'fold':workers?'nonow':counted?'counted':stageRows?'stage':owners?'owner':'flat';
  const where=(none?'none ':'box ')+width+'x'+h+'\n'+all;
  // Never a middle form: the whole form only with every session's CTX row and NOW row, the folded one only with every owner's percent at the
  // right and no CTX row, and so on.
  if(state==='whole')assert(ctx===sessions&&nows===sessions&&!counted&&!ownerPct,where);
  // (Where the owners' rows are narrow the percent column goes for the workers' time column, so the owners' percent is only held from 38 cells.)
  const pct=width>=38?ownerPct===owners:true;
  if(state==='fold')assert(ctx===0&&nows===sessions&&pct&&!counted,where);
  if(state==='nonow')assert(ctx===0&&nows===0&&pct&&!counted,where);
  if(state==='counted')assert(ctx===0&&nows===0&&workers===0&&pct,where);
  // The one-row worker in the `none` form: four cells in, its percent in the owners' column (in the box the columns are the box's own, four cells in).
  if(none)for(const s of rows.filter(s=>/│ [⠋●] worker .*\d+%/.test(s)||/^│ │ {5}↳ [⠋●] worker .*\d+%/.test(s))){const owner=rows.find(r=>/^│ │   [⠋●] owner .*%/.test(r));if(owner)assert.equal(s.lastIndexOf('%'),owner.lastIndexOf('%'),where);}
  // The box (the default form): closed, it has its bottom line and every right `│` stands in one column; open, only its top line and its
  // left side; with `none`, no border line at all.
  const tops=rows.filter(s=>/^│ │   ┌/.test(s)),inside=rows.filter(s=>/^│ │   [│└]/.test(s));
  if(none)assert(!tops.length&&!inside.length,where);
  else if(tops.length){
   const closed=tops.every(s=>/┐ │ │$/.test(s));boxes.add(closed?'closed':'open');
   if(closed)assert(inside.every(s=>/[│┘] │ │$/.test(s))&&inside.filter(s=>s.startsWith('│ │   └')).length===tops.length,where);
   else assert(tops.every(s=>!/┐/.test(s))&&!inside.some(s=>/^│ │   └/.test(s)),where);
  }
  if(!seen.includes(state))seen.push(state);checked++;
 }
 assert.deepEqual(seen,order.filter(x=>seen.includes(x)),width+' '+JSON.stringify(seen));
 assert(seen.includes('whole')&&seen.includes('fold'),width+' '+JSON.stringify(seen));
 if(!configuration.worker_box&&width>=38)assert(boxes.has('closed')&&boxes.has('open'),width+' '+[...boxes]);
}
assert(checked>0);
"""
        self.node(script, "panel-view.cjs")


    def test_the_beat_follows_the_painted_route_rows(self):
        # Restored 11-15 from v4.36.4 (`test_parallel_stages_never_take_each_others_tool_and_the_beat_follows_
        # the_painted_rows`, its opened-detail half gone with in-place opening): the beat runs only while a
        # spinning or blinking part is painted.
        script = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const p=t=>({text:t});
const route={id:'a',title:'ROUTE_A',kind:'working',tool:'apply_patch',agents:1,kind_text:'code(dev·std)',
 stages:[{name:'execute',mark:'●',current:true,harness:'codex',model:'gpt-fixture',effort:'high'},
  {name:'review',mark:'●',current:true,harness:'claude',model:'claude-sonnet-5-5',effort:'med'}],
 workers:[{stage:'execute',harness:'codex',model:'gpt-fixture',ctx:'72%'},{stage:'review',harness:'claude',model:'Sonnet 5.5',ctx:'12%'}],
 paint:{glyph:p('⠏')}};
const rows=v=>v.rows.map(r=>r.map(s=>s.text).join(''));
// A working route the height (a question) dropped from the panel: nothing turns.
const q={count:1,label:'QUESTIONS',rows:[[p('◑ '),p('Q_HEAD')],[p('  '),p('1 YES · 2 NO')]],one:[[p('◑ '),p('Q_HEAD')]],line:[p('Q_HEAD')],choices:2};
// (11-15: the waiting question's `◑` blinks on the breath's beat: its cell is the only moving one.)
for(const [width,height,side] of [[40,8,true],[80,6,false]]){
 const r=view.render({width,height,side,state:'idle',model:{routes:[route],route_count:1},brief:()=>q});
 assert(!rows(r).join('\n').includes(side?'1 ROUTE':'route 1'),rows(r).join('\n'));
 assert.deepEqual(r.spins,[]);assert.deepEqual(r.motion.cells.map(c=>c.ch),['◑'],width+'x'+height);
 const off=view.render({width,height,side,state:'idle',model:{routes:[route],route_count:1},brief:()=>q,configuration:{rule_motion:'off'}});
 assert.equal(off.animated,false,width+'x'+height);
}
// The same route with room to show it: its mark spins and the beat runs.
const shown=view.render({width:40,height:30,side:true,state:'idle',model:{routes:[route],route_count:1},brief:()=>q});
assert(rows(shown).join('\n').includes('1 ROUTE'));assert.equal(shown.animated,true);assert(shown.spins.length>0);
"""
        self.node(script, "panel-view.cjs")

    def test_a_route_folded_to_one_row_keeps_each_value_whole(self):
        # Review RV-PR1528 §2: 32 cells, the boxed one-row route cut `72%` to `7…` (11-15: the box again).
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
 for(const hit of v.hits.filter(h=>h.action?.startsWith('popup:ROUTE:'))){
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
 const branch=rows.findIndex(s=>s.includes('(values-'));
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
        # Spec E9 rule 47 (e), 11-15 (panel polish 4): `rule motion` (`rule` by default, `border`,
        # `scanners` or `off`), saved like `route line`; an unknown value, and the `on` saved before
        # 11-15, read as `rule` alone (the file is not rewritten for it) and never disable the shell.
        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": directory}
            path = shell.policy_path(env)
            path.parent.mkdir()
            path.write_text(json.dumps({"enabled": True, "rule_motion": "sometimes", "route_line": "model"}))
            path.chmod(0o600)
            self.assertTrue(shell.policy(env)[0])
            self.assertEqual((shell.display_policy(env)["rule_motion"], shell.display_policy(env)["route_line"]), ("rule", "model"))
            old = json.dumps({"enabled": True, "rule_motion": "on"})
            path.write_text(old)
            self.assertEqual(shell.display_policy(env)["rule_motion"], "rule")
            self.assertEqual(path.read_text(), old)
            path.write_text(json.dumps({"enabled": True}))
            self.assertEqual(shell.DISPLAY_DEFAULTS["rule_motion"], "rule")
            self.assertEqual(shell.CHOICES["rule_motion"], ("rule", "border", "scanners", "off"))
            for value in ("border", "scanners", "off"):
                desired = {**shell.DISPLAY_DEFAULTS, "rule_motion": value}
                self.assertEqual(shell.save_display_policy(desired, env)["rule_motion"], value)
                self.assertEqual(shell.display_policy(env)["rule_motion"], value)
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
        # Spec E9 rule 34 (a), 36 (11-15: the route box again, nothing opens in place; 11-18 route row 5: the
        # title, the who row two cells in, its CTX row and the stage line four in): a folded route keeps each value whole (a
        # model name with its version) and lets the lesser pieces go first (the effort, the harness); TITLE and NOW
        # fold only between tokens.
        script = self.FIXTURE + r"""
const rows=draw([oneShot,staged,failed],40,76),all=rows.join('\n');
for(const s of rows)assert.equal(cells(s),40,s);
// (11-17: the open tool is not on the owner's row; 11-18: nor its context, which is the row under it: `CTX`, the gauge and the percent.)
for(const want of ['│ │   ⠋ claude Opus 5.5 xhigh ','│ │     CTX ━━─── 38% ','│ │   ⠋ claude Sonnet 5.5 high ','│ │     CTX ━──── 12% ','│ │     3/7: execute ','│ │     2/3: execute ✕ '])
 assert(all.includes(want),want+'\n'+all);
assert(!/· Edit|>_/.test(all),'no tool on a side row\n'+all);
// An owner shows no time wherever the row has room: the route's own stands at the right end of its first line (a route with a kind word).
const roomy=draw([{...oneShot,kind_word:'code'},staged,failed],52,76);
assert(roomy.some(s=>/^│ │ ● code(?: · \d+\/\d+(?:: [\w-]+)?)? +41m │ │$/.test(s))&&roomy.some(s=>s.includes('│ │   ⠋ claude Opus 5.5 xhigh '))&&roomy.some(s=>/^│ │     CTX ━━─── 38% +│ │$/.test(s))&&!roomy.some(s=>/· \d+m/.test(s)),'the owner takes no time where the row has room\n'+roomy.join('\n'));
// No model name, version or path is cut apart, and nothing opens in place under a route.
for(const s of rows)assert(!/\((Opus|Sonnet) *│|│ +5\.5|utilities\/sessio( |$)|sessio *│/.test(s),s+'\n'+all);
for(const gone of ['OWNER','DOER','KIND','┆','impl-review','lines ›'])assert(!all.includes(gone),gone+'\n'+all);
// Narrower, the effort goes, then the harness; the model with its version stays whole.
for(const width of [28,32,36]){
 const narrow=draw([oneShot,staged,failed],width,76);
 for(const s of narrow){assert.equal(cells(s),width,s);assert(!/│ +5\.5|(Opus|Sonnet) *│|Sonne…|Opu…/.test(s),s+'\n'+narrow.join('\n'));}
 assert(narrow.some(s=>/│ │   ⠋ (claude )?Sonnet 5\.5/.test(s)),narrow.join('\n'));
}
// The same function breaks TITLE and NOW at word boundaries, as before (11-17: the NOW's last row ends in `›`, the mark of a row a press opens; 11-22: at the row's right end,
// and the rows stand one cell in, so a row takes one cell less).
const title=rows.findIndex(s=>s.includes('TITLE'));
assert.deepEqual(rows.slice(title,title+6).map(s=>s.slice(2,-1).trimEnd()),[' TITLE  하팅 v4-open 감독 — 셸','        상태·표시 고치기와 Fleet',
 '        세션 줄 다듬기',' NOW    검토 결과 3건을 기다리는','        중이며 CI 결과를 확인하고','        있음                       ›'],all);
// With room the whole values stand on one row each.
const wide=draw([oneShot,staged,failed],140,76,{side:false}).join('\n');
for(const want of ['PG-50 휠 입력','2/3: execute ✕','54m','3/7: execute','claude Opus 5.5 xhigh'])assert(wide.includes(want),want+'\n'+wide);
assert(!wide.includes('>_'),'no command column\n'+wide);   // 11-18
"""
        self.node(script, "panel-view.cjs")

    def test_route_rows_outlast_message_bodies_and_the_rule_outlasts_empty_rows(self):
        # Spec E9 rule 34 (b), (c), 11-15 (the route box again): empty rows, then a value's extra rows,
        # then the structural rows go before the route box gives up anything; 11-16: then every message
        # folds from its two rows (head, body) to one (11-20: before the rule under the metadata goes); then the route box's NOW rows go (11-17: the owner rows take the one-row form), its stage
        # rows go, its owner rows go (11-17: the title row stands alone), and only then it scrolls in place. The rule under the metadata
        # stays while TITLE or NOW takes more than one row; the messages fold all together.
        script = self.FIXTURE + r"""
// (11-17: the first two routes of each three have a NOW, the third none, so its row is drawn only where a session has one.)
const noted=[oneShot,staged,waiting].map(r=>r===waiting?r:{...r,paint:{...r.paint,now:'now step'}});
const nine=[...Array(9)].map((_,i)=>{const r=noted[i%3];return {...r,id:'n'+i,title:r.title+' '+(i+1)};});
// (11-17, route row 5: a route's title stands first, the owner's row two cells in, its NOW and stage row four; the owner's
// rows go before the one-row form, whose title row it shares.)
const place=rows=>rows.filter(s=>/^│ │ {5}(staged )?\d+\/\d+: /.test(s)).length,names=rows=>rows.filter(s=>/^│ │   [⠋●✕] /.test(s)).length,titles=rows=>rows.filter(s=>/^│ │ [⠋●✕] (셸|세션|새 판)/.test(s)).length;
const ctx=rows=>rows.filter(s=>/^│ │ {5}now step/.test(s)).length,shown=rows=>rows.filter(s=>/^│ │ [⠋●✕] /.test(s)).length;
const section=rows=>{const m=rows.findIndex(s=>s.startsWith('│ MESSAGES'));return m<0?[]:rows.slice(m+1).filter(s=>/^│  [←→] /.test(s));};
// 40×60: every route in its whole form (title, who with its context, NOW, stage; the one-shot route has no stage row, the third no NOW) and the three messages (11-20; five before), two rows each.
const c60=draw(nine,40,60);
assert(place(c60)===6&&titles(c60)===9&&names(c60)===9&&ctx(c60)===6&&section(c60).length===3&&c60.filter(s=>/^│   \S/.test(s)).length===3,c60.join('\n'));   // 11-20: three messages at most
// The first height (from 72 down) each step is taken.
const first={};
for(let h=72;h>=16;h--){
 const rows=draw(nine,40,h),all=rows.join('\n'),title=rows.findIndex(s=>s.startsWith('│  TITLE'));
 const f={msg:section(rows).length>0&&!rows.some(s=>/^│   \S/.test(s)),rule:!rows.some(s=>/^│ SESSION ─+ │$/.test(s)),ctx:ctx(rows)<6,place:place(rows)<6,names:names(rows)<9,scroll:shown(rows)<9};
 for(const [k,v] of Object.entries(f))if(v&&first[k]===undefined)first[k]=h;
 // The messages fold all together: a body row stands under every head or under none.
 {const bodies=rows.filter(s=>/^│   \S/.test(s)).length;assert(!bodies||bodies===section(rows).length,h+'\n'+all);}
 // (11-22: the rule under the metadata is gone; the SESSION head's own row, which then takes MODEL's value and row, goes only once TITLE and NOW are one row each.)
 if(title>0&&/^│ {9}\S/.test(rows[title+1]))assert(rows.some(s=>/^│ SESSION ─+ │$/.test(s)),h+'\n'+all);
 // Each route in the box takes the same form; the box never shows more than its rows.
 assert(rows.length<=h&&rows.filter(s=>s.includes('╭─ 9 ROUTE')).length===1,h+'\n'+all);
}
// (11-20: the messages fold first, with MEM's rows, before the rule under the metadata goes.)
assert(first.msg>=first.rule&&first.rule>=first.ctx&&first.ctx>first.place&&first.place>first.names&&first.names>first.scroll,JSON.stringify(first));
// Empty rows go before the rule does: with room to spare the rule stands. (11-16: the QUESTIONS box takes two rows
// where its head row took one, so the height this stood at, 60, is 61 now; 62 leaves the same one spare row.)
const tall=draw(nine,40,62);
assert(tall.some(s=>/^│ SESSION ─+ │$/.test(s)),tall.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_the_steward_number_holds_and_two_structural_empty_rows_part_the_groups(self):
        # Spec E9 rule 35: (a) a steward's number does not blink, only `⚑` changes between
        # beats; (b) one empty row parts [repository, Git] from [model, context]; (c) one
        # empty row parts the head from the first section, and the rows the sections leave
        # stay empty under the last one above MESSAGES; (d) both are structural rows: they go
        # after the values on several rows and the sections' rows, the metadata one before the
        # rule, and all of them before the route box folds.
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
// (b) The metadata's two groups: with Git, and without it (the row parts the repository from the model). 11-20: that row is the SESSION group's head.
const r50=draw([oneShot,staged],40,50);
assert(r50[1].includes('hearting/')&&r50[2].includes('(fix/fleet')&&/^│ SESSION ─+ │$/.test(r50[3])&&r50[4].includes('claude-fable-5-2 high')&&r50[5].startsWith('│  CTX    ━━━━'),r50.join('\n'));
const noGit=view.render({width:40,height:50,side:true,fill:true,model:{...head,git:[],routes:[oneShot],route_count:1},state:'working',configuration:{},questions:()=>[]}).rows.map(r=>r.map(p=>p.text).join(''));
assert(noGit[1].includes('hearting/')&&/^│ SESSION ─+ │$/.test(noGit[2])&&noGit[3].includes('claude-fable-5-2 high'),noGit.join('\n'));
// (c) Six routes and eight messages at 40×58: the question's head, then the route box (every route
// in its whole form), the one parting row above the first section, and the rows the sections leave
// stay empty under the box, above MESSAGES (`MESSAGES +5`, three messages of two rows each, at the panel's bottom).
const more=[...messages,message('←','[52]','08:10','두 번째 검토 — 줄이는 순서를 확인해 주세요, 본문이 길어서 둘째 줄까지 넘어감',6),
 message('→','[73]','08:20','PR 출판 요청 두 번째',7,'전달됨'),message('←','[e7]','08:30','설치 끝',8)];
const noted=[oneShot,staged,waiting].map(r=>r===waiting?r:{...r,paint:{...r.paint,now:'now step'}});   // (the third has no NOW, so no NOW row)
const six=[...Array(6)].map((_,i)=>{const r=noted[i%3];return {...r,id:'s'+i,title:r.title+' '+(i+1)};});
const QUESTION={count:1,label:'QUESTIONS',rows:[[P('◑ '),P('Q_HEAD')],[P('  '),P('1 A · 2 B')]],one:[[P('◑ '),P('Q_HEAD')]],line:[P('Q_HEAD')],choices:2};
const paint=(routes,height,o={})=>view.render({width:40,height,side:true,fill:true,model:{...head,messages:o.messages||more,routes,route_count:routes.length,answerable:!!o.q,...(o.model||{})},
 state:'idle',configuration:{},brief:()=>o.q||null}).rows.map(r=>r.map(p=>p.text).join(''));
const rule=rows=>rows.findIndex(s=>s.startsWith('│ MESSAGES'));
// (11-16: the QUESTIONS section is a thin box too, its name in the top line; `bottom` is the last box's, the route box's.)
const first=rows=>rows.findIndex(s=>/^│ (\d+ )?QUESTIONS |^│ ╭─/.test(s)),bottom=rows=>rows.findLastIndex(s=>s.startsWith('│ ╰'));
const full=paint(six,58),m=rule(full),q0=first(full),rb=bottom(full);
assert(q0>0&&blank(full[q0-1])&&/^│ 1 QUESTIONS +› │$/.test(full[q0])&&full[q0+1].startsWith('│ ╭─ 6 ROUTE ─')&&rb>q0+24&&m>rb&&/^│ MESSAGES \+5 +› │$/.test(full[m]),full.join('\n'));   // 11-20: three at most, five of the eight left out
// MESSAGES keeps to the panel's bottom (spec rule 14): its rows end above the thin rule and the 5h/7d row, the spare rows
// stand between the route box and it.
assert([1,3,5].every(k=>/^│  [←→] \[/.test(full[m+k])&&/^│   \S/.test(full[m+k+1]))&&m+7===full.length-3&&/^│ ─+ │$/.test(full.at(-3))&&full.slice(rb+1,m).every(blank)&&full.at(-2).includes('7d 66%'),full.join('\n'));
// With a question the summary stands in the QUESTIONS box, between its top line and the route box (11-15: right above it).
const asked=paint([oneShot,staged],44,{q:QUESTION,messages}),q=asked.findIndex(s=>s.includes('1 QUESTIONS '));
assert(q>2&&blank(asked[q-1])&&asked[q+1].startsWith('│ │ ◑ Q_HEAD')&&asked[q+2].startsWith('│ │   1 A · 2 B')&&/^│ ╰─+╯ │$/.test(asked[q+3])&&asked[q+4].startsWith('│ ╭─ 2 ROUTE ─')&&rule(asked)>bottom(asked),asked.join('\n'));
// No routes: no box stands (a section with no row to show inside is its head row), and the parting row still stands above the first section.
const none=paint([],30,{model:{routes_known:true}});
assert(none.filter(s=>s.includes('╭')).length===0&&/1 QUESTIONS/.test(none[first(none)])&&!none.join('').includes('ROUTE')&&blank(none[first(none)-1])&&first(none)>0&&rule(none)>first(none),none.join('\n'));
// (d) Short of rows: the parting row goes first, then the thin rule above the last row, then values on
// several rows, then the metadata row and the rule under it, then every message folds to one row; only then
// does the route box fold (its NOW rows, then its stage rows) and scroll.
const flags=h=>{const rows=paint(six,h),at=first(rows),git=rows.findIndex(s=>s.includes('(fix/')),title=rows.findIndex(s=>s.startsWith('│  TITLE'));
 return {spacer:at>0&&blank(rows[at-1]),meta:git>0&&/^│ SESSION ─+ │$/.test(rows[git+1]),multi:title>0&&/^│ {9}\S/.test(rows[title+1]),
  ctx:rows.filter(s=>/^│ │ {5}now step/.test(s)).length,place:rows.filter(s=>/^│ │ {5}(staged )?\d+\/\d+: /.test(s)).length,shown:rows.filter(s=>/^│ │ [⠋●✕] /.test(s)).length,
  foot:/^│ ─+ │$/.test(rows.at(-3)),msgfold:rows.some(s=>/^│  [←→] \[/.test(s))&&!rows.some(s=>/^│   \S/.test(s))};};
const gone={};
for(let h=72;h>=14;h--){
 const f=flags(h),all=paint(six,h).join('\n');
 // (11-22: the rule under the metadata is gone: the SESSION head's row is the one that goes, taking MODEL's value and row.) It does not go while a value takes several rows.
 if(f.multi)assert(f.meta,h+' values on several rows go before the head row\n'+all);
 for(const [k,v] of Object.entries({spacer:!f.spacer,foot:!f.foot,multi:!f.multi,msgfold:f.msgfold,meta:!f.meta,ctx:f.ctx<4,place:f.place<4,shown:f.shown<6}))   // (four NOW rows, four stage rows: the one-shot route has none)
  if(v&&gone[k]===undefined)gone[k]=h;
}
// (11-20: the messages fold right after the values on several rows, before the metadata row and the rule go.)
assert(gone.spacer>=gone.foot&&gone.foot>=gone.multi&&gone.multi>=gone.msgfold&&gone.msgfold>=gone.meta&&gone.meta>=gone.ctx&&gone.ctx>gone.place&&gone.place>gone.shown,JSON.stringify(gone));
'''
        self.node(script, "panel-view.cjs")

    def test_the_repository_row_counts_worktrees_and_mem_reads_the_latest_saved_memory(self):
        # Spec E9 rules 37, 41: beside the native area `REPO hearting · wt 12` and `GIT` without the
        # count. (MEM moved under the messages, spec rule 46; an older backend's row goes there.)
        # The bottom layout keeps its rows as they were.
        script = self.FIXTURE + r'''
const D=t=>({text:t,dim:true}),MEM=[D('3m '),D('decision '),P('셸 패널 route 줄 순서는 누가 / context·시간 / 무엇 / 종류로 정함, 지어낸 기억')];
const side={...head,repo_side:[P('hearting')],worktrees:[D('wt 12')],git_side:[D('fix/fleet-opencode-… ✓')],memory:MEM};
const paint=(routes,width,height,o={})=>view.render({width,height,side:o.side!==false,fill:o.side!==false,model:{...side,...(o.model||{}),routes,route_count:routes.length},
 state:'working',configuration:{},questions:()=>[]});
const text=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const v50=paint([oneShot,staged],40,50),r50=text(v50);
assert(r50[1].startsWith('│ REPO hearting · wt 12 ')&&r50[2].startsWith('│  GIT    fix/fleet-opencode-… ✓ ')&&!r50.slice(1,3).join('').includes('🚧'),r50.join('\n'));
// The labels are TITLE's and NOW's: dim, seven cells; the name keeps its color, the rest is dim.
const labels=[v50.rows[2].find(p=>/^GIT +$/.test(p.text))];   // (11-22: REPO is a group head, its bold name one cell before its value)
assert(labels.every(p=>p&&p.dim===true&&p.text.length===7),JSON.stringify(labels));
assert(v50.rows[1].find(p=>p.text==='REPO').role==='title');
assert(v50.rows[1].find(p=>p.text==='hearting').dim!==true&&v50.rows[1].filter(p=>p.text===' · '||p.text==='wt 12').every(p=>p.dim===true),JSON.stringify(v50.rows[1]));
// Short of cells the branch gives way; the status and a warning stay whole (spec rule 30 (d)).
const tight=text(paint([oneShot],40,50,{model:{git_side:[D('fix/fleet-opencode-…'),D(' ↓3'),D(' ⚠️rebase')]}}));
assert(/^│  GIT    fix\/fleet-.*… ↓3 ⚠️rebase +│$/.test(tight[2]),tight.join('\n'));
// Narrower than 30 cells inside, the labels go first (as TITLE's and NOW's).
const narrow=text(paint([oneShot],32,50));
assert(narrow[1].startsWith('│ REPO hearting · wt 12')&&narrow[2].startsWith('│  fix/fleet-opencode-… ✓')&&!narrow.join('').includes('REPO  '),narrow.join('\n'));   // (11-20: the REPO row is the group's head: its word stands first, as `REPO `)
assert(v50.groups.includes('head')&&v50.groups.includes('git'),v50.groups);
// Only the count was left in the parentheses: the repository row carries the Git group.
const bare=paint([oneShot],40,50,{model:{git_side:[]}});
assert(text(bare)[1].includes('wt 12')&&!text(bare).join('').includes('GIT')&&bare.groups.includes('git'),text(bare).join('\n'));
// Spec E9 rule 46 (e): an older backend's one MEM row (painted pieces only) stands in the MEM
// section under the messages (11-20: one row, its age `3m`, its type left out, then the headline, `…` where it goes on); the head has no MEM row.
const title=r50.findIndex(s=>s.startsWith('│  TITLE')),mem=r50.findIndex(s=>/^│ MEM +› │$/.test(s)),msg=r50.findIndex(s=>s.startsWith('│ MESSAGES'));
assert(title>0&&!r50.slice(0,msg).some(s=>s.includes('MEM'))&&mem>msg&&/^│  · 3m 셸 패널 route 줄 순서는 누가/.test(r50[mem+1])&&!r50.slice(mem).join('').includes('decision')
 &&mem+1===r50.length-4&&/^│ ─+ │$/.test(r50.at(-3)),r50.join('\n'));
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
assert(meta[0].startsWith('│ REPO hearting · wt 12')&&meta[1].startsWith('│  GIT    fix/fleet-opencode-… ✓')&&/^│ SESSION ─+ │$/.test(meta[2])   // (11-20: the SESSION group's head stands where the empty row stood)
 &&meta[3].startsWith('│  MODEL  claude-fable-5-2 high ')&&meta[4].startsWith('│  CTX    ━━━━────── 41% '),r38.join('\n'));
// (11-22: GIT, MODEL, CTX stand one cell in; REPO is the group's head, its bold name and one cell.)
const labels=[2,4,5].map(i=>v38.rows[i].find(p=>/^(GIT|MODEL|CTX) +$/.test(p.text)));
assert(labels.every(p=>p&&p.dim===true&&p.text.length===7),JSON.stringify(labels));
assert(!r38.slice(0,8).join('\n').includes('context'),r38.join('\n'));
assert(v38.groups.includes('context'),v38.groups);
// (b) A long model: the name gives way with `…`, the effort stays whole (as GIT's branch).
for(const o of [{model:{selected:[P('opencode-go/deepseek-v4.1-flash'),D(' '),P('max')]}},{native:{model:'opencode-go/deepseek-v4.1-flash',effort:'max'}}]){
 const row=text(paint(38,o))[4];
 assert(/^│  MODEL  opencode-go\/deepseek-\S*… max │$/.test(row)&&cells(row)===38,row);
}
// (c) 30 cells inside keep the labels; 29 cells inside, all four go and `context` comes back.
assert(text(paint(34))[5].startsWith('│  CTX    ━━━━'),text(paint(34)).join('\n'));
const r33=text(paint(33));
assert(r33[1].startsWith('│ REPO hearting · wt 12')&&r33[2].startsWith('│  fix/fleet-opencode-… ✓')&&r33[4].startsWith('│  claude-fable-5-2 high')
 &&r33[5].startsWith('│  context ━━━━────── 41%')&&!/GIT |MODEL|CTX/.test(r33.slice(0,8).join('')),r33.join('\n'));   // (the route box's own `CTX` rows are below the head; 11-20: the REPO row keeps its word, the group's head)
// (d) A model not known yet.
assert(text(paint(38,{model:{selected:[]}}))[4].startsWith('│  MODEL  Checking '),text(paint(38,{model:{selected:[]}})).join('\n'));
// (e) OpenCode's MCP count stays on the CTX row; the MODEL row carries none.
const mcp=text(paint(38,{model:{mcp:[D('MCP '),P('3/4')]}}));
assert(mcp[5].startsWith('│  CTX    ━━━━────── 41% │ MCP 3/4 ')&&!mcp[4].includes('MCP'),mcp.join('\n'));
// (f) An older backend without `context_side`: the word goes, never `CTX    context`.
for(const context of [[D('context '),...gauge],[P('context ━━━━────── 41%')]]){
 const old=text(paint(38,{model:{context,context_side:undefined}}));
 assert(old[5].startsWith('│  CTX    ━━━━────── 41% ')&&!old.join('').includes('context'),old.join('\n'));
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
for(const [rows,re] of [[side.rows,/^│  GIT    main ↑2 ↓5 +│$/],[under.rows,/^│ \(main ↑2 ↓5 🚧 3\) +│$/]]){
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
        # Spec E9 rule 45: beside the native area `2 ROUTE` (the count first, its word upper case,
        # the running pulse kept) in the route box's top line, `MESSAGES` with its thin rule to `›`, the
        # producer's own label too; role words and kind rows stay as they are; the bottom layout keeps
        # `route N`. (11-15: the route box again; no read pages any more.)
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const owned={...oneShot,role:'owner',kind_word:'code'};
const at=(o={})=>view.render({width:o.width||40,height:60,side:o.side!==false,fill:o.side!==false,model:{...head,routes:[owned,staged],route_count:2,...(o.model||{})},
 state:'working',configuration:{},questions:()=>[],...(o.extra||{})});
const v=at(),rows=plain(v),all=rows.join('\n');
assert(rows.some(s=>s.startsWith('│ ╭─ 2 ROUTE ─'))&&rows.some(s=>/^│ MESSAGES( \+\d+)? +› │$/.test(s))&&!/\broute \d|messages/.test(all),all);   // 11-20: three at most, `+N` counts the rest
const count=v.rows.flat().find(p=>p.text==='2 ROUTE');assert(count&&count.beat===true,JSON.stringify(count));
// (11-17, route row 5: the kind stands alone in the route's first line, `● code`; the owner's line under its title.)
assert(all.includes('⠋ owner')&&rows.some(s=>/^│ │ \S code +\d+m │ │$/.test(s))&&!all.includes('code · one-shot'),all);
// The producer's label alone (no rows yet): its count first too.
const label=plain(at({model:{routes:[],route_count:0,route_label:'route 3 ⠏2'}}));
assert(label.some(s=>s.includes('╭─ 3 ROUTE ⠏2 ─')),label.join('\n'));
// The bottom layout: `route N`, `messages`.
const bottom=plain(at({width:120,side:false})).join('\n');
assert(bottom.includes('╭─ route 2 ')&&/│ messages( \+\d+)? ─+ › │/.test(bottom)&&!/ROUTE|MESSAGES/.test(bottom),bottom);
assert(plain(at({width:120,side:false,model:{routes:[],route_count:0,route_label:'route 3 ⠏2'}})).join('').includes('╭─ route 3 ⠏2 ─'));
"""
        self.node(script, "panel-view.cjs")

    def test_messages_and_mem_keep_to_the_panels_bottom(self):
        # Spec rule 14 (11-11, "메시지는 하단에 고정"; the 11-15 correction of #1638's "spare rows at the
        # bottom"): MESSAGES and MEM stand against the panel's bottom, above the thin rule and the 5h/7d row; the spare
        # rows stand between the sections above (QUESTIONS, the route box, LAB, SESSIONS: their places and
        # order as before, from the top) and MESSAGES. Without MESSAGES, MEM; with neither, the spare rows are below.
        # The bottom layout keeps its label alone.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const blank=s=>/^│ +│$/.test(s);
const tall=(height,o={})=>plain(view.render({width:o.width||40,height,side:o.side!==false,fill:o.side!==false,model:{...head,routes:[oneShot,staged],route_count:2,...(o.model||{})},
 state:'idle',configuration:{},questions:()=>[]}));
for(const height of [50,70,90]){
 const rows=tall(height),r=rows.findIndex(s=>s.startsWith('│ ╭─ 2 ROUTE ')),rb=rows.findIndex((s,i)=>i>r&&s.startsWith('│ ╰')),m=rows.findIndex(s=>s.startsWith('│ MESSAGES'));
 // (11-16: with no row to show inside it, the section stays its head row: an empty box is no box.)
 assert(r>0&&/^│ 1 QUESTIONS /.test(rows[r-1])&&blank(rows[r-2]),height+'\n'+rows.join('\n'));
 // MESSAGES keeps its rows (three at most since 11-20, two each) above the thin rule and the 5h/7d row; the blank rows stand above its head.
 // (11-18: the route box is its top line, the `dispatch` rule, the first route's title, who and CTX row, the second's title, who, CTX row and stage line, then its bottom: `r+9`; no NOW, no row.)
 const last=rows.findLastIndex(s=>/^│  [←→] /.test(s));
 assert(last===rows.length-5&&/^│   \S/.test(rows.at(-4))&&/^│ ─+ │$/.test(rows.at(-3))&&/⚙   ▾ │$/.test(rows.at(-2))&&rb===r+9&&m>rb&&m+8===rows.length-2,height+'\n'+rows.join('\n'));
 assert(blank(rows[m-1])&&rows.slice(rb+1,m).every(blank)&&rows.slice(r+1,rb).every(s=>/^│ │ /.test(s)),height+'\n'+rows.join('\n'));
 // The press targets went with their rows.
 const v=view.render({width:40,height,side:true,fill:true,model:{...head,routes:[oneShot,staged],route_count:2},state:'idle',configuration:{},questions:()=>[]});
 for(let k=0;k<=6;k++)assert(v.hits.some(h=>h.row===m+k&&h.action==='popup:MESSAGES'),k+JSON.stringify(v.hits));
}
// MEM under MESSAGES: its last row above the thin rule and 5h/7d, the blank rows above MESSAGES.
{const rows=tall(50,{model:{memory_items:[{tier:'durable',kind:'decision',age:'3m',text:'MEM_ONE'},{tier:'durable',kind:'fact',age:'1h',text:'MEM_TWO'}]}}),
 m=rows.findIndex(s=>s.startsWith('│ MESSAGES')),e=rows.findIndex(s=>/^│ MEM +› │$/.test(s)),s=rows.findIndex(s=>s.includes('ROUTE_')||/^│ 2 ROUTE /.test(s));
 assert(m>0&&e>m&&/MEM_ONE/.test(rows[e+1])&&/MEM_TWO/.test(rows[e+2])&&e+3===rows.length-3&&/^│ ─+ │$/.test(rows.at(-3))&&blank(rows[m-1]),rows.join('\n'));}
// No MESSAGES: MEM keeps to the bottom. Neither: the sections stand from the top, the blank rows are below.
{const mem=tall(50,{model:{messages:[],memory_items:[{tier:'durable',kind:'decision',age:'3m',text:'MEM_ONE'}]}}),e=mem.findIndex(s=>/^│ MEM +› │$/.test(s));
 assert(e>0&&e+2===mem.length-3&&/MEM_ONE/.test(mem[e+1])&&/^│ ─+ │$/.test(mem.at(-3))&&blank(mem[e-1]),mem.join('\n'));
 const none=tall(50,{model:{messages:[]}}),r=none.findIndex(s=>s.startsWith('│ ╭─ 2 ROUTE ')),rb=none.findIndex((s,i)=>i>r&&s.startsWith('│ ╰'));
 assert(r>0&&rb>r&&none.slice(rb+1,-2).every(blank)&&!none.some(s=>/MEM|MESSAGES/.test(s)),none.join('\n'));}
// No overlap and no overflow at any height 5-60 and width 12-60, beside the native area and below it: the rows
// stay within the height, each as wide as the panel, and a press target stands on a row of its own section.
const mem=[{tier:'durable',kind:'decision',age:'3m',text:'MEM_ONE'},{tier:'durable',kind:'fact',age:'1h',text:'MEM_TWO'}];
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
        # no more than five messages show whatever the height (11-16: two rows each again); the head counts
        # the rest (`MESSAGES +N`). Spare rows never lengthen a body. 11-20 (the person: "MESSAGE도 최대 3개 정도로 다시 줄이고"): three.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const more=[...messages,message('←','[52]','08:10','두 번째 검토',6),message('→','[73]','08:20','PR 출판 요청 두 번째',7,'전달됨'),message('←','[e7]','08:30','설치 끝',8)];
const at=(list,height)=>plain(view.render({width:40,height,side:true,fill:true,model:{...head,messages:list,routes:[oneShot],route_count:1},state:'idle',configuration:{},questions:()=>[]}));
const heads=rows=>rows.filter(s=>/^│  [←→] \[/.test(s));
for(const list of [messages,more])for(const height of [60,90,140]){
 const rows=at(list,height),label=rows.find(s=>s.startsWith('│ MESSAGES'));
 assert.equal(heads(rows).length,3,height+'\n'+rows.join('\n'));
 assert(new RegExp('^│ MESSAGES'+(list.length>3?' \\+'+(list.length-3):'')+' +› │$').test(label),label);
 // The newest three, oldest first.
 assert.deepEqual(heads(rows).map(s=>s.slice(5,9)),list.slice(-3).map(m=>m.who),rows.join('\n'));
}
// Three or fewer: all of them, and no count.
for(const n of [1,2,3]){const rows=at(messages.slice(0,n),90);assert.equal(heads(rows).length,n);assert(rows.some(s=>/^│ MESSAGES +› │$/.test(s)),rows.join('\n'));}
// Spare rows never lengthen a body: a long body is one row (under its head) ending in `…`.
const long=[...messages.slice(0,2),message('←','[52]','08:10','두 번째 검토 — 줄이는 순서를 확인해 주세요, 본문이 길어서 둘째 줄까지 넘어가고 셋째 줄까지 이어지는 긴 본문입니다',6)];
const rows=at(long,90),i=rows.findIndex(s=>s.startsWith('│  ← [52]'));
assert(i>0&&rows[i+1].startsWith('│   두 번째')&&rows[i+1].includes('…')&&!/^│   \S/.test(rows[i+2]),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_each_section_keeps_its_rows_beside_the_route_box(self):
        # 11-15 (popup design E; the route box again; no LAB or SESSIONS, whatever the backend sends): a
        # side panel 44 wide with every section. With room (46 rows) QUESTIONS keeps its summary's two rows
        # (and the guide line), the route box every route whole, MESSAGES three rows, MEM two; short of rows
        # (20) each other section keeps one and the box scrolls in place. The heads in order and the press
        # actions of every head and row; a route's rows open the route popup with it unfolded.
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
 memory_items:[{tier:'durable',kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'},{tier:'durable',kind:'fact',age:'1h',text:'팝업은 Fleet 표'}],
 quota:[p('5h 18%'),p(' │ '),p('7d 64%')]};
const brief=()=>({count:2,label:'QUESTIONS',rows:[[p('◑ 패널을 접었을 때의 모양을 어느 안으로 구현할까요?')],[p('  1 아래 두 줄 (추천) · 2 넓힌 옆 띠 · +2')]],
 one:[[p('◑ 패널을 접었을 때의 모양을 어느 안으로')]],line:[p('패널을 접었을 때')],choices:4});
const paint=height=>pv.render({width:44,height,side:true,model,native:{},state:'working',sid:'s',harness:'claude',frame:0,brief,sent:()=>[]});
// (11-16: with room QUESTIONS is a thin rounded box, its name in the top line; short of rows the box's two lines go and the section reads as a head row and its rows.)
const HEADS=[['QUESTIONS',/^│ ╭─ 3 QUESTIONS ─+ ›─╮ │$|^│ 3 QUESTIONS +› │$/],['ROUTE',/^│ ╭─ 4 ROUTE ─+(?: [↑↓]\d+)? ›─╮ │$|^│ ╭─ 4 ROUTE ─+ ›─╮ │$/],
 ['MESSAGES',/^│ MESSAGES(?: \+\d+)? +› │$/],['MEM',/^│ MEM +› │$/]];
// The rows under each head, up to the next head (MEM: its `◆` rows; a box: up to its bottom line).
const sections=rows=>{const at=HEADS.map(([name,re])=>[name,rows.findIndex(s=>re.test(s))]);
 assert(at.every(([,i])=>i>0),rows.join('\n'));assert(at.every(([,i],k)=>!k||i>at[k-1][1]),'order of the heads\n'+rows.join('\n'));
 const out={};at.forEach(([name,i],k)=>{const end=name==='ROUTE'||/^│ ╭/.test(rows[i])?rows.findIndex((s,j)=>j>i&&s.startsWith('│ ╰'))+1:k+1<at.length?at[k+1][1]:rows.findIndex((s,j)=>j>i&&!/^│  [◆◇·] |^│   \S/.test(s));
  const body=rows.slice(i+1,end);while(body.length&&/^│ +│$/.test(body.at(-1)))body.pop();out[name]={head:i,rows:body,from:i+1};});return out;};
for(const [height,rowsOf] of [[46,{QUESTIONS:2,MESSAGES:6,MEM:2}],[20,{QUESTIONS:1,MESSAGES:1,MEM:1}]]){
 const v=paint(height),rows=v.rows.map(r=>r.map(x=>x.text).join('')),sec=sections(rows),label=height+'\n'+rows.join('\n');
 assert(rows.length<=height&&rows.at(-2).includes('7d 64%'),label);
 assert(!/\bLAB\b|SESSIONS|긴 문맥|분리 모델/.test(rows.join('\n')),label);
 // QUESTIONS: the summary's rows (the guide line stands under them while there is room), the others: their rows.
 // (With room, in the box: its content rows `│ │ … │ │`, its bottom line the section's last row.)
 const boxed=height===46,summary=sec.QUESTIONS.rows.filter(s=>!/입력창에 답/.test(s)&&!s.startsWith('│ ╰'));
 assert.equal(summary.length,rowsOf.QUESTIONS,label);assert(summary[0].startsWith(boxed?'│ │ ◑ 패널을':'│ ◑ 패널을'),label);
 assert.equal(rows[sec.QUESTIONS.head].startsWith('│ ╭'),boxed,label);assert.equal(sec.QUESTIONS.rows.at(-1).startsWith('│ ╰'),boxed,label);
 assert.equal(sec.QUESTIONS.rows.some(s=>/입력창에 답/.test(s)),height===46,label);
 for(const name of ['MESSAGES','MEM'])assert.equal(sec[name].rows.length,rowsOf[name],name+' '+label);
 // The route box: with room every route whole (its glyph and title, and the stage line a staged route has: these
 // routes carry no kind word, owner or workers), in the producer's order; short of rows a window of them, the
 // working first, the border counting the rest.
 const box=sec.ROUTE.rows,routeRows=box.filter(s=>/^│ │ [⠏⠋✕✓] /.test(s));
 // (Each route keeps its time too: these routes carry no owner, and a route does not lose its time for that.)
 if(height===46)assert(routeRows.length===4&&box.filter(s=>/│ │   2\/2: execute/.test(s)).length===1&&['23m','54m','14m','31m'].every(m=>box.filter(s=>new RegExp('│ │ .*'+m+' +│ │$').test(s)).length===1)&&box.at(-1).startsWith('│ ╰─')&&!/[↑↓]\d/.test(box.at(-1)),label);
 else assert(routeRows.length>=1&&routeRows.length<4&&/↓\d+ ─╯/.test(box.at(-1))&&routeRows[0].includes('질문 팝업'),label);
 // Press actions: the question's head and rows open it; every head and row of MESSAGES its popup;
 // the box's top line the route popup and a route's rows the popup with that route unfolded; MEM opens nothing.
 const act=i=>v.hits.filter(h=>h.row===i).map(h=>h.action).filter(a=>a!=='fold'&&a!=='settings');
 for(let i=sec.QUESTIONS.head;i<=sec.QUESTIONS.head+sec.QUESTIONS.rows.length;i++)assert.deepEqual(act(i),['question'],'question '+i+label);
 assert.deepEqual(act(sec.ROUTE.head),['popup:ROUTE']);
 // (11-17: the box's first row is the zone rule `─ dispatch ───`, which opens nothing; it stands while a row of its zone shows, a window of fewer rows too.)
 assert(/^│ │ ─ dispatch ─+ │ │$/.test(box[0]),label);
 const first=sec.ROUTE.from+1;assert.deepEqual(act(first),['popup:ROUTE:a']);assert.deepEqual(act(sec.ROUTE.from),[],label);
 if(height===46){
  assert.deepEqual(box.slice(1,-1).map((_,k)=>act(first+k)[0]),['a','a','a','b','b','c','c','d','d'].map(id=>'popup:ROUTE:'+id),label);}
 assert.deepEqual(act(sec.ROUTE.from+box.length-1),[],'the bottom line opens nothing');
 assert.deepEqual(act(sec.MESSAGES.head),['popup:MESSAGES']);sec.MESSAGES.rows.forEach((_,k)=>assert.deepEqual(act(sec.MESSAGES.from+k),['popup:MESSAGES']));
 // (11-20: MEM opens its popup, a record the popup on that record.)
 assert.deepEqual(act(sec.MEM.head),['popup:MEM']);sec.MEM.rows.forEach((_,k)=>assert.deepEqual(act(sec.MEM.from+k),['popup:MEM:'+k]));
 if(height===46){
  // (11-20: the newest three of the four.)
  assert.deepEqual([0,2,4].map(k=>sec.MESSAGES.rows[k].slice(3,9)),['→ [73]','← [12]','← [52]'],label);
  assert.deepEqual([1,3,5].map(k=>sec.MESSAGES.rows[k].slice(2,6)),['  설치','  답 ','  정리'],label);}
 // MESSAGES and MEM keep to the bottom: MEM's last row, a thin rule (the second thing to go when short of rows), then the 5h/7d row.
 if(height===46)assert(/^│  ◆ /.test(rows.at(-5))&&/^│  ◆ /.test(rows.at(-4))&&/^│ ─+ │$/.test(rows.at(-3))&&rows.at(-2).includes('7d 64%'),label);
 else assert(/^│  ◆ /.test(rows.at(-3))&&rows.at(-2).includes('7d 64%'),label);   // 11-20: short of rows the record is its one row (mark, age, headline)
}
// Taller than the sections need: the blank rows stand between the route box and the MESSAGES head, not under MEM.
{const v=paint(52),rows=v.rows.map(r=>r.map(x=>x.text).join('')),sec=sections(rows),blank=s=>/^│ +│$/.test(s),label=rows.join('\n');
 const end=sec.ROUTE.from+sec.ROUTE.rows.length;
 assert(sec.MESSAGES.head-end>=1&&rows.slice(end,sec.MESSAGES.head).every(blank),label);
 assert(/^│  ◆ /.test(rows.at(-4))&&/^│ ─+ │$/.test(rows.at(-3))&&rows.at(-2).includes('7d 64%')&&sec.MEM.rows.length===2,label);
 // The press targets of the moved rows moved with them.
 const act=i=>v.hits.filter(h=>h.row===i).map(h=>h.action).filter(a=>a!=='fold'&&a!=='settings');
 assert.deepEqual(act(sec.MESSAGES.head),['popup:MESSAGES']);assert.deepEqual(act(sec.MESSAGES.from),['popup:MESSAGES']);
 assert.deepEqual(act(end),[]);}
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
 memory_items:[{tier:'durable',kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'}],quota:[p('5h 18%'),p(' │ '),p('7d 64%')]};
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
assert(/^│ 3 QUESTIONS/.test(rows[1])&&/^│ ◑ /.test(rows[2])&&rows[3].endsWith('⚙   ▾ │'),rows.join('\n'));
rows=paint(6,44).rows.map(r=>r.map(x=>x.text).join(''));
assert(/^│ REPO /.test(rows[1])&&/^│ 3 QUESTIONS/.test(rows[2]),rows.join('\n'));
"""
        self.node(script, "panel-view.cjs")

    def test_the_question_to_answer_here_reads_in_red_letters(self):
        # 11-15 (the person: "빨간 틴트 안에 다시 검은 틴트는 못생겼는데" / "그냥 질문은 글자 자체를 붉은 색으로 하는
        # 정도로하자"; it replaces panel polish 1's button): in the open panel's QUESTIONS section the question a
        # press answers here, now, stands on no ground of its own (no fill, no chips): its words and its
        # choices' row (`1 파일 · 2 표준 출력 (추천) · 3 둘 다`, the ` · ` dim) are in the answer wait's red
        # (`accent`, 174 / 131), never dimmed, no weight added; `↵ 답하기 · Ctrl+]` stands in rose at the right
        # under them; a press on any of its rows opens the question popup. A wait answered elsewhere, the
        # answers on their way and the guide line are never red. Short of rows the action row goes first,
        # then the choices, the first row to the last (heights 5-60 × widths 12-60). Eight colors keep the
        # red, mono and NO_COLOR the weight; no `▌` bar anywhere. A panel shade keeps its own ground.
        # 11-16: with room the section is a thin rounded box (its rows at the box's inner width): its line is the
        # rose (`accent-line`) and blinks where a question is answerable here, the letters inside stand still; a
        # question answered elsewhere leaves the line dim. Short of rows the box's two lines go and the letters
        # blink again as before.
        script = r"""
const assert=require('node:assert/strict'),pv=require(process.argv[1]);
const p=(text,extra={})=>({text,...extra});
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const styles={dim:{foreground:-1,dim:true},loc_repo:{foreground:253},g_blocked:{foreground:217},lvl_g:{foreground:150}};
const model={tag:'[e9]',repo:[p('hearting/')],selected:[p('Opus 5.5')],context_side:[p('━━─── '),p('46%')],wait:'◑답 대기 1',styles,
 labels:{answer_action:'↵ 답하기 · Ctrl+]'},session_title:'패널 손질',now:'그리는 중',route_label:'route 1',route_count:1,route_total:1,
 routes:[{id:'a',title:'휠 입력 시험',kind:'dead',state:'dead',shape:'one-shot-owner',stages:[],elapsed:'3m',paint:{glyph:p('✕')}}],
 messages:[{way:'←',who:'[73]',text:'설치 확인 끝났습니다'}],memory_items:[{tier:'durable',kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'}],
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
const inside=r=>r.slice(3,-3);// (a row's content between the box's two walls)
const grounds=v=>v.rows.map(r=>r.some(x=>x.background!==undefined));
const red=x=>x.foreground===174&&x.dim!==true;
// (a) 256 colors: the head, the sent line, then three rows; no ground anywhere, the words red.
{const v=paint({q:choice(),sent,width:48}),rows=v.rows.map(str),head=rows.findIndex(r=>/^│ ╭─ 1 QUESTIONS ─+ ›─╮ │$/.test(r));
 assert(head>0,rows.join('\n'));assert(/^│ │ ✓ 답 보냄 · 2 표준 출력 +│ │$/.test(rows[head+1])&&/^│ ╰─+╯ │$/.test(rows[head+5]),rows.join('\n'));
 assert(!grounds(v).some(Boolean),rows.join('\n'));
 const at=[head+2,head+3,head+4];
 // The first row: `◑` and the words in the body color (11-16, the person: "질문 바운더리만 붉게 하고, 내용은 흰색으로"), no weight added.
 const first=inside(v.rows[at[0]]);assert(str(first).startsWith('◑ 로그를 어느 쪽에 남길까요? 지금'),str(first));
 const bodyInk=x=>x.foreground===253&&x.dim!==true&&x.bold!==true&&x.role!=='accent';
 assert(first.filter(x=>x.text.trim()).every(bodyInk),JSON.stringify(first));
 // The choices: one row of words, the names in the body color, the numbers in the question's rose (11-17), the ` · ` and `(추천)` dim.
 assert(rows[at[1]].startsWith('│ │   1 파일 · 2 표준 출력 (추천) · 3 둘 다'),rows[at[1]]);
 const ch=inside(v.rows[at[1]]);
 assert(ch.filter(x=>/파일|표준 출력|둘 다/.test(x.text)).every(bodyInk)&&ch.filter(x=>/^\d $/.test(x.text)).every(x=>x.foreground===174&&x.role==='accent'&&x.dim!==true)&&ch.filter(x=>x.text===' · ').every(x=>x.dim===true),JSON.stringify(ch));
 assert(ch.filter(x=>x.text===' (추천)').every(x=>x.dim===true),JSON.stringify(ch));
 // The action at the right, in rose.
 assert(rows[at[2]].endsWith('↵ 답하기 · Ctrl+] │ │'),rows[at[2]]);
 assert(inside(v.rows[at[2]]).filter(x=>x.text.trim()).every(x=>x.foreground===174),JSON.stringify(v.rows[at[2]]));
 // The box's line is the rose and blinks (the answer is wanted here); the letters inside stand still.
 const line=v.rows[head].filter(x=>/^[╭─╮ ]+$/.test(x.text)&&x.text.trim());
 assert(line.length&&line.every(x=>x.role==='accent-line'&&x.blink===true),JSON.stringify(line));
 assert(!v.rows.slice(head+1,head+5).flat().some((x,i)=>x.blink===true&&!/^[│╰╯─ ]+$/.test(x.text)),JSON.stringify(v.rows.slice(head+1,head+5)));
 assert(v.rows[head+5].filter(x=>/^[╰─╯]+$/.test(x.text)).every(x=>x.role==='accent-line'&&x.blink===true),JSON.stringify(v.rows[head+5]));
 // A press on any of its rows (and the head and the bottom line) opens the question popup.
 for(const i of [head,...at,head+5])assert(v.hits.some(h=>h.row===i&&h.action==='question'),String(i));
 // The sent line keeps its own colors, not red.
 assert(v.rows[head+1].find(x=>x.text==='✓ 답 보냄').foreground===150&&!inside(v.rows[head+1]).some(red));
 // The messages' and memories' rows are not red: the question reads apart from them.
 for(const r of v.rows.filter(r=>/\[73\]|패널 접힘/.test(str(r))))assert(!r.some(red),str(r));}
// (b) A wait answered elsewhere, the sent line alone, no question: nothing red but the count, no action row.
for(const q of [there,null]){const v=paint({q,sent}),rows=v.rows.map(str);
 assert(!grounds(v).some(Boolean)&&!rows.join('').includes('↵'),JSON.stringify(q));
 for(const r of v.rows.filter(r=>/다른 세션의 질문|그 창에서 답/.test(str(r))))assert(!inside(r).filter(x=>x.text!=='◑').some(red),str(r));
 // The box's line is dim (nothing here to answer), and does not blink.
 const top=v.rows.find(r=>/^│ ╭─ 1 QUESTIONS/.test(str(r))),line=top.filter(x=>/^[╭─╮ ]+$/.test(x.text)&&x.text.trim());
 assert(line.length&&line.every(x=>x.dim===true&&x.role==='messages'&&x.blink!==true),JSON.stringify(q)+JSON.stringify(line));
 assert(!v.rows.flat().some(x=>x.role==='accent-line'),JSON.stringify(q));}
// (c) Eight colors: the words in the terminal's red; mono and NO_COLOR: by weight; no bar, no ground.
// (11-16: inside the box the letters are the body color, never red or bold; the box's line carries the weight.)
for(const [cfg,m,look] of [[{theme:'mono'},{},x=>x.bold!==true&&x.foreground==null],[{},{colors:8,styles:{...styles,g_blocked:{foreground:1}}},x=>x.foreground!==1&&x.bold!==true],[{},{no_color:true},x=>x.bold!==true]]){
 const v=paint({q:choice(),cfg,model:m}),rows=v.rows.map(str);
 assert(!grounds(v).some(Boolean)&&!rows.join('').includes('▌'),JSON.stringify(cfg)+'\n'+rows.join('\n'));
 const r=v.rows.find(r=>str(r).includes('◑ 로그를'));assert(inside(r).filter(x=>/로그/.test(x.text)).every(look),JSON.stringify([cfg,m,r]));}
// (d) A light face: the deep rose; a shade keeps the panel's own ground under the red words.
{const v=paint({q:choice(),cfg:{theme:'light'}}),r=v.rows.find(r=>str(r).includes('◑ 로그를'));
 assert(inside(r).filter(x=>/로그/.test(x.text)).every(x=>x.foreground===237)&&!grounds(v).some(Boolean),JSON.stringify(r));}
{const v=paint({q:choice(),cfg:{shade:'medium'},background:'dark'}),r=v.rows.find(r=>str(r).includes('◑ 로그를'));
 assert(inside(r).every(x=>x.background===235)&&inside(r).filter(x=>/로그/.test(x.text)).every(x=>x.foreground===253),JSON.stringify(r));}
// (e0) Short of rows (20): the plain section, no box lines; its red letters blink again as before.
{const v=paint({q:choice(),height:20,sent}),rows=v.rows.map(str),at=rows.findIndex(r=>r.includes('◑ 로그를'));
 assert(rows.some(r=>/^│ 1 QUESTIONS +› │$/.test(r))&&!rows.some(r=>/╭─ 1 QUESTIONS/.test(r))&&at>0,rows.join('\n'));
 assert(v.rows[at].filter(x=>/로그/.test(x.text)).every(x=>red(x)&&x.blink===true),JSON.stringify(v.rows[at]));}
// (e) Short of rows: the action row goes first, then the choices; the first row stays to the last.
// Every height 5-60 at every width 12-60 keeps its rows within the panel, each as wide.
const kinds=new Set();
for(let h=5;h<=60;h++)for(let w=12;w<=60;w++){
 const v=paint({q:choice(),height:h,width:w,sent}),rows=v.rows.map(str),label=h+'x'+w+'\n'+rows.join('\n');
 assert(rows.length<=h&&rows.every(r=>cells(r)===w),label);
 const at=rows.findIndex(r=>r.includes('◑ 로'));
 if(w>=20)assert(at>0,label);
 if(at<0)continue;
 const choices=/^│ (?:│ )?  1 /.test(rows[at+1]||''),action=(rows[at+(choices?2:1)]||'').includes('↵');
 assert(!action||choices,label);
 kinds.add(1+(choices?1:0)+(action?1:0));
}
assert.deepEqual([...kinds].sort(),[1,2,3]);
// (f) The folded strip: one row, no ground, the wait red; the question's words are not on it.
{const v=paint({q:choice(),folded:true,width:90,height:1}),r=v.rows[0];
 assert(!grounds(v).some(Boolean)&&r.find(x=>x.text==='◑답 대기 1').foreground===174&&!str(r).includes('로그를'),str(r));
 assert(v.hits.some(h=>h.action==='question'));}
"""
        self.node(script, "panel-view.cjs")

    def test_the_veil_is_one_gray_per_face_and_none_where_no_hue_is_shown(self):
        # 11-15 (panel polish 3): the gray the host veils everything behind a standing popup with: 239 on
        # a dark or unknown face, 250 on a light one; none in mono, eight colors or NO_COLOR, where the
        # popup's frame line is bright (not dim) instead (and, where it veils, the popup's own bright line).
        script = r"""
const assert=require('node:assert/strict'),pv=require(process.argv[1]);
assert.equal(pv.veil({},{colors:256}),239);assert.equal(pv.veil({},{colors:1<<24,background:'dark'}),239);
assert.equal(pv.veil({theme:'light'},{colors:256}),250);assert.equal(pv.veil({},{colors:256,background:'light'}),250);
for(const [cfg,o] of [[{theme:'mono'},{colors:256}],[{},{colors:8}],[{},{colors:256,noColor:true}]])assert.equal(pv.veil(cfg,o),null,JSON.stringify([cfg,o]));
const styles={dim:{foreground:-1,dim:true}};
const lines=v=>v.rows.flat().filter(x=>/^[╭╮╰╯│─ ]+$/.test(x.text)&&/[╭╮╰╯│─]/.test(x.text));
const frame=(model,cfg={})=>lines(pv.popup({cols:100,rows:30,title:[{text:'T'}],body:[[{text:'x'}]],sections:['QUESTIONS'],model:{styles,...model},configuration:cfg}));
// (11-15: where it veils, the popup's line is its face's bright `popupLine` on its own ground.)
for(const [cfg,background,F] of [[{},'',pv.FACES.dark],[{},'dark',pv.FACES.dark],[{theme:'light'},'',pv.FACES.light]]){
 const lit=lines(pv.popup({cols:100,rows:30,title:[{text:'T'}],body:[[{text:'x'}]],sections:['QUESTIONS'],model:{styles,colors:256},configuration:cfg,background}));
 assert(lit.length>4&&lit.every(x=>x.dim!==true&&x.foreground===F.popupLine&&x.background===F.popup),JSON.stringify(lit));}
for(const [m,cfg] of [[{colors:8},{}],[{colors:256,no_color:true},{}],[{colors:256},{theme:'mono'}]])
 assert(frame(m,cfg).every(x=>x.dim!==true&&x.background===undefined),JSON.stringify([m,cfg]));
"""
        self.node(script, "panel-view.cjs")

    def test_a_mem_row_says_durable_or_working_its_age_and_its_kind(self):
        # 11-15 (panel polish 5, spec rule 46): a MEM record says what the memory system records: `◆` durable or `◇` working (`·` for a record that
        # names no tier). 11-20 (the person: "메모리 내용은 한줄로"): the record is one row, as a message's one-row form: the mark, the age and the
        # kind dim (`◆ 10m decision`), then the headline in the body color; its scope is read in the MEM popup (`memoryBody`). This repository's
        # records and the global ones stand together (the backend's order, newest first). Short of cells the kind goes first; the mark and the age
        # stay. Mono and NO_COLOR keep the two marks apart by shape. MESSAGES and MEM keep to the bottom.
        script = self.FIXTURE + r"""
const items=[{tier:'durable',scope:'project',kind:'decision',age:'10m',text:'패널 접힘은 아래 두 줄'},
 {tier:'working',scope:'global',kind:'lesson',age:'2h',text:'스피너 끊김을 고친 법'}];
const drawMem=(o={})=>view.render({width:o.width||44,height:o.height||40,side:true,fill:true,model:{...head,routes:[],route_count:0,memory_items:o.items||items,...(o.model||{})},
 state:'idle',configuration:o.cfg||{},questions:()=>[]});
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const memRows=v=>{const rows=plain(v),i=rows.findIndex(s=>/^│ MEM +(› )?│$/.test(s));return rows.slice(i+1).filter(s=>/^│  [◆◇·] /.test(s));};
let [a,b]=memRows(drawMem({width:52}));
assert(/^│  ◆ 10m decision 패널 접힘은 아래 두 줄 +│$/.test(a),a);
assert(/^│  ◇ 2h lesson 스피너 끊김을 고친 법 +│$/.test(b),b);
// Its words: the lead dim (the mark, the age, the kind), the headline in the body color.
{const v=drawMem({width:52}),rows=plain(v),y=rows.findIndex(s=>s.startsWith('│  ◆ 10m'));
 const lead=v.rows[y].filter(p=>/[◆a-z0-9]/.test(p.text)&&!/패널/.test(p.text));assert(lead.length&&lead.every(p=>p.dim===true),JSON.stringify(v.rows[y]));
 assert(v.rows[y].find(p=>p.text.includes('패널')).dim!==true);}
// Short of cells: the kind goes first; the mark and the age stay.
for(let w=60;w>=14;w--){const rows=memRows(drawMem({width:w})),x=rows[0],y=rows.find(r=>r.startsWith('│  ◇'));
 for(const [r,mark,age] of [[x,'◆','10m'],[y,'◇','2h']]){assert(r.startsWith('│  '+mark+' '+age+' ')&&cells(r)===w,w+' '+r);}}
// A record naming no tier says only what it names.
[a]=memRows(drawMem({items:[{kind:'fact',age:'5m',text:'only a kind'}]}));assert(/^│  · 5m fact only a kind +│$/.test(a),a);
// Mono and NO_COLOR: the two marks stay apart by shape.
for(const o of [{cfg:{theme:'mono'}},{model:{no_color:true}}]){const rows=memRows(drawMem(o));assert(rows[0].startsWith('│  ◆ ')&&rows[1].startsWith('│  ◇ '),JSON.stringify(o));}
// MEM keeps to the panel's bottom, above the thin rule and the last row.
{const rows=plain(drawMem());assert(/^│  ◇ 2h lesson /.test(rows.at(-4))&&/^│ ─+ │$/.test(rows.at(-3))&&rows.at(-2).includes('7d'),rows.join('\n'));}
"""
        self.node(script, "panel-view.cjs")

    def test_mem_stands_under_the_messages_and_gives_way_first(self):
        # Spec E9 rule 46, 11-15: MEM leaves the head for its own section under the messages (its head
        # first and a thin rule on from it, as the messages' `MESSAGES`, no `›`: it opens no popup).
        # 11-16 (v4.36.4's two rows again): each record a dim head row (`◆ decision · 3m`; short of cells
        # the kind goes, the age stays) and its headline two cells in on up to two rows, the second ending
        # in `…`; short of rows the headlines take one row, MEM steps down to one record and then goes
        # before any of the messages does.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const cellsOfRows=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const items=[{tier:'durable',kind:'decision',age:'3m',text:'panel labels in dark grey, and the count leads its word so it reads as a count'},
 {tier:'durable',kind:'lesson',age:'2h',text:'one fix per review finding'}];
const routes=[oneShot,staged,{...oneShot,id:'c'}];
const at=(o={})=>view.render({width:o.width||40,height:o.height||70,side:true,fill:true,model:{...head,routes,route_count:routes.length,memory_items:items,...(o.model||{})},
 state:'idle',configuration:{},questions:()=>[]});
const section=(rows,word)=>{const i=rows.findIndex(s=>new RegExp('^│ '+word+'( \\+\\d+)? +(› )?│$').test(s));if(i<0)return null;let end=i+1;
 while(end<rows.length&&/^│  [◆◇·] |^│  [←→] |^│   \S/.test(rows[end]))end++;return rows.slice(i+1,end);};
let v=at(),rows=plain(v);
const msg=rows.findIndex(s=>s.startsWith('│ MESSAGES')),mem=rows.findIndex(s=>/^│ MEM +› │$/.test(s));
assert(msg>0&&mem>msg&&!rows.slice(0,msg).some(s=>s.includes('MEM')),rows.join('\n'));
// Two records, one row each (11-20): the mark, the age and the kind dim, then the headline (`…` where it goes on).
assert(/^│  ◆ 3m decision panel labels in dark… +│$/.test(rows[mem+1])&&/^│  ◆ 2h lesson one fix per review fin… +│$/.test(rows[mem+2])&&!/^│  [◆◇·] |^│   \S/.test(rows[mem+3]),rows.join('\n'));
// The lead dim; short of cells the kind goes (the headline keeps 14 cells at least), the mark and the age stay.
assert(v.rows[mem+2].filter(p=>/lesson|2h|◆/.test(p.text)&&!/review/.test(p.text)).every(p=>p.dim===true),JSON.stringify(v.rows[mem+2]));
for(const [w,re] of [[40,/^│  ◆ 3m decision /],[34,/^│  ◆ 3m decision /],[30,/^│  ◆ 3m panel labels/],[20,/^│  ◆ 3m /],[16,/^│  ◆ 3m /]]){
 const r=plain(at({width:w})),m=r.findIndex(s=>/^│ MEM +› │$/.test(s));
 assert(re.test(r[m+1])&&r.every(s=>cellsOfRows(s)===w),w+'\n'+r.join('\n'));
}
// The headline in the body color; the title as the messages' (dim, its word the title role).
{const words=v.rows[mem+2].filter(p=>/review/.test(p.text));assert(words.length&&words.every(p=>p.dim!==true),JSON.stringify(v.rows[mem+2]));}
const title=v.rows[mem].find(p=>p.text==='MEM'),mtitle=v.rows[msg].find(p=>p.text==='MESSAGES');
assert(title.role==='title'&&title.dim===true&&mtitle.role==='title'&&mtitle.dim===true);
// 11-20: MEM opens its popup: the head and each record press it (a record by its number).
assert.deepEqual(v.hits.filter(h=>h.row>=mem&&h.row<=mem+2).map(h=>h.action),['popup:MEM','popup:MEM:0','popup:MEM:1'],JSON.stringify(v.hits));
// One record, none.
rows=plain(at({model:{memory_items:items.slice(1)}}));
const one=section(rows,'MEM');assert(one&&one.length===1&&one[0].startsWith('│  ◆ 2h lesson one fix per review fin'),rows.join('\n'));
assert(!plain(at({model:{memory_items:[]}})).join('').includes('MEM'));
// An age missing: the row keeps what is known; a record without a kind reads the same.
{const r=plain(at({model:{memory_items:[{tier:'durable',kind:'',age:'5m',text:'only an age'}]}})),i=r.findIndex(s=>/^│  ◆ 5m only an age +│$/.test(s));assert(i>0,r.join('\n'));}
// No messages: MEM alone under its head.
rows=plain(at({model:{messages:[]}}));assert(!rows.join('').includes('MESSAGES')&&section(rows,'MEM').filter(s=>/^│  ◆ /.test(s)).length===2,rows.join('\n'));
// Narrower than 30 cells inside: the same section.
rows=plain(at({width:32}));assert(section(rows,'MEM')?.[0].startsWith('│  ◆ 3m panel'),rows.join('\n'));
// Short of rows (11-15: every section down to two rows and then one, the lowest first, MEM's headlines
// first taking one row (11-16); then MEM goes whole, its records and then its head, before any message row
// or the MESSAGES head goes).
const memRows=rows=>rows.filter(s=>/^│  ◆ /.test(s)).length,memHead=rows=>rows.some(s=>/^│ MEM +› │$/.test(s));
const msgRows=rows=>rows.filter(s=>/^│  [←→] \[/.test(s)).length,msgHead=rows=>rows.some(s=>s.startsWith('│ MESSAGES'));
let oneRecord=0,noRecord=0,noHead=0,noMsgRow=0,noMsgHead=0;
for(let h=70;h>=8;h--){
 rows=plain(at({height:h}));
 assert(memRows(rows)<=2&&(!memHead(rows)||msgHead(rows)),h+'\n'+rows.join('\n'));
 if(memRows(rows)<2&&!oneRecord)oneRecord=h;if(!memRows(rows)&&!noRecord)noRecord=h;if(!memHead(rows)&&!noHead)noHead=h;
 if(!msgRows(rows)&&!noMsgRow)noMsgRow=h;if(!msgHead(rows)&&!noMsgHead)noMsgHead=h;
 // Spec rule 58: the messages not shown are counted right after their head (`MESSAGES +N`), with or without MEM.
 const shown=msgRows(rows),hidden=msgHead(rows)?head.messages.length-shown:0;
 if(hidden)assert(new RegExp('^│ MESSAGES \\+'+hidden+' +› │$').test(rows.find(s=>s.startsWith('│ MESSAGES'))),h+'\n'+rows.join('\n'));
}
// (A later step that frees more rows than it needed gives them back to the earlier ones, so a state may come back one height lower.)
assert(oneRecord>noRecord&&noRecord>=noHead&&noHead>noMsgRow&&noMsgRow>noMsgHead&&noMsgHead>0,JSON.stringify({oneRecord,noRecord,noHead,noMsgRow,noMsgHead}));
// Spec rule 58 (11-14 night, B-가 with the label first): `MESSAGES` and `MEM` lead their rows (dim,
// the title role) and a thin rule in the same color runs on from each to the row's end (to `›` for
// MESSAGES); nothing stands before a head.
let lined=at();rows=plain(lined);
assert(rows.some(s=>/^│ MESSAGES( \+\d+)? +› │$/.test(s))&&rows.some(s=>/^│ MEM +› │$/.test(s))&&!rows.some(s=>/── (MESSAGES|MEM)/.test(s)),rows.join('\n'));
// (11-22: beside the native area a section's head has no rule: its name, then `›` at the right end.)
const hasRule=word=>lined.rows[rows.findIndex(s=>s.startsWith('│ '+word))].some(p=>/─/.test(p.text));
assert(!hasRule('MESSAGES')&&!hasRule('MEM'),rows.join('\n'));
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

    def test_no_lab_or_sessions_section_stands_whatever_the_backend_sends(self):
        # 11-15: the LAB section (the person: "실험 현황은 다소 섣부른 추가같은데. 지금은 일단 빼") and the SESSIONS
        # section ("세션 현황" meant this session's own progress, not the other sessions) left the panel: an
        # older backend's `lab` or `sessions` draws nothing, changes no frame, glyph or row and opens nothing.
        # The sections stand QUESTIONS, the route box, MESSAGES, MEM.
        script = self.FIXTURE + self.LAB + r"""
const sessions={count:3,counts:[[T('◑1')],[T('●2')]],rows:[1,2,3].map(i=>({glyph:[T('●')],tag:[T('['),T('7'+i),T(']')],title:[T('다른 세션 '+i)],age:[T(i+'m')]}))};
const q={count:1,label:'QUESTIONS',rows:[[P('◑ '),P('조건을 계속 비교할까요?')],[P('  '),P('1 계속 · 2 멈춤')]],one:[[P('◑ '),P('조건을 계속 비교할까요?')]],line:[P('조건을 계속 비교할까요?')],choices:2};
const items=[{tier:'durable',kind:'decision',age:'3m',text:'평가 조건을 같게 유지'}];
for(const state of ['idle','working','blocked'])for(const folded of [false,true])for(const width of [40,32]){
 const at=extra=>view.render({width:folded?120:width,height:folded?1:60,side:!folded,fill:!folded,folded,frame:3,
  model:{...head,tag:'[e9]',routes:[oneShot],route_count:1,memory_items:items,...extra},state,harness:'claude',configuration:{},brief:()=>q});
 const none=at({}),both=at({lab:failedLab(),sessions});
 assert.deepEqual(both.rows,none.rows,state+' '+folded+' '+width);assert.deepEqual(both.hits,none.hits);assert.equal(both.color,none.color);
 const rows=plain(both);
 assert(!/\bLAB\b|SESSIONS|다른 세션|잡음 섞기/.test(rows.join('\n')),rows.join('\n'));
 assert(!both.hits.some(h=>/LAB|SESSIONS/.test(h.action)));
 if(!folded){const order=['1 QUESTIONS','╭─ 1 ROUTE','MESSAGES','MEM'].map(w=>rows.findIndex(r=>r.includes(w)));
  assert(order.every((y,k)=>y>0&&(!k||y>order[k-1])),order+'\n'+rows.join('\n'));}
}
"""
        self.node(script, "panel-view.cjs")

    def test_the_sections_give_way_in_their_order_and_the_question_keeps_its_rows(self):
        # The panel's order (spec rule 34 (b), 46 (d), 11-15, 11-16, 11-20): short of rows MESSAGES and MEM go down first, before any
        # other section gives a row: every message folds from two rows to one, MEM shows two records and then one, MESSAGES two messages
        # and then one (three messages at most). Only then does the route box fold its rows and scroll in place; then MEM goes, then
        # MESSAGES' row and head, and last of these the route box (11-20: the person put it before MESSAGES and MEM).
        # The question keeps its head and first row, the fold and settings row stay.
        script = self.FIXTURE + r"""
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const items=[{tier:'durable',kind:'decision',age:'3m',text:'평가 조건을 같게 유지'},{tier:'durable',kind:'lesson',age:'2h',text:'one fix per review finding'}];
const q={count:1,label:'QUESTIONS',rows:[[P('◑ '),P('조건을 계속 비교할까요?')],[P('  '),P('1 계속 · 2 멈춤')]],one:[[P('◑ '),P('조건을 계속 비교할까요?')]],line:[P('조건을 계속 비교할까요?')],choices:2};
const noted=[oneShot,staged].map(r=>({...r,paint:{...r.paint,now:'now step'}}));   // (11-17: a route's whole form carries its NOW row four cells in)
const sections=(height,o={})=>plain(view.render({width:40,height,side:true,fill:true,model:{...head,routes:[noted[0],noted[1],noted[0]],route_count:3,memory_items:items,...(o.model||{})},
 state:'idle',configuration:{},brief:()=>o.q||null}));
const n=(rows,re)=>rows.filter(s=>re.test(s)).length;
const above=rows=>{const i=rows.findIndex(s=>/^│ MEM ─/.test(s));return i<0?rows:rows.slice(0,i);};
const state=rows=>({body:n(above(rows),/^│   \S/),msg:rows.some(s=>/^│ MESSAGES/.test(s))?n(rows,/^│  [←→] \[/):-1,mem:rows.some(s=>/^│ MEM +›/.test(s))?n(rows,/^│  ◆/):-1,
 box:rows.some(s=>s.startsWith('│ ╭─ 3 ROUTE'))?n(rows,/^│ │ [⠋●✕] /):-1,whole:n(rows,/^│ │ {5}now step/)});
// The first height (from 90 down) each step is taken.
const first={};const seen=[];
for(let h=90;h>=8;h--){
 const rows=sections(h),s=state(rows);seen.push(h+':'+JSON.stringify(s));
 for(const k of ['msg','mem'])for(const at of [4,3,2,1,0,-1])if(s[k]<=at&&first[k+at]===undefined&&!(k==='mem'&&at>=2))first[k+at]=h;
 if(s.msg>0&&!s.body&&first.msgfold===undefined)first.msgfold=h;
 if(s.whole<3&&first.fold===undefined)first.fold=h;if(s.box>=0&&s.box<3&&first.scroll===undefined)first.scroll=h;if(s.box<0&&first.boxGone===undefined)first.boxGone=h;
 assert(rows.at(-2).endsWith('⚙   ▾ │'),h+'\n'+rows.join('\n'));
 assert(rows.length<=h,h);
}
const label=JSON.stringify(first)+'\n'+seen.join('\n');
// Three messages at most from the start (11-20); MESSAGES and MEM stand at their floor (one message, one record) before the route box folds.
assert(first.msg3===90&&first.msg4===90&&first.msgfold>first.msg2&&first.msg2>=first.mem1&&first.mem1>=first.msg1&&first.msg1>first.fold,label);
assert(first.fold>first.scroll&&first.msg1>first.scroll,label);
// MEM and then MESSAGES go whole before the route box does (11-20); the question keeps its rows to the end.
assert(first.scroll>first.mem0&&first.mem0>first['mem-1']&&first['mem-1']>first.msg0&&first.msg0>first['msg-1']&&first['msg-1']>first.boxGone,label);
// With a question: its head and first row stay at every height (its second row goes early, with MEM's rows, before the messages fold).
const asked=rows=>rows.filter(s=>/1 QUESTIONS|조건을 계속|1 계속 · 2 멈춤/.test(s));
for(let h=40;h>=10;h--){const rows=sections(h,{q});
 assert(asked(rows).length>=2,h+'\n'+rows.join('\n'));
 assert(rows.at(-2).endsWith('⚙   ▾ │'),h);}
"""
        self.node(script, "panel-view.cjs")

    def test_the_border_value_sends_one_arc_round_the_frame(self):
        # 11-15 (panel polish 4): what was the default until 11-15 is now the setting `rule motion`'s
        # `border` value, unchanged (#1639):
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
// (11-18: these scenes read the moving frame's vanilla ramp, which a window with no known harness keeps; a working session of a known harness takes
// its harness's ramp, `session_shell_harness_spinner_1118.test.py`'s.)
const run=(o={})=>view.render({width:o.width||40,height:o.height||40,side:o.side!==false,fill:o.side!==false,harness:o.harness||'',
 model:{...head,tag:'[e9]',routes:[],route_count:0,messages:[],styles,...(o.model||{})},state:o.state,frame:o.frame||0,since:o.since,
 configuration:{rule_motion:'border',...(o.cfg||{})},background:o.background||'',questions:()=>[],...(o.extra||{})});
const text=r=>r.map(p=>p.text).join('');
assert.deepEqual(view.BORDER,{LIT:1,ARC:1/4,LAP:60,SHADES:4,BREATHE:48,BREATHE_STEPS:6,BREATHE_STEPS_256:3});
assert.deepEqual(view.FACES.dark.orbit,[230,187,144,101]);assert.deepEqual(view.FACES.light.orbit,[22,65,108,151]);
assert.deepEqual(view.FACES.dark.breath,[217,210,174]);assert.deepEqual(view.FACES.light.breath,[88,124,131]);
// Every cell of a painted panel, by row and column, with the part it stands in.
const grid=v=>v.rows.map(r=>{const out=[];for(const p of r)for(const ch of p.text){out.push({ch,p});if(cells(ch)>1)out.push(null);}return out;});
// The frame's cells clockwise from the top-left corner, as [row, column].
const around=(W,H)=>{const out=[];for(let x=0;x<W;x++)out.push([0,x]);for(let y=1;y<H-1;y++)out.push([y,W-1]);
 for(let x=W-1;x>=0;x--)out.push([H-1,x]);for(let y=H-2;y>0;y--)out.push([y,0]);return out;};
// Each frame cell's look: 'text' (the head's words), 'base' (the neutral frame), or its arc shade 0..3.
const ramp=view.FACES.dark.orbit;
// (11-18: a working session's arc is its harness's ramp, `FACES.dark.ramp.<harness>`; a window with no known harness keeps `FACES.dark.orbit`.)
const looks=(v,h='')=>{const g=grid(v),W=g[0].length,H=g.length,ramp=view.FACES.dark.ramp[h]?.orbit||view.FACES.dark.orbit;return around(W,H).map(([y,x])=>{const c=g[y][x];
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
  if(h!==null&&!l.slice(0).some((x,i)=>x==='text'&&((h-i+P)%P)<=arc)){
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
// (11-22: the rule that stands still here and moves under `rule` is the SESSION head's: `SESSION ───…`, 28 cells of rule at 40 wide.)
const ruleRow=v=>v.rows.findIndex((r,i)=>i>0&&r.some(p=>p.rule));
for(const state of ['working','submitting','blocked']){
 const a=run({state,frame:5}),b=run({state,frame:state==='blocked'?17:6}),ga=grid(a),gb=grid(b),mc=new Set(a.motion.cells.map(c=>c.row+','+c.col));
 ga.forEach((r,y)=>r.forEach((c,x)=>{if(!c)return;if(mc.has(y+','+x)||c.p.spin)return;assert.deepEqual(c,gb[y][x],state+' '+y+','+x);}));
 const i=ruleRow(a);assert(i>0,state);
 for(const v of [a,b]){const parts=v.rows[i].filter(p=>p.rule);
  assert(parts.length===1&&parts[0].text==='─'.repeat(28)&&parts[0].role==='messages'&&parts[0].dim===true&&!parts[0].motion&&!parts[0].tone,state+' '+JSON.stringify(parts));}
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
for(const [state,f] of [['working',5],['blocked',10]]){const all=['claude','codex','opencode'].map(harness=>run({harness,state,frame:f})),hs=['claude','codex','opencode'];
 // (11-18: the same content and tones' steps; a working session's tone colors are its own harness's, so only those are set aside.)
 const alike=v=>v.rows.slice(1,-1).map(r=>r.slice(1,-1).map(p=>p.role==='orbit'&&state==='working'?{...p,foreground:null}:p));
 for(const [i,v] of all.slice(1).entries()){
  assert.deepEqual(alike(v),alike(all[0]),state);
  if(state==='blocked'){assert.deepEqual(v.rows.slice(1),all[0].rows.slice(1),state);continue;}
  const d=startOf(v)-startOf(all[0]),a=looks(v,hs[i+1]),b=looks(all[0],hs[0]),P=a.length;assert(d!==0);
  a.forEach((x,i)=>{const y=b[((i-d)%P+P)%P];if(x!=='text'&&y!=='text')assert.equal(x,y,state+' '+i);});}}
// (j) Short of rows (the rule goes, spec rule 34 (c)) the frame keeps moving; the bottom layout moves alike.
for(let h=30;h>=8;h-=4){const v=run({state:'working',frame:7,height:h});assert(v.motion&&v.motion.cells.length===2*40+2*(h-2)-text(v.rows[0]).indexOf(' ─'),String(h));}
{const b=run({side:false,width:100,height:14,state:'working',frame:7});assert(b.motion&&b.motion.cells.some(c=>c.row===b.rows.length-1)&&b.motion.cells.some(c=>c.col===99));}
"""
        self.node(script, "panel-view.cjs")

    def test_the_rule_motion_setting_picks_how_a_running_session_moves(self):
        # 11-15 (panel polish 4, spec rules 25, 47, 51, 52): the setting `rule motion` has four values.
        # `rule` (the default): running, the outer frame stands still and lit one step below the
        # brightest (187) and a light passes over the head rule (#1639's predecessor, the user's
        # 11-13 values): its middle five cells brightest and four on each side one shade down each,
        # out past one end and back in 26 frames (2.6 s), then the dark rule rests 10 frames (1 s) and
        # the light comes in again on that side; through the rest the host neither wakes nor writes.
        # `scanners`: a wide wave flows round the frame, no edge anywhere (two neighbouring cells one
        # shade apart at most), three waves (fewer on a short frame), one place rising and falling once
        # every 56 frames, its darkest never the still gray. `border`: the arc (its own test). `off`:
        # nothing moves. Waiting, the whole frame breathes red under every value but `off`, the rule
        # stays plain; idle, the still frame. The `on` saved before 11-15 reads as `rule`. The values
        # to tune stand at the top of panel-view.cjs.
        script = self.FIXTURE + r"""
const fs=require('node:fs');
const styles={dim:{dim:true},g_spin:{foreground:230},g_blocked:{foreground:217},g_work_off:{foreground:150,dim:true},g_dead:{foreground:210}};
// (11-18: these scenes read the moving frame's vanilla ramp, which a window with no known harness keeps; a working session of a known harness takes
// its harness's ramp, `session_shell_harness_spinner_1118.test.py`'s.)
const run=(o={})=>view.render({width:o.width||40,height:o.height||40,side:o.side!==false,fill:o.side!==false,harness:o.harness||'',
 model:{...head,tag:'[e9]',routes:[],route_count:0,messages:[],styles,...(o.model||{})},state:o.state,frame:o.frame||0,since:o.since,
 configuration:{...(o.motion?{rule_motion:o.motion}:{}),...(o.cfg||{})},background:o.background||'',questions:()=>[],...(o.extra||{})});
const text=r=>r.map(p=>p.text).join('');
// The values to tune, at the top of the file.
const top=fs.readFileSync(process.argv[1],'utf8').split('const defaults=')[0];
assert(top.includes('const RULE_PEAK=5,RULE_FADE=4,RULE_SCAN=26,RULE_REST=10,BORDER_LIT=1;'),top);
assert(top.includes('const WAVE_COUNT=3,WAVE_LAP=56,WAVE_STEPS=10,WAVE_FLOOR=0.3,WAVE_MIN=24;'),top);
assert.deepEqual(view.RULE,{PEAK:5,FADE:4,SCAN:26,REST:10,EASE:1});assert.deepEqual(view.WAVE,{COUNT:3,LAP:56,STEPS:10,FLOOR:0.3,MIN:24});
assert.deepEqual(view.FACES.dark.scan,[243,246,249,252,255]);
// The setting: `rule` by default, four values beside `route_line`; the settings screen picks them.
assert.equal(view.defaults.rule_motion,'rule');assert.deepEqual(view.options.rule_motion,['rule','border','scanners','off']);
assert.deepEqual(['rule','border','scanners','off'].map(v=>view.change({...view.defaults,rule_motion:v},'rule_motion',1).rule_motion),['border','scanners','off','rule']);
{const i=view.fields.indexOf('rule_motion');assert(i>view.fields.indexOf('route_line'));
 const settings=view.render({width:40,height:24,side:true,model:{labels:{rule_motion:'rule motion'}},settings:{index:i,value:{...view.defaults}},questions:()=>[]});
 assert(text(settings.rows.flat()).includes('rule motion  ‹ rule ›')&&settings.hits.some(h=>h.action==='setting:'+i+':up'));}
// Every cell of a painted panel, the frame's line cells clockwise, the head rule's row.
const grid=v=>v.rows.map(r=>{const out=[];for(const p of r)for(const ch of p.text){out.push({ch,p});if(cells(ch)>1)out.push(null);}return out;});
const around=v=>{const g=grid(v),W=g[0].length,H=g.length,out=[];for(let x=0;x<W;x++)out.push(g[0][x]);for(let y=1;y<H-1;y++)out.push(g[y][W-1]);
 for(let x=W-1;x>=0;x--)out.push(g[H-1][x]);for(let y=H-2;y>0;y--)out.push(g[y][0]);return out.map(c=>c&&'┌┐└┘─│'.includes(c.ch)?c:null);};
const ring=v=>around(v).filter(Boolean);
const ruleAt=v=>v.rows.findIndex((r,i)=>i>0&&r.some(p=>p.rule));
// (11-22: the light runs over the SESSION head's rule alone, `RULE`'s 28 cells at 40 wide, not over its name or value.)
const RULE=28,ruleCells=v=>v.rows[ruleAt(v)].filter(p=>p.rule).flatMap(p=>[...p.text].map(ch=>({ch,p})));
const plainRule=v=>{const parts=v.rows[ruleAt(v)].filter(p=>p.rule);return parts.length===1&&parts[0].text==='─'.repeat(RULE)&&parts[0].role==='messages'&&parts[0].dim===true&&!parts[0].tone;};
// (a) `rule`, the default: the frame lit and still, the light over the rule.
const ramp=view.FACES.dark.scan,CYCLE=26+10-1;
const shades=frame=>{const v=run({state:'working',frame}),cs=ruleCells(v);assert(cs.length===RULE&&cs.every(c=>c.ch==='─'));
 return cs.map(c=>c.p.tone==='scan'?ramp.indexOf(c.p.foreground)+1:0);};
for(let f=0;f<2*CYCLE;f++){
 const v=run({state:'working',frame:f}),frame=ring(v);
 assert(frame.every(c=>c.p.role==='orbit'&&c.p.tone==='arc'&&c.p.level===1&&c.p.foreground===187&&c.p.dim!==true),f+' '+JSON.stringify(frame[3]));
 const sh=shades(f),lit=sh.map((x,i)=>x?i:-1).filter(i=>i>=0);
 for(let i=1;i<RULE;i++)assert(Math.abs(sh[i]-sh[i-1])<=1,f+' '+sh.join(''));
 if(lit.length&&lit[0]>0&&lit.at(-1)<RULE-1)assert.deepEqual(sh.slice(lit[0],lit.at(-1)+1),[1,2,3,4,5,5,5,5,5,4,3,2,1],f+' '+sh.join(''));
}
const dark=f=>shades(f).every(x=>x===0);
assert(dark(0)&&dark(13)&&dark(CYCLE)&&!dark(12)&&!dark(14),'the rule is dark at the turns, one dark beat at the far end');
assert(shades(1)[0]>0&&shades(25)[0]>0&&shades(12)[RULE-1]>0,'out and back in on the same side');
const darkRun=from=>{let n=0;while(dark(from+n))n++;return n;};
assert.equal(darkRun(26),10);assert.equal(darkRun(13),1);
for(const f of [0,5,13,20,26,30])assert.deepEqual(shades(f),shades(f+CYCLE),'one rest per round trip '+f);
// The light comes in on the paint that shows the turn (one frame on).
for(const since of [0,7,1000])assert.deepEqual(ruleCells(run({state:'working',frame:since,since})).map(c=>c.p.tone==='scan'?c.p.foreground:0),
 ruleCells(run({state:'working',frame:1})).map(c=>c.p.tone==='scan'?c.p.foreground:0),String(since));
// The host's cells: the rule's 28 cells only, painted as at that frame; through the rest no wake.
{const v=run({state:'working',frame:3}),y=ruleAt(v);
 assert(v.animated&&v.motion&&v.motion.cells.length===RULE&&v.motion.cells.every((c,i)=>c.row===y&&c.col===10+i&&c.ch==='─'));
 for(const f of [3,17,30,40]){const at=v.motion.at(f),painted=ruleCells(run({state:'working',frame:f}));at.forEach((p,i)=>assert.equal(p.foreground??null,painted[i].p.foreground??null,f+' '+i));}
 assert.equal(v.motion.next(3),4);assert.equal(v.motion.next(25),26);
 for(let f=26;f<=CYCLE;f++)assert.equal(v.motion.next(f),CYCLE+1,String(f));}
// Between two frames only the spinner's row and the rule's row change.
{const a=run({state:'working',frame:5}),b=run({state:'working',frame:6});
 assert.deepEqual(a.rows.map((r,i)=>JSON.stringify(r)===JSON.stringify(b.rows[i])?-1:i).filter(i=>i>=0),[0,ruleAt(a)]);}
// The old `on` reads as `rule`; a hand-edited unknown value too.
for(const v of ['on','spin'])for(const [state,f] of [['working',5],['blocked',9],['idle',3]])
 assert.deepEqual(run({state,frame:f,motion:v}).rows,run({state,frame:f}).rows,v+' '+state);
// (b) `scanners`: the wave. Each line cell's shade index: 24-bit colors in their order of brightness; 256 the ramp.
const waveShade=(c,colors)=>{assert(c.p.role==='orbit'&&c.p.tone==='wave'&&c.p.dim!==true,JSON.stringify(c.p));return c.p.level;};
for(const [o,P,n] of [[{},156,3],[{width:60},196,3],[{side:false,width:100,height:14},224,3],[{side:false,width:30,height:4},64,2],[{side:false,width:20,height:4},44,1]]){
 for(const colors of [256,1<<24]){
  const seq=[];
  for(let f=0;f<56;f++){const v=run({...o,state:'working',frame:f,motion:'scanners',model:{colors}}),all=around(v),cs=ring(v);
   // Each place's level (null behind the head's words), in thousandths.
   const at=all.map(c=>c?Math.round(waveShade(c,colors)*1000):null),q=at.filter(x=>x!==null);
   const pairs=at.map((x,i)=>[x,at[(i+1)%at.length]]).filter(([a,b])=>a!==null&&b!==null),unit=Math.min(...pairs.map(([a,b])=>Math.abs(a-b)).filter(d=>d>0));
   // No edge: two neighbouring cells (round the corners too) one shade apart at most.
   pairs.forEach(([a,b],i)=>assert(Math.abs(a-b)<=unit+1,JSON.stringify([o,colors,f,i,a,b,unit])));
   // Every line cell is lit: none is the still frame gray.
   assert(cs.every(c=>c.p.role!=='frame'));
   if(f===0){const full=at.map((x,i)=>x??null),top=Math.max(...q);
    // The crests: runs of the brightest level round the whole ring (the words' cells read as their neighbour's).
    const filled=full.map((x,i)=>{let k=i;while(full[k]===null)k=(k+full.length-1)%full.length;return full[k];});
    const crests=filled.filter((x,i)=>x===top&&filled[(i+filled.length-1)%filled.length]!==top).length;assert.equal(crests,n,JSON.stringify([o,colors]));
    const colorsSeen=new Set(cs.map(c=>c.p.foreground));
    if(colors>256&&P>=99)assert(colorsSeen.size>=8,JSON.stringify([o,[...colorsSeen]]));
    if(colors===256)assert([...colorsSeen].every(x=>[230,187,144,101].includes(x)),JSON.stringify([...colorsSeen]));}
   seq.push(at);}
  // One period: frame 56 is frame 0; each place rises and falls once (one crest run in 56 frames).
  {const looks=f=>ring(run({...o,state:'working',frame:f,motion:'scanners',model:{colors}})).map(c=>c.p.foreground);assert.deepEqual(looks(56),looks(0));assert.deepEqual(looks(56+17),looks(17));}
  const L=seq[0].length;for(const i of [L-1,L-10,Math.floor(L/2)]){const s=seq.map(q=>q[i]),top=Math.max(...s);assert.equal(s.filter((x,k)=>x===top&&s[(k+55)%56]!==top).length,1,JSON.stringify([o,colors,i,s]));}
 }
}
{const v=run({state:'working',frame:4,motion:'scanners'});assert(v.motion&&v.motion.cells.length===ring(v).length&&v.motion.next(4)===5);assert(plainRule(v));
 const t=view.render({width:40,height:40,side:true,model:{...head,styles,colors:1<<24},state:'working',frame:4,configuration:{rule_motion:'scanners'},questions:()=>[]});
 // The darkest point in 24-bit color is above the still frame's gray (#808080 on the dark face).
 const lows=ring(t).map(c=>c.p.foreground).filter(x=>/^#/.test(x)).map(x=>parseInt(x.slice(1,3),16));assert(Math.min(...lows)>0x80,JSON.stringify(lows.slice(0,5)));}
// (c) Waiting: the breath over the whole frame under every value but `off`, the rule plain; idle: still.
for(const motion of ['rule','border','scanners']){
 const w=run({state:'blocked',frame:0,since:0,motion});
 assert(ring(w).every(c=>c.p.role==='orbit'&&c.p.tone==='breath'),motion);assert(plainRule(w),motion);assert(w.motion&&w.motion.cells.length===ring(w).length);
 assert(ring(run({state:'blocked',frame:24,since:0,motion})).every(c=>c.p.foreground!==ring(w)[0].p.foreground),motion+' breathes');
 for(const state of ['idle','unknown',undefined]){const v=run({state,frame:7,motion});
  assert(ring(v).every(c=>c.p.role==='frame'&&c.p.dim===true),motion+' '+state);assert(plainRule(v));assert.equal(v.motion,null);
  assert.deepEqual(v.rows,run({state,frame:31,motion}).rows);}
}
// (d) `off`, mono, NO_COLOR, eight colors, the settings screen: nothing moves under any value.
for(const motion of ['rule','border','scanners','off'])for(const [cfg,model] of [[{},{}],[{theme:'mono'},{}],[{},{no_color:true}],[{},{colors:8}]]){
 if(motion!=='off'&&!cfg.theme&&!model.no_color&&!model.colors)continue;
 for(const state of ['working','blocked']){const a=run({state,frame:7,motion,cfg,model}),b=run({state,frame:37,motion,cfg,model});
  assert.equal(a.motion,null,JSON.stringify([motion,cfg,model,state]));assert.deepEqual(a.rows,b.rows);assert(plainRule(a));
  assert(!a.rows.flat().some(p=>p.role==='orbit'||p.tone),JSON.stringify([motion,cfg,model,state]));}}
// (e) A route failure never colors the frame or the rule.
{const dead={id:'x',title:'failed',kind:'dead',state:'dead',shape:'one-shot-owner',stages:[],elapsed:'1m',paint:{glyph:{text:'✕'}}};
 for(const motion of ['rule','scanners'])for(const state of ['working','blocked','idle']){
  const a=run({state,frame:9,motion}),b=run({state,frame:9,motion,model:{routes:[dead],route_count:1,route_label:'route 1 ✕1'}});
  assert.deepEqual(ring(b).map(c=>[c.p.foreground,c.p.tone]),ring(a).map(c=>[c.p.foreground,c.p.tone]),motion+' '+state);
  assert.deepEqual(ruleCells(b).map(c=>c.p.foreground??null),ruleCells(a).map(c=>c.p.foreground??null),motion+' '+state);}}
// (f) Claude, Codex and OpenCode draw the same frame and rule on the same frame.
for(const motion of ['rule','scanners'])for(const [state,f] of [['working',5],['blocked',10]]){const all=['claude','codex','opencode'].map(harness=>run({harness,state,frame:f,motion}));
 // (11-18: the moving tones' colors are the working session's own harness's; their steps and positions are the same.)
 const alike=v=>v.rows.slice(1,-1).map(r=>r.slice(1,-1).map(p=>p.role==='orbit'&&state==='working'?{...p,foreground:null}:p));
 for(const v of all.slice(1))assert.deepEqual(alike(v),alike(all[0]),motion+' '+state);}
// (g) A light face: the lit frame and the light take the face's ramps.
{const v=run({state:'working',frame:8,cfg:{theme:'light'}});assert(ring(v).every(c=>c.p.foreground===view.FACES.light.orbit[1]));
 assert(ruleCells(v).filter(c=>c.p.tone==='scan').every(c=>view.FACES.light.scan.includes(c.p.foreground)));}
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
c.update([{id:'exact',revision:'one',answerable:true,questions:[{id:'q',question:'Where should this result go?',free_text:true,choices:['Here','Another folder'],display:{head:[{text:'Question · answer needed'}],body:[[{text:'Where should this result go?'}]],options:[[{text:'1 Here · 2 Another folder'}]],source:[[{text:'this session'}]]}}]}]);
c.enter();c.input(Buffer.from('Draft answer'));
// 11-15: the editor reads in the popup over the window, at its width: a narrow window and 80 columns
// keep the draft and the keys in sight.
for(const [cols,rows] of [[40,16],[80,12],[60,10]]){
 const all=c.detail({budget:v.popupRoom(rows)+1,columns:v.popupWidth(cols)-4});
 const r=v.popup({cols,rows,title:all[0],body:all.slice(1),sections:['QUESTIONS']});
 const text=r.rows.map(row=>row.map(p=>p.text).join('')).join('\n');
 assert(text.includes('Draft answer'),text);assert(text.includes('Here')&&text.includes('Another folder'),text);
 // 11-22: short of rows the key hints give way before the choices and the answer row do.
 if(cols>=80&&rows>=12)assert(text.includes('Esc/Ctrl+] back'),text);
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
    """R27 in the route box (restored 11-15): sent and received requests stand beside the routes, once
    (MESSAGES draws them no more), a press on one opens the MESSAGES popup where it reads whole, and they
    leave the question, the messages and the draft alone."""

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
        # Restored 11-15 from v4.36.4, 11-17: `N ROUTE` in the route box's top line (the requests no tab of it), the request
        # rows in the box after the routes, under the `request` rule; MESSAGES keeps the messages alone and draws no request.
        self.node(r"""
for(const [width,height] of [[32,30],[37,30],[38,30],[80,24]]){
 const lines=text(render(width,height));
 // (11-17, route zones: the title counts the routes alone, the requests are the box's `request` zone.)
 assert(lines.some(s=>width<80?/^│ ╭─ 1 ROUTE ─+ ›─╮ │$/.test(s):/^│ ╭─ route 1 ─+ ›─╮ │$/.test(s)),lines.join('\n'));
 assert(!lines.some(s=>/REQUEST|request \d/.test(s)),lines.join('\n'));
 for(const mark of ['◂','→','✓'])assert(lines.filter(s=>s.includes(mark)).length===1,mark+width+'\n'+lines.join('\n'));
 assert(!lines.join('').includes('⏳')&&!lines.join('').includes('⌛'));
 assert(lines.some(s=>s.includes('MSG_KEPT')),'messages stay at '+width);
 // Requests stand once, in the box: MESSAGES counts and shows the messages alone.
 const m=lines.findIndex(s=>/^│ (MESSAGES +|messages ─+ )› │$/.test(s)),box=lines.findIndex(s=>s.includes('╰'));
 assert(m>box&&box>0&&lines.slice(m+1).every(s=>!/[◂✓]|→ \[52\]/.test(s)),lines.join('\n'));
 assert(lines.length<=height);
}
const none=text(render(37,30,{},{...base,requests:[]}));
assert(none.some(s=>s.includes('1 ROUTE'))&&!none.join('').includes('REQUEST'));
// Spec rules 57, 58 (11-17: the tag is gone, the title is the route count in its state color): the box's
// `request` zone is named by a dim rule of its own.
const rows0=v=>text(v).filter(s=>/^│ │ ─ request ─+ │ │$/.test(s));
const styled={...base,routes:[{...routes[0],state:'idle',kind:'idle'}],styles:{dim:{dim:true},g_idle:{fg:186}}};
for(const width of [32,38]){
 const v=render(width,30,{},styled),parts=v.rows.flat();
 assert(!parts.some(p=>/REQUEST/.test(p.text)),'no request tab in the title');
 const rule=parts.find(p=>p.text==='request'&&p.role==='zone');
 assert(rows0(v).length===1&&rule,JSON.stringify(parts.filter(p=>/request/.test(p.text))));
 const count=parts.find(p=>/^\d+ ROUTE$/.test(p.text));
 assert(count&&count.fg===186,JSON.stringify(count));
 // A press on a request's row opens the MESSAGES popup (as the request rows of MESSAGES did).
 const rows=text(v),at=rows.findIndex(s=>s.includes('◂'));
 assert(at>0&&v.hits.some(h=>h.row===at&&h.action==='popup:MESSAGES'),JSON.stringify(v.hits));
}
""")

    def test_short_of_rows_the_box_scrolls_and_puts_work_to_answer_first(self):
        # Restored 11-15 from v4.36.4 (its press now the MESSAGES popup).
        self.node(r"""
const many=Array.from({length:9},(_,i)=>q('rq-00000'+i,i===5?'◂':'→','[e'+i+']','일 '+i,'전달됨',i===5?'request-in':'request-out'));
const v=render(37,16,{},{...base,requests:many});
const lines=text(v);
assert(lines.some(s=>s.includes('↓')||s.includes('↑')),'the border counts the rows it hides');
assert(lines.some(s=>s.includes('◂ [e5]')),'the received work to answer is among the first shown\n'+lines.join('\n'));
assert(lines.some(s=>s.includes('MSG_KEPT')));
const wheel=render(37,16,{routeScroll:1},{...base,requests:many});
assert.notDeepEqual(text(wheel),lines);
""")
    def test_a_long_status_cuts_the_title_first_and_keeps_the_other_session(self):
        # r3: `◂ 검토 제목 1c… · partial answer` lost its `[6e]`; the title is what gives way.
        # (11-16: beside the native area a request reads as a route's doer: its head row `◂ [6e]` with the age at
        # the right edge, then the title, then the status on a row of its own; short of rows the title and the
        # status share a row and the title is the part cut: the other session and the status stay in every form.
        # 11-17: with the zone rules the shortest such panel holds 11 rows, not 9.)
        self.node(r"""
// (11-17: the route's and the request's named rules take a row each; 11 rows is the shortest panel that still holds the request's head and
// its title and status row, short of which the box scrolls or the request reads on one row.)
for(const width of [32,37,38])for(const height of [30,11])for(const status of ['partial answer','final answer','open'])for(const title of ['검토 제목 1c9036d9 길게 이어지는 제목','review title 1c9036d9 and more words'])for(const [glyph,kind] of [['◂','request-in'],['✓','request-done']]){
 const model={...base,messages:[],requests:[q('rq-aaaaaa',glyph,'[6e]',title,status,kind)]};
 const lines=text(render(width,height,{},model));
 const at=lines.findIndex(s=>s.includes(glyph+' [6e]'));
 const label=[width,height,status,glyph].join(' ')+'\n'+lines.join('\n');
 assert(at>0,'the head row stands with the other session: '+label);
 const rows=lines.slice(at,at+3).join('\n');
 assert(rows.includes(status),'the status stays: '+label);
 assert(rows.includes('…')||rows.includes(title),'the title is cut with an ellipsis: '+label);
 if(height===11)assert(lines[at+1].includes('· '+status),'short of rows the status joins the title row: '+label);
}
// A title that fits stays whole beside the session and the status.
const whole=text(render(38,11,{},{...base,messages:[],requests:[q('rq-aaaaaa','◂','[6e]','짧은 일','final answer','request-in')]}));
assert(whole.some(s=>s.includes('짧은 일 · final answer')),whole.join('\n'));
""")

    def test_a_request_alone_still_draws_the_box_and_a_long_url_title_is_cut_whole(self):
        self.node(r"""
const lone=text(render(32,24,{},{...base,routes:[],route_count:0,requests:[
 q('rq-dddddd','→','[52]','https://example.com/'+'a'.repeat(80),'전달됨','request-out')]}));
// (11-17, route zones: a request alone reads `ROUTE` in the title; the one `request` rule stands, no `dispatch` one: no route stands above.)
assert(lone.some(s=>/^│ ╭─ ROUTE ─+ ›─╮ │$/.test(s))&&!lone.some(s=>/REQUEST/.test(s)),lone.join('\n'));
assert(lone.filter(s=>/^│ │ ─ request ─+ │ │$/.test(s)).length===1&&!lone.some(s=>/─ (dispatch|direct) ─/.test(s))&&!lone.some(s=>s.includes('├')),lone.join('\n'));
const at=lone.findIndex(s=>s.includes('→ [52]'));
assert(at>0&&lone[at+1].includes('…')&&lone[at+2].includes('전달됨'),lone.join('\n'));
assert(lone.every(s=>[...s].length<=32));
""")


if __name__ == "__main__":
    unittest.main()
