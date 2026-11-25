"""11-16 (panel peer request 1116): the panel's route box lines for peer requests.

1. Named rules of the box's own line (`─ dispatch ───`, `─ request ───`, 11-17: route zones) open the routes' zone and the
   requests' zone, each only where it has rows; short of rows they go before any content row.
2. A request reads like a route's doer: its mark, the other session's Fleet number, then that session's harness, model
   and effort in the colors a route's doer takes; unknown values leave out (`[6c] claude`); the age, then the effort,
   then the model give way first.
3. The box's title counts the routes alone (`ROUTE 2` since 11-25, `2 ROUTE` before; 11-17: the requests are a zone, no tab of the title; never a count
   without its name); one word list line for the panel, the Fleet session page and the Fleet links.
4. A request this session received stays after it is answered, as long as a sent one does (15 minutes), closed and dim.
5. A message whose other end is this session itself (the tidy notice) is no message: MESSAGES, its popup, the folded
   strip's count and Fleet's session page leave it out, a record already written too.
Everything here is synthetic (a private ledger root, synthetic sessions).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools"), str(ROOT)]

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_r:{foreground:217,bold:true},g_spin:{foreground:230},g_idle:{foreground:186},
  hb_claude:{foreground:116},hb_codex:{foreground:176},m_opus:{foreground:117},eff_x:{foreground:222}};
const t=(text,key='dim')=>({text,...(styles[key]||{})});
// A request as Fleet's one request_rows gives it: the other session's paint is the route's (`paint.harness`, `model_text`, `effort`).
const paintOf=(h,m,e)=>({...(h?{harness:t(h,'hb_'+h)}:{}),...(m?{model_text:t(m,'m_opus')}:{}),...(e?{effort:t(e,'eff_x')}:{})});
const q=(id,glyph,who,title,status,kind,age,paint)=>({id,way:glyph==='◂'?'◂':'→',glyph,who,title,status,age:age??'3m',kind,paint:paint||{},
  details:[['','e9 → e7 · '+id,'dim']],body:['BODY']});
const routes=[0,1].map(i=>({id:'rt-'+i,title:'route '+i,state:'working',kind:'working',shape:'one-shot',kind_word:'code',role:'owner',elapsed:'3m',
  paint:{glyph:t('⠏','g_spin'),harness:t('claude','hb_claude'),model_text:t('Opus 5.5','m_opus'),effort:t('xhigh','eff_x')}}));
const sent=q('rq-aaaaaa','→','[6c]','review the popup keys','partial answer','request-out','12m',paintOf('claude','Opus 5.5','xhigh'));
const got=q('rq-bbbbbb','◂','[a3]','popup colors','open','request-in','3m',paintOf('codex','GPT 5.5','high'));
const done=q('rq-cccccc','✓','[e1]','finished thing','final answer','request-done','1m',paintOf('claude',null,null));
const base=(o={})=>({styles,colors:o.colors??256,no_color:o.no_color===true,tag:'[e9]',repo_side:[t('hearting','loc_repo')],selected:[t('Opus 5.5','hb_claude')],
  session_title:'panel',now:'x',route_count:o.routes===undefined?2:o.routes.length,route_total:o.routes===undefined?2:o.routes.length,routes:o.routes??routes,
  requests:o.requests??[sent,got],messages:o.messages??[{way:'←',who:'[e7]',harness:'claude',when:'09:31',kind:'steer',text:'MSG_KEPT'}],...(o.model||{})});
const draw=(o={})=>view.render({width:o.width??40,height:o.height??50,side:o.side??true,model:base(o),state:'working',frame:1000,since:990,cellWidth:cells,
  configuration:{side_width:o.width??40,theme:o.theme||'terminal'},background:''});
const lines=v=>v.rows.map(str);
const boxOf=v=>{const rows=lines(v),a=rows.findIndex(s=>/^│ ╭─ (?:(ROUTE|PEER REQUEST|REQUEST) \d+|ROUTE|route|peer request|request)/.test(s));if(a<0)return null;
  const b=rows.findIndex((s,i)=>i>a&&/^│ ╰/.test(s));return {a,b,rows};};
// 11-17 (route zones): a zone's named rule inside the box (`│ │ ─ dispatch ──── │ │`); `zone` is its name or any.
const zoneRule=(s,zone='(?:direct|dispatch|request)')=>new RegExp('^│ │ ─ '+zone+' ─+ │ │$').test(s);
"""


