"""Spec E9 rule 49: the gear inside the panel, folding and unfolding, and folding by itself when narrow."""
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
// (d) The threshold is one constant: 80 native columns beside the open panel (cols - 40 - 2).
assert.equal(d.NATIVE_MIN,80);
assert.equal(fold(122).folded,false);assert.equal(fold(121).folded,true);
for(const width of [32,60,80])assert.equal(d.fold({cols:width+82,configuration:{...configuration,side_width:width}}).folded,false),
 assert.equal(d.fold({cols:width+81,configuration:{...configuration,side_width:width}}).folded,true);
// The old `side_min_columns` does not take part.
assert.equal(d.fold({cols:130,configuration:{...configuration,side_min_columns:1000}}).folded,false);
// A hand fold stays folded however wide; a hand unfold stays open at its width and lapses with the next.
assert.deepEqual(fold(300,{folded:true}),{folded:true,choice:{folded:true}});
assert.deepEqual(fold(100,{folded:false,cols:100}),{folded:false,choice:{folded:false,cols:100}});
assert.deepEqual(fold(101,{folded:false,cols:100}),{folded:true,choice:null});
assert.deepEqual(fold(140,{folded:false,cols:100}),{folded:false,choice:null});
// An open settings editor or question keeps an open panel open; the bottom layout never folds by itself.
assert.equal(fold(100,null,true).folded,false);
assert.equal(d.fold({cols:40,configuration:{...configuration,layout:'bottom'}}).folded,false);
assert.equal(d.fold({cols:40,choice:{folded:true},configuration:{...configuration,layout:'bottom'}}).folded,true);
// The sizes the native is told: open beside (cols - width - 2), the strip (cols - 8 - 1), under (cols).
const at=(cols,expanded,extra={})=>d.layout({cols,rows:40,expanded,configuration:{...configuration,...extra}});
assert.equal(at(180,true).nativeCols,138);assert.equal(at(180,false).nativeCols,171);assert.equal(at(180,false).panelCols,d.STRIP);
assert.equal(at(121,false).nativeCols,112);assert.equal(at(122,true).nativeCols,80);
assert.deepEqual([at(180,false,{position:'left'}).nativeColumn,at(180,false).panelColumn],[9,172]);
assert.deepEqual([at(180,false,{layout:'bottom'}).side,at(180,false,{layout:'bottom'}).nativeCols],[false,180]);
// Opened by hand where the panel cannot stand beside (native under 48), it opens under the native area.
assert.deepEqual([at(80,true).side,at(80,true).nativeCols],[false,80]);
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


if __name__ == "__main__":
    unittest.main()
