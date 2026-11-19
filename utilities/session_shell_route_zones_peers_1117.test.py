"""11-17 (the person: route zone sketch 1 and the PEERS sketch): the shell panel's ROUTE box has three named zones and the head
has a PEERS line.

  * The ROUTE box is one box with three zones under named rules, `─ direct ─`, `─ dispatch ─`, `─ request ─`, in that order;
    each zone's name and mark have their own color (none a harness color or the steward pink); a zone with no rows has no
    rule; no row at all is the empty box it was. The requests are the third zone (no `PEER REQUEST` tab in the title); in
    the `direct` zone the line has no `(direct)`.
  * The head's `PEERS` line, under GIT: `[c7] ⚑  ─ [d4] [3b]`, the same order from every window; numbers in their harness's
    color (this window's own bold), the flag the steward pink, the dash and the `·` dim; `+N` short of cells; no line where
    the window has no relation.
  * The data: the same steward relations Fleet reads (same repository, registered), handed to the panel by the cache.
Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

FIXTURE = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]+'/panel-view.cjs');
const P=t=>({text:t}),D=t=>({text:t,dim:true});
const names=['plan','plan-check','execute','impl-review','test','report'];
const six=at=>names.map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true,key:'stg2_on'}:{name,mark:''});
const doer=(h,m,e,pct,bar)=>({harness:P(h),model_text:P(m),effort:P(e),ctx:P(pct),gauge_row:[P(bar),D('─'.repeat(8-bar.length)),D(' '),P(pct)]});
const worker=(h,m,e,pct,bar,o={})=>({kind:'working',role:'worker',stage:'execute',elapsed:'12m',...o,paint:{glyph:P('⠸'),...doer(h,m,e,pct,bar),now:'route 기록에 갈래 칸을 더하는 중'}});
const direct={id:'d1',zone:'direct',title:'README 넷째 줄 고침',kind:'working',state:'working',elapsed:'2m',shape:'직접',intensity:'direct',kind_word:'code',stages:[],paint:{glyph:P('⠸')}};
const directQuick={...direct,id:'d2',intensity:'quick'};
const dispatch={id:'a',zone:'dispatch',title:'갈래를 route에 기록',kind:'working',state:'working',elapsed:'1h 52m',shape:'staged',stages:six(2),kind_word:'code',intensity:'standard',role:'owner',
  paint:{glyph:P('⠸'),...doer('claude','Opus 5.5','xhigh','41%','━━━━'),now:'검토 지적 둘을 고치고 시험을 다시 돌리는 중'},workers_live:[worker('claude','Sonnet 5.5','high','22%','━━',{tool:'Edit',target:'backend'})]};
const bare={...dispatch,id:'b',zone:undefined,title:'zone-less route'};
const requests=[{id:'q1',kind:'request-out',way:'→',who:'[c2]',title:'보낸 요청의 제목',status:'open'},{id:'q2',kind:'request-in',way:'◂',who:'[6c]',title:'받은 요청의 제목',status:'partial'}];
const STYLES={dim:{dim:true},loc_repo:{foreground:253},zone_direct:{foreground:208},zone_dispatch:{foreground:99},zone_request:{foreground:43},
  tag_steward:{foreground:219,bold:true},tag_steward_dim:{foreground:219,dim:true},hb_claude:{foreground:116},hb_codex:{foreground:176},hb_opencode:{foreground:147},
  g_work:{foreground:150,bold:true},g_work_off:{foreground:150,dim:true},lvl_g:{foreground:114},g_spin:{foreground:150}};
const render=(routes,reqs,width,height,{side=true,peers,model={},styles=STYLES,configuration={}}={})=>view.render({width,height,side,fill:side,harness:'claude',frame:0,
 model:{repo:[P('hearting/')],repo_side:[P('hearting')],git_side:[P('main ✓')],tag:'[c7]',messages:[],styles,routes,requests:reqs,route_count:routes.length,route_total:routes.length,peers,...model},
 state:'working',configuration,questions:()=>[]});
const text=r=>r.map(p=>p.text).join('');
const plain=v=>v.rows.map(text);
const draw=(routes,reqs,width,height,o)=>plain(render(routes,reqs,width,height,o));
// The box's rows, the frame and the box's own sides taken off.
const box=rows=>{const a=rows.findIndex(s=>/╭─ (\d+ )?ROUTE|╭─ route \d+/.test(s)),z=rows.findIndex((s,i)=>i>a&&/^│ ╰/.test(s));
  return a<0?[]:rows.slice(a+1,z).map(s=>s.replace(/^│ │ ?/,'').replace(/ ?│ │$/,'').replace(/\s+$/,''));};
const rules=rows=>rows.filter(s=>/^─ (direct|dispatch|request) ─+$/.test(s));
const zr=(n,w=36)=>'─ '+n+' '+'─'.repeat(w-3-n.length);
const partOf=(v,re)=>v.rows.flat().find(p=>re.test(p.text));
const peerPart=(v,re)=>v.rows.find(r=>/^PEERS/.test(text(r)))?.find(p=>re.test(p.text));
'''


class ZonesTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_box_with_all_three_zones_names_each_in_order(self):
        self.node(r"""
