"""11-17 (the person: route row sketch 5): the shell panel's route lines read as the sketch draws them.

Three scenes of one grammar, everything synthetic:
  * a route that continues a frame: `frame ✓ → code(standard)` / title / owner (its mark, role, harness and model, with its `CTX 40%`
    row under it) / its NOW / the workers and the stage line under the owner, each worker with its `CTX 22% · 12m` row and its own
    NOW under it. The default panel setting `worker_box` (`closed`) draws the workers in a box whose top line is the stage line
    (`┌ staged 3/6: execute ──┐`); `none` is the old form (`staged 3/6: execute` as a row, the workers hung by `↳`), which most of
    the scenes below read in;
  * a frame route: `frame → ??`, its two legs hung straight under the route (`↳ frame`, `↳ frame-alt`, no box); when the legs are
    done it waits for the direction (`◑ frame → ?? · waiting for direction`, not blinking);
  * a route its owner does alone: `code(quick)` / title / owner / its `CTX` row / its NOW, no stage line.
A session shows a time only where it has its own (a worker's, on its CTX row); an owner never does: the route's time is the one at
the right end of its first line.
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
// (11-17: a session's lines are its who row, `CTX 40% · 42m` beside the model with no gauge, and under it its NOW, `paint.now`: a plain string.)
const doer=(h,m,e,pct,now)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),...(now?{now}:{})});
const worker=(h,m,e,pct,now,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',...o,paint:{glyph:P('⠸'),...doer(h,m,e,pct,now)}});
const leg=(stage,h,m,e,pct,now)=>({kind:'working',role:'frame',stage,elapsed:'4m',paint:{glyph:P('⠸'),...doer(h,m,e,pct,now)}});
const R={
 staged:{id:'a',title:'패널 route 줄 다시 짜기',kind:'working',state:'working',elapsed:'42m',shape:'staged',stages:six(2),kind_word:'code',intensity:'standard',role:'owner',
  chain:[{name:'frame',state:'done'},{name:'code',state:'active'}],paint:{glyph:P('⠸'),...doer('claude','Opus 5.5','xhigh','40%','Waiting on two workers')},
  workers_live:[worker('claude','Sonnet 5.5','high','22%','Edit panel-view.cjs',{tool:'Edit',target:'panel-view.cjs'}),worker('opencode','GLM-5.2','max','9%','Bash run-tests.py',{tool:'Bash',target:'run-tests.py'})]},
 frame:{id:'f',title:'보류된 메시지 조사',kind:'working',state:'working',elapsed:'4m',shape:'framed',stages:[],kind_word:'frame',role:'frame',chain:[{name:'frame',state:'active'}],
  paint:{glyph:P('⠸')},workers_live:[leg('frame','claude','Opus 5.5','xhigh','18%','Reading the held messages'),leg('frame-alternative','codex','GPT-5.5','xhigh','11%')]},
 solo:{id:'s',title:'보류된 메시지 조사',kind:'working',state:'working',elapsed:'18m',shape:'one-shot-owner',stages:[{name:'one-shot',current:true,mark:'●'}],kind_word:'code',
  intensity:'quick',role:'owner',tool:'Bash',paint:{glyph:P('⠸'),...doer('codex','GPT-5.5','high','63%','Bash run-tests.py')},workers_live:[]},
};
R.wait={...R.frame,id:'w',kind:'blocked',state:'blocked',paint:{glyph:P('◑')},workers_live:[]};
R.failed={...R.staged,id:'x',kind:'dead',state:'dead',paint:{glyph:P('✕'),...doer('claude','Opus 5.5','xhigh','40%')},
  stages:[{name:'plan',mark:'✓'},{name:'execute',mark:'✕'},{name:'test',mark:''}],workers_live:[]};
R.done={...R.solo,id:'d',kind:'done',state:'done',outcome:'PASS',outcome_key:'lvl_g',tool:undefined,paint:{glyph:P('✓')}};
const STYLES={dim:{dim:true},loc_repo:{foreground:253},stg2_on:{foreground:150,bold:true},g_dead:{foreground:196,bold:true},lvl_g:{foreground:114},lvl_r:{foreground:196,bold:true},g_spin:{foreground:150}};
const base={repo:[P('hearting/')],messages:[],styles:STYLES};
const render=(routes,width,height,{side=true,model={},configuration={},harness='claude',frame=0}={})=>view.render({width,height,side,fill:side,harness,frame,
 model:{...base,routes,route_count:routes.length,route_total:routes.length,...model},state:'working',configuration,questions:()=>[]});
// (11-18: a route's time stands at the right end of its first line: `tm(line,time,width)` is that line.)
const tm=(s,time,w=44)=>s+' '.repeat(Math.max(1,w-8-cells(s)-cells(time)))+time;
const text=r=>r.map(p=>p.text).join('');
const plain=v=>v.rows.map(text);
const draw=(routes,width,height,o)=>plain(render(routes,width,height,o));
// The box's rows, the frame and the box's own sides taken off (and the zone rules `─ dispatch ───`: this test reads the routes).
const box=rows=>{const a=rows.findIndex(s=>/╭─ \d+ ROUTE/.test(s)),z=rows.findIndex((s,i)=>i>a&&s.startsWith('│ ╰'));
  return rows.slice(a+1,z).map(s=>s.replace(/^│ │ ?/,'').replace(/ ?│ │$/,'').replace(/\s+$/,'')).filter(s=>!/^─ (direct|dispatch|request) ─+$/.test(s));};
const only=(route,width=44,o={})=>box(draw([route],width,60,o));
// (11-18: `worker_box: none` draws the workers hung under the owner by `↳` with the stage line as a row of its own, the form these scenes were written in.)
const NB={configuration:{worker_box:'none'}};
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
// (11-18: each session is its who row, its `CTX 40%` row under it (a worker's `CTX 22% · 12m` with its own time; an owner's never has one) and its NOW,
// up to two rows; a worker's stage and open tool are not drawn: the stage line has the stage.)
// The default `worker_box` (closed): the workers sit in a box under the owner, the stage line on its top line, no stage row of its own.
assert.deepEqual(only(R.staged),[
 tm('● frame ✓ → code(standard)','42m'),
 '  패널 route 줄 다시 짜기',
 '  ● owner  claude Opus 5.5 xhigh',
 '    CTX 40%',
 '    Waiting on two workers',
 '  ┌ staged 3/6: execute ───────────┐',
 '  │ ⠋ worker claude Sonnet 5.5     │',
 '  │   CTX 22% · 12m                │',
 '  │   Edit panel-view.cjs          │',
 '  │ ⠋ worker opencode GLM-5.2 max  │',
 '  │   CTX 9% · 12m                 │',
 '  │   Bash run-tests.py            │',
 '  └────────────────────────────────┘',
]);
const boxed=only(R.staged),colOf=(s,w)=>[...s.slice(0,s.indexOf(w))].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
assert.equal(colOf(boxed[5],'┌'),2);assert.equal(colOf(boxed[6],'│'),2);assert.equal(colOf(boxed[6],'⠋'),4);assert.equal(colOf(boxed[7],'CTX'),6);assert.equal(colOf(boxed[8],'Edit'),6);
assert(!boxed.some(s=>/↳/.test(s))&&!boxed.some(s=>/^ {4}staged/.test(s)),boxed.join('\n'));
// `worker_box: none` is the old form: the stage row, the workers hung by `↳`, each one's rows at 8 cells.
assert.deepEqual(only(R.staged,44,NB),[
 tm('● frame ✓ → code(standard)','42m'),
 '  패널 route 줄 다시 짜기',
 '  ● owner  claude Opus 5.5 xhigh',
 '    CTX 40%',
 '    Waiting on two workers',
 '    staged 3/6: execute',
 '    ↳ ⠋ worker claude Sonnet 5.5',
 '        CTX 22% · 12m',
 '        Edit panel-view.cjs',
 '    ↳ ⠋ worker opencode GLM-5.2 max',
 '        CTX 9% · 12m',
 '        Bash run-tests.py',
]);
// No box: no frame characters inside the route's rows, and a worker's rows hang at 4 / 8 cells.
const rows=only(R.staged,44,NB);
assert(!rows.some(s=>/[╭╮╰╯├┤│┌└]/.test(s)),rows.join('\n'));
const col=(s,w)=>[...s.slice(0,s.indexOf(w))].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
assert.equal(col(rows[6],'↳'),4);assert.equal(col(rows[7],'CTX'),8);assert.equal(col(rows[8],'Edit'),8);assert.equal(col(rows[11],'Bash'),8);
assert.equal(col(rows[2],'owner'),4);assert.equal(col(rows[3],'CTX'),4);assert.equal(col(rows[4],'Waiting'),4);
// The NOW is bright while the session works, dim otherwise; the gauge and the worker's stage / tool rows are gone.
const nowOf=(route,re,o)=>rowParts(render([route],44,60,o),re).find(p=>p.text==='Waiting');   // (a NOW is drawn word by word: its first word wears the NOW's color)
assert.equal(nowOf(R.staged,/Waiting on/).foreground,253);assert.notEqual(nowOf(R.staged,/Waiting on/).dim,true);
assert.equal(nowOf({...R.staged,kind:'idle',state:'idle'},/Waiting on/).dim,true);
assert(!rows.some(s=>/━|─|execute ·|· Edit|· Bash/.test(s)),rows.join('\n'));
// An owner shows no time of its own at any width; a worker's time stands on its CTX row.
for(const w of [44,60])assert(!only(R.staged,w,NB).slice(2,5).some(s=>/42m/.test(s)),w);
// The same route at a wider panel keeps the same rows: the effort now fits on the worker's row too.
const wide=only(R.staged,60,NB);
assert.equal(wide[2],'  ● owner  claude Opus 5.5 xhigh');
assert.equal(wide[3],'    CTX 40%');
assert.equal(wide[6],'    ↳ ⠋ worker claude Sonnet 5.5 high');
assert.equal(wide[7],'        CTX 22% · 12m');
assert.equal(wide[8],'        Edit panel-view.cjs');
// Short of cells the effort goes first, then the harness, then the model; the NOW is cut with `…` on its second row.
const ownerRow=(w,o)=>only(R.staged,w,o).find(s=>/● owner/.test(s));   // (the title wraps to a second row at the narrow widths)
assert.equal(ownerRow(36,NB),'  ● owner  claude Opus 5.5');
assert.equal(ownerRow(30,NB),'  ● owner  Opus 5.5');
assert.equal(ownerRow(26,NB),'  ● owner');
const long=only({...R.staged,paint:{...R.staged.paint,now:'a very long NOW that cannot fit in the row at all, ever'}},36,NB);
assert.deepEqual(long.slice(4,6),['    a very long NOW that','    cannot fit in the row a…'],long.join('\n'));
""")

    def test_a_frame_route_hangs_its_two_legs_under_it_and_waits_for_the_direction(self):
        self.node(r"""
// (11-18: the legs hang under the route as they did, no box, whatever `worker_box` says; each is its who row, its `CTX 18% · 4m` row with its own time, its NOW.)
const legs=[
 '  ↳ ⠋ frame     claude Opus 5.5 xhigh',
 '      CTX 18% · 4m',
 '      Reading the held messages',
 '  ↳ ⠋ frame-alt codex GPT-5.5 xhigh',      // (a leg with no NOW has no empty row under it)
 '      CTX 11% · 4m',
];
for(const o of [{},NB])assert.deepEqual(only(R.frame,50,o),[tm('● frame → ??','4m',50),'  보류된 메시지 조사',...legs],JSON.stringify(o));
assert.deepEqual(only(R.frame,60).slice(2),legs);
// Done, the two legs gone and the answer asked for: the mark `◑`, no legs, the word; it does not blink.
assert.deepEqual(only(R.wait,50),[tm('◑ frame → ?? · waiting for direction','4m',50),'  보류된 메시지 조사']);
assert.equal(beats(R.wait).length,0);
// In the person's language the word is the word list's (the backend's labels).
const ko=only(R.wait,50,{model:{labels:{direction_wait:'방향 확인 대기'}}});
assert.equal(ko[0],tm('◑ frame → ?? · 방향 확인 대기','4m',50));
// Short of rows the CTX rows fold into their who rows (the percent at the right edge) and the NOW rows go, so each leg is one row; the first line and the title stay.
let folded=false;
for(let h=40;h>=12;h--){
 const rows=box(draw([R.frame],50,h));
 if(!rows.length)break;
 assert(rows[0]===tm('● frame → ??','4m',50)&&rows[1]==='  보류된 메시지 조사',h+'\n'+rows.join('\n'));
 if(!rows.some(s=>/Reading the held/.test(s))){assert(rows.filter(s=>/↳/.test(s)).length===2&&rows.length===4,h+'\n'+rows.join('\n'));folded=true;break;}
}
assert(folded,'the legs fold into one row each at some height');
""")

    def test_a_route_its_owner_does_alone_has_no_stage_line_and_its_now_stands_under_the_owner_row(self):
        self.node(r"""
assert.deepEqual(only(R.solo),[
 tm('● code(quick)','18m'),
 '  보류된 메시지 조사',
 '  ⠋ owner codex GPT-5.5 high',
 '    CTX 63%',
 '    Bash run-tests.py',
]);
// The `CTX` row is the same at any width and has no time (the owner never shows one: the route's time is on its first line); the owner's open tool
// is not on its row (its NOW says it).
assert.deepEqual(only({...R.solo,target:'utilities/session-shell/host.cjs',tool:'Edit'},60).slice(2),['  ⠋ owner codex GPT-5.5 high','    CTX 63%','    Bash run-tests.py']);
// No kind line text of the old forms anywhere.
for(const old of ['code ·','one-shot','staged 3/6 ·','· execute','· Bash','━'])assert(!only(R.solo,60).join('\n').includes(old),old);
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
assert.equal(only({...R.staged,mode:'debug',visibility:{next:'x'}})[0],tm('● frame ✓ → code(standard)','42m'));
// A route whose record names no intensity has no brackets; a direct route's word stands where the record names none.
assert.equal(only({...R.solo,intensity:undefined})[0],tm('● code','18m'));
assert.equal(only({id:'d',title:'작은 수정',kind:'working',state:'working',elapsed:'3m',shape:'direct',stages:[],kind_word:'code',paint:{glyph:P('⠋')}})[0],tm('● code(direct)','3m'));
assert.equal(only({id:'e',title:'작은 수정',kind:'working',state:'working',elapsed:'3m',shape:'direct',stages:[],kind_word:'code',intensity:'direct',paint:{glyph:P('⠋')}})[0],tm('● code(direct)','3m'));
// Several kinds in one plan: what is behind is dim with `✓`, this one bold, what is left is not drawn (the chain is what the record holds).
assert.equal(only({...R.staged,chain:[{name:'research',state:'done'},{name:'draft',state:'done'},{name:'code',state:'active'}]},60)[0],tm('● research ✓ → draft ✓ → code(standard)','42m',60));
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
 tm('✕ frame ✓ → code(standard)','42m'),
 '  패널 route 줄 다시 짜기',
 '  ✕ owner claude Opus 5.5 xhigh',
 '    CTX 40%',
 '    staged 2/3: execute ✕',
]);   // (11-18: the stage text is on the first line where it fits; here, beside the chain, the name does not fit, so the stage row stands under the owner)
assert.equal(only(R.failed,72)[0],tm('✕ frame ✓ → code(standard) · staged 2/3: execute ✕','42m',72));
const done=only(R.done);
assert.equal(done[0],tm('✓ code(quick)','18m'));assert.equal(done[1],'  PASS · 보류된 메시지 조사');
""")

    def test_narrow_widths_drop_values_from_the_back_and_keep_every_row_in_its_width(self):
        self.node(r"""
// The chain goes first (what is behind), then the intensity; the kind and the stage stay.
assert.equal(only(R.staged,60)[0],tm('● frame ✓ → code(standard) · staged 3/6','42m',60));
assert.equal(only(R.staged,72,NB)[0],tm('● frame ✓ → code(standard)','42m',72));   // (the `none` form keeps the stage as a row of its own)
assert.equal(only(R.staged,30)[0],tm('● code(standard)','42m',30));
assert.equal(only(R.staged,24)[0],tm('● code','42m',24));
assert.equal(only(R.staged,22)[0],tm('● code','42m',22));
for(const width of [22,24,26,30,36,44,60]){
 for(const route of [R.staged,R.frame,R.wait,R.solo,R.failed,R.done]){
  const rows=draw([route],width,60);
  assert(rows.every(s=>cells(s)===width),width+' '+route.id+'\n'+rows.join('\n'));
  const inner=box(rows);
  assert(inner.length>=2,width+' '+route.id);
 }
}
// The stage line shortens `staged` first and keeps the stage (in the box's top line, cut with `…` only after that); a worker's percent is never cut in the middle.
assert(only(R.staged,30,NB).includes('    3/6: execute'),only(R.staged,30,NB).join('\n'));
assert(only(R.staged,30).some(s=>/^  ┌ 3\/6: execute/.test(s)),only(R.staged,30).join('\n'));
for(const o of [{},NB])for(const s of only(R.staged,24,o))assert(!/CTX\s+\d$/.test(s),s);
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
const mk=(shape,o={})=>({...R.staged,id:'m',shape,stages:three,kind_word:'research',chain:[{name:'research',state:'active'}],workers_live:[],paint:{...R.staged.paint,now:'Reading the sources'},...o});
// 11-18 (review C3, round 5): with no worker running the stage text is on the first line (the shape word, the position, then the stage's name where the
// width has room: the name goes first, then the whole text); where the name is not there the stage row `<shape> 2/3: analyze` stands under the owner.
const want=(shape,first,o,width=50)=>{const rows=only(mk(shape,o),width);
 const apart=first.includes(': analyze')?[]:['    '+first.split(' · ').at(-1)+': analyze'];
 assert.deepEqual(rows,[tm(first,'42m',width),'  패널 route 줄 다시 짜기','  ⠋ owner claude Opus 5.5 xhigh','    CTX 40%','    Reading the sources',...apart],rows.join('\n'));};
want('map-reduce','● research(standard) · map-reduce 2/3');
want('map-reduce','● research(standard) · map-reduce 2/3: analyze',{},64);
want('transactional-owner','● research(standard) · transactional-owner 2/3: analyze',{},80);
want('resource+verification','● research(standard) · resource+verification 2/3: analyze',{chain:[{name:'research',state:'active'}]},84);
want('unknown','● research(standard) · 2/3: analyze',{},64);
// One blinking thing, the stage's name where it stands (on the first line, or on the stage row under the owner); the first line's dot stands still.
for(const shape of ['map-reduce','transactional-owner','resource+verification']){
 const own=render([mk(shape)],84,60).rows.flat().filter(p=>p.beat===true&&!/ROUTE$/.test(p.text));assert.equal(own.length,1,shape);assert.equal(own[0].text,'analyze',shape);
 assert.deepEqual(beats(mk(shape)).map(p=>p.text),['analyze'],shape+' (the stage row)');
}
// Short of cells the stage's name goes first, then the stage text whole (the first line's chain and kind stay); every row keeps its width, and
// the stage row stands under the owner exactly where the first line has not got the name.
for(const width of [22,26,30,36,44])for(const shape of ['map-reduce','transactional-owner','resource+verification']){
 const rows=draw([mk(shape)],width,60);assert(rows.every(s=>cells(s)===width),width+shape+'\n'+rows.join('\n'));
 const apartRows=box(rows).filter(s=>/^\s+\S*\s*2\/3: /.test(s));
 assert.equal(apartRows.length,/: analyze/.test(box(rows)[0])?0:1,width+shape+'\n'+rows.join('\n'));
 assert(/^● research/.test(box(rows)[0]),width+shape+'\n'+rows.join('\n'));
}
// A frame route and a route its owner does alone keep no stage line.
assert(!only(R.frame,50).some(s=>/\d\/\d/.test(s))&&!only(R.solo,50).some(s=>/\d\/\d/.test(s)));
// The bottom layout names the shape word in its one cell.
const wide=draw([mk('map-reduce'),mk('transactional-owner')],140,16,{side:false});
assert(wide.some(s=>/research\(standard\) · map-reduce 2\/3: analyze/.test(s))&&wide.some(s=>/transactional-owner 2\/3: analyze/.test(s)),wide.join('\n'));
""")

    def test_a_route_with_no_worker_has_its_stage_name_on_the_first_line_or_on_a_stage_row_under_its_owner(self):
        # 11-18 (round 5): the stage's name is never lost at the default width. Where the first line has no room for `· staged N/M: name` the
        # stage row `staged N/M: name` stands under the owner (what the box's top line does for a route with workers); where the name is on the
        # first line there is no such row. A taller panel never shows less of it.
        self.node(r"""
const three=['search','analyze','report'].map((n,i)=>i<1?{name:n,mark:'✓'}:i===1?{name:n,mark:'●',current:true,key:'stg2_on'}:{name:n,mark:''});
const mk=(o={})=>({...R.staged,id:'m',shape:'staged',stages:three,kind_word:'code',chain:[{name:'code',state:'active'}],workers_live:[],paint:{...R.staged.paint,now:'Reading the sources'},...o});
for(const elapsed of ['1h 52m','23h 59m'])for(const width of [32,36,40,44,50,60]){
 const rows=draw([mk({elapsed})],width,80),inner=box(rows),first=inner[0],apart=inner.filter(s=>/^\s+staged 2\/3: analyze/.test(s));
 assert(rows.every(s=>cells(s)===width),width+'\n'+rows.join('\n'));
 assert.equal(first.includes('staged 2/3: analyze'),apart.length===0,elapsed+' '+width+'\n'+rows.join('\n'));
 assert(first.includes('staged 2/3: analyze')||apart.length===1&&apart[0].includes('analyze'),elapsed+' '+width+'\n'+rows.join('\n'));
}
// The default width (40) puts the name on the stage row, the name never twice, and the first line keeps its place and the time.
const at40=box(draw([mk({elapsed:'1h 52m'})],40,80));
assert.equal(at40.filter(s=>/analyze/.test(s)).length,1,at40.join('\n'));
assert(/^● code(\(standard\))?( · staged 2\/3)? +1h 52m$/.test(at40[0]),at40[0]);
// A route with workers (its box carries the name), `worker box = none` and a route with no kind word keep the round 4 form.
const worked=mk({workers_live:[{kind:'working',role:'worker',stage:'analyze',elapsed:'12m',paint:R.staged.paint}]});
assert(!box(draw([worked],40,80)).some(s=>/^\s+staged 2\/3: analyze/.test(s)),box(draw([worked],40,80)).join('\n'));
// The stage row is the first of its steps to go when rows are short (the NOW rows go before it), and a taller panel never shows less of it.
let seen=0,prev=null;
for(let h=10;h<=70;h++){
 const rows=box(draw([mk({elapsed:'1h 52m'}),mk({id:'n',elapsed:'23h 59m'})],40,h)),n=rows.filter(s=>/^\s+staged 2\/3: analyze/.test(s)).length,nows=rows.filter(s=>/Reading the sources/.test(s)).length;
 if(prev!==null)assert(n>=prev,h+' a taller panel shows no less of the stage row\n'+rows.join('\n'));
 if(nows)assert.equal(n,2,h+' the NOW rows go before the stage row\n'+rows.join('\n'));
 prev=n;seen=Math.max(seen,n);
}
assert.equal(seen,2);
""")

    def test_a_route_with_no_known_owner_keeps_its_time_and_context_and_a_resting_owner_has_no_tool(self):
        self.node(r"""
const none=(o={})=>({id:'n',title:'소유자를 모르는 route',kind:'working',state:'working',elapsed:'23m',shape:'one-shot-owner',stages:[{name:'one-shot',current:true,mark:'●'}],
 kind_word:'code',intensity:'quick',paint:{glyph:P('⠋'),ctx:P('40%')},...o});
// The context is drawn though no owner is named (its mark leads its `CTX 40%` row, there being no who row; no session shows a time of its own here, the route's time is on its
// first line, 11-18).
const a=only(none(),50);assert.deepEqual(a.slice(1),['  소유자를 모르는 route','  ⠋ CTX 40%'],a.join('\n'));assert.equal(a[0],tm('● code(quick)','23m',50));
const b=only(none({paint:{glyph:P('⠋')}}),50);assert.equal(b[0],tm('● code(quick)','23m',50),b.join('\n'));assert(!b.some(s=>/^\s+23m$/.test(s)),b.join('\n'));
const c=only(none({shape:'map-reduce',stages:[{name:'search',mark:'✓'},{name:'analyze',mark:'●',current:true}],paint:{glyph:P('⠋')}}),50);
assert(c[0]===tm('● code(quick) · map-reduce 2/2','23m',50)&&c.filter(s=>/^\s+map-reduce 2\/2: analyze$/.test(s)).length===1,c.join('\n'));   // (the name is not on the first line: the stage row stands under the title)
// Its NOW, where it has one, stands under the first line (the time is on that line); no NOW, no empty row.
const d=only(none({paint:{glyph:P('⠋'),now:'Bash run-tests.py'}}),50);
assert.equal(d[2],'    Bash run-tests.py',d.join('\n'));assert(!d.some(s=>/^\s+23m$/.test(s)),d.join('\n'));
assert(!b.some(s=>s.trim()===''),b.join('\n'));
// An owner at rest draws no tool at all (no `after Bash`, no open tool): its row is its who row, its NOW under it where it has one.
const rest={...R.solo,kind:'idle',state:'idle',tool:undefined,after_tool:'Bash',paint:{glyph:P('●'),...doer('codex','GPT-5.5','high','63%')}};
assert.deepEqual(only(rest,50).slice(2),['  ● owner codex GPT-5.5 high','    CTX 63%']);
assert(!only({...rest,paint:{...rest.paint,now:'idle'}},50).join('\n').includes('after Bash'));
assert.deepEqual(only(R.solo,50).slice(2,4),['  ⠋ owner codex GPT-5.5 high','    CTX 63%']);
""")

    def test_under_the_native_area_a_route_row_and_its_now_are_the_owners(self):
        # 11-18 (review C4): a worker leads the route's own `paint`, but the row under the native area names the owner (`owner_paint`),
        # whose NOW stands under it, so the harness, model and context of the row and the NOW come from one session.
        self.node(r"""
const owned={...R.staged,workers_live:[worker('opencode','GLM-5.2','max','9%','worker is on it')],owner_paint:{...R.staged.paint,now:'owner is on it'},
 paint:{glyph:P('⠸'),...doer('opencode','GLM-5.2','max','9%','worker is on it')}};
for(const width of [100,140]){
 const rows=draw([owned],width,16,{side:false}),row=rows.find(s=>/패널 route/.test(s));
 assert(row&&/claude Opus 5\.5 xhigh/.test(row)&&!/GLM-5\.2/.test(row),width+'\n'+rows.join('\n'));
 assert(rows.some(s=>s.includes('owner is on it'))&&!rows.some(s=>s.includes('worker is on it')),width+'\n'+rows.join('\n'));
}
// A route with no owner values of its own keeps the one `paint` as before.
const alone=draw([{...owned,owner_paint:undefined}],100,16,{side:false});
assert(alone.some(s=>/GLM-5\.2/.test(s)),alone.join('\n'));
""")

    def test_short_of_rows_the_context_row_goes_after_the_now_second_row_and_a_taller_panel_never_shows_less(self):
        # 11-18 (review follow-up 1): the steps short of rows are given back in the order they were taken: a panel that is taller never
        # shows fewer context rows, and a closed box (its bottom line) stands only where the context rows do.
        self.node(r"""
const long='a long NOW that goes on and on over two rows and then a good deal further than that, so that it ends in an ellipsis';
const routes=['a','b'].map(id=>({...R.staged,id,title:'route '+id,paint:{...R.staged.paint,now:long},
 workers_live:[worker('claude','Sonnet 5.5','high','22%',long),worker('opencode','GLM-5.2','max','9%',long,{stage:'execute'})]}));
let before=-1,max=0;const seen=[];
for(let h=20;h<=90;h++){
 const rows=draw(routes,44,h),inner=box(rows),ctx=inner.filter(s=>/^\s+CTX/.test(s)).length,closed=inner.filter(s=>/^\s+└/.test(s)).length;
 seen.push([h,ctx,closed]);
 assert(ctx>=before,JSON.stringify(seen));before=ctx;max=Math.max(max,ctx);
 if(closed)assert.equal(ctx,max===ctx?ctx:max,JSON.stringify(seen));
}
const full=seen.filter(x=>x[1]===max);assert(full.length>0&&seen.some(x=>x[1]<max),JSON.stringify(seen));
for(const [h,ctx,closed] of seen)if(closed)assert.equal(ctx,max,h+' '+JSON.stringify(seen));
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
