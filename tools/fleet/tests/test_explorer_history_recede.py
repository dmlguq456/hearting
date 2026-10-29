"""Finished Fleet routes recede; live work keeps its weight (backlog B, 2026-10-27).

The person: "그 fleet에서 라우트같은거에서 실패기록같은건 좀 어둡게 해서 시각적 강약을
뚜렷하게 하는게 중요할것 같아."

The delta gate is a paired render of the same fixture: one pass with `recede_key`
patched to identity, one real. Glyphs and ordered hits must not move; only style
keys of rows whose route already ended may change, and those rows must land on
A_DIM palette rungs. A live route's failed stage ✕ (`g_dead`) and every blocked
marker (`g_blocked`) stay exactly as strong as before.
"""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import render                                             # noqa: E402
from tools.fleet.explorer import tables                              # noqa: E402
from tools.fleet.explorer.frame import draw                          # noqa: E402
from tools.fleet.explorer.state import NavState                      # noqa: E402
from tools.fleet.explorer.style import recede_key                    # noqa: E402
from tools.fleet.explorer.view_model import load_fixture             # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
TABLE_X = 34                     # every list page starts its table at x=34 (sidebar is 33 wide)

# The fixture's ended routes: two failures waiting for their owner's next run and
# three completions. Match the ended glyph and exact fixture badge in either
# repo-before-ID tables or compact repo summaries, independently of title clipping.
HISTORY_MARKERS = re.compile(r"✕[^\[\]]*\[(?:e4|f5)\]|✓[^\[\]]*\[(?:b6|a9|2c)\]")
PEER_HISTORY = ("✕ lab", "✓ session-tidy")

SURFACES = (
    ("ROUTES", NavState(view="ROUTES", title="ROUTES")),
    ("OVERVIEW", NavState()),
    ("session-page-f5", NavState(scope="session:f5")),
    ("repo-page", NavState(scope="repo:xxxxxxxxx")),
    ("PEERS", NavState(view="PEERS", title="PEERS")),
)


def _lines(grid):
    return ["".join(str(cell[0] or "") for cell in row) for row in grid.cells]


def _render(vm, state, cols, *, identity=False):
    patch = mock.patch.object(tables, "recede_key", lambda key: key) if identity else None
    if patch is None:
        return draw(vm, state, cols, 45, beat=1)
    with patch:
        return draw(vm, state, cols, 45, beat=1)


def _row_regions(grid, surface):
    """The rows that belong to a finished route, plus the PEERS card span for each owner line."""
    lines = _lines(grid)
    spans = []                                    # (y, x0, x1) half-open; x1=None means the table width
    for y, line in enumerate(lines):
        if surface == "PEERS":
            for marker in PEER_HISTORY:
                start = 0
                while True:
                    x = line.find(marker, start)
                    if x < 0:
                        break
                    left = line.rfind("│", 0, x)
                    right = line.find("│", x)
                    if left >= 0 and right > left:
                        spans.append((y, left + 1, right))
                    start = x + len(marker)
        elif HISTORY_MARKERS.search(line[TABLE_X:]):
            spans.append((y, TABLE_X, None))
            if surface != "OVERVIEW":              # the ROUTES table is a two-row route+worker block
                spans.append((y + 1, TABLE_X, None))
    return spans


def _row_diffs(base, real):
    return {y: sorted((x for x in range(len(real[y]))
                       if real[y][x][1] != base[y][x][1]))
            for y in range(len(real)) if any(a[1] != b[1] for a, b in zip(real[y], base[y]))}


