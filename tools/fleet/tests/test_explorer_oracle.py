"""Exact current-screen oracle, plus optional historical reference checks."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
APPROVED = os.environ.get("FLEET_EXPLORER_REFERENCE_DIR")
EVIDENCE = os.environ.get("FLEET_EXPLORER_EVIDENCE_DIR")


def expand_rows(rows):
    return [[cell for glyph, colour, tint, count in row
             for cell in [[glyph, colour, tint]] * count] for row in rows]


def _sidebar_rule(row, width):
    """True when ``row``'s sidebar cells are the faint section rule (with its optional ↓ more)."""
    glyphs = "".join(str(cell[0] or "") for cell in row[1:width - 1])
    plain = "┈" * (width - 2)
    marked = "┈" * (width - 10) + " ↓ more "
    return glyphs in (plain, marked)


def normalize_new_tree_viewport_row(actual_cells, expected_cells, actual_hits, expected_hits,
                                   tree_scroll):
    """Apply only the new scroll help row and the sidebar's faint section rules.

    r14 made one more tree row visible above RESOURCES; the faint rules now take that
    row and the blank row above SESSIONS, so the tree rows and hits are the frozen ones.
    """
    help_y = len(actual_cells) - 2
    help_text = "".join(str(cell[0] or "") for cell in actual_cells[help_y]).rstrip()
    expected_help = (" ↑↓ move · ←→ focus · PgUp/Dn·Home/End·wheel scroll · "
                     "⏎ pane · Tab view · / filter · u usage · F feed · q quit")
    if help_text != expected_help:
        raise AssertionError(f"scroll help row changed outside the approved text: {help_text!r}")
    normalized_cells = [list(row) for row in expected_cells]
    normalized_cells[help_y] = actual_cells[help_y]
    width = int(tree_scroll[2]) if len(tree_scroll) > 2 else 32
    tree_bottom = int(tree_scroll[3]) if len(tree_scroll) > 3 else -1
    for y, row in enumerate(actual_cells):
        if not _sidebar_rule(row, width):
            continue
        # The frozen row above RESOURCES held the tree's cut marker; the one above
        # SESSIONS was blank. Neither carried a hit, so the hits stay the frozen ones.
        if y != tree_bottom and any(str(cell[0] or " ") != " " for cell in expected_cells[y][:width]):
            raise AssertionError(f"the faint sidebar rule covers a painted frozen row {y}")
        if any(hit[0] == y and hit[1] < width for hit in expected_hits):
            raise AssertionError(f"the faint sidebar rule covers a frozen hit on row {y}")
        normalized_cells[y][:width] = actual_cells[y][:width]
    return normalized_cells, expected_hits


def normalize_session_open_link(cells, hits):
    """Remove only the unfolded OVERVIEW session's page link, an intended delta from r14.

    The frozen captures predate the ``PAGE (open ▸)`` row on the unfolded session's spacer
    row, and the r14 reference renderer is external, so it is not re-frozen. Each
    ``("page", "session", tag)`` hit is dropped and exactly its label and link cells go back
    to the blank spacer. Any other content in those cells raises; a frame without the link is
    returned unchanged.
    """
    from tools.fleet.explorer.style import normalize_style
    from tools.fleet.explorer.tables import SESSION_OPEN_LABEL, SESSION_OPEN_LINK
    links = [hit for hit in hits if list(hit[3])[:2] == ["page", "session"]]
    if not links:
        return cells, hits
    blank = [" ", None, None]
    label_style = list(normalize_style("label", None))
    link_style = list(normalize_style("hearting", None))
    normalized = [list(row) for row in cells]
    for y, x0, x1, _action in links:
        label_x = x0 - 7                     # tables._session_unfold: x_val = x_lab + 7
        expected = ([[char] + label_style for char in SESSION_OPEN_LABEL] +
                    [blank] * (7 - len(SESSION_OPEN_LABEL)) +
                    [[char] + link_style for char in SESSION_OPEN_LINK])
        drawn = [list(cell) for cell in cells[y][label_x:x1]]
        if drawn != expected:
            raise AssertionError(f"session page link row {y} is not exactly the approved link: {drawn!r}")
        normalized[y] = list(normalized[y])
        normalized[y][label_x:x1] = [list(blank) for _ in range(x1 - label_x)]
    return normalized, [hit for hit in hits if hit not in links]


class CoreReference(unittest.TestCase):
    def test_freezer_matches_unscrubbed_raw_reference(self):
        if not APPROVED or not Path(APPROVED).is_dir():
            self.skipTest("set FLEET_EXPLORER_REFERENCE_DIR to run the raw reference comparison")
        if not EVIDENCE or not Path(EVIDENCE).is_dir():
            self.skipTest("set FLEET_EXPLORER_EVIDENCE_DIR to run the evidence comparison")
        from tools.fleet.tests.freeze_explorer import check_raw_reference
        report = check_raw_reference(Path(APPROVED), Path(EVIDENCE) / "w2-frame" /
                                     "freezer-vs-reference.json")
        self.assertEqual(report["comparisons"], 36)
        self.assertEqual(report["difference_count"], 0, report["differences"])


