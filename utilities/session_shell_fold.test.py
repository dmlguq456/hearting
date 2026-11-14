"""Spec E9 rule 49: the gear inside the panel, folding and unfolding, and folding by itself when narrow."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"


class FoldTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", script, str(SHELL)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_gear_and_the_fold_button_stand_on_the_last_row_inside(self):
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const p=t=>({text:t});
const model={tag:'[e9]',repo:[p('hearting/')],selected:[p('Opus 5.5')],quota:[p('5h 12%'),p('(↻ 2h)'),p(' │ '),p('7d 66%'),p('(↻ 5d)')],
 route_total:2,route_count:2,routes:[{id:'a',title:'one',kind:'working',paint:{}},{id:'b',title:'two',kind:'idle',paint:{}}]};
const draw=o=>v.render({width:40,height:20,side:true,model,state:'idle',harness:'claude',questions:()=>[],...o});
const text=r=>r.rows.map(row=>row.map(x=>x.text).join(''));
for(const extra of [{},{routeDetail:true}]){
 const r=draw(extra),rows=text(r);
 // (a) The top border is the line alone right of the head; (a), (b) the last row inside ends `»   ⚙`.
 assert(!rows[0].includes('⚙')&&rows[0].endsWith('─┐'),rows[0]);
 assert(rows.at(-2).endsWith('»   ⚙ │')&&rows.filter(s=>s.includes('⚙')).length===1,rows.join('\n'));
 const fold=r.hits.find(h=>h.action==='fold'),gear=r.hits.find(h=>h.action==='settings');
 // The presses: `»` at panel column 33, `⚙` at 37; the cell between them is neither's.
 assert.deepEqual([fold.row,gear.row],[18,18]);
 assert(fold.left<=33&&fold.right>=33&&gear.left<=37&&gear.right>=37&&fold.right<35&&gear.left>35,JSON.stringify([fold,gear]));
 // Both marks dim, as the panel's labels.
 const last=r.rows.at(-2);assert(last.find(x=>x.text==='⚙').dim&&last.find(x=>x.text==='»').dim);
}
// 5h/7d share the row: whole with room, their reset times going first when short.
assert(text(draw({}))[18].startsWith('│ 5h 12%(↻ 2h) │ 7d 66%(↻ 5d)'));
const narrow=text(v.render({width:32,height:20,side:true,model,state:'idle',questions:()=>[]}));
assert(narrow.at(-2).startsWith('│ 5h 12% │ 7d 66%')&&narrow.at(-2).endsWith('»   ⚙ │'),narrow.at(-2));
// The row stays however short the rows run (the head's last rows go first, boxes stay closed).
for(let h=6;h<=14;h++){const rows=text(draw({height:h}));assert(rows.at(-2).endsWith('»   ⚙ │'),h+'\n'+rows.join('\n'));
 assert.equal(rows.filter(s=>s.includes('╭')).length,rows.filter(s=>s.includes('╰')).length,h+'\n'+rows.join('\n'));}
// The settings editor keeps its own last row (Esc), no fold button.
assert(!text(draw({settings:{index:0,value:v.defaults}})).join('').includes('»'));
// (e) The bottom layout: `▾   ⚙`; a left panel folds with `«`.
const bottom=text(v.render({width:80,height:14,side:false,model,state:'idle',configuration:{layout:'bottom'},questions:()=>[]}));
assert(bottom.at(-2).endsWith('▾   ⚙ │'),bottom.join('\n'));
assert(text(draw({configuration:{position:'left'}})).at(-2).endsWith('«   ⚙ │'));
""")

    def test_the_last_row_keeps_the_model_quota_and_gives_way_in_order(self):
        # Correction 7 (2026-11-13): the last row reads `5h … │ 7d … │ fable 70%` (the model word in
        # its model color). Short of cells it gives way in this order: the reset times `(↻ …)`,
        # then 5h, then the model values, then 7d. The folded strip carries none of it.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const P=(t,o={})=>({text:t,...o}),sep=P(' │ ',{dim:true});