class HistoryBoundaryTest(unittest.TestCase):
    def test_only_finished_routes_are_history(self):
        cases = (
            ({"state": "dead", "activity": "ended"}, True),
            ({"state": "done", "activity": "ended"}, True),
            ({"state": "dead"}, True),                  # stored owner records may omit activity
            ({"state": "dead", "activity": "running"}, False),   # worker still running: live group
            ({"state": "done", "activity": "running"}, False),
            ({"state": "blocked", "activity": "ended"}, False),  # a blocked route still needs a person
            ({"state": "working", "activity": "running"}, False),
            ({"state": "idle", "activity": "running"}, False),
        )
        for route, expected in cases:
            with self.subTest(route=route):
                self.assertEqual(tables.route_is_history(route), expected)

    def test_fixture_history_set_is_the_ended_routes(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        history = {route["id"] for route in vm.routes if tables.route_is_history(route)}
        self.assertEqual(history, {"row2", "row3", "loop", "pair", "frame2"})
        live_ids = {route["id"] for route in vm.routes if not tables.route_is_history(route)}
        self.assertEqual(live_ids, {"row1", "f5", "cd", "e4c"})

    def test_recede_mapping_is_idempotent_and_uses_existing_palette_keys(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        grid = draw(vm, NavState(view="ROUTES", title="ROUTES"), 140, 45, beat=1)
        keys = {cell[1] for _y, x0, x1 in _row_regions(grid, "ROUTES")
                for cell in grid.cells[_y][x0:x1]}
        for key in keys:
            if key is None:
                continue
            once = recede_key(key)
            self.assertEqual(recede_key(once), once, key)
            if once != key:
                self.assertIn(once, render._HUE_OF, key)

    def test_peers_owner_without_activity_decides_by_state(self):
        owner = {"state": "dead", "label": "lab", "stages": []}
        self.assertTrue(tables.route_is_history(owner))


class RecedeDeltaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = load_fixture(FIXTURES / "view_model.json")

    def test_finished_route_rows_land_on_recede_fixed_points(self):
        """d1: every key inside a finished route row is unchanged by one more recede pass."""
        for name, state in SURFACES:
            for cols in (140, 168):
                with self.subTest(surface=name, cols=cols):
                    grid = draw(self.vm, state, cols, 45, beat=1)
                    spans = _row_regions(grid, name)
                    self.assertTrue(spans, f"no finished row found on {name}")
                    for y, x0, x1 in spans:
                        end = len(grid.cells[y]) if x1 is None else x1
                        for x in range(x0, end):
                            key = grid.cells[y][x][1]
                            if key is not None:
                                self.assertEqual(recede_key(key), key,
                                                 f"{name}:{cols} row {y} col {x} key {key}")

    def test_recede_moves_only_keys_on_finished_rows(self):
        """d2: glyphs, tints, and ordered hits are unchanged; keys change only where declared."""
        for name, state in SURFACES:
            for cols in (140, 168):
                with self.subTest(surface=name, cols=cols):
                    base = _render(self.vm, state, cols, identity=True)
                    real = _render(self.vm, state, cols)
                    self.assertEqual([[c[0] for c in row] for row in base.cells],
                                     [[c[0] for c in row] for row in real.cells],
                                     f"{name}:{cols} glyphs moved")
                    self.assertEqual([[c[2] for c in row] for row in base.cells],
                                     [[c[2] for c in row] for row in real.cells],
                                     f"{name}:{cols} tints moved")
                    self.assertEqual([(h.y, h.x0, h.x1, repr(h.action)) for h in base.hits],
                                     [(h.y, h.x0, h.x1, repr(h.action)) for h in real.hits],
                                     f"{name}:{cols} hits moved")
                    spans = _row_regions(base, name)
                    allowed = {(y, x) for y, x0, x1 in spans
                               for x in range(x0, len(real.cells[y]) if x1 is None else x1)}
                    diffs = _row_diffs(base.cells, real.cells)
                    for y, xs in diffs.items():
                        with self.subTest(row=y):
                            self.assertTrue(xs, f"{name}:{cols} row {y} changed without a key change")
                            for x in xs:
                                self.assertIn((y, x), allowed, f"{name}:{cols} changed outside a finished row")
                                self.assertEqual(real.cells[y][x][1],
                                                 recede_key(base.cells[y][x][1]))
                    expected_rows = {y for y, _x0, _x1 in spans}
                    # A worker row can already be all-dim (h_*/name_dim); that is a legitimate
                    # no-op. The point is that nothing OUTSIDE the finished rows moved.
                    self.assertLessEqual(set(diffs), expected_rows,
                                         f"{name}:{cols} a live row receded")

    def test_live_failure_and_blocked_keep_their_alarm(self):
        """d2 (live half): row1's re-tried `execute ✕` stays bold red; blocked stays red."""
        for cols in (140, 168):
            with self.subTest(cols=cols):
                base = _render(self.vm, NavState(view="ROUTES", title="ROUTES"), cols, identity=True)
                real = _render(self.vm, NavState(view="ROUTES", title="ROUTES"), cols)
                lines = _lines(real)
                row = next(y for y, line in enumerate(lines) if "execute ✕" in line)
                x = lines[row].index("✕", lines[row].index("execute "))
                self.assertEqual(real.cells[row][x][1], "g_dead")
                for y in range(len(real.cells)):
                    for x in range(TABLE_X, len(real.cells[y])):
                        if base.cells[y][x][1] == "g_blocked":
                            self.assertEqual(real.cells[y][x][1], "g_blocked",
                                             f"blocked marker dimmed at {y},{x}")
                    for x in range(TABLE_X, len(real.cells[y])):
                        if real.cells[y][x][0] == "✕" and real.cells[y][x][1] == "g_dead":
                            self.assertIn("execute ✕", lines[y],
                                          f"a finished-route ✕ stayed bold at row {y}")


if __name__ == "__main__":
    unittest.main()
