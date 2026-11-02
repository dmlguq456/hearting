"""Repository grouping is drawn once on the group rule; OVERVIEW route summaries name the repo on every row."""
import unittest
from dataclasses import replace

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import CellGrid, cell_width
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.tables import _draw_route_line, _route_line_repo_width
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import explorer_fixture as ef


LONG = "fleet-main-history-opencode-ctx-followup-1029"
CJK = "음성인식-실험-저장소"


def model(routes=None):
    sessions = [{"tag": tag, "repo": repo, "repo_id": repo, "harness": "codex",
                 "state": "working", "title": "selected work", "now": "current task",
                 "age": "3m", "context": 50, "window": 1}
                for tag, repo in (("a1", LONG), ("b2", CJK))]
    if routes is None:
        routes = [{"id": "route-" + s["tag"], "repo": s["repo"], "session_tag": s["tag"],
                   "state": "working", "harness": "codex", "title": "route work",
                   "capability": "code", "age": "3m", "stages": [{"name": "execute", "st": "active"}],
                   "worker": ["worker", [["codex", "hb_codex"]], [["running execute", "g_spin"]]]}
                  for s in sessions]
    return ExplorerViewModel.from_mapping({
        "repos": [{"id": name, "name": name} for name in (LONG, CJK)],
        "sessions": sessions, "routes": routes})


def _pane_rows(frame, cols):
    return ["".join(cell[0] for cell in row[34:]) for row in frame.cells[:min(len(frame.cells), 45)]]


def _cell_column(frame, y, glyph):
    return next(index for index, cell in enumerate(frame.cells[y]) if cell[0] == glyph)


class RepoColumnTests(unittest.TestCase):
    def test_compact_repo_route_keeps_open_link_clear_of_worker_identity(self):
        from tools.fleet.tests.test_explorer_demo import NamelessRepos
        from unittest import mock
        with mock.patch("time.time", return_value=1700000000.0):
            NamelessRepos.setUpClass()
            models = (ef.demo_model(), NamelessRepos.vm)
        for vm in models:
            route = next(r for r in vm.routes if r.get("repo") == "lab-project")
            state = NavState(scope="repo:lab-project",
                             folds=(("ropen:repo:lab-project", route["id"]),))
            frame, trace = ef.trace_draw(draw, vm, state, 80, 45)
            self.assertEqual(ef.frame_violations(frame, trace, 80, 45), [])
            link = next(h for h in frame.hits if h.action == ("route", route["id"]))
            line = frame.text_rows()[link.y]
            self.assertEqual(line[link.x0:link.x1].strip(), "open ▸")
            self.assertTrue(line[link.x0].isspace())  # the hit includes the link's leading gap
            self.assertTrue(any(h.action == ("fold", "ropen:repo:lab-project", route["id"])
                                for h in frame.hits))

    def test_header_has_no_repo_column_and_the_rule_names_each_group_once(self):
        vm = model()
        for cols in (80, 100, 140, 180, 240, 320):
            for view, slot in (("OVERVIEW", "sopen:OVERVIEW"), ("ROUTES", "ropen:ROUTES")):
                with self.subTest(cols=cols, view=view):
                    state = NavState(view=view)
                    frame, trace = ef.trace_draw(draw, vm, state, cols, 60)
                    self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                    pane = _pane_rows(frame, cols)
                    header = next(line for line in pane if "ID" in line and "HARNESS" in line)
                    self.assertNotIn("REPO", header)
                    for repo in (LONG, CJK):
                        prefix = repo[:8]
                        rule_rows = [line for line in pane
                                     if line.startswith(" ┈ ") and prefix in line]
                        self.assertEqual(len(rule_rows), 1, (view, cols, repo, rule_rows))
                    action = ("fold", slot, "a1" if view == "OVERVIEW" else "route-a1")
                    hit = next(hit for hit in frame.hits if hit.action == action)
                    row = "".join(cell[0] for cell in frame.cells[hit.y][34:])
                    self.assertNotIn(LONG, row)
                    self.assertNotIn(CJK, row)
                    selected, _effects = reduce(state, {"type": "click", "action": hit.action}, frame)
                    self.assertEqual(selected.opened_for(slot), action[2])
                    unfolded, trace = ef.trace_draw(draw, vm, selected, cols, 60)
                    self.assertEqual(ef.frame_violations(unfolded, trace, cols, 60), [])
                    self.assertTrue(any(h.action == action for h in unfolded.hits))
                    closed, _effects = reduce(selected, {"type": "click", "action": hit.action}, unfolded)
                    self.assertEqual(closed.opened_for(slot), "")

    def test_route_supports_long_and_cjk_group_names(self):
        vm = model(routes=[
            {"id": "route-" + tag, "repo": repo, "session_tag": tag, "state": "working",
             "harness": "codex", "title": "route work", "capability": "code",
             "age": "3m", "stages": [{"name": "execute", "st": "active"}],
             "worker": ["worker", [["codex", "hb_codex"]], [["running execute", "g_spin"]]]}
            for tag, repo in (("a1", LONG), ("b2", CJK))])
        for cols in (80, 100, 140, 240, 320):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="ROUTES"), cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                pane = _pane_rows(frame, cols)
                for repo in (LONG, CJK):
                    self.assertEqual(sum(repo[:8] in line and line.startswith(" ┈ ")
                                         for line in pane), 1)
                for tag in ("a1", "b2"):
                    self.assertTrue(any(f"[{tag}]" in line for line in pane))

    def test_unknown_repo_does_not_borrow_a_display_name(self):
        vm = model()
        unknown = replace(vm, sessions=tuple(dict(s, repo="", repo_id="") for s in vm.sessions),
                          routes=tuple(dict(r, repo="") for r in vm.routes),
                          repos=({"id": "", "name": ""},))
        for view in ("OVERVIEW", "ROUTES"):
            text = "\n".join(draw(unknown, NavState(view=view), 240, 60).text_rows())
            self.assertNotIn(LONG, text)
            self.assertNotIn(CJK, text)


