"""11-27 a press on a route opens a page whose frame stands at the top from the first moment and is not pushed down by its reading.

The two that remained of #1760 (closed; built again on today's main, the person's answer: "A + D 둘 다 넣음 — #1760은 닫음"):

  * A. The popup's route page draws its frame first (the legs' band, the pressed leg's stage boxes, closed under them), then the route row
    (with its kind and worker lines), then what the page says about the route (`why`, `next`): the frame stands on the rows its first
    screen drew it on. Fleet's own screen keeps its order (`whole=False`).
  * D. The panel's `routePageFirst` draws that frame for every form, a unit's, a solo's (`one-shot-owner`), a staged and a team route, from
    the values the panel holds (`bands`, `head_repo`, `session_tag`, the stages). The two drawings are one in every cell but the value cells
    (the border, the head, the names and the arrows stand where the reading puts them), at 80, 120 and 132 columns, with one, three and six
    stages, with every value and with those the panel does not hold yet.
  * A first screen that cannot be drawn (it throws) falls back to the panel's route box: the picture is one the popup can do without.
  * No word changes: the head, the stage boxes' titles and every value are Fleet's own (a team route's first screen keeps the form word `team`
    it has always said where the reading's head says `staged`), and the stage boxes keep the colors the first screen drew them in.
  * Reviews of #1900 (11-27): the frame stands first only where it all fits: the first screen holds the frame's values (a band of six legs at
    most), the page request carries what that first screen was (`framed`), and the whole page fits the answer's rows and bytes. Else the
    answer is main's, byte for byte (a long route, seven legs, a cache that kept the route without these values; a long route's first screen
    still stands the frame, which the reading replaces). The band's stage line is the producer's count over the whole leg; a long repository
    name stands the tag where the reading does; a value not of its shape is no value.
  * What this does not hold equal, by design: what only the reading knows lengthens the frame under the band's panel title, never above it — a
    parallel pair (`∥ 2-way`, measured 28 → 35 rows to the frame's bottom at 132×46), a loop (`↺ N back`, 21 → 24), the owner's raw strip (28 → 34),
    more than sixteen stages (50 → 56, the first screen's `+N` under the sixteen). The comparison here covers chains of one, three and six stages.
Everything is synthetic; no real session, account or terminal is touched.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
from unittest import mock
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "utilities")]

NAMES = ("plan", "plan-check", "execute", "impl-review", "test", "report")
RID = "rt-a000000000000001"
KEYS = ("dim", "loc_repo", "g_idle", "g_work", "g_dead", "g_blocked", "lvl_g", "lvl_y", "lvl_r", "tag_steward", "hb_claude", "hb_codex", "hb_opencode", "hb_other")
FORMS = ("unit", "solo", "staged", "team")
# The rows' width the route popup gives a window of 80, 120 or 132 columns (`popupWidth`: cols-4, the popup's own sides less 4).
COLUMNS = {80: 72, 120: 112, 132: 124}

# The first screen main (815746a78) draws for `build(form, 3, lack=True)` at 72 columns without the frame's values, read from main's own
# `routePageFirst`: the stage boxes of a staged route; none (the route list) for a unit's or a solo's.
_BOXES = ["", "    ┌─ plan ────── ✓ ┐    ┌─ plan-che… ─ ✓ ┐    ┌─ execute ─── ● ┐", "    │ by     claude  │    │ by     claude  │    │ by     claude  │", "    │ time   30m     │    │ time   20m     │    │ time   10m     │", "    │ out    ·       │ ▶  │ out    ·       │ ▶  │ out    ·       │", "    │ unit   ·       │    │ unit   ·       │    │ unit   ·       │", "    └────────────────┘    └────────────────┘    └────────────────┘"]
BEFORE = {"staged": ["╔═ 그림 시험 route  1 leg · team ══════════════════════════════════════╗", "┌─ code ──────────────────────────────── ● ┐", "│                                          │", "└──────────────────────────────────────────┘"] + _BOXES,
          "team": ["╔═ 그림 시험 route  2 legs · team ═════════════════════════════════════╗", "┌─ frame ───────────────────── ✓ ┐    ┌─ code ────────────────────── ● ┐", "│                                │ ▶  │                                │", "└────────────────────────────────┘    └────────────────────────────────┘"] + _BOXES}

NODE = r"""
const fs=require('node:fs'),path=require('node:path');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const cases=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const measure=s=>[...s].reduce((n,c)=>n+(c.codePointAt(0)>0x2e7f?2:1),0);
const text=r=>r.map(p=>p.text).join('');
const out=cases.map(c=>{
  const first=view.routePageFirst({model:c.model,id:c.id,columns:c.columns,cellWidth:measure});
  const tops={};
  for(const rows of c.windowRows){
    // (as `host.cjs` `popupNow` stands a route's page: `fixedTop`)
    const make=parts=>view.popup({cols:c.cols,rows,kind:'route',title:[{text:'ROUTE'}],body:parts,sections:['ROUTE'],current:0,model:{styles:{}},cellWidth:measure,fixedTop:true}).frame;
    tops[rows]={first:first?make(first.rows):null,read:make(c.read.map(t=>[{text:t}]))};
  }
  return {first:first?first.rows.map(text):null,tops};
});
process.stdout.write(JSON.stringify(out));
"""

LABELS = r"(by|time|rounds|gate|out|unit|says)"


def mask(rows):
    """The frame's rows with the cells a value can stand in blanked: a box's value after its label, the leg band's owner line, the head's last word."""
    out = []
    for row in rows:
        row = re.sub(r"^(╔═ .*) · [\w-]+ ═+╗$", r"\1 · WORD ═╗", row)     # (a team route's first screen says `team` where the reading says `staged`)
        row = re.sub(r"│ " + LABELS + r"([^│]*)│",
                     lambda m: "│ " + m.group(1) + m.group(2)[:7 - len(m.group(1))] + " " * (len(m.group(2)) - (7 - len(m.group(1)))) + "│", row)
        row = re.sub(r"│ owner  ([^│]*)│", lambda m: "│ owner  " + " " * len(m.group(1)) + "│", row)
        out.append(row)
    return out


def frame_of(rows):
    """The rows from the top rule to the frame's bottom rule."""
    bottom = next((i for i, row in enumerate(rows) if row.startswith("╚═")), len(rows) - 1)     # (a drawing with no bottom rule: all of it)
    return rows[:bottom + 1]


