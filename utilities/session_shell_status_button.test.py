"""11-15 (design 2, "현황은 REPO 옆에 버튼을 만드는게 나을거 같은데" / "권장안대로하고"; "대기 중인건 점멸을 시키자" /
"질문만"; review RV-PR1652-A A5): the panel's status button, the waiting blink and the folded strip at 30-32 cells.

1. (11-21: the REPO row has no button any more: the STATUS and BACKLOG parts stand under the REPO group, `session_shell_repo_lists_1121.test.py`.) The REPO row is the
   group's head, its rule running to the row's end; no press on it. The bottom layout's first row ends in the two buttons `STATUS +N ›  BACKLOG +N ›` (the counts
   give way first, then the words; `›` stays); the folded strip has none. No document: the word dim. No artifact root: no buttons.
2. What waits for the person's answer goes between two reds (`FACES.*.waitBlink`) on the waiting breath's
   beat: the red letters of the question answered here, the `◑` of one answered elsewhere and the guide
   line's count, `◑답 대기 N` on the strip. Only those cells move; nothing moves with `rule motion` off, mono,
   NO_COLOR or eight colors, and nothing wakes without a wait.
3. The folded strip keeps `N ROUTE` whole from 30 cells with the wait beside it.
4. The popup's bottom line names STATUS by its word; a press opens it by its key. A request body's tables
   come drawn (Fleet's rows) in the MESSAGES popup.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"

HEAD = r"""
const assert=require('node:assert/strict'),path=require('node:path');
const pv=require(path.join(process.argv[1],'panel-view.cjs'));
const cells=s=>[...String(s)].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]/u.test(c)?2:1),0);
const p=(text,extra={})=>({text,...extra});
const str=r=>r.map(x=>x.text).join('');
const styles={dim:{foreground:-1,dim:true},loc_repo:{foreground:253},g_blocked:{foreground:217},lvl_g:{foreground:150},g_spin:{foreground:230},
 g_work_off:{foreground:150,dim:true},g_dead:{foreground:210},lvl_r:{foreground:210},lvl_y:{foreground:186}};
const base={tag:'[e9]',worktrees:[p('wt 12',{dim:true})],selected:[p('Opus 5.5')],context_side:[p('━━─── '),p('46%')],styles};
const render=o=>pv.render({width:o.width||44,height:o.height||20,side:o.side!==false,fill:o.side!==false&&!o.folded,folded:!!o.folded,
 model:{...base,...(o.model||{})},state:o.state||'idle',sid:'s',harness:'claude',frame:o.frame??0,since:o.since,
 brief:()=>o.q||null,sent:o.sent||(()=>[]),configuration:o.cfg||{},cellWidth:cells,background:o.background||''});
// Cell columns of a row of parts: each part's first cell.
const columns=row=>{let x=0;return row.map(part=>{const at=x;x+=cells(part.text);return {...part,at};});};
"""


class StatusButtonTest(unittest.TestCase):
    def node(self, body):
        result = subprocess.run(["node", "-e", HEAD + body, str(SHELL)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_repo_row_has_no_button_and_its_name_gives_way_alone(self):
        # Every side width 24-60 × names of 8, 15, 23 and 32 cells, with and without the worktree count.
        self.node(r"""
