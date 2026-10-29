"""Peer pane parity and layout checks from scrubbed fixture evidence."""
import json
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet.explorer import peers
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.peers import LANE_HEAD_LEFT, LANE_HEAD_RIGHT
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


def _card_boxes(frame):
    """``tag -> (x0, top, x1, bottom)`` in screen cells from each card's fold hits."""
    rows = {}
    for hit in frame.hits:
        if hit.action[:2] == ("fold", "pfocus:PEERS") and hit.x1 - hit.x0 >= 28 and hit.x0 >= SEPARATOR:
            rows.setdefault(hit.action[2], []).append(hit)
    return {tag: (hits[0].x0, min(h.y for h in hits), hits[0].x1 - 1, max(h.y for h in hits))
            for tag, hits in rows.items()}


def _lane_gutter_delta(frame, vm, expected_cells):
    """The approved side-lane delta: only the gutter right of each stacked pair may differ.

    The reference put the lower card's leaving tail at a fixed fourth row, on its divider
    or bottom corner; the lanes now leave and arrive on each card's two first inner rows.
    """
    boxes = _card_boxes(frame)
    normalized = [list(row) for row in expected_cells]
    for upper, lower in (tuple(lane[:2]) for lane in vm.peer_edges.get("side_lanes", ())):
        if upper not in boxes or lower not in boxes:
            continue
        edge = boxes[upper][2]
        for yy in range(boxes[upper][1], boxes[lower][3] + 1):
            for xx in range(edge, edge + 6):
                py, px = yy - 5, xx - SEPARATOR
                if 0 <= py < len(normalized) and 0 <= px < len(normalized[py]):
                    normalized[py][px] = list(frame.cells[yy][xx])
    return normalized


LEGACY_HEADS = {"◀": LANE_HEAD_LEFT, "▶": LANE_HEAD_RIGHT}


def _arrow_glyph_delta(expected_cells):
    """The approved head change: the pane's old wide triangles are now the smaller ◂ ▸."""
    for row in expected_cells:
        for cell in row:
            if cell[0] in LEGACY_HEADS:
                cell[0] = LEGACY_HEADS[cell[0]]
    return expected_cells


def _lane_region(vm, boxes):
    """Every cell a side lane or its two labels paint, in screen coordinates."""
    cells = set()
    for lane in vm.peer_edges.get("side_lanes", ()):
        if len(lane) < 2:
            continue
        upper, lower = str(lane[0]), str(lane[1])
        if upper not in boxes or lower not in boxes:
            continue
        top, bottom = boxes[upper], boxes[lower]
        edge = top[2]
        for yy in range(top[1] + 1, bottom[3] + 3):
            for xx in range(edge, edge + 6):
                cells.add((yy, xx))
        for yy in (top[3] + 1, top[3] + 2):
            for xx in range(top[0] + 2, top[2]):
                cells.add((yy, xx))
    return cells


def _history_recede_delta(expected_cells):
    """The approved backlog-B delta: a finished owner line recedes to the dim rung.

    The frozen PEERS reference keeps every glyph and hit. A line that shows an ended
    owner (`✕ lab`, `✓ session-tidy`) may differ only by `style.recede_key` on the cells
    inside that card; every other frozen cell stays as captured.
    """
    from tools.fleet.explorer.style import recede_key

    normalized = [list(row) for row in expected_cells]
    for row in normalized:
        text = "".join(str(cell[0] or "") for cell in row)
        for marker in ("✕ lab", "✓ session-tidy"):
            start = 0
            while True:
                x = text.find(marker, start)
                if x < 0:
                    break
                left, right = text.rfind("│", 0, x), text.find("│", x)
                if left >= 0 and right > left:
                    for xx in range(left + 1, right):
                        cell = row[xx]
                        row[xx] = [cell[0], recede_key(cell[1]), *cell[2:]]
                start = x + len(marker)
    return normalized