class RouteSummaryRepoRule(unittest.TestCase):
    def test_shared_width_keeps_badges_aligned_and_names_the_repo_on_every_row(self):
        vm = model(routes=[
            {"id": "one", "repo": LONG, "session_tag": "a1", "state": "working", "harness": "codex",
             "title": "first work", "capability": "code", "age": "3m"},
            {"id": "two", "repo": LONG, "session_tag": "b2", "state": "working", "harness": "codex",
             "title": "second work", "capability": "code", "age": "4m"},
            {"id": "three", "repo": CJK, "session_tag": "a1", "state": "working", "harness": "codex",
             "title": "third work", "capability": "code", "age": "5m"},
        ])
        for cols in (100, 140, 180):
            with self.subTest(cols=cols):
                frame = draw(vm, NavState(view="OVERVIEW"), cols, 60)
                rows = []
                for y, row in enumerate(frame.cells):
                    pane = "".join(cell[0] for cell in row[34:])
                    if any(title in pane for title in ("first w", "second w", "third w")):
                        rows.append((y, pane))
                self.assertEqual(len(rows), 3, rows)
                long_rows = [row for _y, row in rows if LONG[:12] in row]
                cjk_rows = [row for _y, row in rows if CJK[:4] in row]
                # Every OVERVIEW ROUTES row names its own repo, not only the first of a run.
                self.assertEqual(len(long_rows), 2)
                self.assertEqual(len(cjk_rows), 1)
                self.assertTrue(all(LONG[:12] in row for _y, row in rows if "[b2]" in row))
                first_two = [y for y, pane in rows if LONG[:12] in pane][:2]
                self.assertEqual(_cell_column(frame, first_two[0], "["),
                                 _cell_column(frame, first_two[1], "["))

    def test_repo_width_is_shared_across_the_drawn_route_set(self):
        routes = [{"id": str(index), "repo": LONG if index < 2 else CJK,
                   "session_tag": f"t{index}", "state": "working", "age": "3m"}
                  for index in range(4)]
        width = _route_line_repo_width(1, 140, routes)
        grid = CellGrid(140, 6)
        previous = None
        for index, route in enumerate(routes):
            repo = route["repo"]
            _draw_route_line(grid, 1, 140, index, route, repo_w=width, show_repo=repo != previous)
            previous = repo
        text = ["".join(cell[0] for cell in row) for row in grid.cells]
        self.assertEqual(sum(LONG + "/" in row for row in text), 1)
        self.assertEqual(sum(CJK + "/" in row for row in text), 1)
        badge_columns = {_cell_column(grid, index, "[") for index in range(4)}
        self.assertEqual(len(badge_columns), 1)
        for row in text:
            self.assertEqual(sum(cell_width(cell) for cell in row), 140)


class RouteSummaryCountBlink(unittest.TestCase):
    def _count_cells(self, vm, beat):
        frame = draw(vm, NavState(view="OVERVIEW"), 140, 60, beat=beat)
        for y, row in enumerate(frame.cells):
            text = "".join(cell[0] for cell in row)
            if "── ROUTES" in text:
                start = text.index("ROUTES") + len("ROUTES  ")
                end = text.index(" open", start) + len(" open")
                return text[start:end], {cell[1] for cell in row[start:end]}
        self.fail("no OVERVIEW ROUTES rule")

    def test_open_count_pulses_on_the_working_beat(self):
        vm = model()
        text_on, keys_on = self._count_cells(vm, 1)
        text_off, keys_off = self._count_cells(vm, 0)
        self.assertEqual(text_on, "2 open")
        self.assertEqual(text_on, text_off)
        self.assertEqual(keys_on, {"g_work"})
        self.assertEqual(keys_off, {"g_work_off"})

    def test_zero_open_routes_stay_steady(self):
        vm = model(routes=[
            {"id": "done-" + tag, "repo": LONG, "session_tag": tag, "state": "done",
             "harness": "codex", "title": "finished", "capability": "code", "age": "9m"}
            for tag in ("a1", "b2")])
        text_on, keys_on = self._count_cells(vm, 1)
        text_off, keys_off = self._count_cells(vm, 0)
        self.assertEqual(text_on, "0 open")
        self.assertEqual(keys_on, keys_off)
        self.assertNotIn("g_work", keys_on)
        self.assertNotIn("g_work_off", keys_on)


if __name__ == "__main__":
    unittest.main()
