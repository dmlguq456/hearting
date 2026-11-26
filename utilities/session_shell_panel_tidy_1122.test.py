"""11-26 (panel B): the REPO and GIT rows are one row, the SESSION head is a bare rule (no word, never merged with MODEL), STATUS is its head alone, BACKLOG lists the chosen items, a message is one row; the lines below that describe the old forms are kept as the 11-22 record.
11-22 (the person, on the three drafts of the panel's tidy-up: "두칸이 아니라 한칸만 띄우도록 하자 그러면"): the line forms of the panel beside the native area.

1. A group head (REPO, SESSION) is its bold name, its value and a rule, no indent. The rows under a head stand one cell in (GIT, PEERS, STATUS, BACKLOG; MODEL, CTX, TITLE, NOW), the items of
   STATUS and BACKLOG two. An activity (QUESTIONS, ROUTE) is a rounded box with `›` at the right end of its top line. MESSAGES and MEM are their name and `›` at the right end, no rule, an item
   one cell in and a message's second row two. RESOURCES and USAGE are their name alone. Every row a press opens ends in that one `›` (STATUS, BACKLOG, NOW, QUESTIONS, ROUTE, MESSAGES, MEM).
2. Only the REPO and SESSION heads, the boxes and the last row's rule are lines; the line between CTX and TITLE is gone. `rule motion`'s light runs over the SESSION head's rule alone.
3. What a press opens is the row it was: the same actions on the same cells (a whole row for a head, an item, NOW, a box's top line), the REPO row's two buttons where the heads go.
4. What stands and the order in which rows give way are what they were: the indent only takes a cell from what a row shows (cut by the old rule), the line between CTX and TITLE was one row, and short of rows
   the SESSION head takes MODEL's value and row, so the head's name stands at every height; where MODEL's value would not stand whole on that row the head does not take it (the MODEL row stands in its own width).
5. A row `cut` or `tailed` ends in `…›` with `›` in the row's last cell and one `…` only, also where a two-cell character falls at the cut. Only the REPO and SESSION names carry weight (in mono and
   `NO_COLOR` too, where weight is the only cue); every other name is the title role's color without it.
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
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},lvl_r:{foreground:217,bold:true},
  g_blocked:{foreground:217},g_spin:{foreground:230},hb_claude:{foreground:116}};
const t=(text,key='dim')=>({text,...(styles[key]||{dim:key==='dim'})});
const MSGS=Array.from({length:7},(_,i)=>i+1).map(n=>({way:n%2?'←':'→',who:'[7'+n+']',harness:'claude',when:'09:0'+n,kind:'report',text:'시험용 메시지 '+n,body:'시험용 메시지 '+n,delivery:null}));
const MEMS=Array.from({length:5},(_,i)=>i+1).map(n=>({kind:'decision',age:n+'m',text:'시험용 기억 '+n+' 제목',tier:'durable',scope:'project',body:''}));
const route=i=>({id:'rt-'+i,title:'시험용 작업 '+i,kind:'working',state:'working',shape:'one-shot-owner',kind_word:'code',elapsed:(10+i)+'m',tool:'Edit',role:'owner',
  paint:{glyph:t('⠏','g_spin'),harness:t('claude','hb_claude'),model_text:t('Opus 5.5','hb_claude'),effort:t('xhigh','dim'),ctx:t('52%','lvl_g'),gauge5:[t('━━━','lvl_g'),t('──'),t(' '),t('52%','lvl_g')]}});
// The two parts' values as the host holds them: `{word, has_doc, items: [{mark, text, key}], total}`.
const SI=[{mark:'!',text:'시험용 정할 것 하나',key:'i:a1'},{mark:'',text:'시험용 하는 일 하나 · #101',key:'i:a2'},{mark:'',text:'시험용 하는 일 둘 — 제목이 길면 이렇게 잘린다 이렇게 길게 이어지는 시험용 제목입니다',key:'i:a3'}];
const BI=[{mark:'',text:'시험용 나중 일 하나',key:'i:b1'},{mark:'',text:'시험용 나중 일 둘',key:'i:b2'},{mark:'',text:'시험용 나중 일 셋',key:'i:b3'}];
const part=(word,list,n,total,has=true)=>({word,has_doc:has,items:list.slice(0,n),total});
const status=(n=3,total=5,has=true)=>part('STATUS',SI,n,total,has),backlog=(n=3,total=14,has=true)=>part('BACKLOG',BI,n,total,has);
function model(o={}){
  const routes=Array.from({length:o.routes??0},(_,i)=>route(i+1));
  return {styles,colors:256,tag:'[e9]',repo_side:[t('hearting','loc_repo')],worktrees:[t('wt 12')],repo:[t('hearting','loc_repo')],git_side:[t('branch'),t(' ✓','lvl_g')],git:[t('branch')],
    selected:o.selected??[t('Opus 5.5','hb_claude'),t(' xhigh')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],context:[t('ctx 41%','lvl_g')],quota:[t('5h ','dim'),t('23%','lvl_g')],
    session_title:'시험용 세션',now:o.now??'시험용 NOW 한 줄',route_label:routes.length?'route '+routes.length:undefined,route_count:routes.length,route_total:routes.length,routes,
    messages:MSGS.slice(-(o.messages??0)),memory_items:MEMS.slice(0,o.mem??0),
    ...(o.status!==undefined?{status_doc:o.status}:{}),...(o.backlog!==undefined?{backlog_doc:o.backlog}:{}),
    ...(o.peers===false?{}:{peers:{groups:[{steward:{tag:'e9',key:'hb_claude',me:true},members:[{tag:'d4',key:'hb_codex'}]}]}})};
}
const brief=()=>({count:1,answerable:true,choices:2,label:'QUESTIONS',rows:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')],[t('1 ','dim'),t('가','loc_repo'),t(' · ','dim'),t('2 ','dim'),t('나','loc_repo')]],
  one:[[t('◑ ','g_blocked'),t('시험용 질문?','loc_repo')]],line:'시험용 질문'});
const draw=(o={})=>view.render({width:o.width??59,height:o.height??40,side:o.side??true,model:model(o),state:'working',harness:'claude',frame:1000,since:990,
  brief:o.question?brief:undefined,sent:()=>[],cellWidth:cells,configuration:{side_width:o.width??59,rule_motion:'off',theme:'terminal',position:'right'}});
const lines=v=>v.rows.map(str);
const inside=v=>lines(v).map(l=>l.slice(2,-2).trimEnd());
const count=(v,re)=>inside(v).filter(l=>re.test(l)).length;

const drawW=o=>view.render({width:o.width??44,height:o.height??52,side:o.side??true,model:{...model(o),messages:MSGS.slice(7-(o.messages??0)),...(o.usage?{usage:[{harness:'claude',parts:[t('5h ','dim'),t('23%','lvl_g')]},{harness:'codex',parts:[t('5h ','dim'),t('3%','lvl_g')]}]}:{})},
  state:o.state??'working',harness:'claude',frame:o.frame??1000,since:990,brief:o.question?brief:undefined,sent:()=>[],cellWidth:cells,
  configuration:{side_width:o.width??44,rule_motion:o.motion??'off',theme:'terminal',position:'right'}});
"""