def same_cells(first, read):
    """The first screen's frame and the reading's, one in every cell but the values'. Returns a reason or None."""
    a, b = frame_of(first), frame_of(read)
    if len(a) != len(b):
        return "the frame is %d rows first and %d read" % (len(a), len(b))
    for i, (x, y) in enumerate(zip(mask(a), mask(b))):
        if x != y:
            return "row %d differs:\n first: %s\n  read: %s" % (i, x, y)
    return None


def build(form, n, lack=False):
    """A route as the live reader hands it over (Fleet's route and its nodes) and the producer's record for the panel."""
    from fleet.explorer import live

    def node(i, name, after=None):
        state = "done" if n > 1 and i < 2 else "active" if i == min(2, n - 1) else "todo"
        return {"id": name, "state": state, "harness": "claude", "model": "Opus 5.5", "effort": "high",
                "unit": "dev/backend" if state == "active" else "qa/" + name, "write_scope": ["artifacts/%s.md" % name] if state == "done" else [],
                "gate_passed": state == "done", "elapsed_min": 30 - 10 * i if state != "todo" else None, "depends_on": [after] if after else []}
    staged = form in ("staged", "team")
    names = [NAMES[i] if staged or i else ("one-shot" if form == "solo" else "unit") for i in range(n)]
    view_nodes = [node(i, names[i], names[i - 1] if i else None) for i in range(n)]
    if staged:
        # (a staged route is one code leg; a team route's code leg follows its frame, done)
        legs = ([["frame", "done", None]] if form == "team" else []) + [["code", "active", "%d/%d" % (min(2, n), n)]]
        capability, shape = "code", "staged"
    else:
        view_nodes[0].update(state="active", gate_passed=False, elapsed_min=3, depends_on=[], write_scope=[],
                             unit="_kernel/owner" if form == "solo" else "qa/code-review")
        legs = [[("code" if form == "solo" else "unit"), "active", "0/%d" % n]]
        capability, shape = ("code" if form == "solo" else "unit"), "one-shot-owner"
    stages = [live._stage_record(item) for item in view_nodes]
    route = {"id": RID, "repo": "project", "session_tag": "e9", "title": "그림 시험 route", "state": "working", "outcome": None, "result_at": None,
             "worker": ["owner" if form == "solo" else "worker", [["claude", "hb_claude"]], [["running 10m", "dim"]]], "age": "23m", "legs": legs,
             "stages": stages, "resume": None, "activity": "running", "harness": "claude", "model": ("Opus 5.5", None), "effort": "high",
             "mode": None if form == "unit" else "dev", "intensity": "standard" if staged else "quick", "profile": "deep", "field": "code",
             "capability": capability, "shape": shape, "worker_stage": "execute" if staged else view_nodes[0]["id"],
             "worker_unit": view_nodes[0]["unit"], "owner_gauge": [40, 0.2], "worker_contexts": [], "owner": None, "current": True,
             "visibility": {"schema": "route_visibility_v1", "route_id": RID, "summary": "ok", "next": "wait",
                            "lines": [["why", "the person asked for it", "info"], ["next", "wait for the stage", "info"]]}}
    groups = live._node_groups(view_nodes, route, RID, legs, time.time(), sum(1 for item in view_nodes if item["state"] == "done"), "active")
    record = live._panel_route_record(route, {RID: groups})
    if lack:     # what the panel does not hold yet (the reading fills it): a time, an output, a unit, the owner's line
        for stage in record["stages"]:
            for key in ("when", "out", "unit"):
                stage.pop(key, None)
        for band in record["bands"]:
            band["lines"] = [line[:1] for line in band["lines"]]       # (the label stays: `owner  `, the values are the reading's)
    vm = json.loads(json.dumps({"repos": [{"id": "project", "name": "project"}], "system": {}, "routes": [route], "nodes": {RID: groups},
                                "sessions": [{"tag": "e9", "repo_id": "project", "harness": "claude", "model": "Opus 5.5", "effort": "high", "context": 46,
                                              "window": 200000, "state": "working", "title": "이 창", "now": "그리는 중", "age": "23m"}]}, default=list))
    return vm, record


