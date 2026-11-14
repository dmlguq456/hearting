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
import contextlib
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet import details, projection, render, route_chain
from tools.fleet.collectors import dispatch, prompt_inputs
from tools.fleet.explorer import live, order, tables
from tools.fleet.explorer.grid import CellGrid
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.state import NavState
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


class RouteDetailsFixture(unittest.TestCase):
    """The reviewer's input and the drawing helpers both route-details suites share."""

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


class DeferredRouteDetails(RouteDetailsFixture):
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
        # Not known is in no count: the rule says "details wait" in place of a count, never 1 open.
        self.assertIn("ROUTES  details wait", lines[rule_at])
        self.assertNotRegex(lines[rule_at], r"\d open")
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
        self.assertEqual(tables.session_route_mark({"route_wait": "wait", "title": "t"}, 0), " route details wait")
        self.assertEqual(tables.session_route_mark({"route_wait": "wait", "title": "t"}, 2), " · details wait")
        self.assertEqual(tables.session_route_mark({"route_wait": "unread", "title": "t"}, 0), " route ?")
        self.assertEqual(tables.session_route_mark({"route_wait": "wait", "title_wait": True}, 0), "")
        self.assertEqual(tables.session_route_tail({"route_wait": "wait"}, 0), "")   # known counts only
        self.assertEqual(tables.session_route_tail({}, 2), "route 2")
        self.assertEqual(tables.route_count_segment({"routes": 0, "routes_wait": "unread"})[0], "route ?")
        self.assertEqual(tables.route_count_segment({"routes": 2, "routes_wait": "unread"})[0], "route 2 · ?")
        self.assertEqual(tables._ROUTE_MARK["wait"], ("·", "dim"))


# The detail pass's own neighbours, made no-ops: only the route projection step is exercised.
_QUIET_ENRICHERS = ((prompt_inputs, "enrich"), (dispatch, "_fill_locations"), (dispatch, "_note_home_view"),
                    (dispatch, "_enrich_claude_stream_session"), (dispatch, "_enrich_codex_attempt_session"),
                    (dispatch, "_enrich_opencode_attempt_session"), (dispatch, "_enrich_attempt_summary"),
                    (route_chain, "enrich"))


