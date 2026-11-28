"""11-27 (the person, after the design's before/after pictures: "여덟 가지 모두 지음"): the panel writes one meaning in one mark.

1. A closed request keeps which way it went after its result (`✓ ←`, `✕ →`): the result green or red, the direction in the
   request color, a cell between them; a record without `way` takes it from `side`, with neither the result stands alone.
   At 36 cells the head row lets its age go; from 44 it keeps it. The one-row form makes no new row.
2. The MESSAGES popup's request arrow is the route box's request color (`zone_request`), the same characters.
4. A route waiting on the person's answer reads `◑` (Fleet's shared route mark); the waiting tally (idle and blocked together)
   keeps `●`.
5. A closed group is `▸` (STATUS headings, `all N ▸`, BACKLOG groups); an open one stays `▾`.
6. The place is `▶`: the setting being changed, the question the popup is on, the choice being chosen (the rows the popup keeps
   in sight follow it), a picked answer; the answer row's own `› ` lead stays.
8. A hidden route count reads `↑ 2 more` / `↓ 22 more` (Git's `↑2 ↓1` on the REPO row keeps its own form); a note that does not fit
   is not drawn at all.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const dir=process.argv[1],view=require(path.join(dir,'panel-view.cjs'));
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_r:{foreground:217},zone_request:{foreground:43},
  g_blocked:{foreground:174},g_idle:{foreground:186},hb_codex:{foreground:117},harness:{foreground:116}};
const t=text=>({text});
const paint={harness:t('codex'),model_text:t('gpt-5.5'),effort:t('high')};
const req=(over={})=>({id:'rq-a',way:'←',glyph:'✓',who:'[b2]',title:'Review the pagination fix',status:'final answer',age:'9m',
  kind:'request-done',side:'in',paint,details:[],body:[],...over});
const rejected=req({id:'rq-b',way:'→',glyph:'✕',status:'rejected',age:'19m',kind:'request-rejected',side:'out'});
const base={route_count:0,routes:[],messages:[],styles};
const draw=(width,height,model,extra={})=>view.render({width,height,side:extra.side??true,fill:extra.side??true,model:{...base,...model},
  state:'working',sid:'x',configuration:{},...extra});
const lines=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const partsOf=(v,needle)=>{const i=lines(v).findIndex(s=>s.includes(needle));assert(i>=0,needle+'\n'+lines(v).join('\n'));return v.rows[i];};
"""


