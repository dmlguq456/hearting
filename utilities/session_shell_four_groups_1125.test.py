"""11-25 (four groups, the person's choice of the fourth design round's "나" draft: "저장소 먼저로 네 묶음 주고로 해줘"): beside the native area the panel is four groups,
`[REPO]` (GIT, STATUS, BACKLOG, MEM), `[SESSION]` (MODEL, CTX, TITLE, PEERS, NOW, QUESTIONS, ROUTE, MESSAGES), `[RESOURCES]`, `[USAGE]`. The rules this file holds
(each checked on fake text only; the older panel suites hold their own forms):

1. the four heads are one form, a frame row `├╴[NAME] value ───┤`; with no RESOURCES `[USAGE]` is the third head;
2. one empty row before each head after the first, and short of rows these empty rows are the first thing given up (before any GPU row of RESOURCES), one at a time (USAGE's,
   RESOURCES', then SESSION's; with the lists SESSION's outlives the BACKLOG items, 11-23: "REPO와 SESSION 구분줄 사이에 여백이 한칸은 있어야 될것 같다"), and a panel with a row to spare has all of them;
3. a list head is bold at the body's brightness, a value's name dim, a group's name bold and the brightest; mono and `NO_COLOR` keep the weights;
4. a BACKLOG item has its dot; the number on a list head is the items off the screen (`+136/139 · FIX 54 · IMPR 36`), the sorts' counts going first when narrow;
5. PEERS stands under TITLE and MEM ends REPO, each with its press rows following it, and a low panel's `[REPO]` head carries the two buttons with the documents' whole counts;
6. RESOURCES: a host's name alone, under it one row with two GPUs in columns that line up across hosts, one gauge length for every host; the form by width (58, 52, 50, 44, 30), the memory
   and the sessions never drawn together; `other`, `No GPU`, an idle GPU all dim; a GPU held by several sessions unfolds only its own host, and a card that overflows its own row gives up its
   gauge, then its memory, then its use and keeps every session number;
7. every row of every size is as wide as the panel in cells (a Hangul is two), the head cells move with the frame, and `rule` motion's light runs to the cell before `┤`.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
NODE = shutil.which("node")

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},plain:{},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},lvl_r:{foreground:217,bold:true},
  g_blocked:{foreground:217},g_spin:{foreground:230},hb_claude:{foreground:116},hb_codex:{foreground:147},hb_opencode:{foreground:153}};
const t=(text,key='dim')=>({text,...(styles[key]||{dim:key==='dim'})});
const level=n=>n>=80?'lvl_r':n>=50?'lvl_y':'lvl_g';
const gauge=(n,used,total)=>{const ratio=100*used/total,fill=ratio===0?0:Math.min(n,Math.max(1,Math.round(n*ratio/100)));
  return [...(fill?[t('━'.repeat(fill),level(ratio))]:[]),t('─'.repeat(n-fill))];};
const gpu=(i,pct,used,total,holders=[])=>{const busy=pct>0||used>0||holders.length>0;
  return {label:[t('GPU'+i)],pct:[busy?t(pct+'%',level(pct)):t(pct+'%')],mem:[t(used+'/'+total+'G')],
    users:holders.flatMap((h,k)=>[...(k?[t(' ')]:[]),t('['+h+']','loc_repo')]),
    ...Object.fromEntries([4,5,6,7,8,9,10].map(n=>['gauge'+n,gauge(n,used,total)]))};};
const host=(name,gpus)=>({host:[t(name,'loc_repo')],stats:[[t('CPU '),t('34%','loc_repo')],[t('MEM '),t('61%','loc_repo')]],gpus});
const HOSTS=[host('lab-a',[gpu(0,92,41,48,['c6']),gpu(1,48,15,48,['d2'])]),host('lab-b',[gpu(0,100,26,32,['c6']),gpu(1,66,18,32,['a7'])]),
  host('lab-c',[gpu(0,78,17,24),gpu(1,0,0,24)]),host('lab-d',[gpu(0,32,6,12,['d2']),gpu(1,0,0,12)]),host('testgpu',[gpu(0,71,37,48,['a7']),gpu(1,0,0,48)])];
const items=(word,names,kind)=>names.map((text,i)=>({text,key:'item-'+(i+1),kind}));
const STATUS={word:'STATUS',has_doc:true,total:5,more:false,items:items('STATUS',['시험용 작업 가','시험용 작업 나','시험용 작업 다'])};
const BACKLOG={word:'BACKLOG',has_doc:true,total:139,more:false,counts:{fix:54,improve:36,note:39,tbd:10},items:items('BACKLOG',['시험용 줄 정리','시험용 글자 맞춤','시험용 안내 고침'],'fix')};
const usage=(h,a,b)=>({harness:h,parts:[t('5h '),t(a+'%',level(a)),t(' │ '),t('7d '),t(b+'%',level(b))]});
const context=[t('━━━━','lvl_g'),t('────'),t(' 41%','loc_repo')];
function model(o={}){
  return {styles,colors:256,tag:'[c6]',steward:true,repo:[t('trial','loc_repo')],repo_side:[t('trial','loc_repo')],worktrees:[t('wt 12')],
    git:[t('main','loc_repo')],git_side:[t('main','loc_repo'),t(' ✓','lvl_g')],selected:[t('Opus 5.5','hb_claude'),t(' xhigh','loc_repo')],context_side:context,context:[t('context '),...context],
    session_title:'시험용 패널 작업',now:o.now??'시험용 결과를 받아 화면을 다시 확인하고 색 대비를 보는 중',
    answerable:o.question!==false,route_label:'route 1',route_count:1,route_total:1,
    routes:[{id:'test-work',title:'시험용 렌더 검증',kind:'working',state:'working',elapsed:'12m',shape:'staged',zone:'dispatch',role:'owner',kind_word:'code',kind_field:'code',intensity:'standard',kind_form:'team',
      stages:[{name:'prepare',mark:'✓'},{name:'execute',mark:'●',current:true},{name:'review',mark:''}],
      paint:{glyph:t('⠋','g_spin'),harness:t('claude','hb_claude'),model_text:t('Opus 5.5','hb_claude'),effort:t('xhigh'),ctx:t('52%','lvl_g'),gauge_row:context,gauge5:context}}],
    peers:o.peers===false?undefined:{groups:[{steward:{tag:'c6',key:'hb_claude',me:true},members:[{tag:'d2',key:'hb_codex'},{tag:'a7',key:'hb_opencode'}]}]},
    status_doc:o.lists===false?undefined:STATUS,backlog_doc:o.lists===false?undefined:o.backlog??BACKLOG,
    messages:[{way:'←',who:'[d2]',harness:'codex',when:'09:01',kind:'report',body:'시험용 검토 결과'},{way:'→',who:'[a7]',harness:'opencode',when:'09:05',kind:'report',body:'시험용 확인 요청'}],
    memory_items:[{kind:'decision',age:'3m',tier:'durable',scope:'project',text:'시험용 저장소 기억',body:''},{kind:'note',age:'8m',tier:'working',scope:'global',text:'시험용 전역 기억',body:''}],
    usage:o.usage===false?[]:[usage('claude',23,41),usage('codex',9,18),usage('opencode',13,28)],
    resources:o.resources===undefined?HOSTS:o.resources,no_color:o.noColor===true};
}
const brief=()=>({count:1,answerable:true,choices:2,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')],[t('1 ','dim'),t('가','loc_repo'),t(' · '),t('2 ','dim'),t('나','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')]],line:'시험용 질문'});
const draw=(o={})=>{const width=o.width??50,height=o.height??52;
  return view.render({width,height,side:true,fill:true,model:model(o),state:o.state??'working',harness:'claude',frame:o.frame??1000,since:990,brief:o.question===false?undefined:brief,sent:()=>[],
    cellWidth:cells,background:o.background??'dark',configuration:{theme:o.theme??'terminal',rule_motion:o.motion??'off',worker_box:'closed',side_width:width,position:'right'}});};
const lines=v=>v.rows.map(str);
const heads=ls=>ls.flatMap((l,i)=>l.startsWith('├╴[')?[{at:i,name:/^├╴\[([A-Z]+)\]/.exec(l)[1]}]:[]);
const gpuRows=ls=>{const a=ls.findIndex(l=>/^├╴\[RESOURCES\]/.test(l)),b=ls.findIndex(l=>/^├╴\[USAGE\]/.test(l));return ls.slice(a+1,b).filter(l=>!/^│ +│$/.test(l)).map(l=>l.slice(2,-2));};
"""