const rows=box(draw([direct,dispatch],requests,44,60));
assert.deepEqual(rules(rows),[zr('direct'),zr('dispatch'),zr('request')]);
assert.deepEqual(rows,[
 zr('direct'),
 '● code                            2m',
 '  README 넷째 줄 고침',
 zr('dispatch'),
 '● code(standard) · 3/6        1h 52m',
 '  갈래를 route에 기록',
 '  ● owner  claude Opus 5.5 xhigh',
 '    CTX ━━━━──── 41%',
 '    검토 지적 둘을 고치고 시험을',
 '    다시 돌리는 중',
 '  ┌ 3/6: execute ──────────────────┐',
 '  │ ⠋ worker claude Sonnet 5.5     │',
 '  │   CTX ━━────── 22% · 12m       │',
 '  │   route 기록에 갈래 칸을       │',
 '  │   더하는 중                    │',
 '  └────────────────────────────────┘',
 zr('request'),
 '→ [c2]',
 '  보낸 요청의 제목',
 '  open',
 '◂ [6c]',
 '  받은 요청의 제목',
 '  partial',
]);
// The session rows are the same under `worker_box: none` apart from the box: the stage row and the `↳` indent come back.
const none=box(draw([direct,dispatch],requests,44,60,{configuration:{worker_box:'none'}}));
assert.deepEqual(none.slice(none.indexOf(zr('dispatch'))+1,none.indexOf(zr('request'))),[
 '● code(standard)              1h 52m',
 '  갈래를 route에 기록',
 '  ● owner  claude Opus 5.5 xhigh',
 '    CTX ━━━━──── 41%',
 '    검토 지적 둘을 고치고 시험을',
 '    다시 돌리는 중',
 '    3/6: execute',
 '    ↳ ⠋ worker claude Sonnet 5.5',
 '        CTX ━━────── 22% · 12m',
 '        route 기록에 갈래 칸을',
 '        더하는 중',
],none.join('\n'));
// The title counts the routes alone: the requests are a zone, not a tab.
const top=draw([direct,dispatch],requests,44,60).find(s=>/ROUTE/.test(s));
assert(/╭─ 2 ROUTE ─+╮/.test(top)&&!/REQUEST/.test(top),top);
// The same box beside the requests' order: they are listed after the routes whatever the order they came in.
const mixed=box(draw([dispatch,direct,bare],requests,44,80));
assert.deepEqual(rules(mixed).map(s=>s.split(' ')[1]),['direct','dispatch','request']);
assert(mixed.indexOf(zr('direct'))<mixed.findIndex(s=>/code\(standard\)/.test(s)));
""")

    def test_a_zone_with_no_row_has_no_rule_and_no_row_at_all_is_the_empty_box(self):
        self.node(r"""
const one=box(draw([direct],[],44,60));
assert.deepEqual(rules(one),[zr('direct')]);
assert.equal(one.length,3,one.join('\n'));      // (11-18: the time stands on the first line, no row of its own)
const disp=box(draw([dispatch],[],44,60));
assert.deepEqual(rules(disp).map(s=>s.split(' ')[1]),['dispatch']);
const reqs=draw([],requests,44,60);
assert(reqs.some(s=>/╭─ ROUTE ─+╮/.test(s)),reqs.join('\n'));     // requests and no route: `ROUTE`, no tab text
assert.deepEqual(rules(box(reqs)).map(s=>s.split(' ')[1]),['request']);
assert(!reqs.some(s=>/PEER REQUEST|REQUEST/.test(s)));
const none=draw([],[],44,60);
assert(!none.some(s=>/ROUTE|─ (direct|dispatch|request) ─/.test(s)),none.join('\n'));   // the empty form it was
// A route with no recorded zone stands in `dispatch`.
assert.deepEqual(rules(box(draw([bare],[],44,60))).map(s=>s.split(' ')[1]),['dispatch']);
""")

    def test_the_direct_zone_drops_the_direct_word_and_keeps_a_real_intensity(self):
        self.node(r"""