def node(test, body):
    result = subprocess.run(["node", "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=60)
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return result.stdout


class ClosedRequestDirectionTest(unittest.TestCase):
    def test_the_head_row_keeps_the_direction_after_the_result_at_36_44_and_60(self):
        node(self, r"""
const want={
 36:['│ │ ✓ ← [b2] codex gpt-5.5 high  │ │','│ │ ✕ → [b2] codex gpt-5.5 high  │ │'],
 44:['│ │ ✓ ← [b2] codex gpt-5.5 high       9m │ │','│ │ ✕ → [b2] codex gpt-5.5 high      19m │ │'],
 60:['│ │ ✓ ← [b2] codex gpt-5.5 high                       9m │ │','│ │ ✕ → [b2] codex gpt-5.5 high                      19m │ │']};
for(const width of [36,44,60]){
 const v=draw(width,40,{requests:[req(),rejected]}),all=lines(v);
 for(const row of want[width])assert(all.includes(row),width+'\n'+all.join('\n'));
 // The result and the direction are two parts: green or red, then the request color.
 const done=partsOf(v,'✓ ←'),refused=partsOf(v,'✕ →');
 const at=(row,ch)=>row.find(p=>p.text===ch);
 assert.equal(at(done,'✓').foreground,150);assert.equal(at(done,'←').foreground,43);
 assert.equal(at(refused,'✕').foreground,217);assert.equal(at(refused,'→').foreground,43);
}
""")

    def test_the_one_row_form_and_the_popup_make_no_new_row(self):
        node(self, r"""
// The bottom layout's one-row form at 36: the title gives way, the other session and the status stay.
const v=draw(36,21,{requests:[req(),rejected]},{side:false}),all=lines(v);
assert(all.includes('│ │ ✓ ← [b2] Rev… · final answer │ │'),all.join('\n'));
assert(all.includes('│ │ ✕ → [b2] Review … · rejected │ │'),all.join('\n'));
// The MESSAGES popup's head (one row) at the popup widths of a 48- and a 144-cell window.
for(const [columns,row] of [[40,'✓ ← [b2] codex Review th… · final answer'],[112,'✓ ← [b2] codex gpt-5.5 high Review the pagination fix · final answer']]){
 const rows=view.messagesBody({model:{requests:[req()],styles},columns}).map(r=>r.map(p=>p.text).join(''));
 assert(rows[0].startsWith(row),columns+' '+rows[0]);
 assert(rows.filter(s=>s.includes('[b2]')).length===1,rows.join('\n'));
}
""")

    def test_an_older_record_takes_its_direction_from_its_side_and_with_neither_the_result_stands_alone(self):
        node(self, r"""
const row=r=>lines(draw(60,40,{requests:[r]})).find(s=>s.includes('[b2]'));
assert(row(req({way:undefined,side:'in'})).includes('✓ ← [b2]'));
assert(row(req({way:undefined,side:'out'})).includes('✓ → [b2]'));
const bare=row(req({way:undefined,side:undefined}));
assert(bare.includes('✓ [b2]')&&!/[←→]/.test(bare),bare);
// An open request keeps its one arrow.
assert(row(req({glyph:'←',kind:'request-in',status:'open'})).startsWith('│ │ ← [b2]'));
""")


class MessagesArrowColorTest(unittest.TestCase):
    def test_the_popup_arrow_is_the_route_box_request_color(self):
        node(self, r"""
const open=[req({id:'o1',glyph:'→',way:'→',kind:'request-out',side:'out',status:'partial answer',title:'Test run',age:'2m'}),
            req({id:'o2',glyph:'←',way:'←',kind:'request-in',side:'in',status:'partial answer',title:'Test run',age:'2m'})];
for(const columns of [40,112]){
 const rows=view.messagesBody({model:{requests:open,styles},columns});
 const heads=rows.filter(r=>r.length&&['→','←'].includes(r[0].text));
 assert.equal(heads.length,2,JSON.stringify(rows));
 for(const r of heads)assert.equal(r[0].foreground,43,JSON.stringify(r[0]));
 // The same characters as before: the route box's own row draws them in the same color.
 const box=draw(36,40,{requests:open});
 assert.equal(partsOf(box,'→ [b2]').find(p=>p.text==='→').foreground,43);
}
""")


class HiddenRouteCountTest(unittest.TestCase):
    def test_the_borders_say_more_and_git_keeps_its_own_counts(self):
        node(self, r"""
const route=i=>({id:'rt-'+String(i).padStart(16,'0'),title:'Route '+i,state:'working',kind:'idle',paint:{glyph:{text:'●'}}});
const many={route_count:35,route_total:35,route_label:'route 35',routes:Array.from({length:35},(_,i)=>route(i)),
  git_side:[{text:'main',dim:true},{text:' ↑2',foreground:150},{text:' ↓1',foreground:217}],head:[{text:'[4f] '},{text:'web-api'}]};
for(const width of [36,44,60]){
 const all=lines(draw(width,33,many,{routeScroll:2}));
 const top=all.find(s=>s.includes('35 ROUTE')),bottom=all.find(s=>/↓ \d+ more ─╯/.test(s));
 assert(top&&/ ↑ 2 more ›─╮ │$/.test(top),width+'\n'+all.join('\n'));
 assert(bottom,all.join('\n'));
 // The hidden note is six cells longer than `↑2` / `↓22`: the line before it is six cells shorter, the box as wide.
 assert.equal([...top].length,width);assert.equal([...bottom].length,width);
 assert(!all.some(s=>/[↑↓]\d+ (?:›)?─[╮╯]/.test(s)),'no count in the old form\n'+all.join('\n'));
}
// A count too long for the line shows not at all (never cut).
const tiny=lines(draw(20,12,{...many,route_count:123456,route_total:123456,routes:many.routes},{routeScroll:2}));
assert(!tiny.some(s=>/[↑↓] \d*$|more\S/.test(s)),tiny.join('\n'));
""")


class HiddenCountShortFormTest(unittest.TestCase):
    """Review of #1905 (🔴1): where the whole note does not fit (or would cost the title), the count stands in its short form,
    `↑100 ›` / `↓23`, as before: no width loses it, and the title and the press cells are the ones the short form leaves."""

    def test_a_narrow_bottom_panel_keeps_both_counts_and_its_press_cells(self):
        node(self, r"""
const route=i=>({id:'rt-'+String(i).padStart(16,'0'),title:'Route '+i,state:'idle',kind:'idle',paint:{glyph:{text:'●'}}});
const many=n=>({route_count:n,route_total:n,route_label:'route '+n,routes:Array.from({length:n},(_,i)=>route(i))});
// The review's window: 20 columns, the bottom layout 14 rows high, 130 routes, scrolled by 100.
const v=draw(20,14,many(130),{side:false,routeScroll:100}),all=lines(v);
assert(all.includes('│ ╭────── ↑100 ›─╮ │'),all.join('\n'));         // the short form, the one drawn before
assert(all.includes('│ ╰── ↓ 23 more ─╯ │'),all.join('\n'));         // the whole one where it fits
const top=all.indexOf('│ ╭────── ↑100 ›─╮ │');
assert.deepEqual(v.hits.filter(h=>h.row===top),[{row:top,left:2,right:17,action:'popup:ROUTE'}]);
assert.deepEqual(v.hits.filter(h=>/^popup:ROUTE:/.test(h.action)).map(h=>h.row),[4,5,6,7,8,9,10]);
// At 36 cells both whole notes stand at once, the title whole beside the upper one.
for(const [side,height,n,scroll,upper,lower] of [[true,33,35,2,'│ ╭─ 35 ROUTE ─────── ↑ 2 more ›─╮ │','│ ╰─────────────────── ↓ 7 more ─╯ │'],
  [false,14,130,100,'│ ╭─ route 130 ──── ↑ 100 more ›─╮ │','│ ╰────────────────── ↓ 23 more ─╯ │']]){
 const w=lines(draw(36,height,many(n),{side,routeScroll:scroll}));
 assert(w.includes(upper)&&w.includes(lower),w.join('\n'));
}
// Where the whole note would fit only by taking the title, the short one stands and the title stays.
for(const [width,n,scroll,upper] of [[28,35,2,'│ ╭─ 35 ROUTE ───── ↑2 ›─╮ │'],[31,130,100,'│ ╭─ 130 ROUTE ───── ↑100 ›─╮ │']]){
 const w=lines(draw(width,33,many(n),{routeScroll:scroll}));
 assert(w.includes(upper),w.join('\n'));
}
// The lower border too: its whole note where it fits, else the short one (a narrow bottom panel, a four-digit count).
const low=lines(draw(18,14,many(1300),{side:false,routeScroll:2}));
assert(low.some(s=>/ ↓\d{4} ─╯ │$/.test(s))&&!low.some(s=>s.includes(' more ─╯')),low.join('\n'));
// No width from 18 on loses a count: the upper one always stands, whole or short.
for(const [n,scroll,widths] of [[35,2,[18,19,20,21,22,24,26,28,30,33,36,40,44,52,60]],[130,100,[18,19,20,21,22,24,26,28,30,33,36,44,60]],[1300,1234,[20,24,36,60]]])for(const width of widths){
 const w=lines(draw(width,14,many(n),{side:false,routeScroll:scroll}));
 assert(w.some(s=>s.includes('↑ '+scroll+' more')||s.includes('↑'+scroll+' ')),width+' '+scroll+'\n'+w.join('\n'));
}
""")


class OlderBackendWaitTest(unittest.TestCase):
    """Review of #1905 (🔴2): the route rows pick `◑` from the route's state, so an older backend that sends `●` for a blocked
    route reads as a new one does; running, idle, the waiting tally and no observation keep their marks."""

    def test_old_and_new_input_together_at_36_44_and_60(self):
        node(self, r"""
const st={...styles,g_work_off:{foreground:150},g_work:{foreground:114},g_blocked:{foreground:174}};
const T=(text,k)=>({text,...(st[k]||{})});
const r=(id,kind,glyph,key,el,extra={})=>({id:'rt-'+id.padEnd(16,'0'),title:'Route '+id,state:kind,kind,role:'owner',kind_word:'code',kind_form:'team',
  intensity:'standard',elapsed:el,paint:{glyph:T(glyph,key),harness:T('claude'),model_text:T('Opus 5.5'),effort:T('xhigh')},...extra});
const routes=[r('run','working','●','g_work','31m',{activity:'running'}),r('idle','idle','●','g_work_off','9m'),
  r('oldwait','blocked','●','g_blocked','4m'),r('newwait','blocked','◑','g_blocked','5m'),r('stale','stale','◌','dim','2h')];
for(const width of [36,44,60]){
 const v=view.render({width,height:60,side:true,fill:true,model:{route_count:5,route_total:5,routes,styles:st},state:'working',sid:'x',configuration:{}});
 const all=lines(v),head=t=>all.findIndex(s=>s.includes('Route '+t))-1,mark=i=>all[i].slice(4,5);
 assert.equal(mark(head('run')),'●',all.join('\n'));                     // running: its pulse
 assert.equal(mark(head('idle')),'●');
 assert.equal(mark(head('oldwait')),'◑',all.join('\n'));                // an older backend's `●` for a wait
 assert.equal(mark(head('newwait')),'◑');
 assert.equal(mark(head('stale')),'◌');
 // the owner lines under them: the running one its spinner, the waits `◑`, in the wait's own color
 const owner=t=>all[head(t)+2];
 assert(/^│ │   [⠀-⣿] owner/.test(owner('run')),owner('run'));
 for(const t of ['oldwait','newwait']){assert(owner(t).startsWith('│ │   ◑ owner'),owner(t));
  const part=v.rows[head(t)+2].find(p=>p.text==='◑');assert.equal(part.foreground,174);}
 assert(owner('idle').startsWith('│ │   ● owner')&&owner('stale').startsWith('│ │   ◌ owner'));
}
// The waiting tally counts idle and blocked together: it keeps its dot.
const tally=lines(view.render({width:36,height:40,side:true,fill:true,model:{route_label:'route 5 ⠏1 ●3 ✕1',styles:st},state:'working',sid:'x',configuration:{}}));
assert(tally.some(s=>s.includes('5 ROUTE ⠏1 ●3 ✕1')),tally.join('\n'));
""")


class AnswerWaitRouteMarkTest(unittest.TestCase):
    def test_a_blocked_route_reads_half_and_the_waiting_tally_keeps_its_dot(self):
        from fleet import session_line
        self.assertEqual(session_line.route_glyph("blocked")[0], "◑")
        self.assertEqual(session_line.route_glyph("idle")[0], "●")
        self.assertEqual(session_line.route_glyph("stale")[0], "◌")
        # idle and blocked are counted together as `waiting`: that count is not an answer wait and keeps `●`.
        self.assertEqual(dict(session_line._TALLY)["waiting"], "●")


class FoldAndPlaceTest(unittest.TestCase):
    def test_a_closed_group_is_a_right_triangle_and_the_chosen_row_leads_with_the_place(self):
        node(self, r"""
const list=require(path.join(dir,'status-list.cjs')),board=require(path.join(dir,'backlog-board.cjs'));
const S=(text,dim)=>({text,...(dim?{dim:true}:{})});
const entries=[{rows:[[S('  '),S('▸ '),S('FIX 1')]],key:'g:fix',group:'fix',alt:[[S('  '),S('▾ '),S('FIX 1')]]},
  {rows:[[S('    '),S('item')]],key:'i:fix:1',group:'fix',when:'open'},{rows:[[S('  '),S('full document ›')]],key:'full'}];
const st={sel:'g:fix',fold:new Set(),items:new Set()};
let v=list.layout(entries,st,{width:40});
const text=r=>r.map(p=>p.text).join('');
assert.equal(text(v.rows[0]),'▶ ▸ FIX 1');
// Enter opens it: the same row, `▾`, the place still `▶`, the same press cells.
const hits=list.hits(v);list.press(st,v,'g:fix');v=list.layout(entries,st,{width:40});
assert.equal(text(v.rows[0]),'▶ ▾ FIX 1');assert.equal(text(v.rows[1]),'    item');
assert.deepEqual(list.hits(v).filter(h=>h.row===0),hits.filter(h=>h.row===0));
// The backlog board writes a closed group `▸` and an open one `▾`.
const answer={title:'B',updated:'',counts:{fix:1,task:1,tbd:0,note:0},total:2,sent:2,cut:false,campaigns:['c'],
  items:[['i:k1','fix','2026-11-18','','','One',0],['i:k2','task','2026-11-20','','','Two',0]]};
const b=board.fromAnswer({board:answer}),bs=board.newState(),tb={sel:'',fold:new Set(),items:new Set(),more:{}};board.prepare(b,bs,tb);
const heads=board.entries(b,bs,{width:60}).filter(e=>e.key&&e.key.startsWith('g:')).map(e=>text((tb.fold.has(e.group)&&e.alt?e.alt:e.rows)[0]).trim());
assert.deepEqual(heads,['▾ FIX 1','▸ TASK 1'],JSON.stringify(heads));
// The settings screen: the setting being changed leads with `▶`, the others with a blank; the press cells are where they were.
const s=view.render({width:36,height:30,side:true,model:{styles,labels:{layout:'Layout',side:'Side'}},state:'idle',settings:{index:0,value:view.defaults},configuration:{}});
const rows=lines(s);assert(rows.includes('│ ▶ Layout  ‹ Side ›               │'),rows.join('\n'));
assert(!rows.some(r=>/│ › \S/.test(r)),rows.join('\n'));
assert(s.hits.some(h=>h.action==='setting:0:down'&&h.left===2+10));
""")

    def test_the_status_listing_folds_with_right_triangles(self):
        from fleet import session_status
        parsed = {"title": "T", "updated": "", "summary": "", "groups": [
            {"name": "In progress", "kind": "running", "items": ["a", "b", "c", "d"], "notes": []},
            {"name": "Planned", "kind": "other", "items": ["e"], "notes": []}]}
        rows = ["".join(part for part, _ in entry["rows"][0]) for entry in session_status.listing_entries(parsed, 60) if entry["rows"]]
        alts = ["".join(part for part, _ in entry["alt"][0]) for entry in session_status.listing_entries(parsed, 60) if entry.get("alt")]
        self.assertIn("  all 4 ▸", rows)
        self.assertIn("  ▸ Planned 1", rows)
        self.assertEqual(alts, ["  all 4 ▾", "  ▾ Planned 1"])
        self.assertIn("  full document ›", rows)                 # a press that opens another place keeps `›`

    def test_the_question_popup_marks_its_place_and_keeps_the_answer_row_lead(self):
        node(self, r"""
const create=require(path.join(dir,'block-input.cjs'));
const measure=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const make=(choices,free=false)=>{const b=create({cellWidth:measure,changed(){},action(){},answer:async()=>({state:'submitted'})});
 b.update([{id:'q-1',revision:'r1',answerable:true,request_state:'open',questions:[{id:'q',question:'Which layout?',choices,free_text:free}]}],'',{});
 b.enter();return b;};
const words=rows=>rows.map(r=>r.map(p=>p.text).join(''));
let b=make(['Per release','Per component','Keep']);
b.input(Buffer.from('\x1b[B'));
let rows=words(b.detail({budget:20,columns:40}));
assert(rows[0].startsWith('▶ ◑ '),rows[0]);                         // the question this popup is on
assert(rows.includes('▶ 2  Per component'),rows.join('\n'));       // the choice being chosen
assert(!rows.some(r=>r.startsWith('› ')||r.startsWith('▸ ')),rows.join('\n'));
// Many choices in few rows: the rows kept in sight follow the chosen one (`findIndex` reads the new mark).
b=make(Array.from({length:12},(_,i)=>'choice '+(i+1)));
for(let i=0;i<9;i++)b.input(Buffer.from('\x1b[B'));
rows=words(b.detail({budget:7,columns:40}));
assert(rows.some(r=>r.startsWith('▶ 10  choice 10')),rows.join('\n'));
// A picked answer, then the question changed: the receipt leads with `▶`; the answer row being typed keeps `› `.
b=make(['Per release','Per component']);
b.input(Buffer.from('2'));
b.update([{id:'q-1',revision:'r2',answerable:true,request_state:'open',questions:[{id:'q',question:'Which layout?',choices:['Per release','Per component']}]}],'',{});
rows=words(b.detail({budget:20,columns:40}));
assert(rows.some(r=>r.startsWith('▶ 2 · Per component')),rows.join('\n'));
const w=make(['A','B'],true);w.input(Buffer.from('hello'));
rows=words(w.detail({budget:20,columns:40}));
assert(rows.some(r=>r.startsWith('› hello')),rows.join('\n'));
""")


if __name__ == "__main__":
    unittest.main(verbosity=2)