const names=['hearting','shell-tools-kit','speech-separation-bench','very-long-project-name-for-tests'];
const doc={word:'STATUS',has_doc:true},backlog={word:'BACKLOG',has_doc:true};
for(const name of names)for(const wt of [true,false])for(let w=24;w<=60;w++){
 const model={repo_side:[p(name,{foreground:116})],...(wt?{}:{worktrees:[]}),status_doc:doc,backlog_doc:backlog};
 const v=render({width:w,model}),rows=v.rows.map(str),row=rows[1],tag=w+' '+name+' '+wt+'\n'+rows.join('\n');
 assert.equal(cells(row),w,tag);
 assert(!row.includes('STATUS')&&!row.includes('›'),tag);
 assert(v.hits.filter(h=>h.row===1).length===0,tag);
 const inner=w-4,label=inner>=30;
 if(label)assert(row.startsWith('│ REPO   '),tag);else assert(row.startsWith('│ REPO '),tag);
 // The rule runs to the end of the row (a thin rule where three cells or more are free).
 assert(/(─+|[^─]) │$/.test(row),tag);
 // The name is whole, or cut with `…`.
 assert(row.includes(name)||row.includes('…'),tag);
}
const at40=name=>str(render({width:40,model:{repo_side:[p(name)],status_doc:doc,backlog_doc:backlog}}).rows[1]);
assert.equal(at40('hearting'),'│ REPO   hearting · wt 12 ──────────── │');
assert.equal(at40('speech-separation-bench'),'│ REPO   speech-separation-bench ───── │');   // (the count goes before the name is cut)
""")

    def test_no_document_dims_the_head_and_no_root_shows_none(self):
        self.node(r"""
// No document: the word dim (not in the title role), `›` dim, and a press on the head still opens the popup.
{const v=render({width:54,height:30,model:{repo_side:[p('hearting')],status_doc:{word:'STATUS',has_doc:false,items:[],total:0}}});
 const at=v.rows.findIndex(r=>str(r).includes('│ STATUS'));assert(at>0,v.rows.map(str).join('\n'));
 const w=v.rows[at].find(x=>x.text==='STATUS'),a=v.rows[at].findLast(x=>x.text==='›');
 assert(w.dim===true&&a.dim===true&&w.role!=='title',JSON.stringify(v.rows[at]));
 assert(v.hits.some(h=>h.row===at&&h.action==='popup:STATUS'));}
// No artifact root (no value): nothing under the REPO row, no press, no word anywhere.
for(const status_doc of [undefined,null,{word:'',has_doc:true}]){
 const v=render({width:54,height:30,model:{repo_side:[p('hearting')],status_doc}});
 assert.match(str(v.rows[1]),/^│ REPO   hearting · wt 12 ─+ │$/,JSON.stringify(status_doc));
 assert(!v.hits.some(h=>/STATUS|BACKLOG/.test(h.action)));
 assert(!v.rows.flat().some(x=>x.text==='STATUS'));}
""")

    def test_the_unlabelled_head_the_bottom_layout_and_the_folded_strip(self):
        self.node(r"""
// Narrower than 30 cells inside (the 32-cell side panel): the first row, unlabelled; no button.
{const v=render({width:32,model:{repo_side:[p('hearting')],status_doc:{word:'STATUS',has_doc:true},backlog_doc:{word:'BACKLOG',has_doc:true}}});
 assert.equal(str(v.rows[1]),'│ REPO hearting · wt 12 ────── │');assert(!v.hits.some(h=>h.row===1));}
