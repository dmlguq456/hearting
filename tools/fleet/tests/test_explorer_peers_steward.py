"""Steward supervision is a column of cards and an exact LINKS relationship."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import itertools
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


def _raw_session(row, targets=None, sent=None, received=None):
    """A collector-shaped session row: the fields the live path reads to mint/reuse a tag."""
    tag = row["tag"]
    return {"harness": row["harness"], "session_id": "sid-" + tag, "session_tag": tag,
            "cwd": "/synthetic/" + row["repo"], "liveness": row.get("state") or "working",
            "steward": row.get("steward", False), "title": row.get("title"),
            "summary": row.get("now"), "context_pct": row.get("context"),
            "window": row.get("window"), "steward_targets": targets,
            "peer_last_sent": sent, "peer_last_recv": received}


def _resolver(cwd):
    return str(cwd).rsplit("/", 1)[-1]


def _source(rows, targets, snapshot=None):
    source = dict(snapshot or {})
    source["sessions"] = [
        _raw_session(row, targets.get(row["tag"], row.get("steward_targets")),
                     row.get("peer_last_sent"), row.get("peer_last_recv"))
        for row in rows]
    source.setdefault("resources", [])
    return source


def _model(ended=False):
    rows = [_row("s1", "repo-a"), _row("t1", "repo-a"), _row("t2", "repo-a", "codex"),
            _row("x1", "repo-b")]
    rows[0]["peer_last_sent"] = {"to_session_id": "sid-t1", "to_harness": "claude",
                                 "kind": "steer", "age_min": 3}
    targets = [
        _target("t1"),
        _target("t2", "codex"),
        _target("x1", "opencode"),
    ]
    if ended:
        targets.append(_target("gone"))
    return _graph_model(rows, {"s1": targets})


def _target(tag, harness="claude"):
    """Exactly the keys ``steward.role_targets`` returns for a watch target."""
    return {"harness": harness, "session_id": "sid-" + tag, "name": tag, "kind": "watch",
            "ts": TS, "source": "watch", "pane": "wB:pAW"}


def _graph_model(rows, targets, snapshot=None):
    """A peer view model whose steward graph is ``targets`` (tag -> target records).

    The session rows are collector-shaped and pass through the real
    ``_session_records`` join, so a target resolves by its native session id
    exactly as it does live.
    """
    source = _source(rows, targets, snapshot)
    session_rows, index = live._session_records(source, NOW, _resolver)
    nodes, edges = live._peer_records(session_rows, index, NOW, source)
    nodes, edges = live._finish_peers(nodes, edges, session_rows, index, [], [], ())
    repos = []
    for row in session_rows:
        if row["repo"] not in repos:
            repos.append(row["repo"])
    mapping = {"sessions": session_rows, "routes": [],
               "repos": [{"id": repo, "name": repo} for repo in repos],
               "peer_nodes": [tuple((repo, [list(column) for column in columns]))
                              for repo, columns in nodes],
               "peer_edges": edges}
    return ExplorerViewModel.from_mapping(mapping)


def _chain_model():
    """s1 → s2 → t2 is one column; l1/l2 keep their side lane and x1 stays LINKS-only."""
    rows = [_row("s1", "repo-a"), _row("s2", "repo-a", "codex"), _row("t2", "repo-a"),
            _row("l1", "repo-a"), _row("l2", "repo-a"), _row("x1", "repo-b")]
    rows[3]["peer_last_sent"] = {"to_session_id": "sid-l2", "to_harness": "claude",
                                 "kind": "steer", "age_min": 3}
    rows[3]["peer_last_recv"] = {"from_session_id": "sid-l2", "from_harness": "claude",
                                 "kind": "steer", "age_min": 3}
    rows[4]["peer_last_sent"] = {"to_session_id": "sid-l1", "to_harness": "claude",
                                 "kind": "steer", "age_min": 3}
    rows[4]["peer_last_recv"] = {"from_session_id": "sid-l1", "from_harness": "claude",
                                 "kind": "steer", "age_min": 3}
    targets = {
        "s1": [_target("s2", "codex"), _target("x1", "opencode")],
        "s2": [_target("t2")],
    }
    return _graph_model(rows, targets)


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

    def _assert_one_steward_column(self, vm, tags, repo="repo-a"):
        self.assertEqual([list(column) for column in vm.peer_edges["steward_columns"]],
                         [list(tags)])
        node = next(entry for entry in vm.peer_nodes if entry[0] == repo)
        self.assertEqual([list(column) for column in node[1]], [list(tags)])
        flat = [tag for _repo, columns in vm.peer_nodes for column in columns for tag in column]
        for tag in tags:
            self.assertEqual(flat.count(tag), 1, (tag, flat))
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="PEERS", title="PEERS"),
                                             cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                boxes = _card_boxes(frame)
                for tag in tags:
                    self.assertIn(tag, boxes)
                    self.assertEqual(boxes[tag][0], boxes[tags[0]][0], (cols, tag))
                x = boxes[tags[0]][0]
                for upper, lower in zip(tags, tags[1:]):
                    for y in range(boxes[upper][3] + 1, boxes[lower][1]):
                        self.assertEqual(frame.cells[y][x][0], "┆", (cols, upper, lower, y))
                self.assertNotEqual(frame.cells[boxes[tags[-1]][3] + 1][x][0], "┆")

    def test_a_target_first_row_order_produces_the_same_steward_column(self):
        rows = [_row("s2", "repo-a", "codex"), _row("s1", "repo-a"), _row("t2", "repo-a")]
        targets = {"s1": [_target("s2", "codex")], "s2": [_target("t2")]}
        self._assert_one_steward_column(_graph_model(rows, targets), ["s1", "s2", "t2"])

    def test_every_permutation_of_a_three_node_chain_is_one_identical_column(self):
        targets = {"a": [_target("b")], "b": [_target("c")]}
        for order in itertools.permutations(("a", "b", "c")):
            with self.subTest(order=order):
                rows = [_row(tag, "repo-a") for tag in order]
                self._assert_one_steward_column(_graph_model(rows, targets), ["a", "b", "c"])

    def test_a_three_node_cycle_starts_at_its_lowest_tag(self):
        rows = [_row("c", "repo-a"), _row("b", "repo-a"), _row("a", "repo-a")]
        targets = {"a": [_target("b")], "b": [_target("c")], "c": [_target("a")]}
        self._assert_one_steward_column(_graph_model(rows, targets), ["a", "b", "c"])

    def test_a_four_node_chain_is_one_column(self):
        rows = [_row("d", "repo-a"), _row("c", "repo-a"), _row("b", "repo-a"),
                _row("a", "repo-a")]
        targets = {"a": [_target("b")], "b": [_target("c")], "c": [_target("d")]}
        self._assert_one_steward_column(_graph_model(rows, targets), ["a", "b", "c", "d"])

    def test_independent_columns_follow_the_heads_session_order(self):
        rows = [_row("h2", "repo-a"), _row("h1", "repo-a"), _row("t1", "repo-a"),
                _row("t2", "repo-a")]
        targets = {"h2": [_target("t2")], "h1": [_target("t1")]}
        vm = _graph_model(rows, targets)
        self.assertEqual([list(column) for column in vm.peer_edges["steward_columns"]],
                         [["h2", "t2"], ["h1", "t1"]])

    def test_a_repo_mismatch_member_stays_out_of_the_chain_column(self):
        rows = [_row("s2", "repo-a", "codex"), _row("s1", "repo-a"), _row("t2", "repo-a"),
                _row("x1", "repo-b", "opencode")]
        targets = {"s1": [_target("s2", "codex"), _target("x1", "opencode")],
                   "s2": [_target("t2")]}
        vm = _graph_model(rows, targets)
        self._assert_one_steward_column(vm, ["s1", "s2", "t2"])
        node = next(entry for entry in vm.peer_nodes if entry[0] == "repo-b")
        self.assertEqual([list(column) for column in node[1]], [["x1"]])
        frame = draw(vm, NavState(view="PEERS", title="PEERS"), 180, 60)
        boxes = _card_boxes(frame)
        self.assertGreater(boxes["x1"][0], boxes["s1"][0])
        text = "\n".join(frame.text_rows())
        self.assertTrue(any("[s1]" in line and "[x1]" in line and "watch ⚑" in line
                            for line in text.splitlines()))
        self.assertEqual(text.count("⚑ watch 3"), 1)
        self.assertIn("repo-b/", text)

    def test_a_historical_alias_keeps_the_short_sid_and_the_ended_note(self):
        rows = [_row("s1", "repo-a")]
        targets = {"s1": [_target("gone")]}
        snapshot = {"observations": {"replay_sources": {
            "historical_tags": {"sid-gone": "g0"}}}}
        vm = _graph_model(rows, targets, snapshot)
        links = [link for link in vm.peer_edges["links"] if str(link[2]) == "watch ⚑"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][1], "sid-gone")
        self.assertEqual(links[0][4], "ended")
        text = "\n".join(draw(vm, NavState(view="PEERS", title="PEERS"), 180, 60).text_rows())
        self.assertNotIn("[g0]", text)
        self.assertIn("⚑ watch 0 · ended 1", text)

    def test_role_target_records_resolve_through_real_session_rows_as_50_to_7a(self):
        """The ``[50] -> [7a]`` fixture uses role_targets' keys and the live tag join."""
        rows = [_row("50", "repo-a"), _row("7a", "repo-a", "codex")]
        rows[0]["steward"] = True
        rows[0]["steward_targets"] = [
            _target("7a", "codex"),
            {**_target("gone"), "session_id": "sid-gone", "pane": "wB:p9J"},
        ]
        vm = _graph_model(rows, {})
        links = [link for link in vm.peer_edges["links"] if str(link[2]) == "watch ⚑"]
        self.assertEqual([link[:2] for link in links],
                         [(("50", "claude"), ("7a", "codex")), (("50", "claude"), "sid-gone")])
        self.assertEqual(links[1][4], "ended")
        self.assertEqual([dict(entry) for entry in vm.peer_edges["watches"]],
                         [{"repo": "repo-a", "text": "watch 1 · ended 1"}])
        text = "\n".join(draw(vm, NavState(view="PEERS", title="PEERS"), 180, 60).text_rows())
        self.assertTrue(any("[50]" in line and "[7a]" in line and "watch ⚑" in line
                            for line in text.splitlines()))
        self.assertIn("⚑ watch 1 · ended 1", text)


if __name__ == "__main__":
    unittest.main()
