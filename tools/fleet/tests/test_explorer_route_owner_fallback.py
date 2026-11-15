"""A route whose owner started on another harness than the one asked for shows the one that ran.

2026-11-14: `--owner codex` routes started on OpenCode after Codex hit its usage limit. Fleet's
ROUTES owner line and the shell panel's ROUTE box read the owner attempt's registry row, not the
route's sealed pin, so both say `opencode` and its model; this pins that reading.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from unittest.mock import patch

from tools.fleet.explorer import live
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import ExplorerViewModel
from tools.fleet.tests import explorer_fixture as ef

NOW = 1_000_000.0
ROUTE_ID = "rt-owner-fallback"
REPO = "/synthetic/repo"


def _records(owner_harness, owner_model):
    view = {"nodes": [{"id": "one-shot", "state": "active", "depends_on": [],
                       "unit": "_kernel/owner", "worker_type": "owner"}],
            "capability": "autopilot-code", "capability_mode": "dev",
            "execution_topology": "one-shot-owner", "effective_intensity": "quick",
            "cwd": REPO, "slug": "review pr"}
    record = {"route_id": ROUTE_ID, "capability": "autopilot-code", "capability_mode": "dev",
              "execution_topology": "one-shot-owner", "selection": {"shape": "solo"},
              "effective_intensity": "quick", "owner_attempt_id": "att-owner", "result": None,
              "slug": "review pr",
              # What was asked for: the sealed pin and request say codex.
              "selection_pins": {"contract_version": 1,
                                 "owner": {"harness": "codex", "model": None, "effort": None}},
              "work_request": {"text": "review", "owner_harness": "codex"}}
    jobs = [{"key": "route-owner", "depth": 1, "dispatch_depth": 1, "route_node": None,
             "worker_type": "owner", "unit": "_kernel/owner", "worker_mode": None,
             "harness": owner_harness, "model": owner_model, "resolved_model": None,
             "effort": "max", "route_id": ROUTE_ID, "attempt_id": "att-owner",
             "elapsed_min": 3, "liveness": "working"}]
    session = {"harness": "claude", "session_id": "sid-parent", "session_tag": "e9",
               "cwd": REPO, "liveness": "idle", "is_child": False, "title": "supervisor"}
    slot = {"view": view, "record": record, "jobs": jobs, "sessions": [session]}
    with patch.object(live, "_collect_routes", return_value={ROUTE_ID: slot}):
        rows, index = live._session_records({"sessions": [session]}, NOW, lambda _cwd: "repo")
        routes, nodes = live._route_records({"resources": []}, rows, index, lambda _cwd: "repo", NOW)
    mapping = {"repos": [{"id": "repo", "name": "repo"}], "sessions": rows, "routes": routes, "nodes": nodes}
    return routes[0], ExplorerViewModel.from_mapping(mapping)


class OwnerFallbackRouteTests(unittest.TestCase):
    def test_owner_line_reads_the_attempt_that_ran_not_the_pin(self):
        for harness, model in (("opencode", "opencode-go/glm-5.3-flash"), ("codex", "gpt-6-astra"),
                               ("claude", "opus")):
            with self.subTest(harness=harness):
                route, vm = _records(harness, model)
                shown = live._model_pair(model, harness)[0]  # Fleet's own display name for the model
                self.assertEqual(route["harness"], harness)
                self.assertEqual(route["model"][0], shown)
                # The shell panel's ROUTE box text is built from the same record.
                self.assertTrue(live._owner_text(route).startswith(f"{harness} ({shown}"), live._owner_text(route))
                frame, trace = ef.trace_draw(draw, vm, NavState(view="ROUTES", title="ROUTES"), 180, 60)
                self.assertEqual(ef.frame_violations(frame, trace, 180, 60), [])
                text = "\n".join("".join(cell[0] for cell in row) for row in frame.cells)
                self.assertIn(harness, text)
                if harness != "codex":
                    self.assertNotIn("codex", text)


if __name__ == "__main__":
    unittest.main()
