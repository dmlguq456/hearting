"""A route whose details the first fast pass deferred reads "details wait", never ended or 0.

The basic frame (nothing to replay, required details pending) is drawn from the fast
projection, which leaves every route record for the detail pass. Such a route's state, whether
it runs and its stages are not known yet: the route row, the project's route count, the
OVERVIEW ROUTES rule and a session's folded count say "details wait" until the details are in.
A route the details show ended reads ended; a saved replay keeps showing its earlier values.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet import projection, render
from tools.fleet.explorer import live, order, tables
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.tests import explorer_fixture as ef
from tools.fleet.tests.test_explorer_initial import _Screen

CARD = "rt-271492e352d13180"      # demo-app's staged route, owned by session a7
LAB = "rt-6f5423d05eaf3189"       # lab-project's running route
RUNNING = (CARD, LAB)
BASIC = {"state": "complete", "observed_at": 10, "generation_identity": "g-1"}


def observations(*, details):
    return {
        "basic": dict(BASIC),
        "required-details": ({"state": "complete", "generation_identity": "g-1", "all_new": True}
                             if details else {"state": "pending"}),
        "resource-runs": {"state": "absent", "attempted": True, "all_new": True},
        **{f"usage:{h}": {"state": "complete", "observed_at": 10} for h in ("claude", "codex", "opencode")},
        "remote-host": {"state": "ok", "attempted": True},
    }


def text_lines(screen):
    rows = {}
    for (y, x), text in screen.cells.items():
        rows.setdefault(y, []).append((x, text))
    return ["".join(text for _x, text in sorted(cells)) for _y, cells in sorted(rows.items())]


class DeferredRouteDetails(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # the saver thread may still be writing
        self.addCleanup(self.temp.cleanup)
        self.runtime = ExplorerRuntime(Path(self.temp.name), strict_readiness=True)
        self.runtime.configure_persistence({"claude", "codex", "opencode"}, strict_readiness=True)
        for patch in (mock.patch.object(order, "order_path", return_value=Path(self.temp.name) / "order.json"),
                      mock.patch.object(render, "_EXPLORER_RUNTIME", self.runtime),
                      mock.patch.object(render, "_EXPLORER_PUBLISHED_MODEL", None),
                      mock.patch.object(render, "_EXPLORER_MODEL", None),
                      mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False),
                      mock.patch.object(render, "_EXPLORER_SHOWN", False),
                      mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})):
            patch.start()
            self.addCleanup(patch.stop)
        render.set_explorer_mode(True)
        self.addCleanup(render.set_explorer_mode, False)
        # The reviewer's input: the same observed sessions and registry jobs, two routes running.
        # ``complete`` carries the route details; ``basic`` is the fast projection of the same rows.
        self.complete, hosts = ef.demo_snapshot()
        self.complete.compute_hosts = hosts
        self.basic = copy.deepcopy(self.complete)
        for session in self.basic.sessions:
            session.route_chain = None
        projection.attach_projections(self.basic.sessions, self.basic.jobs, fast_first=True)

    def draw(self, snapshot, *, details):
        snapshot.observations = observations(details=details)
        screen = _Screen(rows=60, cols=230)
        render._draw(screen, snapshot.sessions, snapshot.jobs, "all", 0, loading=False, snapshot=snapshot)
        return render._EXPLORER_MODEL, text_lines(screen)

    @staticmethod
    def routes(model):
        return {route["id"]: route for route in model.routes}

    @staticmethod
    def status(model, repo):
        return next((row.get("status") or {}) for row in model.repos if row["id"] == repo)

    @staticmethod
    def line(lines, needle):
        return next((line for line in lines if needle in line), "")

    def test_basic_frame_says_details_wait_for_running_routes(self):
        model, lines = self.draw(self.basic, details=False)
        self.assertTrue(ExplorerRuntime.observed_only(render._EXPLORER_READINESS))
        routes = self.routes(model)
        for rid in RUNNING:
            route = routes[rid]
            self.assertTrue(route.get("details_wait"), rid)
            self.assertNotEqual(route["activity"], "ended", rid)   # not ended ...
            self.assertIsNot(route["current"], False, rid)         # ... and not "nothing runs"
            self.assertNotIn(route["state"], ("idle", "done", "dead"), rid)
            self.assertIsNone(route["stages"], rid)                 # stages unknown, not an empty rail
            self.assertEqual(live.route_marker_state(route), "wait")
        # The project's route count is not a 0: it waits.
        demo_app = self.status(model, "demo-app")
        self.assertTrue(demo_app.get("routes_wait"))
        self.assertIn("route details wait", self.line(lines, "demo-app/"))
        # The OVERVIEW ROUTES rule says it in words, never "0 open".
        rule = self.line(lines, "── ROUTES")
        self.assertIn("details wait", rule)
        self.assertNotIn("0 open", rule)
        # Session a7's own route count is still to come (its title cell already says it).
        a7 = next(row for row in model.sessions if row["tag"] == "a7")
        self.assertTrue(a7.get("route_wait"))
        self.assertIn("details wait (main)", self.line(lines, "[a7]"))

    def test_details_arrived_show_running_routes_and_stages(self):
        self.draw(self.basic, details=False)
        model, lines = self.draw(self.complete, details=True)
        routes = self.routes(model)
        card = routes[CARD]
        self.assertEqual((card["state"], card["activity"], card["current"]), ("working", "running", True))
        self.assertIn("execute", [stage["name"] for stage in card["stages"]])
        self.assertEqual(routes[LAB]["current"], True)
        self.assertFalse(any(route.get("details_wait") for route in routes.values()))
        self.assertEqual(self.status(model, "demo-app").get("routes"), 1)
        self.assertNotIn("routes_wait", self.status(model, "demo-app"))
        self.assertIn("route 1 ⠏1", self.line(lines, "demo-app/"))
        self.assertIn("1 open", self.line(lines, "── ROUTES"))
        self.assertIn("route 1", self.line(lines, "[a7]"))
        self.assertFalse(any("details wait" in line for line in lines))

    def test_a_route_the_details_show_ended_reads_ended(self):
        ended = self.complete
        for entity in ended.sessions + ended.jobs:
            work = getattr(entity, "work_projection", None)
            view = getattr(work, "_route_view", None)
            if getattr(work, "route_id", None) != LAB:
                continue
            if isinstance(view, dict):
                for node in (view.get("view") or {}).get("nodes") or ():
                    node["state"] = "done"
            if entity in ended.jobs:
                entity.liveness = "done"
        model, _lines = self.draw(ended, details=True)
        lab = self.routes(model)[LAB]
        self.assertNotIn("details_wait", lab)
        self.assertEqual((lab["state"], lab["activity"], lab["current"]), ("done", "ended", False))
        # A route the details found without a record is not "waiting" either.
        composed = self.routes(model)["rt-63788ad671654b75"]
        self.assertNotIn("details_wait", composed)
        self.assertEqual(composed["activity"], "ended")

    def test_a_saved_replay_keeps_its_earlier_route_values(self):
        self.draw(self.complete, details=True)
        replay = render._EXPLORER_PUBLISHED_MODEL
        self.assertIsNotNone(replay)
        with mock.patch.object(render, "_EXPLORER_PUBLISHED_MODEL", replay), \
                mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", True):
            model, lines = self.draw(self.basic, details=False)
        card = self.routes(model)[CARD]
        self.assertEqual((card["state"], card["activity"], card["current"]), ("working", "running", True))
        self.assertNotIn("details_wait", card)
        self.assertIn("route 1 ⠏1", self.line(lines, "demo-app/"))
        self.assertFalse(any("details wait" in line for line in lines))

    def test_a_bound_deferred_route_is_listed_waiting(self):
        # The owner attempt is a registered depth-1 row: the basic pass knows whose route it is.
        owner = next(job for job in self.basic.jobs if getattr(job, "route_id", None) == CARD)
        owner.depth, owner.worker_type, owner.parent_sid = 1, "owner", "demo-claude-1"
        model, lines = self.draw(self.basic, details=False)
        card = self.routes(model)[CARD]
        self.assertEqual(card["session_tag"], "a7")
        self.assertTrue(card.get("details_wait"))
        self.assertIsNone(card["current"])
        rule_at = next(i for i, line in enumerate(lines) if "── ROUTES" in line)
        self.assertIn("1 open · details wait", lines[rule_at])
        self.assertIn("details wait", lines[rule_at + 1])      # the route row's stage cell
        self.assertIn("[a7]", lines[rule_at + 1])

    def test_count_and_marker_texts(self):
        self.assertEqual(tables.route_count_segment({"routes": 0, "routes_wait": True})[0],
                         "route details wait")
        self.assertEqual(tables.route_count_segment(
            {"routes": 1, "routes_running": 1, "routes_wait": True})[0], "route 1 ⠏1 · details wait")
        self.assertEqual(tables.route_count_segment({"routes": 2, "routes_running": 1})[0], "route 2 ⠏1")
        self.assertEqual(tables.repo_status_segments({"routes": 0, "routes_wait": True}, 80),
                         [("route details wait", "dim")])
        self.assertEqual(tables.session_route_tail({"route_wait": True, "title": "t"}, 0), "route details wait")
        self.assertEqual(tables.session_route_tail({"route_wait": True, "title_wait": True}, 0), "")
        self.assertEqual(tables.session_route_tail({}, 2), "route 2")
        self.assertEqual(tables._ROUTE_MARK["wait"], ("·", "dim"))


if __name__ == "__main__":
    unittest.main()
