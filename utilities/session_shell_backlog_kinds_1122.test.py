"""11-22 (the person: "백로그도 가능하면 좀 세분화해서 그 보기를 유지하고 앞에서는 알파벳 하나가 아니라 그래도 적절히 3~4자로 약어든 단어든 붙이는게 맞지"):
the sort of a backlog line in the document's promise, its two readers and the panel's BACKLOG part. 11-26 (scheme A; the person: "A — FIX · TASK · NOTE에
상태(Discuss 등)와 영역을 더하고, 판은 영역별로 묶어 봄"): the kinds are `FIX`, `TASK`, `NOTE`, `TBD`, the earlier `improve` read as `TASK`.

1. A job's sort is its optional indented `kind:` line (`fix`, `task`, `note`, and the earlier `improve` as `task`; case and the blanks around the value are not read).
   None, another value or two different values is unsorted (`TBD`); nothing is refused. A `kind:` line counts under a job only, and never inside a code fence.
2. `session_status.parse_backlog` and `artifact_entrance._backlog` read it by one rule (`utilities/backlog_items.py`): on the same documents (a table of cases, and many
   random ones) they give the same count of each sort and of each state.
3. The panel's BACKLOG head tells the sorts and its rows stand behind a four-cell word; every row of every picture has the panel's width.
   11-26 (panel B): the head is `BACKLOG +N · Discuss n`, its rows only the ranked ones, state first (`· Ready   TASK 11-26 title`).
4. (The board, the BACKLOG popup, is tested in `utilities/session_shell_backlog_board_1126.test.py`; the entrance's backlog line,
   `N total · FIX n · TASK n · NOTE n · Discuss n · updated …`, with the entrance's own suite, `utilities/artifact_entrance.test.py`.)
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import random
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
NODE = shutil.which("node")
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

MIXED = """# 시험용 백로그
갱신 2026-11-22 09:10

## 2026-11-22_this-window
- 시험용 목록을 다시 열 때 빠진 항목 복구
  kind: fix
- 시험용 사용 안내 갱신
  kind: Improve
  source: 시험용 출처
- 시험용 참고 하나
  kind: note
- 시험용 아직 못 가른 일

## 2026-11-10_other
- 시험용 문서 날짜가 사라지는 오류 재현
  kind:   FIX
  date: 2026-11-20
- 시험용 다른 사용 안내
  kind: improve
- 시험용 두 값
  kind: fix
  kind: improve
- 시험용 모르는 값
  kind: bug

## Unassigned
- 시험용 이 창 밖의 고칠 일
  kind: fix
