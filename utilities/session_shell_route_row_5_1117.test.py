"""11-17 (the person: route row sketch 5): the shell panel's route lines read as the sketch draws them.

Three scenes of one grammar, everything synthetic:
  * a route that continues a frame: `frame ✓ → code(standard)` / title / owner / CTX / `staged 3/6: execute` / the workers
    hung under the owner by `↳` (no box);
  * a frame route: `frame → ??`, its two legs hung straight under the route (`↳ frame`, `↳ frame-alt`); when the legs are
    done it waits for the direction (`◑ frame → ?? · waiting for direction`, not blinking);
  * a route its owner does alone: `code(quick)` / title / owner (with its open tool) / CTX, no stage line.
The kind word is the one bold word, the intensity stands in dim brackets (nothing else goes in them), and exactly one thing
blinks per route: the stage name when a stage line stands, else the first line's dot. Failed, finished and narrow forms keep
the same grammar. The bottom layout and the three harnesses draw the same words.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"

FIXTURE = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]+'/panel-view.cjs');
const P=t=>({text:t}),D=t=>({text:t,dim:true}),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const names=['plan','plan-check','execute','impl-review','test','report'];
const six=at=>names.map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true,key:'stg2_on'}:{name,mark:''});
const doer=(h,m,e,pct,bar)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)]});
const worker=(h,m,e,pct,bar,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',...o,paint:{glyph:P('⠸'),...doer(h,m,e,pct,bar)}});
const leg=(stage,h,m,e,pct,bar)=>({kind:'working',role:'frame',stage,elapsed:'4m',paint:{glyph:P('⠸'),...doer(h,m,e,pct,bar)}});
const R={
 staged:{id:'a',title:'패널 route 줄 다시 짜기',kind:'working',state:'working',elapsed:'42m',shape:'staged',stages:six(2),kind_word:'code',intensity:'standard',role:'owner',
  chain:[{name:'frame',state:'done'},{name:'code',state:'active'}],paint:{glyph:P('⠸'),...doer('claude','Opus 5.5','xhigh','40%','━━━')},
  workers_live:[worker('claude','Sonnet 5.5','high','22%','━━',{tool:'Edit',target:'panel-view.cjs'}),worker('opencode','GLM-5.2','max','9%','━',{tool:'Bash',target:'run-tests.py'})]},
 frame:{id:'f',title:'보류된 메시지 조사',kind:'working',state:'working',elapsed:'4m',shape:'framed',stages:[],kind_word:'frame',role:'frame',chain:[{name:'frame',state:'active'}],
  paint:{glyph:P('⠸')},workers_live:[leg('frame','claude','Opus 5.5','xhigh','18%','━━'),leg('frame-alternative','codex','GPT-5.5','xhigh','11%','━')]},
 solo:{id:'s',title:'보류된 메시지 조사',kind:'working',state:'working',elapsed:'18m',shape:'one-shot-owner',stages:[{name:'one-shot',current:true,mark:'●'}],kind_word:'code',
  intensity:'quick',role:'owner',tool:'Bash',paint:{glyph:P('⠸'),...doer('codex','GPT-5.5','high','63%','━━━━━')},workers_live:[]},
};
R.wait={...R.frame,id:'w',kind:'blocked',state:'blocked',paint:{glyph:P('◑')},workers_live:[]};
R.failed={...R.staged,id:'x',kind:'dead',state:'dead',paint:{glyph:P('✕'),...doer('claude','Opus 5.5','xhigh','40%','━━━')},
  stages:[{name:'plan',mark:'✓'},{name:'execute',mark:'✕'},{name:'test',mark:''}],workers_live:[]};
R.done={...R.solo,id:'d',kind:'done',state:'done',outcome:'PASS',outcome_key:'lvl_g',tool:undefined,paint:{glyph:P('✓')}};
const STYLES={dim:{dim:true},loc_repo:{foreground:253},stg2_on:{foreground:150,bold:true},g_dead:{foreground:196,bold:true},lvl_g:{foreground:114},lvl_r:{foreground:196,bold:true},g_spin:{foreground:150}};
const base={repo:[P('hearting/')],messages:[],styles:STYLES};
const render=(routes,width,height,{side=true,model={},configuration={},harness='claude',frame=0}={})=>view.render({width,height,side,fill:side,harness,frame,
 model:{...base,routes,route_count:routes.length,route_total:routes.length,...model},state:'working',configuration,questions:()=>[]});
const text=r=>r.map(p=>p.text).join('');
const plain=v=>v.rows.map(text);
const draw=(routes,width,height,o)=>plain(render(routes,width,height,o));
// The box's rows, the frame and the box's own sides taken off.
const box=rows=>{const a=rows.findIndex(s=>/╭─ \d+ ROUTE/.test(s)),z=rows.findIndex((s,i)=>i>a&&s.startsWith('│ ╰'));
  return rows.slice(a+1,z).map(s=>s.replace(/^│ │ ?/,'').replace(/ ?│ │$/,'').replace(/\s+$/,''));};
const only=(route,width=44,o={})=>box(draw([route],width,60,o));
const rowParts=(v,re)=>v.rows.find(r=>re.test(text(r)));
// (The box's title `1 ROUTE` pulses while a route runs; it is not a route's line.)
const beats=(route,o={})=>render([route],44,60,o).rows.flat().filter(p=>p.beat===true&&!/ROUTE$/.test(p.text));
'''


class RouteRow5Test(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_route_that_continues_a_frame_reads_kind_title_owner_stage_and_hung_workers(self):
        self.node(r"""