// The bottom layout: the right end of the first row (`repo │ model │ context`) holds two buttons; the counts go first, then the words (`›` stays), then the row is cut.
const two=(w,s,b)=>render({width:w,side:false,height:8,model:{repo:[p('hearting')],context:[p('ctx 46%')],status_doc:{word:'STATUS',has_doc:true,total:s},backlog_doc:{word:'BACKLOG',has_doc:true,total:b}}});
for(const [w,want] of [[120,/│ hearting │ Opus 5\.5 │ ctx 46% +STATUS \+2 ›  BACKLOG \+11 › │$/],[60,/│ hearting │ Opus 5\.5 │ ctx 46% STATUS \+2 ›  BACKLOG \+11 › │$/],[56,/│ hearting │ Opus 5\.5 │ ctx 46% +STATUS ›  BACKLOG › │$/],[52,/│ hearting │ Opus 5\.5 │ ctx 46% +›  › │$/],[34,/│ hearting │ Opus 5\.5 │ ct… ›  › │$/]]){
 const v=two(w,2,11);
 assert.match(str(v.rows[1]),want,w+' '+str(v.rows[1]));assert.equal(cells(str(v.rows[1])),w);
 // The rest of the row opens the status popup, the BACKLOG button its own.
 const [a,b]=['popup:STATUS','popup:BACKLOG'].map(x=>v.hits.find(h=>h.row===1&&h.action===x));
 assert(a&&b&&a.left===2&&b.right===w-3&&a.right+1===b.left,JSON.stringify(v.hits));
 const text=str(v.rows[1]).slice(1);assert(b.left>=2);
}
{const v=two(120,0,0);assert.match(str(v.rows[1]),/ctx 46% +STATUS ›  BACKLOG › │$/,str(v.rows[1]));}
// The folded strip carries no button.
{const v=render({width:120,folded:true,height:1,model:{repo_side:[p('hearting')],status_doc:{word:'STATUS',has_doc:true},backlog_doc:{word:'BACKLOG',has_doc:true}}});
 assert(!str(v.rows[0]).includes('STATUS')&&!v.hits.some(h=>/popup:(STATUS|BACKLOG)/.test(h.action)),str(v.rows[0]));}
""")

    def test_the_waiting_blink_moves_only_the_waiting_red_cells(self):
        self.node(r"""
const here={count:1,label:'QUESTIONS',answerable:true,
 rows:[[p('◑ ',{foreground:217}),p('로그를 어느 쪽에 남길까요?',{foreground:253})],
  [p('  ',styles.dim),p('1 ',styles.dim),p('파일',{foreground:253}),p(' · ',styles.dim),p('2 ',styles.dim),p('표준 출력',{foreground:253})]],
 one:[[p('◑ ',{foreground:217}),p('로그를 어느 쪽에 남길까요?',{foreground:253})]],line:[p('로그를 어느 쪽에 남길까요?')],choices:2};
const there={...here,answerable:false,rows:[[p('◑ '),p('다른 세션의 질문')],[p('  '),p('그 창에서 답',styles.dim)]],choices:0};
const sent=()=>[[p('✓ 답 보냄',{foreground:150}),p(' · ',styles.dim),p('2 표준 출력',{foreground:253})]];
const model={repo_side:[p('hearting')],wait:'◑답 대기 1',session_title:'점멸',route_label:'route 1',route_count:1,route_total:1,
 routes:[{id:'a',title:'휠 입력 시험',kind:'dead',state:'dead',shape:'one-shot-owner',stages:[],elapsed:'3m',paint:{glyph:p('✕')}}]};
