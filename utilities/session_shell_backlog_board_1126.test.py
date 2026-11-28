"""11-26 (scheme A; the person: "A — FIX · TASK · NOTE에 상태(Discuss 등)와 영역을 더하고, 판은 영역별로 묶어 봄" / earlier: "여전히 FIX, IMPR로 뜨네?"):
the backlog's kinds, states, areas and dates, read in one place, and the board the BACKLOG head opens.

1. One reading (`utilities/backlog_items.py`): `kind:` is `fix`, `task` or `note` (the earlier `improve` is `task`; else TBD), `state:` is `discuss`, `ready`
   or `waiting` (else none), `area:` is one area per item (case and runs of blanks folded, the first name written is the group's name; none or two
   different is `No area`), `date:` a real `YYYY-MM-DD` (else none). A wrong value leaves that cell undecided and the document opens; nothing is written.
2. The panel's three rows: the newest item in Discuss first, then the other newest two (none in Discuss: the newest three); each row carries its kind,
   state, area and date for the drawing.
3. The board (`utilities/session-shell/backlog-board.cjs` over `session_status.backlog_board`): the counts of each kind, the whole count and Discuss on
   top; `k`/`a`/`d` group by kind, area, date; inside a group the newest first, the same date in the document's order, no date last; Discuss and
   Waiting before the title; `/` finds a title, `Tab` first an area; `t` shows All, Discuss, Ready, Waiting, No state; Home/End; Enter opens one item's
   lines (date, kind, area, state, campaign, source, note: one item read). The state is the open board's alone.
4. The read limits (1 MiB of the file, 200 KiB of the answer) say how many were read and that there are more, on 1,000-item documents.
5. The board as the product draws it (`utilities/status_popup_picture.py`): every row the frame's width.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
NODE = shutil.which("node")
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

SAMPLE = """# Sample backlog
갱신 2026-11-26 09:30

## 2026-11-26_sample-a
- Sample route resume loses its stage
  date: 2026-11-24
  kind: fix
  area: Routing
  state: ready
  source: sample review
  note: Sample note that says what was seen.
- Sample panel head hides the count
  date: 2026-11-26
  kind: improve
  area:   panel
- Sample question popup forgets the answer
  date: 2026-11-25
  kind: FIX
  area: Questions
  state: Discuss
- Sample routing note to keep
  date: 2026-11-26
  kind: note
  area:  ROUTING
- Sample undated job
  kind: task
  area: Panel
  state: waiting

## Unassigned
- Sample worker leg runs twice
  date: 2026-11-26
  kind: task
  area: Workers
  state: discuss
- Sample wrong values everywhere
  date: 2026-02-30
  kind: bug
  state: later
  area: Fleet
  area: Lab
- Sample same title
  date: 2026-11-20
  kind: fix
