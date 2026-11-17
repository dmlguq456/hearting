"""11-17 (the person): the route's kind line reads ``code(staged 3/6): execute``.

The kind word is the one bold word of the line (the body color, never dim, in mono, eight colors and NO_COLOR too);
the shape in brackets, the colon and the stage stay plain, the stage keeps its color. A route with no stage ends at the
bracket (``lab(one-shot)``, a direct route's own word), one with no shape reads the kind alone. Short of cells the
values go from the back, then the word ``staged`` (``code(3/6): execute ●``). The three harnesses' panels draw it alike.
Everything here is synthetic.
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
const P=t=>({text:t}),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const names=['plan','plan-check','execute','impl-review','test','report'];
const six=at=>names.map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true,key:'stg2_on'}:{name,mark:''});
const paint=g=>({glyph:P(g),harness:P('claude'),model_text:P('Opus 5.5'),effort:P('xhigh'),ctx:P('38%'),gauge5:[P('━━─── 38%')]});
const R={
 staged:{id:'b',title:'속도 3단계',kind:'working',state:'working',elapsed:'1h',shape:'staged',stages:six(2),kind_word:'code',role:'owner',paint:paint('⠋')},
 solo:{id:'a',title:'셸 고치기',kind:'working',state:'working',elapsed:'41m',tool:'Edit',target:'utilities/session-shell/host.cjs',tool_age:'3m',shape:'one-shot-owner',kind_word:'lab',role:'owner',stages:[{name:'one-shot',current:true,mark:'●'}],paint:paint('⠋')},
 direct:{id:'d',title:'작은 수정',kind:'working',state:'working',elapsed:'3m',shape:'직접',kind_word:'code',stages:[],paint:paint('⠏')},
 failed:{id:'e',title:'PG-50',kind:'dead',state:'dead',elapsed:'54m',shape:'staged',kind_word:'code',stages:[{name:'plan',mark:'✓'},{name:'execute',mark:'✕'},{name:'test',mark:''}],paint:paint('✕')},
};
const STYLES={dim:{dim:true},loc_repo:{foreground:253},stg2_on:{foreground:150,bold:true},g_dead:{foreground:196,bold:true},lvl_g:{foreground:114},lvl_r:{foreground:196,bold:true},g_spin:{foreground:150}};
const base={repo:[P('hearting/')],messages:[],styles:STYLES};
const render=(routes,width,height,{side=true,model={},configuration={},harness='claude'}={})=>view.render({width,height,side,fill:side,harness,
 model:{...base,routes,route_count:routes.length,route_total:routes.length,...model},state:'working',configuration,questions:()=>[]});
const plain=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const draw=(routes,width,height,o)=>plain(render(routes,width,height,o));
const kindRows=rows=>rows.filter(s=>/│ │   (code|lab|frame)\(|│ │   \d\/\d|│ │   (staged|one-shot)/.test(s)).map(s=>s.replace(/^│ │ +/,'').replace(/ +│ │$/,''));
'''


class PanelKindLine1117Test(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    # (가) ----------------------------------------------------------------------------------------------------------
    def test_the_side_box_reads_kind_shape_colon_stage_at_every_width(self):
        self.node(r"""
const all=Object.values(R);
assert.deepEqual(kindRows(draw(all,50,40)),['code(staged 3/6): execute ●','lab(one-shot) · Edit host.cjs 3m','code(직접)','code(staged 2/3): execute ✕']);
assert.deepEqual(kindRows(draw(all,40,40)),['code(staged 3/6): execute ●','lab(one-shot) · Edit host.cjs','code(직접)','code(staged 2/3): execute ✕']);
// 30 cells: the tool goes, then the word `staged` (the stage stays): the same one row for each route.
assert.deepEqual(kindRows(draw(all,30,40)),['code(3/6): execute ●','lab(one-shot) · Edit','code(직접)','code(2/3): execute ✕']);
for(const width of [30,40,50]){const rows=draw(all,width,40);assert(rows.every(s=>cells(s)===width),width);
 for(const old of ['code ·','lab ·','staged 3/6 ·','· execute'])assert(!rows.join('\n').includes(old),old+' '+width);}
