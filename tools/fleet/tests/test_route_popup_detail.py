"""Fleet's route table and route page, as the shell popup reuses them: a failure reason wraps whole,
artifact paths are listed, and the session state marker is one function."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from types import SimpleNamespace
import unittest

from tools.fleet import route_visibility as rv
from tools.fleet.explorer import pages, tables
from tools.fleet.explorer.grid import CellGrid

REASON = ("BLOCKED: synthetic runtime has no pty build so the wheel check could not run "
          "and the reason keeps going until the very last word ZEBRA-END")


def _route(segments, state_segments=(("failed", "lvl_r"),)):
    return {"id": "rt-fail", "repo": "", "title": "synthetic route", "capability": "autopilot-code",
            "mode": "", "intensity": "quick", "age": "3m", "activity": "idle",
            "legs": [["code", "failed", "0/1"]], "stages": [{"name": "one-shot", "st": "failed"}],
            "tree_label": "code", "worker_stage": "one-shot",
            "worker": ["owner", [list(s) for s in segments], [list(s) for s in state_segments]]}


def _draw(route, cols):
    grid = CellGrid(cols, 40)
    vm = SimpleNamespace(routes=(route,), repos=(), nodes={})
    state = SimpleNamespace(view="ROUTES", no_repo_group=True,
                            opened_for=lambda key, default="": "rt-fail" if key == "ropen:ROUTES" else default)
    tables.draw_routes(grid, vm, state, x=1, y=1)
    return grid.rows_text()


class WorkerReasonWrapTests(unittest.TestCase):
    def test_reason_text_is_whole_across_rows_at_popup_widths(self):
        for cols in (52, 82, 112):
            rows = _draw(_route([(REASON, "lvl_r")]), cols)
            self.assertTrue(all(len(row) <= cols for row in rows))
            flat = " ".join(" ".join(row.split()) for row in rows)
            for word in REASON.split():
                self.assertIn(word, flat, (cols, word))
            self.assertIn("ZEBRA-END", flat)

    def test_model_cell_worker_line_is_unchanged(self):
        rows = _draw(_route([("claude", "hb_claude"), (" ", "dim"), ("opus", "model"),
                             (" ", "dim"), ("high", "eff_high")]), 112)
        line = next(row for row in rows if "└ owner" in row)
        self.assertIn("claude", line)
        self.assertIn("failed", line)           # the progress cell stays on the same one row


class DetailPathTests(unittest.TestCase):
    DESC = {"shape": "solo", "capability": "autopilot-code", "intensity": "quick",
            "basis": {"kind": "default"}, "stages": [], "verification": {},
            "next": {"kind": "wait", "text": "wait · one-shot"},
            "links": [{"label": "route", "path": "/syn/art/route.json"},
                      {"label": "evidence one-shot", "path": "/syn/art/evidence.md"}]}

    def test_detail_lines_list_link_paths_right_before_next(self):
        lines = rv.detail_lines(self.DESC)
        self.assertEqual(lines[-3:], [("path", "route /syn/art/route.json", "dim"),
                                      ("path", "evidence one-shot /syn/art/evidence.md", "dim"),
                                      ("next", "wait · one-shot", "info")])

    def test_bounded_keeps_next_with_links(self):
        lines = rv.bounded(dict(self.DESC, links=[{"label": "l%d" % i, "path": "/p/%d" % i}
                                                  for i in range(6)]))["lines"]
        self.assertEqual(lines[-1][0], "next")
        self.assertEqual([row[1] for row in lines if row[0] == "path"],
                         ["l%d /p/%d" % (i, i) for i in range(6)])


class VisibilityWrapTests(unittest.TestCase):
    def test_long_path_wraps_at_value_column(self):
        path = "/synthetic/" + "/".join("segment%02d" % i for i in range(12)) + "/route.json"
        vis = {"lines": [["path", "route " + path, "dim"], ["next", "wait", "info"]]}
        grid = CellGrid(60, 40)
        y = pages._route_visibility_lines(grid, vis, 0, 58, 3, 38)
        text = "".join("".join(row.split()) for row in grid.rows_text())
        self.assertIn("route" + path.replace(" ", ""), text)
        self.assertIn("route.json", text)
        self.assertGreater(y, 5)
        self.assertIn("next:", "\n".join(grid.rows_text()))


class SessionMarkerTests(unittest.TestCase):
    def test_mapping(self):
        mark = tables.session_marker
        self.assertEqual(mark({"state": "working"}), ("⠏", "spin"))
        self.assertEqual(mark({"state": "blocked"}), ("●", "blocked"))
        self.assertEqual(mark({"state": "dead"}), ("✕", "dead"))
        self.assertEqual(mark({"state": "done"}), ("✓", "done"))
        self.assertEqual(mark({"state": "idle"}), ("●", "idle"))
        self.assertEqual(mark({}), ("●", "idle"))
        self.assertEqual(mark({"state": "working", "answer_wait_marker": "●"}), ("●", "blocked"))
        self.assertEqual(mark({"state": "idle", "answer_wait_marker": "◑"}), ("◑", "blocked"))


if __name__ == "__main__":
    unittest.main()
