"""11-22 (the person, on the three drafts of the panel's tidy-up: "두칸이 아니라 한칸만 띄우도록 하자 그러면"): the line forms of the panel beside the native area.

1. A group head (REPO, SESSION) is its bold name, its value and a rule, no indent. The rows under a head stand one cell in (GIT, PEERS, STATUS, BACKLOG; MODEL, CTX, TITLE, NOW), the items of
   STATUS and BACKLOG two. An activity (QUESTIONS, ROUTE) is a rounded box with `›` at the right end of its top line. MESSAGES and MEM are their name and `›` at the right end, no rule, an item
   one cell in and a message's second row two. RESOURCES and USAGE are their name alone. Every row a press opens ends in that one `›` (STATUS, BACKLOG, NOW, QUESTIONS, ROUTE, MESSAGES, MEM).
2. Only the REPO and SESSION heads, the boxes and the last row's rule are lines; the line between CTX and TITLE is gone. `rule motion`'s light runs over the SESSION head's rule alone.
3. What a press opens is the row it was: the same actions on the same cells (a whole row for a head, an item, NOW, a box's top line), the REPO row's two buttons where the heads go.
4. What stands and the order in which rows give way are what they were: the indent only takes a cell from what a row shows (cut by the old rule), the line between CTX and TITLE was one row, and short of rows
   the SESSION head takes MODEL's value and row, so the head's name stands at every height; where MODEL's value would not stand whole on that row the head does not take it (the MODEL row stands in its own width).
5. A row `cut` or `tailed` ends in `…›` with `›` in the row's last cell and one `…` only, also where a two-cell character falls at the cut. Only the REPO and SESSION names carry weight (in mono and
   `NO_COLOR` too, where weight is the only cue); every other name is the title role's color without it.

11-25 (four groups, "저장소 먼저로 네 묶음 주고로 해줘"): the group heads are frame rows (`├╴[REPO] … ┤`, `├╴[SESSION] … ┤`), the light runs over the cells between them (30 at width 44; the cell before `┤` is
a plain `─`); MEM stands under BACKLOG (last of the REPO group), PEERS under TITLE; the list heads (STATUS, BACKLOG, MEM, MESSAGES) stand one cell in and are bold with the group names (the value names GIT, MODEL, CTX,
TITLE, PEERS, NOW are not); the items of MESSAGES and MEM two cells in, a message's body three; the boxes' count after the name (`QUESTIONS 1`); the label column from inner width 26; the empty rows between the
groups are the first thing given up.
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
const SHOWN=[
'├╴[REPO] hearting · wt 12 ─────────────────┤',
'│  GIT    branch ✓                         │',
'│  STATUS +2                             › │',
'│   ! 시험용 정할 것 하나                  │',
'│   · 시험용 하는 일 하나 · #101           │',
'│   · 시험용 하는 일 둘 — 제목이 길면 이… │',
'│  BACKLOG +11                           › │',
'│   · 시험용 나중 일 하나                  │',
'│   · 시험용 나중 일 둘                    │',
'│   · 시험용 나중 일 셋                    │',
'│  MEM                                   › │',
'│   ◆ 1m decision 시험용 기억 1 제목       │',
'│                                          │',
'├╴[SESSION] ───────────────────────────────┤',
'│  MODEL  Opus 5.5 xhigh                   │',
'│  CTX    ━━━━──── 41%                     │',
'│  TITLE  시험용 세션                      │',
'│  PEERS  [e9] ⚑  ─ [d4]                   │',
'│  NOW    시험용 NOW 한 줄               › │',
'│ ╭─ QUESTIONS 1 ───────────────────── ›─╮ │'];
assert.deepEqual(ls.slice(1,21).map(l=>l.replace(/ +│$/,' │')),SHOWN.map(l=>l.replace(/ +│$/,' │')),ls.join('\n'));
assert(ls.every(l=>cells(l)===44),ls.join('\n'));
// The lower sections: the name and `›` at the right end, no rule; an item two cells in (MEM, now the last of the REPO group), a message's body row four; the box's `›` at the end of its top line.
const at=re=>ls.findIndex(l=>re.test(l));
const m=at(/^│  MESSAGES +› │$/),e=at(/^│  MEM +› │$/),r=at(/^│ ╭─ ROUTE 1 ─+ ›─╮ │$/),se=at(/^├╴\[SESSION\]/);
assert(m>0&&e>0&&e<se&&r>0&&r<m,ls.join('\n'));
assert(/^│   ← \[77\] claude · 09:07 · report +│$/.test(ls[m+1])&&/^│    시험용 메시지 7 +│$/.test(ls[m+2])&&/^│   ◆ 1m decision 시험용 기억 1 제목 +│$/.test(ls[e+1]),ls.slice(m).join('\n'));
// The lines in the whole picture: the REPO and SESSION heads (frame rows), the two boxes' lines, the rule over the last row. No other.
const lined=ls.map((l,i)=>[i,l]).filter(([,l])=>/─{5}/.test(l)).map(([i,l])=>i);
assert(lined.every(i=>/^├╴\[(REPO|SESSION)\] |^│ [╭│╰]|^┌|^└|^│ ─+ │$/.test(ls[i])),lined.map(i=>ls[i]).join('\n'));
assert(!ls.some(l=>/^│ ─+ │$/.test(l)&&ls.indexOf(l)<ls.length-3),'no line between CTX and TITLE\n'+ls.join('\n'));
""")

    def test_the_indent_is_one_cell_and_a_value_is_cut_by_the_old_rule_in_the_cells_left(self):
        self.node(r"""
for(const width of [59,44,40,36,34,30,26]){
 const v=drawW({width,height:60,question:true,routes:1,messages:3,mem:2,status:status(),backlog:backlog()}),ins=lines(v).map(l=>l.slice(2,-2));
 const lead=re=>{const l=ins.find(s=>re.test(s));assert(l,width+' '+re+'\n'+ins.join('\n'));return l.length-l.trimStart().length;};
 const label=width-4>=26;
 for(const re of label?[/^ *GIT /,/^ *PEERS /,/^ *MODEL /,/^ *CTX /,/^ *TITLE /,/^ *NOW /]:[/^ *branch/,/^ *\[e9\]/,/^ *Opus 5\.5/,/^ *ctx 41%/,/^ *시험용 세션/])assert.equal(lead(re),1,width+' '+re);
 assert.equal(lead(/^ *STATUS/),1,String(width));assert.equal(lead(/^ *BACKLOG/),1,String(width));
 assert.equal(lead(/^ *! 시험용/),2,String(width));
 // The group heads are frame rows (`├╴[REPO] …┤`: the inside starts at the bracket); MESSAGES and MEM stand one cell in like STATUS, their items two.
 for(const re of [/^\[REPO\] /,/^\[SESSION\] /])assert(ins.some(s=>re.test(s)),width+' '+re);
 for(const re of [/^ MESSAGES/,/^ MEM /])assert.equal(lead(re),1,width+' '+re);
 assert(ins.some(s=>/^  [←→] \[/.test(s))&&ins.some(s=>/^  ◆ /.test(s)),width+'\n'+ins.join('\n'));
 assert(lines(v).every(l=>cells(l)===width));
}
// A long text is cut with `…` by the rule it had, in the cells the indent leaves: a STATUS item at 44 (36 inside, 34 for it) ends in `…` and fills its row.
{const v=drawW({width:44,height:60,status:{...status(),items:[{mark:'',text:'시험용 아주 긴 제목이라서 잘려야 합니다 시험용 아주 긴 제목',key:'i:x'}]}}),a=lines(v).map(l=>l.slice(2,-2)),i=a.findIndex(s=>/^ STATUS/.test(s));
 assert(a[i+1].startsWith('  · 시험용')&&a[i+1].trimEnd().endsWith('…')&&cells(a[i+1].trimEnd())>=34,a[i+1]);}
""")

    def test_a_row_a_press_opens_ends_in_one_arrow_at_the_right_end(self):
        self.node(r"""
const v=drawW({question:true,routes:1,messages:1,mem:1,status:status(),backlog:backlog(),height:52}),ls=lines(v),inn=ls.map(l=>l.slice(2,-2));
const endsIn=re=>{const l=inn.find(s=>re.test(s));assert(l,re+'\n'+ls.join('\n'));return l;};
for(const re of [/^ STATUS/,/^ BACKLOG/,/^ NOW /,/^ MESSAGES/,/^ MEM /])assert(/ ›$/.test(endsIn(re))||/^ NOW .*›$/.test(endsIn(re)),re+' '+endsIn(re));
for(const re of [/^ STATUS/,/^ BACKLOG/,/^ NOW /,/^ MESSAGES/,/^ MEM /])assert.equal([...endsIn(re)].filter(c=>c==='›').length,1,re);
assert(/ ›─╮$/.test(endsIn(/^╭─ QUESTIONS 1/))&&/ ›─╮$/.test(endsIn(/^╭─ ROUTE 1/)));
// The mark stands in the last cell of the row where a press lands (the one a person finds it in), and nowhere else on the rows around it.
for(const re of [/^ STATUS/,/^ BACKLOG/,/^ NOW /,/^ MESSAGES/,/^ MEM /])assert.equal(cells(endsIn(re)),40,String(re));
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
// Tall: `├╴[SESSION] ────…┤` (the rule runs from cell 12 to the last cell before `┤`, which is one plain `─` past the light's 30), the light on those 30 cells only, never on the frame cells or the name; no other row carries it.
{const v=drawW({question:true,routes:1,height:52,motion:'rule',state:'working',status:status(),backlog:backlog()}),ls=lines(v);
 const y=ls.findIndex(l=>/^├╴\[SESSION\] ─+┤$/.test(l));assert(y>0,ls.join('\n'));
 const run=ls[y].slice(12).match(/─+/)[0].length;assert.equal(run,31,ls[y]);
 const cs=v.motion.cells.filter(c=>c.rule!==undefined);
 assert.equal(cs.length,30);assert(cs.every((c,i)=>c.row===y&&c.col===12+i&&c.ch==='─'&&c.rule===i),JSON.stringify(cs.slice(0,3)));
 assert(!v.motion.cells.some(c=>c.rule===undefined&&c.row===y),'the name and the frame cells (`├╴` and `┤`) are not moved');
 // The light goes round and comes back: some frame lights cells, some frame (the rest) lights none; the next change is asked for from the rule's own length.
 const lit=f=>v.motion.at(989+f).filter(p=>p.tone==='scan').length;   // (`since` is 990 and a rule's own tick is one on: the host's frame 989+f is the rule's tick f)
 assert(lit(3)>0&&lit(13)===0&&lit(26)===0&&v.motion.next(992)===993);
 for(const f of [0,5,13,20]){const a=v.motion.at(989+f);assert.equal(a.length,v.motion.cells.length);}
 assert(!ls.some((l,i)=>i!==y&&v.rows[i].some(p=>p.rule)),'one row carries the rule');}
// Short of rows the head takes MODEL's value and its row: the rule is the rest of that row, and the light runs over it (not over `[SESSION] Opus 5.5 xhigh`).
{const v=drawW({question:true,routes:1,messages:1,mem:1,height:24,motion:'rule',state:'working',status:status(),backlog:backlog()}),ls=lines(v);
 const y=ls.findIndex(l=>/^├╴\[SESSION\] Opus 5\.5 xhigh ─+┤$/.test(l));assert(y>0,ls.join('\n'));
 const first=ls[y].indexOf('─',10),cs=v.motion.cells.filter(c=>c.rule!==undefined);
 assert(cs.length>=3&&cs.every(c=>c.row===y&&c.col>=first&&c.ch==='─')&&cs.length===[...ls[y].slice(first)].filter(c=>c==='─').length-1,JSON.stringify(cs.slice(0,2)));}
// Still: the plain rule in every other mode (border, scanners, off and a waiting frame keep a rule that does not move).
for(const [motion,state] of [['border','working'],['scanners','working'],['off','working'],['rule','idle'],['rule','blocked']]){
 const v=drawW({height:52,motion,state}),ls=lines(v),y=ls.findIndex(l=>/^├╴\[SESSION\] ─+┤$/.test(l));assert(y>0,motion+state);
 const parts=v.rows[y].filter(p=>p.rule);assert(parts.length===1&&parts[0].text==='─'.repeat(30)&&parts[0].role==='messages'&&parts[0].dim===true,motion+state+JSON.stringify(parts));
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
// 11-25 (four groups): the group names (`[REPO]`, `[SESSION]`) and the list heads (STATUS, BACKLOG, MEM, MESSAGES) are bold; the value names (GIT, MODEL, CTX, TITLE, PEERS, NOW) are not.
const boldOf=(v,word)=>v.rows.flatMap(r=>r.filter(p=>p.text.trim()===word)).map(p=>p.bold===true);
for(const [name,cfg,extra] of [['terminal',{theme:'terminal'},{}],['light',{theme:'light'},{}],['mono',{theme:'mono'},{}],['no_color',{theme:'terminal'},{no_color:true}]])
for(const h of [60,12]){
 const v=view.render({width:44,height:h,side:true,model:{...model({routes:1,messages:1,mem:1,status:status(),backlog:backlog()}),...extra},state:'working',harness:'claude',frame:1000,since:990,sent:()=>[],cellWidth:cells,
  configuration:{side_width:44,rule_motion:'off',position:'right',...cfg}}),tag=name+' '+h+'\n'+lines(v).join('\n');
 assert.deepEqual(boldOf(v,'[REPO]'),[true],'REPO '+tag);
 // The SESSION name stands alone (a head) or before MODEL's value (the merged head): the same part either way.
 assert.deepEqual(boldOf(v,'[SESSION]'),[true],'SESSION '+tag);
 // The list heads: bold (at body brightness) where they stand as heads; at low height STATUS and BACKLOG are the REPO head's buttons, not heads.
 for(const w of h>=40?['STATUS','BACKLOG','MEM','MESSAGES']:['MESSAGES'])assert.deepEqual(boldOf(v,w),[true],w+' '+tag);
 const tiers=v.rows.flatMap(r=>r.filter(p=>['[REPO]','[SESSION]','STATUS','MESSAGES'].includes(p.text.trim())&&p.tier)).map(p=>[p.text.trim(),p.tier]);
 assert(tiers.every(([w,t])=>t===(w[0]==='['?'group':'list')),JSON.stringify(tiers));
 for(const w of ['GIT','NOW','TITLE','MODEL','CTX','PEERS'])assert(boldOf(v,w).every(b=>!b),w+' '+tag);
}
// Short of rows the head is merged with MODEL's value: the name is bold there too.
{const v=drawW({height:14,question:true,mem:3,status:status(),backlog:backlog()});assert(lines(v).some(l=>/^├╴\[SESSION\] Opus 5\.5 xhigh/.test(l)),lines(v).join('\n'));assert.deepEqual(boldOf(v,'[SESSION]'),[true]);}
""")

    def test_a_model_value_that_does_not_stand_whole_does_not_merge_into_the_head(self):
        self.node(r"""
const LONG=[t('GPT-5.5-codex-max','hb_claude'),t(' xhigh')];
let merged=0,alone=0;
for(const w of [44,36,32,30])for(const [sel,whole] of [[LONG,'GPT-5.5-codex-max xhigh'],[model().selected,'Opus 5.5 xhigh']])for(let h=8;h<=40;h++)for(const question of [false,true])for(const mem of [0,3]){
 const v=drawW({width:w,height:h,question,mem,selected:sel,status:status(),backlog:backlog()}),ls=lines(v),a=inside(v),tag=w+'x'+h+'\n'+ls.join('\n');
 assert.equal(ls.length,h,tag);assert(ls.every(l=>cells(l)===w),tag);
 // A merged head holds the value whole; else the head row goes and the MODEL row stands (its value cut by its own width, as before the head was a group's).
 const head=a.find(s=>/^\[SESSION\] [^─]/.test(s));
 if(head){merged++;assert(head.includes(whole),'merged, whole\n'+tag);}
 else if(a.some(s=>/^\[SESSION\]/.test(s)))alone++;
 else assert(a.some(s=>/^ MODEL |^ GPT|^ Opus/.test(s)),'MODEL row stands\n'+tag);
}
assert(merged>0&&alone>0,merged+' '+alone);
// The reviewer's picture: 28 wide (no label column: it stands from inner width 26 now), 14 high, a long model name: the MODEL row keeps the name whole.
{const a=inside(drawW({width:28,height:14,question:true,mem:3,selected:LONG,status:status(),backlog:backlog()}));
 assert(!a.some(s=>/^\[SESSION\] GPT/.test(s))&&a.some(s=>s.includes('GPT-5.5-codex-max xhigh')),a.join('\n'));}
// Where the label column stands (32 wide) the MODEL row cuts the value by its own width and the head does not take it.
{const a=inside(drawW({width:32,height:14,question:true,mem:3,selected:LONG,status:status(),backlog:backlog()}));
 assert(!a.some(s=>/^\[SESSION\] GPT/.test(s))&&a.some(s=>/^ MODEL  GPT-5\.5-codex… xhigh$/.test(s)),a.join('\n'));}
""")

    def test_the_session_head_stays_and_the_rows_give_way_in_the_order_they_did(self):
        self.node(r"""
const o={question:true,routes:1,messages:3,mem:3,status:status(),backlog:backlog()};
const seen=[];
for(let h=70;h>=12;h--){
 const v=drawW({...o,height:h}),ls=lines(v),a=ls.map(l=>l.slice(2,-2).trimEnd());
 assert.equal(ls.length,h,h+'\n'+ls.join('\n'));
 // The group's name is on screen at every height (alone with its rule, or with MODEL's value on MODEL's row), and no row is the line between CTX and TITLE.
 assert(a.some(s=>/^\[SESSION\]( |$)/.test(s)),h+'\n'+ls.join('\n'));
 const alone=a.some(s=>/^\[SESSION\] ─+$/.test(s)),merged=a.some(s=>/^\[SESSION\] Opus 5\.5 xhigh/.test(s));
 assert(alone!==merged,h+'\n'+ls.join('\n'));
 if(merged)assert(!a.some(s=>/^ MODEL /.test(s)),'the head takes MODEL\'s row\n'+ls.join('\n'));
 assert(!a.slice(1,-3).some(s=>/^─+$/.test(s)),h+'\n'+ls.join('\n'));
 seen.push({h,alone,bItems:a.filter(s=>/^  · 시험용 나중/.test(s)).length,sItems:a.filter(s=>/^  [!·] 시험용 (정할|하는)/.test(s)).length,heads:a.filter(s=>/^ (STATUS|BACKLOG)\b/.test(s)).length,
  gap:a.filter(s=>!s.trim()).length,msg:a.filter(s=>/^  [←→] \[/.test(s)).length,body:a.filter(s=>/^   \S/.test(s)&&!/^   [!·] /.test(s)).length,git:a.some(s=>/^ GIT /.test(s)),title:a.some(s=>/^ TITLE /.test(s))});
}
const first=f=>{const x=seen.find(f);return x?x.h:0;};
const order=[first(r=>r.bItems<3),first(r=>r.sItems<3),first(r=>!r.heads),first(r=>!r.alone),first(r=>!r.git)];
// BACKLOG items first, then the STATUS items, then the two heads (the REPO row takes the buttons), then the SESSION head's own row (it takes MODEL's), then Git: each no earlier than the next. TITLE stays to 12 rows.
assert(seen.every(r=>r.title),'TITLE stands at every height');
assert(order.every((x,i)=>x>0&&(!i||x<=order[i-1])),JSON.stringify(order));
assert(order[0]>order[1]&&order[1]>order[2],JSON.stringify(order));
// 11-25: the empty rows between the groups are the first thing given up, before BACKLOG loses an item.
assert(first(r=>r.gap===0)>order[0],JSON.stringify([first(r=>r.gap===0),order]));
// The merged head costs no row more than the head that went: the head row is MODEL's row.
const tall=lines(drawW({...o,height:60})).length,short=lines(drawW({...o,height:30})).length;assert.equal(tall,60);assert.equal(short,30);
""")

    def test_the_presses_are_where_they_were(self):
        self.node(r"""
const v=drawW({question:true,routes:1,messages:3,mem:2,status:status(),backlog:backlog(),height:60}),ls=lines(v),a=ls.map(l=>l.slice(2,-2));
const rowOf=re=>a.findIndex(s=>re.test(s));
const act=i=>v.hits.filter(h=>h.row===i).map(h=>[h.left,h.right,h.action]);
// A whole row for a head, an item, NOW (every row of it), a box's top line, MESSAGES and MEM heads and each of their rows; none on the group heads, GIT, PEERS, MODEL, CTX or TITLE.
const sh=rowOf(/^ STATUS/),bh=rowOf(/^ BACKLOG/);
assert.deepEqual(act(sh),[[2,41,'popup:STATUS']]);assert.deepEqual(act(sh+1),[[2,41,'popup:STATUS:i:a1']]);assert.deepEqual(act(bh+3),[[2,41,'popup:BACKLOG:i:b3']]);
for(const re of [/^\[REPO\] /,/^ GIT /,/^ PEERS /,/^\[SESSION\] /,/^ MODEL /,/^ CTX /,/^ TITLE /])assert.deepEqual(act(rowOf(re)),[],String(re));
assert.deepEqual(act(rowOf(/^ NOW /)),[[2,41,'popup:NOW']]);
assert.deepEqual(act(rowOf(/^╭─ QUESTIONS 1/)),[[2,41,'question']]);assert.deepEqual(act(rowOf(/^╭─ ROUTE 1/)),[[2,41,'popup:ROUTE']]);
const mh=rowOf(/^ MESSAGES/),eh=rowOf(/^ MEM /);
assert.deepEqual(act(mh),[[2,41,'popup:MESSAGES']]);assert.deepEqual(act(mh+1),[[2,41,'popup:MESSAGES']]);assert.deepEqual(act(eh),[[2,41,'popup:MEM']]);assert.deepEqual(act(eh+1),[[2,41,'popup:MEM:0']]);assert.deepEqual(act(eh+2),[[2,41,'popup:MEM:1']]);
// The REPO row where the heads go: the two buttons on the same cells as before (the row's own value is cut by the width it always was).
{const w=drawW({question:true,routes:3,messages:7,mem:5,height:14,status:status(3,5),backlog:backlog(3,14)}),l=lines(w);
 assert(/^├╴\[REPO\] hearting +STATUS ›  BACKLOG ›─┤$/.test(l[1])||/^├╴\[REPO\] hearting · wt 12 +STATUS \+5 ›  BACKLOG \+14 ›─┤$/.test(l[1]),l[1]);
 const p=w.hits.filter(h=>h.row===1);assert.deepEqual(p.map(h=>h.action),['popup:STATUS','popup:BACKLOG']);assert.equal(p[0].left,2);assert.equal(p[1].right,44-3);assert.equal(p[0].right+1,p[1].left);}
""")

    def test_nothing_stands_where_it_did_not_and_every_row_is_as_wide_as_the_panel(self):
        self.node(r"""
let n=0;
for(const width of [59,44,30])for(const height of [52,40,24,14,10,8])for(const question of [false,true])for(const routes of [0,1,3])for(const messages of [0,3])for(const mem of [0,2])for(const usage of [false,true]){
 const v=drawW({width,height,question,routes,messages,mem,usage,status:status(),backlog:backlog()}),ls=lines(v);
 assert.equal(ls.length,height,[width,height]+'\n'+ls.join('\n'));assert(ls.every(l=>cells(l)===width),[width,height,question,routes]+'\n'+ls.join('\n'));
 // The names stand where a person looks: REPO at the first row; the group heads, section heads and the boxes' arrows at their ends.
 assert(/^├╴\[REPO\] /.test(ls[1]),ls[1]);
 for(const l of ls.slice(1,-1)){const s=l.slice(2,-2).trimEnd();if(/^ (MESSAGES|MEM)( |$)/.test(s))assert(/ ›$/.test(s),s);}
 n++;
}
assert(n>=800,String(n));
""")


if __name__ == "__main__":
    unittest.main()
