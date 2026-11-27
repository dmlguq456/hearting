"""11-16 (panel usage 1116): the panel's bottom USAGE section, the red-bordered question box and its border blink,
the scanner's acceleration, the one last line in every harness, OpenCode's cost in its panel.

1. `USAGE ───` names the section the thin rule above the last row became; under it one row per harness (Claude,
   Codex, OpenCode, Fleet's header order): the harness dim, then its windows as the last row showed them (`?` when
   none is reported; 11-27: the three rows share one set of columns). The last of them stands on the last row with the gear and
   the fold button when it fits there.
   Short of rows the other harnesses' rows go (the last first), then the name row, MEM before them; this window's own
   harness stays. Every harness window shows the same three rows.
2. QUESTIONS is one thin rounded box (`╭─ N QUESTIONS › ─╮`), rose where this window has a question to answer, dim
   otherwise; the border blinks between the two reds, the letters inside do not; short of rows the border goes last.
3. The head rule's light slows toward the ends and runs fastest in the middle; the round trip takes as long as before.
4. A harness's own last line is the shell's one line: the working folder, `hearting v<version>`, a newer release's
   words in the version's place; OpenCode's cost stands beside its context in its own panel.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const shell=process.argv[1];
const view=require(path.join(shell,'panel-view.cjs')),display=require(path.join(shell,'native-display.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},lvl_r:{foreground:217,bold:true},
  g_blocked:{foreground:217},g_spin:{foreground:230},g_work:{foreground:150,bold:true},g_work_off:{foreground:150,dim:true},g_idle:{foreground:186},
  g_dead:{foreground:217,bold:true},hb_claude:{foreground:116},hb_codex:{foreground:176},hb_opencode:{foreground:147}};
for(const [i,c] of [147,116,150,186,176].entries()){styles['stg'+i+'_on']={foreground:c,bold:true};styles['stg'+i+'_off']={foreground:c,dim:true};}
const t=(text,key='dim')=>({text,...(styles[key]||{dim:key==='dim'})});
const USAGE=[
 {harness:'claude',parts:[t('5h '),t('12%','lvl_g'),t('(↻ 2h)'),t(' │ '),t('7d '),t('66%','lvl_y'),t('(↻ 5d)'),t(' │ '),t('fable ','dim'),t('3%','lvl_g')],columns:['period','period','model']},
 {harness:'codex',parts:[t('5h '),t('30%','lvl_g'),t('(↻ 1h)'),t(' │ '),t('7d '),t('8%','lvl_g'),t('(↻ 6d)')],columns:['period','period']},
 {harness:'opencode',parts:[],columns:[]}];   // (11-28: each window's kind, as the backend sends it)
function model(o={}){
  return {styles,colors:o.colors??256,no_color:o.no_color===true,tag:'[e9]',repo_side:[t('hearting','loc_repo')],git_side:[t('main'),t(' ✓','lvl_g')],
    selected:[t('Opus 5.5','hb_claude'),t(' xhigh')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],
    quota:USAGE[0].parts,usage:o.usage===null?undefined:o.usage||USAGE,session_title:'패널',now:'값 읽기',
    ...(o.question!==false?{wait:'◑답 대기 1'}:{}),route_label:'',route_count:0,route_total:0,routes:o.routes||[],
    messages:o.messages===false?[]:[{way:'←',who:'[73]',harness:'claude',when:'09:31',kind:'reply',text:'그림 두 장',body:'그림 두 장'}],
    memory_items:o.mem===false?[]:[{kind:'decision',age:'10m',text:'단추는 같은 자리',tier:'durable',scope:'project'}],...(o.model||{})};
}
const brief=()=>({count:1,answerable:true,choices:2,label:'QUESTIONS',
  rows:[[t('◑ ','g_blocked'),t('MESSAGES 몇 건까지 보일까요?','loc_repo')],[t('1 ','dim'),t('5건 (추천)','loc_repo'),t(' · ','dim'),t('2 ','dim'),t('3건','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('MESSAGES 몇 건까지 보일까요?','loc_repo')]],line:'MESSAGES 몇 건까지 보일까요?'});
const draw=(o={})=>view.render({width:o.width??50,height:o.height??46,side:o.side??true,model:model(o),state:o.state||'working',harness:o.harness||'claude',
  frame:o.frame??1000,since:o.since??990,brief:o.question===false?undefined:(o.brief||brief),sent:()=>o.sent||[],cellWidth:cells,
  configuration:{side_width:o.width??50,rule_motion:o.motion||'rule',theme:o.theme||'terminal',...(o.configuration||{})},background:o.background||''});
const lines=v=>v.rows.map(str);
const inside=v=>lines(v).map(l=>l.slice(2,-2));
const at=(v,head)=>inside(v).findIndex(l=>l.startsWith(head));
"""