const red=(v,t)=>{const at=v.motion.at(t);return v.motion.cells.map((c,i)=>c.blink?at[i].foreground:null).filter(x=>x!==null);};
// (a) A question to answer here: the lines of its box (11-16: the person's "붉은 바운더리") and nothing else move, the
// letters inside it stand still (the first row, the choices' words, the action, the sent answer); 217 on the even
// seconds, 95 on the odd; the panel's frame, the route box and the head's state glyph never take part.
for(const state of ['idle','blocked']){
 const v=render({q:here,sent,model,state,since:0,height:30}),rows=v.rows.map(str);
 const blink=v.motion.cells.filter(c=>c.blink),text=blink.map(c=>c.ch).join('');
 // The top line `╭─ … ─╮` (its name and count stand still), a wall pair for each of the four rows inside, the bottom line.
 assert(/^╭─+╮│{8}╰─+╯$/.test(text),state+' '+text);
 // Each blink cell stands where the row shows its glyph.
 for(const c of blink){let x=0,found=false;for(const part of v.rows[c.row])for(const ch of part.text){if(x===c.col&&ch===c.ch)found=true;x+=cells(ch);}
  assert(found,JSON.stringify(c));}
 // No blink cell on the head's border, the route box, or a letter of the sent answer's row (its two walls do).
 for(const c of blink)assert(c.row>0&&!/휠 입력/.test(rows[c.row])&&(!/✓ 답 보냄/.test(rows[c.row])||c.ch==='│'),rows[c.row]);
 // The two reds follow Fleet's one-second beat of the clock: 217 through frames 0-9, 95 through 10-19, and so on.
 for(const [t,want] of [[0,217],[9,217],[10,95],[19,95],[20,217],[29,217],[30,95],[60,217]])
  assert(red(v,t).every(x=>x===want),state+' '+t+' '+red(v,t));
 // Waiting, the frame breathes beside it (its cells keep their own look); idle, the blink alone moves.
 assert.equal(v.motion.cells.some(c=>!c.blink),state==='blocked',state);
 // The next change: the next whole second of the beat (or the breath's next step when sooner).
 if(state==='idle')assert.deepEqual([0,5,10,19,30,47].map(t=>v.motion.next(t)),[10,10,20,20,40,50]);
 else assert(v.motion.next(0)<=10&&v.motion.next(13)<=20,String([v.motion.next(0),v.motion.next(13)]));
 // A whole paint's red is the still accent (174) until the host splices the beat's look in.
 assert(v.rows.flat().filter(x=>x.blink).every(x=>x.foreground===174));
}
// (b) Answered elsewhere: the `◑` and the guide line's count (red now) blink, the words do not.
{const v=render({q:there,model:{...model,wait_guide:'1 QUESTIONS · 아래 입력창에 답'},since:0,height:30});
 const blink=v.motion.cells.filter(c=>c.blink).map(c=>c.ch).join('');assert.equal(blink,'◑1',blink);
 const guide=v.rows.find(r=>str(r).includes('아래 입력창에 답'));
 assert(guide.find(x=>x.text==='1').foreground===174&&guide.find(x=>/QUESTIONS · 아래/.test(x.text)).dim===true,JSON.stringify(guide));}
// (c) The folded strip: `◑답 대기 1`'s glyphs alone, on the same beat; the session's glyph keeps its spin.
{const v=render({q:here,model,folded:true,width:120,height:1,state:'working',since:0});
 assert.equal(v.motion.cells.map(c=>c.ch).join(''),'◑답대기1');assert.equal(v.animated,true);
 assert(red(v,0).every(x=>x===217)&&red(v,10).every(x=>x===95),String(red(v,10)));
 const strip=str(v.rows[0]);
 for(const c of v.motion.cells){let x=0,found=false;for(const ch of strip){if(x===c.col&&ch===c.ch)found=true;x+=cells(ch);}assert(found,JSON.stringify(c));}}
// (d) `rule motion` off, mono, NO_COLOR, eight colors: nothing blinks (idle: nothing moves at all).
for(const [cfg,m] of [[{rule_motion:'off'},{}],[{theme:'mono'},{}],[{},{no_color:true}],[{},{colors:8}]]){
 for(const folded of [false,true]){const v=render({q:here,model:{...model,...m},cfg,folded,width:folded?120:44,height:folded?1:30});
  assert(!v.motion||!v.motion.cells.some(c=>c.blink),JSON.stringify([cfg,m,folded]));
  if(!folded)assert.equal(v.motion,null,JSON.stringify([cfg,m]));}}
// (e) No wait: no blink cell and, idle, no motion and no beat.
for(const folded of [false,true]){const v=render({model:{...model,wait:''},folded,width:folded?120:44,height:folded?1:30});
 assert.equal(v.motion,null,String(folded));assert.equal(v.animated,false,String(folded));}
// (f) A light face: its own pair (88, then 167).
{const v=render({q:here,model,cfg:{theme:'light'},since:0,height:30});assert(red(v,0).every(x=>x===88)&&red(v,10).every(x=>x===167));}
// (g) The two reds are FACES' values (the person tunes them on the installed copy).
assert.deepEqual([pv.FACES.dark.waitBlink,pv.FACES.light.waitBlink],[[217,95],[88,167]]);
""")

    def test_the_folded_strip_keeps_n_route_whole_from_30_cells(self):
        # Review RV-PR1652-A A5: at 30-32 cells with the answer wait beside it, `N ROUTE` was cut (`1 ROU…`).
        self.node(r"""