const rows=box(draw([direct,directQuick,dispatch],[],44,60));
assert(rows.includes('● code                            2m'),rows.join('\n'));
assert(rows.includes('● code(quick)                     2m'),rows.join('\n'));
assert(rows.includes('● code(standard) · 3/6        1h 52m'));
assert(!rows.some(s=>/\(direct\)|\(직접\)|직접/.test(s)));
// With no intensity recorded the shape's word stood in the brackets; in the direct zone it is gone too.
const noIntensity=box(draw([{...direct,intensity:undefined}],[],44,60));
assert(noIntensity.includes('● code                            2m'),noIntensity.join('\n'));
// The bottom layout reads `code` for it as well.
const bottom=draw([direct,dispatch],[],110,16,{side:false}).join('\n');
assert(!/code\(direct\)|code\(직접\)/.test(bottom)&&/│ code +2m/.test(bottom),bottom);
""")

    def test_short_of_rows_content_rows_go_first_and_a_zone_keeps_its_rule_while_one_of_its_rows_shows(self):
        self.node(r"""
// A zone's rule stands for as long as one row of that zone shows: every row has its zone's rule above it, a rule has a row
// under it, the zones keep their order, and a rule that goes does not come back at a lower height.
const zones=['direct','dispatch','request'];
let was=3,shrunk=false,lines=0;
for(let h=70;h>=12;h--){
 const rows=draw([direct,dispatch],requests,44,h);
 const inner=box(rows);
 if(!inner.length)break;
 const named=rules(inner).map(s=>s.split(' ')[1]);
 assert.deepEqual(named,zones.filter(z=>named.includes(z)),h+'\n'+inner.join('\n'));
 assert(inner[0]&&/^─ \w+ ─/.test(inner[0]),h+'\n'+inner.join('\n'));
 inner.forEach((s,i)=>{if(/^─ \w+ ─/.test(s))assert(i+1<inner.length&&!/^─ \w+ ─/.test(inner[i+1]),h+' a rule with no row under it\n'+inner.join('\n'));});
 assert(named.length<=was,h);was=named.length;
 // The content rows are what shrink first: the box has all three rules while it still has a row of every zone.
 if(inner.some(s=>/README/.test(s))&&inner.some(s=>/code\(standard\)|갈래/.test(s))&&inner.some(s=>/\[c2\]/.test(s)))assert.equal(named.length,3,h+'\n'+inner.join('\n'));
 if(inner.length<lines)shrunk=true;lines=inner.length;
}
assert(shrunk);
// The README demo's size (a 40-cell panel, 43 rows) names all three zones, whichever kind of rows the box has.
const demo=box(draw([direct,dispatch],requests,40,43));
assert.deepEqual(rules(demo).map(s=>s.split(' ')[1]),zones,demo.join('\n'));
// Beside the native area at a width of the narrowest labelled layout the rules still hold their zone's name.
const narrow=box(draw([direct],[],32,40));
assert(rules(narrow).length===1&&/^─ direct ─+$/.test(narrow[0]),narrow.join('\n'));
// Only where one route with its rule would no longer fit, and the box would go whole, the rule goes: the route stays without it.
const last=[];for(let h=14;h>=7;h--){const inner=box(draw([direct],[],44,h));last.push([h,inner.length,rules(inner).length]);}
assert(last.some(([h,n,r])=>n>0&&r===0),JSON.stringify(last));                  // a route without its name rather than no route
assert(last.every(([h,n,r],i)=>i===0||r<=last[i-1][2]),JSON.stringify(last));     // and a rule that went does not come back lower
// A box scrolled to fewer routes than there are keeps the rule of each zone it shows a row of.
const tight=box(draw([direct,dispatch],requests,44,14));
assert(rules(tight).length>=1&&/^─ \w+ ─/.test(tight[0]),tight.join('\n'));
""")

    def test_each_zone_wears_its_own_color_and_a_working_direct_dot_wears_its_zone(self):
        self.node(r"""