assert.deepEqual(only(R.staged),[
 '● frame ✓ → code(standard)',
 '  패널 route 줄 다시 짜기',
 '  ● owner  claude Opus 5.5 xhigh',
 '    CTX    ━━━───── 40% · 42m',
 '    staged 3/6: execute',
 '    ↳ ⠋ worker claude Sonnet 5.5',
 '        CTX    ━━────── 22% · 12m',
 '        execute · Edit',
 '    ↳ ⠋ worker opencode GLM-5.2 max',
 '        CTX    ━───────  9% · 12m',
 '        execute · Bash run-tests.py',
]);
// No box: no frame characters inside the route's rows, and a worker's rows hang at 4 / 8 cells.
const rows=only(R.staged);
assert(!rows.some(s=>/[╭╮╰╯├┤│]/.test(s)),rows.join('\n'));
const col=(s,w)=>[...s.slice(0,s.indexOf(w))].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
assert.equal(col(rows[5],'↳'),4);assert.equal(col(rows[6],'CTX'),8);assert.equal(col(rows[7],'execute'),8);
assert.equal(col(rows[2],'owner'),4);assert.equal(col(rows[3],'CTX'),4);assert.equal(col(rows[3],'━'),col(rows[2],'claude'));
assert.equal(col(rows[6],'━'),col(rows[5],'claude'));
// The same route at a wider panel reads whole.
const wide=only(R.staged,60);
assert.equal(wide[5],'    ↳ ⠋ worker claude Sonnet 5.5 high');
assert.equal(wide[7],'        execute · Edit panel-view.cjs');
""")

    def test_a_frame_route_hangs_its_two_legs_under_it_and_waits_for_the_direction(self):
        self.node(r"""
assert.deepEqual(only(R.frame,50),[
 '● frame → ??',
 '  보류된 메시지 조사',
 '  ↳ ⠋ frame     claude Opus 5.5 xhigh',
 '      CTX       ━━────── 18% · 4m',
 '  ↳ ⠋ frame-alt codex GPT-5.5 xhigh',
 '      CTX       ━─────── 11% · 4m',
]);
// Done, the two legs gone and the answer asked for: the mark `◑`, no legs, the word; it does not blink.
assert.deepEqual(only(R.wait,50),['◑ frame → ?? · waiting for direction','  보류된 메시지 조사']);
assert.equal(beats(R.wait).length,0);
// In the person's language the word is the word list's (the backend's labels).
const ko=only(R.wait,50,{model:{labels:{direction_wait:'방향 확인 대기'}}});
assert.equal(ko[0],'◑ frame → ?? · 방향 확인 대기');
// Short of rows the legs fold into one row each (no CTX rows); the first line and the title stay.
for(let h=40;h>=14;h--){
 const rows=box(draw([R.frame],50,h));
 if(!rows.length)break;
 assert(rows[0]==='● frame → ??'&&rows[1]==='  보류된 메시지 조사',h+'\n'+rows.join('\n'));
 if(!rows.some(s=>/CTX/.test(s))){assert(rows.filter(s=>/↳/.test(s)).length===2,h+'\n'+rows.join('\n'));break;}
}
""")

    def test_a_route_its_owner_does_alone_has_no_stage_line_and_its_tool_stands_on_the_owner_row(self):
        self.node(r"""
