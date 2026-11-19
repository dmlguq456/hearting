"""11-19 the shell panel's route box draws a unit a worker called on one row under its caller's row.

The rule is one: the called unit stands right under the row of the worker that called it (or, when the owner or a one-unit route's
worker called it, under that lead), two cells in; a unit that called in turn has its own below it, two cells further in. The row is
`<mark> <unit name> <harness> <model> <effort>` and its time at the right edge, in the dim and the weight of a unit route's worker line.
Short of cells the effort goes first, then the harness, then the model; the name and the time stay. Under the route (the bottom layout)
the row stands in the same columns as the route's row: the title two cells in, the unit's name leading the worker cell, its field in
the kind cell, its time at the end. Only what runs is listed; short of rows these rows go before any other of the route's, and what
remains is what the route reads without them. Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"

FIXTURE = r"""
const assert=require('node:assert/strict'),view=require(process.argv[1]+'/panel-view.cjs');
const P=t=>({text:t}),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const names=['plan','plan-check','execute','impl-review','test','report'];
const six=at=>names.map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true,key:'stg2_on'}:{name,mark:''});
const doer=(h,m,e,pct,now)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),...(now?{now}:{})});
const called=(unit,h,m,e,elapsed,o={})=>({kind:'working',unit_word:unit,elapsed,title:'review of '+unit,kind_word:'code',kind_field:'code',
 paint:{glyph:P('⠸'),harness:P(h),model_text:P(m),effort:P(e)},...o});
