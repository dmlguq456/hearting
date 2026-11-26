"""11-17 (NOW popup): the panel's side and the popup frame's side, in panel-view.

1. The panel's NOW row ends in `›` (the mark every other press-to-open row uses) and a press on it opens the NOW
   popup: beside the native area (every row of a wrapped NOW), in the bottom layout (the row it shares with the
   title). A panel with no NOW has neither.
2. The popup's list drawn by the backend (synthetic rows) stands in the popup frame on both faces and in every
   color count: the row stood on is a chosen ground (reversed where there is no hue), every row is as wide as the
   frame, and no size from 60 to 160 columns and 16 to 60 rows overflows or raises. The sections line names NOW.

Nothing here reads a real session, a real timeline or a real conversation, and nothing is sent to a harness.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "utilities/session-shell"
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools"), str(ROOT / "tools/fleet/tests")]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


POLISH = load("now_popup_panel_head", ROOT / "utilities/session_shell_panel_polish_1116.test.py")
POPUP = load("now_popup_synth", ROOT / "tools/fleet/tests/test_now_popup.py")
PANEL_TEST = load("now_popup_panel_test", ROOT / "utilities/session_shell_panel.test.py")
NODE = shutil.which("node")

PANEL = POLISH.HEAD + r"""
const o=JSON.parse(process.argv[2]);
const m=model(o);if(o.now!==undefined)m.now=o.now;if(o.title!==undefined)m.session_title=o.title;
const v=view.render({width:o.width??50,height:o.height??46,side:o.side??true,model:m,state:'working',harness:'claude',
  frame:1000,since:990,brief:o.question===false?undefined:brief,sent:()=>[],cellWidth:cells,
  configuration:{side_width:o.width??50,rule_motion:'rule',theme:'terminal',position:'right'},folded:!!o.folded});