"""


def parse(text):
    from fleet import session_status
    return session_status.parse_backlog(text.splitlines(), detail=True)


def items_of(parsed):
    return [item for group in parsed["groups"] for item in group["items"]]


class ReadingTest(unittest.TestCase):
    def test_kind_state_area_and_date_are_read_by_one_rule(self):
        got = [(i["text"], i["kind"], i["state"], i["area"], i["date"]) for i in items_of(parse(SAMPLE))]
        self.assertEqual(got, [
            ("Sample route resume loses its stage", "fix", "ready", "Routing", "2026-11-24"),
            ("Sample panel head hides the count", "task", None, "panel", "2026-11-26"),          # the earlier `improve` is TASK
            ("Sample question popup forgets the answer", "fix", "discuss", "Questions", "2026-11-25"),
            ("Sample routing note to keep", "note", None, "Routing", "2026-11-26"),              # folded into the first name written
            ("Sample undated job", "task", "waiting", "panel", None),
            ("Sample worker leg runs twice", "task", "discuss", "Workers", "2026-11-26"),
            ("Sample wrong values everywhere", None, None, None, None),                          # each wrong value undecided; the job still reads
            ("Sample same title", "fix", None, None, "2026-11-20")])

    def test_the_cases_of_each_line(self):
        import backlog_items
        cases = [
            ("- a\n  kind: task\n  kind: improve\n", {"kind": "task"}),       # one kind written two ways
            ("- a\n  kind: fix\n  kind: task\n", {"kind": None}),
            ("- a\n  kind:\n", {"kind": None}),
            ("- a\n  KIND:  Note \n", {"kind": "note"}),
            ("- a\n  state: READY\n  state: ready\n", {"state": "ready"}),
            ("- a\n  state: ready\n  state: waiting\n", {"state": None}),
            ("- a\n  state:\n", {"state": None}),
            ("- a\n  area:\n", {"area": None}),
            ("- a\n  area: Ab  Cd\n- b\n  area: ab cd\n  area: AB\tCD\n", {"area": "Ab Cd"}),
            ("- a\n  area: x\n  area: y\n", {"area": None}),
            ("- a\n  date: 2026-11-26\n  date: 2026-11-26\n", {"date": "2026-11-26"}),
            ("- a\n  date: 2026-11-26\n  date: 2026-11-25\n", {"date": None}),
            ("- a\n  date: 2026-1-2\n", {"date": None}),
            ("- a\n  date: someday\n", {"date": None}),
            ("- a\n```\n  kind: fix\n```\n", {"kind": None}),                 # a fence ends the job: the line is not its
            ("- a\n  state : discuss\n", {"state": None}),                    # a blank before the colon is no such line
        ]
        for body, want in cases:
            job = backlog_items.parse(("## A\n" + body).splitlines())["groups"][0]["jobs"][0]
            for name, value in want.items():
                self.assertEqual(job[name], value, (body, name))
        counts = backlog_items.counts(backlog_items.parse(SAMPLE.splitlines()))
        self.assertEqual(counts, {"fix": 3, "task": 3, "note": 1, "tbd": 1, "discuss": 2, "ready": 1, "waiting": 1, "total": 8})

    def test_nothing_is_written_and_a_strange_document_still_opens(self):
        from fleet import session_status
        with tempfile.TemporaryDirectory(prefix="bb") as folder:
            path = Path(folder) / "backlog.md"
            for text in (SAMPLE, "\x00\x01 ## \n- \n  kind: \x00\n", "## A\n- a\n  area: " + "x" * 5000 + "\n  date: 9999-99-99\n"):
                path.write_text(text, encoding="utf-8")
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                with mock.patch.object(session_status, "_root", lambda cwd, now: folder):
                    lists = session_status.panel_lists(folder, {"path": "", "has_doc": False}, {})
                    _doc, listing, view = session_status.backlog_view(folder, "", 100, want_list=True)
                    board = listing()
                self.assertTrue(lists["backlog"]["has_doc"] and view["has_doc"])
                self.assertEqual(board["total"], lists["backlog"]["total"])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


class PanelRowsTest(unittest.TestCase):
    def test_the_newest_discuss_first_then_the_other_newest_and_the_four_values(self):
        # 11-26 (panel B; the person: "백로그는 사용자가 말한 우선순위로 최대 3개 정도만 띄우고"): the panel's rows are the items with a `priority:` line
        # alone (the order the person gave, 1 first; the same number in the document's order), three at most, each with its four values; with none
        # ranked the panel has no row (no date or Discuss fills a place). (The test's name is the scheme-A order it replaced.)
        from fleet import session_status
        self.assertEqual(session_status.panel_backlog_items(parse(SAMPLE), "")[0], [])
        ranked = (SAMPLE.replace("  state: waiting\n", "  state: waiting\n  priority: 1\n")                       # Sample undated job
                  .replace("  note: Sample note that says what was seen.\n", "  note: Sample note that says what was seen.\n  priority: 2\n")   # route resume
                  .replace("  area:  ROUTING\n", "  area:  ROUTING\n  priority: 2\n")                              # routing note
                  .replace("  area: Lab\n", "  area: Lab\n  priority: first\n")                                  # a wrong value: not ranked
                  .replace("  date: 2026-11-20\n  kind: fix\n", "  date: 2026-11-20\n  kind: fix\n  priority: 7\n"))  # Sample same title
        items, total = session_status.panel_backlog_items(parse(ranked), "")
        self.assertEqual(total, 8)
        self.assertEqual([(i["text"], i["kind"], i["state"], i["area"], i["date"]) for i in items], [
            ("Sample undated job", "task", "waiting", "panel", ""),                                   # (its area the first-written name: `panel`)
            ("Sample route resume loses its stage", "fix", "ready", "Routing", "2026-11-24"),         # 2, first in the document
            ("Sample routing note to keep", "note", "", "Routing", "2026-11-26")])                    # 2, later (its area the first-written name)
        items, _ = session_status.panel_backlog_items(parse(ranked), "", first=99)
        self.assertEqual([i["text"] for i in items], ["Sample undated job", "Sample route resume loses its stage", "Sample routing note to keep", "Sample same title"])
        # the key of a panel row is the board's key of the same item; the board neither shows nor takes the priority
        board = session_status.backlog_board(parse(ranked))
        self.assertTrue({i["key"] for i in items} <= {row[0] for row in board["items"]})
        self.assertNotIn("priority", json.dumps(board))

    @unittest.skipUnless(NODE, "needs node")
    def test_the_host_hands_the_four_values_on_and_the_panel_says_task(self):
        script = r"""
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const src=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const cut=(from,to)=>{const a=src.indexOf(from),b=src.indexOf(to,a);if(a<0||b<0)throw new Error('marker '+from);return src.slice(a,b);};
const ctx={};vm.createContext(ctx);
vm.runInContext(cut('const BACKLOG_KINDS=','function listTotal(')+'\nthis.listItems=listItems;this.listCounts=listCounts;',ctx);
const items=ctx.listItems({items:[{text:'a',key:'i:a',kind:'task',state:'discuss',area:' Panel ',date:'2026-11-26'},{text:'b',key:'i:b',kind:'improve',state:'later',area:'',date:'26-11'}]});
assert.deepEqual(JSON.parse(JSON.stringify(items)),[{mark:'',text:'a',key:'i:a',kind:'task',state:'discuss',area:'Panel',date:'2026-11-26'},{mark:'',text:'b',key:'i:b'}]);
assert.deepEqual(JSON.parse(JSON.stringify(ctx.listCounts({counts:{fix:1,task:2,note:3,tbd:0,discuss:4}}))),{fix:1,task:2,note:3,tbd:0,discuss:4});
assert.equal(ctx.listCounts({counts:{fix:1,improve:2,note:3,tbd:0}}),null);       // an older backend's counts are not taken as the new ones
const view=require(path.join(process.argv[1],'panel-view.cjs')),cells=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const t=(text,key='dim')=>({text,dim:key==='dim'});
for(const width of [50,44,30]){
  const v=view.render({width,height:30,side:true,model:{styles:{},colors:256,tag:'[e9]',repo_side:[t('repo')],selected:[t('model')],
    backlog_doc:{word:'BACKLOG',has_doc:true,items:[{mark:'',text:'Sample one',key:'i:a',kind:'task'},{mark:'',text:'Sample two',key:'i:b',kind:'fix'}],total:9,counts:{fix:4,task:3,note:2,tbd:0}}},
    state:'working',harness:'claude',frame:1000,since:990,sent:()=>[],cellWidth:cells,configuration:{side_width:width,rule_motion:'off',theme:'terminal',position:'right'}});
  const lines=v.rows.map(r=>r.map(p=>p.text).join(''));
  assert(lines.every(l=>cells(l)===width),lines.join('\n'));
  assert(!lines.some(l=>/IMPR/.test(l)),lines.join('\n'));
  // (11-26, panel B: a row is state first, then the kind's word, the date, the title; the head `+N` the items not shown.)
  assert(lines.some(l=>/· —       TASK —     Sa/.test(l))&&lines.some(l=>/· —       FIX  —     Sa/.test(l)),lines.join('\n'));
  if(width>=44)assert(lines.some(l=>/TASK —     Sample one/.test(l)),lines.join('\n'));
  assert(lines.some(l=>/^│  BACKLOG \+7 +› │$/.test(l)),lines.join('\n'));
}
"""
        result = subprocess.run([NODE, "-e", script, str(SHELL)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


BOARD_HEAD = r"""
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const list=require(path.join(process.argv[1],'status-list.cjs')),board=require(path.join(process.argv[1],'backlog-board.cjs'));
const answer=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),b=board.fromAnswer(answer);
const text=r=>r.map(p=>p.text).join('');
const fresh=()=>{const t={sel:'',fold:new Set(),items:new Set(),more:{}},bs=board.newState();board.prepare(b,bs,t);return {t,bs};};
const screen=({t,bs},width=96)=>list.layout(board.entries(b,bs,{width}),t,{width}).rows.map(text);
const chosen=st=>screen(st).find(l=>l.startsWith('▶ '))||'';
const items=st=>screen(st).filter(l=>/^(▶ |  )  (FIX|TASK|NOTE|TBD) /.test(l)).map(l=>l.slice(4).trim());
"""


@unittest.skipUnless(NODE, "needs node")
class BoardTest(unittest.TestCase):
    def setUp(self):
        from fleet import session_status
        import session_shell
        self.status, self.shell = session_status, session_shell
        self._tmp = tempfile.TemporaryDirectory(prefix="bbd")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        patch = mock.patch.object(session_status, "_root", lambda cwd, now: str(self.root))
        patch.start()
        self.addCleanup(patch.stop)

    def popup(self, part, open="", indent=0):
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        return self.shell._status_popup(binding, {"cwd": str(self.root), "routes": [], "status": {}, "part": part, "own": "", "open": open, "indent": indent},
                                        96, 256, False, "backlog")

    def node(self, body, text=SAMPLE):
        (self.root / "backlog.md").write_text(text, encoding="utf-8")
        answer = self.root / "answer.json"
        answer.write_text(json.dumps(self.popup("list"), ensure_ascii=False), encoding="utf-8")
        result = subprocess.run([NODE, "-e", BOARD_HEAD + body, str(SHELL), str(answer)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_head_and_the_kind_groups_newest_first(self):
        self.node(r"""