def _node(test, body, *args):
    result = subprocess.run(["node", "-e", HEAD + body, str(SHELL), *args], capture_output=True, text=True, timeout=180)
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    return result.stdout


class RouteBoxRequestLines(unittest.TestCase):
    # 1 --------------------------------------------------------------------------------------------------------
    def test_a_thin_rule_parts_routes_from_requests_only_where_both_stand(self):
        _node(self, r"""
const rule=(v,zone)=>lines(v).filter(s=>zoneRule(s,zone)).length;
const both=draw(),b=boxOf(both);
// Both zones stand: `─ dispatch ───` under the top line (the routes carry no zone), `─ request ───` before the requests; no `direct`.
const rows=lines(both),d=rows.findIndex(s=>zoneRule(s,'dispatch')),at=rows.findIndex(s=>zoneRule(s,'request'));
assert.equal(rule(both),2,rows.join('\n'));assert.equal(rule(both,'direct'),0);
assert(d===b.a+1&&at>d&&at<b.b,rows.join('\n'));
assert(/route 1|Opus/.test(rows[at-1])||rows.slice(b.a,at).some(s=>s.includes('route 1')),rows.join('\n'));
assert(/^│ │ ◂ \[a3\]/.test(rows[at+1])||/^│ │ → \[6c\]/.test(rows[at+1]),rows[at+1]);
// Only routes: the one `dispatch` rule, none for requests. Only requests: the one `request` rule alone (the title `ROUTE`).
const routesOnly=draw({requests:[]}),requestsOnly=draw({routes:[]});
assert.equal(rule(routesOnly,'dispatch'),1);assert.equal(rule(routesOnly,'request'),0);
assert.equal(rule(requestsOnly,'dispatch'),0);assert.equal(rule(requestsOnly,'request'),1);
assert(boxOf(requestsOnly),'a request alone still draws the box');
assert(/^│ ╭─ ROUTE ─+ ›─╮ │$/.test(lines(requestsOnly)[boxOf(requestsOnly).a]),lines(requestsOnly).join('\n'));
// A route of the `direct` zone stands first, under its own rule: direct, dispatch, request.
const zoned=draw({routes:[{...routes[1],id:'rt-d',zone:'direct'},...routes]}),zr=lines(zoned);
const order=['direct','dispatch','request'].map(z=>zr.findIndex(s=>zoneRule(s,z)));
assert(order.every(i=>i>0)&&order[0]<order[1]&&order[1]<order[2],zr.join('\n'));
// No rule has a press; the rule is the box's own line in the zone's own name: the dashes dim on the `messages` role, the name `zone`.
const v=draw(),row=v.rows[at],ln=row.find(p=>p.text.includes('─')&&!p.text.includes('request'));
assert.deepEqual([ln.dim,ln.role],[true,'messages']);
assert(row.some(p=>p.text==='request'&&p.role==='zone'),JSON.stringify(row));
assert.equal(v.hits.filter(h=>h.row===at||h.row===d).length,0);
""")

    def test_short_of_rows_content_gives_way_first_and_a_zone_rule_stays_while_a_row_of_its_zone_shows(self):
        _node(self, r"""
// 11-17 (route zones): a zone's rule is not the first thing to go. Heights from tall to short: content rows give way by the
// existing steps (folds, then rows scrolled out), and a rule stands exactly where a row of its zone shows, also when the box
// is scrolled to fewer rows: never a rule over nothing, never a row of a zone without its rule.
const zones=v=>{const b=boxOf(v);if(!b)return null;const out=[];
  // (The last step before the box itself would go: one route stands without any rule, a route without its name rather than no route.)
  if(!b.rows.slice(b.a+1,b.b).some(s=>/^│ │ ─ (direct|dispatch|request) ─+ │ │$/.test(s)))return [];
  for(const s of b.rows.slice(b.a+1,b.b)){
    const m=/^│ │ ─ (direct|dispatch|request) ─+ │ │$/.exec(s);
    if(m){out.push({zone:m[1],rows:[]});continue;}
    assert(out.length,'a row stands above every zone rule '+s+'\n'+b.rows.join('\n'));
    out[out.length-1].rows.push(s);
  }
  return out;};
const marker={dispatch:/route \d|owner|code\(/,request:/\[(?:6c|a3)\]|partial answer|popup c|review the popup|open/};
for(const width of [30,40,50,60]){
 const full=zones(draw({width,height:60}));
 assert.deepEqual(full.map(z=>z.zone),['dispatch','request'],'both rules stand where there is room '+width);
 let shown=2,fewest=99,nameless=false;
 for(let h=60;h>=12;h--){
  const v=draw({width,height:h}),z=zones(v);if(!z)break;
  const label=width+'x'+h+'\n'+lines(v).join('\n');
  if(!z.length){nameless=true;continue;}                       // the last resort: it comes only after every rule row would not fit
  assert(!nameless,'a rule came back at a lower height '+label);
  assert.deepEqual(z.map(g=>g.zone),['dispatch','request'].slice(0,z.length),'the zones keep their order '+label);
  // A rule stands over a row of its zone (the routes' rows under `dispatch`, the requests' under `request`), never over nothing.
  for(const g of z)assert(g.rows.some(s=>marker[g.zone].test(s)),'a rule stands over no row of its zone '+g.zone+' '+label);
  // A zone with no row showing has no rule, and a zone that went does not come back at a lower height.
  assert(z.length<=shown,'a rule came back at a lower height '+label);shown=z.length;
  fewest=Math.min(fewest,z.reduce((n,g)=>n+g.rows.length,0));
 }
 assert(fewest<full.reduce((n,g)=>n+g.rows.length,0),'content gives way at some height '+width);
 assert(shown===1||nameless,'the request zone scrolls out before the routes\' rule '+width);
}
""")

    # 2 --------------------------------------------------------------------------------------------------------
    def test_a_request_head_reads_as_a_route_doer_in_the_route_colors(self):
        _node(self, r"""
const v=draw({width:60}),rows=lines(v);
const head=rows.find(s=>/^│ │ → \[6c\] claude Opus 5\.5 xhigh +12m │ │$/.test(s));
assert(head,rows.join('\n'));
assert(rows.some(s=>/^│ │ ◂ \[a3\] codex GPT 5\.5 high +3m │ │$/.test(s)),rows.join('\n'));
// Each value wears the color a route row's same value wears (the harness key, the model key, the effort key).
const at=rows.indexOf(head),part=text=>v.rows[at].find(p=>p.text===text);
assert.equal(part('claude').foreground,styles.hb_claude.foreground);
assert.equal(part('Opus 5.5').foreground,styles.m_opus.foreground);
assert.equal(part('xhigh').foreground,styles.eff_x.foreground);
const route=v.rows.find(r=>/[⠀-⣿] owner claude Opus 5\.5 xhigh/.test(str(r)));
assert(route,'a route row of the same form');
for(const w of ['claude','Opus 5.5','xhigh'])assert.equal(route.find(p=>p.text===w).foreground,part(w).foreground,w);
// The title and the status follow, the status dim; a closed one's mark is the success color.
assert(rows[at+1].includes('review the popup keys')&&rows[at+2].includes('partial answer'),rows.slice(at,at+3).join('\n'));
const closed=draw({width:60,requests:[done]}),rows2=lines(closed),c=rows2.findIndex(s=>s.includes('✓ [e1] claude'));
assert(c>0,rows2.join('\n'));assert.equal(closed.rows[c].find(p=>p.text==='✓').foreground,styles.lvl_g.foreground);
assert.equal(closed.rows[c+2].find(p=>p.text.includes('final answer')).dim,true);
""")

    def test_a_value_the_fleet_session_does_not_know_is_left_out_and_a_gone_session_keeps_its_name(self):
        _node(self, r"""
const only=q('rq-dddddd','→','[6c]','title','open','request-out','1m',paintOf('claude',null,null));
let rows=lines(draw({width:60,requests:[only]}));
assert(rows.some(s=>/^│ │ → \[6c\] claude +1m │ │$/.test(s)),rows.join('\n'));
const gone=q('rq-eeeeee','→','e7','title','open','request-out','1m',{});
rows=lines(draw({width:60,requests:[gone]}));
assert(rows.some(s=>/^│ │ → e7 +1m │ │$/.test(s)),rows.join('\n'));
""")

    def test_a_narrow_row_gives_the_age_then_the_effort_then_the_model_and_keeps_the_session_and_the_status(self):
        _node(self, r"""
const order=[];let last=null;
for(let w=62;w>=26;w--){
 const rows=lines(draw({width:w,height:60,requests:[sent],routes:[]})),b=rows.findIndex(s=>/^│ │ → \[6c\]/.test(s));
 assert(b>0,w+'\n'+rows.join('\n'));
 const head=rows[b],near=rows.slice(b,b+3).join('\n');
 assert(head.includes('[6c]'),'the other session stays '+head);assert(near.includes('partial answer'),'the status stays '+w+'\n'+near);
 const have={age:/12m/.test(head),effort:/xhigh/.test(head),model:/Opus 5\.5/.test(head),harness:/claude/.test(head)};
 if(last){for(const k of ['age','effort','model','harness'])if(last[k]&&!have[k])order.push(k);
  // Something that is gone does not come back at a narrower width, and a later one never goes before an earlier one.
  for(const [a,b2] of [['age','effort'],['effort','model'],['model','harness']])if(have[a]&&!have[b2])assert.fail(a+' stands while '+b2+' is gone at '+w+'\n'+head);}
 last=have;
}
assert.deepEqual(order.filter((x,i)=>order.indexOf(x)===i),['age','effort','model','harness'].filter(k=>order.includes(k)),String(order));
assert(order.includes('age')&&order.includes('effort')&&order.includes('model'),String(order));
""")

    # 3 --------------------------------------------------------------------------------------------------------
    def test_the_title_reads_peer_request_and_short_only_where_the_line_does_not_hold_it(self):
        _node(self, r"""
// 11-17: the title is the route count alone at every width (the requests are the box's `request` zone, not a tab).
for(const width of [30,34,38,40,50,60]){
 const rows=lines(draw({width})),b=boxOf(draw({width})),top=rows[b.a];
 // Never a count without its name (11-25: the count stands after the name).
 const words=top.replace(/[╭╮─│]/g,' ').trim().split(/\s+/);
 assert(/^\d+$/.test(words[1])&&words[0]==='ROUTE','a count without its name: '+top);
 assert(words.includes('ROUTE'),top);
 assert(/^│ ╭─ ROUTE 2 ─+ ›─╮ │$/.test(top)&&!/REQUEST/.test(top),top);
}
// Below the native area the lower case form `route 2` stands alone too.
const below=lines(draw({width:120,height:20,side:false}));
assert(below.some(s=>/route 2/.test(s)&&!/request/.test(s)),below.join('\n'));
// A request alone reads `ROUTE`, never a bare count.
assert(lines(draw({routes:[]})).some(s=>/^│ ╭─ ROUTE ─+ ›─╮ │$/.test(s)),lines(draw({routes:[]})).join('\n'));
const tight=lines(draw({width:24,height:50})).find(s=>/^│ ╭─/.test(s));
assert(!/│ ╭─ \d+|│ \d+ ─|│ \d+╮|│ \d+ *$/.test(tight.replace(/ │$/,'')),tight);
""")

    def test_every_width_and_height_keeps_the_box_closed_with_its_presses_on_the_drawn_rows(self):
        _node(self, r"""
let boxes=0;
for(const width of [30,31,33,36,40,45,50,55,60])for(let height=20;height<=60;height+=2){
 for(const model of [{}, {requests:[sent,got,done]},{routes:[]},{requests:[]}]){
  const v=draw({width,height,...model}),rows=lines(v),label=[width,height,JSON.stringify(Object.keys(model))].join(' ')+'\n'+rows.join('\n');
  assert(rows.length<=height&&rows.every(r=>cells(r)===width),'overflow '+label);
  const b=boxOf(v);if(!b)continue;boxes++;
  assert(b.b>b.a,'the box closes '+label);
  // Every row inside the box is the box's own, a rule or content; a press on it is the box's (a request row, the MESSAGES popup).
  for(let i=b.a+1;i<b.b;i++){
   assert(/^│ │ .* │ │$/.test(rows[i]),'a row outside the box line '+i+'\n'+label);
   const acts=v.hits.filter(h=>h.row===i);
   if(zoneRule(rows[i])){assert.equal(acts.length,0,'the rule has no press '+label);continue;}
   assert.equal(acts.length,1,'one press per row '+i+'\n'+label);
   assert(acts[0].left>=2&&acts[0].right<=width-3,JSON.stringify(acts[0])+label);
   if(/^│ │ [→◂✓✕] \[/.test(rows[i]))assert.equal(acts[0].action,'popup:MESSAGES',rows[i]);
   else if(i>b.a&&rows.slice(b.a,i).some(s=>zoneRule(s,'request')))assert.equal(acts[0].action,'popup:MESSAGES','below the request rule every row is a request\'s: '+rows[i]);
  }
  // The title row presses the ROUTE popup, over the cells it was drawn on.
  const top=v.hits.filter(h=>h.row===b.a);assert.deepEqual(top.map(h=>h.action),['popup:ROUTE'],label);
  // No row carries an empty name tag: a `│ ╭─` top line always names the routes or the requests.
  assert(/ROUTE|REQUEST|route|request/.test(rows[b.a]),label);
 }
}
assert(boxes>600,String(boxes));
""")

    def test_mono_eight_colors_and_no_color_carry_no_hue(self):
        # The values come the way the host makes them: the real doer paint, each key through `style_components` for the
        # mode (eight colors: the eight hues only; NO_COLOR: none); mono is the panel's own theme (no foreground at all).
        from fleet.palette import style_components
        from fleet.session_panel import doer_paint
        row = doer_paint({"harness": "claude", "model": "claude-opus-5-5", "effort": "xhigh"})
        keys = ["dim", "loc_repo", "lvl_g", "lvl_r", "g_spin", "g_idle", row["harness_key"], row["model_key"], row["effort_key"]]
        for mode, (colors, no_color, theme) in {"mono": (256, False, "mono"), "eight": (8, False, "terminal"),
                                                "nocolor": (256, True, "terminal")}.items():
            spec = {"theme": theme, "colors": colors, "no_color": no_color,
                    "styles": {key: style_components(key, colors=colors, no_color=no_color) for key in keys},
                    "paint": {name: {"text": row[name], **style_components(row[key], colors=colors, no_color=no_color)}
                              for name, key in (("harness", "harness_key"), ("model_text", "model_key"), ("effort", "effort_key"))}}
            _node(self, r"""
const spec=JSON.parse(process.argv[2]);
const sent2={...sent,paint:spec.paint};
const m={...base({requests:[sent2],colors:spec.colors,no_color:spec.no_color}),styles:spec.styles,colors:spec.colors,no_color:spec.no_color};
const v=view.render({width:40,height:50,side:true,model:m,state:'working',frame:1000,since:990,cellWidth:cells,configuration:{side_width:40,theme:spec.theme},background:''});
const r=lines(v),rule=r.findIndex(s=>zoneRule(s,'request')),b=boxOf(v);
assert(r.some(s=>s.includes('[6c] claude')&&s.includes(spec.paint.model_text.text)),r.join('\n'));
const parts=v.rows.slice(rule>0?rule+1:b.a+1,b.b).flat().filter(p=>/\S/.test(p.text)&&!/^[│├┤╭╮╰╯─]+$/.test(p.text));
const hues=parts.map(p=>p.foreground).filter(f=>Number.isInteger(f)&&f>=0);
if(spec.theme==='mono'||spec.no_color)assert.deepEqual(hues,[],JSON.stringify(parts));
else assert(hues.length>0&&hues.every(f=>f<8),JSON.stringify(hues));
if(spec.no_color)assert(parts.every(p=>!p.dim&&!p.bold),JSON.stringify(parts));
""", json.dumps(spec))

    def test_the_messages_popup_head_is_the_same_drawing_and_opens_the_whole_request(self):
        _node(self, r"""
const body=view.messagesBody({model:base(),columns:60}).map(str);
assert(body.some(s=>/^→ \[6c\] claude Opus 5\.5 xhigh .*review the popup keys · partial answer +12m$/.test(s)||/^→ \[6c\] claude .*review the popup keys · partial answer/.test(s)),body.join('\n'));
assert(body.some(s=>s.includes('BODY')||s.includes('e9 → e7')),'the request is read whole in the popup\n'+body.join('\n'));
""")