const quota=[P('5h ',{dim:true}),P('12%',{foreground:150}),P('(↻ 2h)',{dim:true}),sep,P('7d ',{dim:true}),P('66%',{foreground:186}),P('(↻ 5d)',{dim:true}),
 sep,P('fable ',{foreground:176}),P('70%',{foreground:186})];
const model={tag:'[e9]',repo:[P('hearting/')],quota};
const last=w=>{const r=v.render({width:w,height:20,side:true,model,state:'idle',harness:'claude',questions:()=>[]});return r.rows.at(-2);};
const text=row=>row.map(x=>x.text).join('');
const want=[[60,'5h 12%(↻ 2h) │ 7d 66%(↻ 5d) │ fable 70%'],[40,'5h 12% │ 7d 66% │ fable 70%'],[32,'7d 66% │ fable 70%'],[26,'7d 66%'],[15,'']];
for(const [w,s] of want){const t=text(last(w));assert(t.startsWith('│ '+s+(s?' ':''))&&t.endsWith('»   ⚙ │')&&!t.includes('…'),w+' '+t);}
// The model word keeps its model color; nothing of the row is cut mid-value.
assert.equal(last(40).find(x=>x.text==='fable ').foreground,176);
// The folded strip has no quota.
const strip=v.render({width:8,height:20,side:true,folded:true,model:{...model,route_total:2},state:'idle',questions:()=>[]}).rows.map(text).join('\n');
assert(!/fable|7d|5h/.test(strip),strip);
""")

    def test_the_folded_strip_carries_the_core_values_only(self):
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const styles={g_spin:{foreground:230},g_blocked:{foreground:217},g_work_off:{foreground:150,dim:true},dim:{dim:true},loc_repo:{foreground:252},tag_steward:{foreground:219,bold:true}};
const strip=(model={},o={})=>v.render({width:8,height:12,side:true,folded:true,model:{tag:'[e9]',styles,...model},state:'working',frame:3,harness:'claude',questions:()=>{throw Error('no question in the strip');},...o});
const text=r=>r.rows.map(row=>row.map(x=>x.text).join(''));
// (c) Eight cells: two borders, a blank cell inside each, four content cells; the state glyph
// with the number, an empty row, the route count, the answer waits, and the unfold mark on the
// last row inside.
let r=strip({route_total:2,wait:'◑답 대기 1'}),rows=text(r);
assert.deepEqual(rows,['┌──────┐','│ ⠸ e9 │','│      │','│ 2 RT │','│ ◑ 1  │','│      │','│      │','│      │','│      │','│      │','│  «   │','└──────┘']);
assert(rows.every(s=>[...s].length===8));
// An absent value draws no row; from ten `9+RT` (routes) and `◑ 9+` (waits); an unknown wait count its glyph alone.
assert.deepEqual(text(strip({route_total:0,wait:''})).slice(1,4),['│ ⠸ e9 │','│      │','│      │']);
assert.deepEqual(text(strip({route_total:12,wait:'◑답 대기 10'})).slice(2,5),['│      │','│ 9+RT │','│ ◑ 9+ │']);
assert.deepEqual(text(strip({route_total:0,wait:'◑답 대기 ?'})).slice(2,4),['│      │','│ ◑    │']);
// The route count's label is dim beside its number (as the panel's labels).
const rt=r.rows[3];assert(rt.find(x=>x.text===' RT').dim&&rt.find(x=>x.text==='2').foreground===252);
// The border takes the session's state color (the open panel's frame), the glyph spins only
// while it runs; waiting `◑`, idle `●` (idle and unknown: the plain line, the number unmoved).
for(const [state,glyph,color] of [['working','⠸',230],['blocked','◑',217],['idle','●',undefined],['unknown',' ',undefined]]){
 const s=strip({},{state}),frame=s.rows.flatMap(row=>[row[0],row.at(-1)]);
 assert.equal(text(s)[1],'│ '+glyph+' e9 │',state);
 assert(frame.every(x=>(x.foreground??undefined)===color&&(color===undefined?x.dim===true:x.dim!==true)),state+JSON.stringify(frame[0]));
 assert.equal(s.animated,state==='working',state);assert.deepEqual(s.spins,state==='working'?[1]:[]);
}
// Only the glyph turns: no steward mark, gear, beat or moving rule; a steward's number keeps its pink.
r=strip({steward:true});
assert(!text(r).join('').match(/[⚑⚙]/)&&r.motion===null&&!r.rows.flat().some(x=>x.beat||x.scan));
assert.equal(r.rows[1].find(x=>x.text==='e9').foreground,219);
// A press on any cell opens the panel.
assert.equal(r.hits.length,12);assert(r.hits.every((h,i)=>h.row===i&&h.left===0&&h.right===7&&h.action==='unfold'));
// Short of rows the empty row goes first, then the route count, the waits; the glyph row and `«` stay.
for(const [h,want] of [[6,['│ ⠸ e9 │','│ 2 RT │','│ ◑ 1  │','│  «   │']],[5,['│ ⠸ e9 │','│ ◑ 1  │','│  «   │']],[4,['│ ⠸ e9 │','│  «   │']],[3,['│  «   │']]])
 assert.deepEqual(text(strip({route_total:2,wait:'◑답 대기 1'},{height:h})).slice(1,-1),want,String(h));
// A left panel's strip unfolds with `»`; every harness gets the same strip.
assert.equal(text(strip({},{configuration:{position:'left'}}))[10],'│  »   │');
for(const harness of ['codex','opencode'])assert.deepEqual(text(strip({},{harness})),text(strip()));
// No color keeps the shapes.
assert.deepEqual(text(strip({no_color:true,route_total:2})),text(strip({route_total:2})));
""")

    def test_folded_under_the_native_area_one_row_and_its_unfold_mark(self):
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const line=(segments,width=60)=>v.render({width,height:14,side:false,folded:true,segments,model:{},state:'idle',configuration:{layout:'bottom'},questions:()=>[]});
let r=line([{text:'[e9] hearting │ Opus 5.5 │ context ━━── 58%'}]);
const row=r.rows[0].map(x=>x.text).join('');
assert.equal(r.rows.length,1);assert(row.startsWith('[e9] hearting │ Opus 5.5')&&row.endsWith(' ▴')&&[...row].length===60,row);
assert.deepEqual(r.hits,[{row:0,left:0,right:59,action:'unfold'}]);
// A status line wider than the row is cut with `…` and keeps `▴` at the right end.
const long=line([{text:'x'.repeat(100)}],30).rows[0].map(x=>x.text).join('');
assert(long.endsWith('… ▴')&&[...long].length===30,long);
""")

    def test_one_judgment_decides_the_fold_and_the_native_width(self):
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs'));
const configuration={layout:'side',side_width:40,side_min_columns:100,position:'right'};
const fold=(cols,choice=null,pinned=false)=>d.fold({cols,choice,configuration,pinned});
// (d) The threshold is one constant: 60 native columns beside the open panel (cols - 40 - 2).
assert.equal(d.NATIVE_MIN,60);
assert.equal(fold(102).folded,false);assert.equal(fold(101).folded,true);
for(const width of [32,60,80])assert.equal(d.fold({cols:width+62,configuration:{...configuration,side_width:width}}).folded,false),
 assert.equal(d.fold({cols:width+61,configuration:{...configuration,side_width:width}}).folded,true);
// #1586 review follow-up 2: a panel folded by itself opens again only three columns past the
// threshold (63 native columns), so a window dragged across it does not jump back and forth; an
// open panel still folds under 60.
const now=(cols,expanded)=>d.fold({cols,configuration,expanded}).folded;
assert.equal(d.REOPEN_MARGIN,3);
assert.deepEqual([101,102,103,104,105].map(c=>now(c,false)),[true,true,true,true,false]);
assert.deepEqual([105,102,101].map(c=>now(c,true)),[false,false,true]);
// The old `side_min_columns` does not take part.
assert.equal(d.fold({cols:130,configuration:{...configuration,side_min_columns:1000}}).folded,false);
// A hand fold stays folded however wide; a hand unfold stays open however narrow.
assert.deepEqual(fold(300,{folded:true}),{folded:true,choice:{folded:true}});
for(const cols of [100,40,140])assert.deepEqual(fold(cols,{folded:false}),{folded:false,choice:{folded:false}});
// An open settings editor or question keeps an open panel open; the bottom layout never folds by itself.
assert.equal(fold(100,null,true).folded,false);
assert.equal(d.fold({cols:40,configuration:{...configuration,layout:'bottom'}}).folded,false);
assert.equal(d.fold({cols:40,choice:{folded:true},configuration:{...configuration,layout:'bottom'}}).folded,true);
// The sizes the native is told: open beside (cols - width - 2), the strip (cols - 8 - 1), under (cols).
const at=(cols,expanded,extra={})=>d.layout({cols,rows:40,expanded,configuration:{...configuration,...extra}});
assert.equal(at(180,true).nativeCols,138);assert.equal(at(180,false).nativeCols,171);assert.equal(at(180,false).panelCols,d.STRIP);
assert.equal(at(101,false).nativeCols,92);assert.equal(at(102,true).nativeCols,60);
assert.deepEqual([at(180,false,{position:'left'}).nativeColumn,at(180,false).panelColumn],[9,172]);
assert.deepEqual([at(180,false,{layout:'bottom'}).side,at(180,false,{layout:'bottom'}).nativeCols],[false,180]);
// Opened by hand where the panel cannot stand beside (native under 48), it opens under the native area.
assert.deepEqual([at(80,true).side,at(80,true).nativeCols],[false,80]);
""")

    def test_the_panel_folds_by_itself_only_under_sixty_native_columns(self):
        # 11-14 night ("자동으로 접히는 기준이 타이트하고"): the open panel folds by itself only where it would
        # leave the native area under 60 columns (it was 80), and opens again three columns past it.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs'));