class PeerPaneReference(unittest.TestCase):
    def test_peers_and_selected_links_match_reference_pane(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            capture = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            for name, folds in (("PEERS", ()), ("PEERS-pick-f5", (("pfocus:PEERS", "f5"),))):
                with self.subTest(cols=cols, state=name):
                    state = NavState(view="PEERS", title="PEERS", folds=folds)
                    frame = draw(vm, state, cols, 45, beat=1)
                    actual_cells, actual_hits = _pane(frame)
                    expected = capture["list_states"][name]
                    expected_cells = _expand(expected["cells"])
                    # The pane slice includes the shared key guide. Apply the
                    # same exact, approved scroll-help delta as the full oracle.
                    from tools.fleet.tests.test_explorer_oracle import normalize_new_tree_viewport_row
                    expected_cells, expected_full_hits = normalize_new_tree_viewport_row(
                        [[list(cell) for cell in row] for row in frame.cells], expected_cells,
                        [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in frame.hits],
                        expected["hits"], frame.tree_scroll)
                    expected_cells = [row[SEPARATOR:] for row in expected_cells[5:]]
                    expected_cells = _lane_gutter_delta(frame, vm, expected_cells)
                    expected_cells = _arrow_glyph_delta(expected_cells)
                    expected_cells = _history_recede_delta(expected_cells)
                    expected_hits = [[hit[0] - 5, hit[1] - SEPARATOR, hit[2] - SEPARATOR, hit[3]]
                                     for hit in expected_full_hits
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

    def test_empty_narrow_links_keep_the_reference_heading_and_columns(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel

        values = load_fixture(FIXTURES / "view_model.json").to_mapping()
        values["peer_nodes"] = []
        values["peer_edges"] = {"links": [], "cross": [], "side_lanes": [], "cards": {}, "watches": []}
        vm = ExplorerViewModel.from_mapping(values)
        for cols in (80, 100):
            with self.subTest(cols=cols):
                text = "\n".join(draw(vm, NavState(view="PEERS", title="PEERS"), cols, 45).text_rows())
                self.assertIn("LINKS  communication", text)
                self.assertIn("FROM", text)
                self.assertIn("KIND", text)

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

    def test_empty_narrow_links_keep_the_reference_heading_and_columns(self):
        vm = _variant(peer_nodes=[], peer_edges={"links": [], "cross": [], "side_lanes": [], "cards": {}, "watches": []})
        for cols in (80, 100):
            with self.subTest(cols=cols):
                text = "\n".join(draw(vm, NavState(view="PEERS", title="PEERS"), cols, 45).text_rows())
                self.assertIn("LINKS  communication", text)
                self.assertIn("FROM", text)
                self.assertIn("KIND", text)

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


def _one_repo(count, lane=False, owners=False):
    from tools.fleet.explorer.view_model import ExplorerViewModel
    values = load_fixture(FIXTURES / "view_model.json").to_mapping()
    tags = [f"x{index}" for index in range(count)]
    values["sessions"] = [{"tag": tag, "harness": "codex", "title": f"hearting task {index}",
                           "state": "idle", "repo": "hearting"} for index, tag in enumerate(tags)]
    columns = [[tag] for tag in tags]
    edges = {"links": [], "cross": [], "side_lanes": [], "cards": {}, "watches": []}
    if lane:
        columns = [tags[:2]] + columns[2:]
        edges["side_lanes"] = [tags[:2]]
        edges["links"] = [[[tags[0], "codex"], [tags[1], "codex"], "steer", "1m ago", "n"],
                          [[tags[1], "codex"], [tags[0], "codex"], "steer", "2m ago", "n"]]
    if owners:
        stage = {"name": "plan", "state": "done"}
        edges["cards"] = {tags[1]: {"owners": [{"state": "idle", "label": "lab", "stages": [stage]}]}}
    values["peer_nodes"] = [("hearting", columns)]
    values["peer_edges"] = edges
    return ExplorerViewModel.from_mapping(values)


class PeerWrappedLayout(unittest.TestCase):
    def test_one_repo_wraps_into_rows_and_columns_when_the_pane_is_wide(self):
        frame = draw(_one_repo(8), NavState(view="PEERS", title="PEERS"), 180, 60, beat=1)
        boxes = _card_boxes(frame)
        self.assertEqual(len(boxes), 8)
        xs = sorted({box[0] for box in boxes.values()})
        tops = sorted({box[1] for box in boxes.values()})
        self.assertEqual(len(xs), 4, xs)
        self.assertEqual(len(tops), 2, tops)
        # Row-major order keeps the session order; nothing leaves the pane.
        ordered = sorted(boxes, key=lambda tag: (boxes[tag][1], boxes[tag][0]))
        self.assertEqual(ordered, [f"x{index}" for index in range(8)])
        self.assertTrue(all(box[2] < 180 - 2 for box in boxes.values()))

    def test_narrow_pane_still_stacks_one_card_per_row(self):
        frame = draw(_one_repo(4), NavState(view="PEERS", title="PEERS"), 80, 60, beat=1)
        self.assertEqual(len({box[0] for box in _card_boxes(frame).values()}), 1)

    def test_side_lane_heads_and_tails_meet_both_cards_on_inner_rows(self):
        for cols in (100, 140, 180):
            for owners in (False, True):
                for offset in (0, 3):
                    with self.subTest(cols=cols, owners=owners, offset=offset):
                        state = NavState(view="PEERS", title="PEERS", scroll=(("pane", offset),))
                        frame = draw(_one_repo(6, lane=True, owners=owners), state, cols, 60, beat=1)
                        boxes = _card_boxes(frame)
                        ux0, uy, edge, ub = boxes["x0"]
                        _lx0, fy, lower_edge, fb = boxes["x1"]
                        self.assertEqual(edge, lower_edge)
                        glyph = lambda xx, yy: frame.cells[yy][xx][0]
                        for top, bottom, corner in ((uy, ub, "┐"), (fy, fb, "┘")):
                            # Head, line and trunk corner all share the title row.
                            self.assertEqual(glyph(edge, top + 1), LANE_HEAD_LEFT)
                            self.assertEqual([glyph(xx, top + 1) for xx in range(edge + 1, edge + 4)],
                                             ["─", "─", corner])
                            self.assertEqual(glyph(edge, bottom), "┘", "the corner stays whole")
                        self.assertTrue(all(glyph(edge + 3, yy) == "│"
                                            for yy in range(uy + 2, fy + 1)))
                        if owners:
                            self.assertEqual(glyph(edge, fy + 3), "┤", "the owner divider stays whole")
                        # The trunk column right of the cards belongs to their gap.
                        others = [box for tag, box in boxes.items() if tag not in ("x0", "x1")]
                        self.assertTrue(all(not (box[0] <= edge + 3 <= box[2] and
                                                 box[1] <= fy <= box[3]) for box in others))

    def test_cross_link_tail_line_and_head_share_one_row(self):
        frame = draw(load_fixture(FIXTURES / "view_model.json"), NavState(view="PEERS", title="PEERS"),
                     168, 60, beat=1)
        boxes = _card_boxes(frame)
        src, dst = boxes["e4"], boxes["f5"]
        if src[2] >= dst[0]:
            self.skipTest("the frames are not side by side at this width")
        row = max(src[1], dst[1]) + 2
        line = "".join(frame.cells[row][xx][0] for xx in range(src[2], dst[0]))
        # The approved fixture path: tail on e4's border, two frame borders, head beside f5.
        self.assertEqual(line, "├─╫───╫" + LANE_HEAD_RIGHT)
        self.assertEqual(frame.cells[row][src[0]][0], "│", "the tail row is not an owner divider")
        self.assertEqual(frame.cells[row][dst[0]][0], "│", "the head row is not an owner divider")


def _fleet_mesh(dst="d4", owners=True, clear=False):
    """One repo with six wrapped columns (two side-lane pairs) and a second repo's steer."""
    from tools.fleet.explorer.view_model import ExplorerViewModel
    values = load_fixture(FIXTURES / "view_model.json").to_mapping()
    hearting = [("50", True), ("e2", False), ("1f", False), ("79", False),
                ("8b", False), ("d5", False), ("d4", False), ("d7", False)]
    sessions = [{"tag": tag, "harness": "claude", "title": f"hearting task {index}",
                 "state": "idle", "repo": "hearting", "steward": steward,
                 "context": 40, "window": 1.0}
                for index, (tag, steward) in enumerate(hearting)]
    sessions.append({"tag": "a7", "harness": "claude", "title": "SR CorrNet eval",
                     "state": "idle", "repo": "SR_CorrNet_SD", "context": 30, "window": 1.0})
    values["sessions"] = sessions
    values["peer_nodes"] = [
        ["hearting", [["50", "e2"], ["1f", "79"], ["8b"], ["d5"], ["d4"], ["d7"]]],
        ["SR_CorrNet_SD", [["a7"]]],
    ]
    links = [[["50", "claude"], ["e2", "claude"], "steer", "1m ago", "n"],
             [["e2", "claude"], ["50", "claude"], "steer", "2m ago", "n"],
             [["1f", "claude"], ["79", "claude"], "steer", "3m ago", "n"],
             [["79", "claude"], ["1f", "claude"], "steer", "4m ago", "n"]]
    edges = {"links": links, "cross": [["a7", dst, "3h 22m"]],
             "side_lanes": [["50", "e2"], ["1f", "79"]], "cards": {}, "watches": []}
    if owners:
        stage = {"name": "plan", "state": "done"}
        edges["cards"] = {tag: {"owners": [{"state": "idle", "label": "lab", "stages": [stage]}]}
                          for tag in ("d4", "d5", "a7")}
    values["peer_edges"] = edges
    seats = [list(seat) for seat in values.get("seats", ())]
    if clear:
        seats.append(["a7", "x", 4, "22:47 · 6m ago", True])
    values["seats"] = seats
    return ExplorerViewModel.from_mapping(values)


class PeerCrossLinks(unittest.TestCase):
    def test_cross_links_only_paint_their_tail_and_badge(self):
        for cols in (120, 180, 206, 240):
            for dst in ("d4", "d5"):
                for owners in (False, True):
                    vm = _fleet_mesh(dst=dst, owners=owners, clear=True)
                    tall = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 60, beat=1)
                    with mock.patch.object(peers, "_draw_cross_links", lambda *a, **k: None):
                        tall_plain = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 60, beat=1)
                    for rows, offset, full, plain in ((60, 0, tall, tall_plain), (26, 3, None, None)):
                        if full is None:
                            state = NavState(view="PEERS", title="PEERS", scroll=(("pane", offset),))
                            full = draw(vm, state, cols, rows, beat=1)
                            with mock.patch.object(peers, "_draw_cross_links", lambda *a, **k: None):
                                plain = draw(vm, state, cols, rows, beat=1)
                        boxes = _card_boxes(tall)
                        shifted = {tag: (box[0], box[1] - offset, box[2], box[3] - offset)
                                   for tag, box in boxes.items()}
                        lane = {(y - offset, x) for y, x in _lane_region(vm, boxes)}
                        protected = set(lane)
                        for box in shifted.values():
                            protected.update((y, x) for y in range(box[1], box[3] + 1)
                                             for x in range(box[0], box[2] + 1))
                        diffs = []
                        for y in range(min(len(full.cells), len(plain.cells))):
                            for x in range(cols):
                                if list(full.cells[y][x]) != list(plain.cells[y][x]):
                                    diffs.append((y, x))
                        with self.subTest(cols=cols, dst=dst, owners=owners, offset=offset):
                            self.assertEqual(full.pane_scroll[4], offset)
                            for y, x in diffs:
                                if (y, x) not in protected:
                                    continue
                                allowed = False
                                for edge in vm.peer_edges["cross"]:
                                    src, target = str(edge[0]), str(edge[1])
                                    if src not in shifted or target not in shifted:
                                        continue
                                    a, b = shifted[src], shifted[target]
                                    ly = max(a[1], b[1]) + 2
                                    allowed = allowed or (a[0] <= x <= a[2] and y == a[3])
                                    allowed = allowed or (y == ly and x == a[2])
                                self.assertTrue(allowed,
                                                f"cross paint escaped into protected cells: {(y, x)}")
                            self.assertFalse(any((y, x) in lane for y, x in diffs),
                                             "cross paint covered a side lane")

    def test_cross_heads_and_tails_meet_card_boundaries(self):
        seen = 0
        cases = [("fixture", load_fixture(FIXTURES / "view_model.json"), cols)
                 for cols in (168, 180, 206, 240)]
        cases += [("mesh", _fleet_mesh(dst=dst, owners=True, clear=True), cols)
                  for cols in (168, 206) for dst in ("d4", "d5")]
        for name, vm, cols in cases:
            frame = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 60, beat=1)
            boxes = _card_boxes(frame)
            rows = frame.text_rows()
            for y, row in enumerate(rows[5:], 5):
                for x, glyph in enumerate(row):
                    if glyph != LANE_HEAD_RIGHT or x < SEPARATOR:
                        continue
                    seen += 1
                    dest = [box for box in boxes.values() if box[0] == x + 1 and box[1] < y < box[3]]
                    self.assertEqual(len(dest), 1, f"{name} {cols} head ({x},{y}) has no destination card")
                    dest_box = dest[0]
                    self.assertEqual(rows[y][dest_box[0]], "│", "head row is an owner divider")
                    tail = x - 1
                    while tail > SEPARATOR and rows[y][tail] in "─╫":
                        tail -= 1
                    self.assertEqual(rows[y][tail], "├", f"{name} {cols} head ({x},{y}) has no tail")
                    source = [box for box in boxes.values() if box[2] == tail and box[1] < y < box[3]]
                    self.assertEqual(len(source), 1, f"{name} {cols} tail ({tail},{y}) has no source card")
                    source_box = source[0]
                    self.assertEqual(rows[y][source_box[0]], "│", "tail row is an owner divider")
        self.assertGreater(seen, 0, "no cross head was drawn in any case")

    def test_cross_badge_starts_after_the_clear_marker(self):
        base = load_fixture(FIXTURES / "view_model.json").to_mapping()
        seats = [list(seat) for seat in base["seats"]]
        for seat in seats:
            if seat[0] == "e4":
                seat[2], seat[3] = 4, "22:47 · 6m ago"
        vm = _variant(seats=seats)
        clear = "◂ gen 3 · 6m"
        for cols in (120, 140, 168):
            with self.subTest(cols=cols):
                full = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 60, beat=1)
                with mock.patch.object(peers, "_draw_cross_links", lambda *a, **k: None):
                    plain = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 60, beat=1)
                boxes = _card_boxes(full)
                box = boxes["e4"]
                row = "".join(full.cells[box[3]][x][0] for x in range(box[0], box[2] + 1))
                start = row.index(clear)
                self.assertGreaterEqual(start, 1)
                for x in range(box[0] + start, box[0] + start + len(clear)):
                    self.assertEqual(full.cells[box[3]][x], plain.cells[box[3]][x],
                                     "cross paint covered the clear marker")
                self.assertIn("steer 3h 22m", row)
                self.assertLess(row.index(clear), row.index("steer 3h 22m"))
                if cols < 168:                    # the frames are stacked here, so the relation is a badge
                    self.assertIn("[f5]", row)
                    self.assertLess(row.index(clear), row.index("[f5]"))
                    self.assertEqual(row[-1], "┘", "the badge overwrote the card corner")

    def test_cross_layout_is_stable_while_scrolled(self):
        vm = _fleet_mesh(owners=True, clear=True)
        for cols in (168, 206):
            for offset in (3, 7):
                with self.subTest(cols=cols, offset=offset):
                    tall = draw(vm, NavState(view="PEERS", title="PEERS"), cols, 60, beat=1)
                    short = draw(vm, NavState(view="PEERS", title="PEERS",
                                              scroll=(("pane", offset),)), cols, 26, beat=1)
                    self.assertGreater(offset, 0)
                    self.assertEqual(short.pane_scroll[4], offset)
                    tall_rows, short_rows = tall.text_rows(), short.text_rows()
                    for y in range(4, short.pane_scroll[3]):
                        if "── LINKS" in short_rows[y] or "── LINKS" in tall_rows[y + offset]:
                            break
                        self.assertEqual(short_rows[y][SEPARATOR:], tall_rows[y + offset][SEPARATOR:],
                                         f"scrolled pane row {y} differs from the shifted long render")


if __name__ == "__main__":
    unittest.main()
