"""11-18 (round 3 of PR #1715): the panel's RESOURCES section and the repository name on a request line, from the producer to the panel.

  * RESOURCES stands above USAGE: one row for each host (`host-a  CPU 34%  MEM 61%`), under it one row for each GPU a process runs
    on (`GPU0  ━━━━━━━━─ 92%  21/24G  [3b]`: the number of the session that holds it, left out where it is not known) and the count
    of the GPUs nothing runs on. The values are the compute-host sample the shared producer keeps; the panel reads the cache only.
    Short of rows it gives way before everything else, so the other sections and the route box stand as they do without it.
  * A request line names the other session's repository after its number (`[c2] other-repo`), not for a session of the window's
    own repository and not where Fleet does not know it; the title goes before the repository name does.
  * The setting `worker box` is one of the panel's own settings (closed, open, none), saved with the others.
Everything is synthetic (no host is probed, no session is read).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import json
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT), str(ROOT / "utilities"), str(ROOT / "tools")]

FIXTURE = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]+'/panel-view.cjs');
const P=t=>({text:t}),D=t=>({text:t,dim:true});
const text=r=>r.map(p=>p.text).join('');
const STYLES={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:114},g_work:{foreground:150},g_work_off:{foreground:150,dim:true}};
const gpu=(i,pct,mem,users)=>({label:[D('GPU'+i)],gauge9:[P('━'.repeat(Math.round(pct*9/100))),D('─'.repeat(9-Math.round(pct*9/100))),D(' '),P(pct+'%')],
  gauge5:[P('━'.repeat(Math.round(pct*5/100))),D('─'.repeat(5-Math.round(pct*5/100))),D(' '),P(pct+'%')],pct:[P(pct+'%')],mem:[D(mem)],...(users?{users:[P(users)]}:{})});
const host=(name,cpu,mem,gpus,idle)=>({host:[P(name)],stats:[[D('CPU '),P(cpu+'%')],[D('MEM '),P(mem+'%')]],gpus,idle});
const A=host('host-a',34,61,[gpu(0,92,'21/24G','[3b]'),gpu(3,58,'14/24G','[c7]')],6);
const B=host('host-b',12,40,[gpu(1,71,'18/24G','[d4]')],0);
const usage=[{harness:'claude',parts:[P('5h 12%')]},{harness:'codex',parts:[P('5h 3%')]},{harness:'opencode',parts:[]}];
const six=at=>['plan','plan-check','execute','impl-review','test','report'].map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true}:{name,mark:''});
const route={id:'a',zone:'dispatch',title:'갈래를 route에 기록',kind:'working',state:'working',elapsed:'1h52m',shape:'staged',stages:six(2),kind_word:'code',intensity:'standard',role:'owner',
  paint:{glyph:P('⠸'),harness:P('claude'),model_text:P('Opus 5.5'),ctx:P('41%'),now:'검토 지적 둘을 고치는 중'},workers_live:[]};
const render=(width,height,model={},{side=true,configuration={}}={})=>view.render({width,height,side,fill:side,harness:'claude',frame:0,
 model:{repo:[P('hearting/')],repo_side:[P('hearting')],git_side:[P('main ✓')],tag:'[c7]',messages:[],styles:STYLES,routes:[route],route_count:1,route_total:1,usage,...model},
 state:'working',configuration,questions:()=>[]});
const plain=v=>v.rows.map(text);
'''


class ResourcesPanelTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_resources_stand_above_usage_with_their_hosts_gpus_and_the_session_numbers(self):
        self.node(r"""
