"""11-18 (round 3 of PR #1715): the panel's RESOURCES section and the repository name on a request line, from the producer to the panel.

  * RESOURCES stands above USAGE: one row for each host (`host-a  CPU 34%  MEM 61%`), under it every GPU of the host in its number's order
    side by side (`GPU0 92% 21/24G [3b]  GPU1 0% 0/24G  GPU3 58% 14/24G [c7]`: the use in its level's color, the number of the
    session that holds it, left out where it is not known; a GPU nothing runs on has the same group with the sample's values, all of it
    dim and no session number; 11-19), onto the next row where they do not fit and never splitting one GPU's group. The values are the compute-host sample the shared producer keeps; the panel reads the cache only.
    Where the width has room left after that and the rows would not be more, each group also has Fleet's memory gauge before its memory number
    (`GPU0 93% ━━━━━━━━─ 21/24G [3b]`; 11-19).
    Short of rows it gives way before everything else, so the other sections and the route box stand as they do without it.
  * A request line names the other session's repository after its number (`[c2] other-repo`), not for a session of the window's
    own repository and not where Fleet does not know it; the title goes before the repository name does.
  * The setting `worker box` is one of the panel's own settings (closed, open, none), saved with the others.
  * 11-25 (four groups, "RESOURCES는 CPU, MEM이 그렇게 중요해? GPU가 더 중요한데"): RESOURCES is the third group, `[RESOURCES]` (a frame head), above `[USAGE]`; a host is its name
    alone (no CPU, no MEM; a host with no GPU reads `name  No GPU`) and under it ONE row with two GPUs side by side, the numbers only (no `GPU` word):
    ` 0 92%━━━━─21/24G[3b] 1  0%───── 0/24G`; where the session that holds a GPU is not known the group reads `other`. One grid for every host; the widest form that
    puts two in a row wins (the gauge 5 cells and spaced, 5, 4, none), then one to a row. A host stands with all its rows or not at all, and the empty rows between
    the groups are the first thing given up, before the GPU rows (from the last host) and the host rows.
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
// The backend's memory gauge at every width it may be given (11-19): `gauge4` … `gauge10`; the fill and the color are the backend's (here a stand-in).
const withGauge=(g,ratio,busy)=>{const out={...g};for(let n=4;n<=10;n++){const filled=ratio>0?Math.min(n-1,Math.max(1,Math.round(ratio*n/100))):0;
 const key=busy?'lvl_g':'dim';out['gauge'+n]=[...(filled?[{text:'━'.repeat(filled),...STYLES[key]}]:[]),D('─'.repeat(n-filled))];}return out;};
const gaugeHost=h=>({...h,gpus:h.gpus.map(g=>withGauge(g,g.users?88:0,!!g.users))});
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
// 11-25: the RESOURCES group's rows (its head `├╴[RESOURCES] ──┤` to the next group's head, the empty row before that head left out), the frame cut off.
const resBody=rows=>{const a=rows.findIndex(s=>/^├╴\[RESOURCES\]/.test(s)),u=rows.findIndex(s=>/^├╴\[USAGE\]/.test(s));
 return a<0?null:rows.slice(a+1,u).filter(s=>!/^│ +│$/.test(s)).map(s=>s.slice(2,-2).trimEnd());};
const isGpuRow=s=>/^ *\d+ /.test(s);
'''


class ResourcesPanelTest(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(["node", "-e", FIXTURE + script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_resources_stand_above_usage_with_their_hosts_gpus_and_the_session_numbers(self):
        self.node(r"""
