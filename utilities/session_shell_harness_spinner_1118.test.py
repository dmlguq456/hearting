"""11-18 (the person: "스피너 컬러도 하네스에 맞추고"): a working spinner on a panel line that names its harness turns in that
harness's color; every other mark keeps the state's own.

Synthetic scenes only. The route box's owner row, worker rows (boxed and hung), frame legs and the head line's session spinner take
`hb_<harness>`'s color (Claude 173, Codex 147, OpenCode 153); a worker with no known harness, the route's first-line dot, a waiting,
failed or finished mark and the no-color/mono panel read as before; the light face maps the harness color to its deep pair.
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
const text=r=>r.map(p=>p.text).join('');
const HB={claude:173,codex:147,opencode:153};
const STYLES={dim:{dim:true},loc_repo:{foreground:253},g_spin:{foreground:230},g_work:{foreground:150,bold:true},g_work_off:{foreground:150,dim:true},
  g_blocked:{foreground:217},g_dead:{foreground:217,bold:true},hb_claude:{foreground:173},hb_codex:{foreground:147},hb_opencode:{foreground:153}};
const G={text:'⠸',foreground:230};      // (the route's own spinner as the backend paints it: Fleet's vanilla `g_spin`)
const H=h=>({text:h,foreground:HB[h]}),P=t=>({text:t});
const SPIN=new Set([...view.SPIN]);
const doer=(h,m,pct)=>({...(h?{harness:H(h)}:{}),model_text:P(m),ctx:P(pct)});
const worker=(h,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',paint:{glyph:G,...doer(h,'Sonnet 5.5','22%')},...o});
const leg=(h)=>({kind:'working',role:'frame',stage:'frame',elapsed:'4m',paint:{glyph:P('⠸'),...doer(h,'Opus 5.5','18%')}});
const owner=(h,o={})=>({id:'a',title:'a route',kind:'working',state:'working',elapsed:'42m',shape:'staged',stages:[{name:'execute',mark:'●',current:true}],kind_word:'code',
  intensity:'standard',role:'owner',paint:{glyph:P('⠸'),...doer(h,'Opus 5.5','40%')},...o});
const render=(routes,{harness='claude',state='working',configuration={},width=44,height=60,frame=0}={})=>view.render({width,height,side:true,fill:true,harness,frame,
  model:{repo:[P('hearting/')],messages:[],styles:STYLES,routes,route_count:routes.length,route_total:routes.length},state,configuration,questions:()=>[]});
// The spinner parts of a row whose text matches `re`.
const spins=(v,re)=>v.rows.filter(r=>re.test(text(r))).flatMap(r=>r.filter(p=>SPIN.has(p.text)));
'''


class HarnessSpinnerTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_worker_and_frame_spinners_turn_in_their_harness_color(self):
        self.node(r"""
for(const [h,number] of Object.entries(HB)){
  // A boxed worker and a hung worker (`worker_box: none`) of each harness.
  for(const configuration of [{},{worker_box:'none'}]){
    const v=render([owner('claude',{workers_live:[worker(h)]})],{configuration});
    const mine=spins(v,/worker/);
    assert.equal(mine.length,1,h+JSON.stringify(configuration));
    assert.equal(mine[0].foreground,number,h);assert.equal(mine[0].spin,true);assert.notEqual(mine[0].role,'state:working');
  }
  // A frame leg (the route has no owner yet).
  const f=render([{id:'f',title:'a frame',kind:'working',state:'working',elapsed:'4m',shape:'framed',stages:[],kind_word:'frame',role:'frame',paint:{glyph:P('⠸')},workers_live:[leg(h)]}]);
  const legs=spins(f,/frame/);assert(legs.length>=1,h);assert(legs.every(p=>p.foreground===number),h+JSON.stringify(legs));
}
// Claude owner, Codex worker and OpenCode worker in one route: each spinner has its own line's harness color.
const mixed=render([owner('claude',{workers_live:[worker('codex'),worker('opencode')]})]);
assert.deepEqual(spins(mixed,/worker/).map(p=>p.foreground),[147,153]);
""")

    def test_an_owner_that_does_the_work_itself_spins_in_its_harness_color_and_a_supervising_one_keeps_its_dim_dot(self):
        self.node(r"""
