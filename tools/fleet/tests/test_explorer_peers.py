"""Peer pane parity and layout checks from scrubbed fixture evidence."""
import json
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
SEPARATOR = 34


def _expand(rows):
    return [[cell for glyph, colour, tint, count in row
             for cell in [[glyph, colour, tint]] * count] for row in rows]


def _pane(frame):
    cells = [[list(cell) for cell in row[SEPARATOR:]] for row in frame.cells[5:]]
    hits = [[hit.y - 5, hit.x0 - SEPARATOR, hit.x1 - SEPARATOR, list(hit.action)]
            for hit in frame.hits if hit.y >= 5 and hit.x0 >= SEPARATOR]
    return cells, hits


class PeerPaneReference(unittest.TestCase):
    def test_peers_and_selected_links_match_reference_pane(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            capture = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            for name, folds in (("PEERS", ()), ("PEERS-pick-f5", (("pfocus:PEERS", "f5"),))):
                with self.subTest(cols=cols, state=name):
                    state = NavState(view="PEERS", title="PEERS", folds=folds)
                    actual_cells, actual_hits = _pane(draw(vm, state, cols, 45, beat=1))
                    expected = capture["list_states"][name]
                    expected_cells = _expand(expected["cells"])
                    expected_cells = [row[SEPARATOR:] for row in expected_cells[5:]]
                    expected_hits = [[hit[0] - 5, hit[1] - SEPARATOR, hit[2] - SEPARATOR, hit[3]]
                                     for hit in expected["hits"]
                                     if hit[0] >= 5 and hit[1] >= SEPARATOR]
                    if actual_cells != expected_cells:
                        mismatch = next(((row, col, expected_cells[row][col], actual_cells[row][col])
                                         for row in range(min(len(actual_cells), len(expected_cells)))
                                         for col in range(min(len(actual_cells[row]), len(expected_cells[row])))
                                         if actual_cells[row][col] != expected_cells[row][col]), None)
                        self.fail(f"{name} pane first cell difference at {cols}: {mismatch}")
                    self.assertEqual(actual_hits, expected_hits, f"{name} pane ordered hits at {cols}")

    def test_narrow_pane_has_bounded_hits_and_no_horizontal_overflow(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        frame = draw(vm, NavState(view="PEERS", title="PEERS"), 80, 100, beat=1)
        self.assertEqual(len(frame.cells), 100)
        self.assertTrue(all(len(row) == 80 for row in frame.cells))
        for hit in frame.hits:
            if hit.y == -1:  # chrome's keyboard target is a deliberate off-screen sentinel
                continue
            self.assertGreaterEqual(hit.y, 0)
            self.assertLess(hit.y, 96)
            self.assertGreaterEqual(hit.x0, 0)
            self.assertLessEqual(hit.x1, 80)
            self.assertLess(hit.x0, hit.x1)

    def test_cards_use_each_harness_palette(self):
        base = load_fixture(FIXTURES / "view_model.json").to_mapping()
        base["sessions"] = [
            {"tag": harness[:2], "harness": harness, "title": harness, "state": "working",
             "context": 40, "window": 1.0, "steward": False}
            for harness in ("claude", "codex", "opencode")]
        base["peer_nodes"] = [("repo", [[item["tag"] for item in base["sessions"]]])]
        base["peer_edges"] = {"links": [], "cross": [], "side_lanes": []}
        from tools.fleet.explorer.view_model import ExplorerViewModel
        frame = draw(ExplorerViewModel.from_mapping(base), NavState(view="PEERS", title="PEERS"), 140, 30)
        used = {cell[1] for row in frame.cells for cell in row if cell[1]}
        for harness in ("claude", "codex", "opencode"):
            self.assertIn("hb_" + harness, used)


def _variant(**changes):
    from tools.fleet.explorer.view_model import ExplorerViewModel
    values = load_fixture(FIXTURES / "view_model.json").to_mapping()
    values.update(changes)
    return ExplorerViewModel.from_mapping(values)


def _rows(vm, cols=140, folds=()):
    return draw(vm, NavState(view="PEERS", title="PEERS", folds=folds), cols, 60, beat=1).text_rows()


class PeerDerivedElements(unittest.TestCase):
    def test_frames_start_directly_under_the_pane_title(self):
        rows = _rows(load_fixture(FIXTURES / "view_model.json"))
        self.assertIn("{ PEERS }", rows[3])
        self.assertTrue(rows[4][SEPARATOR:].startswith("╔═ "), rows[4])

    def test_gpu_and_question_lines_come_from_the_session_and_question_records(self):
        base = load_fixture(FIXTURES / "view_model.json").to_mapping()
        sessions = [dict(item) for item in base["sessions"]]
        for item in sessions:
            if item["tag"] == "f5":
                item["gpu"] = list(item["gpu"]) * 3          # three held GPUs
            if item["tag"] == "a3":
                item["gpu"] = item["gpu"][:1]                # one is not worth a line
        rows = _rows(_variant(sessions=sessions))
        text = "\n".join(rows)
        self.assertIn("GPU ×3", text)
        self.assertNotIn("GPU ×8", text)
        self.assertIn("answer needed · 3d 7h", text)

    def test_watch_footer_and_lane_labels_follow_the_vm(self):
        base = load_fixture(FIXTURES / "view_model.json").to_mapping()
        edges = dict(base["peer_edges"])
        edges["watches"] = [{"repo": base["peer_nodes"][0][0], "text": "watch 4 · ended"}]
        edges["links"] = [list(link) for link in edges["links"]]
        for link in edges["links"]:
            if link[0] == ["a3", "codex"]:
                link[3] = "9m ago"
        text = "\n".join(_rows(_variant(peer_edges=edges)))
        self.assertIn(" ⚑ watch 4 · ended ", text)
        self.assertIn("[a3]→[f5] steer 9m", text)

    def test_clear_marker_needs_a_recent_clear(self):
        base = load_fixture(FIXTURES / "view_model.json").to_mapping()
        seats = [list(seat) for seat in base["seats"]]
        for seat in seats:
            if seat[0] == "0c":
                seat[3] = "22:47"
        self.assertNotIn("◂ gen", "\n".join(_rows(_variant(seats=seats))))
        self.assertIn("◂ gen 5 · 6m", "\n".join(_rows(load_fixture(FIXTURES / "view_model.json"))))

    def test_selected_card_wears_the_bright_border(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        plain = draw(vm, NavState(view="PEERS", title="PEERS"), 140, 45, beat=1)
        picked = draw(vm, NavState(view="PEERS", title="PEERS", folds=(("pfocus:PEERS", "f5"),)),
                      140, 45, beat=1)
        self.assertEqual(plain.cells[22][SEPARATOR + 2][1], "h_claude")
        self.assertEqual(picked.cells[22][SEPARATOR + 2][1], "hb_claude")

    def test_narrow_widths_keep_lanes_and_labels_inside_the_pane(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (80, 100):
            frame = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 100, beat=1)
            for row in frame.cells:
                self.assertEqual(len(row), cols)
            for row in frame.cells[4:80]:
                self.assertEqual(row[cols - 1][0], " ", "pane writes into the last column")
            text = "\n".join(frame.text_rows())
            self.assertIn("[a3]→[f5] steer 3h 17m", text)
            self.assertIn("→[f5] steer 3h 22m", text)

    def test_cards_never_overlap_and_hits_stay_in_the_pane(self):
        base = load_fixture(FIXTURES / "view_model.json").to_mapping()
        edges = dict(base["peer_edges"])
        cards = {tag: dict(card) for tag, card in edges["cards"].items()}
        cards["33"] = {"owners": cards["f5"]["owners"] * 2}          # a tall card in a stacked column
        edges["cards"] = cards
        for vm in (load_fixture(FIXTURES / "view_model.json"), _variant(peer_edges=edges)):
            for cols in (80, 100, 140, 168):
                with self.subTest(cols=cols):
                    frame = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 100, beat=1)
                    boxes = {}
                    for hit in frame.hits:
                        if hit.action[:2] == ("fold", "pfocus:PEERS") and hit.y < 90 and hit.x1 - hit.x0 > 20:
                            boxes.setdefault(hit.action[2], []).append(hit)
                    for tag, rows in boxes.items():
                        self.assertTrue(all(h.x0 >= SEPARATOR and h.x1 <= cols - 1 for h in rows), tag)
                        for other, other_rows in boxes.items():
                            if other != tag:
                                for a in rows:
                                    for b in other_rows:
                                        self.assertFalse(a.y == b.y and a.x0 < b.x1 and b.x0 < a.x1,
                                                         f"{tag} and {other} overlap at row {a.y}")


if __name__ == "__main__":
    unittest.main()