const rows=plain(render(44,50,{resources:[A,B]}));
const at=rows.findIndex(s=>/^├╴\[RESOURCES\] ─+┤$/.test(s)),us=rows.findIndex(s=>/^├╴\[USAGE\] ─+┤$/.test(s));
assert(at>0&&us>at,rows.join('\n'));
const body=resBody(rows);
// (44 cells, 40 inside: the two groups of host-a are 17 wide each (one cell between the memory and the session, 11-25), one in and one apart: 36 inside 40, so they stand side by side on ONE row under the host's name.)
assert.deepEqual(body,['host-a',' 0 92% 21/24G [3b] 3 58% 14/24G [c7]','host-b',' 1 71% 18/24G [d4]'],rows.join('\n'));
// Narrower than the two groups need (36: 32 inside, 36 wanted), each stands on a row of its own; the level's color is the use's: 92 red, 58 yellow, 71 yellow.
assert.deepEqual(resBody(plain(render(36,50,{resources:[A,B]}))),['host-a',' 0 92% 21/24G [3b]',' 3 58% 14/24G [c7]','host-b',' 1 71% 18/24G [d4]']);
assert.deepEqual(resBody(plain(render(60,50,{resources:[A,B]}))),body);   // wider, the same rows (this sample has no gauge)
const coloured=render(60,50,{resources:[A,B]}).rows.flat().filter(p=>/^\d+%$/.test(p.text)&&['92%','58%','71%'].includes(p.text));
assert.deepEqual(coloured.map(p=>[p.text,p.foreground]),[['92%',217],['58%',186],['71%',186]]);
assert(!render(60,50,{resources:[A,B]}).rows.flat().some(p=>/[━─]{2}/.test(p.text)&&p.foreground===114),'no gauge drawn for a sample without one');
// The host row is the name alone: no CPU, no MEM, whatever the sample carries.
assert(!rows.some(s=>/CPU|MEM 6|GPU\d/.test(s)),rows.join('\n'));
assert(rows.every(s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0)===44),'every row keeps the width');
// A holder that is not known reads `other` where the GPU is used; a GPU whose use is not known keeps its memory (in the column of the others').
const odd=plain(render(44,50,{resources:[host('host-a',34,61,[gpu(0,92,'21/24G',null),{label:[D('GPU1')],mem:[D('3/24G')]}])]}));
assert.deepEqual(resBody(odd),['host-a',' 0 92% 21/24G other 1      3/24G other'],odd.join('\n'));
// No resources, no section; a panel beside the native area only.
assert(!plain(render(44,50,{})).some(s=>/RESOURCES/.test(s)));
assert(!plain(render(100,24,{resources:[A]},{side:false})).some(s=>/RESOURCES/.test(s)));
""")

    def test_a_gpu_nothing_runs_on_is_one_group_like_the_others_all_of_it_dim_and_the_host_row_has_no_idle_count(self):
        # 11-19 (the person: "빈 GPU도 일관성있게 표시해줘. 그냥 0% 0/48GB 이런식으로 해놔. 컬러만 좀 어둡게 하면 되잖아. 0%에").
        # 11-25: the group has the number alone (no `GPU`), two to a row under the host's name; a host with no GPU reads `No GPU`.
        self.node(r"""
const eight=host('host-c',20,30,[gpu(0,92,'21/24G','[3b]'),idleGpu(1,'0/24G'),idleGpu(2,'0/24G'),gpu(3,58,'14/24G','[c7]'),idleGpu(4,'0/24G'),idleGpu(5,'0/24G'),idleGpu(6,'0/24G'),idleGpu(7,'0/24G')]);
const four=host('host-d',5,10,[0,1,2,3].map(i=>idleGpu(i,'0/48G')));
const none=host('host-e',7,8,[]);
const body=(w,h)=>resBody(plain(render(w,h,{resources:[eight,four,none]})));
const wide=body(60,60);
assert.deepEqual(wide,['host-c',' 0 92% 21/24G [3b] 1  0%  0/24G',' 2  0%  0/24G      3 58% 14/24G [c7]',' 4  0%  0/24G      5  0%  0/24G',' 6  0%  0/24G      7  0%  0/24G',
 'host-d',' 0  0%  0/48G      1  0%  0/48G',' 2  0%  0/48G      3  0%  0/48G','host-e  No GPU'],wide.join('\n'));
assert(!wide.some(s=>/idle|CPU|MEM|GPU\d/.test(s)));
// The idle group is all dim (the use too: no level color); the working one keeps its level's color.
const parts=render(60,60,{resources:[eight]}).rows.flat();
const pct=parts.filter(p=>['92%','0%','58%'].includes(p.text));
assert.deepEqual(pct.slice(0,4).map(p=>[p.text,p.foreground??null,!!p.dim]),[['92%',217,false],['0%',null,true],['0%',null,true],['58%',186,false]]);
assert(parts.filter(p=>/^0\/24G$/.test(p.text)).every(p=>p.dim));
// An idle group has no holder (not even `other`); the number of its GPU is dim like the others'.
assert(!wide.slice(1,5).join(' ').includes('other'));
// Narrow: the memory goes, then the use is the last of the numbers the form can lose; the idle group keeps its number like the others.
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
for(const width of [18,20,24,28,36,46]){const rows=plain(render(width,60,{resources:[eight,four,none]}));assert(rows.every(s=>cells(s)===width),width+'\n'+rows.join('\n'));
 const b=resBody(rows),first=b.slice(1,b.indexOf('host-d')).join(' ');
 assert.deepEqual([...first.matchAll(/(?:^| )(\d) +\d+%/g)].map(m=>m[1]).sort(),['0','1','2','3','4','5','6','7'],width+'\n'+b.join('\n'));}
