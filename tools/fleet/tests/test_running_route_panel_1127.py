"""A running route keeps its row and its stage in its session's panel, and Fleet counts it the same way (11-27).

A staged route's owner ran for an hour and a half while its session's panel showed no route: the
registry had the owner's open row and the owner was alive, but Fleet's route reading
(``explorer.live._route_records``) called the route not current in two cases:

* a stage ended FAIL or BLOCKED (``dead-worker-*``), so its node reads ``failed``, and no stage
  was running at that moment (the owner handling that result, between stages, or after the last
  stage) — the route left the panel and OVERVIEW's count while ROUTES still listed it;
* the route carried several owner attempts (a continued, resumed or relaunched owner) — no owner
  was chosen, so the route neither bound to its session nor ran by its owner.

The registry rows and the sealed route record are real files here; the dispatch collector, the
route node reading, Fleet's route records and the panel producer (``human_wait_state``) run as in
operation. Only the native observation backends are held: the process table, the native session
enrichers, herdr, and each attempt's liveness (what the shared classifier answers for an open or a
closed row). Made-up ids only.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tools.fleet import collectors, route
from tools.fleet.collectors import claude, dispatch, herdr, liveness, steward, tmux
from tools.fleet.explorer import live
from tools.fleet.model import Session

SID = "sid-native"
KEY = "claude\0" + SID
NODES = ("plan", "execute", "impl-review")


class RunningRoutePanel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rrp-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"), "HOME": str(self.root / "home"),
            "AGENT_DISPATCH_JOBS": str(self.jobs)}))
        record = {"schema_version": 1, "cwd": str(self.root), "artifact_root": str(self.root / "artifacts"),
                  "capability": "autopilot-code", "capability_mode": "dev", "execution_topology": "staged",
                  "effective_intensity": "standard", "owner_attempt_id": "-",
                  "nodes": [{"id": node, "depends_on": [NODES[i - 1]] if i else []}
                            for i, node in enumerate(NODES)]}
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":")[1][:16]
        self.record = record
        self.route_file = self.root / "route.json"
        self.route_file.write_text(json.dumps(record))
        self.lines = []
        self.live = {}           # attempt -> liveness the classifier answers for its open row
        session = Session(harness="claude", pid=os.getpid(), session_id=SID, session_tag="s1",
                          cwd=str(self.root), liveness="idle")
        self.stack.enter_context(mock.patch.object(collectors.procscan, "scan",
                                                   side_effect=lambda **kwargs: [copy.deepcopy(session)]))
        for module in (claude, tmux, steward):
            self.stack.enter_context(mock.patch.object(module, "enrich", return_value=None))
        self.stack.enter_context(mock.patch.object(herdr, "enrich", return_value={"complete": True, "agents": []}))
        self.stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
        self.stack.enter_context(mock.patch.object(
            dispatch, "_dispatch_liveness",
            side_effect=lambda job, *a, **k: "done" if job.status == "done" else self.live.get(job.attempt_id, "working")))
        self.stack.enter_context(mock.patch.object(liveness, "classify", return_value="idle"))
        # The panel producer runs in this process (``_refresh_cache``); no resident one is started.
        import human_wait_state
        self.stack.enter_context(mock.patch.object(human_wait_state, "_request_producer"))

    # -- the registry -------------------------------------------------------------------------------
    def row(self, attempt, status="running", *, owner=False, node=None, liveness_now="working", **extra):
        rid, digest, path = self.record["route_id"], self.record["route_hash"], str(self.route_file)
        # The attempt axes the dispatch wrapper writes on a registered headless row.
        meta = {"attempt_schema_version": "2", "transport": "headless", "execution_surface": "registered-headless",
                "registered_worker": "1", "fallback_hop": "same-harness-headless",
                "attempt_id": attempt, "parent_sid": SID, "parent_harness": "claude", "harness": "codex",
                "capability": "autopilot-code", "route_id": rid, "route_hash": digest, "route_file": path}
        if owner:
            meta.update(worker_type="owner", dispatch_depth="1", owner_route_id=rid, owner_route_hash=digest,
                        owner_route_file=path)
        else:
            meta.update(worker_type="stage", dispatch_depth="2", route_node=node, parent_attempt_id="att-owner")
        meta.update(extra)
        # Written now, as the registry writes it: a closed row stays a display row for its
        # afterglow (a continued owner's first turn is one beside its second).
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.lines.append(f"{stamp}\t{status}\trepo\t{self.root}\t{attempt}\t"
                          + ",".join(k + "=" + v for k, v in meta.items()))
        if status != "done":
            self.live[attempt] = liveness_now

    def observe(self):
        """``(panel rows of this route, Fleet's route record, OVERVIEW counts)`` for one pass."""
        self.jobs.write_text("\n".join(self.lines) + "\n")
        import human_wait_state as cache
        with mock.patch.object(cache, "_route_demands", return_value={KEY: time.time()}), \
                mock.patch.object(cache, "_notify_result"):
            self.assertTrue(cache._refresh_cache())
        view = cache.cached_view(request=False) or {}
        panel = [row for row in (view.get("route_panels") or {}).get(KEY) or ()
                 if row.get("id") == self.record["route_id"]]
        fleet = live.build_view_model(collectors.collect_all(include_report_verification=False))
        found = [r for r in fleet.routes if r.get("id") == self.record["route_id"]]
        counts, _opened = live.current_route_counts(fleet.routes)
        self.view = fleet
        return panel, (found[0] if found else None), counts

    @staticmethod
    def session_tag(view):
        """The number Fleet gave the one session of the fixture (the route's starter)."""
        (tag,) = {row["tag"] for row in view.sessions}
        return tag

    def assert_running(self, stage, state="working"):
        panel, fleet, counts = self.observe()
        self.assertEqual([(r["state"], r["worker_stage"]) for r in panel], [(state, stage)])
        self.assertIsNotNone(fleet)
        self.assertIs(fleet["current"], True)
        # bound to the session that started it, and OVERVIEW counts what the panel shows
        self.assertEqual(fleet["session_tag"], self.session_tag(self.view))
        self.assertEqual(counts.get(fleet["session_tag"]), 1)

    # -- the cases ----------------------------------------------------------------------------------
    def test_owner_after_a_blocked_stage_keeps_the_row_and_the_stage(self):
        self.row("att-owner", owner=True)
        self.row("att-plan", "done", node="plan")
        self.row("att-execute", "done", node="execute", note="dead-worker-blocked", failure_class="blocked")
        self.assert_running("execute")

    def test_a_parked_owner_after_a_failed_stage_keeps_the_row(self):
        self.row("att-owner", owner=True, liveness_now="idle")
        self.row("att-plan", "done", node="plan", note="dead-worker-fail", failure_class="fail")
        self.assert_running("plan", state="idle")

    def test_a_continued_route_binds_and_runs_by_its_current_owner(self):
        self.row("att-owner-1", "done", owner=True)
        self.row("att-owner", owner=True)
        self.row("att-plan", "done", node="plan")
        self.assert_running("execute")
        self.row("att-execute", node="execute")
        self.assert_running("execute")

    def test_owner_finishing_after_the_last_stage_keeps_the_row(self):
        self.row("att-owner", owner=True)
        for node in NODES:
            self.row("att-" + node, "done", node=node)
        self.assert_running("impl-review")

    def test_a_stage_running_under_its_owner_is_shown_as_before(self):
        self.row("att-owner", owner=True)
        self.row("att-plan", node="plan")
        self.assert_running("plan")

    def test_a_long_remote_training_shows_its_host_and_time_under_a_parked_owner(self):
        # The route's lab stage launched a training on a compute host thirty hours ago; its owner is
        # parked waiting for it. The run is a real resource registry row (the recorded receipt says it
        # runs); placement and remote progress reads are held, so no host is reached.
        import resource_placement
        import resource_progress
        import resource_run_registry as runs
        record = {key: value for key, value in self.record.items() if key not in ("route_hash", "route_id")}
        record["nodes"] = [{"id": "plan", "depends_on": []},
                           {"id": "lab-run", "depends_on": ["plan"], "unit": "_kernel/resource"}]
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":")[1][:16]
        self.record = record
        self.route_file = self.root / (record["route_id"] + ".json")
        self.route_file.write_text(json.dumps(record))
        self.row("att-owner", owner=True, liveness_now="idle")
        self.row("att-plan", "done", node="plan")
        receipt = {"kind": "compute-hosts", "run_id": "compute-1", "host": "gpu-b.lab", "receipt_state": "running",
                   **{name + "_sha256": name[0] * 64 for name in ("meta", "exit", "receipt", "outputs", "contract")}}
        registry, index = self.root / "resource-runs.json", self.root / "resource-index.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"train-1": {
            "execution": receipt, "cwd": str(self.root), "status": "running", "resource_policy": "supervised-owner",
            "route": str(self.route_file), "node": "lab-run", "route_id": record["route_id"],
            "parent_attempt_id": "att-owner", "started_at": time.time() - 30 * 3600,
            "command": ["python3", "train.py"]}}}))
        runs.register_registry(registry, index)
        with mock.patch.dict(os.environ, {"AGENT_RESOURCE_RUN_INDEX": str(index)}), \
                mock.patch.object(resource_placement, "observe", return_value=None), \
                mock.patch.object(resource_progress, "collect", return_value=None), \
                mock.patch.object(resource_progress, "remote_candidates", return_value=[]):
            panel, fleet, _counts = self.observe()
        self.assertEqual([(row["worker_stage"], [(s["name"], s["st"]) for s in row["stages"]]) for row in panel],
                         [("lab-run", [("plan", "done"), ("lab-run", "active")])])
        worker = "".join(str(piece[0]) for piece in panel[0]["worker"])
        self.assertIn("gpu-b · train.py", worker)     # where it runs, as its receipt names it
        self.assertIn("running 1d 6h", worker)
        self.assertIs(fleet["current"], True)

    def test_ended_or_stopped_routes_do_not_stay(self):
        # The owner's row closed: the route is over (its result row is the panel's for ten
        # minutes only when the route recorded one; here it has none).
        self.row("att-owner", "done", owner=True)
        for node in NODES:
            self.row("att-" + node, "done", node=node)
        panel, fleet, counts = self.observe()
        self.assertEqual(panel, [])
        self.assertTrue(fleet is None or fleet["current"] is False)
        self.assertEqual(counts, {})
        # The owner's row is still open but its attempt is gone: nothing of the route runs.
        self.lines.clear()
        self.row("att-owner", owner=True, liveness_now="dead")
        self.row("att-plan", "done", node="plan", note="dead-worker-blocked", failure_class="blocked")
        panel, fleet, counts = self.observe()
        self.assertEqual(panel, [])
        self.assertIs(fleet["current"], False)
        self.assertEqual(counts, {})


