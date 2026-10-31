"""Steward supervision is a column of cards and an exact LINKS relationship."""
import unittest

from tools.fleet.explorer import live
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import explorer_fixture as ef
from tools.fleet.tests.test_explorer_peers import _card_boxes

NOW = 1_800_000_000.0
TS = "2026-12-01T00:00:00+00:00"


def _row(tag, repo, harness="claude"):
    return {"tag": tag, "repo": repo, "repo_id": repo, "harness": harness,
            "state": "working", "title": tag + " title", "context": 40, "window": 1.0,
            "age": "5m", "now": "work", "ask": None, "steward": tag == "s1"}


def _session(targets=None, sent=None, received=None):
    return {"steward_targets": targets, "peer_last_recv": received, "peer_last_sent": sent}


def _model(ended=False):
    rows = [_row("s1", "repo-a"), _row("t1", "repo-a"), _row("t2", "repo-a", "codex"),
            _row("x1", "repo-b")]
    targets = [
        {"harness": "claude", "session_id": "sid-t1", "name": "t1", "source": "watch", "ts": TS},
        {"harness": "codex", "session_id": "sid-t2", "name": "t2", "source": "watch", "ts": TS},
        {"harness": "opencode", "session_id": "sid-x1", "name": "x1", "source": "watch", "ts": TS},
    ]
    if ended:
        targets.append({"harness": "claude", "session_id": "sid-gone", "name": "gone",
                        "source": "watch", "ts": TS})
    index = {}
    for row in rows:
        targets_for = targets if row["tag"] == "s1" else None
        sent = ({"to_session_id": "sid-t1", "to_harness": "claude",
                 "kind": "steer", "age_min": 3} if row["tag"] == "s1" else None)
        index[row["tag"]] = {"session": _session(targets_for, sent), "sid": "sid-" + row["tag"],
                             "ids": ("sid-" + row["tag"],), "chain": set(), "interaction": None}
    nodes, edges = live._peer_records(rows, index, NOW, None)
    nodes, edges = live._finish_peers(nodes, edges, rows, index, [], [], ())
    mapping = {"sessions": rows, "routes": [],
               "repos": [{"id": "repo-a", "name": "repo-a"},
                         {"id": "repo-b", "name": "repo-b"}],
               "peer_nodes": [tuple((repo, [list(column) for column in columns]))
                              for repo, columns in nodes],
               "peer_edges": edges}
    return ExplorerViewModel.from_mapping(mapping)


def _chain_model():
    """s1 → s2 → t2 is one column; l1/l2 keep their side lane and x1 stays LINKS-only."""
    rows = [_row("s1", "repo-a"), _row("s2", "repo-a", "codex"), _row("t2", "repo-a"),
            _row("l1", "repo-a"), _row("l2", "repo-a"), _row("x1", "repo-b")]
    targets = {
        "s1": [{"harness": "codex", "session_id": "sid-s2", "name": "s2", "source": "watch", "ts": TS},
               {"harness": "opencode", "session_id": "sid-x1", "name": "x1", "source": "watch", "ts": TS}],
        "s2": [{"harness": "claude", "session_id": "sid-t2", "name": "t2", "source": "watch", "ts": TS}],
    }
    index = {}
    for row in rows:
        tag = row["tag"]
        sent = received = None
        if tag in ("l1", "l2"):
            other = "l2" if tag == "l1" else "l1"
            sent = {"to_session_id": "sid-" + other, "to_harness": "claude",
                    "kind": "steer", "age_min": 3}
            received = {"from_session_id": "sid-" + other, "from_harness": "claude",
                        "kind": "steer", "age_min": 3}
        index[tag] = {"session": _session(targets.get(tag), sent, received),
                      "sid": "sid-" + tag, "ids": ("sid-" + tag,), "chain": set(),
                      "interaction": None}
    nodes, edges = live._peer_records(rows, index, NOW, None)
    nodes, edges = live._finish_peers(nodes, edges, rows, index, [], [], ())
    mapping = {"sessions": rows, "routes": [],
               "repos": [{"id": "repo-a", "name": "repo-a"},
                         {"id": "repo-b", "name": "repo-b"}],
               "peer_nodes": [tuple((repo, [list(column) for column in columns]))
                              for repo, columns in nodes],
               "peer_edges": edges}
    return ExplorerViewModel.from_mapping(mapping)