const st=fresh(),s=screen(st);
assert.deepEqual([s[0],...s.slice(2,5)],['Sample backlog','8 total · FIX 3 · TASK 3 · TBD 1 · NOTE 1','group [Kind] Area Date · showing 8 · Discuss 2','']);
assert.match(s[1],/^updated (2026-)?11-26 09:30$/);
// the first group open, the others folded with their counts, in the order FIX, TASK, TBD, NOTE
assert.deepEqual(s.filter(l=>/[▾▸] (FIX|TASK|TBD|NOTE) \d/.test(l)).map(l=>l.trim().replace(/^▶ /,'')),['▾ FIX 3','▸ TASK 3','▸ TBD 1','▸ NOTE 1']);
// inside a group the newest first; Discuss and Waiting stand before the title, Ready does not
assert.deepEqual(items(st),['FIX   2026-11-25  Questions  Discuss  Sample question popup forgets the answer','FIX   2026-11-24  Routing             Sample route resume loses its stage',
  'FIX   2026-11-20  No area             Sample same title']);
assert(/Sample question popup/.test(chosen(st)));
""")

    def test_a_priority_line_stands_in_the_detail_as_written_and_the_board_does_not_sort_by_it(self):
        # 11-26 (fix 1 of #1863, the supervisor's decision): the board is drawn as before the panel read `priority:`: its list the same rows in the same
        # order, and an opened item's detail the line as a line of text, `priority: 1` (a wrong value, `priority: 0`, the same way).
        ranked = SAMPLE.replace("  kind: improve\n", "  kind: improve\n  priority: 1\n").replace("  kind: FIX\n", "  kind: FIX\n  priority: 0\n")
        for text in (SAMPLE, ranked):
            (self.root / "backlog.md").write_text(text, encoding="utf-8")
            if text is SAMPLE:
                plain = self.popup("list")["board"]
        board = self.popup("list")["board"]
        self.assertEqual(board, plain)
        rows = {row[5]: row[0] for row in board["items"]}
        for title, line in (("Sample panel head hides the count", "priority: 1"), ("Sample question popup forgets the answer", "priority: 0")):
            detail = self.popup("detail", open=rows[title], indent=4)
            self.assertIn("    " + line, ["".join(part[0] for part in row) for row in detail["rows"]], detail)

    def test_k_a_d_group_by_kind_area_and_date(self):
        self.node(r"""