const v=render([direct,dispatch],requests,44,60);
const name=n=>v.rows.flat().find(p=>p.text===n);
assert.equal(name('direct').foreground,208);assert.equal(name('dispatch').foreground,99);assert.equal(name('request').foreground,43);
// Three different colors, none a harness color, the steward pink or a status color of the box (working green, idle yellow, coral,
// the spinner's vanilla).
const used=new Set([208,99,43]);assert.equal(used.size,3);
for(const other of [116,176,147,219,150,186,217,230,114,253])assert(!used.has(other));
// The direct route's mark is its zone's color; a dispatch route's is the working green; a request's arrow the request color.
const mark=v.rows.find(r=>/● code\s+2m\s+│ │$/.test(text(r)));
assert(mark,'no direct first line');
const dot=mark.find(p=>p.text==='●');
assert.equal(dot.foreground,208);
const arrow=v.rows.flat().find(p=>p.text==='→');
assert.equal(arrow.foreground,43);
// The rule's dashes stay dim (the zone name alone has the color).
const rule=v.rows.find(r=>/─ direct ─/.test(text(r)));
assert(rule.filter(p=>/^─+$/.test(p.text)).every(p=>p.dim===true));
// Without color no hue is carried (the names stay words).
for(const o of [{configuration:{theme:'mono'},styles:{dim:{dim:true},loc_repo:{}}},{model:{no_color:true},styles:{dim:{dim:true},loc_repo:{}}}]){
 const w=render([direct,dispatch],requests,44,60,o);
 assert(!w.rows.flat().some(p=>p.foreground!==undefined&&p.foreground!==null),'hue in a colorless render');
 assert(plain(w).some(s=>/─ direct ─/.test(s)));
}
""")

    def test_the_three_zone_colors_are_distinct_from_each_other_the_harness_colors_and_the_steward_pink(self):
        from fleet.palette import style_components
        for colors in (256, 16, 8):
            zone = {key: style_components(key, colors=colors)["foreground"] for key in ("zone_direct", "zone_dispatch", "zone_request")}
            other = {style_components(key, colors=colors)["foreground"]
                     for key in ("hb_claude", "hb_codex", "hb_opencode", "tag_steward")}
            if colors == 256:
                # ... nor a status color the box draws inside it (working, idle, dead and blocked, done, the spinner).
                other |= {style_components(key, colors=colors)["foreground"]
                          for key in ("g_work", "g_work_off", "g_idle", "g_dead", "g_blocked", "lvl_g", "lvl_y", "lvl_r", "g_spin",
                                      "loc_repo", *("stg%d_%s" % (i, on) for i in range(5) for on in ("on", "off")))}
            if colors == 256:
                self.assertEqual(len(set(zone.values())), 3, zone)
            self.assertFalse(set(zone.values()) & other, (colors, zone, other))
        self.assertTrue(style_components("hbb_claude")["bold"])
        self.assertIn("zone_direct", __import__("session_shell").PANEL_STYLE_KEYS)


class PeersTest(unittest.TestCase):
    def node(self, script):
        return ZonesTest.node(self, script)

    def test_the_three_windows_read_the_same_order_and_a_window_with_no_relation_has_no_line(self):
        self.node(r"""
const end=(tag,key,me)=>({tag,key,...(me?{me:true}:{})});
const supervising={me:'c7',groups:[{steward:end('c7','hb_claude',true),members:[end('d4','hb_claude'),end('3b','hb_codex')]}]};
const supervised={me:'d4',groups:[{steward:end('c7','hb_claude'),members:[end('d4','hb_claude',true)]}]};
const both={me:'3b',groups:[{steward:end('c7','hb_claude'),members:[end('3b','hb_codex',true)]},{steward:end('3b','hb_codex',true),members:[end('a1','hb_claude'),end('a2','hb_opencode')]}]};
const rowsOf=(peers,w=60)=>draw([],[],w,30,{peers});
const line=(peers,w=60)=>rowsOf(peers,w).map(s=>s.replace(/^│ /,'').replace(/ │$/,'').trimEnd()).find(s=>/^PEERS/.test(s));
assert.equal(line(supervising),'PEERS  [c7] ⚑  ─ [d4] [3b]');
assert.equal(line(supervised),'PEERS  [c7] ⚑  ─ [d4]');
assert.equal(line(both),'PEERS  [c7] ⚑  ─ [3b] · [3b] ⚑  ─ [a1] [a2]');
// The row stands right under GIT, with the labels' column; none for a window with no relation (and no empty row).
const all=rowsOf(supervising).map(s=>s.replace(/^│ /,'').replace(/ │$/,'').trimEnd());
const at=all.findIndex(s=>/^PEERS/.test(s));
assert(/^GIT /.test(all[at-1])&&/^REPO /.test(all[at-2]),all.join('\n'));
assert.equal(all[at].indexOf('[c7]'),all[at-1].indexOf('main'));
const none=rowsOf(undefined);
assert(!none.some(s=>/PEERS/.test(s)));
assert.equal(none.length,rowsOf({me:'x',groups:[]}).length);
const same=draw([],[],60,30,{peers:undefined}),withLine=rowsOf(supervising);
assert.equal(same.length,withLine.length);     // (the panel's height is its window's; the line takes a row of the head's, not one more)
""")

    def test_numbers_wear_their_harness_color_the_own_one_bold_the_flag_pink_and_the_dash_and_dot_dim(self):
        self.node(r"""
