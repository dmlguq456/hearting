"""11-17 (the person: route row sketch 5): Fleet's route cell reads the shell panel's grammar, and the panel gets what it needs.

Fleet's ROUTES row (the ROUTE popup, the session page and OVERVIEW draw the same cell) reads `frame ✓ → code(standard) ·
3/6: execute` for a route that continues a frame, `frame → ??` for a frame route, `code(quick)` for a route its owner
does alone. The kind is the one bold word, the stage name the one thing that blinks (no `●` after it). The backend hands the
panel the intensity and the chain of kinds the route already carries, and a frame route's two legs as its live workers.
Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

from tools.fleet import session_line, session_panel
from tools.fleet.explorer import live, tables
from tools.fleet.explorer.grid import CellGrid
from tools.fleet.palette import _A_B, _HUE_OF

NAMES = ["plan", "plan-check", "execute", "impl-review", "test", "report"]


def stages(at=2, mark="active", count=6):
    return [{"name": NAMES[i], "st": "done" if i < at else mark if i == at else "todo"} for i in range(count)]


def staged(at=2, mark="active", chain=True, intensity="standard"):
    listed = stages(at, mark)
    legs = ([("frame", "done", None)] if chain else []) + [("code", "active", f"{at}/6")]
    return {"state": "working", "shape": "staged", "capability": "code", "intensity": intensity, "stages": listed,
            "legs": tuple(legs)}


def solo(**extra):
    return {"state": "working", "shape": "one-shot-owner", "capability": "code", "intensity": "quick",
            "stages": [{"name": "one-shot", "st": "active"}], "legs": (("code", "active", None),), **extra}


def frame(status="active", state="working"):
    return {"state": state, "shape": "framed", "capability": "frame", "stages": [{"name": "frame", "st": "active"}],
            "legs": (("frame", status, None),)}


def cell(route, width, beat=1):
    grid = CellGrid(width, 1)
    grid.beat = beat
    tables._draw_route_progress(grid, 0, width, 0, route, card=False)
    cells = grid.cells[0]
    return "".join(c[0] for c in cells).rstrip(), cells


def bold(key):
    return bool(_HUE_OF.get(key, ("d", 0))[1] & _A_B)


class FleetRouteCell(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_route_that_continues_a_frame_reads_the_chain_kind_intensity_and_stage(self):
        text, _ = cell(staged(), 70)
        # 11-19: the bracket says the form and the intensity (`staged·standard`), so the stage line no longer leads with `staged`
        self.assertEqual(text, "frame ✓ → code(team·standard) · 3/6: execute")
        self.assertEqual(cell(staged(chain=False), 70)[0], "code(team·standard) · 3/6: execute")
        self.assertEqual(cell(staged(at=1, mark="failed"), 70)[0], "frame ✓ → code(team·standard) · 2/6: plan-check ✕")
        for old in ("›", "execute ●", "3/6 execute", "staged"):
            self.assertNotIn(old, text)
        # Several kinds in one plan (what is behind is dim with ✓, this one bold).
        route = staged()
        route["legs"] = (("research", "done", None), ("draft", "done", None), ("code", "active", "2/6"))
        self.assertEqual(cell(route, 80)[0], "research ✓ → draft ✓ → code(team·standard) · 3/6: execute")

    def test_short_of_cells_the_chain_goes_then_the_intensity_the_bracket_the_kind_then_the_position(self):
        # 11-19: the intensity (`·standard`) goes first in the bracket, then the bracket and the chain; `staged` is never written before the position
        # (11-19: the form `team` is in the bracket)
        want = [(46, "frame ✓ → code(team·standard) · 3/6: execute"), (40, "code(team·standard) · 3/6: execute"),
                (34, "code(team·standard) · 3/6: execute"), (33, "code(team) · 3/6: execute"), (28, "code(team) · 3/6: execute"), (24, "code · 3/6: execute"), (16, "3/6: execute")]
        for width, expect in want:
            self.assertEqual(cell(staged(), width)[0], expect, width)
        for width in range(14, 70):                     # the stage stays at every width
            self.assertIn("execute", cell(staged(), width)[0], width)

    def test_the_kind_is_the_one_bold_word_and_the_stage_name_the_one_thing_that_blinks(self):
        text, lit = cell(staged(), 70, beat=1)
        _text, dark = cell(staged(), 70, beat=0)
        row = "".join(c[0] for c in lit)
        start = row.index("code")
        self.assertTrue(bold(lit[start][1]), lit[start])
        for offset, ch in enumerate(row):
            if start <= offset < start + 4:
                continue
            self.assertFalse(bold(lit[offset][1]), (offset, ch, lit[offset]))
        at = row.index("execute")
        self.assertNotEqual(lit[at][1], dark[at][1])                        # the stage name changes on the beat
        self.assertEqual([c for i, c in enumerate(lit) if not at <= i < at + 7],
                         [c for i, c in enumerate(dark) if not at <= i < at + 7])    # nothing else does
        self.assertNotIn("●", row)

    def test_a_frame_route_reads_frame_arrow_and_two_question_marks(self):
        self.assertEqual(cell(frame(), 60)[0], "frame → ??  ∥ frame 2-way")
        self.assertEqual(cell(frame("done", "idle"), 80)[0], "frame → ?? · waiting for direction  ∥ frame 2-way")
        self.assertEqual(cell(frame("done", "working"), 80)[0], "frame ✓ → ??  (frame ●)  ∥ frame 2-way")
        self.assertEqual(cell(frame("failed", "dead"), 60)[0], "frame ✕  ∥ frame 2-way")
        for width in range(9, 30):                       # never `frame →`
            self.assertNotRegex(cell(frame(), width)[0], r"→$", width)

    def test_a_route_its_owner_does_alone_reads_its_kind_and_intensity(self):
        self.assertEqual(cell(solo(), 40)[0], "code(solo·quick)")          # (11-19: before `code(quick)`)
        self.assertEqual(cell(solo(), 14)[0], "code(solo)")                # the intensity goes first
        chained = solo(legs=(("frame", "done", None), ("code", "active", None)))
        self.assertEqual(cell(chained, 40)[0], "frame ✓ → code(solo·quick)")

    def test_every_route_with_a_stage_list_reads_the_stage_line_with_its_own_shape_word(self):
        # A research route (map-reduce), refine, ship and spec (transactional-owner) and a resumed route read as a staged one does:
        # kind(intensity), the shape's own word (never translated), the position and the blinking stage; the old `(a ✓ › b ●)` is gone.
        three = [{"name": "search", "st": "done"}, {"name": "analyze", "st": "active"}, {"name": "report", "st": "todo"}]
        route = lambda shape, **kw: {"state": "working", "shape": shape, "capability": "research", "intensity": "standard",
                                     "stages": three, "legs": (("research", "active", None),), **kw}
        self.assertEqual(cell(route("map-reduce"), 70)[0], "research(team·standard) · map-reduce 2/3: analyze")      # (11-19, correction 4: a team form)
        self.assertEqual(cell({**route("transactional-owner"), "capability": "refine"}, 70)[0],
                         "refine(team·standard) · transactional-owner 2/3: analyze")
        resumed = route("resource+verification", resume="2m", legs=(("code", "done", None), ("code", "active", None)))
        self.assertEqual(cell(resumed, 90)[0], "code ✓ → research(standard) · resource+verification 2/3: analyze  resume 2m")
        self.assertEqual(cell(route(""), 70)[0], "research(standard) · 2/3: analyze")      # no shape named: the position and the stage
        self.assertEqual(cell(route("unknown"), 70)[0], "research(standard) · 2/3: analyze")
        for old in ("›", "analyze ●", "search ✓"):
            self.assertNotIn(old, cell(route("map-reduce"), 70)[0])
        for width in range(14, 70):                      # the stage stays at every width
            self.assertIn("analyze", cell(route("map-reduce"), width)[0], width)
        _text, lit = cell(route("map-reduce"), 70, beat=1)
        _text, dark = cell(route("map-reduce"), 70, beat=0)
        at = "".join(c[0] for c in lit).index("analyze")
        self.assertNotEqual(lit[at][1], dark[at][1])      # the stage name blinks, nothing else does
        self.assertEqual([c for i, c in enumerate(lit) if not at <= i < at + 7], [c for i, c in enumerate(dark) if not at <= i < at + 7])

    def test_a_one_step_rows_tool_follows_its_kind(self):
        grid = CellGrid(60, 1)
        route = solo(doer={"tool": "Bash", "target": "tools/run-tests.py"})
        self.assertTrue(tables._draw_route_now(grid, 0, 60, 0, route, tables.route_now_texts(route)))
        self.assertTrue("".join(c[0] for c in grid.cells[0]).startswith("code(solo·quick) Bash tools/run-tests.py"))

    def test_every_fleet_place_draws_the_one_cell(self):
        import inspect
        self.assertIn("_draw_route_progress(", inspect.getsource(tables.draw_route_row))
        self.assertIn("_draw_route_progress(", inspect.getsource(tables._draw_route_line))


class PanelGetsWhatItNeeds(unittest.TestCase):
    def test_the_panels_route_row_carries_the_intensity_and_the_chain(self):
        shown = session_panel.route_kind({"capability": "code", "mode": "dev", "intensity": "standard", "legs": 2,
                                          "chain": [["frame", "done"], ["code", "active"]]})
        self.assertEqual(shown["intensity"], "standard")
        self.assertEqual(shown["chain"], [{"name": "frame", "state": "done"}, {"name": "code", "state": "active"}])
        bare = session_panel.route_kind({"capability": "code"})
        self.assertNotIn("intensity", bare)
        self.assertNotIn("chain", bare)

    def test_a_frame_routes_two_legs_are_its_live_workers_with_no_owner(self):
        job = lambda **kw: SimpleNamespace(key=kw.get("attempt_id"), **kw)
        one = job(attempt_id="f1", depth=1, route_node="frame", worker_type="frame", liveness="working", harness="claude",
                  model="opus", ctx_pct=18, elapsed_min=4.0)
        two = job(attempt_id="f2", depth=1, route_node="frame-alternative", worker_type="frame", liveness="working",
                  harness="codex", model="gpt-5.5", ctx_pct=11, elapsed_min=4.0)
        contexts = [{"stage": "frame", "harness": "claude", "model": live._model_pair("opus")[0], "ctx_pct": 18.0, "attempt": "f1"},
                    {"stage": "frame-alternative", "harness": "codex", "model": live._model_pair("gpt-5.5")[0], "ctx_pct": 11.0,
                     "attempt": "f2"}]
        route = {"id": "rt", "state": "working", "activity": "running", "current": True, "worker_stage": "frame"}
        panels = {"claude\0s": [{"id": "rt", "state": "working", "worker_stage": "frame", "worker_contexts": contexts}]}
        with mock.patch.object(live, "_collect_routes", return_value={"rt": {"jobs": [one, two], "record": {}}}):
            session_line.route_details(object(), panels, {"rt": route}, now=10_000.0)
        row = panels["claude\0s"][0]
        self.assertEqual([(w["role"], w["stage"]) for w in row["workers_live"]],
                         [("frame", "frame"), ("frame", "frame-alternative")])
        self.assertNotIn("owner_doer", row)
        # A worker (not a frame leg) leading a route with no owner still lists nothing.
        self.assertEqual(session_line.route_tree({}, [], None, {"worker": True}, live._get, live, 0.0), {})


if __name__ == "__main__":
    unittest.main()