for(const width of [40,54]){
 const configuration={layout:'side',side_width:width,position:'right'};
 const folded=(cols,expanded=true)=>d.fold({cols,configuration,expanded}).folded;
 const line=width+2+60;  // 102 columns for a 40-column panel, 116 for a 54-column one
 // Open, the native keeps 60 columns beside it; one column fewer folds it.
 assert.deepEqual([line+20,line,line-1].map(c=>folded(c)),[false,false,true],String(width));
 assert.equal(d.layout({cols:line,rows:40,expanded:true,configuration}).nativeCols,60);
 // A window between the old line (80 native columns) and the new one stays open.
 assert.deepEqual([width+2+79,width+2+70].map(c=>folded(c)),[false,false],String(width));
 // Folded by itself, it opens again at 63 native columns and not before.
 assert.deepEqual([line,line+2,line+3].map(c=>folded(c,false)),[true,true,false],String(width));
}
""")

    def test_a_hand_choice_holds_at_every_width_and_only_no_choice_is_judged(self):
        # 11-14 night ("창 크기 조절할때마다 자동으로 접히는게 좀 번거롭네"): a panel opened by hand stays open
        # however the window is resized (under the native area where it cannot stand beside, beside
        # again when the width returns); folded by hand it stays folded; only without a hand choice
        # does the 60-column judgment fold and open it. The host's own fold path, resize by resize.
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function geometry(){'),text.indexOf('function panelColumns(){'));
const seedLine=text.split('\n').find(line=>line.includes('const choice=seed.foldChoice;'));
const host=(cols,expanded=true)=>{
 const c={nativeDisplay:require(path.join(process.argv[1],'native-display.cjs')),cols,rows:40,expanded,foldChoice:null,
  displayConfiguration:{layout:'side',side_width:40,position:'right'},settings:undefined,dragging:false,block:{focused:false,leave(){}}};
 vm.createContext(c);vm.runInContext(code,c);return c;};
// One window resize (host.cjs: applyLayout, then refold), then what stands: open beside, open under, or folded.
const resize=(c,cols)=>{c.cols=cols;c.refold();const g=c.geometry();return c.expanded?(g.side?'beside':'under'):'folded';};
const press=(c,folded)=>{c.foldByHand(folded);c.refold();};
// (1) Opened by hand where it had folded by itself: open at every width, narrower and wider, again and again.
let c=host(95);assert.equal(resize(c,95),'folded');press(c,false);assert.equal(resize(c,95),'beside');
const widths=[70,100,60,130,95,101,200,90,61,140];
assert.deepEqual(widths.map(w=>resize(c,w)),widths.map(w=>w-42>=48?'beside':'under'));
// (2) Folded by hand: folded however wide.
c=host(180);assert.equal(resize(c,180),'beside');press(c,true);
assert.deepEqual([300,400,120,180,90].map(w=>resize(c,w)),Array(5).fill('folded'));
// (3) No hand choice: the 60-column judgment folds it (a 40-column panel under 102 columns) and opens
// it again past the margin (105).
c=host(180);
assert.deepEqual([180,102,101,104,105,102,101,160].map(w=>resize(c,w)),['beside','beside','folded','folded','beside','beside','folded','beside']);
// (4) Opened by hand too narrow to stand beside (under 48 native columns): under the native area, and
// beside again, still open, once the width returns.
c=host(85,false);assert.equal(resize(c,85),'folded');press(c,false);
assert.deepEqual([85,95,80,180,89,90].map(w=>resize(c,w)),['under','beside','under','beside','under','beside']);
// A host hand-over from the earlier release carries `{folded:false,cols}`: it holds at every width too.
c=host(100);vm.runInContext('{const seed={foldChoice:{folded:false,cols:100}};'+seedLine.trim()+'}',c);
assert.deepEqual([100,70,140,60].map(w=>resize(c,w)),['beside','under','beside','under']);
""")

    def test_no_window_size_leaves_the_native_area_without_a_cell(self):
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),d=require(path.join(process.argv[1],'native-display.cjs'));
// A window of nine cells or fewer once left the native beside the strip zero or fewer columns: the
// PTY refused the size and the panel never came back. The strip stands only where the native beside
// it keeps STRIP_NATIVE_MIN columns; narrower, the panel folds to the one row under the native.
assert.equal(d.STRIP_NATIVE_MIN,20);
const strip=(cols,extra={})=>d.layout({cols,rows:40,expanded:false,configuration:{layout:'side',side_width:40,...extra}});
assert.deepEqual([strip(29).side,strip(29).nativeCols,strip(29).panelCols],[true,20,d.STRIP]);
for(const cols of [28,9,1])for(const extra of [{},{position:'left'}]){
 const g=strip(cols,extra);
 assert.deepEqual([g.side,g.folded,g.left,g.nativeCols,g.panelCols,g.nativeColumn,g.panelRow,g.nativeRows],[false,true,false,cols,cols,0,null,39],JSON.stringify({cols,extra,g}));
}
// Every width 1-40 and height 1-12, open or folded, beside or under: the native keeps a cell and a row.
for(const extra of [{},{position:'left'},{layout:'bottom'}])for(const expanded of [true,false])
 for(let cols=1;cols<=40;cols++)for(let rows=1;rows<=12;rows++)for(const panelRows of [undefined,0,1,rows-1,rows,rows+3]){
  const g=d.layout({cols,rows,expanded,configuration:{layout:'side',side_width:40,...extra},panelRows});
  const seen=JSON.stringify({cols,rows,expanded,extra,panelRows,g});
  assert(g.nativeCols>=1&&g.nativeRows>=1,seen);
  assert.equal(g.side?g.nativeCols+g.gap+g.panelCols:g.nativeCols,cols,seen);
  assert.equal(g.nativeRows,g.side?rows:Math.max(1,rows-(panelRows??1)),seen);
 }
