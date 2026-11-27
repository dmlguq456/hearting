"""A running route keeps its row and its stage in its session's panel, and Fleet counts it the same way (11-27).

A staged route's owner ran for an hour and a half while its session's panel showed no route: the
registry had the owner's open row and the owner was alive, but Fleet's route reading
(``explorer.live._route_records``) called the route not current in two cases:

* a stage ended FAIL or BLOCKED (``dead-worker-*``), so its node reads ``failed``, and no stage
  was running at that moment (the owner handling that result, between stages, or after the last
  stage) — the route left the panel and OVERVIEW's count while ROUTES still listed it;
* the route carried several owner attempts (a continued, resumed or relaunched owner) — no owner
  liveness was read at all.

The route now runs while its owner's registry row is open and the attempt is alive, unless the
route is closed (its outcome's ``closed_at``): a closed route, a closed owner row, or several live
owner attempts read exactly as before.

The registry rows, the sealed route record and its outcome sidecar are real files here; the
dispatch collector, the route node reading, Fleet's route records and the panel producer
(``human_wait_state``) run as in operation. Only the native observation backends are held: the
process table, the native session enrichers, herdr, and each attempt's liveness (what the shared
classifier answers for its row). Made-up ids only.
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
from tools.fleet.explorer.tables import route_is_history
from tools.fleet.model import Session

SID = "sid-native"
KEY = "claude\0" + SID
NODES = ("plan", "execute", "impl-review")
DAY = 86400


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
        self.seal([{"id": node, "depends_on": [NODES[i - 1]] if i else []} for i, node in enumerate(NODES)])
        self.lines = []
        self.live = {}           # attempt -> liveness the classifier answers for its row
        self.revived = set()     # closed rows a live process contradicts (F-64)
        session = Session(harness="claude", pid=os.getpid(), session_id=SID, session_tag="s1",
                          cwd=str(self.root), liveness="idle")
        self.stack.enter_context(mock.patch.object(collectors.procscan, "scan",
                                                   side_effect=lambda **kwargs: [copy.deepcopy(session)]))
        for module in (claude, tmux, steward):
            self.stack.enter_context(mock.patch.object(module, "enrich", return_value=None))
        self.stack.enter_context(mock.patch.object(herdr, "enrich", return_value={"complete": True, "agents": []}))
        self.stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
        self.stack.enter_context(mock.patch.object(
            dispatch, "_terminal_row_process_alive",
            side_effect=lambda meta, *a, **k: meta.get("attempt_id") in self.revived))
        self.stack.enter_context(mock.patch.object(
            dispatch, "_dispatch_liveness",
            side_effect=lambda job, *a, **k: self.live.get(job.attempt_id, "done")))
        self.stack.enter_context(mock.patch.object(liveness, "classify", return_value="idle"))
        # The panel producer runs in this process (``_refresh_cache``); no resident one is started.
        import human_wait_state
        self.stack.enter_context(mock.patch.object(human_wait_state, "_request_producer"))

    # -- the route and its registry -----------------------------------------------------------------
    def seal(self, nodes):
        """Write the sealed route record where the runtime keeps it (``<root>/.runtime/routes``)."""
        record = {"schema_version": 1, "cwd": str(self.root), "artifact_root": str(self.root / "artifacts"),
                  "capability": "autopilot-code", "capability_mode": "dev", "execution_topology": "staged",
                  "effective_intensity": "standard", "owner_attempt_id": "-", "slug": "fixture-route",
                  "nodes": nodes}
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":")[1][:16]
        self.record = record
        self.route_file = self.root / "artifacts" / ".runtime" / "routes" / (record["route_id"] + ".json")
        self.route_file.parent.mkdir(parents=True, exist_ok=True)
        self.route_file.write_text(json.dumps(record))

    def close_route(self, proven):
        """The completion controller's (or ``capability-route close``'s) outcome sidecar."""
        self.route_file.with_name(self.route_file.stem + ".outcome.json").write_text(json.dumps({
            "route_id": self.record["route_id"], "route_hash": self.record["route_hash"],
            "terminal_gate_proven": proven,
            "closed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))

    def row(self, attempt, status="running", *, owner=False, node=None, liveness_now="working",
            harness="codex", age=0, **extra):
        rid, digest, path = self.record["route_id"], self.record["route_hash"], str(self.route_file)
        # The attempt axes the dispatch wrapper writes on a registered headless row.
        meta = {"attempt_schema_version": "2", "transport": "headless", "execution_surface": "registered-headless",
                "registered_worker": "1", "fallback_hop": "same-harness-headless",
                "attempt_id": attempt, "parent_sid": SID, "parent_harness": "claude", "harness": harness,
                "capability": "autopilot-code", "route_id": rid, "route_hash": digest, "route_file": path}
        if owner:
            meta.update(worker_type="owner", dispatch_depth="1", owner_route_id=rid, owner_route_hash=digest,
                        owner_route_file=path)
        else:
            meta.update(worker_type="stage", dispatch_depth="2", route_node=node, parent_attempt_id="att-owner")
        meta.update(extra)
        # Written ``age`` seconds ago, as the registry writes it: a closed row stays a display row
        # for its afterglow (a continued owner's first turn is one beside its second).
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
        self.lines.append(f"{stamp}\t{status}\trepo\t{self.root}\t{attempt}\t"
                          + ",".join(k + "=" + v for k, v in meta.items()))
        self.live[attempt] = "done" if status == "done" and attempt not in self.revived else liveness_now

    def observe(self):
        """What the four places show for this route in one pass: the panel's rows, OVERVIEW's count
        for the session, whether ROUTES lists it among the active routes, and the statusline."""
        self.jobs.write_text("\n".join(self.lines) + "\n")
        import human_wait_state as cache
        with mock.patch.object(cache, "_route_demands", return_value={KEY: time.time()}), \
                mock.patch.object(cache, "_notify_result"):
            self.assertTrue(cache._refresh_cache())
        view = cache.cached_view(request=False) or {}
        panel = [row for row in (view.get("route_panels") or {}).get(KEY) or ()
                 if row.get("id") == self.record["route_id"]]
        fleet = live.build_view_model(collectors.collect_all(include_report_verification=False))
        found = next((r for r in fleet.routes if r.get("id") == self.record["route_id"]), None)
        counts, _opened = live.current_route_counts(fleet.routes)
        (tag,) = {row["tag"] for row in fleet.sessions}      # the fixture's one session
        return {"panel": panel, "route": found, "overview": counts.get(tag, 0), "tag": tag,
                "routes_active": int(found is not None and not route_is_history(found)),
                "statusline": cache.cached_statusline("claude", SID)[1]}

    def assert_running(self, stage, state="working"):
        seen = self.observe()
        self.assertEqual([(r["state"], r["worker_stage"]) for r in seen["panel"]], [(state, stage)])
        self.assertIs(seen["route"]["current"], True)
        # bound to the session that started it, and OVERVIEW and the statusline count what the panel shows
        self.assertEqual(seen["route"]["session_tag"], seen["tag"])
        self.assertEqual((seen["overview"], seen["routes_active"]), (1, 1))
        self.assertTrue(str(seen["statusline"]).startswith("route 1"), seen["statusline"])
        return seen

    def assert_over(self, routes_active):
        """Shown as before the change: no panel row, no OVERVIEW count, no statusline route."""
        seen = self.observe()
        self.assertEqual(seen["panel"], [])
        self.assertTrue(seen["route"] is None or seen["route"]["current"] is False)
        self.assertEqual((seen["overview"], seen["routes_active"]), (0, routes_active))
        self.assertFalse(str(seen["statusline"] or "").startswith("route"), seen["statusline"])
        return seen

    @staticmethod
    def detail_owner(route_row):
        return (route_row.get("visibility") or {}).get("codes", {}).get("owner", {})

    # -- the route runs -----------------------------------------------------------------------------
    def test_owner_after_a_blocked_stage_keeps_the_row_and_the_stage(self):
        self.row("att-owner", owner=True)
        self.row("att-plan", "done", node="plan")
        self.row("att-execute", "done", node="execute", note="dead-worker-blocked", failure_class="blocked")
        self.assert_running("execute")

    def test_a_parked_owner_after_a_failed_stage_keeps_the_row(self):
        self.row("att-owner", owner=True, liveness_now="idle")
        self.row("att-plan", "done", node="plan", note="dead-worker-fail", failure_class="fail")
        self.assert_running("plan", state="idle")

    def test_an_owner_waiting_for_a_person_for_three_days_keeps_the_row(self):
        self.row("att-owner", owner=True, liveness_now="blocked", age=3 * DAY)
        self.row("att-plan", "done", node="plan", age=3 * DAY)
        self.row("att-execute", "done", node="execute", note="dead-worker-blocked", failure_class="blocked",
                 age=3 * DAY)
        self.assert_running("execute", state="blocked")

    def test_a_continued_route_runs_by_its_one_live_owner(self):
        self.row("att-owner-1", "done", owner=True)
        self.row("att-owner", owner=True)
        self.row("att-plan", "done", node="plan")
        self.assert_running("execute")
        self.row("att-execute", node="execute")
        self.assert_running("execute")

    def test_an_earlier_owner_closed_later_does_not_take_the_detail(self):
        # The Codex owner runs; the OpenCode owner it replaced wrote its `done` row afterwards.
        self.row("att-owner", owner=True, harness="codex")
        self.row("att-owner-0", "done", owner=True, harness="opencode")
        self.row("att-plan", "done", node="plan")
        seen = self.assert_running("execute")
        for shown in (seen["route"], seen["panel"][0]):
            owner = self.detail_owner(shown)
            self.assertEqual((owner.get("attempt_id"), owner.get("harness"), owner.get("liveness")),
                             ("att-owner", "codex", "working"))
        # Which attempt is the owner is still not guessed for the row's own owner cell.
        self.assertIsNone(seen["route"]["harness"])

    def test_owner_finishing_after_the_last_stage_keeps_the_row(self):
        self.row("att-owner", owner=True)
        for node in NODES:
            self.row("att-" + node, "done", node=node)
        self.assert_running("impl-review")

    def test_a_stage_running_under_its_owner_is_shown_as_before(self):
        self.row("att-owner", owner=True)
        self.row("att-plan", node="plan")
        self.assert_running("plan")

    def test_a_long_remote_training_under_a_parked_owner_is_shown_as_before(self):
        # The lab stage launched a training on a compute host thirty hours ago; its owner is parked.
        # The run is a real resource registry row (its recorded receipt says it runs); placement and
        # remote progress reads are held, so no host is reached.
        import resource_placement
        import resource_progress
        import resource_run_registry as runs
        self.seal([{"id": "plan", "depends_on": []},
                   {"id": "lab-run", "depends_on": ["plan"], "unit": "_kernel/resource"}])
        self.row("att-owner", owner=True, liveness_now="idle")
        self.row("att-plan", "done", node="plan")
        receipt = {"kind": "compute-hosts", "run_id": "compute-1", "host": "gpu-b", "receipt_state": "running",
                   **{name + "_sha256": name[0] * 64 for name in ("meta", "exit", "receipt", "outputs", "contract")}}
        registry, index = self.root / "resource-runs.json", self.root / "resource-index.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"train-1": {
            "execution": receipt, "cwd": str(self.root), "status": "running", "resource_policy": "supervised-owner",
            "route": str(self.route_file), "node": "lab-run", "route_id": self.record["route_id"],
            "parent_attempt_id": "att-owner", "started_at": time.time() - 30 * 3600,
            "command": ["python3", "train.py"]}}}))
        runs.register_registry(registry, index)
        with mock.patch.dict(os.environ, {"AGENT_RESOURCE_RUN_INDEX": str(index)}), \
                mock.patch.object(resource_placement, "observe", return_value=None), \
                mock.patch.object(resource_progress, "collect", return_value=None), \
                mock.patch.object(resource_progress, "remote_candidates", return_value=[]):
            seen = self.assert_running("lab-run", state="idle")
        self.assertEqual([(s["name"], s["st"]) for s in seen["panel"][0]["stages"]],
                         [("plan", "done"), ("lab-run", "active")])
        self.assertIn("running 1d 6h", "".join(str(piece[0]) for piece in seen["panel"][0]["worker"]))

    # -- the route is over --------------------------------------------------------------------------
    def test_a_closed_route_whose_owner_process_lingers_is_over(self):
        # The owner's row is still open and its attempt reads alive, but the route is closed: cancelled
        # (closed with an unproven gate), parked behind a held supervisor lease, or a Codex owner whose
        # process outlived its last `turn.completed`. Each reads as before the change.
        for name, owner_liveness, proven in (("cancelled, process alive", "working", False),
                                             ("lease still held", "idle", True),
                                             ("codex process after its last turn", "working", True)):
            with self.subTest(name):
                self.lines.clear()
                self.row("att-owner", owner=True, liveness_now=owner_liveness)
                for node in NODES:
                    self.row("att-" + node, "done", node=node)
                self.close_route(proven)
                seen = self.assert_over(routes_active=1)
                self.assertIs(seen["route"]["closed"], True)

    def test_a_cancelled_owner_row_a_live_process_contradicts_does_not_revive_the_route(self):
        # F-64: the replacement owner's row is `cancelled` but a live process carries its attempt, so it
        # reads `working`; beside it the earlier owner's `done` row. No open owner row runs the route.
        self.revived.add("att-owner")
        self.row("att-owner-1", "done", owner=True)
        self.row("att-owner", "cancelled", owner=True, liveness_now="working")
        self.row("att-plan", "done", node="plan")
        self.assert_over(routes_active=1)

    def test_two_live_open_owner_attempts_are_not_guessed(self):
        self.row("att-owner-1", owner=True)
        self.row("att-owner", owner=True)
        self.row("att-plan", "done", node="plan")
        self.row("att-execute", "done", node="execute", note="dead-worker-blocked", failure_class="blocked")
        self.assert_over(routes_active=0)              # as before: a failed stage and no owner read

    def test_ended_or_stopped_routes_do_not_stay(self):
        # The owner's row closed: the route is over (its result row is the panel's for ten
        # minutes only when the route recorded one; here it has none).
        self.row("att-owner", "done", owner=True)
        for node in NODES:
            self.row("att-" + node, "done", node=node)
        self.assert_over(routes_active=0)
        # The owner's row is still open but its attempt is gone: nothing of the route runs.
        self.lines.clear()
        self.row("att-owner", owner=True, liveness_now="dead")
        self.row("att-plan", "done", node="plan", note="dead-worker-blocked", failure_class="blocked")
        self.assert_over(routes_active=0)


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


if __name__ == "__main__":
    unittest.main()
