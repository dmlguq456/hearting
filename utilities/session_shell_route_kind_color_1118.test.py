"""11-18 (the person: "code, lab 같은 라우트 유형에 컬러를 넣어줘" / "강도도 컬러를 넣어주자" / the time "route 첫 줄 오른쪽 끝에 둔다"): the shell
panel's ROUTE box.

  * A route's kind word wears its field's hue (code 69, lab 214, design 205, docs 180, research 38 in 256 colors) and the bracket
    after it the same hue stepped by weight: quick dim, standard plain, strong, thorough and adversarial bold. A route with no field
    hue keeps the bold body word and the dim bracket; mono, NO_COLOR and fewer than 256 colors draw as before.
  * The light face takes the dark counterparts (kind and zone hues).
  * A route's time stands at the right end of its first line at every width; short of cells the middle text goes first (cut with
    `…`, then whole), the kind word and the time stay; a route with no time leaves the end empty.
  * Under the native area the command column (`>_ Edit`) is gone and the route's NOW stands on the row under it.
Everything is synthetic; the styles are the palette's own.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

FIELDS = ("code", "lab", "design", "docs", "research")
HUES = {"code": 69, "lab": 214, "design": 205, "docs": 180, "research": 38}
KEYS = ["dim", "loc_repo", "g_work", "g_work_off", "g_idle", "g_blocked", "g_spin", "lvl_g", "hb_claude", "hb_codex", "hb_opencode",
        "tag_steward", "zone_direct", "zone_dispatch", "zone_request",
        *("kind%s_%s" % (w, f) for f in FIELDS for w in ("", "q", "s", "b"))]

FIXTURE = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]+'/panel-view.cjs'),STYLES=JSON.parse(process.argv[2]);
const P=t=>({text:t}),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const names=['plan','plan-check','execute','impl-review','test','report'];
const six=at=>names.map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true}:{name,mark:''});
const doer=(h,m,e,pct,now)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),...(now?{now}:{})});
const route=(o={})=>({id:'a',zone:'dispatch',title:'갈래를 route에 기록',kind:'working',state:'working',elapsed:'1h 52m',shape:'staged',stages:six(2),
  kind_word:'code',kind_field:'code',intensity:'standard',role:'owner',paint:{glyph:P('⠸'),...doer('claude','Opus 5.5','xhigh','41%','검토 지적 둘을 고치는 중')},workers_live:[],...o});
const mk=(word,field,intensity,o={})=>route({id:word,kind_word:word,kind_field:field,intensity,...o});
const render=(routes,width,height,{side=true,model={},configuration={},background='',styles=STYLES}={})=>view.render({width,height,side,fill:side,harness:'claude',frame:0,background,
 model:{repo:[P('hearting/')],repo_side:[P('hearting')],git_side:[P('main ✓')],tag:'[c7]',messages:[],styles,routes,route_count:routes.length,route_total:routes.length,...model},
 state:'working',configuration,questions:()=>[]});
const text=r=>r.map(p=>p.text).join('');
const plain=v=>v.rows.map(text);
const box=rows=>{const a=rows.findIndex(s=>/╭─ (\d+ )?ROUTE|╭─ route \d+/.test(s)),z=rows.findIndex((s,i)=>i>a&&/^│ ╰/.test(s));
  return a<0?[]:rows.slice(a+1,z).map(s=>s.replace(/^│ │ ?/,'').replace(/ ?│ │$/,'').replace(/\s+$/,'')).filter(s=>!/^─ (direct|dispatch|request) ─+$/.test(s));};
const tm=(s,time,w=44)=>s+' '.repeat(Math.max(1,w-8-cells(s)-cells(time)))+time;
const find=(v,re)=>v.rows.flat().find(p=>re.test(p.text));
'''


class KindColorPanelTest(unittest.TestCase):
    def node(self, script, colors=256, no_color=False):
        from fleet.palette import style_components
        styles = {key: style_components(key, colors=colors, no_color=no_color) for key in KEYS}
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL), json.dumps(styles)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_kind_word_wears_its_fields_hue_and_the_bracket_the_same_hue_stepped_by_weight(self):
        self.node(r"""
const HUE={code:69,lab:214,design:205,docs:180,research:38};
for(const [field,hue] of Object.entries(HUE)){
 for(const [word,weight] of [['quick','dim'],['standard','plain'],['strong','bold'],['thorough','bold'],['adversarial','bold']]){
  const v=render([mk(field==='docs'?'draft':field,field,word)],44,40);
  const kind=find(v,new RegExp('^'+(field==='docs'?'draft':field)+'$')),bracket=find(v,/^\(\w+\)$/);
  assert(kind&&kind.foreground===hue&&kind.bold===true&&kind.dim!==true,field+JSON.stringify(kind));
  assert(bracket&&bracket.text==='('+word+')'&&bracket.foreground===hue,field+word+JSON.stringify(bracket));
  assert.equal(bracket.dim===true,weight==='dim',field+word+JSON.stringify(bracket));
  assert.equal(bracket.bold===true,weight==='bold',field+word+JSON.stringify(bracket));
 }
}
""")

    def test_a_route_with_no_field_hue_keeps_the_bold_body_word_and_the_dim_bracket(self):
        self.node(r"""