""")

    def test_a_press_on_the_targets_drawn_for_this_placement_counts_while_a_layout_is_queued(self):
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
// Review 1597 follow-up (11-13): a press made while a layout was queued was dropped even when the
// targets on screen were drawn for the very placement it was made in. The placement decides alone:
// the same placement counts; a press on targets drawn for another placement is still dropped.
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function panelMouse('),text.indexOf('function displayReporter('));
const g={side:true,folded:false,left:false,panelColumn:90,panelCols:40},place='true:true:false:false:40:130:30:false';
let calls=[];
const c={fleetView:null,paste:false,relay:false,dragging:false,settings:null,routeDetail:false,routeArea:null,
  geometry:()=>g,panelColumns:()=>40,panelRow:()=>0,height:()=>30,terminal:{modes:{mouseTrackingMode:'none'}},
  layoutQueued:false,panelHitsPlacement:place,placement:()=>place,
  panelHits:[{row:1,left:9,right:14,action:'question'}],panelAction:action=>calls.push(action)};
vm.createContext(c);vm.runInContext(code,c);
for(const [queued,stamp,expected] of [[false,place,['question']],[true,place,['question']],[false,'old',[]],[true,'old',[]]]){
  c.layoutQueued=queued;c.panelHitsPlacement=stamp;calls=[];
  assert.equal(c.panelMouse(0,101,2,false),true);
  assert.deepEqual(calls,expected,JSON.stringify({queued,stamp}));
}
""")


    def test_the_strip_carries_the_lab_token_under_the_route_count(self):
        # Draft A: `5 LB` (conditions), `!1LB` (failed or stuck), `?1LB` (unconfirmed or unnamed),
        # `9+LB`, as the board words them, under `N RT`. Short of rows a plain count goes before
        # the route count, a `!`/`?` after it, the answer wait last; `!9+LB` keeps `!9+`. The
        # border stays the session's own color.
        self.node(r"""
const assert=require('node:assert/strict'),path=require('node:path'),v=require(path.join(process.argv[1],'panel-view.cjs'));
const styles={dim:{dim:true},loc_repo:{foreground:252},lvl_r:{foreground:217},lvl_y:{foreground:186},g_spin:{foreground:230}};
const T=(t,k)=>({text:t,...(styles[k]||{})});
const token=s=>{const m=/^([!?]?)(\d+\+?)( ?LB)$/.exec(s);return {strip:[T(m[1]+m[2],m[1]==='!'?'lvl_r':m[1]==='?'?'lvl_y':'loc_repo'),T(m[3],'dim')],strip_attention:!!m[1]};};
const strip=(model={},o={})=>v.render({width:8,height:12,side:true,folded:true,model:{tag:'[e9]',styles,...model},state:'idle',harness:'claude',questions:()=>[],...o});
const text=r=>r.rows.map(row=>row.map(x=>x.text).join(''));
assert.deepEqual(text(strip({route_total:1,lab:token('5 LB')})).slice(1,6),['│ ● e9 │','│      │','│ 1 RT │','│ 5 LB │','│      │']);
for(const [s,row] of [['!1LB','│ !1LB │'],['?1LB','│ ?1LB │'],['9+LB','│ 9+LB │'],['!9+LB','│ !9+  │'],['?9+LB','│ ?9+  │']])
 assert.deepEqual(text(strip({lab:token(s)})).slice(1,4),['│ ● e9 │','│      │',row],s);
// With a wait: route count, LAB, wait, in that order.
assert.deepEqual(text(strip({route_total:2,wait:'◑답 대기 1',lab:token('!1LB')})).slice(1,6),['│ ● e9 │','│      │','│ 2 RT │','│ !1LB │','│ ◑ 1  │']);
// The mark takes the board's color, `LB` the label's dim.
const row=strip({lab:token('!1LB')}).rows[3];assert(row.find(x=>x.text==='!1').foreground===217&&row.find(x=>x.text==='LB').dim);
// Short of rows: blank, then `5 LB`, then `2 RT`, then the wait; a `!` outlasts the route count.
const rows=(lab,h)=>text(strip({route_total:2,wait:'◑답 대기 1',lab:token(lab)},{height:h})).slice(1,-1);
assert.deepEqual(rows('5 LB',7),['│ ● e9 │','│ 2 RT │','│ 5 LB │','│ ◑ 1  │','│  «   │']);
assert.deepEqual(rows('5 LB',6),['│ ● e9 │','│ 2 RT │','│ ◑ 1  │','│  «   │']);
assert.deepEqual(rows('!1LB',6),['│ ● e9 │','│ !1LB │','│ ◑ 1  │','│  «   │']);
assert.deepEqual(rows('?1LB',5),['│ ● e9 │','│ ◑ 1  │','│  «   │']);
// No board, no row; the border is the session's own whatever the board says.
assert.deepEqual(text(strip({lab:null})).slice(1,3),['│ ● e9 │','│      │']);
for(const state of ['idle','working','blocked'])
 assert.deepEqual(strip({lab:token('!3LB')},{state,frame:2}).rows.map(r=>[r[0],r.at(-1)]),strip({},{state,frame:2}).rows.map(r=>[r[0],r.at(-1)]),state);
// Every harness gets the same strip.
for(const harness of ['codex','opencode'])assert.deepEqual(text(strip({lab:token('?2LB')},{harness})),text(strip({lab:token('?2LB')})));
""")

    def test_a_press_on_the_lab_rows_opens_and_closes_them_where_they_were_drawn(self):
        # Draft A: a press on the LAB rows opens the condition table in place and closes it again,
        # by the host's own open set (as a route or a message); a press on targets drawn for another
        # placement is dropped (review 1597 follow-up).
        self.node(r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const text=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const code=text.slice(text.indexOf('function panelMouse('),text.indexOf('function displayReporter('))
 +text.slice(text.indexOf('function panelAction('),text.indexOf('// Scroll the route box'));
const g={side:true,folded:false,left:false,panelColumn:90,panelCols:40},place='true:true:false:false:40:130:30:false';
const c={fleetView:null,paste:false,relay:false,dragging:false,settings:null,routeDetail:false,routeArea:null,epoch:0,empty:true,
  geometry:()=>g,panelColumns:()=>40,panelRow:()=>0,height:()=>30,terminal:{modes:{mouseTrackingMode:'none'}},
  layoutQueued:false,panelHitsPlacement:place,placement:()=>place,panelOpen:new Set(),layouts:0,queueLayout(){c.layouts++;},queueDisplay(){},
  panelHits:[{row:16,left:2,right:37,action:'lab:grp_1'},{row:17,left:2,right:37,action:'lab:grp_1'}]};
vm.createContext(c);vm.runInContext(code,c);
assert.equal(c.panelMouse(0,101,17,false),true);assert.deepEqual([...c.panelOpen],['lab:grp_1']);
assert.equal(c.panelMouse(0,101,18,false),true);assert.deepEqual([...c.panelOpen],[]);
// A row with no LAB target: nothing opens.
c.panelMouse(0,101,20,false);assert.deepEqual([...c.panelOpen],[]);
// Targets drawn for another placement: dropped.
c.panelHitsPlacement='old';c.panelMouse(0,101,17,false);assert.deepEqual([...c.panelOpen],[]);
assert.equal(c.layouts,2);
""")


if __name__ == "__main__":
    unittest.main()