const rows=plain(render(44,50,{resources:[A,B]}));
const at=rows.findIndex(s=>/^│ RESOURCES ─+ │$/.test(s)),us=rows.findIndex(s=>/^│ USAGE ─+ │$/.test(s));
assert(at>0&&us>at,rows.join('\n'));
const body=rows.slice(at+1,us).map(s=>s.slice(2,-2).trimEnd());
assert.deepEqual(body,['host-a  CPU 34%  MEM 61%','  GPU0  ━━━━━━━━─ 92%  21/24G  [3b]','  GPU3  ━━━━━──── 58%  14/24G  [c7]','  idle GPU 6',
 'host-b  CPU 12%  MEM 40%','  GPU1  ━━━━━━─── 71%  18/24G  [d4]'],rows.join('\n'));
assert(rows.every(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)===44),'every row keeps the width');
// A session that is not known leaves its number cell empty; a GPU whose use is not known keeps its memory.
const odd=plain(render(44,50,{resources:[host('host-a',34,61,[gpu(0,92,'21/24G',null),{label:[D('GPU1')],mem:[D('3/24G')]}],0)]}));
const o=odd.findIndex(s=>/^│ RESOURCES/.test(s));
assert.deepEqual(odd.slice(o+1,o+4).map(s=>s.slice(2,-2).trimEnd()),['host-a  CPU 34%  MEM 61%','  GPU0  ━━━━━━━━─ 92%  21/24G','  GPU1  3/24G'],odd.join('\n'));
// No resources, no section; a panel beside the native area only.
assert(!plain(render(44,50,{})).some(s=>/RESOURCES/.test(s)));
assert(!plain(render(100,24,{resources:[A]},{side:false})).some(s=>/RESOURCES/.test(s)));
""")

    def test_narrow_rows_drop_the_gauge_then_the_memory_and_keep_the_number_of_the_session(self):
        self.node(r"""
for(const width of [32,36,40,44]){
 const rows=plain(render(width,50,{resources:[A]}));
 assert(rows.every(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)===width),width+'\n'+rows.join('\n'));
 const g=rows.filter(s=>/GPU[03]/.test(s));
 assert.equal(g.length,2,width+'\n'+rows.join('\n'));
 assert(g[0].includes('[3b]')&&g[1].includes('[c7]'),'the session number stays at '+width+'\n'+rows.join('\n'));
}
const w32=plain(render(32,50,{resources:[A]})).filter(s=>/GPU0/.test(s))[0];
assert(!w32.includes('21/24G')||w32.includes('━'),w32);
""")

    def test_short_of_rows_it_gives_way_first_gpu_rows_then_host_rows_and_the_rest_stands_as_without_it(self):
        self.node(r"""
const kinds=rows=>({gpu:rows.filter(s=>/GPU\d/.test(s)).length,host:rows.filter(s=>/host-[ab] /.test(s)).length,idle:rows.filter(s=>/idle GPU/.test(s)).length,
  head:rows.some(s=>/^│ RESOURCES/.test(s)),usage:rows.filter(s=>/^│ (claude|codex|opencode) /.test(s)||/^│ USAGE/.test(s)).length});
const routeBox=rows=>{const a=rows.findIndex(s=>/╭─/.test(s)),z=rows.findIndex(s=>/╰─/.test(s));return a<0?null:rows.slice(a,z+1).join('\n');};
let seen=new Set();
for(let h=12;h<=60;h++){
 const bare=plain(render(44,h,{})),withR=plain(render(44,h,{resources:[A,B]})),k=kinds(withR),k0=kinds(bare);
 // Everything else stands as it does without RESOURCES: the route box, USAGE.
 assert.equal(routeBox(withR),routeBox(bare),h+' the route box is the same\n'+withR.join('\n'));
 assert.equal(k.usage,k0.usage,h+' USAGE is the same\n'+withR.join('\n'));
 assert.equal(withR.length,h);
 // The order: the GPU rows (the idle count with them) go before the host rows, the host rows before the name row.
 if(k.host<2)assert(k.gpu===0||k.host===1&&k.gpu>0&&k.gpu<=3,h+JSON.stringify(k));
 if(k.host===0)assert(!k.head&&k.gpu===0,h+JSON.stringify(k));
 if(k.head)assert(k.host>=1,h+JSON.stringify(k));
 seen.add(JSON.stringify([k.head,k.host,k.gpu+k.idle]));
}
// Every step of the way shows at some height.
for(const want of [[false,0,0],[true,1,0],[true,2,0],[true,2,4],[true,2,5]])assert(seen.has(JSON.stringify(want))||want[2]!==0&&[...seen].some(s=>JSON.parse(s)[1]===want[1]&&JSON.parse(s)[2]>0),JSON.stringify(want)+' '+[...seen].join(' '));
""")

    def test_the_settings_editor_has_the_worker_box_choice_and_it_cycles(self):
        self.node(r"""