class TriedRouteDetails(RouteDetailsFixture):
    """Rule A: a route not known is drawn one way, in the lowest place.

    "details wait" stands only while the details are not tried yet. A detail pass that tried and
    did not bring a route (an error, a timeout, a part of the rows) leaves Fleet's usual unknown
    marks (``·``, ``?``) and keeps what did arrive. A route not known is in no count and never in
    a running colour, and its mark takes only the room nothing else on the line needs.
    """

    def detail_pass(self, attach):
        """The production required-detail pass over the basic rows, merged as the live loop does."""
        basic = copy.deepcopy(self.basic)
        basic.observations = observations(details=False)
        with contextlib.ExitStack() as stack:
            for owner, name in _QUIET_ENRICHERS:
                stack.enter_context(mock.patch.object(owner, name))
            stack.enter_context(mock.patch.object(dispatch, "_scan_degradations", return_value={}))
            stack.enter_context(mock.patch.object(projection, "attach_projections", side_effect=attach))
            detail = details._enrich(basic, required_only=True)
        return details.merge(basic, detail)

    def show(self, snapshot, cols=230):
        screen = _Screen(rows=60, cols=cols)
        render._draw(screen, snapshot.sessions, snapshot.jobs, "all", 0, loading=False, snapshot=snapshot)
        return render._EXPLORER_MODEL, text_lines(screen)

    def test_a_failed_or_timed_out_detail_pass_leaves_unknown_not_waiting(self):
        for error in (OSError("synthetic read failure"), TimeoutError("synthetic timeout")):
            for attempt in range(2):                        # the same twice: never a lasting wait
                merged = self.detail_pass(error)
                self.assertEqual(merged.observations["required-details"]["state"], "partial")
                model, lines = self.show(merged)
                routes = self.routes(model)
                for rid in (CARD, LAB, "rt-63788ad671654b75"):
                    route = routes[rid]
                    self.assertFalse(route.get("details_wait"), (error, attempt, rid))
                    self.assertIsNone(route["current"], rid)          # still not known ...
                    self.assertEqual(route["state"], "wait", rid)     # ... and not ended or 0
                    self.assertEqual(route["activity"], "wait", rid)
                    self.assertIsNone(route["stages"], rid)
                self.assertFalse(any("details wait" in line for line in lines), (error, attempt))
                self.assertEqual(self.status(model, "demo-app").get("routes_wait"), "unread")
                self.assertIn("route ?", self.line(lines, "demo-app/"))
                self.assertIn("ROUTES  ? open", self.line(lines, "── ROUTES"))
                self.assertNotIn("0 open", self.line(lines, "── ROUTES"))

    def test_a_partial_detail_pass_keeps_what_came_and_leaves_the_rest_unknown(self):
        source = {details.identity(entity): entity for entity in self.complete.sessions + self.complete.jobs}

        def partial(sessions, jobs, **_kwargs):
            for entity in sessions + jobs:
                arrived = source.get(details.identity(entity))
                if getattr(getattr(arrived, "work_projection", None), "route_id", None) == CARD:
                    entity.work_projection = copy.deepcopy(arrived.work_projection)
            raise OSError("partial synthetic result")

        model, lines = self.show(self.detail_pass(partial))
        routes = self.routes(model)
        card = routes[CARD]
        self.assertEqual((card["state"], card["activity"], card["current"]), ("working", "running", True))
        self.assertIn("execute", [stage["name"] for stage in card["stages"]])
        for rid in (LAB, "rt-63788ad671654b75"):
            self.assertEqual((routes[rid]["state"], routes[rid]["current"]), ("wait", None), rid)
            self.assertFalse(routes[rid].get("details_wait"), rid)
        self.assertFalse(any("details wait" in line for line in lines))
        self.assertIn("route 1 ⠏1", self.line(lines, "demo-app/"))
        self.assertIn("1 open", self.line(lines, "── ROUTES"))

    def test_the_wait_marker_leaves_the_title_header_once_the_details_were_tried(self):
        readiness = {"details_pending": ("required-details",),
                     "sources": {"required-details": {"state": "partial"}}}
        model = live.build_view_model(self.basic)            # the frame before any marking
        tried = live.mark_details_wait(model, readiness)
        self.assertFalse((tried.system or {}).get("details_wait"))
        self.assertFalse(any(row.get("title_wait") for row in tried.sessions))
        readiness["sources"]["required-details"]["state"] = "pending"
        waiting = live.mark_details_wait(model, readiness)
        self.assertTrue(waiting.system.get("details_wait"))
        self.assertTrue(any(row.get("title_wait") for row in waiting.sessions))

    def test_an_unknown_route_is_in_no_count_and_never_in_a_running_colour(self):
        owner = next(job for job in self.basic.jobs if getattr(job, "route_id", None) == CARD)
        owner.depth, owner.worker_type, owner.parent_sid = 1, "owner", "demo-claude-1"
        model, _lines = self.draw(self.basic, details=False)
        card = self.routes(model)[CARD]
        self.assertIsNone(card["current"])
        self.assertEqual(tables.route_counts([card]), (0, 0, 0))
        self.assertEqual(live.route_state_counts([card]), {"running": 0, "waiting": 0, "stopped": 0})
        self.assertFalse(tables.route_is_history(card))
        grid = CellGrid(133, 60)
        tables._draw_overview_routes_rule(grid, model, NavState(), 34, 97, 132, 3, [card])
        rule = "".join(cell[0] for cell in grid.cells[3])
        self.assertIn("ROUTES  details wait", rule)
        self.assertNotRegex(rule, r"\d open")
        keys = {cell[1] for cell in grid.cells[3]}
        self.assertFalse({"g_work", "g_work_off"} & keys)
        self.assertNotIn("claude 1", rule)                    # nor in the owner·worker mix

    def overview_pair(self, model, width, length, wait):
        """OVERVIEW drawn with a known title and a project name ``length`` long, with the
        routes-not-known marks (``wait``) and without them (the control)."""
        name = ("p" * length)[:length]
        repos = [dict(repo, name=name) if repo["id"] == "demo-app" else repo for repo in model.repos]
        sessions = [dict(row, title="KNOWN TITLE", title_wait=False) if row["tag"] == "a7" else dict(row)
                    for row in model.sessions]
        vm = model.with_updates(repos=repos, sessions=sessions)
        control = vm.with_updates(
            sessions=[dict(row, route_wait=None) for row in vm.sessions],
            repos=[dict(repo, status=dict(repo.get("status") or {}, routes_wait=None)) for repo in vm.repos])
        drawn = []
        for value in ((vm if wait else control), control):
            grid = CellGrid(width, 60)
            tables.draw_overview(grid, value, NavState())
            drawn.append(["".join(cell[0] for cell in row) for row in grid.cells])
        return name, drawn[0], drawn[1]

    def test_the_mark_never_takes_the_room_of_the_answer_wait_or_the_title(self):
        model, _lines = self.draw(self.basic, details=False)
        checked = 0
        for width in range(60, 201, 7):
            for length in (5, 8, 15, 25, 40, 56, 60):
                name, marked, control = self.overview_pair(model, width, length, wait=True)
                header = [row for row in marked if name + "/" in row]
                header_control = [row for row in control if name + "/" in row]
                self.assertEqual(bool(header), bool(header_control), (width, length))
                for row, plain in zip(header, header_control):
                    self.assertEqual("◑ to answer 1" in row, "◑ to answer 1" in plain, (width, length))
                session = next((row for row in marked if "[a7]" in row), None)
                plain = next((row for row in control if "[a7]" in row), None)
                self.assertEqual(session is None, plain is None, (width, length))
                if session is not None:
                    # Cell by cell the row is the control's, except in cells the control left blank.
                    self.assertEqual(len(session), len(plain))
                    for index, (cell, base) in enumerate(zip(session, plain)):
                        if base != " ":
                            self.assertEqual(cell, base, (width, length, index, session, plain))
                checked += 1
        self.assertGreater(checked, 100)

    def test_the_reviewers_four_lines(self):
        model, _lines = self.draw(self.basic, details=False)
        name, marked, control = self.overview_pair(model, 80, 8, wait=True)
        self.assertIn("KNOWN TITLE", next(row for row in marked if "[a7]" in row))      # not "…"
        for width, length in ((100, 25), (133, 56)):
            name, marked, control = self.overview_pair(model, width, length, wait=True)
            self.assertIn("◑ to answer 1", next(row for row in marked if name + "/" in row), (width, length))

    def test_full_screen_sweep_has_no_overflow_or_error(self):
        frames = {"basic": None, "tried": self.detail_pass(OSError("synthetic"))}
        for width in range(60, 201, 10):
            for kind, snapshot in frames.items():
                if snapshot is None:
                    snapshot = copy.deepcopy(self.basic)
                    snapshot.observations = observations(details=False)
                screen = _Screen(rows=60, cols=width)
                render._draw(screen, snapshot.sessions, snapshot.jobs, "all", 0, loading=False,
                             snapshot=snapshot)
                over = [(y, x, text) for (y, x), text in screen.cells.items()
                        if x + tables._seg_width(text) > width]
                self.assertEqual(over, [], (width, kind))


if __name__ == "__main__":
    unittest.main()