@unittest.skipUnless(NODE, "needs node")
class FourGroupsTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_four_heads_are_one_form_and_usage_is_the_third_without_resources(self):
        self.node(r"""
const v=draw(),ls=lines(v);
assert.deepEqual(heads(ls).map(h=>h.name),['REPO','SESSION','RESOURCES','USAGE'],ls.join('\n'));
for(const {at,name} of heads(ls)){
  // `├╴[NAME]`, the rule to the right frame `┤`, the same width as every row.
  assert(new RegExp('^├╴\\['+name+'\\](?: [^─]*)? ?─+┤$','u').test(ls[at]),ls[at]);
  const row=v.rows[at],word=row.find(p=>p.text==='['+name+']');
  assert(word&&word.role==='title'&&word.tier==='group'&&word.bold===true&&word.dim!==true,JSON.stringify(row));
  assert(row[0].text==='├'&&row[1].text==='╴'&&row.at(-1).text==='┤'&&[row[0],row[1],row.at(-1)].every(p=>p.role==='frame-state'),JSON.stringify(row.slice(0,3)));
  assert.equal(cells(ls[at]),50);
}
assert.match(ls[1],/^├╴\[REPO\] trial · wt 12 ─+┤$/);
// With no RESOURCES the USAGE head is the third; it stands after one empty row.
const no=lines(draw({resources:[]})),hs=heads(no);
assert.deepEqual(hs.map(h=>h.name),['REPO','SESSION','USAGE'],no.join('\n'));
assert(/^│ +│$/.test(no[hs[2].at-1]),no.join('\n'));
// The head's rule is the frame's own thin line; the group heads are the only rows that start with `├`.
assert.equal(ls.filter(l=>l.startsWith('├')).length,4);
""")

    def test_one_empty_row_before_each_head_and_these_go_first_short_of_rows(self):
        self.node(r"""
const blank=l=>/^│ +│$/.test(l);
// Tall enough: an empty row before each of SESSION, RESOURCES, USAGE (and none elsewhere inside the groups of this fixture).
const tall=lines(draw({height:70})),hs=heads(tall);
for(const h of hs.slice(1))assert(blank(tall[h.at-1]),h.name+'\n'+tall.join('\n'));
assert(!blank(tall[hs[0].at-1])&&!blank(tall[hs[0].at+1]),'no empty row beside the first head');
// Every empty row stands directly before a head or after the last row of the fill above MESSAGES; none sits between a head and its first row.
for(const h of hs)assert(!blank(tall[h.at+1]),h.name+' is followed by its first row');
const countOf=(ls,re)=>ls.filter(l=>re.test(l)).length;
// `gaps`: the heads with their empty row; `fill`: the other empty rows (the middle's).
const kinds=ls=>{const hs=heads(ls),before=new Set(hs.slice(1).filter(h=>blank(ls[h.at-1])).map(h=>h.at-1));
  return {gaps:hs.slice(1).filter(h=>blank(ls[h.at-1])).map(h=>h.name).join(','),heads:hs.length,fill:ls.filter((l,i)=>blank(l)&&!before.has(i)).length,
    backlog:countOf(ls,/^│   · FIX /),gpus:gpuRows(ls).filter(l=>/^ +\d /.test(l)).length,hosts:countOf(ls,/^│ (lab-[a-d]|testgpu) +│$/)};};
const base=kinds(lines(draw({height:70})));
assert.deepEqual([base.gaps,base.backlog,base.gpus,base.hosts],['SESSION,RESOURCES,USAGE',3,5,5]);
for(const o of [{},{lists:false},{resources:[]},{lists:false,resources:[]},{question:false}]){
  const at={};
  for(let h=70;h>=24;h--){
    const ls=lines(draw({...o,height:h})),k=at[h]=kinds(ls),tag=JSON.stringify(o)+' '+h+'\n'+ls.join('\n');
    // a panel with a row to spare has every empty row between the groups (none stands in the middle while one is short)
    if(k.fill>0)assert.equal(k.gaps.split(',').filter(Boolean).length,k.heads-1,tag);
    // while all three stand nothing else has been given up for the lists or RESOURCES
    if(k.gaps==='SESSION,RESOURCES,USAGE'){assert.equal(k.backlog,o.lists===false?0:3,tag);assert.equal(k.gpus,o.resources?0:5,tag);}
    if(ls.some(l=>/^│  BACKLOG/.test(l))){
      // the lists take the empty rows before RESOURCES and USAGE, then the BACKLOG items, then the one before SESSION (11-23: it outlives the BACKLOG items)
      if(k.backlog<3)assert(!/RESOURCES|USAGE/.test(k.gaps),tag);
      if(!k.gaps.startsWith('SESSION'))assert.equal(k.backlog,0,tag);
    }
  }
  // one at a time, USAGE's first, then RESOURCES', then SESSION's; then (without the lists) a GPU row
  const hs=Object.keys(at).map(Number).sort((a,b)=>b-a),full=hs.find(h=>at[h].fill===0);
  assert.equal(at[full].gaps,o.resources?'SESSION,USAGE':'SESSION,RESOURCES,USAGE',JSON.stringify(o));
  assert.equal(at[full-1].gaps,o.resources?'SESSION':'SESSION,RESOURCES',JSON.stringify(o));
  if(!o.resources)assert.equal(at[full-2].gaps,'SESSION',JSON.stringify(o));
  if(o.lists===false&&!o.resources){
    assert.deepEqual([at[full-3].gaps,at[full-3].gpus],['',5]);
    // a host's two rows go and one is used: the other goes back to the empty row before SESSION, not to the middle
    assert.deepEqual([at[full-4].gaps,at[full-4].hosts,at[full-4].fill],['SESSION',4,0]);
  }
}
// 50 x 52 (the draft's size): RESOURCES takes 11 rows; the two other empty rows and every BACKLOG item stand back, the one before SESSION stays; every GPU row is there.
const at52=lines(draw({height:52})),k52=kinds(at52);
assert.deepEqual([k52.gaps,k52.backlog,k52.gpus,k52.hosts,k52.fill],['SESSION',0,5,5,0],at52.join('\n'));
const s52=heads(at52).find(h=>h.name==='SESSION').at;assert(blank(at52[s52-1])&&/^│   ◇ 8m note/.test(at52[s52-2]),at52.join('\n'));
""")

    def test_a_list_head_is_bold_a_value_name_is_dim_and_mono_and_no_color_keep_the_weights(self):
        self.node(r"""
const find=(v,at,text)=>v.rows[at].find(p=>p.text.trim()===text);
for(const [theme,noColor] of [['terminal',false],['mono',false],['terminal',true]]){
  const v=draw({theme,noColor,height:70}),ls=lines(v);
  const row=re=>ls.findIndex(l=>re.test(l));
  for(const name of ['STATUS','BACKLOG','MEM','MESSAGES']){
    const p=find(v,row(new RegExp('^│  '+name)),name);
    assert(p&&p.bold===true&&p.dim!==true,theme+noColor+' '+name+JSON.stringify(p));
  }
  for(const name of ['GIT','MODEL','CTX','TITLE','PEERS','NOW']){
    const p=find(v,row(new RegExp('^│  '+name+' ')),name);
    assert(p&&p.dim===true&&p.bold!==true,theme+noColor+' '+name+JSON.stringify(p));
  }
  const g=find(v,row(/^├╴\[SESSION\]/),'[SESSION]');assert(g.bold===true&&g.dim!==true,JSON.stringify(g));
  // The body keeps neither: plain.
  const body=v.rows[row(/^│   · 시험용 작업 가/)].find(p=>p.text.includes('시험용 작업 가'));assert(body.bold!==true&&body.dim!==true,JSON.stringify(body));
}
// A known face tones them from the colour table: the group name the brightest, a list name the body's, a value's name stays dim.
for(const [background,bright,bodyTone] of [['dark',255,253],['light',232,237]]){
  const v=draw({background,height:70}),ls=lines(v);
  assert.equal(find(v,ls.findIndex(l=>/^├╴\[REPO\]/.test(l)),'[REPO]').foreground,bright);
  assert.equal(find(v,ls.findIndex(l=>/^│  STATUS/.test(l)),'STATUS').foreground,bodyTone);
}
""")

    def test_backlog_items_have_a_dot_and_the_head_counts_what_is_off_the_screen(self):
        self.node(r"""
const ls=lines(draw({height:70}));
const at=ls.findIndex(l=>/^│  BACKLOG/.test(l));
assert.match(ls[at],/^│  BACKLOG \+136\/139 · FIX 54 · IMPR 36 +› │$/);
for(let i=1;i<=3;i++)assert.match(ls[at+i],/^│   · FIX  시험용 /,ls.join('\n'));
const st=ls.findIndex(l=>/^│  STATUS/.test(l));assert.match(ls[st],/^│  STATUS \+2 +› │$/);
for(let i=1;i<=3;i++)assert.match(ls[st+i],/^│   · 시험용 작업 /);
// No bullet character before any title or list name.
assert(ls.every(l=>!/[▮▯▰▱‣◦]/u.test(l)));
// One fewer item shown: the number grows by one (`+137/139`).
const small=lines(draw({height:44})),sa=small.findIndex(l=>/^│  BACKLOG/.test(l));
const shown=small.slice(sa+1).filter(l=>/^│   · FIX /.test(l)).length;
assert.match(small[sa],new RegExp('^│  BACKLOG \\+'+(139-shown)+'/139 · FIX 54 · IMPR 36'),small.join('\n'));
// Narrow, the sorts' counts go first: `+136/139 FIX 54 IMPR 36`, then `+136/139`, then the word alone.
const heads2=w=>{const l=lines(draw({width:w,height:70}));return l[l.findIndex(x=>/^│  BACKLOG/.test(x))].replace(/^│  /,'').replace(/ *› │$/,'');};
assert.equal(heads2(50),'BACKLOG +136/139 · FIX 54 · IMPR 36');
assert.equal(heads2(40),'BACKLOG +136/139 FIX 54 IMPR 36');
assert.equal(heads2(34),'BACKLOG +136/139');
// The words are the product's own (FIX, IMPR); a document with no sorts keeps the old `+N`.
const plain=lines(draw({height:70,backlog:{word:'BACKLOG',has_doc:true,total:11,more:false,items:items('BACKLOG',['시험용 하나','시험용 둘','시험용 셋'])}}));
assert(plain.some(l=>/^│  BACKLOG \+8 +› │$/.test(l)),plain.join('\n'));
""")

    def test_peers_stands_under_title_and_mem_ends_repo_with_their_press_rows(self):
        self.node(r"""
const v=draw({height:70}),ls=lines(v);
const row=re=>ls.findIndex(l=>re.test(l));
const [git,status,backlog,mem,session,modelAt,ctx,title,peers,now]=[/^│  GIT /,/^│  STATUS/,/^│  BACKLOG/,/^│  MEM/,/^├╴\[SESSION\]/,/^│  MODEL/,/^│  CTX/,/^│  TITLE/,/^│  PEERS/,/^│  NOW/].map(row);
assert(git===2&&status>git&&backlog>status&&mem>backlog&&session>mem&&modelAt===session+1&&ctx===modelAt+1&&title===ctx+1&&peers===title+1&&now===peers+1,[git,status,backlog,mem,session,modelAt,ctx,title,peers,now]+'\n'+ls.join('\n'));
// MEM's head and its two records stand in REPO; an empty row parts them from the SESSION head.
assert.match(ls[mem+1],/^│   ◆ 3m decision /);assert.match(ls[mem+2],/^│   ◇ 8m note /);assert(/^│ +│$/.test(ls[session-1])&&session===mem+4,ls.join('\n'));
// The press rows follow the rows: MEM's head and each record, PEERS none, MESSAGES' rows.
const rowsOf=prefix=>v.hits.filter(h=>h.action.startsWith(prefix)).map(h=>h.row);
assert.deepEqual(rowsOf('popup:MEM'),[mem,mem+1,mem+2]);
assert(!v.hits.some(h=>h.row===peers),JSON.stringify(v.hits));
assert.deepEqual(rowsOf('popup:STATUS').slice(0,1),[status]);assert.deepEqual(rowsOf('popup:BACKLOG').slice(0,1),[backlog]);
assert(rowsOf('popup:NOW').every(r=>r>=now)&&rowsOf('popup:NOW').includes(now));
assert(rowsOf('popup:MESSAGES').length>=2&&Math.min(...rowsOf('popup:MESSAGES'))>Math.max(...rowsOf('popup:ROUTE')));
// MEM's items and the messages stand two cells in under their heads.
assert.match(ls[row(/^│   ← \[d2\]/)],/^│   ← \[d2\] codex/);
// A low panel: STATUS and BACKLOG fold into the `[REPO]` head's buttons with the documents' whole counts, MEM stays under it, the SESSION head takes MODEL's value.
const low=draw({height:24}),lo=lines(low);
assert.match(lo[1],/^├╴\[REPO\] trial +STATUS \+5 ›  BACKLOG \+139 ›─┤$/,lo.join('\n'));
const btn=low.hits.filter(h=>h.row===1);assert.deepEqual(btn.map(h=>h.action),['popup:STATUS','popup:BACKLOG']);assert(btn[0].right<btn[1].left);
assert(lo.some(l=>/^├╴\[SESSION\] Opus 5\.5 xhigh ─+┤$/.test(l)),lo.join('\n'));
assert(!lo.some(l=>/^│  MODEL/.test(l)));
const lm=lo.findIndex(l=>/^│  MEM/.test(l));assert(lm>1&&lm<lo.findIndex(l=>/^├╴\[SESSION\]/.test(l)),lo.join('\n'));
assert.equal(low.hits.filter(h=>h.action==='popup:MEM').map(h=>h.row)[0],lm);
""")

    def test_resources_a_host_name_then_one_row_of_two_gpus_in_columns_with_one_gauge_length(self):
        self.node(r"""
const ls=lines(draw()),rs=gpuRows(ls);
// 50 wide: the host's name alone, then both GPUs of the host on one row, the number alone, the gauge 4 cells and one cell between the memory and the sessions
// (11-25 fix: the memory and the sessions never touch; the gauge goes down instead).
assert.deepEqual(rs.map(l=>l.trimEnd()),[
 'lab-a',' 0  92%━━━─41/48G [c6]  1  48%━───15/48G [d2]',
 'lab-b',' 0 100%━━━─26/32G [c6]  1  66%━━──18/32G [a7]',
 'lab-c',' 0  78%━━━─17/24G other 1   0%──── 0/24G',
 'lab-d',' 0  32%━━── 6/12G [d2]  1   0%──── 0/12G',
 'testgpu',' 0  71%━━━─37/48G [a7]  1   0%──── 0/48G'],rs.join('\n'));
assert.equal(ls.find(l=>/^│  0  92%/.test(l)),'│  0  92%━━━─41/48G [c6]  1  48%━───15/48G [d2]  │');
// 52 to 57 wide: the 5-cell gauge and the cell; from 58: the spaced form; 44 to 49: no gauge, the cell kept.
for(const [width,first] of [[52,' 0  92%━━━━─41/48G [c6]  1  48%━━───15/48G [d2]'],[57,' 0  92%━━━━─41/48G [c6]  1  48%━━───15/48G [d2]'],
  [58,'  0  92% ━━━━─ 41/48G [c6]   1  48% ━━─── 15/48G [d2]'],[49,' 0  92% 41/48G [c6]  1  48% 15/48G [d2]'],[44,' 0  92% 41/48G [c6]  1  48% 15/48G [d2]']]){
  const r=gpuRows(lines(draw({width})));assert.equal(r[1].trimEnd(),first,width+'\n'+r.join('\n'));
  assert(r.every(l=>!/\dG(\[|other)/.test(l)),width+'\n'+r.join('\n'));
}
""")

    def test_resources_columns_line_up_across_hosts_and_every_gauge_has_one_length(self):
        self.node(r"""
for(const width of [59,50,44,30]){
  const v=draw({width,height:70}),ls=lines(v),rs=gpuRows(ls);
  assert(rs.length>=10,width);
  assert(!rs.some(l=>/CPU|MEM|GPU\d/.test(l)),'host rows are the name alone and the GPU is its number: '+width+'\n'+rs.join('\n'));
  const names=rs.filter(l=>/^(lab-|testgpu)/.test(l));assert.equal(names.length,5);assert(names.every(l=>/^[-\w]+ *$/.test(l)));
  const gpuLines=rs.filter(l=>/^ +\d /.test(l));
  // the gauge's cells: every gauge of the section is as long as the others
  const gauges=gpuLines.flatMap(l=>l.match(/[━─]+/gu)||[]);
  if(width===44){assert.equal(gauges.length,0,'no gauge at 44');}
  else{assert.equal(gauges.length,10,width);assert.equal(new Set(gauges.map(g=>g.length)).size,1,width+' '+gauges);
    assert.equal(gauges[0].length,width===50?4:5,width);}
  // the memory and the sessions never touch
  assert(gpuLines.every(l=>!/\dG(\[|other)/.test(l)),width+'\n'+gpuLines.join('\n'));
  // the same column starts on every row of every host: the use's `%` and the memory's `G` stand on one cell each
  const marks=(re)=>new Set(gpuLines.flatMap(l=>{const out=[];let m;const r=new RegExp(re,'gu');while((m=r.exec(l)))out.push(cells(l.slice(0,m.index+m[0].length)));return out;}));
  if(width!==30){
    assert.equal(marks('%').size,2,width+' % columns');
    const cols=new Set(gpuLines.map(l=>(l.match(/%/g)||[]).length));assert.deepEqual([...cols],[2]);
  }
  for(const l of ls)assert.equal(cells(l),width,l);
}
// 59: spaced; 44: no gauge; 30: one GPU to a row (a host is its name and two rows).
const w59=gpuRows(lines(draw({width:59,height:70})));assert.match(w59[1],/^ +0  92% ━━━━─ 41\/48G \[c6\] +1  48% ━━─── 15\/48G \[d2\]/);
const w44=gpuRows(lines(draw({width:44,height:70})));assert.match(w44[1],/^ +0  92% 41\/48G \[c6\] +1  48% 15\/48G \[d2\]/);
const w30=gpuRows(lines(draw({width:30,height:70})));assert.equal(w30.length,15,w30.join('\n'));assert.match(w30[1],/^ +0  92% ━━━━─ 41\/48G \[c6\]/);assert.match(w30[2],/^ +1  48% ━━─── 15\/48G \[d2\]/);
// An idle GPU is all dim (`0%`, `0/24G`, its empty gauge), a busy one's use keeps its level colour; `other` is dim.
const v=draw({height:70}),ls=lines(v),idle=v.rows[ls.findIndex(l=>/^│  0  78%/.test(l))];
const part=(row,pred)=>row.find(pred);
assert(part(idle,p=>p.text==='other').dim===true);
const idleRow=v.rows[ls.findIndex(l=>/^│  0  78%/.test(l))].filter(p=>/0%|0\/24G/.test(p.text));
const second=idleRow.filter(p=>p.text.trim()==='0%'||p.text.trim()==='0/24G');assert(second.length>=1&&second.every(p=>p.dim===true),JSON.stringify(idleRow));
assert.equal(part(v.rows[ls.findIndex(l=>/^│  0  92%/.test(l))],p=>p.text.trim()==='92%').foreground,217);
""")

    def test_a_shared_gpu_unfolds_its_own_host_and_no_gpu_reads_so(self):
        self.node(r"""
const withHolders=(h,i,ids)=>{const m=structuredClone(HOSTS);m[h].gpus[i].users=ids.flatMap((id,k)=>[...(k?[t(' ')]:[]),t('['+id+']','loc_repo')]);return m;};
const base=gpuRows(lines(draw({height:70})));
// Two sessions on lab-b's GPU 0: that host alone takes one GPU to a row; the others, their columns and every gauge length are as they were.
const shared=gpuRows(lines(draw({height:70,resources:withHolders(1,0,['c6','d2'])})));
assert.equal(shared.length,base.length+1,shared.join('\n'));
const i=shared.findIndex(l=>/^lab-b/.test(l));
assert.match(shared[i+1],/^ +0 100%━━━─26\/32G \[c6\] \[d2\] *$/);assert.match(shared[i+2],/^ +1  66%━━──18\/32G \[a7\] *$/);
for(const name of ['lab-a','lab-c','lab-d','testgpu']){const k=base.findIndex(l=>l.startsWith(name)),j=shared.findIndex(l=>l.startsWith(name));assert.equal(shared[j+1],base[k+1],name);}
assert.deepEqual(shared.flatMap(l=>l.match(/[━─]+/gu)||[]).map(g=>g.length),Array(10).fill(4));
// Three sessions on one GPU, and two GPUs of one host each shared: the same, only that host.
const three=gpuRows(lines(draw({height:70,resources:withHolders(1,0,['c6','d2','a7'])}))),j3=three.findIndex(l=>/^lab-b/.test(l));
assert.match(three[j3+1],/\[c6\] \[d2\] \[a7\]/);assert.equal(three.length,base.length+1);
const both=structuredClone(HOSTS);both[1].gpus[0].users=[t('[c6]'),t(' '),t('[d2]')];both[1].gpus[1].users=[t('[a7]'),t(' '),t('[d2]')];
const two=gpuRows(lines(draw({height:70,resources:both}))),j2=two.findIndex(l=>/^lab-b/.test(l));
assert.match(two[j2+1],/\[c6\] \[d2\]/);assert.match(two[j2+2],/\[a7\] \[d2\]/);assert.equal(two[j2+3].slice(0,3),'lab');
// A host that has no GPU reads `No GPU`; a GPU used with no known session reads `other`.
const odd=gpuRows(lines(draw({height:70,resources:[HOSTS[2],{host:[t('noaccel','loc_repo')],stats:[[t('CPU '),t('5%','loc_repo')]],gpus:[]}]})));
assert(odd.some(l=>/^noaccel +No GPU *$/.test(l)),odd.join('\n'));assert(odd.some(l=>/other/.test(l)));
const no=lines(draw({height:70,resources:[{host:[t('noaccel','loc_repo')],stats:[],gpus:[]}]}));
assert(no.some(l=>/^│ noaccel +No GPU +│$/.test(l)),no.join('\n'));
""")

    def test_a_card_that_overflows_its_own_row_keeps_every_session_number(self):
        self.node(r"""
// Two and three sessions on lab-b's GPU 0, 30 to 40 wide: every session number is on the rows, none cut; that card alone gives up its gauge, then its memory, then its use.
const withHolders=ids=>{const m=structuredClone(HOSTS);m[1].gpus[0].users=ids.flatMap((id,k)=>[...(k?[t(' ')]:[]),t('['+id+']','loc_repo')]);return m;};
for(const ids of [['c6','d2'],['c6','d2','a7']])for(let width=30;width<=40;width++){
  const ls=lines(draw({width,height:70,resources:withHolders(ids)})),rs=gpuRows(ls),tag=width+' '+ids+'\n'+rs.join('\n');
  const at=rs.findIndex(l=>/^lab-b/.test(l)),card=rs[at+1];
  for(const id of ids)assert(card.includes('['+id+']'),tag);
  assert(!rs.some(l=>l.includes('…')),tag);
  assert(rs.every(l=>!/\dG(\[|other)/.test(l)),tag);
  // the card keeps what fits, in this order of giving up: the gauge, the memory, the use; the number stays
  const has={gauge:/[━─]/.test(card),mem:/26\/32G/.test(card),pct:/100%/.test(card)};
  assert(!has.mem||has.pct,tag);assert(!has.gauge||has.mem,tag);assert(/^ +0 /.test(card),tag);
  // the host's other card and the other hosts keep the grid (the gauge where the section has one)
  assert.match(rs[at+2],/^ +1  66% ━━━── 18\/32G \[a7\] *$/,tag);
  for(const l of ls)assert.equal(cells(l),width,tag);
}
// 30 wide: two sessions give up the gauge, three the memory too.
const at30=ids=>{const rs=gpuRows(lines(draw({width:30,height:70,resources:withHolders(ids)})));return rs[rs.findIndex(l=>/^lab-b/.test(l))+1].trimEnd();};
assert.equal(at30(['c6','d2']),' 0 100% 26/32G [c6] [d2]');
assert.equal(at30(['c6','d2','a7']),' 0 100% [c6] [d2] [a7]');
// 34 wide: two sessions keep everything, three give up the gauge.
const at34=ids=>{const rs=gpuRows(lines(draw({width:34,height:70,resources:withHolders(ids)})));return rs[rs.findIndex(l=>/^lab-b/.test(l))+1].trimEnd();};
assert.equal(at34(['c6','d2']),' 0 100% ━━━━─ 26/32G [c6] [d2]');
assert.equal(at34(['c6','d2','a7']),' 0 100% 26/32G [c6] [d2] [a7]');
""")

    def test_short_of_rows_a_host_goes_whole_from_the_last_and_its_name_with_it(self):
        self.node(r"""
const kinds=ls=>({hosts:ls.filter(l=>/^│ (lab-[a-d]|testgpu) +│$/.test(l)).length,rows:gpuRows(ls).filter(l=>/^ +\d /.test(l)).length});
for(let h=60;h>=30;h--){
  const ls=lines(draw({height:h})),k=kinds(ls);
  assert.equal(k.hosts,k.rows,h+' a host name stands only over its rows\n'+ls.join('\n'));
  // the hosts that stand are the first ones, in order
  const names=ls.flatMap(l=>{const m=/^│ (lab-[a-d]|testgpu) +│$/.exec(l);return m?[m[1]]:[];});
  assert.deepEqual(names,['lab-a','lab-b','lab-c','lab-d','testgpu'].slice(0,names.length),h+' '+names);
  assert.equal(ls.length,h);
}
// 40 high: only the first hosts; 24 high: none (the group goes before any other row does).
const h40=kinds(lines(draw({height:40})));assert(h40.hosts>=1&&h40.hosts<5,JSON.stringify(h40));
assert.equal(kinds(lines(draw({height:24}))).hosts,0);
""")

    def test_every_row_of_every_size_is_as_wide_as_the_panel_and_the_head_cells_move_with_the_frame(self):
        self.node(r"""
let n=0;
for(const width of [30,32,34,40,44,50,59,68])for(const height of [24,30,40,52,62])for(const resources of [undefined,[]])for(const lists of [true,false])for(const question of [true,false]){
  const v=draw({width,height,resources,lists,question}),ls=lines(v);
  assert.equal(ls.length,height,[width,height]+'');
  assert.deepEqual(ls.map(cells).map((c,i)=>c===width?null:[i,c]).filter(Boolean),[],[width,height,resources&&'none',lists,question]+'\n'+ls.join('\n'));
  for(const h of v.hits)assert(h.row>=0&&h.row<height&&h.left>=0&&h.left<=h.right&&h.right<width,JSON.stringify(h));
  n++;
}
assert(n===320,String(n));
// Under `border` the head's `├`, `╴` and `┤` are cells of the moving frame: the orbit reaches them with the left and right frame's own phase.
const v=draw({motion:'border'}),ls=lines(v),hs=heads(ls).map(h=>h.at);
for(const frame of [1000,1011,1023,1040]){
  const f=draw({motion:'border',frame});
  for(const row of hs){
    const point=col=>f.motion.cells.findIndex(c=>c.row===row&&c.col===col);
    const after=f.motion.at(frame),found=[point(0),point(1),point(49)].map(i=>after[i]);
    assert.deepEqual(found.map(p=>p.text),['├','╴','┤']);
    const left=found[0],tick=found[1];assert.equal(JSON.stringify({...tick,text:''}),JSON.stringify({...left,text:''}),'the tick shares the left frame cell\'s phase');
  }
}
// Under `rule` the light runs over the SESSION head's rule alone, all of it: from after the name to the cell before `┤` (the right padding cell too).
for(const width of [30,50,59]){
  const r=draw({motion:'rule',width}),rl=lines(r),rule=r.motion.cells.filter(c=>c.rule!==undefined),at=heads(rl).find(h=>h.name==='SESSION').at;
  assert(rule.length>0&&new Set(rule.map(c=>c.row)).size===1&&rule[0].row===at);
  const run=/─+┤$/.exec(rl[at])[0].length-1;
  assert.deepEqual(rule.map(c=>c.col),Array.from({length:run},(_,i)=>width-1-run+i),width+' '+rl[at]);
  assert.deepEqual(rule.map(c=>c.rule),rule.map((_,i)=>i));
  // the first paint's rule cells are the same ones the motion moves
  let col=0;const painted=[];for(const p of r.rows[at]){const w=cells(p.text);if(p.rule)for(let x=0;x<w;x++)painted.push(col+x);col+=w;}
  assert.deepEqual(painted,rule.map(c=>c.col));
}
""")

    def test_the_bottom_layout_has_no_group_heads_and_no_empty_rows_between_groups(self):
        self.node(r"""
// Under the native area nothing of the four groups stands: its first row, the title and NOW row, `1 QUESTIONS`, the route box, `messages`, `USAGE` as before.
for(const [width,height] of [[120,9],[120,12],[100,14],[80,10]]){
  const v=view.render({width,height,side:false,fill:true,model:model({}),state:'working',harness:'claude',frame:1000,since:990,brief,sent:()=>[],cellWidth:cells,configuration:{theme:'terminal',rule_motion:'off',worker_box:'closed',side_width:width,position:'bottom'}}),ls=lines(v);
  assert.equal(ls.length,height);assert(ls.every(l=>cells(l)===width),ls.join('\n'));
  assert(!ls.some(l=>/^├╴\[|\[(REPO|SESSION|RESOURCES|USAGE)\]/.test(l)),ls.join('\n'));
  const u=ls.findIndex(l=>/^│ USAGE/.test(l));
  if(u>0)assert(!/^│ +│$/.test(ls[u-1]),'no empty row before USAGE: '+ls.join('\n'));
  assert(ls.some(l=>/│ 1 QUESTIONS|╭─ 1 QUESTIONS /.test(l)),ls.join('\n'));
  assert(!ls.some(l=>/QUESTIONS 1|ROUTE 1/.test(l)),ls.join('\n'));
}
""")


if __name__ == "__main__":
    unittest.main()