const st=fresh();
board.setAxis(b,st.bs,st.t,'area');
let s=screen(st);
assert(s.includes('group Kind [Area] Date · showing 8 · Discuss 2'));
assert.deepEqual(s.filter(l=>/^(▶ |  )[▾▸] /.test(l)).map(l=>l.slice(2)),['▾ panel 2','▸ Questions 1','▸ Routing 2','▸ Workers 1','▸ No area 2']);
assert.deepEqual(items(st),['TASK  2026-11-26  panel               Sample panel head hides the count','TASK  —           panel      Waiting  Sample undated job']);
board.setAxis(b,st.bs,st.t,'date');s=screen(st);
assert.deepEqual(s.filter(l=>/^(▶ |  )[▾▸] /.test(l)).map(l=>l.slice(2)),['▾ 2026-11-26 3','▸ 2026-11-25 1','▸ 2026-11-24 1','▸ 2026-11-20 1','▸ No date 2']);
// the same date in the document's order
assert.deepEqual(items(st).map(l=>l.replace(/ +/g,' ')),['TASK 2026-11-26 panel Sample panel head hides the count','NOTE 2026-11-26 Routing Sample routing note to keep',
  'TASK 2026-11-26 Workers Discuss Sample worker leg runs twice']);
assert(/Sample panel head/.test(chosen(st)));
board.setAxis(b,st.bs,st.t,'kind');assert(screen(st).includes('group [Kind] Area Date · showing 8 · Discuss 2'));
assert.equal(board.setAxis(b,st.bs,st.t,'kind'),false);                     // the same key again changes nothing
""")

    def test_t_filters_by_state_and_every_group_stands_open(self):
        self.node(r"""