@unittest.skipUnless(NODE, "needs node")
class PanelTidyTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_picture_at_44_by_52_is_the_one_the_person_chose(self):
        self.node(r"""
const v=drawW({question:true,routes:1,messages:1,mem:1,status:status(),backlog:backlog(),height:52}),ls=lines(v);
// 11-26 (panel B): REPO and GIT are one row, STATUS is its head alone, BACKLOG's items are the backend's chosen ones in the new item form, the SESSION head is a bare rule, a message is one row.
const SHOWN=[
'│  REPO   hearting (branch ✓)  wt 12       │',
'│  PEERS  [e9] ⚑  ─ [d4]                   │',
'│  STATUS 5                              › │',
'│  BACKLOG +11                           › │',
'│   · —       TBD  —     시험용 나중 일 …  │',
'│   · —       TBD  —     시험용 나중 일 둘 │',
'│   · —       TBD  —     시험용 나중 일 셋 │',
'│ ──────────────────────────────────────── │',
'│  MODEL  Opus 5.5 xhigh                   │',
'│  CTX    ━━━━──── 41%                     │',
'│  TITLE  시험용 세션                      │',
'│  NOW    시험용 NOW 한 줄               › │',
'│                                          │',
'│ ╭─ 1 QUESTIONS ───────────────────── ›─╮ │'];
assert.deepEqual(ls.slice(1,15).map(l=>l.replace(/ +│$/,' │')),SHOWN.map(l=>l.replace(/ +│$/,' │')),ls.join('\n'));
assert(ls.every(l=>cells(l)===44),ls.join('\n'));
// The lower sections: the name and `›` at the right end, no rule; an item one cell in; the box's `›` at the end of its top line.
const at=re=>ls.findIndex(l=>re.test(l));
const m=at(/^│ MESSAGES +› │$/),e=at(/^│ MEM +› │$/),r=at(/^│ ╭─ 1 ROUTE ─+ ›─╮ │$/);
assert(m>0&&e>m&&r>0&&r<m,ls.join('\n'));
assert(/^│  ← \[77\] 시험용 메시지 7 +09:07 │$/.test(ls[m+1])&&e===m+2&&/^│  ◆ 1m decision 시험용 기억 1 제목 +│$/.test(ls[e+1]),ls.slice(m).join('\n'));  // 11-26 (panel B): the message is one row, time at the right end
// The lines in the whole picture: the REPO and SESSION heads, the two boxes' lines, the rule over the last row. No other.
const lined=ls.map((l,i)=>[i,l]).filter(([,l])=>/─{5}/.test(l)).map(([i,l])=>i);
assert(lined.every(i=>/^│ (REPO|SESSION) |^│ [╭│╰]|^┌|^└|^│ ─+ │$/.test(ls[i])),lined.map(i=>ls[i]).join('\n'));
// 11-26 (panel B): the one rule left over the rows is the SESSION rule right above MODEL; none between CTX and TITLE.
const rules=ls.map((l,i)=>/^│ ─+ │$/.test(l)?i:-1).filter(i=>i>=0);
assert(rules.length===2&&/^│  MODEL /.test(ls[rules[0]+1])&&rules[1]>=ls.length-3&&at(/^│  CTX /)+1===at(/^│  TITLE /),'no line between CTX and TITLE\n'+ls.join('\n'));
""")

    def test_the_indent_is_one_cell_and_a_value_is_cut_by_the_old_rule_in_the_cells_left(self):
        self.node(r"""
for(const width of [59,44,40,36,34,30,26]){
 const v=drawW({width,height:60,question:true,routes:1,messages:3,mem:2,status:status(),backlog:backlog()}),ins=lines(v).map(l=>l.slice(2,-2));
 const lead=re=>{const l=ins.find(s=>re.test(s));assert(l,width+' '+re+'\n'+ins.join('\n'));return l.length-l.trimStart().length;};
 const label=width-4>=30;
 for(const re of label?[/^ *REPO /,/^ *PEERS /,/^ *MODEL /,/^ *CTX /,/^ *TITLE /,/^ *NOW /]:[/^ *REPO /,/^ *\[e9\]/,/^ *Opus 5\.5/,/^ *ctx 41%/,/^ *시험용 세션/])assert.equal(lead(re),1,width+' '+re);
 assert.equal(lead(/^ *STATUS/),1,String(width));assert.equal(lead(/^ *BACKLOG/),1,String(width));
 // 11-26 (panel B): STATUS lists no items (its head alone); BACKLOG's chosen items stand two cells in; the SESSION head is a bare rule with no indent (like the old head); REPO is a row one cell in.
 assert(!ins.some(s=>/^ *! 시험용|^ *· 시험용 (정할|하는)/.test(s)),'no STATUS item rows '+width);
 assert.equal(lead(/^ *· /),2,String(width));assert.equal(lead(/^ *─+$/),0,String(width));
 // The lower sections' heads: no indent; the items of MESSAGES and MEM one cell in.
 for(const re of [/^MESSAGES/,/^MEM /])assert(ins.some(s=>re.test(s)),width+' '+re);
 assert(ins.some(s=>/^ [←→] \[/.test(s))&&ins.some(s=>/^ ◆ /.test(s)),width+'\n'+ins.join('\n'));
 assert(lines(v).every(l=>cells(l)===width));
}
// A long text is cut with `…` by the rule it had, in the cells the indent leaves: a BACKLOG item's title at 44 (40 inside) ends in `…` and fills its row.
// (11-26 panel B: it was a STATUS item; STATUS lists none now, so the same check runs on the BACKLOG item.)
{const v=drawW({width:44,height:60,backlog:{...backlog(),items:[{mark:'',text:'시험용 아주 긴 제목이라서 잘려야 합니다 시험용 아주 긴 제목',key:'i:x'}]}}),a=lines(v).map(l=>l.slice(2,-2)),i=a.findIndex(s=>/^ BACKLOG/.test(s));
 assert(a[i+1].startsWith('  · ')&&a[i+1].includes('시험용')&&a[i+1].trimEnd().endsWith('…')&&cells(a[i+1].trimEnd())>=34,a[i+1]);}
""")

    def test_a_row_a_press_opens_ends_in_one_arrow_at_the_right_end(self):
        self.node(r"""
const v=drawW({question:true,routes:1,messages:1,mem:1,status:status(),backlog:backlog(),height:52}),ls=lines(v),inn=ls.map(l=>l.slice(2,-2));
const endsIn=re=>{const l=inn.find(s=>re.test(s));assert(l,re+'\n'+ls.join('\n'));return l;};
for(const re of [/^ STATUS/,/^ BACKLOG/,/^ NOW /,/^MESSAGES/,/^MEM /])assert(/ ›$/.test(endsIn(re))||/^ NOW .*›$/.test(endsIn(re)),re+' '+endsIn(re));
for(const re of [/^ STATUS/,/^ BACKLOG/,/^ NOW /,/^MESSAGES/,/^MEM /])assert.equal([...endsIn(re)].filter(c=>c==='›').length,1,re);
assert(/ ›─╮$/.test(endsIn(/^╭─ 1 QUESTIONS/))&&/ ›─╮$/.test(endsIn(/^╭─ 1 ROUTE/)));
// The mark stands in the last cell of the row where a press lands (the one a person finds it in), and nowhere else on the rows around it.
for(const re of [/^ STATUS/,/^ BACKLOG/,/^ NOW /,/^MESSAGES/,/^MEM /])assert.equal(cells(endsIn(re)),40,String(re));
// A NOW on several rows ends its last row alone with `›`; where the last row is full the mark takes the row's last cell as `…›`.
{const m=model({routes:0}),w=view.render({width:36,height:52,side:true,model:{...m,now:'가가가가 가가가가 가가가가 가가가가 가가가가 가가가가',status_doc:undefined,backlog_doc:undefined},state:'working',harness:'claude',frame:1000,since:990,sent:()=>[],cellWidth:cells,configuration:{side_width:36,rule_motion:'off'}}),a=lines(w).map(l=>l.slice(2,-2).trimEnd());
 const i=a.findIndex(s=>/^ NOW /.test(s));let k=i;while(k+1<a.length&&/^        \S/.test(a[k+1]))k++;
 assert(a[k].endsWith('›')&&a.slice(i,k).every(s=>!s.includes('›')),a.slice(i,k+1).join('\n'));}
// The bottom layout keeps its own form (no indent, its heads keep their rule) and ends the row of a NOW in the same `›`.
{const v=drawW({side:false,width:120,height:14,question:true,routes:1,messages:1,mem:1}),ls=lines(v);
 assert(ls.some(l=>/^│ 시험용 세션 │ NOW 시험용 NOW 한 줄 +› │$/.test(l)),ls.join('\n'));
 assert(ls.some(l=>/^│ messages ─+ › │$/.test(l))&&ls.some(l=>/^│ ╭─ route 1 ─+ ›─╮ │$/.test(l))&&!ls.some(l=>/^│  /.test(l)),ls.join('\n'));}
""")

    def test_the_rule_the_light_moves_over_is_the_session_heads_alone(self):
        self.node(r"""
// 11-26 (panel B): the SESSION head is a bare rule (no word) of the inner width: 40 cells at 44, from col 2; the light runs over all of them. The rule above USAGE/RESOURCES stays still.
// Tall: the light on those cells only; no other row carries it.
{const v=drawW({question:true,routes:1,height:52,motion:'rule',state:'working',status:status(),backlog:backlog(),usage:true}),ls=lines(v);
 const ys=ls.map((l,i)=>/^│ ─+ │$/.test(l)?i:-1).filter(i=>i>=0),y=ys[0];assert(ys.length===2,ls.join('\n'));
 assert(ls[y+1].startsWith('│  MODEL'),'the SESSION rule stands over MODEL\n'+ls.join('\n'));
 const run=ls[y].slice(2).match(/─+/)[0].length;assert.equal(run,40,ls[y]);
 const cs=v.motion.cells.filter(c=>c.rule!==undefined);
 assert.equal(cs.length,40);assert(cs.every((c,i)=>c.row===y&&c.col===2+i&&c.ch==='─'&&c.rule===i),JSON.stringify(cs.slice(0,3)));
 // The light goes round and comes back: some frame lights cells, some frame (the rest) lights none; the next change is asked for from the rule's own length.
 const lit=f=>v.motion.at(989+f).filter(p=>p.tone==='scan').length;   // (`since` is 990 and a rule's own tick is one on: the host's frame 989+f is the rule's tick f)
 assert(lit(3)>0&&lit(13)===0&&lit(26)===0&&v.motion.next(992)===993);
 for(const f of [0,5,13,20]){const a=v.motion.at(989+f);assert.equal(a.length,v.motion.cells.length);}
 assert(!ls.some((l,i)=>i!==y&&v.rows[i].some(p=>p.rule)),'one row carries the rule');
 // The rule above USAGE is there, dim, and does not move.
 const u=ys[1],ur=v.rows[u];assert(/^│ USAGE/.test(ls[u+1])&&!ur.some(p=>p.rule)&&ur.some(p=>p.text.includes('─')&&p.dim===true),ls[u]+JSON.stringify(ur));
 assert(!v.motion.cells.some(c=>c.row===u),'the rule above USAGE is not moved');}
// Short of rows the rule stands over MODEL's own row (a spare row) and the light runs over it; with no spare row there is no rule and no light.
{const v=drawW({question:true,routes:1,messages:1,mem:1,height:24,motion:'rule',state:'working',status:status(),backlog:backlog()}),ls=lines(v);
 const y=ls.findIndex(l=>/^│ ─+ │$/.test(l));assert(y>0&&ls[y+1].startsWith('│  MODEL  Opus 5.5 xhigh'),ls.join('\n'));
 const cs=v.motion.cells.filter(c=>c.rule!==undefined);
 assert.equal(cs.length,40);assert(cs.every(c=>c.row===y&&c.ch==='─'),JSON.stringify(cs.slice(0,2)));}
{const v=drawW({question:true,routes:1,messages:1,mem:1,height:16,motion:'rule',state:'working',status:status(),backlog:backlog()}),ls=lines(v);
 assert(!ls.some(l=>/^│ ─+ │$/.test(l))&&ls.some(l=>/^│  MODEL  Opus 5\.5 xhigh/.test(l)),ls.join('\n'));
 assert(!(v.motion&&v.motion.cells.some(c=>c.rule!==undefined)),'no rule, no light');}
// Still: the plain rule in every other mode (border, scanners, off and a waiting frame keep a rule that does not move).
for(const [motion,state] of [['border','working'],['scanners','working'],['off','working'],['rule','idle'],['rule','blocked']]){
 const v=drawW({height:52,motion,state}),ls=lines(v),y=ls.findIndex(l=>/^│ ─+ │$/.test(l));assert(y>0,motion+state);
 const parts=v.rows[y].filter(p=>p.rule);assert(parts.length===1&&parts[0].text==='─'.repeat(40)&&parts[0].role==='messages'&&parts[0].dim===true,motion+state+JSON.stringify(parts));
 assert(!(v.motion&&v.motion.cells.some(c=>c.rule!==undefined)),motion+state);
}
""")

    def test_the_arrow_of_a_cut_row_stands_in_the_last_cell_and_the_dots_are_one(self):
        self.node(r"""
// A two-cell character at the cut left the `›` one cell short of the end (`…›  │`): the cells left stand between `…` and `›`.
const nowOf=(w,h,now,o={})=>inside(view.render({width:w,height:h,side:true,model:{...model({routes:0,...o}),now,status_doc:undefined,backlog_doc:undefined},state:'working',harness:'claude',frame:1000,since:990,
  sent:()=>[],cellWidth:cells,configuration:{side_width:w,rule_motion:'off'}}));
const NOWS=['abc 가나다라마바사아자차카타파하 가나다라마바사아자차카타파하 끝','가나다라마바사아자차카타파하가나다라마바사아자차카타파하가나다라마바사아자차카타파하','시험용 NOW가 아주 길게 이어져서 두 줄 이상을 차지하는 경우를 그려 본다 시험용 abc','a 가나 다라마바 사아자차카타파하 abcdefghijklmnop 가나다라마바사아자차카타파하 abc 가'];
let seen=0;
for(const w of [59,44,40,36,32])for(const now of NOWS)for(let h=8;h<=50;h++){
 const a=nowOf(w,h,now),tag=w+'x'+h+' '+now+'\n'+a.join('\n');
 for(const s of a){
  assert(!s.includes('……'),'one `…` only\n'+tag);
  if(/…\s?›$/.test(s)){seen++;assert.equal(cells(s),w-4,'`›` in the last cell\n'+tag);assert(s.endsWith(' ›')||s.endsWith('…›'),tag);}
 }
}
assert(seen>=200,String(seen));
// The reviewer's picture: a two-cell character at the cut.
{const a=nowOf(44,32,NOWS[0]),i=a.findIndex(s=>/^ NOW /.test(s));
 assert.equal(a[i+1],'        가나다라마바사아자차카타파하 … ›','\n'+a.join('\n'));}
// The bottom layout's NOW row (its title beside it) is the same `tailed`: heights 6-22, four widths.
for(const w of [120,100,80,60])for(const now of NOWS)for(let h=6;h<=22;h++){
 const ls=lines(view.render({width:w,height:h,side:false,model:{...model({routes:0}),now,status_doc:undefined,backlog_doc:undefined},state:'working',harness:'claude',frame:1000,since:990,sent:()=>[],cellWidth:cells,configuration:{side_width:w,rule_motion:'off'}}));
 for(const s of ls){assert(!s.includes('……'),w+'x'+h+'\n'+s);const m=/^│ (.*NOW .*…)\s?› │$/.exec(s);if(m)assert.equal(cells(s),w,w+'x'+h+'\n'+s);}
}
""")

    def test_only_the_repo_and_session_names_are_bold(self):
        self.node(r"""
// 11-26 (panel B): no group name is bold any more (REPO is a dim-label row, the SESSION head is a bare rule); the BACKLOG head word alone is bold.
const boldOf=(v,word)=>v.rows.flatMap(r=>r.filter(p=>p.text.trim()===word)).map(p=>p.bold===true);
for(const [name,cfg,extra] of [['terminal',{theme:'terminal'},{}],['light',{theme:'light'},{}],['mono',{theme:'mono'},{}],['no_color',{theme:'terminal'},{no_color:true}]])
for(const h of [60,12]){
 const v=view.render({width:44,height:h,side:true,model:{...model({routes:1,messages:1,mem:1,status:status(),backlog:backlog()}),...extra},state:'working',harness:'claude',frame:1000,since:990,sent:()=>[],cellWidth:cells,
  configuration:{side_width:44,rule_motion:'off',position:'right',...cfg}}),tag=name+' '+h+'\n'+lines(v).join('\n');
 assert.deepEqual(boldOf(v,'REPO'),[false],'REPO '+tag);
 assert.deepEqual(boldOf(v,'SESSION'),[],'no SESSION word '+tag);assert.deepEqual(boldOf(v,'GIT'),[],'no GIT row '+tag);
 for(const w of ['REPO','PEERS','MESSAGES','MEM','STATUS','NOW','TITLE','MODEL','CTX'])assert(boldOf(v,w).every(b=>!b),w+' '+tag);
 // BACKLOG: bold when it is the row's head (tall); short of rows it is only a button on the REPO row and not bold.
 if(h===60)assert.deepEqual(boldOf(v,'BACKLOG'),[true],'BACKLOG '+tag);else assert(boldOf(v,'BACKLOG').every(b=>!b),'BACKLOG '+tag);
}
// Short of rows there is no SESSION word at all (the rule over MODEL or MODEL alone).
{const v=drawW({height:14,question:true,mem:3,status:status(),backlog:backlog()});assert(!lines(v).some(l=>/SESSION/.test(l)),lines(v).join('\n'));assert.deepEqual(boldOf(v,'SESSION'),[]);}
""")

    def test_a_model_value_that_does_not_stand_whole_does_not_merge_into_the_head(self):
        self.node(r"""
const LONG=[t('GPT-5.5-codex-max','hb_claude'),t(' xhigh')];
// 11-26 (panel B): the head is a bare rule now and never takes MODEL's value: the MODEL row always stands in its own width (its value cut by its own width), with the rule over it where a row is spare.
let ruled=0,bare=0;
for(const w of [44,36,32,30])for(const [sel,whole] of [[LONG,'GPT-5.5-codex-max xhigh'],[model().selected,'Opus 5.5 xhigh']])for(let h=8;h<=40;h++)for(const question of [false,true])for(const mem of [0,3]){
 const v=drawW({width:w,height:h,question,mem,selected:sel,status:status(),backlog:backlog()}),ls=lines(v),a=inside(v),tag=w+'x'+h+'\n'+ls.join('\n');
 assert.equal(ls.length,h,tag);assert(ls.every(l=>cells(l)===w),tag);
 assert(!a.some(s=>/SESSION/.test(s)),'no SESSION word\n'+tag);
 const m=a.findIndex(s=>/^ MODEL |^ GPT|^ Opus/.test(s));assert(m>0,'MODEL row stands\n'+tag);
 if(w>=36)assert(a[m].includes(whole),'value whole where it fits\n'+tag);
 if(/^─+$/.test(a[m-1]))ruled++;else bare++;
}
assert(ruled>0&&bare>0,ruled+' '+bare);
// The reviewer's picture: 32 wide, 14 high, a long model name: the MODEL row keeps the name whole.
{const a=inside(drawW({width:32,height:14,question:true,mem:3,selected:LONG,status:status(),backlog:backlog()}));
 assert(!a.some(s=>/SESSION/.test(s))&&a.some(s=>s.includes('GPT-5.5-codex-max xhigh')),a.join('\n'));}
""")

    def test_the_session_head_stays_and_the_rows_give_way_in_the_order_they_did(self):
        self.node(r"""
// 11-26 (panel B): the SESSION head is a bare rule over MODEL's own row; no word, no merged row, no GIT row, no STATUS items. The order is: BACKLOG items, the two heads, then the rule over MODEL; the REPO row and TITLE stand to 12 rows.
const o={question:true,routes:1,messages:3,mem:3,status:status(),backlog:backlog()};
const seen=[];
for(let h=70;h>=12;h--){
 const v=drawW({...o,height:h}),ls=lines(v),a=ls.map(l=>l.slice(2,-2).trimEnd());
 assert.equal(ls.length,h,h+'\n'+ls.join('\n'));
 assert(!a.some(s=>/SESSION/.test(s)),'no SESSION word '+h+'\n'+ls.join('\n'));
 const mi=a.findIndex(s=>/^ MODEL /.test(s));assert(mi>0,'MODEL row stands '+h+'\n'+ls.join('\n'));
 const ruled=/^─+$/.test(a[mi-1]);
 // The only rules besides the one over MODEL are the one above the quota/USAGE row and the last row's: none between CTX and TITLE.
 const ci=a.findIndex(s=>/^ CTX /.test(s)),ti=a.findIndex(s=>/^ TITLE /.test(s));assert(ci>0&&ti===ci+1,h+'\n'+ls.join('\n'));
 assert(!a.some(s=>/^ GIT |^ ! 시험용|^  [!·] 시험용 (정할|하는)/.test(s)),'no GIT row, no STATUS items '+h+'\n'+ls.join('\n'));
 seen.push({h,ruled,bItems:a.filter(s=>/^  · /.test(s)).length,heads:a.filter(s=>/^ (STATUS|BACKLOG)\b/.test(s)).length,repo:a.some(s=>/^ REPO /.test(s)),title:ti>0});
}
const first=f=>{const x=seen.find(f);return x?x.h:0;};
const order=[first(r=>r.bItems<3),first(r=>!r.heads),first(r=>!r.ruled)];
// BACKLOG items first, then the two heads (the REPO row takes the buttons), then the rule over MODEL: each strictly earlier than the next. REPO and TITLE stay to 12 rows.
assert(seen.every(r=>r.title&&r.repo),'REPO and TITLE stand at every height');
assert(order.every((x,i)=>x>0&&(!i||x<order[i-1])),JSON.stringify(order));
// The rule over MODEL costs a spare row only: the picture is as high as asked at any height.
const tall=lines(drawW({...o,height:60})).length,short=lines(drawW({...o,height:30})).length;assert.equal(tall,60);assert.equal(short,30);
""")

    def test_the_presses_are_where_they_were(self):
        self.node(r"""
const v=drawW({question:true,routes:1,messages:3,mem:2,status:status(),backlog:backlog(),height:60}),ls=lines(v),a=ls.map(l=>l.slice(2,-2));
const rowOf=re=>a.findIndex(s=>re.test(s));
const act=i=>v.hits.filter(h=>h.row===i).map(h=>[h.left,h.right,h.action]);
// A whole row for a head, an item, NOW (every row of it), a box's top line, MESSAGES and MEM heads and each of their rows; none on the group heads, GIT, PEERS, MODEL, CTX or TITLE.
const sh=rowOf(/^ STATUS/),bh=rowOf(/^ BACKLOG/);
// 11-26 (panel B): STATUS is its head alone (no item rows, no `popup:STATUS:<key>` press anywhere); BACKLOG's chosen items keep their `popup:BACKLOG:<key>` rows; no GIT row; the SESSION head is a bare rule.
assert.deepEqual(act(sh),[[2,41,'popup:STATUS']]);assert(bh===sh+1&&!v.hits.some(h=>/^popup:STATUS:/.test(h.action)),'no STATUS item rows or presses');assert.deepEqual(act(bh+3),[[2,41,'popup:BACKLOG:i:b3']]);
assert(!a.some(s=>/^ GIT /.test(s)),'no GIT row');
for(const re of [/^ REPO /,/^ PEERS /,/^─+$/,/^ MODEL /,/^ CTX /,/^ TITLE /])assert.deepEqual(act(rowOf(re)),[],String(re));
assert.deepEqual(act(rowOf(/^ NOW /)),[[2,41,'popup:NOW']]);
assert.deepEqual(act(rowOf(/^╭─ 1 QUESTIONS/)),[[2,41,'question']]);assert.deepEqual(act(rowOf(/^╭─ 1 ROUTE/)),[[2,41,'popup:ROUTE']]);
const mh=rowOf(/^MESSAGES/),eh=rowOf(/^MEM /);
assert.deepEqual(act(mh),[[2,41,'popup:MESSAGES']]);assert.deepEqual(act(mh+1),[[2,41,'popup:MESSAGES']]);assert.deepEqual(act(mh+2),[[2,41,'popup:MESSAGES']]);assert.deepEqual(act(eh),[[2,41,'popup:MEM']]);assert.deepEqual(act(eh+1),[[2,41,'popup:MEM:0']]);assert.deepEqual(act(eh+2),[[2,41,'popup:MEM:1']]);
// The REPO row where the heads go: the two buttons at its right end, STATUS's press from the row's first cell to BACKLOG's, BACKLOG's to the end.
// 11-26 (panel B): the row's value now carries the Git words; it stays whole (its arrows and warning first, as B's rule says) and the buttons give
// way to `›` where both do not fit (at 44 cells), as they did at narrower widths before; at 59 cells the counted buttons stand, `wt` gone first
// (fix 1 of #1863: their numbers mean what the heads' do, `STATUS 5` and `BACKLOG +14`).
for(const [width,re] of [[44,/^│  REPO   hearting \(branch ✓\)  wt 12  ›  › │$/],[59,/^│  REPO   hearting \(branch ✓\) +STATUS 5 ›  BACKLOG \+14 › │$/]]){
 const w=drawW({width,question:true,routes:3,messages:7,mem:5,height:14,status:status(3,5),backlog:backlog(3,14)}),l=lines(w);
 assert(re.test(l[1]),width+' '+l[1]);
 const p=w.hits.filter(h=>h.row===1);assert.deepEqual(p.map(h=>h.action),['popup:STATUS','popup:BACKLOG']);assert.equal(p[0].left,2);assert.equal(p[1].right,width-3);assert.equal(p[0].right+1,p[1].left);
 assert.equal(l[1].slice(p[1].left).trimStart().slice(0,1)==='›'||l[1].slice(p[1].left).startsWith('BACKLOG'),true,l[1]);}
""")

    def test_a_rule_gives_way_before_a_ranked_backlog_item(self):
        # 11-26 (fix 1 of #1863, the supervisor's decision): of the rows B's forms leave, the BACKLOG items the person ranked come before the rule over
        # the equipment (a rule gives way before what is read, as the rule above the last row always did). At no height with the two heads up does that
        # rule stand while a ranked item is short; some height has an item standing and the rule gone.
        self.node(r"""
const o={width:50,question:true,routes:1,messages:2,mem:2,usage:true,status:status(),backlog:backlog()};
const seen=[];
for(let h=60;h>=14;h--){
  const ls=inside(drawW({...o,height:h})),u=ls.findIndex(l=>/^USAGE$/.test(l));
  const heads=ls.some(l=>/^ BACKLOG\b/.test(l)),items=ls.filter(l=>/^  · /.test(l)).length,rule=u>0&&/^─+$/.test(ls[u-1]);
  seen.push({h,heads,items,rule});
  if(heads&&rule)assert.equal(items,3,h+'\n'+ls.join('\n'));
}
const short=seen.find(r=>r.heads&&r.items>0&&r.items<3&&!r.rule);
assert(short,JSON.stringify(seen));
// The picture at that height: the head counts what is not drawn, one item stands, and the row over USAGE is no rule.
{const ls=inside(drawW({...o,height:short.h})),u=ls.findIndex(l=>/^USAGE$/.test(l));
 assert(ls.some(l=>new RegExp('^ BACKLOG \\+'+(14-short.items)+' +›$').test(l))&&!/^─+$/.test(ls[u-1]),short.h+'\n'+ls.join('\n'));}
""")

    def test_the_repo_row_keeps_its_arrows_and_warning_and_the_buttons_their_words_where_they_fit(self):
        # 11-26 (fixes 1 and 2 of #1863, the supervisor's one order; each gives way only once all before it have): `wt`, the label column's blanks,
        # the folded buttons' counts and then their words (`›  ›`), the letters of the name and the branch (the longer first, each down to its first
        # letter and `…`, a wide first letter whole), then the Git words from their end. A branch the backend already cut keeps a `…`.
        self.node(r"""
const git=(branch,...flags)=>[t(branch),...flags.map(f=>t(f,f.includes('↑')?'lvl_g':f.includes('↓')?'lvl_r':'dim'))];
const row=(width,height,name,parts,lists=true)=>{const v=view.render({width,height,side:true,model:{...model({messages:2,mem:2,routes:1,
  ...(lists?{status:status(),backlog:backlog()}:{})}),repo_side:[t(name,'loc_repo')],git_side:parts,worktrees:[t('wt 12')]},state:'working',harness:'claude',frame:1000,since:990,
  brief,sent:()=>[],cellWidth:cells,configuration:{side_width:width,rule_motion:'off',theme:'terminal',position:'right'}});return {line:lines(v)[1],hits:v.hits.filter(h=>h.row===1)};};
const cases=[   // (the letters a cut leaves depend on the cells; what is pinned: which part is cut, the Git words whole, the buttons)
  [40,24,'hearting',git('bl-git-index-lock-1…',' ↑2',' ↓5',' ⚠️merge'),/^│  REPO hea\S*… \(bl\S*… ↑2 ↓5 ⚠️merge\) ›  › │$/],
  [36,52,'hearting',git('main',' ↑2',' ↓5',' ⚠️merge'),/^│  REPO hear\S*… \(main ↑2 ↓5 ⚠️merge\) │$/],   // (the name is the longer: it goes first)
  [30,52,'hearting',git('main',' ↑2',' ↓5'),/^│  REPO hear\S*… \(main ↑2 ↓5\) │$/],
  [30,24,'trial',git('main',' ↑2',' ↓5'),/^│  REPO t\S*… \(m\S*… ↑2 ↓5\) ›  › │$/],
  [40,52,'trial',git('feature/update-pane…',' ↑12',' ↓345',' ⚠️rebase'),/^│  REPO trial \(fea\S*… ↑12 ↓345 ⚠️rebase\) │$/],
  // (the buttons fold to `›  ›` before a letter of the branch goes; `wt` stands again beside them where it fits)
  [50,24,'hearting',git('main',' ↑2',' ↓5'),/^│  REPO   hearting \(main ↑2 ↓5\)  wt 12 +›  › │$/],
  [50,24,'trial',git('main',' ↑2'),/^│  REPO   trial \(main ↑2\) +STATUS ›  BACKLOG › │$/],
  [40,52,'hearting',git('bl-git-index-lock-1…'),/^│  REPO hearting \(bl-git-index-lock-…\) │$/],
  [44,24,'hearting',git('기능/패널-정리',' ✓'),/^│  REPO   hearting \(기능\/패널-정리 ✓\) ›  › │$/]];
for(const [w,h,name,parts,re] of cases){const r=row(w,h,name,parts);
  assert(re.test(r.line)&&!r.line.includes('… …')&&cells(r.line)===w,w+'x'+h+' '+r.line);
  if(h===24)assert(r.hits.length===2&&r.hits[0].action==='popup:STATUS'&&r.hits[1].action==='popup:BACKLOG'&&r.hits[1].right===w-3,JSON.stringify(r.hits));}
// Every width from 30 to 60, both heights: the Git words are cut only where the name and the branch are already one letter and `…`, and then
// the warning's letters go first (the arrows stand).
let whole=0;
for(let w=30;w<=60;w++)for(const h of [52,24]){const r=row(w,h,'hearting',git('main',' ↑2',' ↓5',' ⚠️merge'));
  if(r.line.includes('↑2 ↓5 ⚠️merge)'))whole++;
  else assert(/^│  REPO \S… \(m… ↑2 ↓5 \S*…\)/.test(r.line),w+'x'+h+' '+r.line);
  assert(!r.line.includes('… …')&&cells(r.line)===w,r.line);}
assert(whole>=55,String(whole));   // (of 62 rows the seven short ones are 30 × 52 and 30–35 × 24, beside the folded buttons: `⚠️mer…` … `…`)
// Truly no room (30 × 24, the folded buttons beside): the warning's letters go first, the arrows stay.
assert(/^│  REPO h… \(m… ↑2 ↓5 …\) ›  › │$/.test(row(30,24,'hearting',git('main',' ↑2',' ↓5',' ⚠️merge')).line),row(30,24,'hearting',git('main',' ↑2',' ↓5',' ⚠️merge')).line);
// The folded buttons' numbers mean what the heads' do: `STATUS 5` (the board's whole count, no `+`; none for zero) and `BACKLOG +N` (every item, none drawn).
{const v=drawW({width:60,height:24,question:true,routes:1,messages:2,mem:2,status:status(),backlog:backlog()});
 assert(/STATUS 5 ›  BACKLOG \+14 › │$/.test(lines(v)[1]),lines(v)[1]);}
{const v=drawW({width:60,height:24,question:true,routes:1,messages:2,mem:2,status:status(0,0),backlog:backlog(0,0)});
 assert(/ STATUS ›  BACKLOG › │$/.test(lines(v)[1]),lines(v)[1]);}
// The bottom layout keeps its own buttons (`STATUS +5 ›  BACKLOG +14 ›`: the items not shown there are all of them).
{const v=drawW({width:120,height:10,side:false,status:status(),backlog:backlog()});assert(lines(v).some(l=>/STATUS \+5 ›  BACKLOG \+14 ›/.test(l)),lines(v).join('\n'));}
""")

    def test_a_cut_branch_keeps_its_mark_and_a_wider_window_never_shows_less_of_the_name_or_the_branch(self):
        # 11-26 (fix 2 of #1863): a branch the backend already cut (`bl-git-index-lock-1…`, nineteen letters and `…`) never stands without a `…`
        # (one cell short it was drawn `(bl-git-index-lock-1)`, a branch that does not exist); widening the window by one cell never shortens the
        # name or the branch (the buttons fold before their letters go); a wide first letter stands whole with its `…`.
        self.node(r"""
const git=(branch,...flags)=>[t(branch),...flags.map(f=>t(f,f.includes('↑')?'lvl_g':f.includes('↓')?'lvl_r':'dim'))];
const row=(width,height,name,parts)=>lines(view.render({width,height,side:true,model:{...model({messages:2,mem:2,routes:1,status:status(),backlog:backlog()}),
  repo_side:[t(name,'loc_repo')],git_side:parts,worktrees:[t('wt 12')]},state:'working',harness:'claude',frame:1000,since:990,brief,sent:()=>[],cellWidth:cells,
  configuration:{side_width:width,rule_motion:'off',theme:'terminal',position:'right'}}))[1];
const parse=line=>{const m=/REPO\s+(.*?) \(([^ )]*)/.exec(line);return m?{name:m[1],branch:m[2]}:null;};
const flagsets=[[],[' ↑2'],[' ↑2',' ↓5',' ⚠️merge'],[' ✓'],[' ↑12',' ↓345',' ⚠️rebase']];
let rows=0;
for(const name of ['hearting','trial','hearting-maintenance-ws','저장소-하팅'])for(const branch of ['bl-git-index-lock-1…','main','panel-b-1126','기능/패널-정리'])for(const fl of flagsets)for(const h of [52,24]){
  let last=null;
  for(let w=32;w<=64;w++){const line=row(w,h,name,git(branch,...fl)),got=parse(line);rows++;
    assert(got&&!line.includes('… …')&&cells(line)===w,w+'x'+h+' '+line);
    if(got.branch!==branch)assert(got.branch.endsWith('…'),'a cut branch keeps its mark: '+w+'x'+h+' '+line);
    if(/^[가-힣]/.test(branch))assert.notEqual(got.branch,'…','a wide first letter stands whole: '+w+'x'+h+' '+line);
    if(/^[가-힣]/.test(name))assert.notEqual(got.name,'…','a wide first letter stands whole: '+w+'x'+h+' '+line);
    if(last){assert(cells(got.branch)>=cells(last.branch)&&cells(got.name)>=cells(last.name),'wider, yet shorter: '+(w-1)+' → '+w+' × '+h+'\n'+last.line+'\n'+line);}
    last={...got,line};}
}
assert.equal(rows,5280);   // (4 names × 4 branches × 5 sets of Git words × 2 heights × 33 widths)
// 40 × 52, the backend's cut branch and no upstream: one cell short of it, it is cut once more, keeping its mark.
assert(/^│  REPO hearting \(bl-git-index-lock-…\) │$/.test(row(40,52,'hearting',git('bl-git-index-lock-1…'))),row(40,52,'hearting',git('bl-git-index-lock-1…')));
""")

    def test_nothing_stands_where_it_did_not_and_every_row_is_as_wide_as_the_panel(self):
        self.node(r"""
let n=0;
for(const width of [59,44,30])for(const height of [52,40,24,14,10,8])for(const question of [false,true])for(const routes of [0,1,3])for(const messages of [0,3])for(const mem of [0,2])for(const usage of [false,true]){
 const v=drawW({width,height,question,routes,messages,mem,usage,status:status(),backlog:backlog()}),ls=lines(v);
 assert.equal(ls.length,height,[width,height]+'\n'+ls.join('\n'));assert(ls.every(l=>cells(l)===width),[width,height,question,routes]+'\n'+ls.join('\n'));
 // The names stand where a person looks: REPO at the first row; the group heads, section heads and the boxes' arrows at their ends.
 assert(/^│  REPO /.test(ls[1]),ls[1]);  // 11-26 (panel B): one cell in
 for(const l of ls.slice(1,-1)){const s=l.slice(2,-2).trimEnd();if(/^(MESSAGES|MEM)( |$)/.test(s))assert(/ ›$/.test(s),s);}
 n++;
}
assert(n>=800,String(n));
""")


if __name__ == "__main__":
    unittest.main()