const ls=lines(v);
console.log(JSON.stringify({lines:ls,hits:v.hits.filter(h=>h.action==='popup:NOW'),all:v.hits.map(h=>h.action)}));
"""

FRAME = r"""
const path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const i=JSON.parse(require('node:fs').readFileSync(process.argv[2],'utf8'));
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}]/u.test(c)?2:1),0);
const out=[];
for(const c of i.cases){
  const parts=r=>r.map(run=>({text:String(run[0]),...(c.answer.styles[run[1]]||{}),...(Number.isInteger(run[2])?{cells:run[2]}:{})}));
  const meta=c.answer.meta,at=meta.findIndex(m=>m.key===c.sel);
  const body=c.answer.rows.map((r,k)=>{const p=parts(r);return k===at?p.map(x=>({...x,tint:true})):p;});
  let v,error='';
  try{v=view.popup({cols:c.cols,rows:c.rows,band:0,kind:'table',title:[{text:'NOW · '},{text:c.answer.title}],body,sections:['STATUS','NOW','QUESTIONS','ROUTE','MESSAGES'],
    current:1,model:{styles:{},colors:c.colors,no_color:c.noColor},
    configuration:{theme:c.theme},cellWidth:cells,background:c.background||''});}catch(e){error=String(e&&e.stack||e);}
  if(error){out.push({id:c.id,error});continue;}
  out.push({id:c.id,rows:v.rows.map(r=>({width:r.reduce((n,p)=>n+cells(p.text),0),text:r.map(p=>p.text).join(''),
    chosen:r.some(p=>p.background===view.FACES[c.face||'dark'].chosen),reversed:r.some(p=>p.reverse===true&&p.text.trim()!==''),
    parts:r.map(p=>({text:p.text,background:p.background,reverse:p.reverse}))})),frame:v.frame,width:v.width,height:v.height,body:v.body});
}
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "needs node")
class PanelOpenerTest(unittest.TestCase):
    def draw(self, **options):
        out = subprocess.run([NODE, "-e", PANEL, str(SHELL), json.dumps(options)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_the_nows_last_row_ends_in_the_mark_and_every_row_of_it_opens_the_popup(self):
        for width in (50, 40, 34):
            with self.subTest(width=width):
                got = self.draw(width=width, now="두 줄 꼴" if width == 50 else "한 줄에 다 들어가지 않는 아주 긴 요약 " * 3)
                rows = [i for i, line in enumerate(got["lines"]) if line.startswith("│ NOW") or (got["hits"] and i in {h["row"] for h in got["hits"]})]
                self.assertTrue(rows and got["hits"], got["lines"])
                self.assertEqual(sorted(rows), sorted(h["row"] for h in got["hits"]))
                last = got["lines"][max(rows)].rstrip("│ ").rstrip()
                self.assertTrue(last.endswith("›"), got["lines"][max(rows)])
                self.assertTrue(all(h["action"] == "popup:NOW" for h in got["hits"]))

    def test_a_panel_with_no_now_has_no_mark_and_no_press(self):
        got = self.draw(now="")
        self.assertEqual(got["hits"], [])
        self.assertFalse(any(line.startswith("│ NOW") for line in got["lines"]), got["lines"])
        self.assertIn("popup:ROUTE", got["all"] + ["popup:ROUTE"])

    def test_the_bottom_layout_shares_the_title_row_and_ends_it_in_the_mark(self):
        got = self.draw(side=False, width=100, height=9, now="두 줄 꼴", title="패널 손질")
        self.assertEqual(len(got["hits"]), 1, got)
        row = got["lines"][got["hits"][0]["row"]]
        self.assertIn("NOW 두 줄 꼴", row)
        self.assertTrue(row.rstrip("│ ").rstrip().endswith("›"), row)
        narrow = self.draw(side=False, width=40, height=9, now="아주 긴 요약 " * 8, title="패널 손질 제목")
        self.assertEqual(len(narrow["hits"]), 1, narrow)
        self.assertTrue(narrow["lines"][narrow["hits"][0]["row"]].rstrip("│ ").rstrip().endswith("›"))

    def test_the_other_presses_stay(self):
        with_now = self.draw(now="두 줄 꼴")
        without = self.draw(now="")
        self.assertEqual(sorted(a for a in with_now["all"] if a != "popup:NOW"), sorted(without["all"]))


SWEEP = POLISH.HEAD + r"""
// The same panel-view without the mark (`base`, made from the source by the test) and with it: the mark changes the
// NOW's last row and nothing else, in the side layout across its sizes.
const base=require(process.argv[2]);
const o=JSON.parse(process.argv[3]);
const paint=(mod,w,h,now,title)=>{const m=model({});m.now=now;m.session_title=title;
  const v=mod.render({width:w,height:h,side:true,model:m,state:'working',harness:'claude',frame:1000,since:990,brief:undefined,sent:()=>[],cellWidth:cells,
  configuration:{side_width:w,rule_motion:'rule',theme:'terminal',position:'right'},folded:false});
  return {lines:lines(v),now:v.hits.filter(x=>x.action==='popup:NOW').map(x=>x.row)};};
const bad=[];let runs=0,changed=0;
for(const title of o.titles)for(let w=o.w?o.w[0]:30;w<=(o.w?o.w[1]:60);w++)for(let h=o.h?o.h[0]:16;h<=(o.h?o.h[1]:40);h++)for(const n of o.lengths){
  const now=('가'.repeat(n)).replace(/(.{4})/g,'$1 ').trim(),a=paint(view,w,h,now,title),b=paint(base,w,h,now,title);runs++;
  const ours=new Set(a.now);
  if(a.lines.length!==b.lines.length){bad.push({w,h,n,why:'rows',a:a.lines.length,b:b.lines.length});continue;}
  const last=Math.max(...a.now,-1);
  for(let i=0;i<a.lines.length;i++){
    if(a.lines[i]===b.lines[i])continue;
    // the NOW's last row alone may differ, and it ends in the mark
    if(i===last&&a.lines[i].trimEnd().replace(/\s*│$/,'').endsWith('›')){changed++;continue;}
    bad.push({w,h,n,why:'row '+i,a:a.lines[i],b:b.lines[i]});break;
  }
}
console.log(JSON.stringify({runs,changed,bad:bad.slice(0,6),badCount:bad.length}));
"""


@unittest.skipUnless(NODE, "needs node")
class MarkKeepsTheRestTest(unittest.TestCase):
    """11-17 review: the mark takes room only where the last row of the NOW has it; no other row of the panel moves."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.dir = Path(tempfile.mkdtemp(prefix="now-mark-"))
        source = (SHELL / "panel-view.cjs").read_text(encoding="utf-8")
        mark = "[text(' '),text('›','dim')]"
        for here in ("Infinity," + mark, "now?" + mark + ":[]"):     # the NOW field's mark and the bottom row's
            assert source.count(here) == 1, here
            source = source.replace(here, here.replace(mark, "[]"))
        (cls.dir / "base.cjs").write_text(source, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def sweep(self, **options):
        out = subprocess.run([NODE, "-e", SWEEP, str(SHELL), str(self.dir / "base.cjs"), json.dumps(options)],
                             capture_output=True, text=True, check=True, timeout=240)
        return json.loads(out.stdout)

    def test_no_row_but_the_nows_last_moves_from_30_to_60_wide_and_16_to_40_high(self):
        got = self.sweep(titles=["패널 손질", "제목도 길어서 여러 줄로 접히는 하팅 v4-open 감독 셸 상태 고치기와 Fleet 세션 줄 다듬기"],
                         lengths=[6, 20, 27, 28, 54, 55, 100])
        self.assertGreater(got["runs"], 10000)
        self.assertEqual(got["badCount"], 0, got["bad"])
        self.assertGreater(got["changed"], 0)       # the mark is there where the sweep put a NOW

    def test_the_default_side_panel_at_40_by_24_with_a_54_cell_now_keeps_its_title(self):
        # The review's case: 40 columns, 24 rows, a NOW of 54 cells (27 wide characters). Every row but the NOW's last
        # stands as it stood without the mark, the title's rows and the first of the NOW's two among them.
        title = "제목도 길어서 여러 줄로 접히는 하팅 v4-open 감독 셸 상태 고치기와 Fleet 세션 줄 다듬기"
        got = self.sweep(titles=[title, "패널 손질"], lengths=[27, 28, 29], w=[40, 40], h=[24, 24])
        self.assertEqual((got["runs"], got["badCount"]), (6, 0), got["bad"])
        self.assertEqual(got["changed"], 6)         # and the mark is on the NOW's last row in every one

    def test_the_panel_suites_own_session_at_40_wide_keeps_its_title_and_its_other_rows(self):
        # The same session the panel suite draws (title, messages, a question), 40 wide, a NOW of 56 cells: where the
        # mark took two cells of every row of the NOW, its last row spilled onto a row of its own (or the title folded);
        # now every row but the NOW's last stands as it stood without the mark.
        script = PANEL_TEST.PanelTest.FIXTURE + r"""
const base=require(process.argv[2]);
const paint=(mod,h,now)=>{const v=mod.render({width:40,height:h,side:true,fill:true,model:{...head,now,routes:[],route_count:0},state:'working',
  configuration:{},expandedKeys:[],questions:()=>[]});return {lines:v.rows.map(r=>r.map(p=>p.text).join('')),now:v.hits.filter(x=>x.action==='popup:NOW').map(x=>x.row)};};
const out=[];
for(const h of [24,25,26,27,28])for(const n of [24,25,26,27,28]){
  const now=('가'.repeat(n)).replace(/(.{4})/g,'$1 ').trim(),a=paint(view,h,now),b=paint(base,h,now),last=Math.max(...a.now,-1);
  const odd=a.lines.length!==b.lines.length||a.lines.some((s,i)=>s!==b.lines[i]&&i!==last);
  out.push({h,n,cells:cells(now),odd,mark:last>=0&&a.lines[last].trimEnd().replace(/\s*│$/,'').endsWith('›')});
}
console.log(JSON.stringify(out));
"""
        out = subprocess.run([NODE, "-e", script, str(SHELL / "panel-view.cjs"), str(self.dir / "base.cjs")], capture_output=True, text=True, check=True)
        cases = json.loads(out.stdout)
        self.assertEqual([c for c in cases if c["odd"]], [], cases)
        self.assertTrue(all(c["mark"] for c in cases), cases)
        self.assertTrue(any(c["h"] in (24, 26, 27) and c["cells"] in (54, 56, 58) for c in cases))

    def test_a_now_that_fills_its_last_row_takes_the_mark_as_an_ellipsis_at_its_end(self):
        script = POLISH.HEAD + """
const m=model({});m.session_title='제목';m.now=process.argv[2];
const v=view.render({width:40,height:24,side:true,model:m,state:'working',harness:'claude',frame:1000,since:990,brief:undefined,sent:()=>[],cellWidth:cells,
 configuration:{side_width:40,rule_motion:'rule',theme:'terminal',position:'right'},folded:false});
console.log(JSON.stringify(lines(v)));"""
        def row(now):    # (24 rows: the NOW is short of rows in its one-row form at this height, as at the 30 it stood at before 11-20)
            out = subprocess.run([NODE, "-e", script, str(SHELL), now], capture_output=True, text=True, check=True)
            return [line for line in json.loads(out.stdout) if line.startswith("│  NOW") or line.startswith("│         ")]
        full = "가가가가 가가가가 가가가가 가가가가 가가가"      # fills the 32 cells of the NOW's row (a word of 3)
        rows = row(full)
        self.assertTrue(rows[-1].rstrip("│ ").rstrip().endswith("…›"), rows)   # no room: `…›` takes the row's end
        short = row("가가가가 가가가가")
        self.assertTrue(short[-1].rstrip("│ ").rstrip().endswith(" ›") and "…" not in short[-1], short)  # room: ` ›`


@unittest.skipUnless(NODE, "needs node")
class PopupFrameTest(unittest.TestCase):
    def answer(self, harness="claude", colors=256, no_color=False, prompts=4, days=2):
        import session_shell
        from fleet.explorer import timeline as tl
        story = POPUP.synth(prompts=prompts, nows=3, days=days, harness=harness)
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"}), mock.patch.object(tl, "rows", return_value=story):
            return session_shell._now_popup({"harness": harness, "sid": POPUP.SID}, {"rows": 20, "columns": 100}, 100, colors, no_color), story

    def frames(self, cases, tmp):
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
        out = subprocess.run([NODE, "-e", FRAME, str(SHELL), str(path)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_the_row_stood_on_is_a_chosen_ground_or_reversed_on_both_faces_and_every_color_count(self):
        import tempfile
        cases = []
        for harness in ("claude", "codex", "opencode"):
            for face, theme in (("dark", "terminal"), ("light", "light")):
                for colors, no_color, theme_use in ((256, False, theme), (8, False, theme), (256, True, theme), (256, False, "mono")):
                    answer, story = self.answer(harness, colors, no_color)
                    sel = story[4]["key"]
                    cases.append({"id": [harness, face, colors, no_color, theme_use], "answer": answer, "sel": sel, "cols": 120, "rows": 40,
                                  "theme": theme_use, "colors": colors, "noColor": no_color, "face": face})
        with tempfile.TemporaryDirectory(prefix="np-", dir="/tmp") as tmp:
            for case, got in zip(cases, self.frames(cases, tmp)):
                with self.subTest(case=case["id"]):
                    self.assertNotIn("error", got, got)
                    hued = case["colors"] >= 256 and not case["noColor"] and case["theme"] != "mono"
                    rows = got["rows"]
                    marked = [r for r in rows if (r["chosen"] if hued else r["reversed"])]
                    self.assertEqual(len(marked), 1, [r["text"] for r in rows])
                    at = case["answer"]["meta"].index(next(n for n in case["answer"]["meta"] if n["key"] == case["sel"]))
                    self.assertIn("".join(run[0] for run in case["answer"]["rows"][at])[:20].strip()[:8], marked[0]["text"])
                    # The popup's inside is one ground: a row is as wide as the frame.
                    self.assertEqual({r["width"] for r in rows if r["text"].strip()} - {got["width"], got["frame"]["width"]}, set())
                    self.assertIn("NOW", rows[-1]["text"] if "NOW" in rows[-1]["text"] else rows[-2]["text"])

    def test_no_size_from_60_to_160_wide_and_16_to_60_high_overflows_or_raises(self):
        import tempfile
        answer, story = self.answer(prompts=7, days=3)
        cases = [{"id": [cols, rows], "answer": answer, "sel": story[9]["key"], "cols": cols, "rows": rows, "theme": "terminal",
                  "colors": 256, "noColor": False} for cols in range(60, 161, 10) for rows in range(16, 61, 11)]
        with tempfile.TemporaryDirectory(prefix="np-", dir="/tmp") as tmp:
            for case, got in zip(cases, self.frames(cases, tmp)):
                with self.subTest(size=case["id"]):
                    self.assertNotIn("error", got, got)
                    cols, rows = case["cols"], case["rows"]
                    self.assertLessEqual(len(got["rows"]), rows)
                    self.assertTrue(all(r["width"] <= cols for r in got["rows"]), [r["width"] for r in got["rows"]])
                    self.assertLessEqual(got["frame"]["width"], cols)

    def test_the_popup_at_80_and_120_columns(self):
        import tempfile
        answer, story = self.answer()
        cases = [{"id": cols, "answer": answer, "sel": story[3]["key"], "cols": cols, "rows": 40, "theme": "terminal", "colors": 256,
                  "noColor": False} for cols in (80, 120)]
        with tempfile.TemporaryDirectory(prefix="np-", dir="/tmp") as tmp:
            narrow, wide = self.frames(cases, tmp)
        # 80 columns: the frame's inside is 72 cells; 120: 112.
        self.assertEqual((narrow["frame"]["width"] - 4, wide["frame"]["width"] - 4), (72, 112))


if __name__ == "__main__":
    unittest.main()
