"""11-19 a press on a route opens a page that stands from the first moment: the same frame before and after its reading, to its content, held at the top.

The person: "그 라우트 눌렀을때 여전히 좀 느리긴하고, 그 대기 시간동안 그냥 라우트 목록이 뜨는데?? 그리고 로딩되서 스테이지 같은 기록이 뜰때 아래에 여백이 붕뜨면서 상하로 꽉차버리는 문제가 있어".

  * The popup's route page draws its frame first (the legs' band, the pressed leg's stage boxes), then the route row, then the lines about it; the frame ends one row
    under its content and a stage box is as tall as its value lines (`draw_route_detail(fit=True)`; Fleet's own screen is not changed).
  * The panel's `routePageFirst` draws that frame for a unit, a solo and a team route from the values the panel holds. The two drawings are one in every cell but the value
    cells (the border, the title, the names and the arrows stand where the reading puts them), at 80 and 120 columns, with one, three and six stages (six wrap to two rows).
  * The popup of a route's page keeps its top edge where the tallest popup's would be and grows downward only; a table and the other popups stay centered.
  * The form reads from the picture: the head's last word is the form (`unit`, `solo`, `team`), a dim line under it says how the form works (in the configured language,
    broken between words), a unit's stage box is titled by the unit's role, a solo's by `owner`; a route with no form has no line.
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
FORMS = ("unit", "solo", "team")
# The popup's inner width a window of 80 or 120 columns gives the route popup (`popupWidth`: cols-4 under 80, else min(196, cols-4); the rows' width is that less 4).
COLUMNS = {80: 72, 120: 112}

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
    const make=(parts,fixed)=>view.popup({cols:c.cols,rows,kind:'route',title:[{text:'ROUTE'}],body:parts,sections:['ROUTE'],current:0,model:{styles:{}},cellWidth:measure,fixedTop:fixed}).frame;
    const read=c.read.map(t=>[{text:t}]);
    tops[rows]={first:first?make(first.rows,true):null,read:make(read,true),firstCentered:first?make(first.rows,false):null};
  }
  return {first:first?first.rows.map(text):null,tops};
});
process.stdout.write(JSON.stringify(out));
"""

LABELS = r"(by|time|rounds|gate|out|unit|says)"


def mask(rows):
    """The frame's rows with the cells a value can stand in blanked: a box's value after its label, the leg band's owner line."""
    out = []
    for row in rows:
        row = re.sub(r"│ " + LABELS + r"([^│]*)│",
                     lambda m: "│ " + m.group(1) + m.group(2)[:7 - len(m.group(1))] + " " * (len(m.group(2)) - (7 - len(m.group(1)))) + "│", row)
        row = re.sub(r"│ owner  ([^│]*)│", lambda m: "│ owner  " + " " * len(m.group(1)) + "│", row)
        out.append(row)
    return out


