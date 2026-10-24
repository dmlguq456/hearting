"""Cell and ordered-hit comparison against the approved, redacted reference captures."""
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

    def test_overview_exact_reference_140_168(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            with self.subTest(cols=cols):
                expected = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
                expected["cells"] = expand_rows(expected["cells"])
                actual = draw(vm, NavState(), cols, 45, beat=1)
                cells = [[list(cell) for cell in row] for row in actual.cells]
                hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                if cells != expected["cells"]:
                    first = next((f"row={y} col={x} expected={expected['cells'][y][x]!r} actual={cells[y][x]!r}"
                                  for y in range(min(len(cells), len(expected["cells"])))
                                  for x in range(min(len(cells[y]), len(expected["cells"][y])))
                                  if cells[y][x] != expected["cells"][y][x]), "dimensions differ")
                    self.fail(f"first cell mismatch at {cols} columns: {first}")
                self.assertEqual(hits, expected["hits"], f"ordered hit mismatch at {cols} columns")


class ListReference(unittest.TestCase):
    def test_routes_questions_and_history_exact_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            for view in ("ROUTES", "QUESTIONS", "HISTORY"):
                with self.subTest(cols=cols, view=view):
                    expected = expected_file["list_states"][view]
                    actual = draw(vm, NavState(view=view, title=view), cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    self.assertEqual(cells, expand_rows(expected["cells"]), f"{view} cells at {cols}")
                    self.assertEqual(hits, expected["hits"], f"{view} ordered hits at {cols}")

    def test_repo_session_route_pages_and_unfolded_route_match_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            states = expected_file["list_states"]
            for case, base in (
                ("repo-page", NavState(scope="repo:xxxxxxxx")),
                ("session-page-f5", NavState(scope="session:f5")),
                ("route-page-f5", NavState(scope="route:f5")),
                ("ROUTES-open-f5", NavState(view="ROUTES",
                                             folds=(("ropen:ROUTES", "f5"),))),
            ):
                with self.subTest(cols=cols, case=case):
                    expected = states[case]
                    from dataclasses import replace
                    state = replace(base, title=expected["title"])
                    actual = draw(vm, state, cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    self.assertEqual(cells, expand_rows(expected["cells"]), f"{case} cells at {cols}")
                    self.assertEqual(hits, expected["hits"], f"{case} ordered hits at {cols}")

    def test_peers_and_filtered_links_match_exact_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            cases = (
                ("PEERS", NavState(view="PEERS", title="PEERS")),
                ("PEERS-pick-f5", NavState(view="PEERS", title="PEERS",
                                            folds=(("pfocus:PEERS", "f5"),))),
            )
            for name, state in cases:
                with self.subTest(cols=cols, state=name):
                    expected = expected_file["list_states"][name]
                    actual = draw(vm, state, cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    self.assertEqual(cells, expand_rows(expected["cells"]), f"{name} cells at {cols}")
                    self.assertEqual(hits, expected["hits"], f"{name} ordered hits at {cols}")


class QuietReference(unittest.TestCase):
    def test_compute_memory_models_system_exact_reference_140_168(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            golden = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            for view in ("COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
                with self.subTest(cols=cols, view=view):
                    expected = golden["list_states"][view]
                    actual = draw(vm, NavState(view=view, title=view), cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    self.assertEqual(cells, expand_rows(expected["cells"]), f"{view} cells at {cols}")
                    self.assertEqual(hits, expected["hits"], f"{view} ordered hits at {cols}")

    def test_folded_question_and_history_generation_timeline_match_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            question_state = NavState(view="QUESTIONS", title="QUESTIONS",
                                      folds=(("qshut:QUESTIONS", "bb"),))
            actual = draw(vm, question_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["QUESTIONS-fold-bb"]
            self.assertEqual([[list(cell) for cell in row] for row in actual.cells], expand_rows(expected["cells"]))
            self.assertEqual([[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits], expected["hits"])

            history_state = NavState(view="HISTORY", title="HISTORY",
                                     folds=(("hopen:HISTORY", "0c"),))
            actual = draw(vm, history_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["HISTORY-open-0c"]
            # P2 owns the seat and generation timeline through generation rows;
            # card and conversation content below it belongs to P4.
            expected_cells = expand_rows(expected["cells"])
            self.assertEqual(actual.cells[:11], tuple(tuple(tuple(cell) for cell in row)
                                                      for row in expected_cells[:11]))
            actual_hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits if 0 <= hit.y <= 10]
            expected_hits = [hit for hit in expected["hits"] if 0 <= hit[0] <= 10]
            self.assertEqual(actual_hits, expected_hits)

            card_state = NavState(view="HISTORY", title="HISTORY",
                                  folds=(("hopen:HISTORY", "0c"),))
            actual = draw(vm, card_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["HISTORY-open-0c-card"]
            self.assertEqual([[list(cell) for cell in row] for row in actual.cells],
                             expand_rows(expected["cells"]), f"HISTORY card/dialogue cells at {cols}")
            self.assertEqual([[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits],
                             expected["hits"], f"HISTORY card/dialogue ordered hits at {cols}")

            picked_state = NavState(view="HISTORY", title="HISTORY",
                                    folds=(("hopen:HISTORY", "0c"),
                                           ("ui:gen:f5", "0")))
            actual = draw(vm, picked_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["HISTORY-open-0c-picked-generation"]
            self.assertEqual([[list(cell) for cell in row] for row in actual.cells],
                             expand_rows(expected["cells"]), f"HISTORY picked-generation cells at {cols}")
            self.assertEqual([[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits],
                             expected["hits"], f"HISTORY picked-generation ordered hits at {cols}")