assert.deepEqual(only(R.solo),[
 '● code(quick)',
 '  보류된 메시지 조사',
 '  ⠋ owner codex GPT-5.5 high · Bash',
 '    CTX   ━━━━━─── 63% · 18m',
]);
// Its file name joins the tool where the row has room.
assert.equal(only({...R.solo,target:'utilities/session-shell/host.cjs',tool:'Edit'},60)[2],'  ⠋ owner codex GPT-5.5 high · Edit host.cjs');
// No kind line text of the old forms anywhere.
for(const old of ['code ·','one-shot','staged 3/6 ·','· execute'])assert(!only(R.solo).join('\n').includes(old),old);
""")

    def test_the_kind_is_the_one_bold_word_and_the_brackets_hold_only_the_intensity(self):
        self.node(r"""
for(const [label,o] of [['256',{}],['light',{configuration:{theme:'light'}}],['mono',{configuration:{theme:'mono'}}],['8 colors',{model:{colors:8}}],['NO_COLOR',{model:{no_color:true}}]]){
 for(const route of [R.staged,R.solo]){
  const row=rowParts(render([route],44,60,o),/code\(/),by=t=>row.find(p=>p.text===t);
  const kind=by('code');assert(kind&&kind.bold===true&&kind.dim!==true,label+JSON.stringify(kind));
  assert(kind.foreground===null||kind.foreground===undefined||[253,237].includes(kind.foreground),label+JSON.stringify(kind));
  const inten=row.find(p=>/^\(\w+\)$/.test(p.text));assert(inten&&inten.bold!==true&&(label!=='256'||inten.dim===true),label+JSON.stringify(row));
  // The chain behind it is dim, its done mark in it.
  if(route===R.staged){const past=row.find(p=>p.text==='frame ✓');assert(past&&past.bold!==true&&(label!=='256'||past.dim===true),label+JSON.stringify(row));}
 }
}
// The brackets carry the intensity and nothing else: no mode, no scope, no shape.
assert.equal(only({...R.staged,mode:'debug',visibility:{next:'x'}})[0],'● frame ✓ → code(standard)');
// A route whose record names no intensity has no brackets; a direct route's word stands where the record names none.
assert.equal(only({...R.solo,intensity:undefined})[0],'● code');
assert.equal(only({id:'d',title:'작은 수정',kind:'working',state:'working',elapsed:'3m',shape:'direct',stages:[],kind_word:'code',paint:{glyph:P('⠋')}})[0],'● code(direct)');
assert.equal(only({id:'e',title:'작은 수정',kind:'working',state:'working',elapsed:'3m',shape:'direct',stages:[],kind_word:'code',intensity:'direct',paint:{glyph:P('⠋')}})[0],'● code(direct)');
// Several kinds in one plan: what is behind is dim with `✓`, this one bold, what is left is not drawn (the chain is what the record holds).
assert.equal(only({...R.staged,chain:[{name:'research',state:'done'},{name:'draft',state:'done'},{name:'code',state:'active'}]},60)[0],'● research ✓ → draft ✓ → code(standard)');
""")

    def test_exactly_one_thing_blinks_per_route(self):
        self.node(r"""
// With a stage line the stage name blinks and the first line's dot stands still.
const staged=beats(R.staged);
assert.equal(staged.length,1,JSON.stringify(staged));assert.equal(staged[0].text,'execute');
// Without one the dot does.
const solo=beats(R.solo);assert.equal(solo.length,1);assert.equal(solo[0].text,'●');
const frame=beats(R.frame);assert.equal(frame.length,1);assert.equal(frame[0].text,'●');
// A stage line that goes (short of rows) hands the blink to the dot: the one thing that blinks is always on a row that stands.
let handed=false;
for(let h=40;h>=10;h--){
 const v=render([R.staged],44,h),own=v.rows.flat().filter(p=>p.beat===true&&!/ROUTE$/.test(p.text)),shown=plain(v);
 if(!shown.some(s=>/● frame|● code|⠋ .*frame/.test(s)))break;
 assert(own.length<=1,h+' '+JSON.stringify(own));
 if(shown.some(s=>/staged 3\/6|3\/6: execute/.test(s)))assert(own.length===1&&own[0].text==='execute',h);
 else if(own.length===1){assert.equal(own[0].text,'●',h);handed=true;}
}
assert(handed,'no height hands the blink from the stage name to the dot');
// Nothing blinks while the route waits, fails or is done; the lit and the dark beat differ only in the blinking part.
for(const route of [R.wait,R.failed,R.done])assert.equal(beats(route).length,0,route.id);
const lit=rowParts(render([R.staged],44,60,{frame:0}),/staged 3\/6/),dark=rowParts(render([R.staged],44,60,{frame:10}),/staged 3\/6/);
assert.notDeepEqual(lit.find(p=>p.text==='execute'),dark.find(p=>p.text==='execute'));
assert.deepEqual(lit.filter(p=>p.text!=='execute'),dark.filter(p=>p.text!=='execute'));
""")

    def test_failed_and_finished_routes_keep_the_grammar(self):
        self.node(r"""
assert.deepEqual(only(R.failed),[
 '✕ frame ✓ → code(standard)',
 '  패널 route 줄 다시 짜기',
 '  ✕ owner claude Opus 5.5 xhigh',
 '    CTX   ━━━───── 40% · 42m',
 '    staged 2/3: execute ✕',
]);
const done=only(R.done);
assert.equal(done[0],'✓ code(quick)');assert.equal(done[1],'  PASS · 보류된 메시지 조사');
""")

    def test_narrow_widths_drop_values_from_the_back_and_keep_every_row_in_its_width(self):
        self.node(r"""
// The chain goes first (what is behind), then the intensity; the kind and the stage stay.
assert.equal(only(R.staged,60)[0],'● frame ✓ → code(standard)');
assert.equal(only(R.staged,30)[0],'● code(standard)');
assert.equal(only(R.staged,24)[0],'● code(standard)');
assert.equal(only(R.staged,22)[0],'● code');
for(const width of [22,24,26,30,36,44,60]){
 for(const route of [R.staged,R.frame,R.wait,R.solo,R.failed,R.done]){
  const rows=draw([route],width,60);
  assert(rows.every(s=>cells(s)===width),width+' '+route.id+'\n'+rows.join('\n'));
  const inner=box(rows);
  assert(inner.length>=2,width+' '+route.id);
 }
}
// The stage line shortens `staged` first and keeps the stage; a worker's percent is never cut in the middle.
assert(only(R.staged,30).includes('    3/6: execute'),only(R.staged,30).join('\n'));
for(const s of only(R.staged,24))assert(!/CTX\s+\d$/.test(s),s);
""")

    def test_the_bottom_layout_reads_kind_intensity_and_stage_in_one_cell(self):
        self.node(r"""
const cell=(rows,title)=>{const s=rows.find(x=>x.startsWith('│ │ ')&&x.includes(title));return s.split(' │ ')[4].trim().replace(/ +\d+[mh]$/,'');};
const wide=draw([R.staged,R.solo,R.failed],140,16,{side:false});
assert.equal(cell(wide,'패널').replace(/ +$/,''),'code(standard) · staged 3/6: execute');
assert(wide.some(s=>/code\(quick\)/.test(s)));
assert(wide.some(s=>/code\(standard\) · staged 2\/3: execute ✕/.test(s)),wide.join('\n'));
// Short of cells the kind goes first, then the word `staged`.
const mid=draw([R.staged,R.failed],80,16,{side:false});
assert(mid.some(s=>/3\/6: execute/.test(s))&&!mid.join('').includes('code('),mid.join('\n'));
for(const s of [...wide,...mid])assert([140,80].includes(cells(s)),s);
""")

    def test_every_route_with_a_stage_list_reads_the_stage_line_under_its_owner(self):
        # Research (map-reduce), refine, ship and spec (transactional-owner) and a resumed route (resource+verification) read
        # the stage line a staged route does, with their own shape word (never translated); none when the record names none.
        self.node(r"""
const three=['search','analyze','report'].map((n,i)=>i<1?{name:n,mark:'✓'}:i===1?{name:n,mark:'●',current:true,key:'stg2_on'}:{name:n,mark:''});
const mk=(shape,o={})=>({...R.staged,id:'m',shape,stages:three,kind_word:'research',chain:[{name:'research',state:'active'}],workers_live:[],...o});
const want=(shape,line,o)=>{const rows=only(mk(shape,o),50);
 assert.deepEqual(rows,['● research(standard)','  패널 route 줄 다시 짜기','  ⠋ owner claude Opus 5.5 xhigh','    CTX   ━━━───── 40% · 42m',line],rows.join('\n'));};
want('map-reduce','    map-reduce 2/3: analyze');
want('transactional-owner','    transactional-owner 2/3: analyze');
want('resource+verification','    resource+verification 2/3: analyze',{chain:[{name:'research',state:'active'}]});
want('unknown','    2/3: analyze');
// One blinking thing, the stage's name; the first line's dot stands still.
for(const shape of ['map-reduce','transactional-owner','resource+verification']){
 const own=beats(mk(shape));assert.equal(own.length,1,shape);assert.equal(own[0].text,'analyze',shape);
}
// Short of cells the shape word goes first, the position and the stage stay; every row keeps its width.
assert(only(mk('transactional-owner'),30).includes('    2/3: analyze'),only(mk('transactional-owner'),30).join('\n'));
for(const width of [22,26,30,36,44])for(const shape of ['map-reduce','transactional-owner','resource+verification']){
 const rows=draw([mk(shape)],width,60);assert(rows.every(s=>cells(s)===width),width+shape+'\n'+rows.join('\n'));
 assert(box(rows).some(s=>/2\/3: anal/.test(s)),width+shape+'\n'+rows.join('\n'));
}
// A frame route and a route its owner does alone keep no stage line.
assert(!only(R.frame,50).some(s=>/\d\/\d/.test(s))&&!only(R.solo,50).some(s=>/\d\/\d/.test(s)));
// The bottom layout names the shape word in its one cell.
const wide=draw([mk('map-reduce'),mk('transactional-owner')],140,16,{side:false});
assert(wide.some(s=>/research\(standard\) · map-reduce 2\/3: analyze/.test(s))&&wide.some(s=>/transactional-owner 2\/3: analyze/.test(s)),wide.join('\n'));
""")

    def test_a_route_with_no_known_owner_keeps_its_time_and_context_and_a_resting_owner_its_last_tool(self):
        self.node(r"""
const none=(o={})=>({id:'n',title:'소유자를 모르는 route',kind:'working',state:'working',elapsed:'23m',shape:'one-shot-owner',stages:[{name:'one-shot',current:true,mark:'●'}],
 kind_word:'code',intensity:'quick',paint:{glyph:P('⠋'),ctx:P('40%'),gauge_row:[P('━━━'),D('─────'),D(' '),P('40%')]},...o});
// The time and the context are drawn though no owner is named (with the row's gauge, or the time alone).
const a=only(none(),50);assert(a.some(s=>/━━━─+ 40%\s+23m$/.test(s)),a.join('\n'));
const b=only(none({paint:{glyph:P('⠋')}}),50);assert(b.some(s=>/^\s+23m$/.test(s)),b.join('\n'));
const c=only(none({shape:'map-reduce',stages:[{name:'search',mark:'✓'},{name:'analyze',mark:'●',current:true}],paint:{glyph:P('⠋')}}),50);
assert(c.some(s=>/^\s+23m$/.test(s))&&c.some(s=>/map-reduce 2\/2: analyze/.test(s)),c.join('\n'));
// An owner at rest names the tool it ended on, at the end of its row; a running one names its open tool.
const rest={...R.solo,kind:'idle',state:'idle',tool:undefined,after_tool:'Bash',paint:{glyph:P('●'),...doer('codex','GPT-5.5','high','63%','━━━━━')}};
assert.equal(only(rest,50)[2],'  ● owner codex GPT-5.5 high · after Bash');
assert.equal(only({...rest,after_tool:undefined},50)[2],'  ● owner codex GPT-5.5 high');
assert.equal(only(R.solo,50)[2],'  ⠋ owner codex GPT-5.5 high · Bash');
""")

    def test_the_three_harness_panels_draw_the_same_rows(self):
        self.node(r"""
for(const route of [R.staged,R.frame,R.solo]){
 const want=only(route);
 for(const harness of ['claude','codex','opencode'])assert.deepEqual(only(route,44,{harness}),want,route.id+' '+harness);
}
""")


if __name__ == "__main__":
    unittest.main()
