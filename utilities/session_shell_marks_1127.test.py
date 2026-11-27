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


class AnswerWaitRouteMarkTest(unittest.TestCase):
    def test_a_blocked_route_reads_half_and_the_waiting_tally_keeps_its_dot(self):
        from fleet import session_line
        self.assertEqual(session_line.route_glyph("blocked")[0], "◑")
        self.assertEqual(session_line.route_glyph("idle")[0], "●")
        self.assertEqual(session_line.route_glyph("stale")[0], "◌")
        # idle and blocked are counted together as `waiting`: that count is not an answer wait and keeps `●`.
        self.assertEqual(dict(session_line._TALLY)["waiting"], "●")



if __name__ == "__main__":
    unittest.main(verbosity=2)
