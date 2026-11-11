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
                    expected = f"route {counts['s1']} · frame" if "s1" in opened else None
                    self.assertEqual(labels.get("claude\0sid-current"), expected)
                    self.assertEqual(labels.get("claude\0sid-before"), expected)
                    self.assertNotIn("opencode\0sid-current", labels)

    def test_mixed_routes_and_untrusted_stage_are_safe(self):
        snapshot = self.snapshot(2)
        snapshot["jobs"][1].work_projection._route_view["view"]["nodes"][0]["id"] = "execute"
        self.assertEqual(live.session_route_status(snapshot)["claude\0sid-current"],
                         "route 2 · frame, execute")
        snapshot["jobs"][1].work_projection._route_view["view"]["nodes"][0]["id"] = "\x1b[31mPRIVATE\n"
        self.assertEqual(live.session_route_status(snapshot)["claude\0sid-current"], "route 2 · frame")

    def test_shell_detail_reuses_the_same_interpretation_and_only_actual_stages(self):
        snapshot = self.snapshot(stage="inline")
        with mock.patch.object(live, "_route_records", wraps=live._route_records) as reader:
            labels, panels = live.session_route_status(snapshot, include_details=True)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(labels, live.session_route_status(snapshot))
        rows = panels["claude\0sid-current"]
        self.assertEqual(rows, panels["claude\0sid-before"])
        self.assertEqual([stage["name"] for stage in rows[0]["stages"]], ["inline"])
        self.assertNotIn("opencode\0sid-current", panels)

    def test_shell_detail_carries_what_routes_unfolds_from_the_same_records(self):
        # Spec rule 21: kind, owner, each stage's doer and box, workers and age come from
        # the very route records and node boxes ROUTES draws; nothing is read again.
        snapshot = self.snapshot(stage="execute")
        view = snapshot["jobs"][0].work_projection._route_view["view"]
        view["nodes"] = [{"id": "plan", "state": "done", "harness": "codex", "model": "gpt-fixture",
                          "effort": "high", "elapsed_min": 12, "unit": "plan/code"},
                         {"id": "execute", "state": "active", "harness": "claude", "unit": "dev/backend",
                          "route_attempt_round": 2},
                         {"id": "review", "state": "todo"}]
        view.update(capability="autopilot-code", capability_mode="dev", effective_intensity="standard")
        with mock.patch.object(live, "_route_records", wraps=live._route_records) as reader:
            _, panels = live.session_route_status(snapshot, include_details=True)
        self.assertEqual(reader.call_count, 1)
        row = panels["claude\0sid-current"][0]
        routes, nodes = live._route_records(snapshot, *live._session_records(snapshot, None, live._resolver(snapshot))[:2],
                                            live._resolver(snapshot), __import__("time").time())
        route = routes[0]
        self.assertEqual([stage["name"] for stage in row["stages"]], ["plan", "execute", "review"])
        plan, execute = row["stages"][0], row["stages"][1]
        self.assertEqual((plan["h"], plan["m"], plan["e"], plan["el"]), ("codex", "gpt-fixture", "high", "12m"))
        self.assertEqual(plan["unit"], "plan/code")
        self.assertEqual(execute.get("r"), 2)
        self.assertEqual(row["mode"], route["mode"])
        self.assertEqual(row["intensity"], route["intensity"])
        self.assertEqual(row["legs"], len(route["legs"]))
        boxes = {item["n"]: item for group in nodes[route["id"]] for item in group.get("detail") or ()}
        self.assertEqual(execute.get("verdict"), boxes["execute"]["verdict"])
        from tools.fleet import session_panel
        shown = session_panel.presentation({}, {"route_panels": panels}, "claude", "sid-current", 40)["routes"][0]
        self.assertEqual(shown["kind_text"], "code(dev·std)")
        self.assertEqual([stage["mark"] for stage in shown["stages"]], ["✓", "●", ""])
        self.assertEqual(shown["stages"][0]["harness"], "codex")
        self.assertEqual(shown["stages"][1]["round"], 2)

    def test_shell_carries_exact_route_visibility_without_another_read(self):
        snapshot = self.snapshot(stage="inline")
        interpret = live._route_records
        evidence = {"schema": "route_visibility_v1", "route_id": "rt-0",
                    "summary": "direct · CI reported", "next": "closed",
                    "lines": [["CI", "CI fixture PASS same-head", "good"]]}
        def with_visibility(*args):
            rows = interpret(*args)
            for row in rows:
                row["visibility"] = evidence
            return rows
        with mock.patch.object(live, "_route_records", side_effect=with_visibility) as read:
            _, panels = live.session_route_status(snapshot, include_details=True)
            self.assertEqual(read.call_count, 1)
            actual = panels["claude\0sid-current"][0]["visibility"]
            self.assertEqual(actual["lines"], evidence["lines"])
            evidence["route_id"] = "rt-other"
            _, other = live.session_route_status(snapshot, include_details=True)
            self.assertNotIn("visibility", other["claude\0sid-current"][0])

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
            expected = {"claude\0sid-native": "route 1 · execute"}
            for requested in (None, [("claude", "sid-native")]):
                snapshot = collectors.collect_all(route_sessions=requested, include_report_verification=False)
                self.assertTrue(snapshot.observations["basic"]["all_new"])
                self.assertEqual(live.session_route_status(snapshot), expected)
            stack.enter_context(mock.patch.object(cache, "_route_demands",
                return_value={"claude\0sid-native": time.time()}))
            stack.enter_context(mock.patch.object(cache, "_notify_result"))
            self.assertTrue(cache._refresh_cache())
            self.assertEqual(cache.cached_statusline("claude", "sid-native"), ((0, 0), expected["claude\0sid-native"]))
