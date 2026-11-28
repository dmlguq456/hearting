"""A route shown under the session that holds it now.

`peer-steward request <session> --route <id>` binds the route to the receiver in its seat
ledger and never rewrites the registry row, so the owner job still says `parent_sid=<sender>`
and the sender's route chain still lists the route. Fleet's parent edge (the job's tree parent,
`resolve_parent_edges`) already names the holder; the route row and the per-session `route N`
count follow that edge first whenever it differs from the registered parent. The cases below
pin what each kind of differing edge shows, next to what `main` showed.
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


def _session(sid, tag, *, chain=False):
    row = {"harness": "claude", "session_id": sid, "session_tag": tag, "cwd": REPO, "liveness": "idle",
           "is_child": False}
    if chain:
        row["route_chain"] = {"nodes": [{"route_id": ROUTE_ID}]}
    return row


def _route_tag(edge_sid, sessions=None, *, sender_names_route=False):
    """The route row's session tag and the per-session counts, with the owner job's edge set."""
    view = {"nodes": [{"id": "one-shot", "state": "active", "depends_on": [],
                       "unit": "_kernel/owner", "worker_type": "owner"}],
            "capability": "autopilot-code", "cwd": REPO}
    record = {"route_id": ROUTE_ID, "capability": "autopilot-code", "owner_attempt_id": "att-owner"}
    job = {"key": "route-owner", "dispatch_depth": 1, "worker_type": "owner", "harness": "claude",
           "route_id": ROUTE_ID, "attempt_id": "att-owner", "parent_sid": "sid-sender",
           "_parent_edge_sid": edge_sid, "liveness": "working"}
    if sessions is None:
        sessions = [_session("sid-sender", "a1", chain=True), _session("sid-receiver", "b2")]
    slot = {"view": view, "record": record, "jobs": [job],
            "sessions": [sessions[0]] if sender_names_route else []}
    with patch.object(live, "_collect_routes", return_value={ROUTE_ID: slot}):
        rows, index = live._session_records({"sessions": sessions}, NOW, lambda _cwd: "repo")
        routes, _nodes = live._route_records({"resources": []}, rows, index, lambda _cwd: "repo", NOW)
    return routes[0]["session_tag"], live.current_route_counts(routes)[0]


class HeldRouteAttributionTests(unittest.TestCase):
    def test_a_handed_route_is_counted_once_under_its_holder(self):
        # `assign` (and an existing /clear or retire successor): the edge is the current holder.
        # main showed it under the sender, whose chain recorded the start.
        for named in (False, True):
            with self.subTest(sender_names_route=named):
                self.assertEqual(_route_tag("sid-receiver", sender_names_route=named), ("b2", {"b2": 1}))

    def test_a_managed_client_edge_shows_the_route_where_its_job_hangs(self):
        # The registered parent is a hidden app-server; the edge resolved to its visible client.
        # main showed the route under no session at all.
        sessions = [_session("sid-client", "c3"), _session("sid-receiver", "b2")]
        self.assertEqual(_route_tag("sid-client", sessions), ("c3", {"c3": 1}))

    def test_an_observation_grace_edge_keeps_the_previous_answer(self):
        # A grace-held edge repeats the last confirmed parent: the registered one keeps main's
        # order exactly (here: no row for it this tick, so no session, as in main) ...
        sessions = [_session("sid-receiver", "b2")]
        self.assertEqual(_route_tag("sid-sender", sessions), (None, {}))
        # ... and one that is not shown this tick falls back to main's order too.
        self.assertEqual(_route_tag("sid-elsewhere")[0], "a1")

    def test_an_unhanded_route_keeps_its_launcher(self):
        for edge in ("sid-sender", None):
            with self.subTest(edge=edge):
                self.assertEqual(_route_tag(edge), ("a1", {"a1": 1}))


if __name__ == "__main__":
    unittest.main()