class FramedLegRoute(unittest.TestCase):
    """A staged leg its frame opened, after the frame closed: the leg runs as itself (11-27)."""

    def test_leg_after_a_finished_frame_keeps_its_row_after_a_blocked_stage(self):
        rid = "rt-legfixture00001"
        view = {"nodes": [{"id": "plan", "state": "done"}, {"id": "execute", "state": "failed"},
                          {"id": "impl-review", "state": "pending"}],
                "capability": "autopilot-code", "capability_mode": "dev", "execution_topology": "staged",
                "effective_intensity": "standard", "cwd": "/fixture-wt/leg", "slug": "leg"}
        record = {"route_id": rid, "capability": "autopilot-code", "capability_mode": "dev",
                  "execution_topology": "staged", "selection": {"shape": "staged"}, "owner_attempt_id": "-"}
        owner = {"key": "route-leg", "depth": 1, "dispatch_depth": 1, "route_node": None, "worker_type": "owner",
                 "harness": "codex", "route_id": rid, "attempt_id": "att-owner", "status": "open",
                 "liveness": "working", "parent_sid": SID, "elapsed_min": 90}
        chain = [{"route_id": "rt-frame", "capability": "route-frame", "state": "done"},
                 {"route_id": rid, "capability": "autopilot-code", "state": "active",
                  "route_plan": {"decision": "frame/route-decision.json", "digest": "sha256:" + "0" * 64, "index": 0}}]
        session = {"harness": "claude", "session_id": SID, "session_tag": "s1", "cwd": "/fixture-wt/leg",
                   "liveness": "idle", "is_child": False, "title": "supervisor", "route_chain": {"nodes": chain}}
        slot = {"view": view, "record": record, "jobs": [owner], "sessions": [session]}
        now = time.time()
        with mock.patch.object(live, "_collect_routes", return_value={rid: slot}):
            rows, index = live._session_records({"sessions": [session]}, now, lambda _cwd: "repo")
            routes, _nodes = live._route_records({"resources": []}, rows, index, lambda _cwd: "repo", now)
        leg = next(r for r in routes if r["id"] == rid)
        self.assertEqual((leg["current"], leg["state"], leg["worker_stage"], leg["session_tag"]),
                         (True, "working", "execute", "s1"))

    def test_a_continued_route_out_of_its_session_chain_stays_the_sessions(self):
        # The session chain keeps its last sixteen routes (``route_chain.MAX_NODES``): a long-lived
        # supervisor's older route is no longer in it. Its two owner attempts both name the session.
        rid = "rt-oldfixture00001"
        view = {"nodes": [{"id": "plan", "state": "done"}, {"id": "execute", "state": "pending"}],
                "capability": "autopilot-code", "capability_mode": "dev", "execution_topology": "staged",
                "cwd": "/fixture-wt/old", "slug": "old"}
        record = {"route_id": rid, "capability": "autopilot-code", "execution_topology": "staged",
                  "owner_attempt_id": "-"}
        owner = lambda attempt, liveness, status: {  # noqa: E731
            "key": "route-old", "depth": 1, "dispatch_depth": 1, "route_node": None, "worker_type": "owner",
            "harness": "codex", "route_id": rid, "attempt_id": attempt, "status": status,
            "liveness": liveness, "parent_sid": SID, "elapsed_min": 30}
        session = {"harness": "claude", "session_id": SID, "session_tag": "s1", "cwd": "/fixture-wt/old",
                   "liveness": "idle", "is_child": False, "title": "supervisor"}
        jobs = [owner("att-owner-1", "done", "done"), owner("att-owner-2", "working", "open")]
        slot = {"view": view, "record": record, "jobs": jobs, "sessions": []}
        now = time.time()
        with mock.patch.object(live, "_collect_routes", return_value={rid: slot}):
            rows, index = live._session_records({"sessions": [session]}, now, lambda _cwd: "repo")
            routes, _nodes = live._route_records({"resources": []}, rows, index, lambda _cwd: "repo", now)
        route_row = next(r for r in routes if r["id"] == rid)
        self.assertEqual((route_row["current"], route_row["state"], route_row["worker_stage"],
                          route_row["session_tag"]), (True, "working", "execute", "s1"))
        # Which attempt is the owner is still not guessed for its display.
        self.assertIsNone(route_row["harness"])


if __name__ == "__main__":
    unittest.main()