""")

    def test_the_gpu_groups_and_the_host_values_stand_on_one_grid_at_every_width(self):
        # 11-19 (the person: "그리고 GPU 표시를 행과 열 간격을 맞춰."): every group the same width, the use and the memory right-aligned, the session numbers left
        # after them (blank where there is none); the same number of groups on every row of every host. 11-25: one grid for every host, the host row the name alone.
        self.node(r"""
const hosts=[host('host-a',34,61,[gpu(0,92,'21/24G','[3b]'),gpu(1,40,'10/48G','[c7]'),idleGpu(2,'0/48G'),idleGpu(3,'0/48G'),gpu(4,100,'48/48G','[d4]'),idleGpu(5,'0/48G'),idleGpu(6,'0/48G')]),
 host('longer-host-b',3,12,[idleGpu(0,'0/24G'),idleGpu(1,'0/24G'),gpu(2,7,'2/24G',null)]),host('c',100,5,[])];
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const bodyOf=w=>{const rows=plain(render(w,60,{resources:hosts}));assert(rows.every(s=>cells(s)===w),w+'\n'+rows.join('\n'));return resBody(rows);};
// A group: the number, the use, [the gauge], the memory, [the sessions]: where each one starts and ends in the row.
const GROUP=/(\d) +(\d+%) *(?:[━─]+)? *(\d+\/\d+G)(?: *(\[[^\]]+\]|other))?/g;
const groupsOf=r=>[...r.matchAll(GROUP)].map(m=>({at:m.index,pct:m.index+m[0].indexOf(m[2])+m[2].length-m.index,mem:m.index+m[0].indexOf(m[3])+m[3].length-m.index,
 who:m[4]&&m[4][0]==='['?m[0].indexOf(m[4]):null,other:m[4]==='other'?m[0].indexOf(m[4]):null}));
for(let width=28;width<=60;width++){
 const body=bodyOf(width),ctx=width+'\n'+body.join('\n');
 // The host rows: the name alone (and `No GPU` where there is none); every host stands, in its order.
 assert.deepEqual(body.filter(s=>!isGpuRow(s)),['host-a','longer-host-b','c  No GPU'],ctx);
 // The GPU rows of every host: the same number of groups on every row but the host's last, the groups on the same cells, the values at the same offset in them.
 const per=[body.slice(1,body.indexOf('longer-host-b')),body.slice(body.indexOf('longer-host-b')+1,body.indexOf('c  No GPU'))];
 const all=per.flat();
 assert(all.length>0,ctx);
 const perRow=groupsOf(all[0]).length;
 assert(perRow===1||perRow===2,ctx);
 for(const host of per){host.forEach((r,i)=>{const g=groupsOf(r);assert(i<host.length-1?g.length===perRow:g.length>=1&&g.length<=perRow,ctx);});}
 const first=groupsOf(all.find(r=>groupsOf(r).length===perRow));
 const sets={at:new Set(),pct:new Set(),mem:new Set(),who:new Set(),other:new Set()};
 for(const r of all)groupsOf(r).forEach((g,i)=>{assert.equal(g.at,first[i].at,'the groups start on the same cells '+ctx);sets.pct.add(g.pct);sets.mem.add(g.mem);if(g.who!==null)sets.who.add(g.who);if(g.other!==null)sets.other.add(g.other);});
 assert.equal(sets.pct.size,1,'the use ends on one cell '+ctx);
 assert.equal(sets.mem.size,1,'the memory ends on one cell '+ctx);
 assert(sets.who.size<=1,'the session numbers start on one cell '+ctx);
 // (an unknown holder, `other`, stands one cell on from where a number stands when there is no gauge between: it must not touch the memory.)
 assert(sets.other.size<=1&&[...sets.other].every(o=>!sets.who.size||o-[...sets.who][0]===1||o===[...sets.who][0]),'the unknown holder stands in the sessions column '+ctx);
 // Every GPU of every host is there, once, in its number's order.
 assert.deepEqual(all.flatMap(r=>[...r.matchAll(/(?:^| )(\d) +\d+%/g)].map(m=>m[1])).join(''),'0123456'+'012',ctx);
}
// The picture at 60 (inner 56): a group 19 wide (one cell between the memory and the sessions, 11-25), two to a row, `100%` the widest use; the host rows the names alone.
assert.deepEqual(bodyOf(60),['host-a',' 0  92% 21/24G [3b]  1  40% 10/48G [c7]',' 2   0%  0/48G       3   0%  0/48G',' 4 100% 48/48G [d4]  5   0%  0/48G',' 6   0%  0/48G',
 'longer-host-b',' 0   0%  0/24G       1   0%  0/24G',' 2   7%  2/24G other','c  No GPU'],bodyOf(60).join('\n'));
