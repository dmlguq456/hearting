"""11-21 (the person: "버튼을 REPO의 아래에 각각을 두고 +? 숫자 띄우고 백로그랑 현황을 나열하자" / "각각 3줄 정도씩 목록으로 만들고 추가분을 +? 형식으로"):
the panel's STATUS part and BACKLOG part under the REPO group, and the BACKLOG popup.

1. Beside the native area the REPO group ends in two parts, each a section head (`STATUS +2 ──── ›`, a press opens that popup) and its first three items (one row each,
   `!` before what waits, `·` before the rest; a press on one opens the popup standing on it). `+N` on the head counts the items not shown, as MESSAGES' does (none for
   zero). No item: the head alone. No document: the word dim, nothing under it, still pressable. The `STATUS ›` button on the REPO row is gone.
2. Short of rows the BACKLOG items go first (3, 2, 1, none), then the order MESSAGES and MEM already had, then the STATUS items; NOW, QUESTIONS and the route box
   never give up a row for the new parts (they go on MESSAGES, MEM, the SESSION head, then Git, PEERS and the two heads together: a head never stands without Git).
3. The bottom layout has no list: its first row ends in `STATUS +N ›  BACKLOG +N ›` and the panel is no taller.
4. The two parts' items come with the status value (one read, the document read again only where it changed: `have`), and the panel's drawing reads no file.
5. The BACKLOG popup is the status popup's list screen (`status-list.cjs`) over `<artifact root>/backlog.md`; its groups are the document's `##` headings as written.
6. (From the review of #1793) a list dropped for its size leaves no stale style index; the document's rows are made only where the list is dropped.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
NODE = shutil.which("node")
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

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
    selected:[t('Opus 5.5','hb_claude'),t(' xhigh')],context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],context:[t('ctx 41%','lvl_g')],quota:[t('5h ','dim'),t('23%','lvl_g')],
    session_title:'시험용 세션',now:'시험용 NOW 한 줄',route_label:routes.length?'route '+routes.length:undefined,route_count:routes.length,route_total:routes.length,routes,
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
"""