// The box is as tall at 30 as at 50 (no row is added).
const high=w=>draw(all,w,40).filter(s=>/^│ │ /.test(s)).length;
assert.equal(high(30),high(50));assert.equal(high(40),high(50));
""")

    def test_the_bottom_layout_reads_the_same_forms_and_narrows_kind_then_tool_then_staged(self):
        self.node(r"""
const cell=(rows,title)=>{const s=rows.find(x=>x.startsWith('│ │ ')&&x.includes(title));return s.split(' │ ')[4].trim();};
const all=Object.values(R);
const wide=draw(all,140,16,{side:false});
assert.deepEqual(['속도','셸 고','작은','PG-50'].map(t=>cell(wide,t)),['code(staged 3/6): execute ●','lab(one-shot)','code(직접)','code(staged 2/3): execute ✕']);
const mid=draw(all,80,16,{side:false});
assert.deepEqual(['속도','PG-50'].map(t=>cell(mid,t).replace(/ +\d+[mh]$/,'')),['3/6: execute ●','2/3: execute ✕']);
assert(!wide.join('').includes('code ·')&&!mid.join('').includes('staged 3/6 execute'));
for(const s of [...wide,...mid])assert(cells(s)===140||cells(s)===80,s);
""")

    def test_only_the_kind_word_is_bold_and_the_stage_keeps_its_color(self):
        self.node(r"""
const stage=route=>{const v=render([route],40,30);return v.rows.find(r=>r.map(p=>p.text).join('').includes(route.kind_word+'('));};
for(const [label,o] of [['256',{}],['light',{configuration:{theme:'light'}}],['mono',{configuration:{theme:'mono'}}],['8 colors',{model:{colors:8}}],['NO_COLOR',{model:{no_color:true}}],
    ['shade',{configuration:{shade:'light'}}]]){
 const row=stage(R.staged);const by=text=>row.find(p=>p.text===text);
 const kind=by('code');assert(kind&&kind.bold===true&&kind.dim!==true,label+JSON.stringify(kind));
 // body color: no hue of its own (the body color), never dim
 assert(kind.foreground===null||kind.foreground===undefined||[253,237].includes(kind.foreground),label+JSON.stringify(kind));
 for(const text of ['(','staged 3/6',')',': ','execute'])assert(by(text)&&by(text).bold!==true,label+' '+text+JSON.stringify(by(text)));
 // the stage keeps its color (Fleet's stage color, #1662) and the mark stays
 if(label==='256'){assert.equal(by('execute').foreground,150);}
 assert(row.some(p=>p.text==='●'),label);
}
// a failed route: the same, and its stage keeps the failure color
const failed=stage(R.failed),f=t=>failed.find(p=>p.text===t);
assert(f('code').bold===true&&f('(').bold!==true&&f('staged 2/3').bold!==true&&f(': ').bold!==true,JSON.stringify(failed));
// the bottom layout's cell: kind bold, the rest not
const bottom=render([R.staged],140,16,{side:false}).rows.find(r=>r.some(p=>p.text==='code'));
assert(bottom.find(p=>p.text==='code').bold===true&&bottom.filter(p=>p.text!=='code'&&['(','staged 3/6',')',': ','execute'].includes(p.text)).every(p=>p.bold!==true));
""")

    def test_a_route_with_no_stage_ends_at_the_bracket_and_one_with_no_shape_reads_the_kind_alone(self):
        self.node(r"""
const nostage={...R.direct,id:'n',shape:'',title:'shapeless'};
const rows=draw([nostage],40,30);
assert(rows.some(s=>/│ │   code +│ │$/.test(s)),rows.join('\n'));
const kindless={...R.staged,kind_word:''};
assert.deepEqual(kindRows(draw([kindless],40,30)),['staged 3/6: execute ●']);
assert.deepEqual(kindRows(draw([{...R.solo,kind_word:''}],40,30)),['one-shot · Edit host.cjs 3m']);
// A direct route's word is the screen language's own, as the backend sends it.
for(const word of ['direct','직접'])assert.deepEqual(kindRows(draw([{...R.direct,shape:word}],40,30)),['code('+word+')']);
""")

    def test_the_three_harness_panels_draw_the_same_line(self):
        self.node(r"""
const want=kindRows(draw(Object.values(R),40,40));
for(const harness of ['claude','codex','opencode'])assert.deepEqual(kindRows(draw(Object.values(R),40,40,{harness})),want,harness);
""")


if __name__ == "__main__":
    unittest.main()
