"""11-18 (round 3 of PR #1715): the panel's RESOURCES section and the repository name on a request line, from the producer to the panel.

  * RESOURCES stands above USAGE: one row for each host (`host-a  CPU 34%  MEM 61%`), under it every GPU of the host in its number's order
    side by side (`GPU0 92% 21/24G [3b]  GPU1 0% 0/24G  GPU3 58% 14/24G [c7]`: no gauge, the use in its level's color, the number of the
    session that holds it, left out where it is not known; a GPU nothing runs on has the same group with the sample's values, all of it
    dim and no session number; 11-19), onto the next row where they do not fit and never splitting one GPU's group. The values are the compute-host sample the shared producer keeps; the panel reads the cache only.
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
const STYLES={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:114},lvl_y:{foreground:186},lvl_r:{foreground:217},g_work:{foreground:150},g_work_off:{foreground:150,dim:true}};
const level=pct=>pct>=80?'lvl_r':pct>=50?'lvl_y':'lvl_g';
const gpu=(i,pct,mem,users)=>({label:[D('GPU'+i)],pct:[{text:pct+'%',...STYLES[level(pct)]}],mem:[D(mem)],...(users?{users:[P(users)]}:{})});
const host=(name,cpu,mem,gpus)=>({host:[P(name)],stats:[[D('CPU '),P(cpu+'%')],[D('MEM '),P(mem+'%')]],gpus});
const idleGpu=(i,mem)=>({label:[D('GPU'+i)],pct:[D('0%')],mem:[D(mem)]});
const A=host('host-a',34,61,[gpu(0,92,'21/24G','[3b]'),gpu(3,58,'14/24G','[c7]')]);
const B=host('host-b',12,40,[gpu(1,71,'18/24G','[d4]')]);
const usage=[{harness:'claude',parts:[P('5h 12%')]},{harness:'codex',parts:[P('5h 3%')]},{harness:'opencode',parts:[]}];
const six=at=>['plan','plan-check','execute','impl-review','test','report'].map((name,i)=>i<at?{name,mark:'✓'}:i===at?{name,mark:'●',current:true}:{name,mark:''});
const route={id:'a',zone:'dispatch',title:'갈래를 route에 기록',kind:'working',state:'working',elapsed:'1h 52m',shape:'staged',stages:six(2),kind_word:'code',intensity:'standard',role:'owner',
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
// (44 cells: the two groups of host-a are 20 wide each, 2 in and 2 apart: 44 inside 40 is too much, so each stands on a row.)
assert.deepEqual(body,['host-a  CPU 34%  MEM 61%','  GPU0 92% 21/24G [3b]','  GPU3 58% 14/24G [c7]',
 'host-b  CPU 12%  MEM 40%','  GPU1 71% 18/24G [d4]'],rows.join('\n'));
// Wider, they stand side by side; the level's color is the use's: 92 red, 58 yellow, 71 yellow.
const wide=plain(render(60,50,{resources:[A,B]})),w=wide.findIndex(s=>/^│ RESOURCES/.test(s));
assert.deepEqual(wide.slice(w+1,w+4).map(s=>s.slice(2,-2).trimEnd()),['host-a  CPU 34%  MEM 61%','  GPU0 92% 21/24G [3b]  GPU3 58% 14/24G [c7]','host-b  CPU 12%  MEM 40%'],wide.join('\n'));
const coloured=render(60,50,{resources:[A,B]}).rows.flat().filter(p=>/^\d+%$/.test(p.text)&&['92%','58%','71%'].includes(p.text));
assert.deepEqual(coloured.map(p=>[p.text,p.foreground]),[['92%',217],['58%',186],['71%',186]]);
assert(!render(60,50,{resources:[A,B]}).rows.flat().some(p=>/[━─]{2}/.test(p.text)&&p.foreground===114),'no gauge');
assert(rows.every(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)===44),'every row keeps the width');
// A session that is not known leaves its number cell empty; a GPU whose use is not known keeps its memory (in the column of the others').
const odd=plain(render(44,50,{resources:[host('host-a',34,61,[gpu(0,92,'21/24G',null),{label:[D('GPU1')],mem:[D('3/24G')]}])]}));
const o=odd.findIndex(s=>/^│ RESOURCES/.test(s));
assert.deepEqual(odd.slice(o+1,o+3).map(s=>s.slice(2,-2).trimEnd()),['host-a  CPU 34%  MEM 61%','  GPU0 92% 21/24G  GPU1      3/24G'],odd.join('\n'));
// No resources, no section; a panel beside the native area only.
assert(!plain(render(44,50,{})).some(s=>/RESOURCES/.test(s)));
assert(!plain(render(100,24,{resources:[A]},{side:false})).some(s=>/RESOURCES/.test(s)));
""")

    def test_a_gpu_nothing_runs_on_is_one_group_like_the_others_all_of_it_dim_and_the_host_row_has_no_idle_count(self):
        # 11-19 (the person: "빈 GPU도 일관성있게 표시해줘. 그냥 0% 0/48GB 이런식으로 해놔. 컬러만 좀 어둡게 하면 되잖아. 0%에").
        self.node(r"""
const eight=host('host-c',20,30,[gpu(0,92,'21/24G','[3b]'),idleGpu(1,'0/24G'),idleGpu(2,'0/24G'),gpu(3,58,'14/24G','[c7]'),idleGpu(4,'0/24G'),idleGpu(5,'0/24G'),idleGpu(6,'0/24G'),idleGpu(7,'0/24G')]);
const four=host('host-d',5,10,[0,1,2,3].map(i=>idleGpu(i,'0/48G')));
const none=host('host-e',7,8,[]);
const body=(w,h)=>{const rows=plain(render(w,h,{resources:[eight,four,none]})),at=rows.findIndex(s=>/^│ RESOURCES/.test(s)),us=rows.findIndex(s=>/^│ USAGE/.test(s));return rows.slice(at+1,us).map(s=>s.slice(2,-2).trimEnd());};
const wide=body(60,60);
assert.deepEqual(wide,['host-c  CPU 20%  MEM 30%','  GPU0 92% 21/24G [3b]  GPU1  0%  0/24G','  GPU2  0%  0/24G       GPU3 58% 14/24G [c7]','  GPU4  0%  0/24G       GPU5  0%  0/24G','  GPU6  0%  0/24G       GPU7  0%  0/24G',
 'host-d  CPU  5%  MEM 10%','  GPU0  0%  0/48G       GPU1  0%  0/48G','  GPU2  0%  0/48G       GPU3  0%  0/48G','host-e  CPU  7%  MEM  8%'],wide.join('\n'));
assert(!wide.some(s=>/idle/.test(s)));
// The idle group is all dim (the use too: no level color); the working one keeps its level's color.
const parts=render(60,60,{resources:[eight]}).rows.flat();
const pct=parts.filter(p=>['92%','0%','58%'].includes(p.text));
assert.deepEqual(pct.slice(0,4).map(p=>[p.text,p.foreground??null,!!p.dim]),[['92%',217,false],['0%',null,true],['0%',null,true],['58%',186,false]]);
assert(parts.filter(p=>/^0\/24G$/.test(p.text)).every(p=>p.dim));
// Narrow: the memory goes, then the use; the idle group keeps its label like the others.
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
for(const width of [18,20,24,28,36,46]){const rows=plain(render(width,60,{resources:[eight,four,none]}));assert(rows.every(s=>cells(s)===width),width+'\n'+rows.join('\n'));
 const g=rows.join(' ');for(let i=0;i<8;i++)assert(g.includes('GPU'+i),width+' GPU'+i);}
""")

    def test_the_gpu_groups_and_the_host_values_stand_on_one_grid_at_every_width(self):
        # 11-19 (the person: "그리고 GPU 표시를 행과 열 간격을 맞춰."): every group the same width, the label left, the use and the memory
        # right-aligned, the session numbers left after them (blank where there is none); the same number of groups on every row of every host.
        self.node(r"""
const hosts=[host('host-a',34,61,[gpu(0,92,'21/24G','[3b]'),gpu(1,40,'10/48G','[c7]'),idleGpu(2,'0/48G'),idleGpu(3,'0/48G'),gpu(4,100,'48/48G','[d4]'),idleGpu(5,'0/48G'),idleGpu(6,'0/48G')]),
 host('longer-host-b',3,12,[idleGpu(0,'0/24G'),idleGpu(1,'0/24G'),gpu(2,7,'2/24G',null)]),host('c',100,5,[])];
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const bodyOf=w=>{const rows=plain(render(w,60,{resources:hosts})),at=rows.findIndex(s=>/^│ RESOURCES/.test(s)),us=rows.findIndex(s=>/^│ USAGE/.test(s));
 assert(rows.every(s=>cells(s)===w),w+'\n'+rows.join('\n'));return rows.slice(at+1,us).map(s=>s.slice(2,-2).trimEnd());};
for(let width=28;width<=60;width++){
 const body=bodyOf(width),gpuRows=body.filter(s=>/^  GPU\d/.test(s)),ctx=width+'\n'+body.join('\n');
 assert(gpuRows.length>0,ctx);
 const starts=s=>[...s.matchAll(/GPU\d/g)].map(m=>m.index);
 // The groups start on the same cells on every row, and every row but the last of each host has as many groups as the first.
 const first=starts(gpuRows[0]);
 for(const r of gpuRows){const at=starts(r);assert.deepEqual(at,first.slice(0,at.length),ctx);}
 const perRow=first.length;
 // Where a host's GPUs end, its last row holds the rest; no row has more groups than the section's.
 assert(gpuRows.every(r=>starts(r).length<=perRow),ctx);
 // The values stand at the same offset from the start of their group: the use ends, the memory ends, the session number starts.
 const rel=(re)=>new Set(gpuRows.flatMap(r=>{const at=starts(r);return at.map((a,i)=>{const seg=r.slice(a,at[i+1]??r.length),m=seg.match(re);return m?m.index+m[0].length:null;}).filter(x=>x!==null);}));
 if(gpuRows.some(r=>/%/.test(r)))assert.equal(rel(/\d%/).size,1,'the use ends on one cell '+ctx);
 if(gpuRows.some(r=>/\dG/.test(r)))assert.equal(rel(/\dG/).size,1,'the memory ends on one cell '+ctx);
 const sess=new Set(gpuRows.flatMap(r=>{const at=starts(r);return at.map((a,i)=>{const seg=r.slice(a,at[i+1]??r.length),m=seg.indexOf('[');return m<0?null:m;}).filter(x=>x!==null);}));
 assert(sess.size<=1,'the session numbers start on one cell '+ctx);
 // The host rows: the stats in the same columns for every host, a value's end on the same cell (the names are whole while any stat is).
 const hostRows=body.filter(s=>/^(host-a|longer-host-b|c) /.test(s));
 if(hostRows.every(r=>/CPU/.test(r))){
  assert.equal(new Set(hostRows.map(r=>r.indexOf('CPU'))).size,1,ctx);
  assert.equal(new Set(hostRows.map(r=>r.search(/CPU +\d+%/)+r.match(/CPU +\d+%/)[0].length)).size,1,ctx);
 }
 if(hostRows.every(r=>/MEM/.test(r)))assert.equal(new Set(hostRows.map(r=>r.indexOf('MEM'))).size,1,ctx);
}
// The picture at 60 (inner 56): a group 21 wide, two to a row, `100%` the widest use.
assert.deepEqual(bodyOf(60),['host-a         CPU  34%  MEM 61%','  GPU0  92% 21/24G [3b]  GPU1  40% 10/48G [c7]','  GPU2   0%  0/48G       GPU3   0%  0/48G','  GPU4 100% 48/48G [d4]  GPU5   0%  0/48G','  GPU6   0%  0/48G',
 'longer-host-b  CPU   3%  MEM 12%','  GPU0   0%  0/24G       GPU1   0%  0/24G','  GPU2   7%  2/24G','c              CPU 100%  MEM  5%'],bodyOf(60).join('\n'));
""")

    def test_narrow_groups_drop_the_memory_then_the_use_and_keep_the_number_of_the_session(self):
        self.node(r"""
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
for(const width of [18,20,22,24,26,28,32,36,40,44,60]){
 const rows=plain(render(width,50,{resources:[A]}));
 assert(rows.every(s=>cells(s)===width),width+'\n'+rows.join('\n'));
 const g=rows.filter(s=>/GPU[03]/.test(s)).join(' ');
 assert(g.includes('[3b]')&&g.includes('[c7]'),'the session numbers stay at '+width+'\n'+rows.join('\n'));
 assert(/GPU0/.test(g)&&/GPU3/.test(g),width);
}
// One GPU's group is whole or in a shorter form, never split between rows; every group takes the form that lets the widest stand alone.
const at=w=>plain(render(w,50,{resources:[A]})).filter(s=>/GPU[03]/.test(s)).map(s=>s.slice(2,-2).trimEnd());
assert.deepEqual(at(32),['  GPU0 92% 21/24G [3b]','  GPU3 58% 14/24G [c7]']);
assert.deepEqual(at(24),['  GPU0 92% [3b]','  GPU3 58% [c7]']);          // 20 inside: the memory goes first
assert.deepEqual(at(20),['  GPU0 92% [3b]','  GPU3 58% [c7]']);          // 16 inside: still the use
assert.deepEqual(at(18),['  GPU0 [3b]','  GPU3 [c7]']);                // 14 inside: then the use; the number stays
// A GPU whose session is not known keeps its use and memory.
const unknown=plain(render(24,50,{resources:[host('host-a',34,61,[gpu(0,92,'21/24G',null)])]})).filter(s=>/GPU0/.test(s))[0].slice(2,-2).trimEnd();
assert.equal(unknown,'  GPU0 92% 21/24G');
""")

    def test_a_host_row_gives_way_mem_then_cpu_then_the_name_is_cut(self):
        # 11-19: the row no longer ends in an `idle GPU N` count (every GPU has its own group); the order of leaving is as it was.
        self.node(r"""
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const LONG='gpu-box-with-a-rather-long-hostname';
const rowOf=(width,h)=>{const rows=plain(render(width,60,{resources:[h]})),at=rows.findIndex(s=>/^│ RESOURCES/.test(s));
 assert(rows.every(s=>cells(s)===width),width+'\n'+rows.join('\n'));return rows[at+1].slice(2,-2).trimEnd();};
for(const name of ['devbox','gpu-box',LONG]){
 const h=host(name,31,39,[]);
 for(let width=18;width<=64;width++){
  const r=rowOf(width,h),has=x=>r.includes(x);
  // The order of leaving: MEM first, CPU next, then the name's cut; what stays is what the later ones need.
  if(has('MEM'))assert(has('CPU')&&!r.includes('…'),name+' '+width+': '+r);
  if(has('CPU'))assert(!r.includes('…'),name+' '+width+': '+r);
  assert(!has('idle'),r);
  // The order on the row is still the name, CPU, MEM.
  const at=['CPU','MEM'].map(x=>r.indexOf(x)).filter(i=>i>=0);
  assert.deepEqual(at,[...at].sort((a,b)=>a-b),r);
 }
}
// The widths the person looks at: the long name is cut to the row; with room it is whole with CPU.
assert.equal(rowOf(32,host(LONG,12,8,[])),'gpu-box-with-a-rather-long-…');
assert.equal(rowOf(44,host('gpu-box',12,38,[])),'gpu-box  CPU 12%  MEM 38%');
assert.equal(rowOf(28,host('gpu-box',12,38,[])),'gpu-box  CPU 12%');   // 24 inside 25: MEM goes first
assert.equal(rowOf(60,host(LONG,12,8,[])),LONG+'  CPU 12%  MEM 8%');
""")

    def test_short_of_rows_it_gives_way_first_gpu_rows_then_host_rows_and_the_rest_stands_as_without_it(self):
        self.node(r"""
const kinds=rows=>({gpu:rows.filter(s=>/GPU\d/.test(s)).length,host:rows.filter(s=>/host-[ab]/.test(s)).length,
  head:rows.some(s=>/^│ RESOURCES/.test(s)),usage:rows.filter(s=>/^│ (claude|codex|opencode) /.test(s)||/^│ USAGE/.test(s)).length});
const routeBox=rows=>{const a=rows.findIndex(s=>/╭─/.test(s)),z=rows.findIndex(s=>/╰─/.test(s));return a<0?null:rows.slice(a,z+1).join('\n');};
let seen=new Set();
for(let h=12;h<=60;h++){
 const bare=plain(render(44,h,{})),withR=plain(render(44,h,{resources:[A,B]})),k=kinds(withR),k0=kinds(bare);
 // Everything else stands as it does without RESOURCES: the route box, USAGE.
 assert.equal(routeBox(withR),routeBox(bare),h+' the route box is the same\n'+withR.join('\n'));
 assert.equal(k.usage,k0.usage,h+' USAGE is the same\n'+withR.join('\n'));
 assert.equal(withR.length,h);
 // The order: the GPU rows go before the host rows, the host rows before the name row.
 if(k.host<2)assert(k.gpu===0||k.host===1&&k.gpu>0&&k.gpu<=3,h+JSON.stringify(k));
 if(k.host===0)assert(!k.head&&k.gpu===0,h+JSON.stringify(k));
 if(k.head)assert(k.host>=1,h+JSON.stringify(k));
 seen.add(JSON.stringify([k.head,k.host,k.gpu]));
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
assert.deepEqual(seen,['closed','open','none','closed']);   // (one step each way round, back where it began)
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
        clock = [100.0]
        with mock.patch.object(state, "_resources", {"started": 0.0, "at": 0.0, "snapshot": None, "thread": None, "on": False}), \
                mock.patch.object(state.threading, "Thread", Quick), \
                mock.patch.object(state, "_shared_resource_sample", return_value=None), \
                mock.patch("time.time", lambda: clock[0]), \
                mock.patch("tools.fleet.collectors.compute_hosts.collect", fake):
            first = state._resource_sample(100.0)
            self.assertEqual(first[0], {"configured": True, "hosts": [], "observed_at": 5.0})  # (here the thread ran at once)
            clock[0] = 100.0 + state.RESOURCE_SAMPLE_S - 1
            state._resource_sample(clock[0])
            self.assertEqual(len(started), 1)                                                  # not again within the period
            clock[0] = 100.0 + state.RESOURCE_SAMPLE_S + 1
            state._resource_sample(clock[0])
            self.assertEqual(len(started), 2)

    def test_only_the_resident_producers_loop_samples_the_hosts(self):
        import human_wait_state as state
        self.assertFalse(state._resources["on"])          # a refresh in a test or a one-shot reads no host
        import inspect
        loop = inspect.getsource(state._producer_loop)
        self.assertIn('_resources["on"] = True', loop)
        self.assertIn('_resources["on"] = False', loop)

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
        self.assertNotIn("idle", a)
        self.assertEqual([g["label"][0][0] for g in a["gpus"]], ["GPU0", "GPU1", "GPU2", "GPU3"])       # every GPU, in its number's order
        self.assertEqual(a["gpus"][0]["users"][0][0], "[3b]")
        self.assertEqual(a["gpus"][0]["pct"], [("92%", "lvl_r")])
        self.assertEqual([p[0] for p in a["gpus"][0]["mem"]], ["21/24G"])
        self.assertNotIn("users", a["gpus"][1])                                                          # nothing runs on it: the sample's values, all dim
        self.assertEqual((a["gpus"][1]["pct"], a["gpus"][1]["mem"], a["gpus"][2]["label"]), ([("0%", "dim")], [("0/24G", "dim")], [("GPU2", "dim")]))
        self.assertNotIn("users", a["gpus"][3])
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