const st=fresh(),seen=[];
for(let i=0;i<5;i++){board.cycleFilter(b,st.bs,st.t);const s=screen(st);seen.push([s[3],items(st).length,s.filter(l=>l.includes('▸')&&!l.includes('full document')).length]);}
assert.deepEqual(seen,[
  ['group [Kind] Area Date · showing 2 of 8 · Discuss 2 · state Discuss',2,0],
  ['group [Kind] Area Date · showing 1 of 8 · Discuss 2 · state Ready',1,0],
  ['group [Kind] Area Date · showing 1 of 8 · Discuss 2 · state Waiting',1,0],
  ['group [Kind] Area Date · showing 4 of 8 · Discuss 2 · state No state',4,0],
  ['group [Kind] Area Date · showing 8 · Discuss 2',3,3]]);
""")

    def test_find_a_title_or_an_area_and_esc_undoes_one_thing_at_a_time(self):
        self.node(r"""
const st=fresh(),type=s=>{for(const ch of Buffer.from(s,'utf8').toString('latin1'))board.type(b,st.bs,st.t,'',ch);};
board.startFind(st.bs);type('SAME');
assert.equal(board.typing(st.bs),true);
assert.deepEqual(board.inputRow(b,st.bs,96).map(p=>p.text).join(''),'/ Title: SAME▏   Tab field · Enter find · Esc cancel');
board.type(b,st.bs,st.t,'enter');
assert.equal(board.typing(st.bs),false);
assert.equal(screen(st)[3],'group [Kind] Area Date · showing 1 of 8 · Discuss 2 · find Title "SAME"');
assert(/Sample same title/.test(chosen(st)));
// `Tab` first: an area's name, case and blanks folded
assert.equal(board.back(b,st.bs,st.t),true);
board.startFind(st.bs);board.type(b,st.bs,st.t,'tab');type('rout');board.type(b,st.bs,st.t,'backspace');type('t');board.type(b,st.bs,st.t,'enter');
assert.equal(screen(st)[3],'group [Kind] Area Date · showing 2 of 8 · Discuss 2 · find Area "rout"');
assert.deepEqual(items(st).map(l=>l.split('  ').pop().trim()),['Sample route resume loses its stage','Sample routing note to keep']);
// nothing found: one line says so
board.back(b,st.bs,st.t);board.startFind(st.bs);type('없는 제목');board.type(b,st.bs,st.t,'enter');
assert(screen(st).some(l=>l.includes('nothing matches · Esc clears')));assert.equal(screen(st)[3],'group [Kind] Area Date · showing 0 of 8 · Discuss 2 · find Title "없는 제목"');
// Esc: what is typed, then the opened items, then what was found, then the state; then nothing (the popup closes)
board.back(b,st.bs,st.t);board.cycleFilter(b,st.bs,st.t);board.startFind(st.bs);type('x');
const t=st.t;t.items.add(t.sel);
const undo=[];for(let i=0;i<5;i++){undo.push(board.back(b,st.bs,t));}
assert.deepEqual(undo,[true,true,true,false,false]);
assert.equal(board.typing(st.bs),false);assert.equal(t.items.size,0);assert.equal(screen(st)[3],'group [Kind] Area Date · showing 8 · Discuss 2');
""")

    def test_home_end_and_the_detail_of_one_item(self):
        self.node(r"""