""")

    def test_narrow_groups_drop_the_memory_then_the_use_and_keep_the_number_of_the_session(self):
        # 11-25: two to a row while 36 cells are inside (one cell always between the memory and the sessions), then one to a row under the host's name; only below 17 inside the
        # memory goes, then the use.
        self.node(r"""
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
for(const width of [18,20,22,24,26,28,32,36,40,44,60]){
 const rows=plain(render(width,50,{resources:[A]}));
 assert(rows.every(s=>cells(s)===width),width+'\n'+rows.join('\n'));
 const g=resBody(rows).join(' ');
 assert(g.includes('[3b]')&&g.includes('[c7]'),'the session numbers stay at '+width+'\n'+rows.join('\n'));
 assert(/(^| )0 /.test(g)&&/(^| )3 /.test(g),width+'\n'+rows.join('\n'));
}
// One GPU's group is whole or in a shorter form, never split between rows; the groups of the section take one form together.
const at=w=>resBody(plain(render(w,50,{resources:[A]})));
assert.deepEqual(at(40),['host-a',' 0 92% 21/24G [3b] 3 58% 14/24G [c7]']);    // 36 inside: the two stand side by side
assert.deepEqual(at(38),['host-a',' 0 92% 21/24G [3b]',' 3 58% 14/24G [c7]']);  // 34 inside, 36 wanted: one to a row
assert.deepEqual(at(24),['host-a',' 0 92% 21/24G [3b]',' 3 58% 14/24G [c7]']);
assert.deepEqual(at(21),['host-a','0 92% 21/24G [3b]','3 58% 14/24G [c7]']);    // 17 inside: the group fills it, no indent
assert.deepEqual(at(20),['host-a','0 92% [3b]','3 58% [c7]']);                  // 16 inside: the memory goes first (it never touches the session); the use and the number stay
assert.deepEqual(at(18),['host-a','0 92% [3b]','3 58% [c7]']);
// A GPU whose session is not known keeps its use and memory, and reads `other`.
const unknown=resBody(plain(render(24,50,{resources:[host('host-a',34,61,[gpu(0,92,'21/24G',null)])]})))[1];
assert.equal(unknown,' 0 92% 21/24G other');
""")

    def test_the_memory_gauge_stands_only_where_the_rows_are_as_many_and_otherwise_the_picture_is_what_it_was(self):
        # 11-19 (the person, on v4.59.6's panel: "가로로 길어지면 RESOURCE에 게이지도 넣어주면 좋고"): rows first, the gauge only with what the width leaves.
        # 11-25: the gauge is 4 or 5 cells (half what it was), spaced or close to the numbers, the sessions always one cell after the memory; two GPUs to a row without a gauge
        # beat one to a row with it.
        self.node(r"""
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const two=host('host-a',34,61,[gpu(0,92,'21/24G','[3b]'),idleGpu(1,'0/24G')]),one=host('host-b',12,40,[idleGpu(0,'0/48G')]);
const eight=host('host-c',20,30,[gpu(0,92,'21/24G','[3b]'),idleGpu(1,'0/24G'),idleGpu(2,'0/24G'),gpu(3,58,'14/24G','[c7]'),idleGpu(4,'0/24G'),idleGpu(5,'0/24G'),idleGpu(6,'0/24G'),idleGpu(7,'0/24G')]);
const four=host('host-d',5,10,[0,1,2,3].map(i=>idleGpu(i,'0/48G')));
const body=(w,hosts)=>{const rows=plain(render(w,60,{resources:hosts}));
 assert(rows.every(s=>cells(s)===w),w+'\n'+rows.join('\n'));return resBody(rows);};
