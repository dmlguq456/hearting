"""A route a steward handed to another live session is shown under that session.

`peer-steward request <session> --route <id>` binds the route to the receiver in its seat
ledger and never rewrites the registry row, so the owner job still says `parent_sid=<sender>`
and the sender's route chain still lists the route. Fleet's parent edge already resolves the
holder; the route row (and the per-session `route N` count built from it) follows that edge
first, so the route is not drawn under both sessions.
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


def _route_tag(edge_sid, *, sender_names_route=False):
    view = {"nodes": [{"id": "one-shot", "state": "active", "depends_on": [],
                       "unit": "_kernel/owner", "worker_type": "owner"}],
            "capability": "autopilot-code", "cwd": REPO}
    record = {"route_id": ROUTE_ID, "capability": "autopilot-code", "owner_attempt_id": "att-owner"}
    job = {"key": "route-owner", "dispatch_depth": 1, "worker_type": "owner", "harness": "claude",
           "route_id": ROUTE_ID, "attempt_id": "att-owner", "parent_sid": "sid-sender",
           "_parent_edge_sid": edge_sid, "liveness": "working"}
    sender = {"harness": "claude", "session_id": "sid-sender", "session_tag": "a1", "cwd": REPO,
              "liveness": "idle", "is_child": False,
              "route_chain": {"nodes": [{"route_id": ROUTE_ID}]}}
    receiver = {"harness": "claude", "session_id": "sid-receiver", "session_tag": "b2", "cwd": REPO,
                "liveness": "idle", "is_child": False}
    slot = {"view": view, "record": record, "jobs": [job],
            "sessions": [sender] if sender_names_route else []}
    with patch.object(live, "_collect_routes", return_value={ROUTE_ID: slot}):
        rows, index = live._session_records({"sessions": [sender, receiver]}, NOW, lambda _cwd: "repo")
        routes, _nodes = live._route_records({"resources": []}, rows, index, lambda _cwd: "repo", NOW)
    return routes[0]["session_tag"], live.current_route_counts(routes)[0]


class HandedRouteAttributionTests(unittest.TestCase):
    def test_a_handed_route_is_counted_once_under_its_holder(self):
        for named in (False, True):
            with self.subTest(sender_names_route=named):
                tag, counts = _route_tag("sid-receiver", sender_names_route=named)
                self.assertEqual(tag, "b2")
                self.assertEqual(counts, {"b2": 1})

    def test_an_unhanded_route_keeps_its_launcher(self):
        for edge in ("sid-sender", None):
            with self.subTest(edge=edge):
                self.assertEqual(_route_tag(edge), ("a1", {"a1": 1}))

    def test_a_holder_not_shown_here_keeps_the_previous_order(self):
        self.assertEqual(_route_tag("sid-elsewhere")[0], "a1")


if __name__ == "__main__":
    unittest.main()