const worker=(h,m,e,pct,now,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',...o,paint:{glyph:P('⠸'),...doer(h,m,e,pct,now)}});
const staged=(workers,o={})=>({id:'a',title:'rate limiting',kind:'working',state:'working',elapsed:'40m',shape:'staged',stages:six(2),kind_word:'code',intensity:'standard',role:'owner',
 chain:[{name:'code',state:'active'}],paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')},workers_live:workers,...o});
const solo=(o={})=>({id:'s',title:'small change',kind:'working',state:'working',elapsed:'20m',shape:'one-shot-owner',stages:[{name:'one-shot',current:true,mark:'●'}],kind_word:'code',
 intensity:'quick',role:'owner',paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh')},workers_live:[],...o});
const STYLES={dim:{dim:true},loc_repo:{foreground:253},stg2_on:{foreground:150,bold:true},g_dead:{foreground:196,bold:true},lvl_g:{foreground:114},g_spin:{foreground:150}};
const base={repo:[P('hearting/')],messages:[],styles:STYLES};
const render=(routes,width,height,{side=true,model={},configuration={},harness='claude'}={})=>view.render({width,height,side,fill:side,harness,frame:0,
 model:{...base,routes,route_count:routes.length,route_total:routes.length,...model},state:'working',configuration,questions:()=>[]});
const text=r=>r.map(p=>p.text).join('');
const draw=(routes,w,h,o)=>render(routes,w,h,o).rows.map(text);
const box=rows=>{const a=rows.findIndex(s=>/╭─ [\d ]*ROUTE|╭─ route/.test(s)),z=rows.findIndex((s,i)=>i>a&&/^│ ╰/.test(s));
  return rows.slice(a+1,z).map(s=>s.replace(/^│ │ ?/,'').replace(/ ?│ │$/,'').replace(/\s+$/,'')).filter(s=>!/^─ (direct|dispatch|request) ─+$/.test(s));};
const only=(route,width=44,o={})=>box(draw([route],width,60,o));
const col=(s,w)=>cells(s.slice(0,s.indexOf(w)));
const R=staged([{...worker('claude','Sonnet 5.5','high','22%'),called:[called('code-review','claude','Opus 5.5','xhigh','3m'),
 called('fact-verify','claude','Sonnet 5.5','high','1m',{called:[called('web-lookup','codex','GPT-5.5','high','1m')]})]}]);
const NONE=staged([worker('claude','Sonnet 5.5','high','22%')]);
"""


class CalledUnitPanelTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_called_unit_stands_under_the_worker_row_in_its_box_two_cells_in_with_its_time_at_the_right_edge(self):
        self.node(r"""
assert.deepEqual(only(R),[
 '● code(standard) · staged 3/6    40m',
 '  rate limiting',
 '  ● owner  claude Opus 5.5 xhigh',
 '  ┌ staged 3/6: execute ───────────┐',
 '  │ ⠋ worker claude Sonnet 5.5     │',
 '  │   CTX 22% · 12m                │',
 '  │   ⠋ code-review Opus 5.5    3m │',
 '  │   ⠋ fact-verify Sonnet 5.5  1m │',
 '  │     ⠋ web-lookup GPT-5.5    1m │',
 '  └────────────────────────────────┘',
]);
const rows=only(R);
assert.equal(col(rows[4],'⠋'),4);assert.equal(col(rows[6],'⠋'),6);assert.equal(col(rows[8],'⠋'),8);
// (the time ends at the box's right edge on every row, in one column)
assert.equal(new Set([6,7,8].map(i=>cells(rows[i].replace(/ │$/,'')))).size,1);
// The unit's name is the role word's dim; no other new color or word.
const row=render([R],44,60).rows.find(r=>/code-review/.test(text(r)));
assert.equal(row.find(p=>p.text==='code-review').dim,true);
""")

    def test_the_same_rows_without_the_box_stand_two_cells_in_under_the_worker_hung_by_its_arrow(self):
        self.node(r"""
const rows=only(R,44,{configuration:{worker_box:'none'}});
const at=rows.findIndex(s=>/↳ ⠋ worker/.test(s));
assert.deepEqual(rows.slice(at,at+5),['    ↳ ⠋ worker claude Sonnet 5.5','        CTX 22% · 12m','        ⠋ code-review Opus 5.5    3m',
 '        ⠋ fact-verify Sonnet 5.5  1m','          ⠋ web-lookup GPT-5.5    1m']);
""")

    def test_the_owner_that_called_has_the_unit_under_its_own_row_and_a_one_unit_routes_worker_likewise(self):
        self.node(r"""
assert.deepEqual(only(solo({called:[called('fact-verify','claude','Opus 5.5','xhigh','3m')]})),[
 '● code(quick)                    20m',
 '  small change',
 '  ⠋ owner claude Opus 5.5 xhigh',
 '    ⠋ fact-verify claude Opus 5.5 3m',
]);
const unitRoute=solo({unit_route:true,unit_word:'code-review',shape:'one-shot-owner',called:[called('fact-verify','claude','Opus 5.5','xhigh','3m')]});
const rows=only(unitRoute);
assert(/code-review claude Opus 5\.5/.test(rows[2]),rows.join('\n'));
assert.equal(col(rows[3],'⠋'),col(rows[2],'⠋')+2,rows.join('\n'));
assert(/fact-verify/.test(rows[3]));
""")

    def test_a_context_row_of_the_caller_stands_above_the_unit(self):
        self.node(r"""
const rows=only(R),at=rows.findIndex(s=>/CTX 22%/.test(s));
assert(/code-review/.test(rows[at+1]),rows.join('\n'));
""")

    def test_short_of_cells_the_effort_goes_first_then_the_harness_then_the_model_and_the_name_and_the_time_stay(self):
        self.node(r"""
const wide=staged([{...worker('claude','Sonnet 5.5','high','22%'),called:[called('code-review','claude','Opus 5.5','xhigh','3m')]}]);
const line=width=>only(wide,width).find(s=>/code-re/.test(s)).replace(/^\s*│?\s*/,'').replace(/\s*│$/,'');
assert.match(line(64),/^⠋ code-review claude Opus 5\.5 xhigh +3m$/);
assert.match(line(48),/^⠋ code-review claude Opus 5\.5 3m$/);          // the effort has gone
assert.match(line(44),/^⠋ code-review Opus 5\.5 +3m$/);                // then the harness
assert.match(line(32),/^⠋ code-review +3m$/);                         // then the model: the name and the time stay
assert.equal(line(28),'⠋ code-review');                                // the name whole from 27 cells, where the time has no room
assert.equal(line(27),'⠋ code-review');
assert.equal(line(26),'⠋ code-revi…');                                  // narrower, the name is cut and ends in `…`
assert.equal(line(24),'⠋ code-re…');
""")

    def test_a_route_with_no_called_unit_reads_as_it_did(self):
        self.node(r"""
const plain=staged([worker('claude','Sonnet 5.5','high','22%')]);
assert.deepEqual(draw([{...plain,workers_live:plain.workers_live.map(w=>({...w,called:[]}))}],44,40),draw([plain],44,40));
assert.deepEqual(draw([{...plain,called:[]}],100,16,{side:false}),draw([plain],100,16,{side:false}));
assert(!only(plain).some(s=>/code-review/.test(s)));
""")

    def test_short_of_rows_the_called_rows_go_before_any_other_row_of_the_route_and_what_remains_is_the_route_without_them(self):
        self.node(r"""
let dropped=0,shown=0;
for(let h=14;h<=44;h++){
 const a=draw([R],44,h),b=draw([NONE],44,h),n=a.filter(s=>/code-review|fact-verify|web-lookup/.test(s)).length;
 if(n===0){dropped++;assert.deepEqual(a,b,'h='+h);}
 else{shown++;assert.equal(n,3,'h='+h);assert(a.some(s=>/CTX 22%/.test(s))&&a.some(s=>/└─/.test(s)),'h='+h);}
}
assert(dropped>0&&shown>0,dropped+' '+shown);
// the one-row-per-route fold draws none
assert(!draw([R],44,15).some(s=>/code-review/.test(s)));
""")

    def test_under_the_route_the_unit_is_a_row_in_the_same_columns_and_every_bar_stands_at_one_cell(self):
        self.node(r"""
const bars=s=>[...s].reduce((a,c,i,all)=>c==='│'?[...a,cells(all.slice(0,i).join(''))]:a,[]);
for(const width of [130,100,80,60]){
 const rows=only(R,width,{side:false});
 assert.equal(rows.length,4,rows.join('\n'));
 for(const row of rows.slice(1))assert.deepEqual(bars(row),bars(rows[0]),width+'\n'+rows.join('\n'));
 // the title and the mark stand two cells in, the next unit's two further; each row ends in its own time
 assert.equal(col(rows[0],'⠋'),0);assert.equal(col(rows[1],'⠋'),2);assert.equal(col(rows[2],'⠋'),2);assert.equal(col(rows[3],'⠋'),4);
 assert(/ 40m$/.test(rows[0])&&/ 3m$/.test(rows[1])&&/ 1m$/.test(rows[2])&&/ 1m$/.test(rows[3]),rows.join('\n'));
}
// Wide: the shared worker column grows to hold the unit's name, harness, model and effort whole.
const wide=only(R,130,{side:false});
assert(/│ code-review claude Opus 5.5 xhigh +│/.test(wide[1]),wide.join('\n'));
assert(/│ web-lookup codex GPT-5.5 high +│/.test(wide[3]),wide.join('\n'));
// The unit's field stands in the kind cell where the route's own reads `code(standard) · staged 3/6: execute`.
assert(/│ code +\d+m$/.test(wide[1]),wide.join('\n'));
// Narrower: the name stays in the cell with as much of the model as it holds (the effort, then the harness, give way).
const mid=only(R,100,{side:false});
assert(/│ code-review Opus 5.5 +│/.test(mid[1]),mid.join('\n'));
""")

    def test_under_the_route_a_called_row_never_takes_from_its_callers_row_at_any_width(self):
        self.node(r"""
const bars=s=>[...s].reduce((a,c,i,all)=>c==='│'?[...a,cells(all.slice(0,i).join(''))]:a,[]);
const flat=s=>s.replace(/\s+/g,' ').trim();
const owned=(title,h,m,e)=>staged([worker('claude','Sonnet 5.5','high','22%')],{title,paint:{glyph:P('⠸'),harness:P(h),model_text:P(m),effort:P(e)}});
const withUnit=(r,c)=>({...r,workers_live:r.workers_live.map(w=>({...w,called:[c]}))});
const mixes=[
 ['usual',owned('rate limiting','claude','Opus 5.5','xhigh'),called('code-review','claude','Opus 5.5','xhigh','3m')],
 ['short title',owned('auth','claude','Opus 5.5','xhigh'),called('code-review','claude','Opus 5.5','xhigh','3m')],
 ['a longer worker cell',owned('rate limiting','claude','Opus 5.5','high'),called('code-review','claude','Sonnet 5.5','xhigh','3m')],
 ['the longest worker cell',owned('rate limiting','claude','Opus 5.5','high'),called('code-review','codex','GPT-5.5-codex-max','xhigh','3m')],
 ['a longer time',owned('auth','claude','Opus 5.5','high'),called('code-review','codex','GPT-5.5-codex-max','xhigh','1h 5m')],
];
for(const [name,route,unit] of mixes)for(const width of [...Array(111).keys()].map(i=>i+40)){
 const plain=only(route,width,{side:false}),rows=only(withUnit(route,unit),width,{side:false});
 assert.equal(rows.length,plain.length+1,name+' '+width);
 // the route's rows read letter for letter as they read with no called unit (only the blanks between cells may differ)
 assert.deepEqual([rows[0],...rows.slice(2)].map(flat),plain.map(flat),name+' '+width+'\n'+rows.join('\n')+'\n'+plain.join('\n'));
 // the called row stands in the route row's columns: its bars at the route row's cells (a route row cut short by its own time
 // keeps fewer bars, those it keeps are the same)
 const rb=bars(rows[0]),cb=bars(rows[1]);
 assert.deepEqual(cb.slice(0,rb.length),rb,name+' '+width+'\n'+rows.join('\n'));
 // two cells in, the name and the time stay in the row it was given
 assert(/^ {2}⠋/.test(rows[1]),name+' '+width+'\n'+rows.join('\n'));
}
""")

    def test_a_box_of_two_routes_keeps_the_other_routes_rows_as_they_read_with_no_called_unit(self):
        # 11-19 (review of #1742, N5): the width test above has one route; with a second route in the box its rows read the same too.
        self.node(r"""
const flat=s=>s.replace(/\s+/g,' ').trim();
const other=solo({id:'o',title:'cleanup of imports',elapsed:'9m'});
const withUnit=(r,c)=>({...r,workers_live:r.workers_live.map(w=>({...w,called:[c]}))});
const unit=called('code-review','claude','Sonnet 5.5','xhigh','3m');
for(const width of [...Array(111).keys()].map(i=>i+40)){
 const plain=box(draw([NONE,other],width,60,{side:false})),rows=box(draw([withUnit(NONE,unit),other],width,60,{side:false}));
 const at=rows.findIndex(s=>/code-review/.test(s));
 if(at<0)continue;                                  // (no room for the called row at this width: the rows are the plain ones)
 assert.deepEqual([...rows.slice(0,at),...rows.slice(at+1)].map(flat),plain.map(flat),width+'\n'+rows.join('\n'));
}
""")

    def test_the_widths_that_take_the_columns_grown_by_a_called_row_are_one_tail_of_the_widths(self):
        # 11-19 (review of #1742, N1): the columns grown by a called row are taken only where the stage column has its richest form
        # (as the widened worker column is), so the widths that take them are one run up to the widest, never in and out: a
        # wider window never lays the columns narrower, nor takes a word (`Sonnet 5.5`) off the called row.
        self.node(r"""
const withUnit=(r,c)=>({...r,workers_live:r.workers_live.map(w=>({...w,called:[c]}))});
const other=solo({id:'o',title:'cleanup of imports',elapsed:'9m'});
const owned=(title,m,e)=>staged([worker('claude','Sonnet 5.5','high','22%')],{title,paint:{glyph:P('⠸'),harness:P('claude'),model_text:P(m),effort:P(e)}});
const combos=[
 ['two routes',[owned('rate limiting','Opus 5.5','high'),other],called('code-review','claude','Sonnet 5.5','xhigh','3m')],
 ['one route',[owned('rate limiting','Opus 5.5','high')],called('code-review','claude','Sonnet 5.5','xhigh','3m')],
 ['short title',[owned('auth','Opus 5.5','xhigh'),other],called('code-review','claude','Opus 5.5','xhigh','3m')],
 ['a long worker cell',[owned('rate limiting','Opus 5.5','high'),other],called('code-review','codex','GPT-5.5-codex-max','xhigh','1h 5m')],
];
// (a width takes the grown columns where the route rows' blanks differ from those of the box with no called unit)
const grownAt=(routes,unit,width)=>{
 const plain=box(draw(routes,width,60,{side:false})),rows=box(draw([withUnit(routes[0],unit),...routes.slice(1)],width,60,{side:false}));
 const at=rows.findIndex(s=>/code-review/.test(s));
 return at>=0&&JSON.stringify([...rows.slice(0,at),...rows.slice(at+1)])!==JSON.stringify(plain);
};
for(const [name,routes,unit] of combos){
 const widths=[...Array(111).keys()].map(i=>i+40).filter(width=>grownAt(routes,unit,width));
 for(let i=1;i<widths.length;i++)assert.equal(widths[i],widths[i-1]+1,name+': the widths that take the grown columns are '+widths.join(','));
 assert(!widths.length||widths[widths.length-1]===150,name+': the run reaches the widest width: '+widths.join(','));
}
""")

    def test_the_three_harness_panels_draw_the_same_rows(self):
        self.node(r"""
const want=only(R);
for(const harness of ['claude','codex','opencode'])assert.deepEqual(only(R,44,{harness}),want,harness);
""")


if __name__ == "__main__":
    unittest.main()