class CurrentReference(unittest.TestCase):
    def test_all_cells_and_ordered_hits_match_current_screen(self):
        from tools.fleet.tests.freeze_explorer_oracle import CASES, REGENERATE, capture
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            golden = json.loads((FIXTURES / f"oracle-{cols}.json").read_text())
            self.assertEqual(set(golden["states"]), set(CASES), REGENERATE)
            for name, state in CASES.items():
                with self.subTest(cols=cols, state=name):
                    expected = golden["states"][name]
                    actual = capture(vm, state, cols)
                    cells = expand_rows(expected["cells"])
                    if actual["cells"] != cells:
                        first = next((f"row={y} col={x} expected={want!r} actual={got!r}"
                                      for y, (want_row, got_row) in enumerate(zip(cells, actual["cells"]))
                                      for x, (want, got) in enumerate(zip(want_row, got_row))
                                      if want != got), "dimensions differ")
                        self.fail(f"{name} at {cols} columns: {first}. {REGENERATE}")
                    self.assertEqual(actual["hits"], expected["hits"],
                                     f"{name} ordered hits at {cols}. {REGENERATE}")


class NarrowOverviewTable(unittest.TestCase):
    def test_id_cell_never_touches_age_at_50_columns(self):
        import dataclasses
        vm = load_fixture(FIXTURES / "view_model.json")
        for age, tag in (("14h", "?26"), ("26d 14h", "?26"), ("14h", "e88"), ("26d 14h", "한글태그")):
            sessions = tuple({**s, "tag": tag, "age": age} for s in vm.sessions)
            frame = draw(dataclasses.replace(vm, sessions=sessions), NavState(), 50, 30, beat=1)
            rows = ["".join(str(c[0] or " ") for c in row)[34:] for row in frame.cells]
            with self.subTest(age=age, tag=tag):
                header = next(r for r in rows if " ID " in r)
                self.assertNotIn("HAGE", header)
                self.assertIn(" AGE", header)
                body = [r for r in rows if r.rstrip().endswith(age)]
                self.assertTrue(body)
                for row in body:
                    self.assertRegex(row, r"[^\s] \s*" + age.replace(" ", " ") + r"\s*$")
                    self.assertNotRegex(row, r"[^\s ]" + age.split()[0])


class SessionOpenLinkNormalizer(unittest.TestCase):
    """The r14 delta of the OVERVIEW session page link stays exactly one link wide."""

    @staticmethod
    def _json(frame):
        return ([[list(cell) for cell in row] for row in frame.cells],
                [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in frame.hits])

    def test_frames_without_the_link_are_unchanged(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        states = (NavState(), NavState(view="PEERS", title="PEERS"),
                  NavState(view="ROUTES", title="ROUTES"), NavState(view="HISTORY", title="HISTORY"),
                  NavState(view="HISTORY", title="HISTORY", folds=(("hopen:HISTORY", "0c"),)),
                  NavState(scope="session:f5"), NavState(scope="route:f5"),
                  NavState(scope="repo:xxxxxxxx"), NavState(usage_open=True))
        for cols in (140, 168):
            for state in states:
                with self.subTest(cols=cols, view=state.view, scope=state.scope):
                    cells, hits = self._json(draw(vm, state, cols, 45, beat=1))
                    self.assertFalse(any(hit[3][:1] == ["page"] for hit in hits))
                    self.assertEqual(normalize_session_open_link(cells, hits), (cells, hits))

    def test_the_link_is_the_only_delta_of_an_unfolded_session(self):
        from unittest import mock
        from tools.fleet.explorer import tables
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols, tag in ((140, "09"), (168, "09"), (140, "f5"), (168, "bb")):
            with self.subTest(cols=cols, tag=tag):
                state = NavState(opened=tag, folds=(("sopen:OVERVIEW", tag),))
                cells, hits = self._json(draw(vm, state, cols, 45, beat=1))
                self.assertEqual([hit[3] for hit in hits if hit[3][:1] == ["page"]],
                                 [["page", "session", tag]])
                with mock.patch.object(tables, "_session_open_link", lambda *_args: None):
                    plain = self._json(draw(vm, state, cols, 45, beat=1))
                self.assertEqual(normalize_session_open_link(cells, hits), plain)

    def test_anything_else_in_the_link_cells_is_not_normalized_away(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        state = NavState(opened="09", folds=(("sopen:OVERVIEW", "09"),))
        cells, hits = self._json(draw(vm, state, 140, 45, beat=1))
        y, x0 = next((hit[0], hit[1]) for hit in hits if hit[3][:1] == ["page"])
        for x in (x0 - 7, x0 - 2, x0 + 3):
            changed = [list(map(list, row)) for row in cells]
            changed[y][x] = ["X", "soft", None]
            with self.subTest(x=x), self.assertRaises(AssertionError):
                normalize_session_open_link(changed, hits)


if __name__ == "__main__":
    unittest.main()