for(const width of [30,31,32,33,40])for(const wait of ['','◑답 대기 1','◑답 대기 12'])for(const n of [0,1,3,12]){
 const model={repo_side:[p('hearting')],wait,route_label:n?'route '+n+' ⠏1':'',route_count:n,route_total:n,
  routes:Array.from({length:n},(_,i)=>({id:'r'+i,title:'t'+i,kind:'working',paint:{glyph:p('⠸')}})),messages:[{way:'←',who:'[e7]',text:'x'}]};
 for(const tag of ['[e9]','[steward]']){
  const v=render({model:{...model,tag},folded:true,width,height:1,state:'working'}),row=str(v.rows[0]),label=width+' '+JSON.stringify([wait,n,tag])+' '+row;
  assert.equal(cells(row),width,label);assert(row.endsWith('▴'),label);
  if(n)assert(row.includes(n+' ROUTE'),label);
  if(wait)assert(row.includes(wait),label);
  assert(!row.includes('…'),label);
  // The presses stay on their words.
  if(wait){const h=v.hits.find(x=>x.action==='question');assert(h,label);}
  if(n){const h=v.hits.find(x=>x.action==='popup:ROUTE');assert(h&&h.right<width-1,label);}
 }}
""")

    def test_the_popup_names_status_in_english_and_a_request_body_carries_its_tables(self):
        self.node(r"""
const v=pv.popup({cols:100,rows:30,kind:'table',title:[p('STATUS')],body:[[p('x')]],sections:['STATUS','QUESTIONS','ROUTE','MESSAGES'],current:0,model:{styles},cellWidth:cells});
const bottom=v.rows.map(str).find(r=>r.includes('╰─'));assert(bottom.includes('╰─ STATUS · QUESTIONS · ROUTE · MESSAGES '),bottom);
const at=bottom.indexOf('STATUS'),left=[...bottom.slice(0,at)].reduce((n,c)=>n+cells(c),0)+v.left;
const hit=v.hits.find(h=>h.action==='popup:STATUS');assert(hit&&hit.left===left&&hit.right===left+5,JSON.stringify([hit,left]));
assert.deepEqual(v.hits.filter(h=>h.action.startsWith('popup:')).map(h=>h.action),['popup:STATUS','popup:QUESTIONS','popup:ROUTE','popup:MESSAGES']);
// A request body: its lines wrap as before; its drawn table rows stand two cells in, as Fleet drew them.
const model={styles,requests:[{id:'q1',way:'→',who:'[73]',title:'표 요청',body:['| 이름 | 값 |','|---|--:|','| 가 | 12 |','끝 줄'],
 body_parts:[[p('이름',{foreground:253,bold:true}),p('   '),p('값',{foreground:253,bold:true})],[p('┈┈┈┈┈┈┈┈┈',{dim:true})],[p('가'),p('     '),p('12')],'끝 줄']}]};
const rows=pv.messagesBody({model,columns:60,cellWidth:cells}).map(str);
assert.deepEqual(rows.slice(1),['  이름   값','  ┈┈┈┈┈┈┈┈┈','  가     12','  끝 줄'],rows.join('\n'));
// An older backend (no `body_parts`): the lines wrap as they did.
assert.deepEqual(pv.messagesBody({model:{styles,requests:[{...model.requests[0],body_parts:undefined}]},columns:60,cellWidth:cells}).map(str).slice(1),
 ['  | 이름 | 값 |','  |---|--:|','  | 가 | 12 |','  끝 줄']);
""")

    def test_a_popup_row_carries_no_c1_control_to_the_terminal(self):
        # The host writes each popup row through one rule (`view.strings`): C0, DEL and C1 (U+0080-U+009F, the
        # 8-bit CSI U+009B among them) become a space, for every popup (questions, MESSAGES, ROUTE, status).
        source = (SHELL / "host.cjs").read_text()
        line = next(row for row in source.splitlines() if "view.strings=view.rows.map" in row)
        self.node(r"""
