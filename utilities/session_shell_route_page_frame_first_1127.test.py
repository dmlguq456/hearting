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
    it has always said where the reading's head says `staged`).
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


def read_page(vm, columns):
    """The backend's reading of the route's page (`_panel_popup`) over the given collection: its rows as text."""
    import session_shell
    from fleet import session_tags
    from fleet.explorer.view_model import ExplorerViewModel
    held = ExplorerViewModel.from_mapping(vm)
    with mock.patch.object(session_tags, "assigned_tag", lambda harness, sid: "e9"), \
            mock.patch.object(session_shell, "registry_root", lambda: None), \
            mock.patch.object(session_shell, "_popup_view_model", lambda harness, sid, kind: held), \
            mock.patch.object(session_shell, "_paint_mode", lambda: (256, False)), \
            mock.patch.dict(session_shell._HELD_VIEW, clear=True):
        result = session_shell._panel_popup({"harness": "claude", "sid": "sid"}, {"kind": "routes", "columns": columns, "open": RID, "page": True, "rows": 40})
    assert result.get("ok") and result.get("page"), result
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

    def test_the_panel_without_a_band_keeps_its_box(self):
        # (an older backend's record has no band: the first screen is the panel's route box, `null` here)
        for form in FORMS:
            with self.subTest(form=form):
                _vm, record = build(form, 3)
                record.pop("bands")
                got = self.draw([{"model": {"routes": panel_of(record), "styles": self.styles}, "id": RID, "columns": 112, "cols": 120,
                                  "windowRows": [46], "read": ["x"]}])
                self.assertIsNone(got[0]["first"])

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
               routesBody:()=>({rows:[['BOX']],hits:[],routes:['r']})}};
  vm.createContext(ctx);vm.runInContext(src.slice(a,b+2),ctx);
  out[mode]=ctx.routeFirst({width:120,open:'r',page:true}).rows[0][0];
}
assert.deepEqual(out,{throws:'BOX',frame:'FRAME',none:'BOX'});
console.log('ok');
"""
        done = subprocess.run([self.node, "-e", script, str(ROOT / "utilities/session-shell")], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr)

    def test_the_band_the_panel_carries_is_bounded_and_text_only(self):
        from fleet import session_panel
        from fleet.explorer import live
        long = "x" * 500
        groups = [{"n": long, "st": long, "verdict": long, "kind": long, "lines": [[[long, long]] * 20] * 5, "title": [[long, long]] * 20}] * 9
        bands = live._panel_bands(groups)
        self.assertEqual(len(bands), 6)
        self.assertEqual((len(bands[0]["n"]), len(bands[0]["st"]), len(bands[0]["verdict"]), len(bands[0]["kind"])), (64, 16, 16, 8))
        self.assertEqual((len(bands[0]["lines"]), len(bands[0]["lines"][0]), len(bands[0]["title"])), (2, 8, 10))
        self.assertEqual((len(bands[0]["lines"][0][0][0]), len(bands[0]["lines"][0][0][1])), (80, 24))
        # (the panel's side: control characters are spaces, a value of another shape is no value, never an exception for the drawing)
        shown = session_panel.route_bands([{"n": "a\x1b[31mb", "st": "active", "lines": "not a list", "title": {"x": 1}},
                                           {"n": "c", "lines": [[["d\x07e", "dim"], "junk"]], "title": [["t\x00", "head"]]}, "junk"])
        self.assertEqual(shown[0]["lines"], [])
        self.assertEqual(shown[0]["title"], [])
        self.assertNotRegex(shown[0]["n"], r"[\x00-\x1f]")
        self.assertEqual(shown[1]["lines"], [[["d e", "dim"]]])
        self.assertEqual(shown[1]["title"], [["t ", "head"]])
        self.assertEqual(len(shown), 2)
        self.assertEqual(session_panel.route_bands(None), [])


if __name__ == "__main__":
    unittest.main()
