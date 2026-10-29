"""Exact collected facts survive the join, projection, and actual Fleet frame.

ContractProjectionTests uses synthetic native facts. It does not claim native
response consumption or resumed provider work; those belong to convergence.
"""
import unittest
from dataclasses import replace
from pathlib import Path
import importlib.util
import json
import sys
from unittest import mock

from tools.fleet import collectors, render
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.models import _draw_context
from tools.fleet.explorer.grid import CellGrid
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.model import DispatchJob, Session, WorkProjection


def _load_native_fixture(case, name):
    """Scope standalone sibling imports (including lazy imports) to this case."""
    utilities = Path(__file__).resolve().parents[3] / "utilities"
    spec = importlib.util.spec_from_file_location(name, utilities / "human_wait_state.test.py")
    module = importlib.util.module_from_spec(spec)
    # Keep the standalone suite's path insertions through fixture cleanup,
    # then restore the original list without changing the runner environment.
    path_scope = mock.patch.object(sys, "path", [str(utilities), *sys.path])
    path_scope.start()
    case.addCleanup(path_scope.stop)
    spec.loader.exec_module(module)
    return module


class ContractProjectionTests(unittest.TestCase):
    def route_snapshot(self, harness="claude", **owner_values):
        view = {"cwd": "/synthetic/repo", "capability": "autopilot-code",
                "nodes": [{"id": "execute", "state": "active", "harness": harness,
                           "unit": "dev/backend"}]}
        work = WorkProjection(source="route-exact", route_id="rt-exact",
                              _route_view={"view": view, "record": {
                                  "route_id": "rt-exact", "owner_attempt_id": "att-owner"}})
        session = Session(harness=harness, pid=10, session_id="sid-parent", session_tag="p1",
                          cwd="/synthetic/repo", title="현재 사용자 목적을 보존하는 긴 제목",
                          liveness="idle", context_window_tokens=1_000_000,
                          active_context_tokens=900_000)
        owner = DispatchJob(key="owner", slug="task", harness=harness, depth=1,
                            route_id="rt-exact", attempt_id="att-owner", parent_sid="sid-parent",
                            liveness="working", work_projection=work, **owner_values)
        workers = []
        for number, active in enumerate((200_000, 300_000)):
            worker = DispatchJob(key=f"worker-{number}", slug="task", harness=harness, depth=2,
                                 route_id="rt-exact", route_node="execute",
                                 attempt_id=f"att-worker-{number}", parent_sid="sid-parent",
                                 liveness="working", work_projection=work,
                                 active_context_tokens=active, context_window_tokens=1_000_000)
            workers.append(worker)
        return {"sessions": [session], "jobs": [owner, *workers], "resources": [],
                "project_of": lambda _: "repo"}

    def test_cwd_title_join_cannot_copy_context_or_cumulative_usage(self):
        child = Session(harness="claude", pid=10, proc_start="100", session_id="sid-child",
                        cwd="/synthetic/repo", is_child=True, title="User purpose",
                        active_context_tokens=123, context_window_tokens=1000,
                        session_total_tokens=9999)
        job = DispatchJob(key="worker", slug="task", harness="claude", cwd=child.cwd)
        collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.title, child.title)
        self.assertIsNone(job.active_context_tokens)
        self.assertIsNone(job.session_total_tokens)
        job.pid, job.proc_start = child.pid, child.proc_start
        collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.active_context_tokens, 123)
        self.assertEqual(job.session_total_tokens, 9999)

    def test_conflicting_native_sid_cannot_copy_metrics_from_matching_pid(self):
        child = Session(harness="codex", pid=10, proc_start="100", session_id="sid-foreign",
                        title="Foreign purpose", summary="Foreign progress", is_child=True,
                        active_context_tokens=700, context_window_tokens=1000)
        job = DispatchJob(key="worker", slug="task", harness="codex", pid=10, proc_start="100")
        job._runtime_session_id = "sid-exact"
        collectors._adopt_child_titles([child], [job])
        self.assertIsNone(job.active_context_tokens)
        self.assertIsNone(job.title)
        self.assertIsNone(job.summary)

    def test_owner_never_borrows_parent_window_and_workers_keep_separate_gauges(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                snapshot = self.route_snapshot(harness, active_context_tokens=100_000)
                vm = build_view_model(snapshot)
                route = vm.routes[0]
                self.assertEqual(route["owner_gauge"], (None, None))
                self.assertEqual(len(route["worker_contexts"]), 2)
                expected = [render._native_context_gauge(job)[0] for job in snapshot["jobs"][1:]]
                self.assertEqual([row["context"] for row in route["worker_contexts"]], expected)
                text = "\n".join(draw(vm, NavState(view="ROUTES"), 220, 60).text_rows())
                for pct in expected:
                    self.assertIn(f"{pct}%", text)

    def test_worker_only_and_multiple_owners_have_unknown_owner_context(self):
        snapshot = self.route_snapshot(active_context_tokens=500_000, context_window_tokens=1_000_000)
        snapshot["jobs"] = snapshot["jobs"][1:]
        self.assertEqual(build_view_model(snapshot).routes[0]["owner_gauge"], (None, None))
        from tools.fleet.explorer.live import _owner_job
        self.assertIsNone(_owner_job(snapshot["jobs"]))
        owner = self.route_snapshot()["jobs"][0]
        self.assertIsNone(_owner_job([owner, replace(owner, attempt_id="att-other")]))

    def test_selected_stage_attempt_keeps_exact_parallel_siblings(self):
        snapshot = self.route_snapshot("codex")
        backing = snapshot["jobs"][0].work_projection._route_view
        backing["view"]["nodes"][0]["attempt_id"] = "att-worker-0"
        for job in snapshot["jobs"][1:]:
            job._registry_metadata = {
                "session_chain_id": "chain-current", "parent_attempt_id": "att-owner",
                "subsession_mode": "parallel", "stage_authority": "0",
                "route_id": "rt-exact", "route_node": "execute"}
        foreign = replace(snapshot["jobs"][2], key="foreign", attempt_id="att-foreign",
                          active_context_tokens=900_000)
        foreign._registry_metadata = {**snapshot["jobs"][2]._registry_metadata,
                                      "session_chain_id": "chain-previous"}
        snapshot["jobs"].append(foreign)
        contexts = build_view_model(snapshot).routes[0]["worker_contexts"]
        expected = [render._native_context_gauge(job)[0] for job in snapshot["jobs"][1:3]]
        self.assertEqual([row["context"] for row in contexts], expected)

    def test_unassigned_owner_placeholder_does_not_hide_unique_exact_owner(self):
        snapshot = self.route_snapshot("codex", active_context_tokens=300_000,
                                       context_window_tokens=1_000_000)
        snapshot["jobs"][0].work_projection._route_view["record"]["owner_attempt_id"] = "-"
        expected_pct, _window = render._native_context_gauge(snapshot["jobs"][0])
        self.assertEqual(build_view_model(snapshot).routes[0]["owner_gauge"],
                         (expected_pct, 1.0))

    def test_owner_uses_applied_policy_percentage_and_selected_denominator(self):
        snapshot = self.route_snapshot(ctx_pct=50, context_display_window_tokens=600_000,
                                       active_context_tokens=300_000, context_window_tokens=1_000_000,
                                       context_policy={"state": "applied"})
        vm = build_view_model(snapshot)
        self.assertEqual(vm.routes[0]["owner_gauge"], (50, .6))
        text = "\n".join(draw(vm, NavState(view="ROUTES"), 220, 60).text_rows())
        self.assertIn("50%", text)

    def test_folded_session_shows_only_current_route_count(self):
        vm = build_view_model(self.route_snapshot())
        for cols in (140, 168, 180):
            with self.subTest(cols=cols):
                state = NavState(view="OVERVIEW")
                frame = draw(vm, state, cols, 45)
                self.assertIn("route 1", "\n".join(frame.text_rows()))
                hit = next(h for h in frame.hits if h.action == ("fold", "sopen:OVERVIEW", "p1"))
                opened, _ = reduce(state, {"type": "click", "action": hit.action}, frame)
                self.assertEqual(opened.opened, "p1")
        history = replace(vm, routes=tuple(dict(row, current=False, state="done") for row in vm.routes))
        self.assertNotIn("route 1", "\n".join(draw(history, NavState(), 180, 45).text_rows()))

    def test_older_questions_never_light_current_session_marker(self):
        snapshot = self.route_snapshot()
        snapshot["answer_waits"] = {"complete": True, "total": 0, "older": 1,
                                    "items": [{"id": "S1:old", "source": "S1", "stale": "older",
                                               "harness": "claude", "session_id": "sid-parent"}]}
        vm = build_view_model(snapshot)
        self.assertIsNone(vm.sessions[0]["ask"])
        self.assertIsNone(vm.sessions[0]["answer_wait_marker"])
        text = "\n".join(draw(vm, NavState(view="OVERVIEW"), 180, 45).text_rows())
        self.assertIn("0 needs answer", text)
        self.assertTrue(vm.questions[0]["older"])

    def test_all_context_choices_wrap_in_a_forty_cell_pane_with_real_current_value(self):
        data = {"context": {"codex": 1_000_000}, "token_units": {
            "400k": 400_000, "600k": 600_000, "800k": 800_000, "1M": 1_000_000}}
        grid = CellGrid(100, 80)
        _draw_context(grid, data, NavState(), 1, 1, 40)
        choices = [hit for hit in grid.hits if hit.action[:2] == ("ui", "ctx:codex")]
        self.assertEqual({hit.action[2] for hit in choices}, {"400k", "600k", "800k", "1M"})
        current = next(hit for hit in choices if hit.action[2] == "1M")
        self.assertIn("[ 1M ]", grid.rows_text()[current.y])
        selected = NavState(folds=(("ui:ctx:codex", "800k"),))
        grid = CellGrid(100, 80)
        _draw_context(grid, data, selected, 1, 1, 40)
        self.assertTrue(any(hit.action == ("models.apply", "codex", "context-window", 800_000, False)
                            for hit in grid.hits))

    def test_context_choice_and_apply_are_visible_scroll_targets_on_short_screens(self):
        from tools.fleet.explorer.view_model import load_fixture
        vm = load_fixture(Path(__file__).parent / "fixtures/explorer/view_model.json")
        for cols in (140, 168, 180):
            for rows in (24, 35):
                with self.subTest(cols=cols, rows=rows):
                    state = NavState(view="MODELS")
                    choice = None
                    for _ in range(12):
                        frame = draw(vm, state, cols, rows)
                        # No off-screen choice/action is clickable.
                        self.assertTrue(all(0 <= hit.y < rows - 4 for hit in frame.hits
                                            if hit.action[:1] in (("ui",), ("models.apply",))))
                        choice = next((hit for hit in frame.hits
                                       if hit.action == ("ui", "ctx:codex", "1M")), None)
                        if choice:
                            break
                        state, _ = reduce(state, {"type": "key", "key": "pagedown"}, frame)
                    self.assertIsNotNone(choice)
                    state, _ = reduce(state, {"type": "key", "key": "enter", "hit": choice}, frame)
                    for _ in range(12):
                        frame = draw(vm, state, cols, rows)
                        apply = next((hit for hit in frame.hits if hit.action == (
                            "models.apply", "codex", "context-window", 1_000_000, False)), None)
                        if apply:
                            break
                        state, _ = reduce(state, {"type": "key", "key": "pagedown"}, frame)
                    self.assertIsNotNone(apply)
                    _, effects = reduce(state, {"type": "key", "key": "enter", "hit": apply}, frame)
                    self.assertEqual(effects, (apply.action,))

    def test_current_route_boundary_covers_done_unstarted_and_live_children(self):
        for owner_state, node_state, children, expected in (
                ("idle", "active", False, True), ("blocked", "todo", False, True),
                ("working", "done", False, False), ("dead", "todo", False, False),
                ("dead", "active", False, False), ("dead", "active", True, True)):
            with self.subTest(owner=owner_state, node=node_state, children=children):
                snapshot = self.route_snapshot()
                snapshot["jobs"][0].liveness = owner_state
                snapshot["jobs"][0].work_projection._route_view["view"]["nodes"][0]["state"] = node_state
                if not children:
                    snapshot["jobs"] = snapshot["jobs"][:1]
                route = build_view_model(snapshot).routes[0]
                self.assertEqual(route["current"], expected)
                if owner_state == "dead" and children:
                    self.assertEqual(route["activity"], "running")

    def test_native_zero_and_policy_unknown_keep_same_gauge_on_owner_and_session(self):
        for active in (0, 123456):
            for policy in ("unknown", "disabled"):
                snapshot = self.route_snapshot("codex", active_context_tokens=active,
                                               context_window_tokens=950_000,
                                               context_policy={"state": policy})
                parent = snapshot["sessions"][0]
                parent.active_context_tokens, parent.context_window_tokens = active, 950_000
                parent.context_policy = {"state": policy}
                vm = build_view_model(snapshot)
                self.assertEqual(vm.routes[0]["owner_gauge"],
                                 (vm.sessions[0]["context"], vm.sessions[0]["window"]))
                self.assertIsNotNone(vm.routes[0]["owner_gauge"][0])


class NativeResponseIntegrationTests(unittest.TestCase):
    """Real Fleet selection/API/socket with the existing strict native consumer.

    The upstream is controlled. These tests prove one-shot transport and
    response-dependent continuation, not a fresh provider/native-binary run.
    """
    def setUp(self):
        self.backend_tests = _load_native_fixture(self, "fleet_surface_native_fixture")
        self.fixture = self.backend_tests.SelectedAnswerIntegrationTest("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def check_native_response(self, approval):
        from tools.fleet.explorer.runtime import ExplorerRuntime
        from tools.fleet.explorer.state import serialize
        from tools.fleet.explorer.view_model import persistable_view_model
        from tools.fleet.refresh import fleet_answer_wait_view
        backend, observation = self.backend_tests.state, self.backend_tests.observation
        fixture = self.fixture.gateway_fixture
        request_id = "approval-exact" if approval else 502
        turn_id, item_id = "turn-original", "item-original"
        method = "item/commandExecution/requestApproval" if approval else "item/tool/requestUserInput"
        extra = {"command": "fixture original work"} if approval else {"questions": [{
            "id": "choice", "header": "Route", "question": "PRIVATE-ORIGINAL-PROMPT",
            "options": [{"label": "Proceed", "description": "Continue the same work"}]}]}
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {
            "threadId": "thread-1", "turnId": turn_id, "itemId": item_id, **extra}}
        native = {"decision": "accept"} if approval else {"answers": {"choice": {"answers": ["Proceed"]}}}
        marker = Path(self.fixture.tmp.name) / "original-work-progress.json"
        consumer = fixture.server.expect_interaction_response(request, native, marker)
        fixture.server.current.write_json({"jsonrpc": "2.0", "method": "turn/started", "params": {
            "threadId": "thread-1", "turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        fixture.client.wait_for(lambda row: row.get("method") == "turn/started")
        self.fixture._server_request(request)
        self.assertTrue(backend.publish_cache(observation.read_answer_waits(jobs_paths=[])))
        waits = fleet_answer_wait_view(backend.cached_fleet_view(request=False))
        vm = build_view_model({"sessions": [Session(harness="codex", session_id="thread-1",
            session_tag="u1", pid=10, cwd="/synthetic/repo", liveness="blocked")],
            "answer_waits": waits, "project_of": lambda _: "repo"})
        state = NavState(view="QUESTIONS")
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
                mock.patch.object(render, "_EXPLORER_MODEL", vm), \
                mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
                mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0), \
                mock.patch.object(render, "_explorer_queue_question_effect") as refresh:
            def click(prefix):
                frame = draw(vm, render._EXPLORER_STATE, 180, 70)
                hit = next(hit for hit in frame.hits if hit.action[:len(prefix)] == prefix)
                current, effects = reduce(render._EXPLORER_STATE,
                                           {"type": "key", "key": "enter", "hit": hit}, frame)
                render._EXPLORER_STATE = current
                results = []
                for effect in effects:
                    result = ExplorerRuntime.execute_question_action(effect)
                    results.append((effect, result))
                    render._explorer_accept_question_result({"effect": effect, "result": result})
                return results

            self.assertNotIn("PRIVATE-ORIGINAL-PROMPT", "\n".join(draw(vm, state, 180, 70).text_rows()))
            click(("question.select",))
            detail = render._EXPLORER_STATE.question_overlay["detail"]
            self.assertEqual(detail["request_id"], request_id)
            self.assertIs(type(detail["request_id"]), type(request_id))
            self.assertEqual(detail["state"], "open")
            self.assertFalse(marker.exists())
            if approval:
                self.assertEqual(click(("question.decision", "defer")), [])
                self.assertEqual(consumer["consumed"], [])
                response_action = ("question.decision", "approve")
            else:
                self.assertIn("PRIVATE-ORIGINAL-PROMPT", "\n".join(draw(
                    vm, render._EXPLORER_STATE, 180, 70).text_rows()))
                drafted = click(("question.choice", "choice", "Proceed"))
                self.assertEqual(drafted[0][0][0], "questions.draft")
                self.assertFalse(marker.exists(), "saving a draft advanced the original task")
                response_action = ("question.submit",)
            submitted = click(response_action)
            self.assertEqual(len(submitted), 1)
            effect, result = submitted[0]
            self.assertIn(result["state"], ("submitted", "received", "applied"))
            self.assertTrue(consumer["progress"].wait(5))
            fixture.client.wait_for(lambda row: row.get("method") == "item/completed"
                                    and row.get("params", {}).get("item", {}).get("id") == item_id)
            progress = json.loads(marker.read_text())
            self.assertEqual((progress["thread_id"], progress["turn_id"], progress["request_id"]),
                             ("thread-1", turn_id, request_id))
            self.assertEqual(consumer["allowed_work"], ["original-task-work"])
            self.assertEqual(len(consumer["consumed"]), 1)
            self.assertEqual(ExplorerRuntime.execute_question_action(effect)["state"], "applied")
            self.assertEqual(len(consumer["consumed"]), 1)
            self.assertTrue(refresh.called, "submit did not request an exact native state reread")
            exported = json.dumps(persistable_view_model(vm)) + json.dumps(serialize(render._EXPLORER_STATE))
            for private in ("PRIVATE-ORIGINAL-PROMPT", '"native_ref"', '"draft"', '"answers"'):
                self.assertNotIn(private, exported)
            self.assertTrue(backend.publish_cache(observation.read_answer_waits(jobs_paths=[])))
            refreshed = build_view_model({"sessions": [Session(harness="codex", session_id="thread-1",
                session_tag="u1", pid=10, cwd="/synthetic/repo", liveness="working")],
                "answer_waits": fleet_answer_wait_view(backend.cached_fleet_view(request=False))})
            self.assertEqual(refreshed.system["answer_waits"]["total"], 0)
            self.assertIsNone(refreshed.sessions[0]["answer_wait_marker"])

    def test_fleet_question_reaches_original_native_consumer_once_and_resumes_work(self):
        self.check_native_response(approval=False)

    def test_fleet_approval_defers_without_work_then_approve_resumes_original_consumer_once(self):
        self.check_native_response(approval=True)


class OpenCodeHTTPIntegrationTests(unittest.TestCase):
    def check_native_response(self, permission):
        from tools.fleet import interaction
        from tools.fleet.explorer.runtime import ExplorerRuntime
        from tools.fleet.refresh import fleet_answer_wait_view
        tests = _load_native_fixture(self, "fleet_surface_http_fixture")
        fixture = tests.SelectedQuestionActionsTest("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        request_id = "per_original" if permission else "frm_original"
        kind = "permission" if permission else "decision"
        source = "opencode-permission" if permission else "opencode-form"
        if permission:
            fixture.api.permission(request_id)
            expected, collection, turn_id = {"decision": "once"}, "permission", "msg_permission"
        else:
            fixture.api.question(request_id)
            expected, collection, turn_id = {"answer": {"choice": ["red"]}}, "form", "msg_1"
        marker = Path(fixture.tmp.name) / "original-next-step.json"
        path = f"/api/session/{fixture.sid}/{collection}/{request_id}/reply"
        consumer = tests._OpenCodeHTTPConsumer(fixture.api, path, expected, marker)
        self.addCleanup(consumer.close)
        fixture.endpoint.update(url=consumer.url, server_urls=[consumer.url])
        self.assertTrue(interaction.set_wait(fixture.sid, "opencode", kind, source,
                                             request_id=request_id, turn_id=turn_id))
        self.assertTrue(tests.state.publish_cache(tests.observation.read_answer_waits(jobs_paths=[])))
        waits = fleet_answer_wait_view(tests.state.cached_fleet_view(request=False))
        vm = build_view_model({"sessions": [Session(harness="opencode", session_id=fixture.sid,
            session_tag="o1", pid=10, cwd="/synthetic/repo", liveness="blocked")], "answer_waits": waits})
        with mock.patch.object(tests.state, "_opencode_http", fixture.real_http), \
                mock.patch.object(render, "_EXPLORER_STATE", NavState(view="QUESTIONS")), \
                mock.patch.object(render, "_EXPLORER_MODEL", vm), \
                mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
                mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0), \
                mock.patch.object(render, "_explorer_queue_question_effect"):
            def click(prefix):
                frame = draw(vm, render._EXPLORER_STATE, 180, 70)
                hit = next(hit for hit in frame.hits if hit.action[:len(prefix)] == prefix)
                state, effects = reduce(render._EXPLORER_STATE, {"type": "click", "action": hit.action}, frame)
                render._EXPLORER_STATE = state
                result = None
                for effect in effects:
                    result = ExplorerRuntime.execute_question_action(effect)
                    render._explorer_accept_question_result({"effect": effect, "result": result})
                return effects, result
            click(("question.select",))
            self.assertEqual(render._EXPLORER_STATE.question_overlay["state"], "open")
            if permission:
                self.assertEqual(click(("question.decision", "defer")), ((), None))
                action = ("question.decision", "approve")
            else:
                effects, _ = click(("question.choice", "choice", "red"))
                self.assertEqual(effects[0][0], "questions.draft")
                action = ("question.submit",)
            self.assertFalse(marker.exists())
            self.assertEqual(consumer.consumed, [])
            effects, result = click(action)
            self.assertEqual(result["state"], "received" if permission else "applied")
            self.assertEqual(render._EXPLORER_STATE.question_overlay["state"], result["state"])
            self.assertEqual(json.loads(marker.read_text()), ["original-session-next-step"])
            self.assertEqual(consumer.allowed_work, ["original-task-work"])
            ExplorerRuntime.execute_question_action(effects[0])
            self.assertEqual(len(consumer.consumed), 1)
            self.assertEqual(fixture.api.unexpected, [])

    def test_fleet_form_http_reply_consumes_original_request_and_continues_once(self):
        self.check_native_response(permission=False)

    def test_fleet_permission_http_reply_continues_but_ui_keeps_received(self):
        self.check_native_response(permission=True)


if __name__ == "__main__":
    unittest.main()