const st=fresh();
assert.equal(board.ends(b,st.bs,st.t,true),true);assert(/Sample routing note to keep/.test(chosen(st)));        // the last item: its group (NOTE) opens with it
assert(screen(st).some(l=>l.includes('▾ NOTE 1')));
board.ends(b,st.bs,st.t,false);assert(/Sample question popup/.test(chosen(st)));
console.log(JSON.stringify({sel:st.t.sel}));
""")
        sel = None
        for row in self.popup("list")["board"]["items"]:
            if row[5] == "Sample question popup forgets the answer":
                sel = row[0]
        got = self.popup("detail", open=sel, indent=10)
        self.assertEqual(["".join(run[0] for run in row) for row in got["rows"]],
                         [" " * 10 + "2026-11-25 · FIX · Questions · Discuss", " " * 10 + "campaign 2026-11-26_sample-a"])
        first = self.popup("list")["board"]["items"]
        route = next(row[0] for row in first if row[5] == "Sample route resume loses its stage")
        rows = ["".join(run[0] for run in row).strip() for row in self.popup("detail", open=route)["rows"]]
        self.assertEqual(rows, ["2026-11-24 · FIX · Routing · Ready", "campaign 2026-11-26_sample-a", "source sample review", "note Sample note that says what was seen."])
        wrong = next(row[0] for row in first if row[5] == "Sample wrong values everywhere")
        rows = ["".join(run[0] for run in row).strip() for row in self.popup("detail", open=wrong)["rows"]]
        self.assertEqual(rows, ["— · TBD · No area · —", "campaign Unassigned"])                   # the wrong lines themselves are cells, not detail

    def test_the_read_limits_say_how_many_and_that_there_are_more(self):
        # 1,000 items with long notes: the file passes the 1 MiB read; what was read is counted and the board says there is more
        long_notes = "# T\n갱신 2026-11-26 09:30\n## A\n" + "".join(
            "- Sample R%04d item\n  date: 2026-11-%02d\n  kind: fix\n  note: %s\n" % (n, n % 28 + 1, "n" * 1100) for n in range(1, 1001))
        self.assertGreater(len(long_notes.encode()), 1 << 20)
        out = json.loads(self.node(r"""
const st=fresh(),s=screen(st);console.log(JSON.stringify({counts:s[2],limit:s[3],cut:b.cut,total:b.total,sent:b.items.length}));""", long_notes))
        self.assertTrue(out["cut"])
        self.assertTrue(0 < out["total"] < 1000, out)
        self.assertEqual(out["counts"], "%d+ total · FIX %d · TASK 0 · TBD 0 · NOTE 0" % (out["total"], out["total"]))
        self.assertEqual(out["limit"], "read %d · more in backlog.md (the file is longer than is read)" % out["total"])
        # 1,000 items with long titles: the answer passes 200 KiB; the newest that fit stand and the board says how many more
        long_titles = "# T\n갱신 2026-11-26 09:30\n## A\n" + "".join(
            "- Sample R%04d %s\n  date: 2026-%02d-%02d\n  kind: task\n" % (n, "title " * 50, n % 12 + 1, n % 28 + 1) for n in range(1, 1001))
        (self.root / "backlog.md").write_text(long_titles, encoding="utf-8")
        answer = self.popup("list")
        self.assertLessEqual(len(json.dumps({"id": 0, **answer}, ensure_ascii=False).encode()), self.shell.BACKLOG_ANSWER_BYTES)
        out = json.loads(self.node(r"""