for(const field of ['plan','admin','other','']){
 const v=render([mk('spec',field,'standard')],44,40);
 const kind=find(v,/^spec$/),bracket=find(v,/^\(standard\)$/);
 assert(kind&&kind.bold===true&&(kind.foreground===null||kind.foreground===undefined),field+JSON.stringify(kind));
 assert(bracket&&bracket.dim===true&&bracket.bold!==true&&(bracket.foreground===null||bracket.foreground===undefined),field+JSON.stringify(bracket));
}
// A frame route's chain: what is behind stays dim; the route's own kind wears its hue, an earlier leg named after a field does not.
const chain=render([mk('code','code','standard',{chain:[{name:'frame',state:'done'},{name:'code',state:'active'}]})],60,40);
const past=find(chain,/^frame ✓$/);assert(past&&past.dim===true&&!past.foreground,JSON.stringify(past));
assert.equal(find(chain,/^code$/).foreground,69);assert.equal(find(chain,/^\(standard\)$/).foreground,69);
""")

    def test_mono_no_color_and_fewer_than_256_colors_draw_as_before(self):
        # (The backend's styles carry no hue under 256 colors or NO_COLOR; the panel also checks its own mode.)
        for colors, no_color, configuration, model in ((256, False, "{theme:'mono'}", "{}"), (8, False, "{}", "{colors:8}"),
                                                      (256, True, "{}", "{no_color:true}")):
            with self.subTest(colors=colors, no_color=no_color, theme=configuration):
                self.node(r"""
const v=render([mk('code','code','quick'),mk('lab','lab','strong',{id:'l'})],44,40,{configuration:%s,model:%s});
for(const word of ['code','lab']){const p=find(v,new RegExp('^'+word+'$'));assert(p&&p.bold===true&&!p.foreground,JSON.stringify(p));}
// (The bracket is what a route with no field hue draws in the same mode: the mode's own `dim`, never a weight step or a hue.)
const bare=render([mk('spec','','quick'),mk('spec','','strong',{id:'l'})],44,40,{configuration:%s,model:%s});
for(const w of ['(quick)','(strong)']){const p=v.rows.flat().find(x=>x.text===w),q=bare.rows.flat().find(x=>x.text===w);assert(p&&q&&p.dim===q.dim&&p.bold===q.bold&&!p.foreground,JSON.stringify([p,q]));}
""" % (configuration, model, configuration, model), colors=colors, no_color=no_color)

    def test_the_light_face_takes_the_dark_counterparts(self):
        self.node(r"""
const LIGHT={code:26,lab:130,design:162,docs:94,research:24};
for(const [field,dark] of Object.entries(LIGHT)){
 const v=render([mk(field==='docs'?'draft':field,field,'standard')],44,40,{configuration:{theme:'light'}});
 assert.equal(find(v,new RegExp('^'+(field==='docs'?'draft':field)+'$')).foreground,dark,field);
 assert.equal(find(v,/^\(standard\)$/).foreground,dark,field);
}
// A terminal that tells its background to be light does the same without the setting.
assert.equal(find(render([mk('code','code','standard')],44,40,{background:'light'}),/^code$/).foreground,26);
// The three zone names too (orange, violet, aqua): contrast 4.5 or more on white.
const z=render([mk('code','code','standard',{zone:'direct'}),mk('code','code','standard',{id:'b'}),],44,60,{configuration:{theme:'light'},
 model:{requests:[{id:'q1',kind:'request-out',way:'→',who:'[c2]',title:'t',status:'open'}]}});
for(const [name,dark] of [['direct',130],['dispatch',55],['request',29]])assert.equal(find(z,new RegExp('^'+name+'$')).foreground,dark,name);
// The dark face keeps the numbers.
assert.equal(find(render([mk('code','code','standard')],44,40),/^code$/).foreground,69);
""")

    def test_the_time_stands_at_the_right_end_of_the_first_line_at_every_width_and_the_middle_text_goes_first(self):
        self.node(r"""