def frame_of(rows):
    """The rows from the top rule to the frame's bottom rule."""
    bottom = next((i for i, row in enumerate(rows) if re.search(r"╚═", row)), len(rows) - 1)     # (a drawing with no bottom rule: all of it)
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
    """A route as the live reader hands it over (Fleet's route and its nodes) and the panel's values for it (what `session_panel.presentation` makes)."""
    from fleet import session_panel
    from fleet.explorer import live

    def node(i, name, after=None):
        state = "done" if n > 1 and i < 2 else "active" if i == min(2, n - 1) else "todo"
        return {"id": name, "state": state, "harness": "claude", "model": "Opus 5.5", "effort": "high",
                "unit": "dev/backend" if state == "active" else "qa/" + name, "write_scope": ["artifacts/%s.md" % name] if state == "done" else [],
                "gate_passed": state == "done", "elapsed_min": 30 - 10 * i if state != "todo" else None, "depends_on": [after] if after else []}
    names = [NAMES[i] if form == "team" or i else ("one-shot" if form == "solo" else "unit") for i in range(n)]
    if form == "team":
        view_nodes = [node(i, names[i], names[i - 1] if i else None) for i in range(n)]
        legs = [["frame", "done", None], ["code", "active", "%d/%d" % (min(2, n), n)]]
        capability, shape = "code", "staged"
    else:
        view_nodes = [node(i, names[i], names[i - 1] if i else None) for i in range(n)]
        view_nodes[0].update(state="active", gate_passed=False, elapsed_min=3, depends_on=[], write_scope=[],
                             unit="_kernel/owner" if form == "solo" else "qa/code-review")
        legs = [[("code" if form == "solo" else "unit"), "active", "0/%d" % n]]
        capability, shape = ("code" if form == "solo" else "unit"), "one-shot-owner"
    stages = [live._stage_record(item) for item in view_nodes]
    route = {"id": RID, "repo": "project", "session_tag": "e9", "title": "route를 누르면 뜨는 그림", "state": "working", "outcome": None, "result_at": None,
             "worker": ["owner" if form == "solo" else "worker", [["claude", "hb_claude"]], [["running 10m", "dim"]]], "age": "23m", "legs": legs, "stages": stages,
             "resume": None, "activity": "running", "harness": "claude", "model": ("Opus 5.5", None), "effort": "high", "mode": None if form == "unit" else "dev",
             "intensity": "quick" if form != "team" else "standard", "profile": "deep", "field": "code", "capability": capability, "shape": shape,
             "worker_stage": view_nodes[0]["id"] if form != "team" else "execute", "worker_unit": view_nodes[0]["unit"], "owner_gauge": [40, 0.2],
             "worker_contexts": [], "owner": None,
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
    panel = session_panel.presentation({}, {"route_panels": {"claude\0sid": [record]}}, "claude", "sid", 40)["routes"]
    vm = json.loads(json.dumps({"repos": [{"id": "project", "name": "project"}], "system": {}, "routes": [route], "nodes": {RID: groups},
                                "sessions": [{"tag": "e9", "repo_id": "project", "harness": "claude", "model": "Opus 5.5", "effort": "high", "context": 46,
                                              "window": 200000, "state": "working", "title": "이 창", "now": "그리는 중", "age": "23m"}]}, default=list))
    return vm, panel


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


class RoutePressPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is needed for the panel's drawing")
        cls.tmp = tempfile.TemporaryDirectory(prefix="rpp1119-", dir="/tmp")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.env_patch = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "ko"})
        cls.env_patch.start()
        cls.addClassCleanup(cls.env_patch.stop)
        from fleet.palette import style_components
        cls.styles = {key: style_components(key, colors=256, no_color=False) for key in KEYS}
        cls.pictures = {}

    def picture(self, form, n, window, lack=False, language="ko"):
        """``(first rows, read rows, tops)`` for one route: the panel's first screen, the backend's reading, and where the popup's top edge stands."""
        key = (form, n, window, lack, language)
        if key not in self.pictures:
            with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": language}):
                vm, panel = build(form, n, lack)
                read = read_page(vm, COLUMNS[window])
            case = {"model": {"routes": panel, "styles": self.styles, "colors": 256}, "id": RID, "columns": COLUMNS[window], "cols": window,
                    "windowRows": [24, 40], "read": read}
            path = Path(self.tmp.name) / ("case-%d.json" % len(self.pictures))
            path.write_text(json.dumps([case]))
            done = subprocess.run([self.node, "-e", NODE, str(ROOT / "utilities/session-shell"), str(path)], capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            got = json.loads(done.stdout)[0]
            self.pictures[key] = (got["first"], read, got["tops"])
        return self.pictures[key]

    def test_the_first_screen_is_the_readings_frame_in_every_cell_but_the_values(self):
        for form in FORMS:
            for window in (80, 120):
                for n in (1, 3, 6):
                    for lack in (False, True):
                        with self.subTest(form=form, window=window, stages=n, lack=lack):
                            first, read, _tops = self.picture(form, n, window, lack)
                            self.assertIsNotNone(first, "no first screen for the %s route" % form)
                            self.assertIsNone(same_cells(first, read))

    def test_the_cells_comparison_sees_a_moved_cell(self):
        first, read, _tops = self.picture("team", 6, 120)
        moved = [row.replace("┌─ plan ", "┌── plan ", 1) for row in first]
        self.assertIn("differs", same_cells(moved, read) or "")
        shorter = first[:-1] + ["╚" + "═" * 5 + "╝"]
        self.assertIsNotNone(same_cells(first[:-3] + shorter[-1:], read))

    def test_the_popups_top_edge_is_the_same_first_and_read_and_grows_downward_only(self):
        for form in FORMS:
            for window in (80, 120):
                for n in (1, 6):
                    with self.subTest(form=form, window=window, stages=n):
                        _first, _read, tops = self.picture(form, n, window)
                        for rows in ("24", "40"):
                            pair = tops[rows]
                            self.assertEqual(pair["first"]["top"], pair["read"]["top"], pair)
                            self.assertLessEqual(pair["first"]["height"], pair["read"]["height"])
        # (a table or any other popup stays centered: the same short body would stand lower, not at the top edge)
        _first, _read, tops = self.picture("solo", 1, 120)
        self.assertGreater(tops["40"]["firstCentered"]["top"], tops["40"]["first"]["top"])

    def test_the_frame_ends_one_row_under_its_content_and_a_box_is_as_tall_as_its_lines(self):
        for form in FORMS:
            for window in (80, 120):
                for n in (1, 3, 6):
                    with self.subTest(form=form, window=window, stages=n):
                        first, read, _tops = self.picture(form, n, window)
                        for rows in (first, read):
                            frame = frame_of(rows)
                            last_box = max(i for i, row in enumerate(frame) if "│ └" in row)
                            # (one blank row under the last box, then the frame's bottom rule; nothing stretched)
                            self.assertEqual(len(frame) - 1 - last_box, 2, "\n".join(frame))
                            self.assertTrue(re.fullmatch(r"║ │ +│ ║", frame[-2]), frame[-2])
                            tops = [i for i, row in enumerate(frame) if re.search(r"│ ┌─ ", row)]
                            for top in tops:
                                bottom = next(i for i in range(top, len(frame)) if "│ └" in frame[i])
                                filled = [row for row in frame[top + 1:bottom] if re.search(r"│ (by|time|rounds|gate|out|unit|says) ", row)]
                                self.assertEqual(bottom - top - 1, len(filled), "an empty row in a box:\n" + "\n".join(frame[top:bottom + 1]))

    def test_the_form_reads_from_the_picture(self):
        notes = {"ko": {"unit": "unit: 작업자 하나가 한 가지 일만 하고 보고서를 돌려줍니다. 파일은 고치지 않습니다.", "solo": "solo: owner 하나가 처음부터 끝까지 혼자 합니다.",
                        "team": "team: owner가 단계마다 작업자에게 맡깁니다."},
                 "en": {"unit": "unit: one worker does one job and hands back a report. It changes no file.", "solo": "solo: one owner does the whole job alone.",
                        "team": "team: the owner hands each stage to a worker."}}
        for language in ("ko", "en"):
            for form in FORMS:
                for window in (80, 120):
                    with self.subTest(language=language, form=form, window=window):
                        first, read, _tops = self.picture(form, 1, window, language=language)
                        for rows in (first, read):
                            self.assertRegex(rows[0], r"^╔═ .* · %s ═+╗$" % form)
                            self.assertNotIn("staged", rows[0])
                            self.assertNotIn("one-shot-owner", rows[0])
                            # the dim line under the head: the whole sentence, broken between words only
                            lines = []
                            for row in rows[1:3]:
                                m = re.fullmatch(r"║  (.+?) +║", row)
                                if m and not row.startswith("║  ┌"):
                                    lines.append(m.group(1))
                            self.assertEqual(" ".join(lines), notes[language][form], rows[:4])
                            self.assertTrue(lines[0].startswith(form + ":"), lines)
        # (the unit's line wraps at 80 columns; its words stay whole)
        first, _read, _tops = self.picture("unit", 1, 80)
        self.assertTrue(re.fullmatch(r"║  파일은 고치지 않습니다\. +║", first[2]), first[:4])
        # unit: the box is the unit's role, the band has no owner line; solo: the box is the owner itself; team: the stage names
        first, read, _tops = self.picture("unit", 1, 120)
        for rows in (first, read):
            self.assertTrue(any("┌─ code-review " in row for row in rows), rows)
            self.assertFalse(any("┌─ unit " in row for row in rows), rows)
            self.assertFalse(any(re.search(r"│ owner  ", row) for row in rows), rows)
        first, read, _tops = self.picture("solo", 1, 120)
        for rows in (first, read):
            self.assertTrue(any("┌─ owner " in row for row in rows), rows)
            self.assertFalse(any("one-shot" in row for row in frame_of(rows)), rows)
        first, read, _tops = self.picture("team", 6, 120)
        for rows in (first, read):
            for name in NAMES:
                self.assertTrue(any("┌─ %s " % name in row for row in rows), (name, rows))
            self.assertFalse(any("┌─ owner " in row for row in rows))

    def test_a_route_with_no_form_has_no_line_and_the_panel_without_a_band_keeps_its_box(self):
        from fleet.explorer import pages
        from fleet.explorer.grid import CellGrid
        from fleet.explorer.view_model import ExplorerViewModel
        vm, _panel = build("team", 3)
        vm["routes"][0].update(capability="frame", shape="framed")
        held = ExplorerViewModel.from_mapping(vm)
        grid = CellGrid(112, 60)
        pages.draw_route_detail(grid, held, held.routes[0], 0, 110, 0, fit=True)
        rows = ["".join(cell[0] for cell in cells).rstrip() for cells in grid.cells]
        self.assertTrue(re.fullmatch(r"╔═ .*  23m · 2 legs ═+╗", rows[0]), rows[0])   # (no word: a frame has no form)
        self.assertRegex(rows[1], r"^║  ┌─ leg ")                                       # (and no line: the band follows the head at once)
        # (an older backend's row has no band: the first screen is the panel's route box, `null` here)
        vm, panel = build("team", 3)
        for route in panel:
            route.pop("bands", None)
        case = {"model": {"routes": panel, "styles": self.styles}, "id": RID, "columns": 112, "cols": 120, "windowRows": [40], "read": ["x"]}
        path = Path(self.tmp.name) / "no-bands.json"
        path.write_text(json.dumps([case]))
        done = subprocess.run([self.node, "-e", NODE, str(ROOT / "utilities/session-shell"), str(path)], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(json.loads(done.stdout)[0]["first"])

    def test_the_page_order_is_the_frame_then_the_route_row_then_the_lines_for_a_popup_only(self):
        from fleet.explorer import pages
        from fleet.explorer.grid import CellGrid
        from fleet.explorer.view_model import ExplorerViewModel
        vm, _panel = build("team", 3)
        vm["system"] = {}
        popup = read_page(vm, 112)
        text = "\n".join(popup)
        self.assertLess(text.index("╔═"), text.index("PROGRESS"))
        self.assertLess(text.index("PROGRESS"), text.index("why:"))
        self.assertTrue(popup[0].startswith("╔═"), popup[0])          # (the frame is the first row: no empty rows above it)
        held = ExplorerViewModel.from_mapping(vm)
        grid = CellGrid(112, 60)
        pages._route_page(grid, held, RID, 0, 111, 54, None, whole=False)      # Fleet's own page keeps its order
        whole = "\n".join("".join(cell[0] for cell in cells).rstrip() for cells in grid.cells)
        self.assertLess(whole.index("why:"), whole.index("PROGRESS"))
        self.assertLess(whole.index("PROGRESS"), whole.index("╔═"))

    def test_a_solo_routes_owner_reads_owner_wherever_fleet_marks_its_doer_a_worker(self):
        from fleet import session_line
        solo = {"worker": ["owner", [["claude", "hb_claude"]], []], "doer": {"harness": "claude", "worker": True}}
        self.assertEqual(session_line.route_doer(solo, [], None, None)["role"], "owner")
        staged = {"worker": ["worker", [["claude", "hb_claude"]], []], "doer": {"harness": "claude", "worker": True}}
        self.assertEqual(session_line.route_doer(staged, [], None, None)["role"], "worker")
        frame = {"worker": ["frame", [["claude", "hb_claude"]], []], "doer": {"harness": "claude", "worker": True}}
        self.assertEqual(session_line.route_doer(frame, [], None, None)["role"], "frame")


if __name__ == "__main__":
    unittest.main()
