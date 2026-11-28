"""11-16 (panel polish 1116, 9): MEM draws v4.36.4's two rows per record again (spec rule 46 (b)); 11-18 (the person: "MEM는 최대 최근 3개만 띄워도 되겠다"): the newest three at most;
11-20 (the person: "메모리 내용은 한줄로 바꾸고 (지금은 두줄이네) 메시지 처럼 팝업이 가능하게"): one row a record, a press opens the MEM popup.

A record is one row, as a message's one-row form: its mark (`◆` durable, `◇` working, `·` naming no tier), its age and its kind dim, then its
headline in the body color (`…` where it goes on; the kind goes first where the headline would be left under 14 cells). This repository's and the
global records together, newest first, `MEM_MAX` (3) at most; the head `MEM ──── ›`. A press on the head or a record opens the MEM popup (the
record pressed on top), which reads each record whole (`memoryBody`). Short of rows MEM shows two records, then one, after the messages took the
one-row form; they are the first sections to go down. The backend sends three (it sent two, then five) and, since 11-20, each record's body.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import datetime
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const styles={dim:{dim:true},loc_repo:{foreground:253},lvl_g:{foreground:150},lvl_y:{foreground:186},g_blocked:{foreground:217},g_spin:{foreground:230},
  g_work:{foreground:150,bold:true},g_work_off:{foreground:150,dim:true},g_idle:{foreground:186},hb_claude:{foreground:116},hb_codex:{foreground:176},hb_opencode:{foreground:147}};
const t=(text,key='dim')=>({text,...(styles[key]||{dim:key==='dim'})});
const MEM=[{tier:'durable',scope:'project',kind:'decision',age:'10m',text:'접기 단추는 펼침 단추와 같은 자리, 접고 펴는 곳이 한 모서리'},
  {tier:'working',scope:'global',kind:'lesson',age:'2h',text:'점멸은 1초 박자로'},
  {tier:'durable',scope:'project',kind:'correction',age:'3h 10m',text:'메시지는 3건이 아니라 두 줄 꼴로, 건수만 줄이라는 뜻이었다 — 줄 수로 읽지 말 것, 길게 이어지는 셋째 줄까지'},
  {tier:'durable',scope:'global',kind:'fact',age:'1d 2h',text:'패널 설정은 session-shell.json 하나'},
  {tier:'',scope:'project',kind:'done',age:'2d 0h',text:'현황 단추와 팝업 병합'},
  {tier:'working',scope:'project',kind:'lesson',age:'3d 0h',text:'여섯째 기록은 서지 않는다'}];
const MSG=[['←','[73]','claude','09:31','reply','그림 두 장 보냈습니다'],['→','[e7]','codex','09:40','message','접기 단추 자리 비교 부탁'],
  ['←','[12]','opencode','09:52','report','시험 통과'],['←','[e7]','codex','10:03','reply','권장안이 낫습니다'],['→','[12]','opencode','10:05','message','고맙습니다']]
  .map(([way,who,harness,when,kind,text])=>({way,who,harness,when,kind,text,body:text,delivery:way==='→'?['sent','dim']:null}));
function model(o={}){
  return {styles,colors:256,tag:'[e9]',repo_side:[t('hearting','loc_repo')],git_side:[t('panel-polish-1116')],selected:[t('Opus 5.5','hb_claude')],
    context_side:[t('━━━━','lvl_g'),t('────'),t(' 41%')],quota:[t('5h ','dim'),t('23%','lvl_g'),t(' │ '),t('7d ','dim'),t('61%','lvl_y')],
    session_title:'패널 손질',now:'MEM 두 줄 꼴',wait:'◑답 대기 1',route_label:'route 1',route_count:1,route_total:1,
    routes:[{id:'rt-1',title:'패널 손질',kind:'working',state:'working',shape:'one-shot-owner',kind_word:'code',elapsed:'42m',role:'owner',
      paint:{glyph:t('⠏','g_spin'),harness:t('claude','hb_claude'),model_text:t('Opus 5.5','hb_claude'),ctx:t('52%','lvl_g'),gauge5:[t('━━━','lvl_g'),t('──'),t(' '),t('52%','lvl_g')]}}],
    messages:o.messages??MSG,memory_items:o.mem??MEM,...(o.model||{})};
}
// (11-28: each waiting question one row, `◑ 답 대기 · <its first line>`; no choices row.)
const brief=()=>({count:1,answerable:true,label:'QUESTIONS',waits:[{word:'답 대기',answerable:true,open:'S4:t@r',line:[t('몇 건까지?','loc_repo')]}]});
const draw=(o={})=>view.render({width:o.width??50,height:o.height??60,side:true,model:model(o),state:'working',harness:'claude',frame:1000,since:990,brief,sent:()=>[],
  cellWidth:cells,configuration:{side_width:50,rule_motion:'rule',...(o.cfg||{})}});
const lines=v=>v.rows.map(r=>r.map(p=>p.text).join(''));
const mem=v=>{const ls=lines(v),i=ls.findIndex(s=>/^│ MEM +(› )?│$/.test(s));if(i<0)return null;const rows=[];
  for(let k=i+1;k<ls.length&&/^│  [◆◇·] /.test(ls[k]);k++)rows.push(ls[k]);return {at:i,head:ls[i],rows};};
const msgs=v=>{const ls=lines(v),a=ls.findIndex(s=>s.startsWith('│ MESSAGES')),b=ls.findIndex(s=>/^│ MEM +(› )?│$/.test(s));
  const rows=a<0?[]:ls.slice(a+1,b<0?ls.length-3:b);return {count:rows.filter(s=>/^│  [←→] /.test(s)).length,two:rows.some(s=>/^│   \S/.test(s))};};
"""


class MemRowsTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run(["node", "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_a_record_is_one_row_three_at_most_and_a_press_opens_the_popup(self):
        self.node(r"""
const v=draw(),m=mem(v);
assert(m,lines(v).join('\n'));
assert.match(m.head,/^│ MEM +› │$/,'the head ends in `›` now: a press opens the popup (11-22: no rule, one cell in for its records)');
// One row a record, the newest first, the mark, the age and the kind, then the headline (`…` where it goes on).
assert.equal(m.rows.length,3,m.rows.join('\n'));
const rows=m.rows.map(s=>s.slice(2,-2).trim());
assert(rows[0].startsWith('◆ 10m decision 접기 단추는 펼침')&&rows[1]==='◇ 2h lesson 점멸은 1초 박자로'&&rows[2].startsWith('◆ 3h 10m correction 메시지는'),rows.join('\n'));
assert(rows[0].endsWith('…'),rows[0]);
assert(!lines(v).join('\n').includes('패널 설정은')&&!lines(v).join('\n').includes('현황 단추')&&!lines(v).join('\n').includes('여섯째'),'the fourth and later records do not stand');
// The lead (mark, age, kind) dim, the headline in the body color.
const y=lines(v).findIndex(s=>s.startsWith('│  ◇ 2h'));
assert(v.rows[y].filter(p=>/[◇a-z0-9]/.test(p.text)&&!/점멸/.test(p.text)).every(p=>p.dim===true),JSON.stringify(v.rows[y]));
assert(v.rows[y].filter(p=>/점멸/.test(p.text)).every(p=>p.foreground===253&&p.dim!==true),JSON.stringify(v.rows[y]));
// A press on the head or a record: the MEM popup, the record pressed named (`popup:MEM:<n>`).
const act=i=>v.hits.filter(h=>h.row===i&&!['fold','settings'].includes(h.action)).map(h=>h.action);
assert.deepEqual(act(m.at),['popup:MEM']);m.rows.forEach((_,k)=>assert.deepEqual(act(m.at+1+k),['popup:MEM:'+k]));
// MEM stands under MESSAGES, its last row above the thin rule and the last row.
const ls=lines(v);assert(m.at>ls.findIndex(s=>s.startsWith('│ MESSAGES'))&&ls.indexOf(m.rows.at(-1))===ls.length-4&&/^│ ─+ │$/.test(ls.at(-3)),ls.join('\n'));
// An older backend's painted record: its age, its headline on the one row.
const old=draw({mem:[],model:{memory_items:undefined,memory:[t('3m '),t('decision '),t('예전 백엔드의 한 줄','loc_repo')]}}),om=mem(old);
assert(om&&om.rows.length===1&&/^│  · 3m 예전 백엔드의 한 줄 +│$/.test(om.rows[0]),lines(old).join('\n'));
""")

    def test_short_of_cells_the_kind_goes_first_and_the_mark_and_the_age_stay(self):
        self.node(r"""
for(let w=60;w>=14;w--){
  const m=mem(draw({width:w,mem:MEM.slice(0,1)})),h=m.rows[0],label=w+'\n'+m.rows.join('\n');
  assert(h.startsWith('│  ◆ 10m ')&&cells(h)===w,label);
  // The kind stands while the headline keeps 14 cells at least.
  if(w-5-cells('◆ 10m decision ')>=14)assert(h.includes('10m decision '),label);else assert(!h.includes('decision'),label);
}
// Mono and NO_COLOR keep the marks apart by shape.
for(const o of [{cfg:{theme:'mono'}},{model:{no_color:true}}]){const m=mem(draw({...o,mem:[MEM[0],MEM[1],MEM[4]]}));assert(m.rows[0].startsWith('│  ◆ ')&&m.rows.some(r=>r.startsWith('│  ◇ '))&&m.rows.some(r=>r.startsWith('│  · ')),m.rows.join('\n'));}
""")

    def test_short_of_rows_messages_fold_then_records_and_messages_show_fewer(self):
        out = self.node(r"""
const seen=[];
for(let h=70;h>=14;h--){
  const v=draw({height:h}),m=mem(v),g=msgs(v);
  seen.push({h,records:m?m.rows.length:0,head:!!m,msg:g.count,two:g.two});
}
assert(seen[0].records===3&&seen[0].msg===3&&!seen[0].two,JSON.stringify(seen[0]));
// 11-20: MESSAGES and MEM are the first sections to go down, together: MEM shows two records and MESSAGES two messages, then one of each; three messages at most.
// 11-26 (panel B): a side panel message is one row at every height (the two-row form and its fold are the bottom layout's), so there is no fold step: MEM goes down first, then the messages.
assert(seen.every(s=>!s.two),JSON.stringify(seen));
const msgFewer=seen.find(s=>s.head&&s.msg<3),firstFewer=seen.find(s=>s.head&&s.records<3);
assert(msgFewer&&firstFewer&&firstFewer.h>=msgFewer.h,JSON.stringify(seen));
assert(seen.some(s=>s.records===1&&s.msg===1),JSON.stringify(seen));
console.log(JSON.stringify(seen.map(s=>[s.h,s.records,s.msg,s.two?2:1])));
""")
        self.assertTrue(out.strip())

    def test_the_backend_sends_three_records(self):
        from fleet import session_panel, session_work
        import session_shell as shell
        import human_wait_state
        import session_tidy
        self.assertEqual(session_panel.MEMORY_ITEMS, 3)
        now = datetime.datetime(2026, 11, 16, 10, 0, 0)
        rows = [{"ts": "2026-11-16T09:%02d:00" % (50 - n), "type": "decision", "headline": "기록 %d" % n,
                 "tier": "durable", "scope": "project"} for n in range(7)]
        items = session_panel.session_memory_items({"memory_recent": rows}, now)
        self.assertEqual([item["text"] for item in items], ["기록 %d" % n for n in range(3)])
        # The projection asks the journal for three.
        asked = []

        def recent(cwd, limit=2):
            asked.append(limit)
            return [], []
        view = {"route_panels": {}, "items": [], "by_session": {}}
        with mock.patch.object(session_work, "recent_memory", side_effect=recent), \
                mock.patch.object(human_wait_state, "cached_view", return_value=view), \
                mock.patch.object(human_wait_state, "cached_statusline", return_value=((0, 0), "")), \
                mock.patch.object(session_tidy, "waiting_notice_display", return_value=("", [], "")):
            shell.panel_projection("claude", "00000000-0000-4000-8000-000000001169", columns=50, rows=46)
        self.assertEqual(asked, [3])


if __name__ == "__main__":
    unittest.main()