const rows=w=>box(plain(render([route()],w,60)));
// (11-18, review C3: the stage text is on the first line, `· 3/6` beside the workers' box, the name too where no box holds it; short of cells
// the name goes first, then the stage text, before the chain, the intensity, the kind word or the time.)
assert.equal(rows(44)[0],tm('● code(standard) · 3/6','1h 52m'));
assert.equal(rows(32)[0],tm('● code(standard)','1h 52m',32));   // (the panel is never narrower than 32 cells: `sideWidth`)
assert.equal(rows(60)[0],tm('● code(standard) · 3/6: execute','1h 52m',60));   // (this route has no worker: no box holds the stage name)
// The kind word and the time stay down to the narrowest: the chain's earlier legs and the intensity go before them.
for(let w=24;w<=60;w++){
 const first=rows(w)[0];assert(/1h 52m$/.test(first)&&/^● code/.test(first),w+' '+first);
 assert.equal(cells(first)<=w-8,true,w+' '+first);
}
// A frame route that waits for the person's direction: the middle text is cut with `…` before the kind word or the time go.
const wait=route({kind:'blocked',state:'blocked',shape:'framed',kind_word:'frame',kind_field:undefined,elapsed:'4m',stages:[],intensity:'',chain:[{name:'frame',state:'done'}],paint:{glyph:P('◑')}});
assert.equal(box(plain(render([wait],50,60)))[0],tm('◑ frame → ?? · waiting for direction','4m',50));
const cut=box(plain(render([wait],36,60)))[0];
assert(/^◑ frame → \?\? · .*…\s+4m$/.test(cut),cut);
for(let w=22;w<=60;w++){const f=box(plain(render([wait],w,60)))[0];assert(/ 4m$/.test(f)&&/frame/.test(f)&&cells(f)<=w-8,w+' '+f);}
// No time: the end stays empty (no empty row, no stray digits).
const none=box(plain(render([route({elapsed:''})],44,60)));
assert.equal(none[0],'● code(standard) · 3/6: execute');assert(!none.some(s=>/^\s+$/.test(s)));
// The owner's row carries its context, and the route's time stays only on the kind line.
const wide=box(plain(render([route()],72,60)));
assert(wide.some(s=>/^ +⠋ owner .*41%$/.test(s)),wide.join('\n'));
assert(!wide.slice(1).some(s=>/1h 52m/.test(s)),wide.join('\n'));
""")

    def test_a_direct_route_keeps_its_time_on_its_first_line_with_no_row_of_its_own(self):
        self.node(r"""
const direct=mk('code','code','',{id:'d',zone:'direct',shape:'direct',stages:[],elapsed:'2m',role:'',paint:{glyph:P('⠸')}});
const rows=box(plain(render([direct],44,60)));
assert.deepEqual(rows,[tm('● code','2m'),'  갈래를 route에 기록'],rows.join('\n'));
""")

    def test_under_the_native_area_the_command_column_is_gone_and_the_now_stands_under_the_row(self):
        self.node(r"""
const r=route({tool:'Edit',target:'x.cjs',stages:six(2)});
for(const w of [90,100,140]){   // (the time reads `1h 52m`, as the producer words it)
 const rows=plain(render([r],w,22,{side:false}));
 assert(!rows.some(s=>s.includes('>_')),w+'\n'+rows.join('\n'));
 for(const s of rows)assert.equal(cells(s),w,s);
 const i=rows.findIndex(s=>s.includes('갈래를 route'));      // (11-19: at 90 the kind cell takes more room, the title is cut with `…`)
 assert(i>0&&/│ │   검토 지적 둘을 고치는 중 +│ │$/.test(rows[i+1]),w+'\n'+rows.join('\n'));
}
// The kind word wears its hue there too.
assert.equal(find(render([r],140,22,{side:false}),/^code$/).foreground,69);
// Short of rows the NOW row is the first to go.
const short=plain(render([r],100,8,{side:false}));
assert(!short.some(s=>s.includes('검토 지적')),short.join('\n'));
""")


class KindColorDataTest(unittest.TestCase):
    def test_the_panels_route_row_carries_the_field_whose_hue_the_kind_word_takes(self):
        from fleet import session_panel
        view = {"route_panels": {"claude\0sid": [
            {"id": "r1", "title": "t", "state": "working", "shape": "staged", "capability": "draft", "field": "docs"},
            {"id": "r2", "title": "t", "state": "working", "shape": "staged", "capability": "code", "field": ""},
            {"id": "r3", "title": "t", "state": "working", "shape": "staged", "capability": "spec", "field": "plan"},
            {"id": "r4", "title": "t", "state": "working", "shape": "staged", "capability": "draft"},
            {"id": "r5", "title": "t", "state": "working", "shape": "staged", "capability": "unit", "field": "lab"}]}}
        panel = session_panel.presentation({}, view, "claude", "sid", 80)
        self.assertEqual([route.get("kind_field") for route in panel["routes"]], ["docs", "code", None, None, "lab"])

    def test_the_panel_asks_for_every_kind_key(self):
        import session_shell
        for field in FIELDS:
            for weight in ("", "q", "s", "b"):
                self.assertIn("kind%s_%s" % (weight, field), session_shell.PANEL_STYLE_KEYS)


if __name__ == "__main__":
    unittest.main()