class StewardColumnTests(unittest.TestCase):
    def test_targets_stack_under_their_steward_with_a_grey_dotted_stalk(self):
        vm = _model()
        self.assertEqual([list(column) for column in vm.peer_edges["steward_columns"]],
                         [["s1", "t1", "t2"]])
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="PEERS", title="PEERS"),
                                             cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                boxes = _card_boxes(frame)
                self.assertEqual(boxes["s1"][0], boxes["t1"][0])
                self.assertEqual(boxes["t1"][0], boxes["t2"][0])
                self.assertLess(boxes["s1"][3], boxes["t1"][1])
                self.assertLess(boxes["t1"][3], boxes["t2"][1])
                x = boxes["s1"][0]
                for upper, lower in (("s1", "t1"), ("t1", "t2")):
                    for y in range(boxes[upper][3] + 1, boxes[lower][1]):
                        self.assertEqual(frame.cells[y][x][0], "┆", (cols, upper, lower, y))
                self.assertNotEqual(frame.cells[boxes["t2"][3] + 1][x][0], "┆")

    def test_one_watch_footer_per_frame(self):
        vm = _model()
        for cols in (120, 180):
            text = "\n".join(draw(vm, NavState(view="PEERS", title="PEERS"),
                                  cols, 60).text_rows())
            self.assertEqual(text.count("⚑ watch 3"), 1)

    def test_links_carry_one_exact_watch_per_target_and_keep_a_coexisting_steer(self):
        vm = _model()
        text = "\n".join(draw(vm, NavState(view="PEERS", title="PEERS"),
                              180, 60).text_rows())
        watch_rows = [line for line in text.splitlines() if "watch ⚑" in line]
        self.assertEqual(len(watch_rows), 3)
        for tag in ("t1", "t2", "x1"):
            self.assertTrue(any("[" + tag + "]" in line for line in watch_rows), tag)
        self.assertTrue(any("[t1]" in line and "steer" in line for line in text.splitlines()))
        self.assertIn("[s1] claude", text)

    def test_ended_target_has_no_card_but_stays_in_links_and_the_footer(self):
        vm = _model(ended=True)
        text = "\n".join(draw(vm, NavState(view="PEERS", title="PEERS"), 180, 60).text_rows())
        self.assertNotIn("[gone]", text)
        self.assertTrue(any("sid-gone"[:8] in line and "watch ⚑" in line
                            for line in text.splitlines()))
        self.assertIn("⚑ watch 3 · ended 1", text)

    def test_repo_mismatch_keeps_the_target_in_its_own_frame_and_links_only(self):
        vm = _model()
        self.assertEqual([list(column) for column in vm.peer_edges["steward_columns"]],
                         [["s1", "t1", "t2"]])
        frame = draw(vm, NavState(view="PEERS", title="PEERS"), 180, 60)
        boxes = _card_boxes(frame)
        self.assertGreater(boxes["x1"][0], boxes["s1"][0])
        text = "\n".join(frame.text_rows())
        self.assertTrue(any("[s1]" in line and "[x1]" in line and "watch ⚑" in line
                            for line in text.splitlines()))

    def test_a_chained_steward_is_one_column_without_a_duplicate_card(self):
        vm = _chain_model()
        self.assertEqual([list(column) for column in vm.peer_edges["steward_columns"]],
                         [["s1", "s2", "t2"]])
        flat = [tag for _repo, columns in vm.peer_nodes for column in columns for tag in column]
        self.assertEqual(sorted(flat), sorted(set(flat)), flat)
        self.assertEqual([list(column) for column in vm.peer_edges["side_lanes"]], [["l1", "l2"]])
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="PEERS", title="PEERS"),
                                             cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                boxes = _card_boxes(frame)
                for upper, lower in (("s1", "s2"), ("s2", "t2"), ("l1", "l2")):
                    self.assertEqual(boxes[upper][0], boxes[lower][0], (cols, upper, lower))
                x = boxes["s1"][0]
                for upper, lower in (("s1", "s2"), ("s2", "t2")):
                    for y in range(boxes[upper][3] + 1, boxes[lower][1]):
                        self.assertEqual(frame.cells[y][x][0], "┆", (cols, upper, lower, y))
                self.assertNotEqual(frame.cells[boxes["t2"][3] + 1][x][0], "┆")
                text = "\n".join(frame.text_rows())
                self.assertEqual(text.count("⚑ watch 3"), 1)
                self.assertNotIn("⚑ watch 1", text)
                watch_rows = [line for line in text.splitlines() if "watch ⚑" in line]
                self.assertEqual(len(watch_rows), 3)
                for tag in ("s2", "t2", "x1"):
                    self.assertTrue(any("[" + tag + "]" in line for line in watch_rows), tag)
                self.assertIn("repo-b/", text)
                if cols == 180:
                    self.assertGreater(boxes["x1"][0], boxes["s1"][0])


if __name__ == "__main__":
    unittest.main()