const st=fresh(),s=screen(st);console.log(JSON.stringify({counts:s[2],limit:s[3],sent:b.items.length,total:b.total,dates:b.items.map(r=>r.date)}));""", long_titles))
        self.assertEqual((out["total"], out["counts"]), (1000, "1,000 total · FIX 0 · TASK 1,000 · TBD 0 · NOTE 0"))
        self.assertTrue(0 < out["sent"] < 1000, out["sent"])
        self.assertEqual(out["limit"], "showing the newest %s of 1,000 · %d more in backlog.md" % (format(out["sent"], ","), 1000 - out["sent"]))
        self.assertEqual(out["dates"], sorted(out["dates"], reverse=True))
        # and 1,000 short items stand whole
        short = "# T\n## A\n" + "".join("- Sample R%04d\n  kind: note\n" % n for n in range(1, 1001))
        out = json.loads(self.node(r"""console.log(JSON.stringify({sent:b.items.length,cut:b.cut,lines:screen(fresh()).filter(l=>/more in backlog/.test(l)).length}));""", short))
        self.assertEqual(out, {"sent": 1000, "cut": False, "lines": 0})


@unittest.skipUnless(NODE, "needs node")
class PictureTest(unittest.TestCase):
    """The board as the product draws it (`utilities/status_popup_picture.py`: the backend's answer, the host's board and list code, the popup's frame)."""

    def picture(self, text, width, height, steps, find="", area=""):
        with tempfile.TemporaryDirectory(prefix="bbp") as folder:
            path = Path(folder) / "backlog.md"
            path.write_text(text, encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "utilities/status_popup_picture.py"), str(path), "--kind", "backlog",
                                     "--width", str(width), "--height", str(height), "--steps", steps, "--find", find, "--find-area", area],
                                    capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        screens = {}
        for block in result.stdout.split("--- ")[1:]:
            name, _, body = block.partition(" ---\n")
            screens[name] = [line for line in body.splitlines() if line.startswith(("╭", "│", "╰"))]
        return screens

    @staticmethod
    def cells(line):
        return sum(2 if ord(c) > 0x2e7f else 1 for c in line)

    def test_the_screens_at_100_by_26_and_60_by_20(self):
        for width, height in ((100, 26), (60, 20)):
            shown = self.picture(SAMPLE, width, height, "first,area,date,discuss,typing,find,detail,findarea,end", find="same", area="panel")
            self.assertEqual(len(shown), 9)
            for name, rows in shown.items():
                self.assertTrue(rows, name)
                self.assertTrue(all(self.cells(r) == width for r in rows), (width, name, [self.cells(r) for r in rows]))
            text = {name: "\n".join(rows) for name, rows in shown.items()}
            self.assertIn("8 total · FIX 3 · TASK 3 · TBD 1 · NOTE 1", text["first screen (grouped by kind)"])
            self.assertIn("▾ panel 2", text["a: grouped by area"])
            self.assertIn("▾ 2026-11-26 3", text["d: grouped by date"])
            self.assertIn("state Discuss", text["t: only Discuss"])
            self.assertIn("/ Title: same", text["/ same: being typed"])
            self.assertIn('find Title "same"', text["/ same Enter: a title found"])
            self.assertIn("2026-11-20 · FIX · No area · —", text["/ same Enter Enter: the item opened"])
            self.assertIn('find Area "panel"', text["/ Tab panel Enter: an area found"])
            self.assertNotIn("IMPR", "\n".join(text.values()))
        # narrow: the area column gives way first, then the date
        narrow = "\n".join(self.picture(SAMPLE, 60, 20, "first")["first screen (grouped by kind)"])
        self.assertIn("FIX   2026-11-25  Discuss  Sample question", narrow)
        self.assertNotIn("Questions  Discuss", narrow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