const gpuRows=b=>b.filter(isGpuRow);
let gauged=0,plainOnes=0;
for(const [a,b] of [[two,one],[eight,four]].map(x=>[x,x.map(gaugeHost)])){
 for(let w=28;w<=100;w++){
  const without=body(w,a),withIt=body(w,b);
  // (1) The rows are as many as without the gauge, and every other row (the host names, the sections around) is the same.
  assert.equal(withIt.length,without.length,w+'\n'+withIt.join('\n'));
  assert.deepEqual(withIt.filter(s=>!isGpuRow(s)),without.filter(s=>!isGpuRow(s)),w);
  const drawn=withIt.some(s=>/[━─]/.test(s));
  if(drawn){
   gauged++;
   // The gauge is 4 or 5 cells, the same on every group, and stands right before the memory number.
   const runs=withIt.flatMap(s=>s.match(/[━─]+/g)||[]);
   assert(runs.length>0&&runs.every(r=>r.length===runs[0].length&&r.length>=4&&r.length<=5),w+'\n'+withIt.join('\n'));
   assert(gpuRows(withIt).every(s=>!/\d%\s+\d+\/\d+G/.test(s)),w);
   assert(gpuRows(withIt).every(s=>/[━─]+ *\d+\/\d+G/.test(s)),w+'\n'+withIt.join('\n'));
  }else{
   // (2) Where it cannot stand with the rows as they are, the picture is what it was without it, character for character.
   plainOnes++;
   assert.deepEqual(withIt,without,w+'\n'+withIt.join('\n'));
  }
 }
}
assert(gauged>0&&plainOnes>0,gauged+' '+plainOnes);
// The picture: hosts of two GPUs and one. 59 wide (55 inside): the gauge 5 cells, spaced, the two groups on a row. 50: close to the numbers, the session one cell after the memory.
assert.deepEqual(body(59,[two,one].map(gaugeHost)),['host-a','  0 92% ━━━━─ 21/24G [3b]  1  0% ─────  0/24G','host-b','  0  0% ─────  0/48G']);
assert.deepEqual(body(50,[two,one].map(gaugeHost)),['host-a',' 0 92%━━━━─21/24G [3b] 1  0%───── 0/24G','host-b',' 0  0%───── 0/48G']);
assert.deepEqual(body(46,[two,one].map(gaugeHost)),['host-a',' 0 92%━━━─21/24G [3b] 1  0%──── 0/24G','host-b',' 0  0%──── 0/48G']);   // 4 cells
assert.deepEqual(body(44,[two,one].map(gaugeHost)),['host-a',' 0 92% 21/24G [3b] 1  0%  0/24G','host-b',' 0  0%  0/48G']);   // no gauge: the cell before the session stays
// Two to a row without a gauge (40: 36 inside) beat one to a row with it: the picture is what it was.
assert.deepEqual(body(40,[two,one].map(gaugeHost)),body(40,[two,one]));
assert.deepEqual(body(40,[two,one]),['host-a',' 0 92% 21/24G [3b] 1  0%  0/24G','host-b',' 0  0%  0/48G']);
// Narrower than the two need even so, one to a row with the gauge (the rows are the same as without).
assert.deepEqual(body(30,[two,one].map(gaugeHost)),['host-a',' 0 92% ━━━━─ 21/24G [3b]',' 1  0% ─────  0/24G','host-b',' 0  0% ─────  0/48G']);
// A host of eight GPUs and a host of four: the gauge costs no row.
assert.equal(gpuRows(body(90,[eight,four].map(gaugeHost))).length,gpuRows(body(90,[eight,four])).length);
// (3) The gauge of a GPU nothing runs on is dim all through; a working GPU's fill has its level's color and the rest is dim.
const v=render(64,60,{resources:[two,one].map(gaugeHost)}),texts=v.rows.map(text);
const from=texts.findIndex(s=>/^├╴\[RESOURCES\]/.test(s)),to=texts.findIndex(s=>/^├╴\[USAGE\]/.test(s));
const parts=v.rows.slice(from+1,to).flat().filter(p=>/^[━─]+$/.test(p.text));
assert(parts.length>0);
const idleParts=parts.filter(p=>/^─+$/.test(p.text)&&p.dim),filled=parts.filter(p=>/━/.test(p.text));
assert.equal(filled.length,1);assert.equal(filled[0].foreground,114);assert(!filled[0].dim);
assert.equal(idleParts.length,parts.length-1);
// A sample that carries no gauge draws no gauge, whatever the width.
assert(!body(100,[two,one]).some(s=>/[━─]/.test(s)));
""")

    def test_a_host_row_gives_way_mem_then_cpu_then_the_name_is_cut(self):
        # 11-19: the row no longer ends in an `idle GPU N` count (every GPU has its own group). 11-25: the host row is the name alone (no CPU, no MEM to give way);
        # a host with no GPU reads `name  No GPU`, and where the row is short the name is cut with `…` (the `No GPU` goes with the cut, the name's last).
        self.node(r"""
const cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const LONG='gpu-box-with-a-rather-long-hostname';
const rowOf=(width,h)=>{const rows=plain(render(width,60,{resources:[h]}));
 assert(rows.every(s=>cells(s)===width),width+'\n'+rows.join('\n'));return resBody(rows)[0];};
for(const name of ['devbox','gpu-box',LONG]){
 for(const h of [host(name,31,39,[]),host(name,31,39,[gpu(0,5,'1/24G','[3b]')])]){
  for(let width=18;width<=64;width++){
   const r=rowOf(width,h),inner=width-4;
   assert(!/CPU|MEM|idle/.test(r),r);
   assert(cells(r)<=inner,name+' '+width+': '+r);
   // The row is the name (and `  No GPU` after it for a host with none): whole while it fits, then cut with `…` from the back.
   const full=name+(h.gpus.length?'':'  No GPU');
   if(cells(full)<=inner)assert.equal(r,full,width+'');
   else assert(r.endsWith('…')&&full.startsWith(r.slice(0,-1)),name+' '+width+': '+r);
  }
 }
}
// The widths the person looks at: the long name is cut to the row; with room it is whole.
assert.equal(rowOf(32,host(LONG,12,8,[])),'gpu-box-with-a-rather-long-…');
assert.equal(rowOf(44,host('gpu-box',12,38,[])),'gpu-box  No GPU');
assert.equal(rowOf(44,host('gpu-box',12,38,[gpu(0,5,'1/24G',null)])),'gpu-box');
assert.equal(rowOf(60,host(LONG,12,8,[])),LONG+'  No GPU');
""")

    def test_short_of_rows_it_gives_way_first_gpu_rows_then_host_rows_and_the_rest_stands_as_without_it(self):
        # 11-25: the empty rows between the groups go before any of RESOURCES' rows, one at a time; a host stands with all its rows or not at all (from the last host first),
        # and what a host's rows leave over goes back to the empty rows (the one before SESSION first), never to the middle.
        self.node(r"""
const C=host('host-c',2,2,[0,1,2,3].map(i=>idleGpu(i,'0/48G')));   // two rows at 44 (two to a row)
const ROWS={'host-a':1,'host-b':1,'host-c':2};
const kinds=rows=>({gpu:rows.filter(s=>isGpuRow(s.slice(2,-2))).length,host:rows.filter(s=>/^│ host-[abc]/.test(s)).length,
  head:rows.some(s=>/^├╴\[RESOURCES\]/.test(s)),usage:rows.filter(s=>/^│ (claude|codex|opencode) /.test(s)||/^├╴\[USAGE\]/.test(s)).length});