const end=(tag,key,me)=>({tag,key,...(me?{me:true}:{})});
const both={me:'3b',groups:[{steward:end('c7','hb_claude'),members:[end('3b','hb_codex',true)]},{steward:end('3b','hb_codex',true),members:[end('a1','hb_claude'),end('a2','hb_opencode')]}]};
const v=render([],[],70,30,{peers:both});
const row=v.rows.find(r=>/^│ PEERS/.test(text(r)));
const num=t=>row.filter(p=>p.text==='['+t+']');
// 11-18 (the person: "나머지는 컬러도 조금 어둡게"): this window's own number is its harness color, bold; every other session's is its
// harness's one step back (`harnessName`, the USAGE names'; the light face's `peerOther`), plain; both resolved by the face as fixed colors.
const dark=(...p)=>view.appearance([p],{theme:'terminal'},{colors:256,background:'dark'})[0][0];
assert.equal(num('c7')[0].role,'harness-name');assert.equal(num('c7')[0].harness,'claude');assert.equal(num('a1')[0].harness,'claude');assert.equal(num('a2')[0].harness,'opencode');
assert.equal(dark(num('c7')[0]).foreground,view.FACES.dark.harnessName.claude);assert.equal(dark(num('a2')[0]).foreground,view.FACES.dark.harnessName.opencode);
assert.equal(num('3b').length,2);assert(num('3b').every(p=>p.foreground===176&&p.bold===true&&p.role==='peer'));       // this window's own number: bold, lit
assert.equal(dark(num('3b')[0]).foreground,176);
assert(num('c7')[0].bold!==true&&num('a1')[0].bold!==true);
// The light face: each harness's `peerOther` (the lighter step); mono keeps no color and tells them apart by weight alone.
const lit=(theme,...p)=>view.appearance([p],{theme},{colors:256,background:theme==='light'?'light':''})[0][0];
assert.equal(lit('light',num('a2')[0]).foreground,view.FACES.light.peerOther.opencode);
assert.equal(lit('light',num('a1')[0]).foreground,view.FACES.light.peerOther.claude);
// 11-18 review: on every face (dark, light, unknown, and each with a shade) this window's number is never the color of another
// window's of the same harness: the light face's other-window value is its own lighter step, not the deep hue (was the same 166 / 31).
// (The harness colors are the product's own, 173 / 147 / 153, not this file's older fixture values: the light face derives this window's
// number from them, so only the real ones show the old bug.)
const REAL={...STYLES,hb_claude:{foreground:173},hb_codex:{foreground:147},hb_opencode:{foreground:153}};
for(const h of ['claude','codex','opencode']){
  const grp={me:'3b',groups:[{steward:end('3b','hb_'+h,true),members:[end('a1','hb_'+h)]}]};
  const r=render([],[],70,30,{peers:grp,styles:REAL}).rows.find(x=>/^│ PEERS/.test(text(x)));
  const mine=r.find(x=>x.text==='[3b]'),other=r.find(x=>x.text==='[a1]');
  for(const [cfg,bg] of [[{theme:'terminal'},'dark'],[{theme:'light'},'light'],[{theme:'terminal'},''],[{theme:'terminal',shade:'medium'},'dark'],[{theme:'light',shade:'light'},'light']]){
    const f=x=>view.appearance([[x]],cfg,{colors:256,background:bg})[0][0];
    assert.notEqual(f(mine).foreground,f(other).foreground,h+' '+JSON.stringify(cfg)+' '+bg);
    assert.equal(f(mine).bold,true);assert.notEqual(f(other).bold,true);
  }
}
assert.equal(lit('mono',num('c7')[0]).foreground,null);assert.equal(lit('mono',num('3b')[0]).bold,true);assert.notEqual(lit('mono',num('c7')[0]).bold,true);
const flags=row.filter(p=>p.text==='⚑');assert.equal(flags.length,2);assert(flags.every(p=>p.foreground===219));
assert(row.filter(p=>p.text==='─'||p.text===' · ').every(p=>p.dim===true));
// Nothing says what a session is doing: no state word, glyph or mark on the row.
assert(!/working|idle|blocked|●|⠋|◑/.test(text(row)));
// The label is dim, as REPO and GIT.
assert(row.find(p=>/^PEERS/.test(p.text)).dim===true);
""")

    def test_short_of_cells_the_last_numbers_go_and_the_count_stands_inside_the_group_that_lost_them(self):
        self.node(r"""
