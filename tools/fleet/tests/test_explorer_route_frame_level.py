"""A framed route with no owner attempt shows its frame legs at owner level."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.fleet.explorer import live
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.tables import _harness_mix
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import explorer_fixture as ef


NOW = 1_000_000.0


def _job(node, harness, model, effort, running=False):
    return SimpleNamespace(
        key="route-frame", depth=1, dispatch_depth=2, route_node=node,
        worker_type="frame", liveness="working" if running else "done", harness=harness, model=None,
        resolved_model=model, effort=effort, route_id="rt-frame",
        attempt_id="att-" + node, elapsed_min=5, route_attempt_round=1)


def _snapshot(running=False):
    node_state = "active" if running else "done"
    view = {
        "nodes": [
            {"id": "frame", "state": node_state, "depends_on": [], "parallel_group": "frame"},
            {"id": "frame-alternative", "state": node_state, "depends_on": [],
             "parallel_group": "frame"},
        ],
        "capability": "frame", "execution_topology": "framed", "capability_mode": "dev",
        "cwd": "/synthetic/repo", "slug": "subject",
    }
    slot = {
        "view": view,
        "record": {"result": None, "slug": "subject"},
        "jobs": [_job("frame", "claude", "Opus 5.5", "high", running),
                 _job("frame-alternative", "codex", "gpt-6.1-sol", "medium", running)],
        "sessions": [{"harness": "claude", "session_id": "sid-owner", "session_tag": "z9",
                      "cwd": "/synthetic/repo", "liveness": "idle", "is_child": False,
                      "title": "owner"}],
    }
    sessions = [{"harness": "claude", "session_id": "sid-owner", "session_tag": "z9",
                 "cwd": "/synthetic/repo", "liveness": "idle", "is_child": False,
                 "title": "owner"}]
    return view, slot, sessions


def _vm(running=False):
    view, slot, sessions = _snapshot(running)
    with patch.object(live, "_collect_routes", return_value={"rt-frame": slot}):
        rows, index = live._session_records({"sessions": sessions}, NOW, lambda _cwd: "repo")
        routes, nodes = live._route_records({"resources": []}, rows, index,
                                            lambda _cwd: "repo", NOW)
    vm = ExplorerViewModel.from_mapping({
        "repos": [{"id": "repo", "name": "repo"}],
        "sessions": rows, "routes": routes, "nodes": nodes})
    return view, routes[0], vm


class FrameOwnerLevelTests(unittest.TestCase):
    def test_owner_telemetry_is_borrowed_from_the_frame_legs_without_mutating_the_record(self):
        view, route, _vm_value = _vm()
        self.assertEqual(route["harness"], "claude")
        self.assertEqual(route["model"][0], "Opus 5.5")
        self.assertEqual(route["effort"], "high")
        self.assertEqual(route["shape"], "framed")
        self.assertEqual(route["legs"], [["frame", "done", "2/2"]])
        self.assertEqual([node.get("id") for node in view["nodes"]], ["frame", "frame-alternative"])
        self.assertTrue(all("harness" not in node for node in view["nodes"]))

    def test_route_row_says_frame_and_the_owner_cell_is_not_unknown(self):
        _view, _route, vm = _vm()
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(draw, vm, NavState(view="ROUTES", title="ROUTES"),
                                             cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                pane = ["".join(cell[0] for cell in row[34:]) for row in frame.cells]
                row = next(line for line in pane if "[z9]" in line)
                self.assertIn("frame", row)
                self.assertNotIn("worker", row)
                self.assertTrue("claude·" in row or "claude (" in row, row)
                self.assertTrue(any("└ frame" in line for line in pane), pane[:12])

    def test_both_frame_legs_show_their_own_harness_and_model(self):
        _view, _route, vm = _vm()
        for cols in (120, 180):
            with self.subTest(cols=cols):
                frame, trace = ef.trace_draw(
                    draw, vm, NavState(view="ROUTES", title="ROUTES",
                                       folds=(("ropen:ROUTES", "rt-frame"),)), cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                text = "\n".join(frame.text_rows())
                self.assertIn("frame  claude (Opus 5.5·high)", text)
                self.assertIn("alt  codex (gpt-6.1-sol·medium)", text)

    def test_tree_route_line_uses_the_frame_harness_and_mix_counts_the_owner_once(self):
        _view, route, vm = _vm(running=True)
        frame = draw(vm, NavState(view="ROUTES", title="ROUTES"), 140, 60)
        tree = "\n".join("".join(cell[0] for cell in row[:34]) for row in frame.cells)
        self.assertIn("frame: claude", tree)
        mix = "".join(text for text, _key in _harness_mix([route]))
        self.assertIn("claude 1", mix)
        self.assertIn("codex 0", mix)

    def test_a_frame_leg_shown_as_the_body_row_keeps_the_frame_label(self):
        view, slot, sessions = _snapshot()
        slot = dict(slot, view=dict(view, nodes=view["nodes"][:1]))
        with patch.object(live, "_collect_routes", return_value={"rt-frame": slot}):
            rows, index = live._session_records({"sessions": sessions}, NOW, lambda _cwd: "repo")
            routes, _nodes = live._route_records({"resources": []}, rows, index,
                                                 lambda _cwd: "repo", NOW)
        self.assertEqual(routes[0]["worker"][0], "frame")


if __name__ == "__main__":
    unittest.main()
