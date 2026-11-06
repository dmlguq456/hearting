"""Session-local badges consume OVERVIEW's exact route projection."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.fleet.explorer import live
from tools.fleet.model import DispatchJob, Session, WorkProjection
from tools.fleet.collectors import dispatch


class SessionRouteStatusTests(unittest.TestCase):
    def snapshot(self, count=1, state="working", stage="frame"):
        session = Session(harness="claude", pid=os.getpid(), session_id="sid-current", session_tag="s1",
                          session_aliases=["sid-before"], cwd="/fixture", liveness="idle")
        jobs = []
        for index in range(count):
            rid = "rt-" + str(index)
            nodes = [{"id": stage, "state": "done" if state == "done" else "active"}]
            record = {"route_id": rid, "owner_attempt_id": "att-" + str(index)}
            view = {"nodes": nodes, "liveness": "unknown" if state == "stale" else "working"}
            work = WorkProjection(source="route-exact", route_id=rid,
                                  _route_view={"record": record, "view": view})
            jobs.append(DispatchJob(key="owner", slug=rid, harness="claude", depth=1,
                route_id=rid, attempt_id=record["owner_attempt_id"], parent_sid="sid-current",
                liveness="working" if state == "stale" else state, work_projection=work))
        return {"sessions": [session], "jobs": jobs, "resources": []}

    def test_zero_one_multiple_and_closed_follow_overview(self):
        for count in (0, 1, 3):
            for state in ("working", "idle", "blocked", "done", "stale", "dead"):
                with self.subTest(count=count, state=state):
                    snapshot = self.snapshot(count, state)
                    vm = live.build_view_model(snapshot)
                    counts, opened = live.current_route_counts(vm.routes)
                    labels = live.session_route_status(snapshot)
                    expected = f"⏳ route {counts['s1']} · frame" if "s1" in opened else None
                    self.assertEqual(labels.get("claude\0sid-current"), expected)
                    self.assertEqual(labels.get("claude\0sid-before"), expected)
                    self.assertNotIn("opencode\0sid-current", labels)

    def test_mixed_routes_and_untrusted_stage_are_safe(self):
        snapshot = self.snapshot(2)
        snapshot["jobs"][1].work_projection._route_view["view"]["nodes"][0]["id"] = "execute"
        self.assertEqual(live.session_route_status(snapshot)["claude\0sid-current"],
                         "⏳ route 2 · frame, execute")
        snapshot["jobs"][1].work_projection._route_view["view"]["nodes"][0]["id"] = "\x1b[31mPRIVATE\n"
        self.assertEqual(live.session_route_status(snapshot)["claude\0sid-current"], "⏳ route 2 · frame")

    def test_scoped_rows_reuse_existing_job_and_evidence_interpreters(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text("\n".join([
                "2026-11-06T00:00:00Z\trunning\trepo\t/wt\towner\tcapability=autopilot-code,attempt_id=att-1,parent_sid=sid-before,route_id=rt-a,route_node=execute,harness=claude",
                "2026-11-06T00:00:01Z\tdone\trepo\t/wt\tchild\tcapability=autopilot-code,attempt_id=att-2,parent_attempt_id=att-1,route_id=rt-a,route_node=plan,harness=codex",
                "2026-11-06T00:00:00Z\trunning\trepo\t/wt\tother\tcapability=autopilot-code,attempt_id=att-other,parent_sid=foreign,route_id=rt-b,route_node=execute,harness=claude",
            ]) + "\n")
            session = self.snapshot()["sessions"][0]
            with dispatch._dispatch_registry_cache.registry_read_scope():
                full = dispatch.parsed_registry_rows(jobs)
                expected = tuple(row for row in full if row[2].get("route_id") == "rt-a")
                with mock.patch("tools.fleet.route_chain.session_lines", return_value=[]), \
                     dispatch.route_display_read_scope([session]):
                    selected = dispatch.parsed_registry_rows(jobs)
                    self.assertEqual(selected, expected)
                    actual_jobs, malformed = dispatch._scan_jobs_log(jobs, set(), include_stall=False)
                    actual_evidence = dispatch._registry_evidence_for_path(jobs, selected)
                self.assertEqual(malformed, 0)
                # Same row materialization, without interpreting unrelated route history.
                with mock.patch.object(dispatch, "parsed_registry_rows", return_value=expected):
                    expected_jobs, _ = dispatch._scan_jobs_log(jobs, set(), include_stall=False)
                self.assertEqual([j.attempt_id for j in actual_jobs], [j.attempt_id for j in expected_jobs])
                self.assertEqual(actual_evidence, dispatch._registry_evidence_for_path(jobs, expected))
                self.assertEqual(dispatch.parsed_registry_rows(jobs), full)

    def test_route_family_is_discovered_before_cross_registry_worker_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            child, owner = Path(tmp) / "canonical.log", Path(tmp) / "legacy.log"
            child.write_text("2026-11-06T00:00:00Z\trunning\trepo\t/wt\tchild\tattempt_id=att-child,route_id=rt-family,route_node=execute\n")
            owner.write_text("2026-11-06T00:00:00Z\trunning\trepo\t/wt\towner\tattempt_id=att-owner,parent_sid=sid-before,route_id=rt-family\n")
            with dispatch._dispatch_registry_cache.registry_read_scope(), \
                 mock.patch("tools.fleet.route_chain.session_lines", return_value=[]), \
                 dispatch.route_display_read_scope(self.snapshot()["sessions"]):
                scope = dispatch._ROUTE_DISPLAY_ROWS.get()
                dispatch._prime_route_display_rows([child, owner], scope)
                self.assertEqual(dispatch.parsed_registry_rows(child)[0][2]["attempt_id"], "att-child")

    def test_handed_over_owner_uses_the_shared_holder_interpreter(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.log"
            path.write_text("2026-11-06T00:00:00Z\trunning\trepo\t/wt\towner\tattempt_schema_version=2,dispatch_depth=1,worker_type=owner,attempt_id=att-owner,parent_sid=sid-elsewhere,route_id=rt-handed\n")
            with dispatch._dispatch_registry_cache.registry_read_scope(), \
                 mock.patch("tools.fleet.route_chain.session_lines", return_value=[]), \
                 mock.patch("dispatch_seat_handover.resolve_recipient", return_value=SimpleNamespace(
                     harness="claude", session_id="sid-current")) as holder, \
                 dispatch.route_display_read_scope(self.snapshot()["sessions"]):
                selected = dispatch.parsed_registry_rows(path)
                self.assertEqual(selected[0][2]["attempt_id"], "att-owner")
                self.assertEqual(holder.call_args.args, ("job-holder",))

    def test_scoped_collector_and_display_producer_match_the_full_route_projection(self):
        import copy
        import json
        import time
        from contextlib import ExitStack
        import human_wait_state as cache
        from tools.fleet import collectors, route
        from tools.fleet.collectors import claude, herdr, tmux, steward, liveness
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            jobs = root / "jobs.log"
            stack.enter_context(mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(root / "state"), "HOME": str(root / "home"),
                "AGENT_DISPATCH_JOBS": str(jobs)}))
            record = {"schema_version": 1, "nodes": [{"id": "execute", "depends_on": []}],
                "cwd": str(root), "artifact_root": str(root / "artifacts"),
                "capability": "autopilot-code", "owner_attempt_id": "att-display"}
            record["route_hash"] = route.route_hash(record)
            record["route_id"] = "rt-" + record["route_hash"].split(":")[1][:16]
            path = root / "route.json"
            path.write_text(json.dumps(record))
            metadata = {"attempt_id": "att-display", "parent_sid": "sid-native",
                "parent_harness": "claude", "harness": "claude", "capability": "autopilot-code",
                "worker_type": "owner", "dispatch_depth": "1", "route_node": "execute",
                "route_id": record["route_id"], "route_hash": record["route_hash"], "route_file": str(path),
                "owner_route_id": record["route_id"], "owner_route_hash": record["route_hash"],
                "owner_route_file": str(path)}
            jobs.write_text(f"{time.time()}\trunning\trepo\t{root}\towner\t"
                            + ",".join(k + "=" + v for k, v in metadata.items()) + "\n")
            session = Session(harness="claude", pid=os.getpid(), session_id="sid-native",
                              session_tag="s1", cwd=str(root), liveness="idle")
            # Only native observation backends are controlled; row selection,
            # normalization, parent/route projection and cache publication run.
            stack.enter_context(mock.patch.object(collectors.procscan, "scan",
                side_effect=lambda **kwargs: [copy.deepcopy(session)]))
            for module in (claude, tmux, steward):
                stack.enter_context(mock.patch.object(module, "enrich", return_value=None))
            stack.enter_context(mock.patch.object(herdr, "enrich", return_value={"complete": True, "agents": []}))
            stack.enter_context(mock.patch.object(dispatch, "_scan_processes", return_value=[]))
            stack.enter_context(mock.patch.object(dispatch, "_dispatch_liveness", return_value="working"))
            stack.enter_context(mock.patch.object(liveness, "classify", return_value="idle"))
            expected = {"claude\0sid-native": "⏳ route 1 · execute"}
            for requested in (None, [("claude", "sid-native")]):
                snapshot = collectors.collect_all(route_sessions=requested, include_report_verification=False)
                self.assertTrue(snapshot.observations["basic"]["all_new"])
                self.assertEqual(live.session_route_status(snapshot), expected)
            stack.enter_context(mock.patch.object(cache, "_route_demands",
                return_value={"claude\0sid-native": time.time()}))
            stack.enter_context(mock.patch.object(cache, "_notify_result"))
            self.assertTrue(cache._refresh_cache())
            self.assertEqual(cache.cached_statusline("claude", "sid-native"), ((0, 0), expected["claude\0sid-native"]))