const end=(tag,key,me)=>({tag,key,...(me?{me:true}:{})});
const many=['a1','a2','a3','a4','a5'].map(t=>end(t,'hb_claude'));
const peers={me:'3b',groups:[{steward:end('c7','hb_claude'),members:[end('3b','hb_codex',true)]},{steward:end('3b','hb_codex',true),members:many}]};
const line=(w,p=peers)=>draw([],[],w,30,{peers:p}).map(s=>s.replace(/^│ /,'').replace(/ │$/,'').trimEnd()).find(s=>/^PEERS/.test(s));
assert.equal(line(80),'PEERS  [c7] ⚑  ─ [3b] · [3b] ⚑  ─ [a1] [a2] [a3] [a4] [a5]');
assert.equal(line(56),'PEERS  [c7] ⚑  ─ [3b] · [3b] ⚑  ─ [a1] [a2] [a3] +2');
// The count stands inside the group whose numbers went (never after [3b] of the first group, which would read as that group's).
assert.equal(line(48),'PEERS  [c7] ⚑  ─ [3b] · [3b] ⚑  ─ [a1] +4');
assert.equal(line(42),'PEERS  [c7] ⚑  ─ [3b] · [3b] ⚑  ─ +5');       // the person's third sketch, two cells wider for the second cell after each flag
// Where the second group's head and count do not fit, the group goes whole and no count is put on the group left.
assert.equal(line(36),'PEERS  [c7] ⚑  ─ [3b]');
assert.equal(line(34),'PEERS  [c7] ⚑  ─ [3b]');
// Two supervisors of one window: the second one's head with its count, else it goes whole (never `[c7] ⚑  ─ [3b] +1`).
const two={me:'3b',groups:[{steward:end('c7','hb_claude'),members:[end('3b','hb_codex',true)]},{steward:end('e1','hb_claude'),members:[end('3b','hb_codex',true)]}]};
assert.equal(line(60,two),'PEERS  [c7] ⚑  ─ [3b] · [e1] ⚑  ─ [3b]');
assert.equal(line(36,two),'PEERS  [c7] ⚑  ─ [3b]');
// One group: its own count after its own numbers.
const one={me:'c7',groups:[{steward:end('c7','hb_claude',true),members:['d4','3b','a1','a2'].map(t=>end(t,'hb_claude'))}]};
assert.equal(line(36,one),'PEERS  [c7] ⚑  ─ [d4] [3b] +2');
// No width draws a `+N` that follows the numbers of another group, or a row wider than the room.
const cells=l=>[...l].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
for(const p of [peers,two,one])for(let w=34;w<=90;w++){
 const l=line(w,p);assert(l,w);
 assert(cells(l)<=w-4,w+' '+l);
 assert(!/\] \+\d+ · /.test(l),w+' a count before another group: '+l);
 if(/ \+\d+$/.test(l))assert(/─( \[[0-9a-f]{2}\])* \+\d+$/.test(l)&&!/\] · \[[0-9a-f]{2}\] \+\d+$/.test(l),w+' '+l);
}
// Narrower than the labelled layout the row has no label, as REPO and GIT have none.
const bareRow=draw([],[],30,30,{peers}).map(s=>s.replace(/^│ /,'').replace(/ │$/,'').trimEnd()).find(s=>/^\[c7\] ⚑/.test(s));
assert(bareRow&&!/PEERS/.test(bareRow),bareRow);
""")

    def test_the_bottom_layout_gives_it_one_optional_row(self):
        self.node(r"""