@unittest.skipUnless(NODE, "needs node")
class PanelListsTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_picture_under_the_repo_group_and_the_count_of_what_is_not_shown(self):
        self.node(r"""
const v=draw({status:status(),backlog:backlog(),routes:1,messages:3,mem:2,height:40}),ls=lines(v);
const want=[
'│ REPO   hearting · wt 12 ─────────────────────────────── │',
'│ GIT    branch ✓                                         │',
'│ PEERS  [e9] ⚑  ─ [d4]                                   │',
'│ STATUS +2 ─────────────────────────────────────────── › │',
'│ ! 시험용 정할 것 하나                                   │',
'│ · 시험용 하는 일 하나 · #101                            │',
'│ · 시험용 하는 일 둘 — 제목이 길면 이렇게 잘린다 이렇게… │',
'│ BACKLOG +11 ───────────────────────────────────────── › │',
'│ · 시험용 나중 일 하나                                   │',
'│ · 시험용 나중 일 둘                                     │',
'│ · 시험용 나중 일 셋                                     │',
'│ SESSION ─────────────────────────────────────────────── │'];
assert.deepEqual(ls.slice(1,13),want,ls.join('\n'));
assert(ls.every(l=>cells(l)===59));
// `+N` is the head's own text in the MESSAGES head's colour (`loc_repo`); the mark of what waits is the status list's own yellow.
const row=v.rows[4],plus=row.find(p=>p.text==='+2');assert(plus&&plus.foreground===253,JSON.stringify(row));
{const w=v.rows[5].find(p=>p.text==='! '),d=v.rows[6].find(p=>p.text==='· ');assert(w&&w.foreground===186&&d&&d.dim===true,JSON.stringify([v.rows[5],v.rows[6]]));}
// No `+N` where every item shows (the total is the three), and none for a total that was not told.
{const a=inside(draw({status:status(3,3),backlog:backlog(2,2),height:40}));
 assert(a.some(l=>/^STATUS ─+ ›$/.test(l))&&a.some(l=>/^BACKLOG ─+ ›$/.test(l)),a.join('\n'));}
{const a=inside(draw({status:{word:'STATUS',has_doc:true,items:SI},backlog:{word:'BACKLOG',has_doc:true},height:40}));
 assert(a.some(l=>/^STATUS ─+ ›$/.test(l))&&a.some(l=>/^BACKLOG ─+ ›$/.test(l))&&a.filter(l=>/^[!·] /.test(l)).length===3,a.join('\n'));}
// No item: the head alone (no empty row). A total of nothing and a total with no items shown (the host holds the count only) say `+N`.
{const a=inside(draw({status:status(0,0),backlog:backlog(0,0),height:40}));
 const at=a.findIndex(l=>/^STATUS/.test(l));assert(/^BACKLOG/.test(a[at+1])&&/^SESSION/.test(a[at+2]),a.join('\n'));}
{const a=inside(draw({status:status(0,7),backlog:backlog(0,9),height:40}));assert(a.some(l=>/^STATUS \+7 ─+ ›$/.test(l))&&a.some(l=>/^BACKLOG \+9 ─+ ›$/.test(l)),a.join('\n'));}
// The REPO row has no button now, and the heads stand between PEERS and SESSION.
{const a=inside(draw({status:status(),backlog:backlog(),height:40}));assert(!a[1].includes('›')&&/^REPO   hearting · wt 12 ─+$/.test(a[1]),a[1]);
 const at=n=>a.findIndex(l=>l.startsWith(n));assert(at('PEERS')<at('STATUS')&&at('STATUS')<at('BACKLOG')&&at('BACKLOG')<at('SESSION'));}
""")

    def test_a_missing_document_is_a_dim_word_and_no_root_has_no_part(self):
        self.node(r"""
// No document: the word plain dim (not the title role), `›` dim, no item, and a press still opens the popup.
{const v=draw({status:status(0,0,false),backlog:backlog(0,0,false),height:40}),a=inside(v);
 for(const [word,action] of [['STATUS','popup:STATUS'],['BACKLOG','popup:BACKLOG']]){
  const at=a.findIndex(l=>l.startsWith(word));assert(at>0);
  const w=v.rows[at].find(p=>p.text===word),arrow=v.rows[at].findLast(p=>p.text==='›');
  assert(w.dim===true&&w.role===undefined&&arrow.dim===true,JSON.stringify(v.rows[at]));
  assert(v.hits.some(h=>h.row===at&&h.action===action));}
 assert(!a.some(l=>/^[!·] /.test(l)));}
// With a document the word has the title role (as the other sections' heads).
{const v=draw({status:status(),backlog:backlog(),height:40});const at=inside(v).findIndex(l=>l.startsWith('STATUS'));assert.equal(v.rows[at].find(p=>p.text==='STATUS').role,'title');}
// Only one of the two parts has a value: only that one stands. No value at all (no artifact root): nothing, no press.
{const a=inside(draw({status:status(),height:40}));assert(a.some(l=>l.startsWith('STATUS'))&&!a.some(l=>l.startsWith('BACKLOG')));}
for(const status_doc of [undefined,null,{word:'',has_doc:true}]){const v=draw({status:status_doc,backlog:status_doc,height:40}),a=inside(v);
 assert(!a.some(l=>/^(STATUS|BACKLOG)/.test(l)),JSON.stringify(status_doc));assert(!v.hits.some(h=>/STATUS|BACKLOG/.test(h.action)));}
""")

    def test_presses_on_a_head_open_its_popup_and_on_an_item_open_it_standing_on_that_item(self):
        self.node(r"""
const v=draw({status:status(),backlog:backlog(),height:40}),a=inside(v);
const at=re=>a.findIndex(l=>re.test(l));
const sh=at(/^STATUS/),bh=at(/^BACKLOG/);
const acts=v.hits.filter(h=>/popup:(STATUS|BACKLOG)/.test(h.action)).map(h=>[h.row,h.action]);
assert.deepEqual(acts,[[sh,'popup:STATUS'],[sh+1,'popup:STATUS:i:a1'],[sh+2,'popup:STATUS:i:a2'],[sh+3,'popup:STATUS:i:a3'],
  [bh,'popup:BACKLOG'],[bh+1,'popup:BACKLOG:i:b1'],[bh+2,'popup:BACKLOG:i:b2'],[bh+3,'popup:BACKLOG:i:b3']],JSON.stringify(acts));
// A press stands for the whole row; no press on the REPO, GIT, PEERS or SESSION rows.
assert(v.hits.filter(h=>/popup:(STATUS|BACKLOG)/.test(h.action)).every(h=>h.left===2&&h.right===56));
assert(!v.hits.some(h=>h.row<sh&&/STATUS|BACKLOG/.test(h.action)));
// An item with no key opens the popup at its top: the head's own press.
{const w=draw({status:{...status(),items:[{mark:'',text:'시험용 키 없음'}]},backlog:backlog(0,0),height:40});
 assert(w.hits.some(h=>h.action==='popup:STATUS'&&inside(w)[h.row]==='· 시험용 키 없음'),JSON.stringify(w.hits));}
""")

    def test_narrow_panels_cut_the_head_and_the_items(self):
        self.node(r"""
for(const width of [59,44,40,36,32,30]){
  const v=draw({width,status:status(),backlog:backlog(),height:50}),ls=lines(v),a=inside(v);
  assert(ls.every(l=>cells(l)===width),width+'\n'+ls.join('\n'));
  const sh=a.findIndex(l=>/^STATUS/.test(l)),bh=a.findIndex(l=>/^BACKLOG/.test(l));
  assert(sh>0&&bh===sh+4,width+'\n'+ls.join('\n'));
  assert(/^STATUS( \+2)? ─+ ›$/.test(a[sh])&&/^BACKLOG( \+11)? ─+ ›$/.test(a[bh]),a[sh]+'|'+a[bh]);
  assert.equal(a[sh+1].slice(0,2),'! ');assert(a[sh+3].endsWith('…'),a[sh+3]);   // the long title is cut at the width
}
// The least a head is: `STATUS +2 ─ ›` (the rule and the count give way to nothing but a space and `›` before the word does).
{const v=draw({width:24,status:status(),backlog:backlog(),height:50}),a=inside(v);assert(a.some(l=>/^STATUS( \+2)? [─ ]*›$/.test(l)),a.join('\n'));}
// Hangul is two cells: a row never passes the panel.
{const v=draw({width:30,status:{...status(),items:[{mark:'!',text:'한글로만 이루어진 아주 긴 제목이라서 잘려야 합니다 한글로만',key:'i:x'}]},backlog:backlog(0,0),height:50});
 assert(lines(v).every(l=>cells(l)===30));}
""")

    def test_short_of_rows_the_items_give_way_in_their_order_and_the_other_three_keep_their_rows(self):
        self.node(r"""
const base={routes:3,messages:7,mem:5,question:true};
const pick=v=>{const a=inside(v);return {b:a.filter(l=>/^· 시험용 나중 일/.test(l)).length,s:a.filter(l=>/^[!·] 시험용 (정할|하는)/.test(l)).length,
  msg:a.filter(l=>/^[←→] /.test(l)).length,two:a.filter(l=>/^  시험용 메시지/.test(l)).length,mem:a.filter(l=>/^◆ /.test(l)).length,
  heads:a.filter(l=>/^(STATUS|BACKLOG)\b/.test(l)).length,git:a.some(l=>/^GIT /.test(l)),peers:a.some(l=>/^PEERS /.test(l)),
  now:a.filter(l=>/^NOW /.test(l)).length,q:a.filter(l=>/◑|1 가 · 2 나|↵ answer|QUESTIONS/.test(l)).length,route:a.filter(l=>(/^│ .*│$/.test(l)&&!/◑|1 가|↵ answer/.test(l))||/^╭─ \d+ ROUTE/.test(l)).length};};
const seen=[];
for(let h=70;h>=10;h--){const v=draw({...base,height:h,status:status(),backlog:backlog()});seen.push({h,...pick(v)});assert.equal(lines(v).length,h);}
const full=seen[0];assert.deepEqual([full.b,full.s,full.heads],[3,3,2],JSON.stringify(full));
for(const r of seen){
  // Items go in the order BACKLOG, then (after MESSAGES is down to one message and MEM to one record) STATUS.
  if(r.msg>1||r.two>0)assert.equal(r.b===3||r.h>=40,true,'BACKLOG goes before the messages fold: '+JSON.stringify(r));
  if(r.s<3&&r.heads>0)assert(r.b===0&&r.msg<=1&&r.mem<=1,JSON.stringify(r));
  if(r.b<3)assert(r.heads>=0&&true);
  // A head never stands without Git, and the heads go together with Git and PEERS.
  if(r.heads>0)assert(r.git,JSON.stringify(r));
  if(!r.git)assert.equal(r.heads,0,JSON.stringify(r));
  // Items stand under a head only.
  if(r.heads===0)assert.deepEqual([r.b,r.s],[0,0],JSON.stringify(r));
}
// The lowest height with the three kept: NOW, QUESTIONS and the route box have, with the parts on, at least the rows they have with the parts off.
const rowsOf=h=>pick(draw({...base,height:h}));
for(const [w,extra] of [[59,{}],[40,{}],[30,{}]])for(const h of [34,24,20,14])for(const question of [false,true])for(const routes of [1,3])for(const messages of [3,7])for(const mem of [2,5]){
  const o={width:w,height:h,question,routes,messages,mem},off=pick(draw(o)),on=pick(draw({...o,status:status(),backlog:backlog()}));
  assert(on.now>=off.now&&on.q>=off.q&&on.route>=off.route,JSON.stringify({o,off,on}));
}
""")

    def test_the_bottom_layout_has_two_buttons_and_no_list_and_is_no_taller(self):
        self.node(r"""
const two=(w,h,s,b,o={})=>draw({side:false,width:w,height:h,status:{word:'STATUS',has_doc:true,items:SI,total:s},backlog:{word:'BACKLOG',has_doc:true,items:BI,total:b},...o});
for(const w of [120,80,60]){const v=two(w,8,5,14),ls=lines(v);
  assert(/STATUS( \+5)? ›  BACKLOG( \+14)? › │$/.test(ls[1])||/ ›  › │$/.test(ls[1]),w+' '+ls[1]);
  assert(ls.every(l=>cells(l)===w));
  assert(!ls.some(l=>/^│ [!·] /.test(l)),ls.join('\n'));
  const [a,b]=['popup:STATUS','popup:BACKLOG'].map(x=>v.hits.find(h=>h.row===1&&h.action===x));
  assert(a&&b&&a.left===2&&b.right===w-3&&a.right+1===b.left,JSON.stringify(v.hits));
  // No taller than the panel without them, whatever the height.
  for(const h of [3,4,5,6,8,12]){assert.equal(lines(two(w,h,5,14)).length,lines(draw({side:false,width:w,height:h})).length);}}
// The counts go first, then the words: 120 and 80 whole, 60 the words only, 52 the arrows only.
assert.match(lines(two(120,8,5,14))[1],/ctx 41% +STATUS \+5 ›  BACKLOG \+14 › │$/);
assert.match(lines(two(80,8,5,14))[1],/ctx 41% +STATUS \+5 ›  BACKLOG \+14 › │$/);
assert.match(lines(two(60,8,5,14))[1],/ctx 41% +STATUS ›  BACKLOG › │$/);
assert.match(lines(two(52,8,5,14))[1],/ctx 41% +›  › │$/);
// A zero count is not written.
assert.match(lines(two(120,8,0,0))[1],/ctx 41% +STATUS ›  BACKLOG › │$/);
""")

    def test_the_drawing_reads_no_file_and_asks_nothing(self):
        out = self.node(r"""
const calls=[];
for(const name of Object.getOwnPropertyNames(fs))if(typeof fs[name]==='function'&&/^(read|stat|lstat|open|access|exists|readdir|realpath)/.test(name)){
  const real=fs[name];fs[name]=function(...a){calls.push(name);return real.apply(this,a);};}
for(let i=0;i<60;i++)for(const side of [true,false])draw({side,width:side?59:100,height:side?34:8,status:status(),backlog:backlog(),routes:1,messages:3,mem:2});
console.log(JSON.stringify({file_reads:calls.length}));
""")
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {"file_reads": 0})


