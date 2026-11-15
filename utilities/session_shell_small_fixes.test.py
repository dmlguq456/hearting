"""11-15 (panel small fixes): six small things after the question button and the popup veil.

1. A popup's key line is never cut in a word: it breaks between its ` · ` pieces (the question popup, the
   table popups and the MESSAGES popup all use `panelView.keyRows`), and the closing key stays.
2. The choices row of a question to answer here always shows the first choice (its name cut with `…`,
   its number kept), the rest counted `+N`; the folded strip's row of what to do follows the same rule.
3. The sessions section puts every session Fleet's table counts as waiting on a person first, whatever
   glyph it paints, and counts it apart from the idle sessions of the same glyph.
4. `veilRow` also turns colon-form SGR gray.
5. The question summary's `answerable` is `active(row)`, the one formula.
6. The head rule stands without a session title (the default `rule motion` must not wait on one).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

HEAD = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const shell=process.argv[1],root=path.join(shell,'..','..');
const view=require(path.join(shell,'panel-view.cjs'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const messages=JSON.parse(fs.readFileSync(path.join(root,'tools/i18n/messages.json'),'utf8')).messages;
const words=(lang,prefix)=>Object.fromEntries(Object.entries(messages).filter(([k])=>k.startsWith(prefix)).map(([k,v])=>[k.slice(prefix.length),v[lang]]));
const str=r=>r.map(x=>x.text).join('');
"""


