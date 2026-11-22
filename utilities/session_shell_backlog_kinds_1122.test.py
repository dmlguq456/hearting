"""11-22 (the person: "백로그도 가능하면 좀 세분화해서 그 보기를 유지하고 앞에서는 알파벳 하나가 아니라 그래도 적절히 3~4자로 약어든 단어든 붙이는게 맞지"):
the sort of a backlog line (`FIX`, `IMPR`, `NOTE`, `TBD`) in the document's promise, its two readers, the panel's BACKLOG part, the BACKLOG popup and the entrance's line.

1. A job's sort is its optional indented `kind:` line (`fix`, `improve`, `note`; case and the blanks around the value are not read). None, another value or two different
   values is unsorted (`TBD`); nothing is refused. A `kind:` line counts under a job only, and never inside a code fence.
2. `session_status.parse_backlog` and `artifact_entrance._backlog` read it by one rule: on the same documents (a table of cases, and many random ones) they give the same count of each sort.
3. The panel's BACKLOG head tells the sorts and its rows stand behind a four-cell word; every row of every picture has the panel's width.
4. The popup is by sort (FIX first and open, the others folded lines with their counts), this window's campaign first inside a sort; an item's key names the same item wherever it stands.
5. (The entrance's backlog line, `FIX n · IMPR n · NOTE n · TBD n · N total · updated …`, is tested with the entrance's own suite, `utilities/artifact_entrance.test.py`.)
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
from unittest import mock

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


def sort_counts(text):
    """(parse_backlog's, the entrance's) counts of each sort of `text`."""
    from fleet import session_status
    import artifact_entrance
    parsed = session_status.parse_backlog(text.splitlines())
    with tempfile.TemporaryDirectory(prefix="bk") as folder:
        (Path(folder) / "backlog.md").write_text(text, encoding="utf-8")
        seen = artifact_entrance._backlog(Path(folder))
    return session_status.backlog_counts(parsed), seen


class KindRuleTest(unittest.TestCase):
    CASES = [
        ("three_values", "## A\n- a\n  kind: fix\n- b\n  kind: improve\n- c\n  kind: note\n", {"fix": 1, "improve": 1, "note": 1, "tbd": 0}),
        ("case_and_blanks", "## A\n- a\n  KIND: FIX\n- b\n\t kind:\timprove  \n- c\n  Kind:   Note\t\n", {"fix": 1, "improve": 1, "note": 1, "tbd": 0}),
        ("none", "## A\n- a\n- b\n  date: x\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 2}),
        ("unknown_value", "## A\n- a\n  kind: bug\n- b\n  kind:\n- c\n  kind: fix it\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 3}),
        ("two_values", "## A\n- a\n  kind: fix\n  kind: note\n- b\n  kind: fix\n  kind: bug\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 2}),
        ("same_value_twice", "## A\n- a\n  kind: fix\n  note: x\n  kind: Fix\n", {"fix": 1, "improve": 0, "note": 0, "tbd": 0}),
        ("in_fence", "## A\n```\n- a\n  kind: fix\n```\n- b\n```\n  kind: fix\n```\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 1}),
        ("fence_ends_the_job", "## A\n- a\n```\n```\n  kind: fix\n- b\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 2}),
        ("under_a_heading", "## A\n  kind: fix\n- a\n## B\n  kind: note\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 1}),
        ("under_other_text", "## A\n- a\ntext\n  kind: fix\n* star\n  kind: note\n- b\n  kind: improve\n", {"fix": 0, "improve": 1, "note": 0, "tbd": 1}),
        ("before_the_first_heading", "- a\n  kind: fix\n## A\n- b\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 1}),
        ("not_indented", "## A\n- a\nkind: fix\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 1}),
        ("sub_bullet_is_no_kind_line", "## A\n- a\n  - kind: fix\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 1}),
        ("blank_lines_between", "## A\n- a\n\n  kind: note\n\n- b\n", {"fix": 0, "improve": 0, "note": 1, "tbd": 1}),
        ("crlf", "## A\r\n- a\r\n  kind: fix\r\n- b\r\n  kind: improve\r\n", {"fix": 1, "improve": 1, "note": 0, "tbd": 0}),
        ("key_with_a_blank_before_the_colon", "## A\n- a\n  kind : fix\n", {"fix": 0, "improve": 0, "note": 0, "tbd": 1}),
        ("nothing", "", {"fix": 0, "improve": 0, "note": 0, "tbd": 0}),
    ]

    def test_the_sort_is_read_the_same_by_both_readers_on_each_case(self):
        for name, text, want in self.CASES:
            mine, entrance = sort_counts(text)
            self.assertEqual(mine, want, name)
            self.assertEqual(entrance["kinds"], want, name)
            self.assertEqual(entrance["count"], sum(want.values()), name)

    FRAGMENTS = ["## A", "## B", "  ## C", "### D", "# T", "- a", "- b", "- c", "* star", "-x", "text", "", "  kind: fix", "\tKind:IMPROVE ", "  kind:", "    kind: note",
                 "  kind: bug", "  - kind: fix", "kind: fix", "  date: x", "  source: y", "```", "~~~", "````", "갱신 2026-11-22 09:10", "갱신 2026-13-45 99:99", "- - -",
                 "  kind: Note", "- ", "　", " kind: improve", "  KIND:fix", "<!--", "-->"]

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
                self.assertEqual(seen["kinds"], counts, repr(text))
                self.assertEqual(seen["count"], session_status.backlog_total(parsed), repr(text))
                # the detail is read too: the same sorts, and the same count
                full = session_status.parse_backlog(text.splitlines(), detail=True)
                self.assertEqual(session_status.backlog_counts(full), counts, repr(text))

    def test_a_64_kib_document_counts_its_sorts_at_the_limit_and_not_one_byte_over(self):
        import artifact_entrance
        from fleet import session_status

        def sized(n):
            body, k = "## A\n", 0
            while True:
                piece = "- %05d\n  kind: %s\n" % (k + 1, ("fix", "improve", "note", "")[k % 4])
                if len((body + piece).encode()) > n:
                    break
                k += 1
                body += piece
            return body + "x" * (n - len(body.encode()))
        for size, readable in ((65536, True), (65537, False)):
            text = sized(size)
            self.assertEqual(len(text.encode()), size)
            with tempfile.TemporaryDirectory(prefix="bk") as folder:
                (Path(folder) / "backlog.md").write_text(text, encoding="utf-8")
                seen = artifact_entrance._backlog(Path(folder))
            counts = session_status.backlog_counts(session_status.parse_backlog(text.splitlines()))
            if readable:
                self.assertEqual(seen["kinds"], counts)
                self.assertGreater(counts["fix"], 500)
                self.assertEqual(seen["count"], sum(counts.values()))
            else:
                self.assertEqual((seen["count"], seen["kinds"]), (None, None))
                self.assertGreater(sum(counts.values()), 500)           # the panel reads on (to 1 MiB): the document is counted there

    def test_the_rule_is_written_in_the_meta_contract(self):
        meta = (ROOT / "core" / "ARTIFACT_META.md").read_text(encoding="utf-8")
        section = meta[meta.index("### `backlog.md`"):]
        for words in ("`kind:`", "`fix`", "`improve`", "`note`", "unsorted", "`TBD`", "nothing is refused, warned or gated", "not inside a code fence",
                      "The title line carries no tag, status or owner mark", "with its `kind:` line then", "register it as a question"):
            self.assertIn(words, section)


@unittest.skipUnless(NODE, "needs node")
class PanelPictureTest(unittest.TestCase):
    HEAD = (ROOT / "utilities/session_shell_panel_tidy_1122.test.py").read_text(encoding="utf-8").split('HEAD = r"""')[1].split('"""')[0]

    def docs(self):
        """The backlog values the host holds, from the documents the panel reads (the backend's own code)."""
        from fleet import session_status
        out = {}
        legacy = re.sub(r"^ +kind:.*\n|^\tKind:.*\n", "", MIXED, flags=re.M)
        many = "## A\n" + "".join("- 시험용 항목 %03d\n  kind: %s\n" % (n, "fix" if n % 3 else "improve") for n in range(1, 1300))
        for name, text, own in (("mixed", MIXED, "2026-11-22_this-window"), ("legacy", legacy, ""), ("notes", "## A\n- 참고 하나\n  kind: note\n- 참고 둘\n  kind: note\n", ""),
                                ("empty", "# T\n", ""), ("big", many, ""), ("tbd_notes", "## A\n- 참고\n  kind: note\n- 못 가른 일\n", "")):
            parsed = session_status.parse_backlog(text.splitlines(), detail=False)
            items, total = session_status.panel_backlog_items(parsed, own)
            out[name] = {"word": "BACKLOG", "has_doc": True, "items": items, "total": total, "counts": session_status.backlog_counts(parsed)}
        return out

    def node(self, body, docs):
        result = subprocess.run([NODE, "-e", self.HEAD + body, str(SHELL), json.dumps(docs, ensure_ascii=False)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_the_pictures_by_width_and_height_and_the_cells_of_every_row(self):
        self.node(r"""
const docs=JSON.parse(process.argv[2]);
const host=d=>({...d,items:d.items.map(i=>({mark:'',text:i.text,key:i.key,kind:i.kind}))});
const at=(o)=>{const v=draw({...o,status:status(),routes:1,messages:2,mem:1});return {v,ls:lines(v),a:inside(v)};};
const head=a=>a.find(l=>/^ BACKLOG/.test(l)),rows=a=>a.filter(l=>/^  (FIX |IMPR|NOTE|TBD ) /.test(l));
// every row of every picture has the panel's width (cells counted, Hangul two)
let pictures=0;
for(const name of Object.keys(docs))for(const width of [59,44,30])for(const height of [40,24,14]){
  const {ls}=at({width,height,backlog:host(docs[name])});pictures++;
  assert(ls.every(l=>cells(l)===width),name+' '+width+'x'+height+'\n'+ls.map(l=>cells(l)+' '+l).join('\n'));}
assert.equal(pictures,54);
// the mixed document at 59 and 44 wide, tall: the head tells the sorts, the three rows stand behind four cells (FIX, FIX, IMPR: the order is the sorts')
{const {a,ls}=at({width:44,height:40,backlog:host(docs.mixed)});
 assert(/^ BACKLOG FIX 3 · IMPR 2 · \+2 \/9 +›$/.test(head(a)),ls.join('\n'));
 const r=rows(a);assert.equal(r.length,3,a.join('\n'));
 assert.deepEqual(r.map(l=>l.slice(2,6)),['FIX ','FIX ','FIX '],r.join('\n'));}
{const {a}=at({width:59,height:40,backlog:{...host(docs.mixed),items:host(docs.mixed).items.slice(0,3)}});
 assert(/^ BACKLOG FIX 3 · IMPR 2 · \+2 \/9 +›$/.test(head(a)),a.join('\n'));
 assert(rows(a).every(l=>/^  (FIX |IMPR) 시험용/.test(l)),a.join('\n'));}
// FIX and IMPR are told by the word and the colour (the table's own: yellow, green; NOTE and TBD dim)
{const {v}=at({width:59,height:40,backlog:host({...docs.mixed,items:[{kind:'fix',text:'KIND_A',key:'i:fix:a'},{kind:'improve',text:'KIND_B',key:'i:imp:b'},{kind:'note',text:'KIND_C',key:'i:note:c'}]})});
 const row=txt=>v.rows.find(r=>r.map(p=>p.text).join('').includes(txt));
 const word=(txt,w)=>row(txt).find(p=>p.text===w);
 assert.equal(word('KIND_A','FIX ').foreground,186);assert.equal(word('KIND_B','IMPR').foreground,150);assert.equal(word('KIND_C','NOTE').dim,true);}
// no more width than 30: the ` · ` and the `/N` have given way, the words stay (and `+N`); one stage narrower the `+N` goes before the words do
{const {a}=at({width:30,height:40,backlog:host(docs.mixed)});assert(/^ BACKLOG FIX 3 IMPR 2 \+2 +›$/.test(head(a)),head(a));}
{const {a}=at({width:28,height:40,backlog:host(docs.mixed)});assert(/^ BACKLOG FIX 3 IMPR 2 +›$/.test(head(a)),head(a));}
{const {a}=at({width:20,height:40,backlog:host(docs.mixed)});assert(/^ BACKLOG 3 2 +›$/.test(head(a)),head(a));}
// one cell wider never tells less: the stages (nothing, the numbers, the words, `+N`, the ` · `, `/N`) only ever come in that order as the width grows, and `+N` never stands alone
{const rank=h=>{const t=h.replace(/^ BACKLOG/,'').replace(/ *›$/,'').trim();
   if(!t)return 0;if(!/[A-Z]/.test(t))return 1;if(t.includes('/'))return 5;if(t.includes('·'))return 4;if(/\+\d+\+?$/.test(t))return 3;return 2;};
 let tried=0;
 for(const name of Object.keys(docs)){let last=-1,seen=new Set();
  for(let width=14;width<=80;width++){const {a}=at({width,height:40,backlog:host(docs[name])});const h=head(a);tried++;
   assert(!/^ BACKLOG \+\d+\+? +›$/.test(h),name+' '+width+': '+h);
   const r=rank(h);assert(r>=last,name+' '+width+': '+h+' tells less than '+last);last=r;seen.add(r);}
  if(name==='big'||name==='mixed')for(const r of [1,2,3,4,5])assert(seen.has(r),name+' never stands at stage '+r);}
 assert.equal(tried,6*67);}
// an old document (no sort written): FIX and IMPR are `?`, every row is TBD
{const {a}=at({width:44,height:40,backlog:host(docs.legacy)});
 assert(/^ BACKLOG FIX \? · IMPR \? \/9 +›$/.test(head(a)),a.join('\n'));assert(rows(a).every(l=>l.startsWith('  TBD  ')),a.join('\n'));assert.equal(rows(a).length,3);}
// only notes: the zeros are told; no `+N`
{const {a}=at({width:44,height:40,backlog:host(docs.notes)});assert(/^ BACKLOG FIX 0 · IMPR 0 \/2 +›$/.test(head(a)),a.join('\n'));assert.equal(rows(a).length,2);assert(rows(a).every(l=>l.startsWith('  NOTE ')));}
// notes and unsorted but no FIX or IMPR: zeros
{const {a}=at({width:44,height:40,backlog:host(docs.tbd_notes)});assert(/^ BACKLOG FIX 0 · IMPR 0 \/2 +›$/.test(head(a)),a.join('\n'));assert.deepEqual(rows(a).map(l=>l.slice(2,6)),['TBD ','NOTE']);}
// an empty document and no document
{const {a}=at({width:44,height:40,backlog:host(docs.empty)});assert(/^ BACKLOG FIX 0 · IMPR 0 \/0 +›$/.test(head(a)),a.join('\n'));assert.equal(rows(a).length,0);}
{const {a,v}=at({width:44,height:40,backlog:{word:'BACKLOG',has_doc:false,items:[],total:0}});assert(/^ BACKLOG +›$/.test(head(a)),a.join('\n'));
 const at2=a.findIndex(l=>/^ BACKLOG/.test(l));assert(v.rows[at2+1].map(p=>p.text).join('').includes('SESSION'));}
// three-digit counts (and more) stand whole where there is room and give way where there is not
{const {a}=at({width:59,height:40,backlog:host(docs.big)});assert(/^ BACKLOG FIX 866 · IMPR 433 · \+1296 \/1299 +›$/.test(head(a))||/^ BACKLOG FIX 866 · IMPR 433 · \+1296 +›$/.test(head(a)),head(a));}
{const {a}=at({width:30,height:40,backlog:host(docs.big)});assert(/^ BACKLOG 866 433 +›$/.test(head(a)),head(a));}
{const {a}=at({width:36,height:40,backlog:host(docs.big)});assert(/^ BACKLOG FIX 866 IMPR 433 +›$/.test(head(a)),head(a));}
{const {a}=at({width:37,height:40,backlog:host(docs.big)});assert(/^ BACKLOG FIX 866 IMPR 433 \+1296 +›$/.test(head(a)),head(a));}
// a backend that sends no counts is drawn as it was
{const {a}=at({width:44,height:40,backlog:backlog()});assert(a.some(l=>/^ BACKLOG \+11 +›$/.test(l)),a.join('\n'));assert(a.some(l=>/^  · 시험용 나중 일 하나/.test(l)));}
""", self.docs())

    def test_short_of_rows_the_buttons_carry_fix_and_improve_together(self):
        self.node(r"""
const docs=JSON.parse(process.argv[2]);
const host=d=>({...d,items:d.items.map(i=>({mark:'',text:i.text,key:i.key,kind:i.kind}))});
const at=(o)=>{const v=draw({...o,status:status(),routes:1,messages:2,mem:1});return {v,ls:lines(v),a:inside(v)};};
// bottom layout: the first row ends in the two buttons, BACKLOG's number the FIX and IMPR together
{const v=draw({side:false,width:120,height:14,status:status(),backlog:host(docs.mixed),routes:1}),a=lines(v);
 assert(/STATUS \+\d+ ›  BACKLOG \+5 ›│$/.test(a[1])||/BACKLOG \+5 ›/.test(a[1]),a[1]);assert(a.every(l=>cells(l)===120));}
{const v=draw({side:false,width:120,height:14,status:status(),backlog:host(docs.legacy),routes:1}),a=lines(v);assert(/BACKLOG \? ›/.test(a[1]),a[1]);}
{const v=draw({side:false,width:120,height:14,status:status(),backlog:host(docs.notes),routes:1}),a=lines(v);assert(/BACKLOG ›/.test(a[1]),a[1]);}
// beside the native area, short of rows: the REPO row ends in the buttons and no list stands
{const {a}=at({width:44,height:14,backlog:host(docs.mixed)});assert(a.some(l=>/BACKLOG( \+5)? ›/.test(l))&&!a.some(l=>/^  (FIX|IMPR)/.test(l)),a.join('\n'));}
""", self.docs())


class BackendTest(unittest.TestCase):
    def setUp(self):
        from fleet import session_status
        import session_shell
        self.status, self.shell = session_status, session_shell
        self._tmp = tempfile.TemporaryDirectory(prefix="bkk")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        patch = mock.patch.object(session_status, "_root", lambda cwd, now: str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        self.cwd = str(self.root)

    def write(self, text):
        (self.root / "backlog.md").write_text(text, encoding="utf-8")

    def popup(self, part, own="2026-11-22_this-window", width=100, open=""):
        binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
        return self.shell._status_popup(binding, {"cwd": self.cwd, "routes": [], "status": {}, "part": part, "own": own, "open": open}, width, 256, False, "backlog")

    @staticmethod
    def text(entry):
        return "".join(run[0] for run in entry["rows"][0]) if entry.get("rows") and entry["rows"][0] else ""

    def test_the_list_is_by_sort_with_the_first_sort_open_and_this_windows_campaign_first(self):
        self.write(MIXED)
        entries = self.popup("list")["list"]
        got = [(self.text(e), e.get("key", "")[:6]) for e in entries]
        self.assertEqual([t for t, _ in got], [
            "시험용 백로그", "updated 11-22 09:10", "FIX 3 · IMPR 2 · TBD 3 · NOTE 1 · 9 total", "",
            "FIX 3", "  시험용 목록을 다시 열 때 빠진 항목 복구", "  시험용 문서 날짜가 사라지는 오류 재현", "  시험용 이 창 밖의 고칠 일", "",
            "  › IMPR 2", "    시험용 사용 안내 갱신", "    시험용 다른 사용 안내",
            "  › TBD 3", "    시험용 아직 못 가른 일", "    시험용 두 값", "    시험용 모르는 값",
            "  › NOTE 1", "    시험용 참고 하나", "", "  full document ›"])
        self.assertEqual([k for _, k in got if k.startswith("g:")], ["g:imp", "g:tbd", "g:note"])
        self.assertEqual({e["group"] for e in entries if e.get("group")}, {"imp", "tbd", "note"})      # (the open sort has no group of its own: it cannot be folded)
        # every item opens, to its lines with its sort first; a sorted item's own `kind:` line is not repeated, an unsorted one's stays
        self.assertTrue(all(e.get("lazy") for e in entries if e.get("key", "").startswith("i:")))
        keys = {self.text(e).strip(): e["key"] for e in entries if e.get("key", "").startswith("i:")}

        def detail(title):
            return ["".join(run[0] for run in row).strip() for row in self.popup("detail", open=keys[title])["rows"]]
        self.assertEqual(detail("시험용 사용 안내 갱신"), ["kind: IMPR", "source: 시험용 출처"])
        self.assertEqual(detail("시험용 문서 날짜가 사라지는 오류 재현"), ["kind: FIX", "date: 2026-11-20"])
        self.assertEqual(detail("시험용 아직 못 가른 일"), ["kind: TBD"])
        self.assertEqual(detail("시험용 두 값"), ["kind: TBD", "kind: fix", "kind: improve"])
        self.assertEqual(detail("시험용 모르는 값"), ["kind: TBD", "kind: bug"])

    def test_the_first_sort_with_an_item_is_open_and_an_old_document_is_all_tbd(self):
        self.write("# T\n## A\n- a\n  kind: improve\n- b\n  kind: note\n")
        shown = [self.text(e) for e in self.popup("list")["list"]]
        self.assertEqual(shown[2], "FIX 0 · IMPR 1 · TBD 0 · NOTE 1 · 2 total")
        self.assertEqual(shown[4:], ["IMPR 1", "  a", "", "  › NOTE 1", "    b", "", "  full document ›"])
        old = re.sub(r"^ +kind:.*\n", "", MIXED, flags=re.M)
        self.write(old)
        shown = [self.text(e) for e in self.popup("list")["list"]]
        self.assertEqual(shown[2], "FIX ? · IMPR ? · TBD 9 · NOTE ? · 9 total")
        self.assertEqual(shown[4], "TBD 9")
        self.assertFalse([t for t in shown if t.startswith("  ›")])
        self.write("# T\n")
        shown = [self.text(e) for e in self.popup("list")["list"]]
        self.assertEqual(shown[2], "FIX 0 · IMPR 0 · TBD 0 · NOTE 0 · 0 total")

    def test_an_items_key_names_the_same_item_in_the_panel_and_the_popup_whatever_the_window_and_the_sort_that_stands_open(self):
        self.write(MIXED + "## 2026-11-10_other\n- 시험용 문서 날짜가 사라지는 오류 재현\n  kind: fix\n")      # the same title twice in one campaign and sort
        popup_keys = {}
        for own in ("2026-11-22_this-window", "2026-11-10_other", "Unassigned", ""):
            parsed = self.status.parse_backlog(self.write_and_read(), detail=False)
            panel, _total = self.status.panel_backlog_items(parsed, own, first=99)
            listed = [e["key"] for e in self.popup("list", own=own)["list"] if e.get("key", "").startswith("i:")]
            self.assertEqual(sorted(i["key"] for i in panel), sorted(listed), own)
            self.assertEqual(len({i["key"] for i in panel}), len(panel), own)                          # no two items share a key
            for item in panel:
                popup_keys.setdefault(item["text"], set()).add(item["key"]) if item["text"] != "시험용 문서 날짜가 사라지는 오류 재현" else None
        # an item has one key whatever this window's campaign is
        self.assertTrue(all(len(keys) == 1 for keys in popup_keys.values()), popup_keys)
        # the key opens its own item
        entries = self.popup("list")["list"]
        for e in entries:
            if e.get("key", "").startswith("i:"):
                self.assertTrue(self.popup("detail", open=e["key"])["ok"], e["key"])
        # the panel's rows carry the sort of the item and the key's sort id says it
        parsed = self.status.parse_backlog(MIXED.splitlines(), detail=False)
        panel, total = self.status.panel_backlog_items(parsed, "2026-11-22_this-window")
        self.assertEqual(total, 9)
        self.assertEqual([(i["kind"], i["key"].split(":")[1]) for i in panel], [("fix", "fix"), ("fix", "fix"), ("fix", "fix")])
        panel, _ = self.status.panel_backlog_items(parsed, "", first=99)
        self.assertEqual([i["kind"] for i in panel], ["fix", "fix", "fix", "improve", "improve", "tbd", "tbd", "tbd", "note"])

    def write_and_read(self):
        return (self.root / "backlog.md").read_text(encoding="utf-8").splitlines()

    def test_a_panel_press_stands_the_popup_on_the_item_and_opens_its_sort(self):
        self.write(MIXED)
        parsed = self.status.parse_backlog(MIXED.splitlines(), detail=False)
        panel, _ = self.status.panel_backlog_items(parsed, "", first=99)
        pressed = [i for i in panel if i["kind"] == "note"][0]["key"]                               # an item of a folded sort
        with tempfile.TemporaryDirectory(prefix="bkj") as folder:
            path = Path(folder) / "answer.json"
            path.write_text(json.dumps(self.popup("list", own=""), ensure_ascii=False), encoding="utf-8")
            out = subprocess.run([NODE, "-e", r"""
const fs=require('node:fs'),path=require('node:path');
const list=require(path.join(process.argv[1],'status-list.cjs'));
const entries=list.entriesOf(JSON.parse(fs.readFileSync(process.argv[2],'utf8'))),key=process.argv[3];
const closed={sel:key,fold:new Set(),items:new Set()},gid=key.split(':')[1];
const open={sel:key,fold:new Set([gid]),items:new Set()};
console.log(JSON.stringify({gid,closed:list.layout(entries,closed).fellBack,open:list.layout(entries,open).fellBack,stands:list.layout(entries,open).sel===key}));
""", str(SHELL), str(path), pressed], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), {"gid": "note", "closed": True, "open": False, "stands": True})

    def test_the_panel_read_carries_the_counts_and_never_writes_the_document(self):
        path = self.root / "backlog.md"
        self.write(MIXED)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        page = {"path": str(self.root / "campaigns" / "x" / "campaign.md"), "has_doc": True}
        got = self.status.panel_lists(self.cwd, page, {})["backlog"]
        self.assertEqual(got["counts"], {"fix": 3, "improve": 2, "note": 1, "tbd": 3})
        self.assertEqual(got["total"], 9)
        self.popup("list")
        self.popup("detail", open=got["items"][0]["key"])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        path.unlink()
        self.assertIsNone(self.status.panel_lists(self.cwd, page, {})["backlog"]["counts"])

    def test_the_answer_of_a_long_backlog_stays_a_list_of_titles(self):
        body = "# T\n## A\n" + "".join("- 시험용 항목 %03d 이 제목은 조금 길어서 줄을 채우는 시험용 문장입니다\n  kind: %s\n" % (n, ("fix", "improve", "note", "x")[n % 4]) for n in range(500))
        self.write(body)
        answer = self.popup("list")
        self.assertTrue(answer["ok"] and answer.get("list"))
        self.assertEqual(len([e for e in answer["list"] if e.get("key", "").startswith("i:")]), 500)
        self.assertLess(len(json.dumps(answer, ensure_ascii=False).encode()), 200 * 1024)


class PictureTest(unittest.TestCase):
    """The popup as the product draws it (`utilities/status_popup_picture.py`: the backend's answer laid out by the host's list code in the popup's frame)."""

    THREE = ("# 시험용 백로그\n갱신 2026-11-22 09:10\n"
             + "".join("\n## %s\n" % name + "".join("- %s 항목 %02d\n  kind: %s\n" % (name[-1], n, ("fix", "improve", "note", "")[(n + k) % 4])
                                                   for n in range(1, 5 if name != "Demo C" else 4)) for k, name in enumerate(("Demo A", "Demo B", "Demo C"))))

    def picture(self, text, width, height, steps, own="Demo B"):
        with tempfile.TemporaryDirectory(prefix="bkp") as folder:
            path = Path(folder) / "backlog.md"
            path.write_text(text, encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "utilities/status_popup_picture.py"), str(path), "--kind", "backlog", "--own", own,
                                     "--width", str(width), "--height", str(height), "--steps", steps], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        screens = {}
        for block in result.stdout.split("--- ")[1:]:
            name, _, body = block.partition(" ---\n")
            screens[name] = [line for line in body.splitlines() if line.startswith(("╭", "│", "╰"))]
        return screens

    @staticmethod
    def cells(line):
        return sum(2 if ord(c) > 0x2e7f else 1 for c in line)

    def test_the_first_screen_a_group_and_an_item_at_100_by_26_and_140_by_40(self):
        for width, height, frame in ((100, 26, 100), (140, 40, 116)):
            shown = self.picture(self.THREE, width, height, "first,group,item")
            for name, rows in shown.items():
                self.assertTrue(rows, name)
                self.assertTrue(all(self.cells(r) == frame for r in rows), (width, name, [self.cells(r) for r in rows]))
            first = "\n".join(shown["first screen"])
            self.assertRegex(first, r"│ FIX \d+ · IMPR \d+ · TBD \d+ · NOTE \d+ · 11 total")
            self.assertIn("│ FIX ", first)
            self.assertIn("› IMPR", first) if height >= 40 else None
            opened = "\n".join(shown["an item opened"])
            self.assertIn("kind: FIX", opened)

    def test_a_folded_sort_opens_and_a_document_of_125_lines_stands(self):
        rows = "".join("\n## Demo %s\n" % name + "".join("- 시험용 항목 %s%03d\n  kind: %s\n" % (name, n, ("fix", "improve", "note", "")[(n + len(name)) % 4]) for n in range(1, 42))
                       for name in ("A", "BB", "CCC"))
        text = "# 시험용 백로그\n갱신 2026-11-22 09:10\n" + rows + "\n## Demo D\n- 시험용 둘\n- 시험용 셋\n"
        shown = self.picture(text, 100, 26, "first,group,end")
        self.assertTrue(any("125 total" in r for r in shown["first screen"]), shown["first screen"])
        self.assertTrue(any("full document" in r for r in shown["the end of the list"]), shown["the end of the list"][-4:])
        self.assertTrue(all(self.cells(r) == 100 for rows in shown.values() for r in rows))
        group = "\n".join(shown["a group opened"])
        self.assertRegex(group, r"▾ (IMPR|TBD|NOTE) \d+")

    def test_a_document_with_no_sort_is_one_tbd_group_and_an_empty_one_has_none(self):
        old = re.sub(r"^ +kind:.*\n", "", self.THREE, flags=re.M)
        first = "\n".join(self.picture(old, 100, 26, "first")["first screen"])
        self.assertIn("FIX ? · IMPR ? · TBD 11 · NOTE ? · 11 total", first)
        self.assertIn("│ TBD 11", first)
        self.assertNotIn("›", first.replace("full document ›", ""))
        empty = "\n".join(self.picture("# T\n", 100, 26, "first")["first screen"])
        self.assertIn("FIX 0 · IMPR 0 · TBD 0 · NOTE 0 · 0 total", empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