const routeBox=rows=>{const a=rows.findIndex(s=>/╭─/.test(s)),z=rows.findIndex(s=>/╰─/.test(s));return a<0?null:rows.slice(a,z+1).join('\n');};
// `gaps`: the empty row before [SESSION] (the last of them to go); `allGaps`: the empty rows right before any group head; `fill`: any other empty row (the middle's).
const gaps=rows=>rows.filter((s,i)=>/^│ +│$/.test(s)&&/^├╴\[SESSION\]/.test(rows[i+1]||'')).length;
const allGaps=rows=>rows.filter((s,i)=>/^│ +│$/.test(s)&&/^├╴\[/.test(rows[i+1]||'')).length;
const fill=rows=>rows.filter((s,i)=>/^│ +│$/.test(s)&&!/^├╴\[/.test(rows[i+1]||'')).length;
for(const hosts of [[A,B],[A,B,C]]){
 const names=hosts.map(h=>h.host[0].text),whole=names.reduce((n,x)=>n+ROWS[x],0);
 let seen=new Set();
 for(let h=12;h<=60;h++){
  const bare=plain(render(44,h,{})),withR=plain(render(44,h,{resources:hosts})),k=kinds(withR),k0=kinds(bare);
  // Everything else stands as it does without RESOURCES: the route box, USAGE.
  assert.equal(routeBox(withR),routeBox(bare),h+' the route box is the same\n'+withR.join('\n'));
  assert.equal(k.usage,k0.usage,h+' USAGE is the same\n'+withR.join('\n'));
  assert.equal(withR.length,h);
  // The order: the GPU rows go first from the last host, then its name with them (a host is whole or not there); the head goes with the last host.
  const shown=names.slice(0,k.host);
  assert.equal(k.gpu,shown.reduce((n,x)=>n+ROWS[x],0),h+JSON.stringify(k)+'\n'+withR.join('\n'));
  assert.equal(k.head,k.host>=1,h+JSON.stringify(k));
  // The empty rows between the groups are the first to go: while RESOURCES stands with some of its rows missing, no row is to spare (none in the middle), and of the empty
  // rows between the groups only the rows the missing host's name and GPU rows left over stand, the one before SESSION first (with none of it standing, the rows it would
  // not fit in may be what the empty rows come back on).
  if(k.host>=1&&k.gpu<whole){
   const left=ROWS[names[k.host]];   // (the missing host's name and GPU rows, less the one row that was wanted)
   assert.equal(fill(withR),0,h+' no row in the middle while a host is short\n'+withR.join('\n'));
   assert(allGaps(withR)<=left,h+' the empty rows go first\n'+withR.join('\n'));
   if(allGaps(withR))assert.equal(gaps(withR),1,h+' the one before SESSION is the last to go\n'+withR.join('\n'));
  }
  // A panel with a row to spare has every empty row between the groups.
  if(fill(withR))assert.equal(allGaps(withR),withR.filter(s=>/^├╴\[/.test(s)).length-1,h+' a row in the middle while a gap is short\n'+withR.join('\n'));
  seen.add(JSON.stringify([k.head,k.host,k.gpu]));
 }
 // Every step of the way shows at some height: no RESOURCES, each host more, all of them.
 for(const want of [[false,0,0],...names.map((_x,i)=>[true,i+1,names.slice(0,i+1).reduce((n,x)=>n+ROWS[x],0)])])assert(seen.has(JSON.stringify(want)),JSON.stringify(want)+' '+[...seen].join(' '));
 // With all its rows the section has the empty rows before the groups again (at a height with room to spare).
 assert.equal(gaps(plain(render(44,60,{resources:hosts}))),1,'the empty rows before the groups come back');
}
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
        # 11-19: each GPU's memory gauge at every width 4..10 is Fleet's VRAM gauge (its fill function and its level key); a GPU nothing
        # runs on has it all dim; the widths are the ones the panel picks from.
        from fleet.render_gpu import _gauge_segs, _resource_level_key
        self.assertEqual(sorted(k for k in a["gpus"][0] if k.startswith("gauge")), sorted("gauge%d" % n for n in range(4, 11)))
        for cells in range(4, 11):
            filled, empty = _gauge_segs(100.0 * 21 / 24, cells, track=cells)
            self.assertEqual(a["gpus"][0]["gauge%d" % cells], [(filled[0], _resource_level_key(100.0 * 21 / 24, True)), (empty[0], "dim")])
            self.assertEqual(a["gpus"][1]["gauge%d" % cells], [("\u2500" * cells, "dim")])
            self.assertTrue(all(len("".join(t for t, _k in a["gpus"][i]["gauge%d" % cells])) == cells for i in range(4)))
        self.assertEqual(a["gpus"][3]["gauge10"][0][1], _resource_level_key(100.0 * 14 / 24, True))
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
