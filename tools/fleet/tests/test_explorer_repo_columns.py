"""Repository identification precedes ID at the actual overview/route paint and hit boundaries."""
import unittest
from dataclasses import replace

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import CellGrid, cell_width
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.tables import _draw_route_line
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import explorer_fixture as ef


LONG = "fleet-main-history-opencode-ctx-followup-1029"
CJK = "음성인식-실험-저장소"


def model():
    sessions = [{"tag": tag, "repo": repo, "repo_id": repo, "harness": "codex",
                 "state": "working", "title": "selected work", "now": "current task",
                 "age": "3m", "context": 50, "window": 1}
                for tag, repo in (("a1", LONG), ("b2", CJK))]
    routes = [{"id": "route-" + s["tag"], "repo": s["repo"], "session_tag": s["tag"],
               "state": "working", "harness": "codex", "title": "route work",
               "capability": "code", "age": "3m", "stages": [{"name": "execute", "st": "active"}],
               "worker": ["worker", [["codex", "hb_codex"]], [["running execute", "g_spin"]]]}
              for s in sessions]
    return ExplorerViewModel.from_mapping({
        "repos": [{"id": name, "name": name} for name in (LONG, CJK)],
        "sessions": sessions, "routes": routes})


class RepoColumnTests(unittest.TestCase):
    def test_columns_share_width_without_overwrite_and_hits_still_fold(self):
        vm = model()
        for cols in (80, 100, 140, 180, 240, 320):
            for view, slot in (("OVERVIEW", "sopen:OVERVIEW"), ("ROUTES", "ropen:ROUTES")):
                with self.subTest(cols=cols, view=view):
                    state = NavState(view=view)
                    frame, trace = ef.trace_draw(draw, vm, state, cols, 60)
                    self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                    header = next(line for line in frame.text_rows() if "REPO" in line and "ID" in line)
                    self.assertLess(header.index("REPO"), header.index("ID"))
                    action = ("fold", slot, "a1" if view == "OVERVIEW" else "route-a1")
                    hit = next(hit for hit in frame.hits if hit.action == action)
                    for column in (header.index("REPO"), header.index("ID")):
                        at_cell = next(h for h in frame.hits if h.y == hit.y and h.x0 <= column < h.x1)
                        self.assertEqual(at_cell.action, action)
                    row = "".join(cell[0] for cell in frame.cells[hit.y][34:])
                    self.assertLess(row.index("fleet" if cols >= 100 else "f"), row.index("[a1]"))
                    if cols >= 240:
                        self.assertIn(LONG + "/", row)
                        self.assertIn(CJK + "/", "\n".join(frame.text_rows()))
                    selected, _effects = reduce(state, {"type": "click", "action": hit.action}, frame)
                    self.assertEqual(selected.opened_for(slot), action[2])
                    unfolded, trace = ef.trace_draw(draw, vm, selected, cols, 60)
                    self.assertEqual(ef.frame_violations(unfolded, trace, cols, 60), [])
                    self.assertTrue(any(h.action == action for h in unfolded.hits))
                    closed, _effects = reduce(selected, {"type": "click", "action": hit.action}, unfolded)
                    self.assertEqual(closed.opened_for(slot), "")

    def test_summary_repo_precedes_badge_and_uses_wide_room(self):
        for cols in (80, 100, 140, 240, 320):
            for repo in (LONG, CJK):
                with self.subTest(cols=cols, repo=repo):
                    grid = CellGrid(cols, 4)
                    route = dict(model().routes[0], repo=repo)
                    _draw_route_line(grid, 1, cols - 1, 1, route)
                    row = "".join(cell[0] for cell in grid.cells[1])
                    self.assertLess(row.index(repo[:1]), row.index("[a1]"))
                    self.assertEqual(sum(cell_width(cell[0]) for cell in grid.cells[1]), cols)
                    self.assertIn("3m", row)
                    if cols >= 240:
                        self.assertIn(repo + "/", row)

    def test_unknown_repo_does_not_borrow_a_display_name(self):
        vm = model()
        unknown = replace(vm, sessions=tuple(dict(s, repo="", repo_id="") for s in vm.sessions),
                          routes=tuple(dict(r, repo="") for r in vm.routes),
                          repos=({"id": "", "name": ""},))
        for view in ("OVERVIEW", "ROUTES"):
            text = "\n".join(draw(unknown, NavState(view=view), 240, 60).text_rows())
            self.assertNotIn(LONG, text)
            self.assertNotIn(CJK, text)


if __name__ == "__main__":
    unittest.main()