class PanelUsage1116Test(unittest.TestCase):
    def node(self, body, *args, script=HEAD):
        result = subprocess.run(["node", "-e", script + body, str(SHELL), *args], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    # 1 ---------------------------------------------------------------------------------------------------------
    def test_usage_names_the_bottom_section_and_lists_the_three_harnesses(self):
        self.node(r"""
const v=draw(),L=inside(v),a=at(v,'USAGE');
assert(a>0,lines(v).join('\n'));
assert.match(L[a],/^USAGE +$/,L[a]);                         // the name alone (11-22: no thin line), no `›`
assert.deepEqual(L.slice(a+1,a+4).map(l=>l.trimEnd().split(/\s+/)[0]),['claude','codex','opencode'],L.slice(a).join('\n'));
// 11-27 ("USAGE 공통 열"): the windows the three rows report, one set of columns (the reset times go first at this width).
assert.match(L[a+1],/^claude   5h  12%  7d  66%  fable   3% *$/,L[a+1]);
assert.match(L[a+2].trimEnd(),/^codex    5h  30%  7d   8%  fable    \?$/,L[a+2]);
assert.match(L[a+3],/^opencode 5h    \?  7d    \?  fable    \? +⚙ +▾$/,L[a+3]);   // none reported: `?` dim in each column; the last row keeps the buttons
assert.equal(L.length-2,a+3,'the last row is the harness row');
// Looks: the name dim, the section name as MESSAGES' and MEM's (dim, title role), the line in the messages' line role.
const row=v.rows[a+1];assert.equal(row.find(p=>p.text.startsWith('claude')).role,'harness-name');
const head=v.rows[a+1-1];assert(head.some(p=>p.role==='title'&&p.text==='USAGE'),JSON.stringify(head));
assert(!head.some(p=>/─/.test(p.text)),'11-22: a section head beside the native area has no rule');
const q=v.rows[a+3].find(p=>p.text==='?');assert.equal(q.dim,true);
// The level colors are Fleet's.
assert.equal(row.find(p=>p.text==='66%').foreground,186);assert.equal(row.find(p=>p.text==='12%').foreground,150);
// No press on a usage row but the two buttons: the gear and the fold button keep their cells (right end, 3 apart).
const last=v.rows.length-2,pressed=v.hits.filter(h=>h.row===last);
assert.deepEqual(pressed.map(h=>h.action).sort(),['fold','settings']);
assert.equal(lines(v)[last].slice(-9),' ⚙   ▾ │'.padStart(9));
""")

    def test_the_last_harness_row_steps_aside_for_the_buttons_when_it_does_not_fit(self):
        self.node(r"""
// 11-27: the last row is laid out in the three rows' columns and is not fitted again beside the buttons: where it does not fit
// there as it is (20 cells at 30 wide), it keeps its own row and the buttons theirs. (A model window goes before 5h and 7d.)
const odd=[USAGE[0],USAGE[1],{harness:'opencode',parts:[t('fable-long-name '),t('100%','lvl_r')],columns:['model']}];
let v=draw({usage:odd,width:30}),a=at(v,'USAGE'),L=inside(v);
assert.equal(L[a+3].trimEnd(),'opencode 5h    ?  7d    ?',L.slice(a).join('\n'));
assert.match(L[a+4],/^ +⚙ +▾$/,L[a+4]);
assert.equal(L.length-2,a+4);
// Wider, the same row stands beside the buttons again.
v=draw({usage:odd,width:40});L=inside(v);a=at(v,'USAGE');
assert.match(L[a+3],/^opencode 5h    \?  7d    \? +⚙ +▾$/,L.slice(a).join('\n'));
// Nothing of any usage row is under the gear or the button, at any width; every row is the panel's width.
for(const width of [30,34,38,44,50,60]){
  v=draw({usage:odd,width,height:30});L=lines(v);const last=L.length-2;
  assert(/⚙ {3}▾ │$/.test(L[last]),L[last]);
  for(const [i,l] of L.entries())assert.equal(cells(l),width,i+': '+l);
}
""")

    def test_short_of_cells_each_row_gives_up_what_the_last_row_gave_up(self):
        self.node(r"""
const wide=[USAGE[0],USAGE[1],{...USAGE[0],harness:'opencode'}];
const rowOf=(width,name)=>{const v=draw({usage:wide,width,height:30});return inside(v).find(l=>l.startsWith(name)).replace(/ [⚙▾ ]+$/,'').trimEnd().replace(/ +/g,' ');};
// 11-27 ("USAGE 공통 열"): one set of columns for the three rows: short of cells the reset times go in every row together, then the
// model windows from the last; 5h and 7d stand side by side to the end; each value whole.
assert.equal(rowOf(60,'claude'),'claude 5h 12%(↻ 2h) 7d 66%(↻ 5d) fable 3%');
assert.equal(rowOf(50,'claude'),'claude 5h 12% 7d 66% fable 3%');
assert.equal(rowOf(50,'codex'),'codex 5h 30% 7d 8% fable ?');
assert.equal(rowOf(34,'claude'),'claude 5h 12% 7d 66%');
assert.equal(rowOf(30,'codex'),'codex 5h 30% 7d 8%');
// The last row is not fitted again beside the buttons: it keeps the columns of the rows above.
assert.equal(rowOf(30,'opencode'),'opencode 5h 12% 7d 66%');
assert.equal(rowOf(40,'opencode'),'opencode 5h 12% 7d 66%');
// Narrower than 5h and 7d need, each row is fitted on its own: the reset times, the model windows from the last, then (11-28)
// the windows of time from the last, as the backend names them (before, a label guess gave 5h up first: `claude 7d 66%`).
assert.equal(rowOf(24,'claude'),'claude 5h 12%');
""")

    def test_short_of_rows_the_other_harnesses_go_then_the_name_row_and_mem_goes_before_them(self):
        self.node(r"""
const names=['claude','codex','opencode'];
const present=(v)=>({head:at(v,'USAGE')>=0,lines:names.map(n=>inside(v).some(l=>l.startsWith(n+' ')||l.trimEnd()===n)),mem:at(v,'MEM')>=0});
for(const harness of names)for(let width=30;width<=60;width+=10)for(let height=6;height<=60;height++){
  const v=draw({harness,width,height}),p=present(v),L=lines(v);
  assert.equal(L.length,height);for(const l of L)assert.equal(cells(l),width,`${harness} ${width}x${height}: ${l}`);
  const own=names.indexOf(harness);
  // MEM keeps its place only while all of USAGE stands: it goes first.
  if(p.mem)assert(p.head&&p.lines.every(Boolean),`${harness} ${width}x${height} MEM kept, USAGE not whole`);
  // The other harnesses' rows go before the name row: while one of them stands, so does the name.
  if(p.lines.some((x,i)=>x&&i!==own))assert(p.head,`${harness} ${width}x${height} another harness's row without the name row`);
  // The other rows go the last first (the one nearest the buttons).
  const others=names.map((n,i)=>i).filter(i=>i!==own);
  if(!p.lines[others[0]])assert(!p.lines[others[1]],`${harness} ${width}x${height} order`);
  // This window's own row stays as long as any does.
  if(p.lines.some(Boolean))assert(p.lines[own],`${harness} ${width}x${height} own row gone before the others`);
  // From 20 rows up nothing of it gives way.
  if(height>=20)assert(p.head&&p.lines.every(Boolean),`${harness} ${width}x${height}`);
}
// A height short by one row shows what is left: the name row and this window's own row on the last row.
let low=6;while(!inside(draw({harness:'opencode',width:40,height:low})).some(l=>/^opencode/.test(l)))low++;
const L=inside(draw({harness:'opencode',width:40,height:low}));
assert(low<=14,String(low));assert(/^opencode/.test(L[L.length-2]),L.join('\n'));
assert(!L.some(l=>/^(claude|codex) /.test(l)),L.join('\n'));
""")

    def test_every_harness_window_shows_the_same_three_rows(self):
        self.node(r"""
const rows=harness=>{const v=draw({harness,width:50,height:46}),L=inside(v),a=at(v,'USAGE');
  return L.slice(a,a+4).map(l=>l.replace(/ +[⚙▾ ]+$/,'').trimEnd());};
const base=rows('claude');
assert.equal(base.length,4);
for(const harness of ['codex','opencode'])assert.deepEqual(rows(harness),base,harness);
// A value that changes shows at the next paint: the same rows from a model with other values.
const other=USAGE.map(u=>u.harness==='codex'?{...u,parts:[t('5h '),t('77%','lvl_y')],columns:['period']}:u);
const v=draw({usage:other});assert(inside(v).some(l=>/^codex +5h +77%  7d +\?  fable +\?$/.test(l.trimEnd())),lines(v).join('\n'));
// A model without `usage` (an older backend) keeps the one row of 5h/7d.
const old=draw({usage:null});assert(!inside(old).some(l=>l.startsWith('USAGE')));
assert(inside(old).some(l=>/^5h 12%\(↻ 2h\) │ 7d 66%/.test(l)));
""")

    def test_the_folded_strip_is_as_before(self):
        self.node(r"""
const v=view.render({width:140,height:3,side:false,folded:true,model:model(),state:'working',harness:'codex',frame:1,brief,sent:()=>[],cellWidth:cells,configuration:{}});
const s=str(v.rows[0]);
assert(!/USAGE|opencode|codex /.test(s),s);   // this window's 5h and 7d only (the model's `quota`)
assert(/5h 12%/.test(s),s);
""")

    # 2 ---------------------------------------------------------------------------------------------------------
    def test_the_question_is_a_rose_bordered_box_with_its_name_in_the_top_line(self):
        self.node(r"""
const v=draw(),L=inside(v),a=L.findIndex(l=>/^╭─ 1 QUESTIONS ─+ ›─╮$/.test(l));
assert(a>=0,lines(v).join('\n'));
const b=L.findIndex((l,i)=>i>a&&/^╰─+╯$/.test(l));
assert.equal(b,a+4,L.slice(a,b+1).join('\n'));               // the first row, the choices row and the action row inside
assert.match(L[a+1],/^│ ◑ MESSAGES 몇 건까지 보일까요\? +│$/);
assert.match(L[a+2],/^│ 1 5건 \(추천\) · 2 3건 +│$/);
assert.match(L[a+3],/^│ +↵ answer · Ctrl\+\] │$/);
for(const l of L.slice(a,b+1))assert.equal(cells(l),46,l);
// The line is the rose where a question is answered here; every cell of it (corners, rules, sides).
const rows=v.rows.slice(a,b+1);
const line=rows.flatMap(r=>r.filter(p=>/[╭╮╰╯│─]/.test(p.text)&&!/[A-Za-z0-9]/.test(p.text)&&p.role==='accent-line'));
assert(line.length>=8,JSON.stringify(rows[0]));
const top=v.rows[a];assert(top.some(p=>p.text==='╭'&&p.role==='accent-line'));assert(top.some(p=>p.text==='╮'&&p.role==='accent-line'));
const resolved=view.appearance([[top.find(p=>p.text==='╭')]],{},{colors:256,background:'dark'})[0][0];
assert.equal(resolved.foreground,view.FACES.dark.accent);
// The name: its count in the rose, the word dim as a section name, `›` dim; the whole top line opens the question popup.
assert(top.some(p=>p.role==='accent'&&p.text==='1'));assert(top.some(p=>p.role==='title'&&p.text==='QUESTIONS'));
for(let y=a;y<=b;y++)assert(v.hits.some(h=>h.row===y&&h.action==='question'&&h.left===2&&h.right===47),'row '+y);
// The letters inside keep their red and their order; no ground is painted.
// 11-16: the border alone is red; the question's words and its `◑` are the body color (253), not blinking.
const first=v.rows[a+1].find(p=>p.text.startsWith('MESSAGES'));assert.equal(first.foreground,253);assert(!first.role&&!first.blink&&first.dim===false);
assert.equal(v.rows[a+1].find(p=>p.text==='◑').foreground,253);
assert(v.rows[a+2].filter(p=>/건/.test(p.text)).every(p=>p.foreground===253));
assert(v.rows[a+2].filter(p=>/^\d $/.test(p.text)).every(p=>p.role==='accent'&&p.foreground===174));
assert(v.rows.flat().every(p=>p.background===undefined));
""")

    def test_a_question_answered_elsewhere_has_a_dim_line_and_no_question_no_box(self):
        self.node(r"""
const elsewhere=()=>({count:1,answerable:false,choices:0,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('다른 창에서 답할 질문','loc_repo')]],one:[[t('◑ ','g_blocked'),t('다른 창에서 답할 질문','loc_repo')]]});
let v=draw({brief:elsewhere}),L=inside(v),a=L.findIndex(l=>/^╭─ 1 QUESTIONS ─+ ›─╮$/.test(l));
assert(a>=0,lines(v).join('\n'));
const top=v.rows[a];
assert(top.every(p=>p.role!=='accent-line'),'dim line');assert(top.some(p=>p.text==='╭'&&p.role==='messages'&&p.dim===true));
assert(top.every(p=>!p.blink));
// A sent answer on its way is one dim line inside the same box.
v=draw({brief:elsewhere,sent:[[t('↗ 답을 보냈습니다','dim')]]});L=inside(v);
a=L.findIndex(l=>/^╭─ 1 QUESTIONS/.test(l));assert(L.slice(a,a+6).some(l=>/답을 보냈습니다/.test(l)));
// Nothing to ask: no box at all.
v=draw({question:false});assert(!inside(v).some(l=>/QUESTIONS|^[╭╰]/.test(l.replace(/^╭─ .*ROUTE.*/,''))),lines(v).join('\n'));
// Mono and eight colors: the line keeps its weight (the question's undimmed, the other dim); NO_COLOR too.
for(const o of [{theme:'mono'},{model:{colors:8}},{model:{no_color:true}}]){
  v=draw(o);L=inside(v);a=L.findIndex(l=>/^╭─ 1 QUESTIONS/.test(l));assert(a>=0,JSON.stringify(o));
  assert(v.rows[a].every(p=>!p.blink),JSON.stringify(o));
  assert(v.rows[a].filter(p=>/[╭─╮]/.test(p.text)).every(p=>p.dim!==true),JSON.stringify(o)+' '+JSON.stringify(v.rows[a]));
}
""")

    def test_the_boxs_rows_are_laid_out_at_its_inner_width_and_never_cut_by_the_border(self):
        self.node(r"""
const long=()=>({count:2,answerable:true,choices:5,label:'QUESTIONS',
  rows:[[t('◑ ','g_blocked'),t('이 아주 긴 질문의 첫 줄은 상자 안쪽 폭보다 훨씬 길어서 다시 감겨야 합니다 this first line is much longer than the box','loc_repo')],
   [t('1 ','dim'),t('하나','loc_repo'),t(' · ','dim'),t('2 ','dim'),t('둘','loc_repo'),t(' · ','dim'),t('3 ','dim'),t('셋','loc_repo'),t(' · ','dim'),t('4 ','dim'),t('넷','loc_repo'),t(' · ','dim'),t('5 ','dim'),t('다섯','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('이 아주 긴 질문의 첫 줄은','loc_repo')]]});
for(let width=30;width<=60;width++)for(const height of [60,46,30]){
  const v=draw({width,height,brief:long}),L=lines(v),a=inside(v).findIndex(l=>/^╭─ /.test(l)&&/QUESTIONS/.test(l));
  assert(a>=0,`${width}x${height}`);
  for(const l of L)assert.equal(cells(l),width,`${width}x${height}: ${l}`);
  let b=a+1;while(b<L.length&&!/^│ ╰/.test(L[b]))b++;
  for(let y=a+1;y<b;y++)assert(/^│ │ .* │ │$/.test(L[y])||/^│ │.*│ │$/.test(L[y]),`${width}x${height}: ${L[y]}`);   // every inside row ends before the right side
  // The first choice is always there, the first row ends in `…` once, never `……`.
  assert(!L.join('\n').includes('……'),`${width}x${height}`);
  assert(/1 하나|1 /.test(L[a+2]),`${width}x${height} choices: ${L[a+2]}`);
}
""")

    def test_short_of_rows_the_border_goes_after_the_content_and_the_section_reads_as_before(self):
        self.node(r"""
const shape=h=>{const v=draw({height:h,width:50});const L=inside(v);
  const boxed=L.some(l=>/^╭─ .*QUESTIONS/.test(l)),plain=L.some(l=>/^1 QUESTIONS +›$/.test(l));return {boxed,plain,L};};
let seen={boxed:false,plain:false};
for(let h=8;h<=60;h++){const s=shape(h);
  assert(!(s.boxed&&s.plain),'one form at a time '+h);
  // The head and the first row of the question stay at every height from 8 rows.
  assert(s.boxed||s.plain,`${h}\n${s.L.join('\n')}`);
  assert(s.L.some(l=>/MESSAGES 몇 건/.test(l)),String(h));
  seen[s.boxed?'boxed':'plain']=true;
}
assert(seen.boxed&&seen.plain);
// One or two rows for the section (a window of 8 rows): the plain head and its first row.
const s=shape(8);assert(s.plain,s.L.join('\n'));
""")

    # 3 ---------------------------------------------------------------------------------------------------------
    def test_the_border_blinks_between_the_two_reds_on_the_one_second_beat_and_only_its_cells_change(self):
        self.node(r"""
const v=draw({motion:'rule'}),F=view.FACES.dark.waitBlink;
const blinkCells=v.motion.cells.map((c,i)=>[c,i]).filter(([c])=>c.blink);
assert(blinkCells.length>=30,String(blinkCells.length));
// Only the box's own lines blink (their glyphs), never a letter or a space.
for(const [c] of blinkCells)assert(/^[╭╮╰╯│─]$/.test(c.ch),c.ch);
const colors=tick=>blinkCells.map(([,i])=>v.motion.at(tick)[i].foreground);
for(const tick of [1000,1004,1009])assert(colors(tick).every(c=>c===F[0]),'lit '+tick);
for(const tick of [1010,1015,1019])assert(colors(tick).every(c=>c===F[1]),'other '+tick);
assert(colors(1020).every(c=>c===F[0]));
// The two reds are far enough apart to see (L* gap) and the same as the polish's values.
assert.deepEqual(F,[217,95]);
// A different tick changes the blink cells only: the other moving cells follow their own motion.
const same=v.motion.cells.map((c,i)=>[c,i]).filter(([c])=>!c.blink&&c.rule===undefined);
assert.equal(same.length,v.motion.cells.length-blinkCells.length-v.motion.cells.filter(c=>c.rule!==undefined).length);
// Its next change is the beat.
const stopped=draw({motion:'off',state:'idle'});assert(!stopped.motion||!stopped.motion.cells.some(c=>c.blink),'off');
// The letters inside do not blink; a question answered elsewhere keeps the letters' blink (`◑` and its count).
const elsewhere=()=>({count:1,answerable:false,choices:0,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('다른 곳','loc_repo')]],one:[[t('◑ ','g_blocked'),t('다른 곳','loc_repo')]]});
const e=draw({brief:elsewhere});assert(e.motion.cells.filter(c=>c.blink).some(c=>c.ch==='◑'),'the ◑ blinks');
assert(!e.motion.cells.filter(c=>c.blink).some(c=>/[╭╮╰╯│]/.test(c.ch)),'a dim line does not blink');
""")

    def test_nothing_blinks_with_motion_off_mono_eight_colors_no_color_or_nothing_waiting(self):
        self.node(r"""
const none=o=>{const v=draw(o);return !v.motion||!v.motion.cells.some(c=>c.blink);};
assert(none({motion:'off'}));assert(none({theme:'mono'}));assert(none({model:{colors:8}}));assert(none({model:{no_color:true}}));
assert(none({question:false}));
assert(!none({}));
// No question waiting: no tick of its own either (the host keeps its beat only while a blink is painted).
const v=draw({question:false,state:'idle'});assert(!v.blinks&&!v.animated);
""")

    # 4 ---------------------------------------------------------------------------------------------------------
    def test_the_scanner_slows_at_the_ends_and_runs_fastest_in_the_middle(self):
        self.node(r"""
const n=46,R=view.RULE;
const trip=[];for(let f=0;f<R.SCAN;f++)trip.push(view.scanAt(f,n));
// The same round trip: lit for the same SCAN frames, resting REST frames, out at one end and back.
assert.equal(R.SCAN,26);assert.equal(R.REST,10);
assert.equal(view.scanAt(R.SCAN,n),null);assert.notEqual(view.scanAt(R.SCAN-1,n),null);
const cycle=R.SCAN+R.REST-1;assert.equal(view.scanAt(cycle,n),trip[0]);
// Cells moved at each beat: small near the turns, large in the middle (a cosine, not a constant).
const step=trip.slice(1).map((x,i)=>Math.abs(x-trip[i]));
const half=R.SCAN/2;
const ends=Math.max(step[0],step.at(-1),step[half-1],step[half]),middle=Math.max(...step);
assert(ends<=2,`ends ${ends} ${step}`);assert(middle>=ends*3,`middle ${middle} ends ${ends} ${step}`);
// Going out then coming back, never past the far end, and it turns once.
const peak=Math.max(...trip),turn=trip.indexOf(peak);
for(let i=1;i<=turn;i++)assert(trip[i]>=trip[i-1],'out '+trip);
for(let i=turn+1;i<trip.length;i++)assert(trip[i]<=trip[i-1],'back '+trip);
assert(Math.abs(turn-(R.SCAN/2))<=1,String(turn));
// The beat's writes do not grow: the cells that change per beat in a whole round trip are at most as many as before
// (the old steady speed changed 2·(n+2·reach)/SCAN cells at each beat; the cosine's fastest beat stays within 2x of the old one).
const reach=R.FADE+(R.PEAK-1)/2,lo=-(R.FADE+(R.PEAK-1)/2)-1,hi=n+R.FADE+(R.PEAK-1)/2;
const levelAt=(c,i)=>Math.max(0,R.FADE+1-Math.max(0,Math.abs(i-c)-(R.PEAK-1)/2));
const changed=path=>path.slice(1).reduce((sum,c,k)=>sum+Array.from({length:n},(_,i)=>levelAt(c,i)!==levelAt(path[k],i)?1:0).reduce((a,b)=>a+b,0),0);
const steady=Array.from({length:R.SCAN},(_,q)=>Math.round(lo+(q<R.SCAN/2?q/(R.SCAN/2):2-q/(R.SCAN/2))*(hi-lo)));
// Cells written over one round trip: no more than at the steady speed; the busiest beat at most a few cells more.
assert(changed(trip)<=changed(steady),`${changed(trip)} vs ${changed(steady)}`);
const busiest=path=>Math.max(...path.slice(1).map((c,k)=>Array.from({length:n},(_,i)=>levelAt(c,i)!==levelAt(path[k],i)?1:0).reduce((a,b)=>a+b,0)));
assert(busiest(trip)<=busiest(steady)*2,`${busiest(trip)} vs ${busiest(steady)}`);
// Short rule, long rule.
for(const w of [20,46,90]){const a=view.scanAt(0,w),b=view.scanAt(R.SCAN/2,w);assert(a<0&&b>=w,`${w} ${a} ${b}`);}
// `rule motion` off: no cell moves.
assert(!draw({motion:'off'}).motion||!draw({motion:'off'}).motion.cells.some(c=>c.rule!==undefined));
const running=draw({motion:'rule'});assert(running.motion.cells.some(c=>c.rule!==undefined));
""")

    # 5 ---------------------------------------------------------------------------------------------------------
    def test_a_harness_own_last_line_is_found_on_its_native_screen(self):
        self.node(r"""
const uuid='019e1a2b-1111-2222-3333-444444444444';
// Codex: the footer row above its help row, whatever it holds; the composer, a menu or other rows are not it.
assert.deepEqual(display.footerRows('codex',['', '› hi','',`  ${uuid} · gpt-5 xhigh · Context 5% used`,'  ? for shortcuts']),[3]);
assert.deepEqual(display.footerRows('codex',['› hi','  gpt-5 xhigh · ~/proj','  ? for shortcuts']),[1]);          // before a thread exists: no id
assert.deepEqual(display.footerRows('codex',['› draft',`  ${uuid} · gpt-5`]),[1]);                              // a draft: no help row, the id row
assert.deepEqual(display.footerRows('codex',['› draft','  just text']),[]);
assert.deepEqual(display.footerRows('claude',['a','b']),[]);
// OpenCode: its bottom bar among the last three rows.
assert.deepEqual(display.footerRows('opencode',['  ┃ prompt text','','  12.3K (4%) $0.10 ctrl+p commands']),[2]);
assert.deepEqual(display.footerRows('opencode',['┃ prompt','~/proj/hearting:main      1 MCP  /status','  esc interrupt   ctrl+p commands']),[1,2]);
assert.deepEqual(display.footerRows('opencode',['a','b','c']),[]);
// The shell reads the identity from the native lines it holds, never from the painted line: the function is pure.
const screen=['› hi',`  ${uuid} · gpt-5 · Context 5% used`,'  ? for shortcuts'],copy=screen.slice();
display.footerRows('codex',screen);assert.deepEqual(screen,copy);
assert.equal(display.observe('codex',screen,uuid).sid,uuid);
""")

    def test_the_last_line_is_the_folder_and_the_release_and_a_newer_release_takes_the_right_text(self):
        self.node(r"""
const fs=require('node:fs'),vm=require('node:vm');
const text=fs.readFileSync(path.join(shell,'host.cjs'),'utf8');
const from=text.indexOf('// The release this window runs');assert(from>0);
const code=text.slice(from,text.indexOf('let claudeStatus=',from));
const run=(model,release)=>{const c={cellWidth:s=>cells(s),panelModel:model,myRelease:release};vm.createContext(c);vm.runInContext(code,c);return c;};
const strip=s=>s.replace(/\x1b\[[0-9;]*m/g,'');
const REL='/home/u/.local/share/hearting/releases/v4.40.0/utilities/session-shell/host.cjs';
let c=run({footer_path:'~/work/hearting'},REL),line=strip(c.footerLine(60));
assert.equal(cells(line),60);assert(line.startsWith('  ~/work/hearting'),line);assert(line.trimEnd().endsWith('hearting v4.40.0'),line);
assert(c.footerLine(60).includes('\x1b[2m'),'dim');
// The folder is cut from the front, `…` first; the version stays; very narrow, the version alone.
line=strip(run({footer_path:'~/a/very/long/path/to/the/project/folder'},REL).footerLine(40));
assert.equal(cells(line),40);assert(/^  …\S+ +hearting v4\.40\.0 $/.test(line)&&line.includes('folder'),line);
line=strip(run({footer_path:'~/work/hearting'},REL).footerLine(22));assert.equal(cells(line),22);assert(line.trimEnd().endsWith('hearting v4.40.0'),line);
// A newer release waiting: its words take the version's place.
line=strip(run({footer_path:'~/work/hearting',footer_notice:'새 판 대기'},REL).footerLine(60));
assert(line.trimEnd().endsWith('새 판 대기')&&!line.includes('hearting v'),line);
// A source checkout has no release folder: `hearting` alone.
line=strip(run({footer_path:'~/x'},'/home/u/src/hearting/utilities/session-shell/host.cjs').footerLine(40));assert(line.trimEnd().endsWith('hearting'),line);
assert.equal(cells(strip(run({},REL).footerLine(30))),30);
""")

    def test_the_backend_sends_the_folder_the_notice_the_cost_and_every_harness_usage(self):
        import session_shell
        from fleet import session_panel
        home = os.path.expanduser("~")
        self.assertEqual(session_shell._footer_path(home + "/work/hearting"), "~/work/hearting")
        self.assertEqual(session_shell._footer_path(home), "~")
        self.assertEqual(session_shell._footer_path("/srv/data/x"), "/srv/data/x")
        self.assertEqual(session_shell._footer_path(None), "")
        # The usage rows: this window's harness from its observed values, the others from the records Fleet keeps.
        own = {"quota_windows": [("5h", 12, None), ("7d", 66, None)], "weekly_pct": 66, "model_quotas": []}
        payloads = {"codex": {"rl_5h": 30, "rl_7d": 8}, "opencode": {}}
        from fleet.collectors import usage_cache
        with mock.patch.object(usage_cache, "read", side_effect=lambda name, *a, **k: {"payload": payloads.get(name, {})}) as read:
            rows = session_panel.usage_rows("claude", own)
            self.assertEqual([row["harness"] for row in rows], ["claude", "codex", "opencode"])
            self.assertEqual([call.args[0] for call in read.call_args_list], ["codex", "opencode"])   # claude's is not read twice
            text = lambda row: "".join(part[0] for part in row["parts"])
            self.assertTrue(text(rows[0]).startswith("5h 12%"), text(rows[0]))
            self.assertIn("30%", text(rows[1]))
            self.assertEqual(rows[2]["parts"], [])
            rows = session_panel.usage_rows("opencode", {"quota_windows": [("5h", 5, None)], "model_quotas": []})
            self.assertEqual([call.args[0] for call in read.call_args_list][2:], ["claude", "codex"])
        with mock.patch.object(usage_cache, "read", side_effect=RuntimeError("unreadable")):
            self.assertEqual([r["parts"] for r in session_panel.usage_rows("codex", own)][0::2], [[], []])
        # A change to any of the three records redraws the panel: all three are in what the shell watches.
        from fleet import session_view
        source = (ROOT / "tools/fleet/session_view.py").read_text()
        self.assertIn('usage_cache._path(name) for name in ("claude", "codex", "opencode")', source)
        # OpenCode's cost stands in its panel only.
        base = {"harness": "opencode", "cost": 1.234}
        panel = session_panel.presentation(dict(base), {}, "opencode", "s", 40)
        self.assertEqual(panel["cost"], [("$1.23", "loc_repo")])
        for name, cost in (("claude", 1.234), ("opencode", 0), ("opencode", None)):
            self.assertEqual(session_panel.presentation({"harness": name, "cost": cost}, {}, name, "s", 40)["cost"], [])

    def test_opencode_cost_stands_beside_its_context_in_its_panel(self):
        self.node(r"""
const v=draw({model:{cost:[t('$1.23','loc_repo')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')]}});
assert(inside(v).some(l=>/^ CTX +━━━━──── 41% │ \$1\.23$/.test(l.trimEnd())),lines(v).join('\n'));
const none=draw();assert(!lines(none).join('\n').includes('$'));
""")

    def test_a_route_the_session_does_itself_stands_in_the_route_box(self):
        self.node(r"""
const direct={id:'rt-d',title:'작은 수정',kind:'working',state:'working',shape:'직접',kind_word:'code',elapsed:'3m',role:'',
  paint:{glyph:t('⠏','g_spin')},stages:[]};
const v=draw({routes:[direct],model:{route_label:'route 1 ⠏1',route_count:1,route_total:1}}),L=inside(v);
const a=L.findIndex(l=>/^╭─ 1 ROUTE ─+ ›─╮$/.test(l));assert(a>=0,L.join('\n'));
const box=L.slice(a,L.findIndex((l,i)=>i>a&&/^╰/.test(l))+1).join('\n');
// (11-17, route row 5: the first line names the kind with its intensity in brackets, the title two cells in.)
const rows=box.split('\n');
// (11-17, route zones: a route with no zone is a `dispatch` one, its rule the box's first row.)
assert(/^╭─ 1 ROUTE/.test(rows[0])&&/^│ ─ dispatch ─+ │$/.test(rows[1])&&/^│ ● code\(직접\) +3m │$/.test(rows[2])&&/^│   작은 수정 +│$/.test(rows[3])&&!/code · 직접/.test(box),box);
// In the `direct` zone the zone's name says it: the word `(직접)` (the shape's) is left out of the kind.
const own=draw({routes:[{...direct,zone:'direct'}],model:{route_label:'route 1 ⠏1',route_count:1,route_total:1}}),M=inside(own),c=M.findIndex(l=>/^╭─ 1 ROUTE ─+ ›─╮$/.test(l)),
 zoned=M.slice(c,M.findIndex((l,i)=>i>c&&/^╰/.test(l))+1);
assert(/^│ ─ direct ─+ │$/.test(zoned[1])&&/^│ ● code +3m │$/.test(zoned[2])&&!zoned.join('\n').includes('직접')&&!zoned.some(l=>/dispatch/.test(l)),zoned.join('\n'));
""")


    # 6 (corrected 11-16 12:57) ---------------------------------------------------------------------------------
    def test_only_the_box_line_is_red_and_the_words_inside_are_the_body_color(self):
        self.node(r"""
const F=view.FACES;
for(const [opt,body,rose] of [[{},F.dark.body===253?253:253,F.dark.accent],[{light:true,theme:'light'},237,F.light.accent]]){
  const v=draw({...opt,background:opt.light?'light':'dark'}),L=inside(v),a=L.findIndex(l=>/^╭─ 1 QUESTIONS/.test(l));
  const top=v.rows[a];
  const painted=p=>view.appearance([[p]],{theme:opt.theme||'terminal'},{colors:256,background:opt.light?'light':'dark'})[0][0];
  // The border: every line cell the rose (both faces).
  assert(top.filter(p=>/[╭─╮]/.test(p.text)&&p.role==='accent-line').every(p=>painted(p).foreground===rose));
  // The words and the choices' row in the box: the body color and nothing else red.
  const pick=y=>v.rows[y].filter(p=>p.text.trim()&&!/^[│ ]+$/.test(p.text));
  const words=pick(a+1).concat(pick(a+2).filter(p=>!/ · |\(추천\)|^\d+ $/.test(p.text)));
  assert(words.length>=3);
  for(const p of words)assert.equal(painted(p).foreground,body,JSON.stringify(p));
  // 11-17: the choices' numbers are the question's rose, inside the box too.
  const numbers=pick(a+2).filter(p=>/^\d+ $/.test(p.text));
  assert(numbers.length>=2);
  for(const p of numbers)assert.equal(painted(p).foreground,rose,JSON.stringify(p));
  // The rose is left on `↵ answer · Ctrl+]` (as it was; only the words became the body color) and on the count in the label.
  assert(pick(a+3).every(p=>p.role==='accent'),JSON.stringify(pick(a+3)));
}
// Folded back to the plain section the letters are red again; the folded strip's `◑답 대기 N` is rose and blinks as before.
const plain=draw({height:18}),L=inside(plain),i=L.findIndex(l=>/^◑ MESSAGES/.test(l));
assert(i>0&&plain.rows[i].find(p=>p.text.startsWith('MESSAGES')).role==='accent');
const strip=view.render({width:140,height:3,side:false,folded:true,model:model(),state:'working',harness:'claude',frame:1,brief,sent:()=>[],cellWidth:cells,configuration:{}});
assert(strip.rows[0].find(p=>p.text.startsWith('◑'))&&strip.rows[0].find(p=>p.text.startsWith('◑')).role==='accent');
// A question answered elsewhere keeps its `◑` and count in the rose.
const elsewhere=()=>({count:1,answerable:false,choices:0,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('다른 곳','loc_repo')]],one:[[t('◑ ','g_blocked'),t('다른 곳','loc_repo')]]});
const e=draw({brief:elsewhere}),k=inside(e).findIndex(l=>/^╭─ 1 QUESTIONS/.test(l));
assert.equal(e.rows[k+1].find(p=>p.text==='◑').role,'accent');
// mono / eight colors / NO_COLOR: no color on the letters, the weight of the border tells.
for(const o of [{theme:'mono'},{model:{colors:8}}]){
  const m=draw(o),y=inside(m).findIndex(l=>/^╭─ 1 QUESTIONS/.test(l));
  assert(m.rows[y+1].filter(p=>/MESSAGES/.test(p.text)).every(p=>p.bold!==true&&p.role!=='accent'),JSON.stringify(o));
}
""")

    # 2 (corrected 11-16 12:57) ---------------------------------------------------------------------------------
    def test_the_usage_rows_name_each_harness_in_its_hue_one_step_down(self):
        self.node(r"""
const F=view.FACES,hb={claude:173,codex:147,opencode:153};   // 11-18: the harness hues: Claude orange 173, Codex lavender 147, OpenCode sky 153
const L=(n)=>{const c=[0,95,135,175,215,255],m=n-16,rgb=[c[Math.floor(m/36)],c[Math.floor(m/6)%6],c[m%6]].map(x=>x/255).map(x=>x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4);
  const y=0.2126*rgb[0]+0.7152*rgb[1]+0.0722*rgb[2];return 116*Math.cbrt(y)-16;};
const none=[USAGE[0],{harness:'codex',parts:[],columns:[]},{harness:'opencode',parts:[],columns:[]}];
const nameOf=(v,name)=>v.rows.flat().find(p=>p.role==='harness-name'&&p.text===name);
for(const [opt,table,face] of [[{},{claude:130,codex:104,opencode:110},'dark'],[{light:true,theme:'light'},{claude:166,codex:68,opencode:31},'light']]){
  assert.deepEqual(F[face].harnessName,table);
  for(const usage of [USAGE,none]){
    const v=draw({...opt,background:face,usage});
    for(const n of ['claude','codex','opencode']){const p=nameOf(v,n);assert(p,n);assert.equal(p.role,'harness-name');assert.equal(p.foreground,table[n],`${face} ${n}`);assert.equal(p.dim,false);}
  }
}
// One step down from the Fleet hue the head's own name uses (hb_*): a lower L* on the dark face, and a quieter value than the numbers.
for(const n of ['claude','codex','opencode'])assert(L(F.dark.harnessName[n])<L(hb[n])-8,n);
// The same hue family: claude orange, codex lavender (blue) and opencode sky (cyan-blue; a 13-step CIEDE2000 gap apart, 104 and 110).
const hueOf=n=>{const c=[0,95,135,175,215,255],m=n-16,[r,g,b]=[c[Math.floor(m/36)],c[Math.floor(m/6)%6],c[m%6]];return r>g&&g>=b&&r-b>=95?'k':r>g&&b>g&&Math.abs(r-b)<=40?'m':g>=r&&b>=r&&Math.abs(g-b)<=40?'c':b>r&&b>g?'l':'?';};
assert.deepEqual(['claude','codex','opencode'].map(n=>hueOf(F.dark.harnessName[n])),['k','l','c']);
// Mono, eight colors: no color; NO_COLOR: the style the rows were given (no hue).
for(const o of [{theme:'mono'},{model:{colors:8}},{model:{no_color:true}}]){
  const p=nameOf(draw(o),'codex');assert(p&&!Number.isInteger(p.foreground),JSON.stringify([o,p]));
}
""")

    # 12 --------------------------------------------------------------------------------------------------------
    def test_an_expanded_stage_names_wears_the_color_fleet_gives_that_stage(self):
        self.node(r"""
const stages=[['plan','✓','stg0_off'],['plan-check','✓','stg1_off'],['execute','●','stg2_on'],['impl-review','','stg3_off'],['test','','stg4_off'],['report','','stg0_off']]
  .map(([name,mark,key])=>({name,mark,key,current:mark==='●'}));
const worker={glyph:'⠏',kind:'working',role:'worker',stage:'execute',stage_key:'stg2_on',tool:'Edit',target:'/a/b/panel-view.cjs',elapsed:'3m',
  paint:{glyph:t('⠏','g_spin'),harness:t('claude','hb_claude'),model_text:t('Sonnet 5.5','hb_claude'),effort:t('high','dim'),ctx:t('40%','lvl_g'),gauge5:[t('━━','lvl_g'),t('───'),t(' 40%')]}};
const route={id:'rt-s',title:'패널 손질',kind:'working',state:'working',shape:'staged',kind_word:'code',elapsed:'42m',role:'owner',stages,
  paint:{glyph:t('⠏','g_spin'),harness:t('codex','hb_codex'),model_text:t('GPT 5.5','hb_codex'),effort:t('xhigh','dim'),ctx:t('52%','lvl_g'),gauge5:[t('━━━','lvl_g'),t('──'),t(' 52%')]},
  owner_paint:{glyph:t('⠏','g_spin'),harness:t('codex','hb_codex'),model_text:t('GPT 5.5','hb_codex'),effort:t('xhigh','dim'),ctx:t('52%','lvl_g'),gauge5:[t('━━━','lvl_g'),t('──'),t(' 52%')]},
  workers_live:[worker]};
const opt={routes:[route],model:{route_label:'route 1 ⠏1',route_count:1,route_total:1},question:false};
const v=draw({...opt,height:60});
const L=lines(v);
// 11-17 (route row 5, the two-row session lines): a worker's `execute · Edit` stage row is gone, so the stage's name stands
// only on the owner's stage line `3/6: execute`: in the color Fleet gives the stage `execute` (its 3rd, running:
// index 2, lit), no kind word. 11-18: with workers the default `worker_box` draws that line as the top line of the box the
// workers sit in (`┌ 3/6: execute ──┐`, two cells in); `worker_box: none` draws it as its own row, four cells in.
const stageRow=(vv,name)=>vv.rows.find(r=>new RegExp('^│ │ {3}┌ \\S* ?\\d/\\d+: '+name).test(str(r)));
assert(!v.rows.some(r=>/│ +execute · Edit/.test(str(r))),'the worker no longer has a stage row\n'+L.join('\n'));
const kind=v.rows.find(r=>/^│ │ {3}┌ 3\/6: execute/.test(str(r)));assert(kind,L.join('\n'));
const flat=draw({...opt,height:60,configuration:{worker_box:'none'}}),flatKind=flat.rows.find(r=>/^│ │ {5}3\/6: execute/.test(str(r)));
assert(flatKind&&!lines(flat).some(l=>/[┌└]/.test(l.slice(4))),lines(flat).join('\n'));
assert.equal(flatKind.find(p=>p.text==='execute').foreground,150);
const stageName=kind.find(p=>p.text==='execute');assert.equal(stageName.foreground,150);assert.equal(stageName.bold,true,'lit like Fleet\'s running stage');
// Other states by Fleet's rule: a finished stage dim of its hue, a failed one the failure red, a waiting one dim.
const other=stages.map(s=>s.name==='execute'?{...s,mark:'✕',key:'lvl_r'}:s);
const failed=draw({...opt,height:60,routes:[{...route,stages:other}]});
const f=stageRow(failed,'execute').find(p=>p.text==='execute');assert.equal(f.foreground,217);assert.equal(f.bold,true);
const allDone=stages.map(s=>({...s,mark:'✓',current:false}));   // nothing running: the line stands on the last stage, finished
// (11-18, review C2: a worker's box is titled by the stage the worker runs; with no worker running the stage text is on the route's first line.)
const done=draw({...opt,height:60,routes:[{...route,stages:allDone,workers_live:[]}]});
const d=done.rows.find(r=>/^│ │ ● code · 6\/6: report/.test(str(r))).find(p=>p.text==='report');assert.equal(d.foreground,147);assert.equal(d.dim,true);
// No key sent (an older backend): the body color as before.
const old=draw({...opt,height:60,routes:[{...route,stages:stages.map(s=>({...s,key:undefined}))}]});
assert.equal(stageRow(old,'execute').find(p=>p.text==='execute').foreground,253);
// Mono, eight colors and NO_COLOR read as before: the body color, no weight.
for(const o of [{theme:'mono'},{model:{...opt.model,colors:8}},{model:{...opt.model,no_color:true}}]){
  const m=draw({...opt,...o,model:{...opt.model,...(o.model||{})},height:60}),nm=stageRow(m,'execute').find(p=>p.text==='execute');
  assert(nm&&!(nm.foreground&&nm.foreground!==253)&&nm.bold!==true&&nm.dim!==true,JSON.stringify([o,nm]));
}
// The folded one-row form (a bottom-layout row) keeps its name color.
const one=draw({...opt,side:false,width:100,height:14}),oneRow=one.rows.find(r=>/3\/6: execute/.test(str(r)));assert(oneRow,lines(one).join('\n'));
assert(str(oneRow).includes('code · 3/6: execute'),str(oneRow));   // (the bottom layout's kind cell, 11-17: `code · 3/6: execute`)
assert(oneRow.filter(p=>p.text==='execute').every(p=>p.foreground===253||p.foreground===undefined||p.foreground===null),JSON.stringify(oneRow));
""")

if __name__ == "__main__":
    unittest.main()