const line=""" + json.dumps(line) + r""",m=/\.replace\((\/\[[^\/]+\]\/g),' '\)/.exec(line);
assert(m,line);const rule=eval(m[1]);
for(const c of ['\x00','\x1b','\x7f','\x80','\x85','\x9b','\x9f'])assert.equal(('a'+c+'b').replace(rule,' '),'a b',JSON.stringify(c));
assert.equal('표 ┈ 현황 · ✓'.replace(rule,' '),'표 ┈ 현황 · ✓');
""")

    def test_the_status_popup_rows_are_laid_out_by_fleets_measure(self):
        # The backend's status rows fit the popup by Fleet's `cell_width` and carry it per run; the host's popup lays
        # a run out by those cells, so a character the two measures disagree on (a Thai mark, a new emoji) moves
        # nothing. An answer past the host's 60 KiB keeps the rows that fit and one dim `…`.
        sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
        import session_shell
        from fleet import session_status
        from fleet.explorer.grid import cell_width
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            doc = Path(folder) / "현황.md"
            doc.write_text("# 현황\n\n| 이름 | 값 |\n|:--|--:|\n| ไท่ย | 🥱 하품 |\n| 표 | 12 |\n")
            view = {"word": "STATUS", "has_doc": True, "campaign": "c", "age": "3m", "title": "STATUS c · 3m",
                    "path": str(doc), "slot": "", "watch": [str(doc)]}
            with mock.patch.object(session_status, "read", return_value=view), \
                    mock.patch.object(session_shell, "_popup_view_model", side_effect=RuntimeError("no Fleet here")):
                result = session_shell._status_popup({"harness": "claude", "sid": "s"}, {"cwd": folder, "routes": []}, 40, 256, False)
                self.assertTrue(result["ok"] and result["title"] == "STATUS c · 3m", result)
                self.assertTrue(any("ไท่ย" in run[0] for row in result["rows"] for run in row), result["rows"])
                for row in result["rows"]:
                    self.assertTrue(all(run[2] == cell_width(run[0]) for run in row), row)
                    self.assertLessEqual(sum(run[2] for run in row), 40, row)
                doc.write_text("# 현황\n\n" + "".join("- 긴 기록 줄 %04d 한글 글자가 여럿 든 줄\n" % n for n in range(4000)))
                big = session_shell._status_popup({"harness": "claude", "sid": "s"}, {"cwd": folder, "routes": []}, 100, 256, False)
                weight = len(json.dumps({"id": 0, **big}, ensure_ascii=False).encode())
                self.assertLessEqual(weight, session_shell.POPUP_ANSWER_BYTES)
                self.assertEqual(big["rows"][-1][0][0], "…")
                # A request without the session's folder is refused.
                self.assertFalse(session_shell._status_popup({"harness": "claude", "sid": "s"}, {"cwd": "relative", "routes": []}, 40, 256, False)["ok"])
        self.node(r"""
// The host's measure says a part is 1 cell; Fleet measured 3: the row is laid out by Fleet's 3.
const one=s=>[...s].length;
const v=pv.popup({cols:60,rows:12,kind:'table',title:[p('현황')],body:[[p('ab',{cells:3}),p('c')]],sections:['STATUS'],model:{styles},cellWidth:one});
const row=v.rows.find(r=>str(r).includes('abc'));const inside=row.slice(row.findIndex(x=>x.text.includes('│'))+1);
const pad=inside.find(x=>/^ +$/.test(x.text)&&x.text.length>2);
assert.equal(pad.text.length,v.inner-4,JSON.stringify(row));
assert(!row.some(x=>'cells' in x),'the measure is not drawn');
""")


if __name__ == "__main__":
    unittest.main()