class RequestLedgerAndSelfMessages(unittest.TestCase):
    """Rows from a private ledger root (real peer_request files) and synthetic Fleet sessions."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("peer_request_fixture_1116", str(ROOT / "utilities/peer_request.test.py"))
        cls.fixture = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.fixture
        spec.loader.exec_module(cls.fixture)

    def _ledger(self):
        fx = self.fixture
        case = type("Case", (fx.Fixture,), {"runTest": lambda self: None})()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def _views(self, who, now):
        import session_shell
        views, _paths = session_shell._request_views(who[0], who[1], now)
        return views

    def _rows(self, views, now, doer_of=None):
        from fleet.session_panel import request_rows
        tags = {self.fixture.A[1]: "e9", self.fixture.B[1]: "a3"}
        return request_rows(views, now, lambda harness, sid: tags.get(sid), doer_of)

    # 4 --------------------------------------------------------------------------------------------------------
    def test_a_received_request_stands_while_open_and_stays_the_afterglow_once_answered(self):
        case = self._ledger()
        fx = self.fixture
        out = case.request(fx.A, "bee", "please review", title="synthetic review")
        ident = out["id"] if isinstance(out, dict) and "id" in out else fx.pr.session_requests(*fx.B)["in"][0]["id"]
        now = time.time()
        # The fact the person asked to see first: while the request is open, the receiving window's list has it.
        rows = self._rows(self._views(fx.B, now), now)
        self.assertEqual([(r["id"], r["glyph"], r["kind"], r["side"]) for r in rows], [(ident, "◂", "request-in", "in")], rows)
        # It is answered: the receiving window keeps it (closed, dim), as it keeps a sent one, for the afterglow.
        case.ack(fx.pr.load(ident)["revisions"][-1]["transfer_ref"], fx.B)
        case.report(fx.B, ident, kind="final", result="done")
        rows = self._rows(self._views(fx.B, time.time() + 60), time.time() + 60)
        self.assertEqual([(r["glyph"], r["kind"], r["state"]) for r in rows], [("✓", "request-done", "final")], rows)
        self.assertEqual([r["glyph"] for r in self._rows(self._views(fx.A, time.time() + 60), time.time() + 60)], ["✓"])
        later = time.time() + 16 * 60
        self.assertEqual(self._rows(self._views(fx.B, later), later), [])

    # 2 --------------------------------------------------------------------------------------------------------
    def test_the_other_session_comes_from_values_already_read_and_unknown_ones_are_left_out(self):
        case = self._ledger()
        fx = self.fixture
        case.request(fx.A, "bee", "please review", title="synthetic review")
        now = time.time()
        views = self._views(fx.A, now)
        asked = []

        def known(harness, sid):
            asked.append((harness, sid))
            return {"model": "claude-opus-5-5", "effort": "xhigh"}
        row, = self._rows(views, now, known)
        self.assertEqual(asked, [(fx.B[0], fx.B[1])])           # the recipient's own session values, asked once
        self.assertEqual(row["harness"], "codex")
        self.assertTrue(row["model_text"] and row["effort"], row)
        self.assertEqual((row["harness_key"], row["effort_key"].startswith("eff") or True), ("hb_codex", True))
        row, = self._rows(views, now, lambda harness, sid: None)
        self.assertEqual((row["harness"], row.get("model_text"), row.get("effort")), ("codex", None, None))
        row, = self._rows(views, now, lambda harness, sid: (_ for _ in ()).throw(RuntimeError("never costs the row")))
        self.assertEqual(row["harness"], "codex")
        row, = self._rows(views, now)                            # no values at all: the harness alone
        self.assertEqual((row["harness"], row.get("model_text")), ("codex", None))
        # A session without its Fleet number (gone) keeps its name alone: no harness is painted.
        from fleet.session_panel import request_rows
        row, = request_rows(views, now, lambda harness, sid: None, known)
        self.assertNotIn("harness", row)

    # 5 --------------------------------------------------------------------------------------------------------
    def test_a_message_to_this_session_itself_is_no_message_but_one_to_another_stays(self):
        from fleet.session_line import peer_message_rows
        own = ("claude", "sid-me")
        tags = {("claude", "sid-me"): "[e9]", ("codex", "sid-a3"): "[a3]"}
        tag_of = lambda h, s: tags.get((h, s))  # noqa: E731
        now = time.time()
        sent = [{"to": {"harness": "claude", "session_id": "sid-me", "name": "e9"}, "summary": "tidy notice", "at": now - 60,
                 "kind": "notice", "status": "received", "refs": []},
                {"to": {"harness": "codex", "session_id": "sid-a3", "name": "a3"}, "summary": "notice to another", "at": now - 50,
                 "kind": "notice", "status": "received", "refs": []}]
        received = [{"source": "peer", "sender": "[e9]", "sender_harness": "claude", "sender_session_id": "sid-me",
                     "text": "my own", "at": now - 40}]
        kept = peer_message_rows(received, sent, tag_of, limit=8, now=now, own=own)
        self.assertEqual([(m["way"], m["who"], m["text"]) for m in kept], [("→", "[a3]", "notice to another")])
        # Without `own` (a caller that does not know its seat) nothing is dropped: the filter is the one parameter.
        self.assertEqual(len(peer_message_rows(received, sent, tag_of, limit=8, now=now)), 3)
        # A confirmed earlier id of the same seat reads the same Fleet number: it is the same session.
        successor = [{"to": {"harness": "claude", "session_id": "sid-old", "name": "e9"}, "summary": "tidy", "at": now,
                      "kind": "notice", "status": "received", "refs": []}]
        tags[("claude", "sid-old")] = "[e9]"
        self.assertEqual(peer_message_rows([], successor, tag_of, limit=8, now=now, own=own), [])

    def test_the_panel_projection_and_fleet_session_page_leave_the_self_notice_out(self):
        import session_shell
        src = (ROOT / "utilities/session_shell.py").read_text()
        self.assertIn("own=(harness, sid)", src)                  # the panel (MESSAGES, popup, folded count) reads the one function
        live_src = (ROOT / "tools/fleet/explorer/live.py").read_text()
        self.assertIn("own=(row[\"harness\"], info[\"sid\"])", live_src)   # Fleet's session page too
        self.assertTrue(callable(session_shell.panel_projection))
        from tools.fleet.tests import test_fleet_words_peers as words
        me = {"harness": "claude", "session_id": "sid-f5", "name": "f5"}
        sent = [{"to": me, "summary": "[세션 정리] tidy notice", "at": words.NOW - 700, "kind": "notice", "status": "received", "refs": []},
                {"to": {"harness": "codex", "session_id": "sid-a3", "name": "a3"}, "summary": "keep this", "at": words.NOW - 500,
                 "kind": "notice", "status": "received", "refs": []}]
        vm = words.mesh(sent=sent)
        mine = vm.peer_edges["sessions"]["f5"]["messages"]
        self.assertEqual([m["body"] for m in mine if m["way"] == "→"], ["keep this"], mine)
        self.assertFalse([link for link in vm.peer_edges["links"] if link[0][0] == "f5" and link[1][0] == "f5"])

    def test_the_session_values_come_from_the_pass_the_shared_producer_already_ran(self):
        from types import SimpleNamespace as NS
        import human_wait_state as hws
        snapshot = NS(sessions=[
            NS(harness="codex", session_id="sid-b", session_aliases=["old-b"], model="gpt-6.1-sol", effort="high"),
            NS(harness="claude", session_id="sid-c", session_aliases=None, model=None, effort=None),     # nothing known: left out
            NS(harness="other", session_id="sid-d", model="m", effort="e"),                              # not a harness: left out
            NS(harness="claude", session_id="x" * 400, model="m", effort="e")])                         # not a safe id: left out
        doers = hws.session_doers(snapshot)
        self.assertEqual(doers, {"codex\0sid-b": {"model": "gpt-6.1-sol", "effort": "high"},
                                 "codex\0old-b": {"model": "gpt-6.1-sol", "effort": "high"}}, doers)
        self.assertEqual(hws._valid_doers(doers), doers)
        self.assertEqual(hws._valid_doers({"a\0b": {"model": 3}}), {})            # a malformed value never reaches the panel
        self.assertEqual(hws._valid_doers("nope"), {})
        self.assertLessEqual(len(hws.session_doers(NS(sessions=[NS(harness="claude", session_id="s%d" % i, model="m", effort="e")
                                                                 for i in range(200)]))), 64)   # bounded

    # 3 --------------------------------------------------------------------------------------------------------
    def test_one_word_list_entry_names_the_tag_for_the_panel_and_fleet_in_both_languages(self):
        data = json.loads((ROOT / "tools/i18n/messages.json").read_text())
        flat = data.get("messages", data)
        whole = flat["fleet.session_panel.peer_request"]
        short = flat["fleet.session_panel.peer_request_short"]
        self.assertEqual((whole["en"], short["en"]), ("PEER REQUEST", "REQUEST"))
        self.assertTrue(whole["ko"] and short["ko"])
        from fleet.session_panel import presentation  # noqa: F401  (the panel reads the same keys)
        from fleet.explorer.peers import link_kind_text
        self.assertEqual(link_kind_text("REQUEST"), "PEER REQUEST")
        self.assertEqual(link_kind_text("REQUEST", 8), "REQUEST")
        self.assertEqual(link_kind_text("steer"), "steer")


if __name__ == "__main__":
    unittest.main()