def panel_of(record):
    from fleet import session_panel
    return session_panel.presentation({}, {"route_panels": {"claude\0sid": [record]}}, "claude", "sid", 40)["routes"]


def read_page(vm, columns, framed=True):
    """The backend's reading of the route's page (`_panel_popup`) over the given collection: its rows as text."""
    result = page_answer(vm, columns, framed)
    assert result.get("ok") and result.get("page"), result
    return text_of(result)


def text_of(result):
    return ["".join(text for text, _style in row) for row in result["rows"]]


class RoutePageFrameFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is needed for the panel's drawing")
        cls.tmp = tempfile.TemporaryDirectory(prefix="rpf1127-", dir="/tmp")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.env_patch = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"})
        cls.env_patch.start()
        cls.addClassCleanup(cls.env_patch.stop)
        from fleet.palette import style_components
        cls.styles = {key: style_components(key, colors=256, no_color=False) for key in KEYS}
        cls.pictures = {}

    def draw(self, cases):
        path = Path(self.tmp.name) / ("case-%d.json" % time.monotonic_ns())
        path.write_text(json.dumps(cases))
        done = subprocess.run([self.node, "-e", NODE, str(ROOT / "utilities/session-shell"), str(path)], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def pictures_of(self, keys):
        """``{(form, n, window, lack): (first rows, read rows, tops)}``: the panel's first screen, the backend's reading, the popup's frames."""
        todo = [key for key in keys if key not in self.pictures]
        if todo:
            cases, reads = [], []
            for form, n, window, lack in todo:
                vm, record = build(form, n, lack)
                read = read_page(vm, COLUMNS[window])
                reads.append(read)
                cases.append({"model": {"routes": panel_of(record), "styles": self.styles, "colors": 256}, "id": RID, "columns": COLUMNS[window],
                              "cols": window, "windowRows": [24, 46], "read": read})
            for key, read, got in zip(todo, reads, self.draw(cases)):
                self.pictures[key] = (got["first"], read, got["tops"])
        return {key: self.pictures[key] for key in keys}

    def picture(self, form, n, window, lack=False):
        return self.pictures_of([(form, n, window, lack)])[(form, n, window, lack)]

    def test_the_first_screen_is_the_readings_frame_in_every_cell_but_the_values(self):
        keys = [(form, n, window, lack) for form in FORMS for window in COLUMNS for n in (1, 3, 6) for lack in (False, True)]
        for (form, n, window, lack), (first, read, _tops) in self.pictures_of(keys).items():
            with self.subTest(form=form, window=window, stages=n, lack=lack):
                self.assertIsNotNone(first, "no first screen for the %s route" % form)
                self.assertIsNone(same_cells(first, read))

    def test_the_cells_comparison_sees_a_moved_cell(self):
        first, read, _tops = self.picture("team", 6, 120)
        moved = [row.replace("┌─ plan ", "┌── plan ", 1) for row in first]
        self.assertIn("differs", same_cells(moved, read) or "")
        self.assertIsNotNone(same_cells(first[:-3] + ["╚" + "═" * 5 + "╝"], read))
        # (the band one row lower, as the reading put it before 11-27 under its route row, is caught too)
        self.assertIsNotNone(same_cells(first[:1] + [""] + first[1:], read))

    def test_the_frame_is_the_pages_first_row_and_its_top_edge_is_the_same_first_and_read(self):
        keys = [(form, n, window, False) for form in FORMS for window in COLUMNS for n in (1, 6)]
        for (form, n, window, _lack), (first, read, tops) in self.pictures_of(keys).items():
            with self.subTest(form=form, window=window, stages=n):
                self.assertTrue(first[0].startswith("╔═") and read[0].startswith("╔═"), (first[0], read[0]))
                for rows in ("24", "46"):
                    pair = tops[rows]
                    self.assertEqual(pair["first"]["top"], pair["read"]["top"], pair)
                    self.assertLessEqual(pair["first"]["height"], pair["read"]["height"])

    def test_the_frame_closes_under_its_last_box_and_a_box_is_as_tall_as_its_lines(self):
        keys = [(form, n, window, False) for form in FORMS for window in (80, 120) for n in (1, 3, 6)]
        for (form, n, window, _lack), (first, read, _tops) in self.pictures_of(keys).items():
            with self.subTest(form=form, window=window, stages=n):
                for rows in (first, read):
                    frame = frame_of(rows)
                    last_box = max(i for i, row in enumerate(frame) if "│ └" in row)
                    self.assertEqual(len(frame) - 1 - last_box, 1, "\n".join(frame))      # (nothing stretched under the boxes)
                    for top in (i for i, row in enumerate(frame) if re.search(r"│ ┌─ ", row)):
                        bottom = next(i for i in range(top, len(frame)) if "│ └" in frame[i])
                        filled = [row for row in frame[top + 1:bottom] if re.search(r"│ (by|time|rounds|gate|out|unit|says) ", row)]
                        self.assertEqual(bottom - top - 1, len(filled), "an empty row in a box:\n" + "\n".join(frame[top:bottom + 1]))

    def test_no_word_of_fleets_frame_changes_for_any_form(self):
        words = {"unit": ("one-shot-owner", "one-shot-owner", "unit"), "solo": ("one-shot-owner", "one-shot-owner", "one-shot"),
                 "staged": ("staged", "team", "plan"), "team": ("staged", "team", "plan")}
        for form, (read_word, first_word, title) in words.items():
            for window in (80, 132):
                with self.subTest(form=form, window=window):
                    first, read, _tops = self.picture(form, 1 if form in ("unit", "solo") else 3, window)
                    self.assertRegex(read[0], r"^╔═ project/ › \[e9\] › ")
                    self.assertRegex(first[0], r"^╔═ project/ › \[e9\] › ")
                    if window == 132:      # (at 80 columns Fleet's head is cut at the rule)
                        self.assertRegex(read[0], r" · %s ═+╗$" % read_word)
                        self.assertRegex(first[0], r" · %s ═+╗$" % first_word)
                    for rows in (first, read):
                        self.assertTrue(any("┌─ %s " % title in row for row in rows), rows)
                        self.assertRegex(rows[1], r"^║  ┌─ leg ")       # (the band follows the head at once)

    def test_without_the_frames_values_both_screens_are_as_before(self):
        # (review 2 of #1900, red 2: seven legs, or a cache that kept the route without its band, opened on the route list and turned into the frame
        # once read; main draws that route's stage boxes from the first moment. An older backend's record is the same case.)
        for form in FORMS:
            for how in ("no band", "seven legs"):
                with self.subTest(form=form, how=how):
                    vm, record = build(form, 3, lack=True)
                    if how == "no band":
                        record.pop("bands")
                    panel = panel_of(record)
                    if how == "seven legs":
                        panel[0]["bands"] = [dict(record["bands"][0], n="leg %d" % i) for i in range(7)]
                    got = self.draw([{"model": {"routes": panel, "styles": self.styles}, "id": RID, "columns": 72, "cols": 80, "windowRows": [24],
                                      "read": ["x"]}])[0]["first"]
                    # (as main's `routePageFirst` draws it, cell for cell: the stage boxes of a staged route, the route list for a unit's or a solo's)
                    self.assertEqual(got, BEFORE.get(form))
                    # (the reading for a first screen that was not the frame: the page as before, the lines first)
                    self.assertEqual(read_page(vm, 72, framed=False), text_of(drawn(vm, 72, False)[0]))

    def test_the_page_order_is_the_frame_then_the_route_row_then_the_lines_for_a_popup_only(self):
        from fleet.explorer import pages
        from fleet.explorer.grid import CellGrid
        from fleet.explorer.view_model import ExplorerViewModel
        for form in FORMS:
            with self.subTest(form=form):
                vm, _record = build(form, 3)
                text = "\n".join(read_page(vm, 112))
                self.assertLess(text.index("╚═"), text.index("PROGRESS"))
                self.assertLess(text.index("PROGRESS"), text.index("└ kind"))
                self.assertLess(text.index("└ kind"), text.index("why:"))
                self.assertLess(text.index("why:"), text.index("next:"))
                grid = CellGrid(112, 60)
                pages._route_page(grid, ExplorerViewModel.from_mapping(vm), RID, 0, 111, 54, None, whole=False)      # Fleet's own page keeps its order
                whole = "\n".join("".join(cell[0] for cell in cells).rstrip() for cells in grid.cells)
                self.assertLess(whole.index("why:"), whole.index("PROGRESS"))
                self.assertLess(whole.index("PROGRESS"), whole.index("╔═"))

    def test_a_first_screen_that_throws_falls_back_to_the_panels_box(self):
        # BC-ROUTE-FIRST-SCREEN-THROW-FALLBACK-1119: `routeFirst` (host.cjs) takes `routePageFirst`'s exception and stands the panel's box.
        script = r"""
const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('node:assert/strict');
const src=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const a=src.indexOf('function routeFirst(t){'),b=src.indexOf('\n}\n',a);
if(a<0||b<0)throw new Error('routeFirst not found');
const out={};
for(const mode of ['throws','frame','none']){
  const ctx={routePopup:null,sid:'s',popupKeys:{ROUTE:'k'},panelModel:{route_read_at:7},cellWidth:null,displayConfiguration:{},terminalBackground:'',Math,Number,
    panelView:{routePageFirst:()=>{if(mode==='throws')throw new TypeError('(parts||[]).map is not a function');return mode==='frame'?{rows:[['FRAME']],hits:[],routes:['r']}:null;},
               routeFramed:()=>true,routesBody:()=>({rows:[['BOX']],hits:[],routes:['r']})}};
  vm.createContext(ctx);vm.runInContext(src.slice(a,b+2),ctx);
  out[mode]=ctx.routeFirst({width:120,open:'r',page:true}).rows[0][0];
}
assert.deepEqual(out,{throws:'BOX',frame:'FRAME',none:'BOX'});
console.log('ok');
"""
        done = subprocess.run([self.node, "-e", script, str(ROOT / "utilities/session-shell")], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr)

    def test_the_first_screen_notes_whether_it_showed_the_frame(self):
        # (review 3 of #1900, red 3: the page request takes `framed` from the first screen shown (`routeFirst`), never from the panel's values when sent)
        script = r"""
const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('node:assert/strict');
const src=fs.readFileSync(path.join(process.argv[1],'host.cjs'),'utf8');
const a=src.indexOf('function routeFirst(t){'),b=src.indexOf('\n}\n',a);
const run=(popup,framed,drawn)=>{
  const ctx={routePopup:popup,sid:'s',popupKeys:{ROUTE:'k'},panelModel:{},cellWidth:null,displayConfiguration:{},terminalBackground:'',Math,Number,
    panelView:{routePageFirst:()=>drawn?{rows:[[{text:'X'}]],hits:[],routes:['r']}:null,routeFramed:()=>framed,routesBody:()=>({rows:[[{text:'BOX'}]],hits:[],routes:['r']})}};
  vm.createContext(ctx);vm.runInContext(src.slice(a,b+2),ctx);return ctx.routeFirst({width:120,open:'r',page:true}).framed;};
const cached=first=>({sid:'s',width:120,open:'r',key:'k',page:true,rows:[[{text:first}]],hits:[],routes:['r'],at:1});
// (the page read last, shown again: its own order says, whatever the panel holds now)
assert.equal(run(cached('╔═ project/ › [e9] › 제목'),false,true),true);
assert.equal(run(cached('   todo:        a'),true,true),false);
// (drawn now from the panel's values: the frame, or the screen as before 11-27, or the route list)
assert.equal(run(null,true,true),true);
assert.equal(run(null,false,true),false);
assert.equal(run(null,true,false),false);
console.log('ok');
"""
        done = subprocess.run([self.node, "-e", script, str(ROOT / "utilities/session-shell")], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr)

    def test_the_band_the_panel_carries_is_bounded_and_text_only(self):
        from fleet import session_panel
        from fleet.explorer import live
        long = "x" * 500
        group = {"n": long, "st": long, "verdict": long, "kind": long, "lines": [[[long, long]] * 20] * 5, "title": [[long, long]] * 20}
        bands = live._panel_bands([group] * 6)
        self.assertEqual(len(bands), 6)
        self.assertEqual((len(bands[0]["n"]), len(bands[0]["st"]), len(bands[0]["verdict"]), len(bands[0]["kind"])), (64, 16, 16, 8))
        self.assertEqual((len(bands[0]["lines"]), len(bands[0]["lines"][0]), len(bands[0]["title"])), (2, 8, 10))
        self.assertEqual((len(bands[0]["lines"][0][0][0]), len(bands[0]["lines"][0][0][1])), (80, 24))
        # (a route of more legs than the panel carries has no band: its first screen is the panel's box, never another selection and layout)
        self.assertEqual(live._panel_bands([group] * 7), [])
        # (the producer: a value not of its shape is no value, never an exception: `lines: 6`, an object for a text, a key that is not a string)
        odd = live._panel_bands([{"kind": "leg", "n": {"bad": 1}, "st": "active", "lines": 6, "title": [[{"bad": "value"}, "head"], ["t", ("d", 0, 1)]],
                                  "detail": "not a list"}])
        self.assertEqual(odd, [{"n": "", "st": "active", "verdict": "", "kind": "leg", "lines": [], "title": [["t", ""]]}])
        staged = live._panel_bands([{"kind": "leg", "n": "leg 1", "st": "active", "detail": [{"n": {"bad": 1}, "st": "active"}, {"n": "b", "st": "todo"}]},
                                    {"kind": "leg", "n": "leg 2", "st": "todo", "detail": [{"group": (), "members": ("x", "y")}]}])
        self.assertEqual([band["stage"]["n"] for band in staged], ["", "None"])      # (an object is no name; a parallel pair is Fleet's own `None`)
        # (the panel's side: control characters are spaces, a piece keeps its own spaces, a value of another shape is no value)
        shown = session_panel.route_bands([{"n": "a\x1b[31mb", "st": "active", "lines": "not a list", "title": {"x": 1}},
                                           {"n": "c", "lines": [[["owner  ", "dim"], [{"bad": "value"}, "soft"], ["d\x07e", "dim"], "junk"]],
                                            "title": [["t\x00", "head"]], "stage": {"n": "s", "st": "active", "at": 2, "done": 9, "of": 3}},
                                           "junk"])
        self.assertEqual(shown[0]["lines"], [])
        self.assertEqual(shown[0]["title"], [])
        self.assertNotRegex(shown[0]["n"], r"[\x00-\x1f]")
        self.assertEqual(shown[1]["lines"], [[["owner  ", "dim"], ["d e", "dim"]]])
        self.assertEqual(shown[1]["title"], [["t ", "head"]])
        self.assertNotIn("stage", shown[1])          # (more done than there are: not a count)
        self.assertEqual(len(shown), 2)
        self.assertEqual(session_panel.route_bands(None), [])
        self.assertEqual(session_panel.route_bands([{"n": "x"}] * 7), [])
        # (and the drawing: an object where a piece's text stands draws nothing there)
        _vm, record = build("staged", 3)
        record["bands"][0]["lines"] = [[["owner  ", "dim"], [{"bad": "value"}, "soft"]]]
        got = self.draw([{"model": {"routes": panel_of(record), "styles": self.styles}, "id": RID, "columns": 112, "cols": 120, "windowRows": [46],
                          "read": ["x"]}])[0]["first"]
        owner = next(row for row in got if "owner" in row)
        self.assertNotIn("bad", owner)
        self.assertRegex(owner, r"│ owner +│")

    def test_a_page_that_does_not_fit_the_answer_is_the_page_as_before(self):
        # (review 2 of #1900, red 1 and 3: the frame closed early lost `next:` where the lines alone filled the answer, an answer past the bytes the host
        # reads never came, and a closed-early frame hid the running stages. A page that does not fit is the page as main draws it, cut to the host's bytes.)
        import session_shell
        path = "/very/long/fixture/path/" + "segment-name/" * 12 + "artifact.md"
        twenty = [["why", "the person asked for it", "info"]] + [["path", path, "info"]] * 18 + [["next", "wait for the stage", "info"]]
        short = [["why", "the person asked for it", "info"]] + [["path", "p%03d" % i, "info"] for i in range(148)] + [["next", "wait for the stage", "info"]]
        wide = [["why", "the person asked for it", "info"]] + [["path", "ก" * 165 + "%03d" % i, "info"] for i in range(18)] + [["next", "wait for the stage", "info"]]
        folded = [["why", "the person asked for it", "info"]] + [["path", "/" + "a" * 129, "info"]] * 13 + [["next", "wait for the stage", "info"]]
        cases = (("150 short lines, 39 stages, 80×24", 39, short, 72, {}), ("15 lines folded at 30 cells", 6, folded, 30, {}),
                 ("wide letters filling the answer, 45 stages", 45, wide, 192, {}), ("wide letters, 6 path lines, 37 stages", 37, wide[:7] + wide[-1:], 192, {}),
                 ("39 stages, the last two parallel, step-39 running", 39, twenty, 72, {"running": 38, "parallel": ((37, 38),)}))
        for label, n, lines, columns, extra in cases:
            with self.subTest(label):
                vm, _record = build_long(n, lines, **extra)
                fitted, end = drawn(vm, columns, True)
                self.assertTrue(end > session_shell.POPUP_ROWS or weight(fitted) > session_shell.POPUP_ANSWER_BYTES, (end, weight(fitted)))
                result, before = page_answer(vm, columns), drawn(vm, columns, False)[0]
                self.assertFalse(text_of(result)[0].startswith("╔═"), text_of(result)[0])
                # (main's answer whole: every row, style and press target; one past the held reader's 64 KiB the host reads the one-shot way)
                self.assertEqual((result["rows"], result["styles"], result["hits"]), (before["rows"], before["styles"], before["hits"]))
                self.assertTrue(any(row.strip().startswith("next:") for row in text_of(result)))
                self.assertLessEqual(len(result["rows"]), session_shell.POPUP_ROWS)

    def test_the_frame_stands_first_up_to_the_answers_rows_and_bytes(self):
        # (the edge: a page whose last row is the answer's last stands its frame first; one row more, or one line past the bytes, is the page as before)
        import session_shell
        line = lambda i: ["path", "p%03d" % i, "info"]
        lines = lambda k: [line(i) for i in range(k)]
        vm, _record = build_long(6, lines(1))
        k = session_shell.POPUP_ROWS - drawn(vm, 72, True)[1] + 1               # (a short line is one row)
        for count, framed in ((k, True), (k + 1, False)):
            with self.subTest(rows=count):
                vm, _record = build_long(6, lines(count))
                self.assertEqual(drawn(vm, 72, True)[1], session_shell.POPUP_ROWS + (0 if framed else 1))
                rows = read_page(vm, 72)
                self.assertEqual(rows[0].startswith("╔═"), framed)
                self.assertEqual(rows, text_of(drawn(vm, 72, framed)[0]))
                self.assertIn("path:        p%03d" % (count - 1), [row.strip() for row in rows])
                if framed:
                    self.assertEqual(rows[-1].strip(), "path:        p%03d" % (count - 1))      # (the whole page: its last line is the last row)
        wide = lambda i: ["path", "ก" * 165 + "%03d" % i, "info"]
        sized = lambda k: build_long(6, [wide(i) for i in range(k)])[0]
        low, high = 1, 120                     # (the most wide lines whose framed page weighs what the host reads)
        self.assertLessEqual(weight(drawn(sized(low), 192, True)[0]), session_shell.POPUP_ANSWER_BYTES)
        self.assertGreater(weight(drawn(sized(high), 192, True)[0]), session_shell.POPUP_ANSWER_BYTES)
        while high - low > 1:
            middle = (low + high) // 2
            low, high = (middle, high) if weight(drawn(sized(middle), 192, True)[0]) <= session_shell.POPUP_ANSWER_BYTES else (low, middle)
        for count, framed in ((low, True), (high, False)):
            with self.subTest(wide_lines=count):
                vm = sized(count)
                self.assertLess(drawn(vm, 192, True)[1], session_shell.POPUP_ROWS)       # (the rows fit: the bytes decide)
                result = page_answer(vm, 192)
                rows = text_of(result)
                self.assertEqual(rows[0].startswith("╔═"), framed)
                self.assertEqual(rows, text_of(drawn(vm, 192, framed)[0]))               # (the frame first, or main's answer whole)
                self.assertEqual(weight(result) <= session_shell.POPUP_ANSWER_BYTES, framed)

    def test_the_bands_stage_line_is_the_producers_count(self):
        # (review of #1900, red 2: the first screen counted again from the sixteen stages the panel holds, and stacked parallel pairs flat)
        for label, n, running, parallel in (("19, the 19th running", 19, 18, ()), ("9 with a parallel pair", 9, 4, ((3, 4),))):
            with self.subTest(label):
                vm, record = build_long(n, None, running=running, parallel=parallel)
                read = read_page(vm, 124)
                first = self.draw([{"model": {"routes": panel_of(record), "styles": self.styles}, "id": RID, "columns": 124, "cols": 132,
                                    "windowRows": [46], "read": read}])[0]["first"]
                line = lambda rows: next(row for row in rows if "│ stage  " in row)
                self.assertEqual(line(first), line(read))
                self.assertEqual(mask(first[:4]), mask(read[:4]))          # (the head and the band: every cell but the head's last word)
        # (seven legs handed to the drawing: no band of six with another one picked; the first screen as before, see the test above)

    def test_a_long_repository_name_stands_the_tag_where_the_reading_does(self):
        # (review of #1900, red 3: a 95-character repository name moved the tag 87 → 103 cells and lost its `/` on the first screen)
        repo = "r" + "e" * 93 + "o"
        for columns, cols in ((124, 132), (72, 80)):
            with self.subTest(columns=columns):
                vm, record = build_long(3, None, repo=repo)
                read = read_page(vm, columns)
                first = self.draw([{"model": {"routes": panel_of(record), "styles": self.styles}, "id": RID, "columns": columns, "cols": cols,
                                    "windowRows": [46], "read": read}])[0]["first"]
                self.assertEqual(mask(first[:1]), mask(read[:1]))
                if columns == 124:
                    self.assertIn(repo + "/ › [e9] › ", first[0])

    def test_the_stage_boxes_keep_the_colors_the_first_screen_drew_them_in(self):
        # (review of #1900, red 4: the state word of a box went from the dim gray (247) to the body color (253) in the popup's final colors)
        script = r"""
const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const view=require(path.join(process.argv[1],'panel-view.cjs'));
const c=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const model={routes:c.routes,styles:c.styles,colors:256};
const b=view.routePageFirst({model,id:c.id,columns:124});
const p=view.popup({cols:132,rows:46,kind:'route',title:[{text:'ROUTE'}],body:b.rows,sections:['ROUTE'],current:0,model,fixedTop:true});
// The final style of the cell a word starts in, on the first row of the popup that holds it with the other words given.
const at=(word,also)=>{for(const row of p.rows){const text=row.map(q=>q.text).join('');if(!also.test(text))continue;const i=text.lastIndexOf(word);if(i<0)continue;
  let x=0;for(const q of row){if(x<=i&&i<x+q.text.length){const {text:_t,...style}=q;return JSON.stringify(style);}x+=q.text.length;}}return null;};
const dim=at('time   ',/│ time /),body=at('since',/│ time +since/);
assert(dim&&body&&dim!==body,'the dim gray and the body color differ: '+dim+' '+body);
for(const word of ['PASS','running'])assert.equal(at(word,/┌─ (plan|execute) /),dim,word+' in a stage box');
assert.equal(at('✓',/┌─ plan /),at('✓',/┌─ plan /));
console.log('ok');
"""
        _vm, record = build("staged", 3)
        import session_shell
        from fleet.palette import style_components
        styles = {key: style_components(key, colors=256, no_color=False) for key in session_shell.PANEL_STYLE_KEYS}
        path = Path(self.tmp.name) / "colors.json"
        path.write_text(json.dumps({"routes": panel_of(record), "styles": styles, "id": RID}))
        done = subprocess.run([self.node, "-e", script, str(ROOT / "utilities/session-shell"), str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr)


def build_long(n, lines, *, running=None, parallel=(), repo="project"):
    """A staged route of ``n`` serial stages (each with a gate, a second round and words), its visibility ``lines`` (or two) and a task summary;
    ``parallel`` pairs of stage places run as a 2-way group."""
    from fleet.explorer import live
    running = min(2, n - 1) if running is None else running
    nodes = []
    for i in range(n):
        name = "step-%02d" % (i + 1)
        state = "done" if i < running else "active" if i == running else "todo"
        nodes.append({"id": name, "state": state, "harness": "claude", "model": "Opus 5.5", "effort": "high", "gate": "code-plan-review",
                      "says": "working on the change", "route_attempt_round": 2, "unit": "dev/backend" if state == "active" else "qa/" + name,
                      "write_scope": ["artifacts/%s.md" % name] if state == "done" else [], "gate_passed": state == "done",
                      "elapsed_min": max(1, 200 - 5 * i) if state != "todo" else None, "depends_on": [nodes[-1]["id"]] if nodes else []})
    for first, second in parallel:
        nodes[first]["parallel_group"] = nodes[second]["parallel_group"] = "pg%d" % first
        nodes[second]["depends_on"] = list(nodes[first]["depends_on"])
        for after in nodes[second + 1:second + 2]:
            after["depends_on"] = [nodes[first]["id"], nodes[second]["id"]]
    legs = [["code", "active", "%d/%d" % (running, n)]]
    route = {"id": RID, "repo": repo, "session_tag": "e9", "title": "긴 시험 route", "state": "working", "outcome": None, "result_at": None,
             "worker": ["worker", [["claude", "hb_claude"]], [["running 10m", "dim"]]], "age": "23m", "legs": legs,
             "stages": [live._stage_record(item) for item in nodes], "resume": None, "activity": "running", "harness": "claude",
             "model": ("Opus 5.5", None), "effort": "high", "mode": "dev", "intensity": "standard", "profile": "deep", "field": "code",
             "capability": "code", "shape": "staged", "worker_stage": nodes[running]["id"], "worker_unit": nodes[running]["unit"],
             "owner_gauge": [40, 0.2], "worker_contexts": [], "owner": None, "current": True,
             "visibility": {"schema": "route_visibility_v1", "route_id": RID, "summary": "ok", "next": "wait",
                            "lines": lines or [["why", "the person asked for it", "info"], ["next", "wait for the stage", "info"]]}}
    groups = live._node_groups(nodes, route, RID, legs, time.time(), running, "active")
    vm = json.loads(json.dumps({"repos": [{"id": repo, "name": repo}], "routes": [route], "nodes": {RID: groups},
                                "system": {"task_summary": {"by_route": {RID: {"todo": ["a"], "doing": ["b"], "done": ["c"]}}}},
                                "sessions": [{"tag": "e9", "repo_id": repo, "harness": "claude", "model": "Opus 5.5", "effort": "high", "context": 46,
                                              "window": 200000, "state": "working", "title": "이 창", "now": "그리는 중", "age": "23m"}]}, default=list))
    return vm, live._panel_route_record(route, {RID: groups})


def page_answer(vm, columns, framed=True):
    """The backend's whole answer for the route's page (`_panel_popup`), as the host reads it (``framed``: its first screen was the frame)."""
    import session_shell
    from fleet import session_tags
    from fleet.explorer.view_model import ExplorerViewModel
    held = ExplorerViewModel.from_mapping(vm)
    with mock.patch.object(session_tags, "assigned_tag", lambda harness, sid: "e9"), \
            mock.patch.object(session_shell, "registry_root", lambda: None), \
            mock.patch.object(session_shell, "_popup_view_model", lambda harness, sid, kind: held), \
            mock.patch.object(session_shell, "_paint_mode", lambda: (256, False)), \
            mock.patch.dict(session_shell._HELD_VIEW, clear=True):
        return session_shell._panel_popup({"harness": "claude", "sid": "sid"}, {"kind": "routes", "columns": columns, "open": RID, "page": True, "rows": 24,
                                                                       **({"framed": True} if framed else {})})


def drawn(vm, columns, frame_first):
    """``(answer, end)``: the route's page as `_route_page` draws it in either order into the popup's grid (`popup_rows`, its empty head gone),
    uncut by bytes, and the row under what it drew: what the backend's answer is compared with."""
    import session_shell
    from fleet.explorer import pages
    from fleet.explorer.grid import CellGrid
    from fleet.explorer.view_model import ExplorerViewModel
    from dataclasses import replace
    held = ExplorerViewModel.from_mapping(vm)
    grid = CellGrid(columns, session_shell.POPUP_ROWS + 8)
    end = pages._route_page(grid, replace(held, routes=tuple(held.routes)), RID, 0, columns - 1, grid.rows,
                            pages._SubsetState(None, "route:" + RID, RID, True), whole=True, frame_first=frame_first)
    rows, styles, hits = session_shell.popup_rows(grid, colors=256, no_color=False)
    while rows and not rows[0]:
        rows.pop(0)
        hits = [{**hit, "row": hit["row"] - 1} for hit in hits if hit["row"] > 0]
    # (the answer the backend writes, its time as wide as the backend's)
    return {"ok": True, "kind": "routes", "rows": rows, "styles": styles, "hits": hits, "routes": [RID], "page": True, "read_at": int(time.time())}, end


def weight(result):
    return len(json.dumps({"id": 0, **result}, ensure_ascii=False).encode())


if __name__ == "__main__":
    unittest.main()