for(const [h,number] of Object.entries(HB)){
  const solo=render([owner(h,{workers_live:[],tool:'Bash'})],{configuration:{worker_box:'none'}});
  const mine=spins(solo,/owner/);assert.equal(mine.length,1,h);assert.equal(mine[0].foreground,number,h);
}
const supervising=render([owner('claude',{workers_live:[worker('codex')]})]);
assert.equal(spins(supervising,/owner/).length,0);                      // the owner's own mark is the dim `●` while its workers run
const dot=supervising.rows.find(r=>/● owner/.test(text(r))).find(p=>p.text==='●');assert.equal(dot.dim,true);
""")

    def test_lines_without_a_known_harness_and_the_other_marks_keep_the_states_color(self):
        self.node(r"""
// No harness named on the line: the state's own spinner (`g_spin`'s vanilla), as before.
const none=render([owner('claude',{workers_live:[worker(null)]})]);
const w=spins(none,/worker/);assert.equal(w.length,1);assert.equal(w[0].foreground,230);assert.equal(w[0].role,'state:working');
// The route's first line: a dot in the state's color, not a spinner.
const head=render([owner('codex',{workers_live:[worker('codex')]})]).rows.find(r=>/code\(standard\)/.test(text(r)));
assert(head.every(p=>!SPIN.has(p.text)));
const dot=head.find(p=>p.text==='●');assert(dot&&dot.foreground!==147,JSON.stringify(dot));
// A finished or failed worker's mark and a waiting owner keep their state colors.
const done=render([owner('claude',{workers_live:[worker('codex',{kind:'dead',paint:{glyph:{text:'✕',foreground:217,bold:true},...doer('codex','GPT-5.5','9%')}})]})]);
const cross=done.rows.flat().find(p=>p.text==='✕');assert(cross);assert.equal(cross.foreground,217);
""")

    def test_the_head_lines_session_spinner_is_its_harness_color_in_both_forms(self):
        self.node(r"""
for(const [h,number] of Object.entries(HB)){
  for(const folded of [false,true]){
    const v=view.render({width:44,height:folded?3:40,side:true,fill:true,harness:h,frame:0,folded,model:{repo:[P('hearting/')],messages:[],styles:STYLES,tag:'[e9]'},state:'working',configuration:{},questions:()=>[]});
    const head=v.rows.flat().filter(p=>p.spin===true&&SPIN.has(p.text));
    assert(head.length>=1,h+folded);assert.equal(head[0].foreground,number,h+' '+folded);assert.equal(head[0].role,'session');
  }
}
// Idle and waiting sessions keep their own marks and colors.
for(const state of ['idle','blocked']){
  const v=view.render({width:44,height:40,side:true,fill:true,harness:'codex',frame:0,model:{repo:[P('hearting/')],messages:[],styles:STYLES,tag:'[e9]'},state,configuration:{},questions:()=>[]});
  assert(v.rows.flat().every(p=>!(p.spin===true)),state);
}
""")

    def test_the_light_face_maps_the_harness_colors_to_their_deep_pairs_and_mono_keeps_no_color(self):
        self.node(r"""
const v=render([owner('claude',{workers_live:[worker('claude'),worker('codex'),worker('opencode')]})]);
const light=view.appearance(v.rows,{theme:'light'},{colors:256,background:'light'});
const deep=light.flatMap(r=>r.filter(p=>SPIN.has(p.text)&&p.spin)).map(p=>p.foreground);
assert.deepEqual(deep.slice(-3),[166,25,31]);                               // claude 173 -> 166, codex 147 -> 25, opencode 153 -> 31
const dark=view.appearance(v.rows,{theme:'terminal'},{colors:256,background:'dark'});
assert.deepEqual(dark.flatMap(r=>r.filter(p=>SPIN.has(p.text)&&p.spin)).map(p=>p.foreground).slice(-3),[173,147,153]);
const mono=view.appearance(v.rows,{theme:'mono'},{colors:256});
assert(mono.flat().filter(p=>SPIN.has(p.text)&&p.spin).every(p=>p.foreground===null));
// No color: the styles carry none, so the spinner keeps the state's own (no) color.
const plain=render([owner('claude',{workers_live:[worker('codex')]})]);
""")


if __name__ == "__main__":
    unittest.main()