# -- the backend: the two short lists, the backlog document and its popup -------------------------------------------------------

BACKLOG = """# 시험용 백로그
갱신 2026-11-21 11:02

## 2026-11-02_some-campaign
- 일 하나
  date: 2026-11-20
  source: 어딘가
  note: 맥락 하나
- 일 둘
  - 하위 항목은 일이 아니다

## 2026-10-01_other-campaign
- 다른 일

```
## 코드 울타리 안
- 세지 않는다
```

## Unassigned
- 정해지지 않은 일
"""

STATUS_PAGE = """<!-- hearting:campaign-document v1 -->
# 시험용 캠페인

갱신 2026-11-21 11:02

## 하는 중
- 하는 일 A
- 하는 일 B
- 하는 일 C
- 하는 일 D

## 기다리는 것
- 정할 것 하나
- 정할 것 둘

## 최근에 끝난 것
- 끝난 일
앞서 끝난 기록 3개는 접어 둠.
"""


class BackendTest(unittest.TestCase):
    def setUp(self):
        from fleet import session_status
        import session_shell
        self.status, self.shell = session_status, session_shell
        self._tmp = tempfile.TemporaryDirectory(prefix="rl")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        patch = mock.patch.object(session_status, "_root", lambda cwd, now: str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        self.cwd = str(self.root)

    def backlog(self, text=BACKLOG):
        path = self.root / "backlog.md"
        path.write_text(text, encoding="utf-8")
        return path

    def page(self, name="2026-11-02_some-campaign", text=STATUS_PAGE):
        folder = self.root / "campaigns" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "campaign.md").write_text(text, encoding="utf-8")
        return {"path": str(folder / "campaign.md"), "has_doc": True}

    # (rule 5 of the form) ##-headings are the groups as written; a top bullet is an item; indented lines are its detail; fences are not read.
    def test_the_backlog_is_read_by_its_headings_bullets_and_indented_lines(self):
        parsed = self.status.parse_backlog(BACKLOG.splitlines())
        self.assertEqual(parsed["title"], "시험용 백로그")
        self.assertEqual(parsed["updated"], (2026, 11, 21, 11, 2))
        self.assertEqual([(g["name"], [i["text"] for i in g["items"]]) for g in parsed["groups"]],
                         [("2026-11-02_some-campaign", ["일 하나", "일 둘"]), ("2026-10-01_other-campaign", ["다른 일"]), ("Unassigned", ["정해지지 않은 일"])])
        self.assertEqual(parsed["groups"][0]["items"][0]["detail"], ["date: 2026-11-20", "source: 어딘가", "note: 맥락 하나"])
        self.assertEqual(parsed["groups"][0]["items"][1]["detail"], ["- 하위 항목은 일이 아니다"])      # a sub-bullet is detail, not an item
        # A document of another form does not break: what is read is shown, and no headings or no bullets is no item.
        for body in ("", "그냥 글\n", "- 머리말 없는 글머리표\n", "## 머리말만\n", "# 제목만\n\n갱신 2026-11-21 11:02\n", "\x00\x01 ## \n- \n"):
            parsed = self.status.parse_backlog(body.splitlines())
            self.assertEqual(sum(len(g["items"]) for g in parsed["groups"]), 0, body)
        # The same heading twice: two groups in the document's order.
        parsed = self.status.parse_backlog("## A\n- 1\n## A\n- 2\n- 3\n".splitlines())
        self.assertEqual([[i["text"] for i in g["items"]] for g in parsed["groups"]], [["1"], ["2", "3"]])

    def test_the_panels_backlog_items_are_this_windows_campaign_first_then_the_rest_and_the_count_is_the_whole_documents(self):
        parsed = self.status.parse_backlog(BACKLOG.splitlines())
        items, total = self.status.panel_backlog_items(parsed, "2026-11-02_some-campaign")
        self.assertEqual([i["text"] for i in items], ["일 하나", "일 둘", "다른 일"])
        self.assertEqual(total, 4)
        items, total = self.status.panel_backlog_items(parsed, "2026-10-01_other-campaign")
        self.assertEqual([i["text"] for i in items], ["다른 일", "일 하나", "일 둘"])
        items, total = self.status.panel_backlog_items(parsed, "")                         # no campaign known: the document's order
        self.assertEqual([i["text"] for i in items], ["일 하나", "일 둘", "다른 일"])
        items, total = self.status.panel_backlog_items(parsed, "2026-11-02_some-campaign-2")    # the name is the folder's, letter for letter
        self.assertEqual([i["text"] for i in items], ["일 하나", "일 둘", "다른 일"])
        self.assertTrue(all(i["mark"] == "" and i["key"].startswith("i:") for i in items))

    def test_the_panels_status_items_are_the_list_screens_order_waiting_then_in_progress_with_its_marks_and_keys(self):
        lines = STATUS_PAGE.splitlines()
        parsed = self.status.parse_listing(lines)
        items, total = self.status.panel_items(parsed)
        self.assertEqual([(i["mark"], i["text"]) for i in items], [("!", "정할 것 하나"), ("!", "정할 것 둘"), ("", "하는 일 A")])
        self.assertEqual(total, 6)                                                         # waiting 2 + in progress 4; the finished and the folded count nothing
        # The keys are the list screen's own: a press on the item can stand on it there.
        entries = self.status.listing_entries(parsed, 80)
        have = {entry["key"] for entry in entries if entry.get("key")}
        self.assertTrue({i["key"] for i in items} <= have)
        # One reading: the same items as the list screen draws, in the order it draws them.
        drawn = [text_of(entry["rows"][0]) for entry in entries if entry.get("key", "").startswith("i:")]
        self.assertEqual([t.strip().lstrip("!").strip() for t in drawn[:3]], [i["text"] for i in items])

    def test_the_lists_are_read_with_the_value_and_a_document_that_did_not_change_is_not_read_again(self):
        view = self.page()
        self.backlog()
        got = self.status.panel_lists(self.cwd, view, {}, now=time.time())
        self.assertEqual(got["status"]["total"], 6)
        self.assertEqual(len(got["status"]["items"]), 3)
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["total"]), (True, 4))
        # Asked again with the signs it gave: nothing is read or parsed.
        have = {"status": got["status"]["sign"], "backlog": got["backlog"]["sign"]}
        with mock.patch.object(self.status, "read_text", side_effect=AssertionError("read")), \
                mock.patch.object(self.status, "read_backlog", side_effect=AssertionError("read")), \
                mock.patch.object(self.status, "parse_listing", side_effect=AssertionError("parsed")), \
                mock.patch.object(self.status, "parse_backlog", side_effect=AssertionError("parsed")):
            again = self.status.panel_lists(self.cwd, view, have, now=time.time())
        self.assertEqual(again, {"status": {"same": True}, "backlog": {"same": True}})
        # A change (modification time or size) reads it again; the sign is text (a time in nanoseconds does not pass JSON numbers).
        self.assertIsInstance(got["backlog"]["sign"], str)
        path = self.backlog(BACKLOG + "- 더\n")
        os.utime(path, (1_800_000_000, 1_800_000_000))
        self.assertNotEqual(self.status.panel_lists(self.cwd, view, have, now=time.time())["backlog"], {"same": True})

    def test_no_backlog_an_unreadable_one_and_a_strange_one_stand_as_no_document_and_never_raise(self):
        view = self.page()
        got = self.status.panel_lists(self.cwd, view, {})
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["items"], got["backlog"]["total"]), (False, [], 0))
        (self.root / "backlog.md").mkdir()                                                   # a folder of that name
        got = self.status.panel_lists(self.cwd, view, {})
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["total"]), (False, 0))
        (self.root / "backlog.md").rmdir()
        (self.root / "backlog.md").symlink_to(self.root / "elsewhere.md")                    # a link (and a dangling one)
        self.assertEqual(self.status.panel_lists(self.cwd, view, {})["backlog"]["has_doc"], False)
        (self.root / "backlog.md").unlink()
        (self.root / "backlog.md").write_bytes(b"\xff\xfe## \x00\n- \xc3\n")                    # broken text
        got = self.status.panel_lists(self.cwd, view, {})
        self.assertEqual((got["backlog"]["has_doc"], got["backlog"]["total"]), (True, 0))
        with mock.patch.object(self.status, "read_backlog", side_effect=PermissionError("no")):      # whatever the read raises
            self.assertIsNone(self.status.panel_lists(self.cwd, view, {}))
        with mock.patch.object(self.status, "_root", lambda cwd, now: None):                              # no artifact root: no lists
            self.assertIsNone(self.status.panel_lists(self.cwd, view, {}))
        # A status page that is not the generated one has no items (the popup has no list screen for it either).
        hand = self.page("2026-11-03_hand", "# 손으로 쓴 글\n\n## 하는 중\n- 하나\n")
        self.assertEqual(self.status.panel_lists(self.cwd, hand, {})["status"]["items"], [])

    def test_the_status_read_carries_the_lists_and_asks_the_signs_back(self):
        value = {"word": "STATUS", "has_doc": True, "path": self.page()["path"], "title": "STATUS x", "slot": "", "watch": []}
        self.backlog()
        with mock.patch.object(self.status, "read", lambda cwd, routes=(), now=None: value):
            first = self.shell._panel_status({}, {"cwd": self.cwd, "routes": []})
            self.assertEqual((first["ok"], first["status"]), (True, value))
            self.assertEqual(first["lists"]["backlog"]["total"], 4)
            have = {"status": first["lists"]["status"]["sign"], "backlog": first["lists"]["backlog"]["sign"], "junk": 5}
            second = self.shell._panel_status({}, {"cwd": self.cwd, "routes": [], "have": have})
            self.assertEqual(second["lists"], {"status": {"same": True}, "backlog": {"same": True}})
            # No artifact root: no value and no lists.
        with mock.patch.object(self.status, "read", lambda cwd, routes=(), now=None: None):
            self.assertEqual(self.shell._panel_status({}, {"cwd": self.cwd, "routes": []}), {"ok": True, "status": None, "lists": None})

    def popup(self, part, own="2026-11-02_some-campaign", width=80, kind="backlog"):
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        return self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": part, "own": own}, width, 256, False, kind)

    def test_the_backlog_popup_is_the_status_screens_own_parts_over_the_document(self):
        self.backlog()
        answer = self.popup("list")
        self.assertEqual((answer["ok"], answer["kind"], answer["part"], answer["rows"], answer["has_doc"]), (True, "backlog", "list", [], True))
        self.assertTrue(answer["title"].startswith("BACKLOG"))
        entries = answer["list"]
        shown = [("".join(run[0] for run in e["rows"][0]), e.get("key", "")[:2]) for e in entries]
        self.assertEqual([t for t, _ in shown], ["시험용 백로그", "updated 11-21 11:02", "", "2026-11-02_some-campaign 2", "  일 하나", "  일 둘", "",
                                                  "  › 2026-10-01_other-campaign 1", "    다른 일", "  › Unassigned 1", "    정해지지 않은 일", "", "  full document ›"])
        self.assertEqual([k for _, k in shown], ["", "", "", "", "i:", "i:", "", "g:", "i:", "g:", "i:", "", "fu"])
        # The item with indented lines carries them (they stand under it while it is open); the one without carries none.
        self.assertEqual(["".join(run[0] for run in row) for row in entries[4]["more"]], ["    date: 2026-11-20", "    source: 어딘가", "    note: 맥락 하나"])
        self.assertNotIn("more", entries[5] if not entries[5].get("more") else {})
        # Not the campaign of this window: every group is a folded line, in the document's order.
        other = [("".join(run[0] for run in e["rows"][0])) for e in self.popup("list", own="")["list"] if e.get("key", "").startswith("g:")]
        self.assertEqual(other, ["  › 2026-11-02_some-campaign 2", "  › 2026-10-01_other-campaign 1", "  › Unassigned 1"])
        # The document read whole is the other answer; the Fleet part is not the backlog's.
        whole = self.popup("doc")
        self.assertNotIn("list", whole)
        self.assertTrue(any("2026-11-02_some-campaign" in "".join(run[0] for run in row) for row in whole["rows"]))
        self.assertEqual(self.popup("fleet"), {"ok": False, "reason": "invalid-status"})
        # An older document is said to be old.
        os.utime(self.root / "backlog.md", (time.time() - 5 * 86400,) * 2)
        self.assertIn("old document", "".join(run[0] for run in self.popup("list")["list"][1]["rows"][0]))

    def test_the_backlog_popup_without_a_document_says_so_in_one_line(self):
        for part in ("list", "doc"):
            answer = self.popup(part)
            self.assertEqual((answer["ok"], answer["has_doc"], answer["title"]), (True, False, "BACKLOG"))
            self.assertNotIn("list", answer)
            self.assertEqual(["".join(run[0] for run in row) for row in answer["rows"]], ["No backlog document yet."])
        (self.root / "backlog.md").mkdir()
        self.assertEqual(self.popup("list")["has_doc"], False)

    def test_an_item_opens_to_its_indented_lines_in_the_status_lists_own_code_and_a_press_stands_on_it(self):
        self.backlog()
        answer = self.popup("list")
        with tempfile.TemporaryDirectory(prefix="rlj") as folder:
            path = Path(folder) / "answer.json"
            path.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
            out = subprocess.run([NODE, "-e", r"""
const fs=require('node:fs'),path=require('node:path');
const list=require(path.join(process.argv[1],'status-list.cjs'));
const entries=list.entriesOf(JSON.parse(fs.readFileSync(process.argv[2],'utf8'))),text=r=>r.map(p=>p.text).join('');
const st={sel:'',fold:new Set(),items:new Set()};
const lines=()=>list.layout(entries,st,{width:60}).rows.map(text);
const first=lines(),item=list.layout(entries,st).marks.find(m=>m.key.startsWith('i:')&&m.text);
// (the first item has indented lines: it can be opened; the second has none)
const second=list.layout(entries,st).marks.filter(m=>m.key.startsWith('i:'))[1];
const out={item_can_open:!!item,second_can_open:!!second.text};
out.pressed=list.press(st,list.layout(entries,st),item.key);
const opened=lines();out.more=opened.length-first.length;out.shown=opened.filter(l=>/date:|source:|note:/.test(l));
const v=list.layout(entries,st,{width:60});out.tinted=v.rows.slice(v.at,v.at+v.span).every(r=>r.some(p=>p.tint));out.span=v.span;
out.hits=list.hits(v).filter(h=>h.action[1]===item.key).length;
out.press_again=list.press(st,list.layout(entries,st),item.key);out.closed=lines().length===first.length;
// A press from the panel stands on the item (the popup is opened with its key as the chosen line).
const sel={sel:item.key,fold:new Set(),items:new Set()};out.stands=list.layout(entries,sel).sel===item.key&&!list.layout(entries,sel).fellBack;
const gone={sel:'i:nothere',fold:new Set(),items:new Set()};out.fell=list.layout(entries,gone).fellBack;
// The second group is a folded line; pressing it opens its items.
const g=list.layout(entries,st).marks.find(m=>m.key.startsWith('g:'));out.group=list.press(st,list.layout(entries,st),g.key);
out.group_items=lines().filter(l=>/다른 일/.test(l)).length;
console.log(JSON.stringify(out));
""", str(SHELL), str(path)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got, {"item_can_open": True, "second_can_open": True, "pressed": "item", "more": 3,
                               "shown": ["    date: 2026-11-20", "    source: 어딘가", "    note: 맥락 하나"], "tinted": True, "span": 4, "hits": 4,
                               "press_again": "item", "closed": True, "stands": True, "fell": True, "group": "fold", "group_items": 1})

    # -- the review of #1793, the three it left -------------------------------------------------------------------------

    def test_a_list_dropped_for_its_size_leaves_no_stale_style_index(self):
        # A generated page with no bullet at all and so much text that the list and the document both pass the answer's limit: the list is dropped, and
        # the closing `…` row points at a style the answer has (it did not, once the styles the list added were dropped but their names were kept).
        body = STATUS_PAGE.split("## 하는 중")[0] + "## 기다리는 것\n" + "".join("note line %04d " % n + "z" * 60 + "\n" for n in range(780))
        view = self.page("2026-11-04_big", body)
        view = {**view, "title": "STATUS fixture", "word": "STATUS", "slot": "", "watch": [], "age": "", "campaign": "fixture"}
        os.utime(view["path"], (1_800_000_000, 1_800_000_000))
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        answers = {}
        for part in ("list", "doc"):
            with mock.patch.object(self.status, "held", return_value=view), mock.patch.object(self.shell, "_popup_view_model", side_effect=AssertionError("Fleet")):
                answers[part] = self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": part}, 100, 256, False)
        first = answers["list"]
        self.assertNotIn("list", first)                                                       # the list passed the limit: the document's screen
        for answer in answers.values():
            used = {run[1] for row in answer["rows"] for run in row}
            self.assertTrue(used and max(used) < len(answer["styles"]), (used, len(answer["styles"])))
        self.assertEqual({k: v for k, v in first.items() if k != "part"}, {k: v for k, v in answers["doc"].items() if k != "part"})
        self.assertEqual(first["styles"][first["rows"][-1][0][1]].get("dim"), True)           # the closing `…` is dim

    def test_the_documents_rows_are_made_only_where_the_list_is_dropped(self):
        view = {**self.page(), "word": "STATUS", "title": "STATUS x", "slot": "", "watch": [], "age": "", "campaign": "x"}
        with mock.patch.object(self.status, "_doc_rows_of", side_effect=AssertionError("rows were made for a list that stands")):
            rows, listing = self.status.popup_doc(view, 80, time.time(), want_list=True)
            self.assertTrue(callable(rows) and listing)
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        with mock.patch.object(self.status, "held", return_value=view), mock.patch.object(self.status, "_doc_rows_of", side_effect=AssertionError("made")):
            self.assertIn("list", self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": "list"}, 80, 256, False))
        # The full document asks without a list and gets its rows as before; a dropped list makes them once.
        rows, listing = self.status.popup_doc(view, 80, time.time())
        self.assertEqual((type(rows), listing), (list, None))
        calls = []
        real = self.status._doc_rows_of
        with mock.patch.object(self.status, "_doc_rows_of", lambda *a: calls.append(1) or real(*a)), \
                mock.patch.object(self.shell, "POPUP_ANSWER_BYTES", 300), mock.patch.object(self.status, "held", return_value=view):
            answer = self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": "list"}, 80, 256, False)
        self.assertEqual((len(calls), "list" in answer, bool(answer["rows"])), (1, False, True))


HOST = r"""
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const root=process.argv[1];
const src=fs.readFileSync(path.join(root,'host.cjs'),'utf8');
const cut=(from,to)=>{const a=src.indexOf(from),b=src.indexOf(to,a);if(a<0||b<0)throw new Error('marker '+from);return src.slice(a,b);};
const statusList=require(path.join(root,'status-list.cjs'));
const same=(a,b)=>assert.equal(JSON.stringify(a),JSON.stringify(b));   // (objects made inside a vm context are not the main context's)
"""


@unittest.skipUnless(NODE, "needs node")
class HostTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run([NODE, "-e", HOST + body, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_status_read_brings_the_two_lists_and_a_document_that_did_not_change_keeps_its_own(self):
        self.node(r"""(async()=>{
const asked=[];let answers=[];
const ctx={process,path,JSON,Set,Array,Number,String,Math,Promise,panelModel:{},table:null,selection:1,lastWatch:[],listed:k=>k==='STATUS'||k==='BACKLOG',
  readAction:(extra,op)=>{asked.push(JSON.parse(JSON.stringify(extra)));return Promise.resolve(answers.shift());},
  observeFiles:()=>false,queueProjection:()=>{},queueDisplay:()=>{},askTable:()=>{ctx.tableAsked=(ctx.tableAsked||0)+1;}};
vm.createContext(ctx);
vm.runInContext(cut('let statusValue=null,','// At a projection:')+'\nthis.api={askStatus,statusDoc,backlogDoc,ownFolder,get lists(){return panelLists;}};',ctx);
const api=ctx.api,settle=()=>new Promise(r=>setTimeout(r,10));
const item=(text,key,mark='')=>({text,key,mark});
const value={word:'STATUS',has_doc:true,path:'/x/2026-11-02_camp/campaign.md',title:'STATUS camp',watch:[]};
// No value yet: neither part.
assert.equal(api.statusDoc(),null);assert.equal(api.backlogDoc(),null);
answers=[{ok:true,status:value,lists:{status:{sign:'S1',items:[item('가','i:1','!'),item('나','i:2'),item('다','i:3'),item('라','i:4')],total:6},
  backlog:{sign:'B1',has_doc:true,items:[item('백','i:b')],total:9}}}];
ctx.askStatus();await settle();
same(asked[0].have,{});                                 // nothing held yet: nothing to send back
let s=api.statusDoc(),b=api.backlogDoc();
same([s.word,s.has_doc,s.items.length,s.total,s.items[0]],['STATUS',true,3,6,{mark:'!',text:'가',key:'i:1'}]);   // three at most
same([b.word,b.has_doc,b.items.length,b.total],['BACKLOG',true,1,9]);
assert.equal(api.ownFolder(),'2026-11-02_camp');
// The next read sends the signs back; "same" keeps what is held, a new sign replaces it.
answers=[{ok:true,status:value,lists:{status:{same:true},backlog:{same:true}}}];ctx.askStatus();await settle();
same(asked[1].have,{status:'S1',backlog:'B1'});
same([api.statusDoc().total,api.backlogDoc().total],[6,9]);
answers=[{ok:true,status:value,lists:{status:{same:true},backlog:{sign:'B2',has_doc:false,items:[],total:0}}}];ctx.askStatus();await settle();
same([api.statusDoc().total,api.backlogDoc().has_doc,api.backlogDoc().items.length],[6,false,0]);
// A read that failed keeps the lists; a value of no artifact root clears them.
answers=[{ok:false}];ctx.askStatus();await settle();assert.equal(api.statusDoc().total,6);
answers=[{ok:true,status:null,lists:null}];ctx.askStatus();await settle();
assert.equal(api.statusDoc(),null);assert.equal(api.backlogDoc(),null);
// A strange answer is cut to what the panel can use: not an array, a total smaller than the items, a total that is no number, items without text.
answers=[{ok:true,status:value,lists:{status:{sign:'S3',items:'x',total:'many'},backlog:{sign:'B3',has_doc:true,items:[item('a','k'),{text:5},null,item('b','k2')],total:1}}}];
ctx.askStatus();await settle();
same([api.statusDoc().items.length,api.statusDoc().total],[0,0]);
same([api.backlogDoc().items.map(i=>i.text),api.backlogDoc().total],[['a','b'],2]);
// The panel model got both values with the read (the drawing reads from it, nothing else).
assert(ctx.panelModel.status_doc&&ctx.panelModel.backlog_doc);
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_a_press_on_an_item_opens_the_popup_standing_on_it_and_the_backlog_is_a_listed_kind(self):
        self.node(r"""
const calls=[];
const ctx={Set,Array,Number,String,Math,epoch:0,empty:false,actionLines:[],resetTableInput:()=>{},heldStop:()=>{},block:{focused:false,leave:()=>{},enter:()=>{}},
  table:null,statusPopup:null,statusGeneration:3,cols:100,expanded:false,routePopup:null,cellWidth:t=>[...t].length,panelModel:{},
  panelView:{popupWidth:()=>96,memoryBody:()=>({starts:[]})},tableKind:()=>'table',queueDisplay:()=>calls.push('display'),askTable:()=>calls.push('askTable'),
  blockAction:()=>{},rankedRoutes:()=>[],routeFirst:()=>null,listed:k=>k==='STATUS'||k==='BACKLOG'};
vm.createContext(ctx);
vm.runInContext(cut('function openPopup(name,open','// What a routes popup shows before')+'\nthis.openPopup=openPopup;',ctx);
ctx.openPopup('STATUS','i:abc:def');
assert.equal(ctx.table.kind,'STATUS');assert.equal(ctx.table.sel,'i:abc:def');assert.equal(ctx.table.listing,null);assert(calls.includes('askTable'));
// (with no item pressed the popup stands on its first line, as before)
ctx.openPopup('STATUS','');assert.equal(ctx.table.sel,'');
calls.length=0;ctx.openPopup('BACKLOG','i:1:2');
assert.equal(ctx.table.kind,'BACKLOG');assert.equal(ctx.table.sel,'i:1:2');assert(ctx.table.fold instanceof Set&&ctx.table.items instanceof Set);assert(calls.includes('askTable'));
// What a panel press names parses to that: `popup:STATUS:i:a:b` is the section STATUS and the key `i:a:b`.
const [,name,...rest]='popup:BACKLOG:i:a:b'.split(':');same([name,rest.join(':')],['BACKLOG','i:a:b']);
// The sections the host walks, and the popup kind the backend is asked for.
const sections=src.match(/const SECTIONS=\[([^\]]*)\]/)[1],kinds=src.match(/const POPUP_KIND=\{([^}]*)\}/)[1];
assert.equal(sections,"'STATUS','BACKLOG','NOW','QUESTIONS','ROUTE','MESSAGES','MEM'");assert(kinds.includes("BACKLOG:'backlog'"));
""")


def text_of(row):
    return "".join(part for part, _key in row)


if __name__ == "__main__":
    unittest.main()