const end=(tag,key,me)=>({tag,key,...(me?{me:true}:{})});
const peers={me:'c7',groups:[{steward:end('c7','hb_claude',true),members:[end('d4','hb_claude'),end('3b','hb_codex')]}]};
const rows=draw([direct],[],110,16,{side:false,peers});
assert(rows.some(s=>/PEERS \[c7\] ⚑  ─ \[d4\] \[3b\]/.test(s)),rows.join('\n'));
const none=draw([direct],[],110,16,{side:false});
assert(!none.some(s=>/PEERS/.test(s)));
""")


class FeedTest(unittest.TestCase):
    """What the panel reads: the cache's `session_peers` becomes the PEERS groups; the zone comes from the route's shape."""

    def test_peer_groups_put_the_supervisor_first_then_the_window_with_the_ones_it_supervises(self):
        import session_shell
        entry = {"tag": "3b", "supervisors": [["c7", "claude"]], "targets": [["a1", "claude"], ["a2", "opencode"]]}
        got = session_shell.peer_groups(entry, "[3b]", "codex")
        self.assertEqual(got["me"], "3b")
        self.assertEqual(got["groups"], [
            {"steward": {"tag": "c7", "key": "hb_claude"}, "members": [{"tag": "3b", "key": "hb_codex", "me": True}]},
            {"steward": {"tag": "3b", "key": "hb_codex", "me": True},
             "members": [{"tag": "a1", "key": "hb_claude"}, {"tag": "a2", "key": "hb_opencode"}]}])
        self.assertIsNone(session_shell.peer_groups(None, "[3b]", "codex"))
        self.assertIsNone(session_shell.peer_groups({"tag": "3b", "supervisors": [], "targets": []}, "[3b]", "codex"))
        # A harness the panel does not know takes the neutral key.
        odd = session_shell.peer_groups({"tag": "x", "supervisors": [["c7", "other"]], "targets": []}, "[x]", "claude")
        self.assertEqual(odd["groups"][0]["steward"]["key"], "hb_other")

    def test_the_cache_keeps_only_well_formed_bounded_peer_entries(self):
        import human_wait_state
        good = {"claude\0s1": {"tag": "c7", "supervisors": [], "targets": [["d4", "claude"]]}}
        self.assertEqual(human_wait_state._valid_peers(good), good)
        for bad in ({"no-nul": good["claude\0s1"]},
                    {"claude\0s1": {"tag": "c7", "supervisors": [["d4"]], "targets": []}},
                    {"claude\0s1": {"tag": "c7", "supervisors": [], "targets": [["d4", "x" * 99]]}},
                    {"claude\0s1": {"tag": "c7", "supervisors": [], "targets": [["d4", "claude"]] * 17}},
                    {"claude\0s1": {"tag": 7, "supervisors": [], "targets": []}}, [], None):
            self.assertEqual(human_wait_state._valid_peers(bad), {})
        self.assertEqual(human_wait_state._valid_peers({"claude\0s%d" % i: good["claude\0s1"] for i in range(65)}), {})
        # The producer cuts a longer map to the cache's bound (the first keys stay), so the panels keep their lines.
        many = {"claude\0s%d" % i: good["claude\0s1"] for i in range(75)}
        cut = human_wait_state._bounded(many)
        self.assertEqual(len(cut), 64)
        self.assertEqual(list(cut), list(many)[:64])
        self.assertEqual(human_wait_state._valid_peers(cut), cut)
        self.assertEqual(human_wait_state._bounded(None), {})

    def test_a_route_of_the_direct_shape_is_the_direct_zone_and_every_other_is_dispatch(self):
        from fleet import session_panel
        view = {"route_panels": {"claude\0sid": [
            {"id": "r1", "title": "t", "state": "working", "shape": "direct"},
            {"id": "r2", "title": "t", "state": "working", "shape": "staged"},
            {"id": "r3", "title": "t", "state": "working", "shape": "one-shot-owner"}]}}
        panel = session_panel.presentation({}, view, "claude", "sid", 80)
        self.assertEqual([route["zone"] for route in panel["routes"]], ["direct", "dispatch", "dispatch"])


if __name__ == "__main__":
    unittest.main()