"""

# 11-26 (panel B): the same document with what the panel's chosen rows are made of: `priority:` (the person's order), `state:`, `area:`, `date:`.
RANKED = (MIXED
          .replace("- 시험용 목록을 다시 열 때 빠진 항목 복구\n  kind: fix\n", "- 시험용 목록을 다시 열 때 빠진 항목 복구\n  kind: fix\n  state: discuss\n  area: Routing\n  date: 2026-11-20\n  priority: 2\n")
          .replace("- 시험용 사용 안내 갱신\n  kind: Improve\n", "- 시험용 사용 안내 갱신\n  kind: Improve\n  state: ready\n  area: Docs\n  date: 2025-03-04\n  priority: 1\n")
          .replace("- 시험용 참고 하나\n  kind: note\n", "- 시험용 참고 하나\n  kind: note\n  priority: 3\n"))


KINDS = ("fix", "task", "note", "tbd")


def kinds_of(counts):
    """The kinds of `session_status.backlog_counts` (which counts the states too)."""
    return {name: counts[name] for name in KINDS}


def sort_counts(text):
    """(parse_backlog's, the entrance's) counts of each sort of `text`."""
    from fleet import session_status
    import artifact_entrance
    parsed = session_status.parse_backlog(text.splitlines())
    with tempfile.TemporaryDirectory(prefix="bk") as folder:
        (Path(folder) / "backlog.md").write_text(text, encoding="utf-8")
        seen = artifact_entrance._backlog(Path(folder))
    return kinds_of(session_status.backlog_counts(parsed)), seen


class KindRuleTest(unittest.TestCase):
    CASES = [
        ("three_values", "## A\n- a\n  kind: fix\n- b\n  kind: improve\n- c\n  kind: note\n", {"fix": 1, "task": 1, "note": 1, "tbd": 0}),
        ("case_and_blanks", "## A\n- a\n  KIND: FIX\n- b\n\t kind:\timprove  \n- c\n  Kind:   Note\t\n", {"fix": 1, "task": 1, "note": 1, "tbd": 0}),
        ("none", "## A\n- a\n- b\n  date: x\n", {"fix": 0, "task": 0, "note": 0, "tbd": 2}),
        ("unknown_value", "## A\n- a\n  kind: bug\n- b\n  kind:\n- c\n  kind: fix it\n", {"fix": 0, "task": 0, "note": 0, "tbd": 3}),
        ("two_values", "## A\n- a\n  kind: fix\n  kind: note\n- b\n  kind: fix\n  kind: bug\n", {"fix": 0, "task": 0, "note": 0, "tbd": 2}),
        ("same_value_twice", "## A\n- a\n  kind: fix\n  note: x\n  kind: Fix\n", {"fix": 1, "task": 0, "note": 0, "tbd": 0}),
        ("in_fence", "## A\n```\n- a\n  kind: fix\n```\n- b\n```\n  kind: fix\n```\n", {"fix": 0, "task": 0, "note": 0, "tbd": 1}),
        ("fence_ends_the_job", "## A\n- a\n```\n```\n  kind: fix\n- b\n", {"fix": 0, "task": 0, "note": 0, "tbd": 2}),
        ("under_a_heading", "## A\n  kind: fix\n- a\n## B\n  kind: note\n", {"fix": 0, "task": 0, "note": 0, "tbd": 1}),
        ("under_other_text", "## A\n- a\ntext\n  kind: fix\n* star\n  kind: note\n- b\n  kind: improve\n", {"fix": 0, "task": 1, "note": 0, "tbd": 1}),
        ("before_the_first_heading", "- a\n  kind: fix\n## A\n- b\n", {"fix": 0, "task": 0, "note": 0, "tbd": 1}),
        ("not_indented", "## A\n- a\nkind: fix\n", {"fix": 0, "task": 0, "note": 0, "tbd": 1}),
        ("sub_bullet_is_no_kind_line", "## A\n- a\n  - kind: fix\n", {"fix": 0, "task": 0, "note": 0, "tbd": 1}),
        ("blank_lines_between", "## A\n- a\n\n  kind: note\n\n- b\n", {"fix": 0, "task": 0, "note": 1, "tbd": 1}),
        ("crlf", "## A\r\n- a\r\n  kind: fix\r\n- b\r\n  kind: improve\r\n", {"fix": 1, "task": 1, "note": 0, "tbd": 0}),
        ("key_with_a_blank_before_the_colon", "## A\n- a\n  kind : fix\n", {"fix": 0, "task": 0, "note": 0, "tbd": 1}),
        ("nothing", "", {"fix": 0, "task": 0, "note": 0, "tbd": 0}),
        # 11-26: `task` is a kind, and the earlier `improve` is read as it (the two together are one kind, not two values)
        ("task_and_improve", "## A\n- a\n  kind: task\n- b\n  kind: TASK\n  kind: improve\n- c\n  kind: improve\n  kind: fix\n", {"fix": 0, "task": 2, "note": 0, "tbd": 1}),
    ]

    def test_the_sort_is_read_the_same_by_both_readers_on_each_case(self):
        for name, text, want in self.CASES:
            mine, entrance = sort_counts(text)
            self.assertEqual(mine, want, name)
            self.assertEqual(entrance["kinds"], want, name)
            self.assertEqual(entrance["count"], sum(want.values()), name)

    FRAGMENTS = ["## A", "## B", "  ## C", "### D", "# T", "- a", "- b", "- c", "* star", "-x", "text", "", "  kind: fix", "\tKind:IMPROVE ", "  kind:", "    kind: note",
                 "  kind: bug", "  - kind: fix", "kind: fix", "  date: x", "  source: y", "```", "~~~", "````", "갱신 2026-11-22 09:10", "갱신 2026-13-45 99:99", "- - -",
                 "  kind: Note", "- ", "　", " kind: improve", "  KIND:fix", "<!--", "-->",
                 # 11-26: the state line counts the same way in both readers
                 "  kind: task", "  state: discuss", "  State: Waiting", "  state: ready", "  state: later", "  area: Routing", "  date: 2026-11-26"]

    def test_the_two_readers_agree_on_many_random_documents(self):
        rng = random.Random(1122)
        from fleet import session_status
        import artifact_entrance
        with tempfile.TemporaryDirectory(prefix="bk") as folder:
            path = Path(folder) / "backlog.md"
            for n in range(600):
                text = rng.choice(["\n", "\r\n"]).join(rng.choice(self.FRAGMENTS) for _ in range(rng.randint(0, 40)))
                path.write_text(text, encoding="utf-8")
                seen = artifact_entrance._backlog(Path(folder))
                parsed = session_status.parse_backlog(text.splitlines())
                counts = session_status.backlog_counts(parsed)
                self.assertEqual(seen["kinds"], kinds_of(counts), repr(text))
                self.assertEqual(seen["states"], {name: counts[name] for name in ("discuss", "ready", "waiting")}, repr(text))
                self.assertEqual(seen["count"], session_status.backlog_total(parsed), repr(text))
                # the detail is read too: the same sorts, and the same count
                full = session_status.parse_backlog(text.splitlines(), detail=True)
                self.assertEqual(session_status.backlog_counts(full), counts, repr(text))

    def test_both_readers_read_a_backlog_to_the_same_limit(self):
        import artifact_entrance
        from fleet import session_status
        self.assertEqual(artifact_entrance.MAX_BACKLOG_BYTES, session_status.BACKLOG_READ_LIMIT)

    def test_a_document_at_the_read_limit_counts_its_sorts_and_not_one_byte_over(self):
        import artifact_entrance
        from fleet import session_status

        def sized(n):   # ASCII pieces, so characters are bytes; the length is kept, not re-encoded (1 MiB)
            pieces, used, k = ["## A\n"], 5, 0
            while True:
                piece = "- %05d\n  kind: %s\n" % (k + 1, ("fix", "task", "note", "")[k % 4])
                if used + len(piece) > n:
                    break
                pieces.append(piece)
                used += len(piece)
                k += 1
            return "".join(pieces) + "x" * (n - used)
        limit = artifact_entrance.MAX_BACKLOG_BYTES
        for size, readable in ((limit, True), (limit + 1, False)):
            text = sized(size)
            self.assertEqual(len(text.encode()), size)
            with tempfile.TemporaryDirectory(prefix="bk") as folder:
                (Path(folder) / "backlog.md").write_text(text, encoding="utf-8")
                seen = artifact_entrance._backlog(Path(folder))
            counts = kinds_of(session_status.backlog_counts(session_status.parse_backlog(text.splitlines())))
            if readable:
                self.assertEqual(seen["kinds"], counts)
                self.assertGreater(counts["fix"], 500)
                self.assertEqual(seen["count"], sum(counts.values()))
            else:
                self.assertEqual((seen["count"], seen["kinds"]), (None, None))
                self.assertGreater(sum(counts.values()), 500)           # the panel lists what it read and says there is more

    def test_the_rule_is_written_in_the_meta_contract(self):
        meta = (ROOT / "core" / "ARTIFACT_META.md").read_text(encoding="utf-8")
        section = meta[meta.index("### `backlog.md`"):]
        for words in ("`kind:`", "`fix`", "`task`", "`note`", "`improve`", "unsorted", "`TBD`", "nothing is refused, warned or gated", "not inside a code fence",
                      "The title line carries no tag, status or owner mark", "its `kind:` line then", "register it as a question",
                      "`state:`", "`discuss`", "`ready`", "`waiting`", "`area:`", "`No area`", "`date:`"):
            self.assertIn(words, section)


@unittest.skipUnless(NODE, "needs node")
class PanelPictureTest(unittest.TestCase):
    HEAD = (ROOT / "utilities/session_shell_panel_tidy_1122.test.py").read_text(encoding="utf-8").split('HEAD = r"""')[1].split('"""')[0]

    def docs(self):
        """The backlog values the host holds, from the documents the panel reads (the backend's own code)."""
        from fleet import session_status
        out = {}
        legacy = re.sub(r"^ +kind:.*\n|^\tKind:.*\n", "", MIXED, flags=re.M)
        many = "## A\n" + "".join("- 시험용 항목 %03d\n  kind: %s\n" % (n, "fix" if n % 3 else "task") for n in range(1, 1300))
        for name, text, own in (("mixed", MIXED, "2026-11-22_this-window"), ("legacy", legacy, ""), ("notes", "## A\n- 참고 하나\n  kind: note\n- 참고 둘\n  kind: note\n", ""),
                                ("ranked", RANKED, ""), ("empty", "# T\n", ""), ("big", many, ""), ("tbd_notes", "## A\n- 참고\n  kind: note\n- 못 가른 일\n", "")):
            parsed = session_status.parse_backlog(text.splitlines(), detail=False)
            items, total = session_status.panel_backlog_items(parsed, own)
            out[name] = {"word": "BACKLOG", "has_doc": True, "items": items, "total": total, "counts": session_status.backlog_counts(parsed)}
        return out

    def node(self, body, docs):
        result = subprocess.run([NODE, "-e", self.HEAD + body, str(SHELL), json.dumps(docs, ensure_ascii=False)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_pictures_by_width_and_height_and_the_cells_of_every_row(self):
        # 11-26 (panel B): the head is `BACKLOG +N · Discuss n`, the rows only the ones the person ranked (`priority:`), state first:
        # `  · Ready   TASK 11-26 Fit long titles`; the kind words and colours are the table's.
        self.node(r"""
const docs=JSON.parse(process.argv[2]);
const host=(d,year=2026)=>({...d,year,items:d.items.map(i=>({mark:'',text:i.text,key:i.key,kind:i.kind,state:i.state,area:i.area,date:i.date}))});
const at=(o)=>{const v=draw({...o,status:status(),routes:1,messages:2,mem:1});return {v,ls:lines(v),a:inside(v)};};
const head=a=>a.find(l=>/^ BACKLOG/.test(l)),rows=a=>a.filter(l=>/^  · /.test(l));
// every row of every picture has the panel's width (cells counted, Hangul two)
let pictures=0;
for(const name of Object.keys(docs))for(const width of [59,44,30])for(const height of [40,24,14]){
  const {ls}=at({width,height,backlog:host(docs[name])});pictures++;
  assert(ls.every(l=>cells(l)===width),name+' '+width+'x'+height+'\n'+ls.map(l=>cells(l)+' '+l).join('\n'));}
assert.equal(pictures,63);
// the ranked document at 44 wide, tall: the head tells what is not shown and the Discuss count; the three chosen rows stand in the person's order
// (priority 1, 2, 3), state first, then the kind word, the date (`YY-MM-DD` another year, `MM-DD` this year, `—` none; the column as wide as the
// widest date shown, so the titles stand in one column), the title
{const {a,ls}=at({width:44,height:40,backlog:host(docs.ranked)});
 assert(/^ BACKLOG \+6 · Discuss 1 +›$/.test(head(a)),ls.join('\n'));
 const r=rows(a);assert.equal(r.length,3,a.join('\n'));
 assert(r[0].startsWith('  · Ready   TASK 25-03-04 시험용')&&r[1].startsWith('  · Discuss FIX  11-20    시험용')&&r[2].startsWith('  · —       NOTE —        시험용 참고'),r.join('\n'));}
// the year the backend names decides which date has no year: in 2025 the old one is `03-04` and this year's is `26-11-20`
{const {a}=at({width:44,height:40,backlog:host(docs.ranked,2025)});const r=rows(a);
 assert(r[0].startsWith('  · Ready   TASK 03-04    ')&&r[1].startsWith('  · Discuss FIX  26-11-20 '),r.join('\n'));}
// from 55 cells inside (59 wide) the area stands in eight cells (nine with its gap) before the title; `—` where there is none
{const {a}=at({width:59,height:40,backlog:host(docs.ranked)});const r=rows(a);
 assert(r[0].startsWith('  · Ready   TASK 25-03-04 Docs     시험용 사용')&&r[1].startsWith('  · Discuss FIX  11-20    Routing  시험용 목록')&&r[2].startsWith('  · —       NOTE —        —        시험용 참고 하나'),r.join('\n'));
 assert(!rows(at({width:44,height:40,backlog:host(docs.ranked)}).a).some(l=>l.includes('Routing')||l.includes('Docs')));}
// the state, kind and colour words (the table's: Discuss and Waiting yellow bold, Ready dim; FIX yellow, TASK green, NOTE and TBD dim)
{const {v}=at({width:59,height:40,backlog:host({...docs.ranked,items:[{kind:'fix',state:'discuss',text:'KIND_A',key:'i:a'},{kind:'task',state:'ready',text:'KIND_B',key:'i:b'},{kind:'note',state:'waiting',text:'KIND_C',key:'i:c'}]})});
 const row=txt=>v.rows.find(r=>r.map(p=>p.text).join('').includes(txt));
 const word=(txt,w)=>row(txt).find(p=>p.text.trim()===w);
 assert.equal(word('KIND_A','FIX').foreground,186);assert.equal(word('KIND_B','TASK').foreground,150);assert.equal(word('KIND_C','NOTE').dim,true);
 assert(word('KIND_A','Discuss').foreground===186&&word('KIND_A','Discuss').bold===true);
 assert(word('KIND_C','Waiting').foreground===186&&word('KIND_C','Waiting').bold===true);
 assert.equal(word('KIND_B','Ready').dim,true);}
// narrower, the head gives way in order: ` · ` goes (then `Discuss n`, then `+N`)
{const {a}=at({width:30,height:40,backlog:host(docs.ranked)});assert(/^ BACKLOG \+6 · Discuss 1 +›$/.test(head(a)),head(a));}
{const {a}=at({width:28,height:40,backlog:host(docs.ranked)});assert(/^ BACKLOG \+6 Discuss 1 +›$/.test(head(a)),head(a));}
{const {a}=at({width:26,height:40,backlog:host(docs.ranked)});assert(/^ BACKLOG \+6 +›$/.test(head(a)),head(a));}
{const {a}=at({width:14,height:40,backlog:host(docs.ranked)});assert(/^ BACKLOG +›$/.test(head(a)),head(a));}
// one cell wider never tells less: the stages (nothing, `+N`, `Discuss n`, ` · `) only ever come in that order as the width grows
{const rank=h=>{const t=h.replace(/^ BACKLOG/,'').replace(/ *›$/,'').trim();
   if(!t)return 0;if(t.includes('·'))return 3;if(t.includes('Discuss'))return 2;return 1;};
 let tried=0;
 for(const name of Object.keys(docs)){let last=-1,seen=new Set();
  for(let width=14;width<=80;width++){const {a}=at({width,height:40,backlog:host(docs[name])});const h=head(a);tried++;
   const r=rank(h);assert(r>=last,name+' '+width+': '+h+' tells less than '+last);last=r;seen.add(r);}
  if(name==='big'||name==='ranked')for(const r of [0,1,2,3])assert(seen.has(r),name+' never stands at stage '+r);}
 assert.equal(tried,7*67);}
// a document nobody ranked has the head alone (no row): the whole count is `+N`, Discuss 0 is told
{const {a}=at({width:44,height:40,backlog:host(docs.legacy)});
 assert(/^ BACKLOG \+9 · Discuss 0 +›$/.test(head(a)),a.join('\n'));assert.equal(rows(a).length,0,a.join('\n'));}
for(const name of ['notes','tbd_notes']){const {a}=at({width:44,height:40,backlog:host(docs[name])});assert(/^ BACKLOG \+2 · Discuss 0 +›$/.test(head(a)),a.join('\n'));assert.equal(rows(a).length,0);}
// an empty document and no document
{const {a}=at({width:44,height:40,backlog:host(docs.empty)});assert(/^ BACKLOG Discuss 0 +›$/.test(head(a)),a.join('\n'));assert.equal(rows(a).length,0);}   // (no `+N`: no ` · ` before Discuss)
{const {a,v}=at({width:44,height:40,backlog:{word:'BACKLOG',has_doc:false,items:[],total:0}});assert(/^ BACKLOG +›$/.test(head(a)),a.join('\n'));
 const at2=a.findIndex(l=>/^ BACKLOG/.test(l));assert(/^│ ─+ │$/.test(v.rows[at2+1].map(p=>p.text).join(''))&&!v.rows.some(r=>r.some(p=>p.text.includes('SESSION'))));}
// three-digit counts (and more) stand whole where there is room and give way where there is not
{const {a}=at({width:59,height:40,backlog:host(docs.big)});assert(/^ BACKLOG \+1299 · Discuss 0 +›$/.test(head(a)),head(a));}
{const {a}=at({width:32,height:40,backlog:host(docs.big)});assert(/^ BACKLOG \+1299 · Discuss 0 +›$/.test(head(a)),head(a));}
{const {a}=at({width:30,height:40,backlog:host(docs.big)});assert(/^ BACKLOG \+1299 Discuss 0 +›$/.test(head(a)),head(a));}
{const {a}=at({width:26,height:40,backlog:host(docs.big)});assert(/^ BACKLOG \+1299 +›$/.test(head(a)),head(a));}
{const {a}=at({width:19,height:40,backlog:host(docs.big)});assert(/^ BACKLOG +›$/.test(head(a)),head(a));}
// a backend that sends no counts and no state/area/date: `+N` alone in the head, the rows with `—` where a value is missing
{const {a}=at({width:44,height:40,backlog:backlog()});assert(a.some(l=>/^ BACKLOG \+11 +›$/.test(l)),a.join('\n'));assert(a.some(l=>/^  · —       TBD  — +시험용 나중 일/.test(l)),a.join('\n'));}
""", self.docs())

    def test_short_of_rows_the_buttons_carry_fix_and_task_together(self):
        self.node(r"""
const docs=JSON.parse(process.argv[2]);
const host=d=>({...d,items:d.items.map(i=>({mark:'',text:i.text,key:i.key,kind:i.kind}))});
const at=(o)=>{const v=draw({...o,status:status(),routes:1,messages:2,mem:1});return {v,ls:lines(v),a:inside(v)};};
// bottom layout: the first row ends in the two buttons, BACKLOG's number the FIX and TASK together
{const v=draw({side:false,width:120,height:14,status:status(),backlog:host(docs.mixed),routes:1}),a=lines(v);
 assert(/STATUS \+\d+ ›  BACKLOG \+5 ›│$/.test(a[1])||/BACKLOG \+5 ›/.test(a[1]),a[1]);assert(a.every(l=>cells(l)===120));}
{const v=draw({side:false,width:120,height:14,status:status(),backlog:host(docs.legacy),routes:1}),a=lines(v);assert(/BACKLOG \? ›/.test(a[1]),a[1]);}
{const v=draw({side:false,width:120,height:14,status:status(),backlog:host(docs.notes),routes:1}),a=lines(v);assert(/BACKLOG ›/.test(a[1]),a[1]);}
// beside the native area, short of rows: the REPO row ends in the buttons and no list stands (11-26 panel B: the repository row's own text is
// longer, so at 44 wide the buttons are folded to `›  ›`; wider, the same words and numbers as before stand after it)
{const {a}=at({width:44,height:14,backlog:host(docs.ranked)});assert(/^ REPO .* ›  ›$/.test(a[1])&&!a.some(l=>/^  · /.test(l)),a.join('\n'));}
// (11-26, fix 1 of #1863: beside the native area the numbers mean what the heads' do: `STATUS 5` the board's whole count, `BACKLOG +N` every item
// of the document, none drawn, whatever its kinds; the bottom layout above keeps FIX and TASK together)
for(const name of ['mixed','legacy','notes']){
 const {a}=at({width:70,height:14,backlog:host(docs[name])}),want=new RegExp('STATUS 5 ›  BACKLOG \\+'+docs[name].total+' ›$');
 assert(/^ REPO /.test(a[1])&&want.test(a[1])&&!a.some(l=>/^  · /.test(l)),a.join('\n'));}
{const {a}=at({width:70,height:14,backlog:host(docs.empty)});assert(/STATUS 5 ›  BACKLOG ›$/.test(a[1]),a[1]);}
""", self.docs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