class SmallFixesTest(unittest.TestCase):
    def node(self, body, *args):
        result = subprocess.run(["node", "-e", HEAD + body, str(SHELL), *args], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_question_popups_key_line_breaks_between_pieces_and_keeps_the_closing_key(self):
        # `↑↓ choose · Enter send · t text · Tab next · ←→ section · Esc/Ctrl+] back` is 73 cells: in the 72-cell
        # popup it was cut at `bac`. Every window width 20-200, both languages, every kind of question.
        self.node(r"""
const create=require(path.join(shell,'block-input.cjs'));
const kinds={choice:{answerable:true,questions:[{id:'q',question:'Zq body',choices:['AAA','BBB (추천)'],recommended:1}]},
 text:{answerable:true,questions:[{id:'q',question:'Zq body'}]},
 decision:{answerable:true,answer_kind:'decision',questions:[{id:'q',question:'Zq body'}]},
 there:{answerable:false,answer_state:'answer_there',questions:[{id:'q',question:'Zq body'}]}};
let shown=0;
for(const lang of ['en','ko'])for(const [kind,item] of Object.entries(kinds)){
 const labels=words(lang,'fleet.session_block.'),close=lang==='en'?'back':'돌아가기';
 for(const rows of [10,24,40])for(let cols=20;cols<=200;cols++){
  const inner=view.popupWidth(cols,'text')-4,room=view.popupRoom(rows,0);
  const b=create({changed(){},answer:async()=>({state:'submitted'}),action(){},cellWidth:cells});
  b.update([{id:'x',revision:'r',...item}],'',{labels,styles:{}});b.enter();
  const out=b.detail({budget:room+1,columns:inner}).map(str),tag=kind+' '+lang+' '+rows+'x'+cols+'\n'+out.join('\n');
  const known=new Set(Object.values(labels).flatMap(v=>String(v).split(' · ').flatMap(p=>p.split(' '))).filter(Boolean));
  const isKey=s=>s.replace(/ · /g,' ').split(' ').filter(Boolean).every(w=>known.has(w));
  let first=out.length;while(first>0&&isKey(out[first-1]))first--;
  const foot=out.slice(first);
  assert(foot.length>=1,'a key line stands\n'+tag);
  assert(foot.every(r=>cells(r)<=inner),'within the popup\n'+tag);
  assert(foot.at(-1).split(' ').at(-1)===close,'the closing key stays last\n'+tag);
  if(rows===40){ // room for every piece: all words, in order, none cut
   const all=foot.join(' ').replace(/ · /g,' ').split(' '),want=labels.navigate.replace(/ · /g,' ').split(' ');
   assert.deepEqual(all.slice(-want.length),want,tag);
  }
  shown++;
 }
}
assert(shown>2000,String(shown));
""")

    def test_the_table_and_messages_popups_break_their_key_lines_by_the_same_rule(self):
        # The routes, LAB, sessions and MESSAGES popups: their keys (`←→ section · PgUp/PgDn · p fold panel · f Fleet ·
        # Esc/Ctrl+] back`) break between pieces at any width 20-200 and the closing key stays, down to the lowest
        # window the popup keeps rows in; a piece wider than a row (narrowest windows) breaks at its spaces only.
        self.node(r"""
const src=fs.readFileSync(path.join(shell,'host.cjs'),'utf8'),a=src.indexOf('function wrapGuide('),b=src.indexOf("// A table popup's title");
assert(a>=0&&b>a);
const ctx={panelView:view,cellWidth:cells};vm.createContext(ctx);vm.runInContext(src.slice(a,b)+'\nthis.wrap=wrapGuide;',ctx);
let shown=0;
for(const lang of ['en','ko']){
 const l=words(lang,'fleet.session_panel.'),close=lang==='en'?'back':'돌아가기';
 const known=new Set(Object.values(l).flatMap(v=>String(v).replace('{panel}','p fold panel').split(' · ').flatMap(p=>p.split(' '))).filter(Boolean).concat(['3/20']));
 for(const own of ['',l.popup_route,l.popup_route_page,l.popup_sessions])for(const place of ['','3/20'])for(const fold of [l.popup_fold,l.popup_unfold])
  for(let cols=20;cols<=200;cols++)for(const rows of [8,12,24]){
   const lines=[place,own,l.popup_keys.replace('{panel}',fold)].filter(Boolean);
   const inner=view.popupWidth(cols,'table')-4,room=view.popupRoom(rows,0),limit=Math.max(1,room-1);
   const got=ctx.wrap(lines,inner,limit),tag=lang+' '+rows+'x'+cols+' '+JSON.stringify(got);
   assert(got.length>=1&&got.length<=limit,tag);
   assert(got.every(r=>cells(r)<=inner),tag);
   assert(got.every(r=>r.replace(/ · /g,' ').split(' ').filter(Boolean).every(w=>known.has(w))),'no word is cut\n'+tag);
   assert.equal(got.at(-1).split(' ').at(-1),close,'the closing key stays\n'+tag);
   // In a popup frame (the MESSAGES popup is the keys alone): painted whole, never cut.
   const f=view.popup({cols,rows:Math.max(rows,8),kind:'table',title:[{text:'MESSAGES'}],body:got.map(t=>[{text:t,dim:true}]),sections:['MESSAGES'],current:0});
   const painted=f.rows.map(str).join('\n');
   for(const r of got.slice(0,f.body))assert(painted.includes(r),'painted whole\n'+r+'\n'+painted);
   shown++;
  }
}
assert(shown>5000,String(shown));
// One piece wider than the row: broken at its spaces, the closing key last.
assert.deepEqual(view.keyRows(['Tab next','Esc/Ctrl+] close the popup now'],16),['Tab next','Esc/Ctrl+] close','the popup now']);
// Short of rows the pieces before the last go first, from the end backward.
assert.deepEqual(view.keyRows(['a','b','c','Esc'],4,{rows:1}),['Esc']);
assert.deepEqual(view.keyRows(['aaaa','bbbb','cccc','Esc'],10,{rows:2}),['aaaa','bbbb · Esc']);
""")

    CHOICE = r"""
const p=(text,extra={})=>({text,...extra});
const styles={dim:{foreground:-1,dim:true},loc_repo:{foreground:253},g_blocked:{foreground:217},lvl_g:{foreground:150}};
const model={tag:'[e9]',repo:[p('hearting/')],selected:[p('Opus 5.5')],wait:'◑답 대기 1',styles,labels:{answer_action:'↵ 답하기 · Ctrl+]'},
 session_title:'패널 손질',now:'그리는 중',route_label:'route 1',route_count:0};
const choice=(first,n)=>({count:1,label:'QUESTIONS',answerable:true,
 rows:[[p('◑ ',{foreground:217}),p('로그를 어느 쪽에 남길까요?',{foreground:253})],
  [p('  ',styles.dim),p('1 ',styles.dim),p(first,{foreground:253}),...[...Array(n-1)].flatMap((_,i)=>[p(' · ',styles.dim),p((i+2)+' ',styles.dim),p('둘 '+(i+2),{foreground:253})])]],
 one:[[p('◑ ',{foreground:217}),p('로그를 어느 쪽에 남길까요?',{foreground:253})]],line:[p('로그를 어느 쪽에 남길까요?',{foreground:253})],choices:n});
const paint=(o)=>view.render({width:o.width,height:o.height||30,side:!o.folded,fill:!o.folded,folded:!!o.folded,
 model:{...model,...(o.model||{})},state:'idle',sid:'s',harness:'claude',frame:0,brief:()=>o.q,sent:()=>[],configuration:{},background:''});
const firsts=['ＡＢＣＤＥＦ (추천)','파일','🙂🙂🙂 emoji label that is long','x'.repeat(80),'a','한글로 아주 긴 첫 선택지의 이름입니다'];
"""

    def test_the_choices_row_always_shows_the_first_choice(self):
        # Inner width 26 and a first choice `ＡＢＣＤＥＦ (추천)` left only `+3`. Now the first choice shows (its name cut
        # with `…`, its number kept) and the rest are counted; never wider than the panel (Korean, full-width,
        # emoji), heights 5-60 x widths 12-60; the folded strip's row of what to do follows the same rule.
        self.node(self.CHOICE + r"""
let rows=0;
for(const first of firsts)for(const n of [1,2,5]){
 for(let h=5;h<=60;h+=1)for(let w=12;w<=60;w++){
  const v=paint({q:choice(first,n),height:h,width:w}),lines=v.rows.map(str),tag=h+'x'+w+' '+first+' '+n+'\n'+lines.join('\n');
  assert(lines.length<=h&&lines.every(r=>cells(r)===w),tag);
  const chip=v.rows.find(r=>r.some(x=>x.background===235||x.background===255));
  if(!chip)continue;
  const text=chip.filter(x=>x.background===235).map(x=>x.text).join(''),line=str(chip);
  assert(/^ 1/.test(text),'the first chip, its number kept\n'+tag);
  const first1=[...first].slice(0,1).join('');
  if(w>=16)assert(text.includes(first1)||first.length<1,'its name starts\n'+tag);
  assert(text.includes(first)||text.includes('…'),'whole or cut with …\n'+tag);
  if(n>1&&!text.includes('2 '))assert(/\+\d+/.test(line),'the rest counted\n'+tag);
  rows++;
 }
}
assert(rows>5000,String(rows));
// The folded strip's row of what to do.
let strips=0;
for(const first of firsts)for(const n of [1,2,4])for(let w=20;w<=90;w++){
 const v=paint({q:choice(first,n),folded:true,width:w,height:4}),lines=v.rows.map(str);
 assert(lines.every(r=>cells(r)<=w),w+'\n'+lines.join('\n'));
 const todo=lines.find(r=>r.startsWith('◑답 대기')),bare=paint({q:{...choice(first,n),choices:0,rows:[choice(first,n).rows[0]]},folded:true,width:w,height:4}).rows.map(str).find(r=>r.startsWith('◑답 대기')).trimEnd();
 assert(todo,w+'\n'+lines.join('\n'));
 if(w-1-cells(bare)>=5)assert(todo.includes(' · 1'),'the first choice stands (room '+(w-1-cells(bare))+')\n'+w+' '+first+'\n'+todo);
 if(n>1&&w-1-cells(bare)>=9&&!todo.includes(' · 2 ')&&todo.includes(' · 1'))assert(/\+\d+\s*$/.test(todo),'the rest counted\n'+todo);
 strips++;
}
assert(strips>600,String(strips));
// The case from the review: inner width 26, `ＡＢＣＤＥＦ (추천)`, three more choices.
{const lines=paint({q:choice('ＡＢＣＤＥＦ (추천)',4),width:30}).rows.map(str);assert(lines.some(r=>/│   1 ＡＢＣＤＥＦ.*\+3 +│/.test(r)),lines.join('\n'));}
""")

    def test_the_sessions_section_leads_with_every_session_waiting_on_a_person(self):
        # Fleet paints a native decision's wait `●` (blocked color), not `◑`: that session was ranked with the idle
        # ones, left off the three lines, and its count merged into the idle `●N` in one color.
        from fleet.palette import style_components
        import session_shell
        rows = [
            {"tag": "10", "harness": "claude", "state": "working", "title": "me", "age": "1m"},                       # own
            {"tag": "11", "harness": "codex", "state": "idle", "title": "idle one", "age": "2m"},
            {"tag": "12", "harness": "opencode", "state": "idle", "title": "idle two", "age": "3m"},
            {"tag": "13", "harness": "claude", "state": "working", "title": "runs", "age": "4m"},
            {"tag": "14", "harness": "claude", "state": "working", "title": "native decision", "age": "5m",
             "ask": "answer needed", "answer_wait_marker": "●"},
            {"tag": "15", "harness": "codex", "state": "idle", "title": "gate", "age": "6m",
             "ask": "answer needed", "answer_wait_marker": "◑"},
            {"tag": "16", "harness": "claude", "state": "idle", "title": "idle three", "age": "7m"},
        ]
        from fleet import session_tags
        original = session_tags.assigned_tag
        session_tags.assigned_tag = lambda harness, sid: "10"
        self.addCleanup(setattr, session_tags, "assigned_tag", original)
        out = session_shell.session_summary(SimpleNamespace(sessions=rows), "claude", "s")
        tags = [next(p["text"] for p in row["tag"] if p["text"] not in "[]") for row in out["rows"] if not row["own"]]
        self.assertEqual(set(tags[:2]), {"14", "15"}, "the sessions waiting on a person lead: " + str(tags))
        self.assertEqual(tags[2], "13", "then the running, then the idle: " + str(tags))
        counts = [part[0]["text"] for part in out["counts"]]
        self.assertEqual(counts, ["◑1", "●1", "⠏1", "●3"], counts)
        waiting, idle = [part[0] for part in out["counts"] if part[0]["text"].startswith("●")]
        self.assertNotEqual({k: v for k, v in waiting.items() if k != "text"}, {k: v for k, v in idle.items() if k != "text"},
                            "the waiting `●` and the idle `●` are different colors, so different groups")
        # A row's glyph and color are Fleet's session table's own.
        glyphs = {next(p["text"] for p in row["tag"] if p["text"] not in "[]"): row["glyph"][0] for row in out["rows"]}
        self.assertEqual(glyphs["14"], {"text": "●", **style_components("blocked", colors=256, no_color=False)})
        self.assertEqual(out["count"], 6)

    def test_the_sessions_key_follows_the_same_set(self):
        # Another session's wait wakes the section only while that session is one Fleet counts as waiting: an owner, a
        # current wait (not folded as older), one a person must answer (not a blocked owner's, not an answer on its way).
        import session_shell
        item = lambda n, **extra: {"id": "w" + str(n), "source": "S1", "owner": {"harness": "codex", "session_id": "s" + str(n)}, **extra}
        key = lambda items: session_shell._sessions_key({"items": items}, "claude", "me")
        nothing = key([])
        self.assertEqual(nothing, "")
        for quiet in (item(1, stale=True), item(2, source="S3"), item(3, answer="sent"),
                      {"id": "w4", "source": "S1", "owner": {}}, {"id": "w5", "source": "S1", "owner": {"harness": "claude", "session_id": "me"}}):
            self.assertEqual(key([quiet]), "", quiet)
        self.assertNotEqual(key([item(6)]), "")
        self.assertEqual(key([item(6), item(1, stale=True)]), key([item(6)]), "an older wait changes nothing")
        self.assertNotEqual(key([item(6)]), key([item(7)]))

    def test_veil_row_turns_colon_form_sgr_gray_too(self):
        self.node(r"""
const src=fs.readFileSync(path.join(shell,'host.cjs'),'utf8'),a=src.indexOf('function veilRow('),b=src.indexOf('// Before a frame');
const ctx={};vm.createContext(ctx);vm.runInContext(src.slice(a,b)+'\nthis.veil=veilRow;',ctx);
const veil=s=>ctx.veil(s,239),gray='\x1b[0;38;5;239m';
// The `;` forms as before: a color number that is 8 is not hidden text.
assert.equal(veil('\x1b[38;5;196mA\x1b[0m'),gray+'A'+gray);
assert.equal(veil('\x1b[38;5;8m\x1b[48;2;8;8;8mx'),gray+gray+'x');
assert.equal(veil('\x1b[1;8mH'),'\x1b[0;38;5;239;8mH');
// The colon forms (`38:5:196`, `38:2::r:g:b`, `4:3`, `58:5:9`) are turned gray, and one with an 8 in it is no hidden text.
for(const raw of ['38:5:196','38:2::10:20:30','48:5:17','4:3','58:5:9','1;38:5:8','38:5:8']){
 const out=veil('\x1b['+raw+'mT');assert.equal(out,gray+'T',raw);
}
assert.equal(veil('\x1b[8mH'),'\x1b[0;38;5;239;8mH');
assert.equal(veil('no escape'),'no escape');
""")

    def test_the_summary_answerable_is_the_one_formula_of_active(self):
        # Another session's question (opened from the sessions popup) reads `answerable` as the own session's `active(row)`.
        src = (SHELL / "block-input.cjs").read_text()
        summary = src[src.index("function summary()"):]
        self.assertNotIn("answerable:!stale", summary, "no second formula for the same meaning")
        self.assertEqual(summary.count("answerable:active("), 2, "both summaries use active(row)")
        self.node(r"""
const create=require(path.join(shell,'block-input.cjs'));
const make=()=>create({changed(){},answer:async()=>({state:'submitted'}),action(){}});
const item=(id,extra={})=>({id,revision:'r',answerable:true,questions:[{id:'q',question:'Q?'}],...extra});
// The panel's summary is this session's own question, whatever the popup shows.
for(const [extra,want] of [[{},true],[{answerable:false},false]]){
 const own=make();own.update([item('a',extra)],'',{});assert.equal(own.summary().answerable,want);
 const b=make();b.update([item('a',extra)],'',{});
 b.enterForeign({tag:'7',owner:{harness:'codex',session_id:'x'},items:[item('f')]});
 assert.equal(b.summary().answerable,want,'another session\'s question open: the same word');
}
""")

    def test_the_head_rule_stands_without_a_session_title(self):
        # The default `rule motion` moves a light over the head rule; a session with no title (a new one, any
        # harness) had no rule, so nothing moved. The rule now stands where something stands above it.
        self.node(r"""
const p=(text,extra={})=>({text,...extra});
const styles={dim:{foreground:-1,dim:true},loc_repo:{foreground:253}};
const base={tag:'[e9]',repo:[p('hearting/')],repo_side:[p('hearting')],selected:[p('Opus 5.5')],context_side:[p('━━─── '),p('46%')],styles,route_count:0};
const paint=(harness,extra,o={})=>view.render({width:o.width||40,height:o.height||30,side:o.side!==false,fill:o.side!==false,model:{...base,...extra},state:'working',sid:'s',harness,
 frame:0,brief:()=>null,sent:()=>[],configuration:{},background:''});
for(const harness of ['claude','codex','opencode'])for(const [name,extra] of [['title',{session_title:'제목'}],['none',{}],['now',{now:'그리는 중'}]]){
 const v=paint(harness,extra),lines=v.rows.map(str),tag=harness+' '+name+'\n'+lines.join('\n');
 const rule=lines.findIndex(r=>/^│ ─{36} │$/.test(r));
 assert(rule>0,'the head rule stands\n'+tag);
 assert(v.motion&&v.motion.cells.length>0&&v.motion.cells.every(c=>c.row===rule),'the default light moves over it\n'+tag);
 if(name==='none')assert.equal(lines[rule+1],'│'+' '.repeat(38)+'│','no title: the rule is the parting, no extra empty row\n'+tag);
 else assert.equal(lines[rule+1].includes('TITLE')||lines[rule+1].includes('NOW'),true,tag);
}
// The bottom layout has none (the border alone is bright).
const bottom=view.render({width:120,height:6,side:false,fill:false,model:{...base},state:'working',sid:'s',harness:'claude',frame:0,brief:()=>null,sent:()=>[],configuration:{},background:''});
assert(!bottom.rows.slice(1,-1).map(str).some(r=>/─{30}/.test(r)));
""")


if __name__ == "__main__":
    unittest.main()
