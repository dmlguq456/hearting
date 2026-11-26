"""A route the session declares and does itself (``--shape direct``) stands in the shell panel's ROUTE box."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from unittest import mock

from tools.fleet import session_line, session_panel
from tools.fleet.explorer import live
from tools.fleet.model import DispatchJob, Session, WorkProjection

NOW = 1_800_000_000.0
KEY = "claude\0sid-direct"


def chain_node(rid="rt-direct", *, state="open", shape="direct", result=None):
    # The fields route_chain.assemble gives a node (copied from it).
    return {"label": "code", "capability": "autopilot-code", "capability_mode": "dev", "automatic": False,
            "route_id": rid, "route_file": None, "state": state, "shape": shape, "intensity": "direct",
            "round": 1, "handoff_to": None, "handoff_from": None, "ambiguity": [],
            "source_route_id": None, "ts": NOW - 900, "result": result}


def snapshot(node, liveness="idle", jobs=(), elapsed_min=60):
    chain = {"v": 1, "key": "repo", "visible": False, "plan": [], "nodes": [node] if node else [],
             "current": node, "automatic_records": []}
    session = Session(harness="claude", pid=os.getpid(), session_id="sid-direct", session_tag="s1",
                      cwd="/fixture", liveness=liveness, route_chain=chain, elapsed_min=elapsed_min)
    return {"sessions": [session], "jobs": list(jobs), "resources": []}


def finished(at):
    return {"name": "dfix", "result": "success", "at": at, "source": "route-outcome", "route_id": "rt-direct"}


def shown(panels, label, *, now=None, human_at=0, lang="en"):
    with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": lang}):
        return session_panel.presentation({"route_label": label}, {"route_panels": panels}, "claude", "sid-direct", 60,
                                          now=now, display={"human": {"at": human_at}} if now else None)


class DirectRoutePanelTests(unittest.TestCase):
    def test_an_open_direct_route_is_counted_and_listed_with_the_shape_word(self):
        snap = snapshot(chain_node())
        labels, panels = live.session_route_status(snap, NOW, include_details=True)
        self.assertEqual(labels[KEY], "route 1")
        (row,) = panels[KEY]
        self.assertEqual((row["id"], row["shape"], row["state"], row["stages"]), ("rt-direct", "direct", "idle", []))
        self.assertEqual(row["capability"], "code")
        model = shown(panels, labels[KEY])
        self.assertEqual((model["route_count"], len(model["routes"])), (1, 1))
        self.assertEqual(model["routes"][0]["shape"], "direct")
        self.assertEqual(model["routes"][0]["kind_word"], "code")
        self.assertEqual(shown(panels, labels[KEY], lang="ko")["routes"][0]["shape"], "직접")

    def test_the_session_line_reader_counts_it_and_marks_it(self):
        labels, panels = session_line.session_route_status(snapshot(chain_node(), liveness="working"), NOW,
                                                           include_details=True)
        self.assertTrue(labels[KEY].startswith("route 1"), labels)
        self.assertEqual(panels[KEY][0]["marker"], "working")

    def test_no_live_session_no_standing_route(self):
        labels, panels = live.session_route_status(snapshot(chain_node(), liveness="dead"), NOW, include_details=True)
        self.assertEqual((labels, panels[KEY]), ({}, []))

    def test_a_finished_direct_route_retires_like_any_finished_route(self):
        snap = snapshot(chain_node(state="done", result=finished(NOW - 60)))
        labels, panels = live.session_route_status(snap, NOW, include_details=True)
        self.assertEqual(labels, {})                       # not counted: it is no current route
        (row,) = panels[KEY]
        self.assertEqual((row["state"], row["outcome"], row["shape"]), ("done", "success", "direct"))
        label = ""
        self.assertEqual([r["id"] for r in shown(panels, label, now=NOW)["routes"]], ["rt-direct"])
        # The person's next input, or ten minutes, takes it off (the rule every finished route follows).
        self.assertEqual(shown(panels, label, now=NOW, human_at=NOW - 30)["routes"], [])
        self.assertEqual(shown(panels, label, now=NOW + 601)["routes"], [])

    def test_a_declaration_older_than_this_session_process_is_not_a_standing_route(self):
        # Fleet's open-route rule (route_chain.current_capability): `ts >= session_start - slack`, the start
        # being now less the process's age. The declaration is 15 minutes old (NOW - 900).
        for state in ("open", "finish-pending"):
            for elapsed, stands in ((5, False), (12, False), (13, True), (16, True), (60, True)):
                with self.subTest(state=state, session_minutes=elapsed):
                    labels, panels = live.session_route_status(snapshot(chain_node(state=state), elapsed_min=elapsed), NOW,
                                                               include_details=True)
                    self.assertEqual(KEY in labels, stands, (labels, panels))
                    self.assertEqual(len(panels[KEY]), 1 if stands else 0)
        # Three days old, the same session started an hour ago, and read a month later: never stands.
        old = chain_node()
        old["ts"] = NOW - 3 * 86400
        for at in (NOW, NOW + 30 * 86400):
            labels, panels = live.session_route_status(snapshot(old, elapsed_min=60), at, include_details=True)
            self.assertEqual((labels, panels[KEY]), ({}, []))
        # The session line reader (the statusline's count) agrees.
        labels, panels = session_line.session_route_status(snapshot(old, liveness="working", elapsed_min=60), NOW,
                                                           include_details=True)
        self.assertEqual((labels, panels[KEY]), ({}, []))
        # And the panel shows no ROUTE for it.
        self.assertEqual(shown(panels, "")["routes"], [])

    def test_a_route_finished_in_an_earlier_run_still_retires_as_before(self):
        # Only an open declaration is tied to the process's start; a finished one keeps the finished rule.
        node = chain_node(state="done", result=finished(NOW - 60))
        node["ts"] = NOW - 3 * 86400
        _, panels = live.session_route_status(snapshot(node, elapsed_min=5), NOW, include_details=True)
        self.assertEqual([row["state"] for row in panels[KEY]], ["done"])

    def test_a_route_that_is_not_the_last_or_not_direct_adds_no_row(self):
        for node in (chain_node(shape=None), chain_node(state="unknown"), chain_node(shape="staged")):
            with self.subTest(node=node["state"] + str(node["shape"])):
                labels, panels = live.session_route_status(snapshot(node), NOW, include_details=True)
                self.assertEqual((labels, panels[KEY]), ({}, []))

    def test_staged_and_one_shot_routes_keep_their_words_and_stages(self):
        for topology, word in (("staged", "staged"), ("one-shot-owner", "one-shot-owner")):
            with self.subTest(topology=topology):
                rid = "rt-" + topology
                record = {"route_id": rid, "owner_attempt_id": "att-1", "execution_topology": topology}
                view = {"nodes": [{"id": "execute", "state": "active"}], "liveness": "working"}
                work = WorkProjection(source="route-exact", route_id=rid, _route_view={"record": record, "view": view})
                job = DispatchJob(key="owner", slug=rid, harness="claude", depth=1, route_id=rid, attempt_id="att-1",
                                  parent_sid="sid-direct", liveness="working", work_projection=work)
                labels, panels = live.session_route_status(snapshot(None, jobs=[job]), NOW, include_details=True)
                self.assertEqual(labels[KEY], "route 1 · execute")
                (row,) = panels[KEY]
                self.assertEqual((row["shape"], [s["name"] for s in row["stages"]]), (topology, ["execute"]))
                self.assertEqual(shown(panels, labels[KEY])["routes"][0]["shape"], topology)

    def test_a_route_an_entity_already_carries_is_not_added_twice(self):
        rid = "rt-direct"
        record = {"route_id": rid, "owner_attempt_id": "att-1", "execution_topology": "inline"}
        view = {"nodes": [{"id": "inline", "state": "active"}], "liveness": "working"}
        work = WorkProjection(source="route-exact", route_id=rid, _route_view={"record": record, "view": view})
        job = DispatchJob(key="owner", slug=rid, harness="claude", depth=1, route_id=rid, attempt_id="att-1",
                          parent_sid="sid-direct", liveness="working", work_projection=work)
        _, panels = live.session_route_status(snapshot(chain_node(), jobs=[job]), NOW, include_details=True)
        self.assertEqual([row["id"] for row in panels[KEY]], [rid])


if __name__ == "__main__":
    unittest.main()