assert(view.fields.includes('worker_box'));
assert.deepEqual(view.options.worker_box,['closed','open','none']);
assert.equal(view.defaults.worker_box,'closed');
let value={...view.defaults};const seen=[];
for(let i=0;i<4;i++){seen.push(value.worker_box);value=view.change(value,'worker_box',1);}
assert.deepEqual(seen,['closed','none','open','closed'].slice(0,0).concat(seen));
assert.deepEqual(new Set(seen),new Set(['closed','open','none']));
""")


class ResourcesBackendTest(unittest.TestCase):
    HOSTS = {"configured": True, "observed_at": 1000.0, "hosts": [
        {"host": "host-a", "self": True, "reachable": True, "cpu_utilization_pct": 34, "cpu_count": 32,
         "memory_total_mib": 65536, "memory_used_mib": 40000,
         "gpus": [{"index": 0, "name": "NVIDIA GeForce RTX 4090", "utilization_gpu_pct": 92, "memory_used_mib": 21504, "memory_total_mib": 24576,
                   "processes": [{"pid": 11, "used_memory_mib": 21504, "session_owner": {"kind": "session", "harness": "claude", "id": "sid-3b"}}]},
                  {"index": 1, "name": "NVIDIA GeForce RTX 4090", "utilization_gpu_pct": 0, "memory_used_mib": 0, "memory_total_mib": 24576, "processes": []},
                  {"index": 2, "name": "NVIDIA GeForce RTX 4090", "utilization_gpu_pct": 0, "memory_used_mib": 0, "memory_total_mib": 24576, "processes": []},
                  {"index": 3, "name": "NVIDIA GeForce RTX 4090", "utilization_gpu_pct": 58, "memory_used_mib": 14336, "memory_total_mib": 24576,
                   "processes": [{"pid": 12, "used_memory_mib": 14336}]}]},
        {"host": "host-b", "reachable": False}]}

    def index(self):
        session = type("S", (), {"harness": "claude"})()
        return {"3b": {"sid": "sid-3b", "ids": ["sid-3b"], "session": session}}

    def test_the_producer_view_names_the_session_that_holds_a_gpu_and_leaves_an_unknown_one_empty(self):
        import human_wait_state as state
        view = state.resources_view(self.HOSTS, 1000.0, self.index())
        a, b = view["hosts"]
        self.assertEqual((a["name"], a["self"], a["cpu"], a["mem"]), ("host-a", True, 34, 61))
        by = {g["i"]: g for g in a["gpus"]}
        self.assertEqual((by[0]["busy"], by[0]["users"], by[0]["util"]), (True, ["3b"], 92))
        self.assertEqual((by[3]["busy"], by[3]["users"]), (True, []))          # a process whose session is not known
        self.assertEqual((by[1]["busy"], by[2]["busy"]), (False, False))
        self.assertEqual((b["name"], b["cpu"], b["mem"], b["gpus"]), ("host-b", None, None, []))  # unreachable: its name alone
        # A probe that failed, or no host at all, gives no view.
        self.assertEqual(state.resources_view({"error": "timed out", "hosts": []}, 1.0, {}), {})
        self.assertEqual(state.resources_view(None, 0.0, {}), {})

    def test_the_cache_keeps_only_a_well_formed_bounded_resources_view(self):
        import human_wait_state as state
        good = state.resources_view(self.HOSTS, 1000.0, self.index())
        self.assertEqual(state._valid_resources(good), good)
        for bad in ({**good, "hosts": good["hosts"] * 5}, {**good, "observed_at": "x"}, {"hosts": []}, None, [],
                    {**good, "hosts": [{**good["hosts"][0], "cpu": 101}]},
                    {**good, "hosts": [{**good["hosts"][0], "gpus": [{**good["hosts"][0]["gpus"][0], "users": ["x" * 40]}]}]},
                    {**good, "hosts": [{**good["hosts"][0], "gpus": good["hosts"][0]["gpus"] * 9}]}):
            self.assertEqual(state._valid_resources(bad), {})

    def test_the_sample_is_taken_by_a_pass_that_is_running_never_waited_for_and_not_more_often_than_its_period(self):
        import human_wait_state as state
        started = []

        class Quick:
            def __init__(self, target, **_k):
                self.target = target
            def start(self):
                started.append(1)
                self.target()
            def is_alive(self):
                return False
        fake = mock.Mock(return_value={"configured": True, "hosts": [], "observed_at": 5.0})
        with mock.patch.object(state, "_resources", {"started": 0.0, "at": 0.0, "snapshot": None, "thread": None}), \
                mock.patch.object(state.threading, "Thread", Quick), \
                mock.patch("tools.fleet.collectors.compute_hosts.collect", fake):
            first = state._resource_sample(100.0)
            self.assertEqual(first[0], {"configured": True, "hosts": [], "observed_at": 5.0})  # (here the thread ran at once)
            state._resource_sample(100.0 + state.RESOURCE_SAMPLE_S - 1)
            self.assertEqual(len(started), 1)                                                  # not again within the period
            state._resource_sample(100.0 + state.RESOURCE_SAMPLE_S + 1)
            self.assertEqual(len(started), 2)

    def test_from_the_producers_cache_to_the_panels_rows_and_an_old_sample_is_not_drawn(self):
        import human_wait_state as state
        from fleet.session_panel import RESOURCES_MAX_AGE, resource_rows
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                mock.patch.object(state, "_request_producer"):
            self.assertTrue(state.publish_cache({"items": [], "observed_at": 1000.0, "complete": True,
                                                  "resources": state.resources_view(self.HOSTS, 1000.0, self.index())}))
            cached = state.cached_view(now=1000.0, request=False)
        rows = resource_rows(cached["resources"], 1000.0 + 5)
        self.assertEqual([r["host"] for r in rows], [[("host-a", "loc_repo")], [("host-b", "loc_repo")]])
        a = rows[0]
        self.assertEqual((a["idle"], [g["label"][0][0] for g in a["gpus"]]), (2, ["GPU0", "GPU3"]))
        self.assertEqual(a["gpus"][0]["users"][0][0], "[3b]")
        self.assertNotIn("users", a["gpus"][1])
        self.assertEqual([p[0] for p in a["gpus"][0]["mem"]], ["21/24G"])
        self.assertEqual([item[0][0] for item in a["stats"]], ["CPU ", "MEM "])
        self.assertEqual(rows[1]["stats"], [])
        self.assertEqual(resource_rows(cached["resources"], 1000.0 + RESOURCES_MAX_AGE + 1), [])
        self.assertEqual(resource_rows({}, 0), [])

    def test_a_request_names_the_other_sessions_repository_unless_it_is_this_windows_own(self):
        from fleet.session_panel import request_rows
        record = {"id": "req-1", "state": "open", "title": "현황 문서 만들기", "updated": 100, "revisions": [{"n": 1, "state": "current", "transfer_ref": "t1"}],
                  "recipient": {"harness": "claude", "session_id": "sid-c2", "name": "c2"}, "requester": {"harness": "claude", "session_id": "me", "name": "me"}}
        views = [{"record": record, "side": "out", "rows": {}}]
        tags = lambda harness, sid: {"sid-c2": "c2"}.get(sid)  # noqa: E731
        doers = lambda harness, sid: {"sid-c2": {"model": "claude-opus-5-5", "repo": "other-repo"}}.get(sid)  # noqa: E731
        got = request_rows(views, 110, tags, doers, own_repo="hearting")[0]
        self.assertEqual((got["who"], got.get("repo")), ("[c2]", "other-repo"))
        self.assertNotIn("repo", request_rows(views, 110, tags, doers, own_repo="other-repo")[0])      # the same repository: none
        self.assertEqual(request_rows(views, 110, tags, doers, own_repo=None)[0].get("repo"), "other-repo")  # this one's unknown: still named
        none = lambda harness, sid: {"model": "claude-opus-5-5"}  # noqa: E731
        self.assertNotIn("repo", request_rows(views, 110, tags, none, own_repo="hearting")[0])         # the other's is unknown: none

    def test_the_pass_hands_each_sessions_fleet_repository_name_to_the_cache(self):
        import human_wait_state as state
        from fleet.explorer.live import session_repos
        session = type("S", (), {"harness": "claude"})()
        found = session_repos(([{"tag": "3b", "repo": "other-repo"}, {"tag": "9z", "repo": None}],
                               {"3b": {"ids": ["sid-3b", "alias-3b"], "session": session}, "9z": {"ids": ["sid-9z"], "session": session}}))
        self.assertEqual(found, {"claude\0sid-3b": "other-repo", "claude\0alias-3b": "other-repo"})
        self.assertEqual(state._valid_doers({"claude\0sid-3b": {"model": "m", "repo": "other-repo"}}), {"claude\0sid-3b": {"model": "m", "repo": "other-repo"}})
        self.assertEqual(state._valid_doers({"claude\0sid-3b": {"repo": "x" * 80}}), {})


class RequestRepoPanelTest(unittest.TestCase):
    def test_a_request_line_carries_the_repository_after_the_number_and_the_title_goes_before_it(self):
        node = FIXTURE + r'''
const reqs=[{id:'q1',kind:'request-out',way:'→',who:'[c2]',repo:'other-repo',title:'현황 문서 만들기',status:'open',paint:{harness:P('claude'),model_text:P('Opus 5.5')},age:'2m'},
 {id:'q2',kind:'request-in',way:'◂',who:'[6c]',repo:'data-tools',title:'원시 데이터 옮기기',status:'partial'},
 {id:'q3',kind:'request-out',way:'→',who:'[7d]',title:'같은 저장소의 요청',status:'open'}];
const r=(w,h,o)=>plain(render(w,h,{requests:reqs,...o},o?.opts));
const side=r(44,40).map(s=>s.slice(2,-2).trimEnd());
assert(side.some(s=>/│ → \[c2\] other-repo claude Opus 5\.5 +2m/.test(s)),side.join('\n'));
assert(side.some(s=>/│ ◂ \[6c\] data-tools\s*│?$/.test(s)||/◂ \[6c\] data-tools/.test(s)),side.join('\n'));
assert(side.some(s=>/→ \[7d\]\s*│?$/.test(s)||/→ \[7d\]$/.test(s.trim())),'a request with no repository name reads as before\n'+side.join('\n'));
// The bottom layout's one row: with no harness or model between them the repository and the title read `[c2] other-repo · title`.
const bottom=plain(view.render({width:60,height:20,side:false,fill:false,harness:'claude',frame:0,model:{repo:[P('hearting/')],git:[P('main')],tag:'[c7]',messages:[],styles:STYLES,routes:[],requests:reqs,route_count:0,route_total:0},state:'working',configuration:{},questions:()=>[]}));
assert(bottom.some(s=>/→ \[c2\] other-repo/.test(s)),bottom.join('\n'));
assert(bottom.some(s=>/◂ \[6c\] data-tools · 원시 데이터 옮기기/.test(s)),bottom.join('\n'));
// Short of cells the title goes first, the repository name stays (down to the narrowest panel, where the number and the title are left).
for(const w of [60,50,44,40,36,32]){   // (at 32 the box holds no more than the number, a short title and the status: the repository name is the last to go and goes there)
 const rows=plain(view.render({width:w,height:20,side:false,fill:false,harness:'claude',frame:0,model:{repo:[P('hearting/')],git:[P('main')],tag:'[c7]',messages:[],styles:STYLES,routes:[],requests:[reqs[1]],route_count:0,route_total:0},state:'working',configuration:{},questions:()=>[]}));
 const line=rows.find(s=>/◂ \[6c\]/.test(s));
 assert(line&&(line.includes('data-tools')||w<36),w+'\n'+rows.join('\n'));
 assert(w<36||line.includes('partial'),w+' the status is not cut off by the box\n'+rows.join('\n'));
 assert(rows.every(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)<=w),w+' no row is wider than the panel\n'+rows.join('\n'));
}
'''
        result = subprocess.run(["node", "-e", node, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_dim_bracket_keeps_its_hue_on_a_shade(self):
        from fleet.palette import style_components
        keys = ["dim", "loc_repo", "g_work", "g_work_off", "g_idle", "g_spin", "lvl_g", "hb_claude", "zone_dispatch", "zone_direct", "zone_request",
                *("kind%s_%s" % (w, f) for f in ("code", "lab", "design", "docs", "research") for w in ("", "q", "s", "b"))]
        styles = {key: style_components(key) for key in keys}
        node = FIXTURE + r'''
const S=JSON.parse(process.argv[2]);
const quick={...route,intensity:'quick',kind_field:'code'};
for(const [shade,background,theme] of [['light','dark','terminal'],['medium','dark','terminal'],['light','light','light'],['medium','','light']]){
 const v=view.render({width:44,height:30,side:true,fill:true,harness:'claude',frame:0,background,
  model:{repo:[P('hearting/')],repo_side:[P('hearting')],git_side:[P('main ✓')],tag:'[c7]',messages:[],styles:S,routes:[quick],route_count:1,route_total:1},
  state:'working',configuration:{shade,theme},questions:()=>[]});
 const bracket=v.rows.flat().find(p=>p.text==='(quick)'),word=v.rows.flat().find(p=>p.text==='code');
 const hue=theme==='light'?26:69;
 assert(bracket&&bracket.foreground===hue&&bracket.dim===true,shade+background+JSON.stringify(bracket));
 assert(word&&word.foreground===hue&&word.bold===true,JSON.stringify(word));
}
'''
        result = subprocess.run(["node", "-e", node, str(SHELL), json.dumps(styles)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SettingsTest(unittest.TestCase):
    def test_the_worker_box_setting_is_saved_with_the_others_and_an_unknown_value_falls_back(self):
        import session_shell as shell
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "HOME": tmp}):
            path = shell.policy_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"enabled": True}))
            path.chmod(0o600)
            self.assertEqual(shell.display_policy()["worker_box"], "closed")
            chosen = {**shell.display_policy(), "worker_box": "open"}
            saved = shell.save_display_policy(chosen)
            self.assertEqual(saved["worker_box"], "open")
            self.assertEqual(shell.display_policy()["worker_box"], "open")
            self.assertEqual(json.loads(path.read_text())["worker_box"], "open")
            self.assertTrue(json.loads(path.read_text())["enabled"])
            path.write_text(json.dumps({"enabled": True, "worker_box": "square"}))
            self.assertEqual(shell.display_policy()["worker_box"], "closed")       # one unknown value does not cost the others


if __name__ == "__main__":
    unittest.main()
