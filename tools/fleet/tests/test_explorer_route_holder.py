"""A route handed on with `request --route` is shown under the session that holds it now.

`peer-steward request <session> --route <id>` binds the route to the receiver in its seat ledger
and never rewrites the registry row, so the owner job still says `parent_sid=<sender>` and the
sender's route chain still lists the route. The collector marks a job whose route an `assign`
row bound with its exact holder (`_route_holder`, harness and session); only then does the route
row (and the per-session `route N` count) go to that holder. Every other parent edge — an
existing /clear or retire successor, a managed client, an observation grace — keeps `main`'s order.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from unittest.mock import patch

from tools.fleet.explorer import live

NOW = 1_000_000.0
ROUTE_ID = "rt-handed"
REPO = "/synthetic/repo"


def _session(sid, tag, *, harness="claude", chain=False):
    row = {"harness": harness, "session_id": sid, "session_tag": tag, "cwd": REPO, "liveness": "idle",
           "is_child": False}
    if chain:
        row["route_chain"] = {"nodes": [{"route_id": ROUTE_ID}]}
    return row


def _route_tag(*, holder=None, edge=None, sessions=None, sender_names_route=False):
    """The route row's session tag and the per-session counts for one owner job."""
    view = {"nodes": [{"id": "one-shot", "state": "done", "depends_on": [],
                       "unit": "_kernel/owner", "worker_type": "owner"}],
            "capability": "autopilot-code", "cwd": REPO}
    record = {"route_id": ROUTE_ID, "capability": "autopilot-code", "owner_attempt_id": "att-owner"}
    job = {"key": "route-owner", "dispatch_depth": 1, "worker_type": "owner", "harness": "claude",
           "route_id": ROUTE_ID, "attempt_id": "att-owner", "parent_sid": "sid-sender",
           "_parent_edge_sid": edge, "_route_holder": holder, "liveness": "done"}
    if sessions is None:
        sessions = [_session("sid-sender", "a1", chain=True), _session("sid-receiver", "b2")]
    slot = {"view": view, "record": record, "jobs": [job],
            "sessions": [sessions[0]] if sender_names_route else []}
    with patch.object(live, "_collect_routes", return_value={ROUTE_ID: slot}):
        rows, index = live._session_records({"sessions": sessions}, NOW, lambda _cwd: "repo")
        routes, _nodes = live._route_records({"resources": []}, rows, index, lambda _cwd: "repo", NOW)
    return routes[0]["session_tag"], live.current_route_counts(routes)[0]


class HandedRouteAttributionTests(unittest.TestCase):
    def test_a_handed_route_is_shown_once_under_its_exact_holder(self):
        for named in (False, True):
            with self.subTest(sender_names_route=named):
                tag, counts = _route_tag(holder=("claude", "sid-receiver"), edge="sid-receiver",
                                         sender_names_route=named)
                self.assertEqual(tag, "b2")
                self.assertNotIn("a1", counts)

    def test_a_same_sid_row_of_another_harness_is_not_the_holder(self):
        sessions = [_session("sid-sender", "a1", chain=True), _session("sid-receiver", "c3", harness="codex")]
        self.assertEqual(_route_tag(holder=("claude", "sid-receiver"), sessions=sessions)[0], "a1")

    def test_edges_without_a_hand_off_keep_mains_order(self):
        # An existing successor or a managed client (edge differs, no `assign`), an observation grace
        # (the registered parent), or no edge at all: the sender's chain keeps the route, as in main.
        cases = {"successor-or-client": "sid-receiver", "grace": "sid-sender", "none": None}
        for name, edge in cases.items():
            with self.subTest(edge=name):
                self.assertEqual(_route_tag(edge=edge)[0], "a1")
        sessions = [_session("sid-sender", "a1", chain=True), _session("sid-receiver", "c3", harness="codex")]
        self.assertEqual(_route_tag(edge="sid-receiver", sessions=sessions)[0], "a1")

    def test_a_holder_not_shown_here_keeps_mains_order(self):
        self.assertEqual(_route_tag(holder=("claude", "sid-elsewhere"))[0], "a1")


if __name__ == "__main__":
    unittest.main()
