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


def _snapshot(running=False, owner=None):
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
    record = {"result": None, "slug": "subject"}
    jobs = [_job("frame", "claude", "Opus 5.5", "high", running),
            _job("frame-alternative", "codex", "gpt-6.1-sol", "medium", running)]
    if owner is not None:
        record["owner_attempt_id"] = "att-owner"
        jobs.append({"key": "autopilot-code", "depth": 1, "dispatch_depth": 1,
                     "route_node": None, "worker_type": "owner", "liveness": "done",
                     "harness": owner.get("harness", ""),
                     "resolved_model": owner.get("model", "gpt-6.1-sol"),
                     "effort": owner.get("effort", "medium"), "route_id": "rt-frame",
                     "attempt_id": "att-owner", "elapsed_min": 11})
    slot = {
        "view": view, "record": record, "jobs": jobs,
        "sessions": [{"harness": "claude", "session_id": "sid-owner", "session_tag": "z9",
                      "cwd": "/synthetic/repo", "liveness": "idle", "is_child": False,
                      "title": "owner"}],
    }
    sessions = [{"harness": "claude", "session_id": "sid-owner", "session_tag": "z9",
                 "cwd": "/synthetic/repo", "liveness": "idle", "is_child": False,
                 "title": "owner"}]
    return view, slot, sessions


def _vm(running=False, owner=None, attempts=None):
    view, slot, sessions = _snapshot(running, owner)
    with patch.object(live, "_collect_routes", return_value={"rt-frame": slot}):
        rows, index = live._session_records({"sessions": sessions}, NOW, lambda _cwd: "repo")
        routes, nodes = live._route_records({"resources": []}, rows, index,
                                            lambda _cwd: "repo", NOW)
    mapping = {"repos": [{"id": "repo", "name": "repo"}],
               "sessions": rows, "routes": routes, "nodes": nodes}
    if attempts is not None:
        mapping["attempts"] = attempts
    vm = ExplorerViewModel.from_mapping(mapping)
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

    def test_a_framed_route_with_an_owner_attempt_keeps_the_owner_raw_box(self):
        attempts = {"route": "rt-frame", "att": "att-owner",
                    "lines": ["assistant: hello", "tool Bash: ls"]}
        _view, route, vm = _vm(owner={"harness": "codex"}, attempts=attempts)
        self.assertEqual(route["harness"], "codex")
        for cols in (120, 180):
            with self.subTest(cols=cols):
                state = NavState(view="ROUTES", title="ROUTES",
                                 folds=(("ropen:ROUTES", "rt-frame"),))
                frame, trace = ef.trace_draw(draw, vm, state, cols, 60)
                self.assertEqual(ef.frame_violations(frame, trace, cols, 60), [])
                text = "\n".join(frame.text_rows())
                self.assertEqual(text.count("owner raw"), 1)
                self.assertIn("attempt-view att-owner", text)
                self.assertIn("● hello", text)
                self.assertIn("● Bash  ls", text)
                self.assertEqual(text.count("frame-review and the legs after it"), 1)
                self.assertEqual(text.count("frame  claude (Opus 5.5·high)"), 1)
                self.assertEqual(text.count("alt  codex (gpt-6.1-sol·medium)"), 1)
                lines = text.splitlines()
                band = next(i for i, line in enumerate(lines)
                            if "frame ∥ frame-alternative" in line and "┌" in line)
                self.assertIn("frame  claude", lines[band + 1])

    def test_framed_progress_mark_follows_the_frame_legs(self):
        _view, _route, running = _vm(running=True)
        _view, _route, finished = _vm()
        for cols in (120, 180):
            with self.subTest(cols=cols):
                text = "\n".join(draw(running, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines() if "[z9]" in line and "frame" in line)
                self.assertIn("frame ●", row)
                self.assertIn("∥ frame 2-way", row)
                text = "\n".join(draw(finished, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines() if "[z9]" in line and "frame" in line)
                self.assertIn("frame ✓", row)
                self.assertIn("∥ frame 2-way", row)

    def test_an_owner_attempt_without_a_harness_does_not_borrow_a_frame_leg(self):
        _view, route, vm = _vm(owner={"harness": ""})
        self.assertIsNone(route["harness"])
        self.assertEqual(route["model"][0], "gpt-6.1-sol")
        self.assertEqual(route["effort"], "medium")
        for cols in (120, 180):
            with self.subTest(cols=cols):
                text = "\n".join(draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                      cols, 60).text_rows())
                row = next(line for line in text.splitlines() if "[z9]" in line and "frame" in line)
                self.assertNotIn("claude·high", row)
                self.assertNotIn("Opus 5.5", row)

    def test_narrow_routes_harness_fits_the_observed_text_and_clips_with_an_ellipsis(self):
        _view, _route, vm = _vm(owner={"harness": "claude", "effort": "high"})
        row = next(line for line in draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                         120, 60).text_rows()
                   if "[z9]" in line and "frame" in line)
        self.assertIn("claude·high", row[34:])

        _view, _route, vm = _vm(owner={"harness": "opencode", "effort": "xhigh"})
        row = next(line for line in draw(vm, NavState(view="ROUTES", title="ROUTES"),
                                         80, 60).text_rows()
                   if "[z9]" in line and "frame" in line)
        self.assertIn("opencode·…", row[34:])
        self.assertNotIn("opencode·x ", row[34:])

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
