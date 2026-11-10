"""VM mapping keeps unknowns explicit and freezes nested input."""
import unittest
import json
from datetime import datetime
from pathlib import Path
from unittest import mock

from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.chrome import count_word
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.model import DispatchJob, Session, WorkProjection


class RouteSubjects(unittest.TestCase):
    def snapshot(self, **record_values):
        record = {"route_id": "rt-subject", "route_hash": "subject-hash", "slug": "opaque-task-1029",
                  "owner_attempt_id": "att-owner", "work_request": {"text": "# 사용자 목적\n\n상세 요청"},
                  **record_values}
        work = WorkProjection(source="route-exact", route_id="rt-subject", route_hash="subject-hash",
                              _route_view={"view": {"slug": record["slug"], "nodes": []}, "record": record})
        owner = DispatchJob(key="owner", slug=record["slug"], harness="codex", depth=1,
                            route_id="rt-subject", route_hash="subject-hash", attempt_id="att-owner",
                            work_projection=work, title="정확한 작업 주제", liveness="working")
        owner._summary_sid = "dispatch-att-owner"
        return {"jobs": [owner], "sessions": [], "resources": []}

    def test_exact_attempt_title_precedes_the_locator_and_preserves_last_success(self):
        snapshot = self.snapshot()
        snapshot["jobs"][0].title_ts = 1       # old success is still the exact source; no regeneration
        self.assertEqual(build_view_model(snapshot).routes[0]["title"], "정확한 작업 주제")

    def test_recorded_subject_and_display_title_keep_precedence(self):
        for values, expected in (({"result": {"name": "Recorded outcome"}}, "Recorded outcome"),
                                 ({"display_title": "Named route"}, "Named route"),
                                 ({"title": "User supplied name"}, "User supplied name")):
            with self.subTest(values=values):
                self.assertEqual(build_view_model(self.snapshot(**values)).routes[0]["title"], expected)

    def test_sealed_request_heading_or_literal_first_line_is_used_without_a_title(self):
        for text, expected in (("\n## 현재 작업 목적\n\n후속", "현재 작업 목적"),
                               ("Restore session selection.\nKeep existing behavior.", "Restore session selection.")):
            snapshot = self.snapshot(work_request={"text": text})
            snapshot["jobs"][0].title = None
            self.assertEqual(build_view_model(snapshot).routes[0]["title"], expected)

    def test_foreign_or_ambiguous_title_sources_do_not_override_the_sealed_request(self):
        for case in ("hash", "owner-attempt", "sidecar-attempt", "ambiguity", "cwd-child", "record-id"):
            with self.subTest(case=case):
                snapshot = self.snapshot()
                owner = snapshot["jobs"][0]
                backing = owner.work_projection._route_view
                if case == "hash":
                    backing["record"]["route_hash"] = "other-hash"
                elif case == "owner-attempt":
                    backing["record"]["owner_attempt_id"] = "att-other"
                elif case == "sidecar-attempt":
                    owner._summary_sid = "dispatch-att-other"
                elif case == "ambiguity":
                    owner.association_ambiguity = "multiple-child-session-id-candidates"
                elif case == "cwd-child":
                    owner._child_session_associated = True
                else:
                    backing["record"]["route_id"] = "rt-foreign"
                self.assertEqual(build_view_model(snapshot).routes[0]["title"],
                                 None if case == "record-id" else "사용자 목적")

    def test_slug_and_readable_slug_are_not_user_purpose_sources(self):
        snapshot = self.snapshot(result={"name": "opaque task"})
        self.assertEqual(build_view_model(snapshot).routes[0]["title"], "정확한 작업 주제")
        snapshot["jobs"][0].title = "opaque-task-1029"
        self.assertEqual(build_view_model(snapshot).routes[0]["title"], "사용자 목적")

    def test_absent_subject_never_borrows_parent_or_previous_route_purpose(self):
        from tools.fleet.explorer import live
        snapshot = self.snapshot(work_request={})
        owner = snapshot["jobs"][0]
        owner.title = None
        parent = Session(harness="codex", pid=10, session_tag="p1", session_id="sid-parent",
                         title="Unrelated current session topic", liveness="working")
        parent.route_chain = {"nodes": [
            {"route_id": "rt-before", "capability": "route-frame", "result": {"name": "Old frame topic"}},
            {"route_id": "rt-subject", "capability": "autopilot-code"}]}
        snapshot["sessions"] = [parent]
        self.assertIsNone(build_view_model(snapshot).routes[0]["title"])
        self.assertIsNone(live._chain_title(parent.route_chain["nodes"], "rt-subject"))
        owner.work_projection._route_view["record"]["work_request"] = {"text": "   "}
        self.assertIsNone(build_view_model(snapshot).routes[0]["title"])

    def test_exact_chain_result_keeps_its_own_name(self):
        from tools.fleet.explorer import live
        nodes = [{"route_id": "rt-old", "result": {"name": "Previous topic"}},
                 {"route_id": "rt-subject", "result": {"name": "Own topic"}}]
        self.assertEqual(live._chain_title(nodes, "rt-subject"), "Own topic")
        nodes[1]["result"]["route_id"] = "rt-foreign"
        self.assertIsNone(live._chain_title(nodes, "rt-subject"))


class CoreFields(unittest.TestCase):
    def test_harness_fields_and_deep_immutability(self):
        sessions = [Session(harness=harness, pid=index + 1, session_tag=f"s{index}",
                            cwd="/synthetic/repo", title=None, ctx_pct=0,
                            liveness="working", elapsed_min=7)
                    for index, harness in enumerate(("claude", "codex", "opencode"))]
        vm = build_view_model({"sessions": sessions, "compute_hosts": [],
                               "project_of": lambda _cwd: "synthetic-repo"})
        self.assertEqual(tuple(row["harness"] for row in vm.sessions), ("claude", "codex", "opencode"))
        self.assertIsNone(vm.sessions[0]["title"])
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.sessions[0]["state"], "working")
        self.assertEqual(vm.sessions[0]["age"], "7m")
        with self.assertRaises(TypeError):
            vm.sessions[0]["title"] = "changed"

    def test_reference_fixture_has_complete_scrubbed_core_records(self):
        path = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"
        vm = load_fixture(path)
        self.assertGreater(len(vm.hosts), 0)
        self.assertGreater(len(vm.sessions), 0)
        self.assertGreater(len(vm.routes), 0)
        self.assertGreater(len(vm.nodes), 0)
        self.assertGreater(len(vm.questions), 0)
        self.assertGreater(len(vm.seats), 0)
        self.assertGreater(len(vm.generations), 0)
        self.assertGreater(len(vm.cards), 0)
        self.assertGreater(len(vm.dialogue), 0)
        self.assertGreater(len(vm.peer_nodes), 0)
        self.assertGreater(len(vm.compute), 0)
        self.assertTrue(vm.models)
        self.assertTrue(vm.system)
        encoded = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("mock", encoded)

    def test_live_boundary_keeps_exact_pane_and_source_record_joins(self):
        session = {"harness": "codex", "session_id": "sid-live", "cwd": "/synthetic/repo",
                   "herdr_pane": "workspace:pane-7", "context_pct": 0}
        graph = ({"harness": "codex", "session_id": "sid-live",
                  "pane_id": "workspace:pane-7"},)
        facts = {"routes": (("rt-test", {"id": "rt-test"}),), "warnings": ()}
        vm = build_view_model({"sessions": [session], "pane_graph": graph,
                               "project_of": lambda _cwd: "synthetic-repo"},
                              accepted_detail={"source_records": facts})
        self.assertEqual(vm.sessions[0]["pane"], "workspace:pane-7")
        self.assertEqual(vm.sessions[0]["context"], 0)
        self.assertEqual(vm.pane_graph[0]["session_id"], "sid-live")
        self.assertEqual(vm.source_records["warnings"], ())


class ConfigFields(unittest.TestCase):
    def test_cached_config_and_memory_projection_preserve_missing_and_zero(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        config = {"models": {"profiles": ["balanced"], "context": {"codex": 0}}}
        snapshot = {"sessions": [], "resources": [], "memory": None}
        vm = build_view_model(snapshot, accepted_detail={"memory": [("repo", [("12:00", "saved", "note", "fact")])]},
                              cached_config=config)
        self.assertEqual(vm.models["profiles"], ("balanced",))
        self.assertEqual(vm.models["context"]["codex"], 0)
        self.assertEqual(vm.memory[0][1][0][3], "fact")
        missing = build_view_model({"sessions": [], "resources": []}, cached_config={})
        self.assertIsNone(missing.models)
        self.assertIsNone(missing.memory)
        self.assertIsInstance(ExplorerViewModel.from_mapping(missing), ExplorerViewModel)

    def test_memory_lifecycle_rows_are_dropped_and_records_are_deduplicated(self):
        memory = {"recent": [], "by_repo": {"memory-store": [
            {"id": "a", "ts": "2026-10-24T08:00:00+09:00", "action": "tidy-add",
             "type": "note", "project": "actual-project", "snippet": "2026-10-24 [aa](claude sid): useful"},
            {"id": "a", "ts": "2026-10-24T08:00:00+09:00", "action": "tidy-add",
             "type": "note", "project": "actual-project", "snippet": "duplicate"},
            {"id": "e", "ts": "2026-10-24T07:00:00+09:00", "action": "lifecycle-expire",
             "type": "note", "project": "actual-project", "snippet": "expired row"},
        ]}}
        vm = build_view_model({"sessions": [], "resources": [], "memory": memory},
                              now=datetime.fromisoformat("2026-10-24T08:00:00+09:00").timestamp() + 60)
        rows = vm.memory[0][1]
        self.assertEqual(len(rows), 1)
        local = datetime.fromisoformat("2026-10-24T08:00:00+09:00").astimezone().strftime("%H:%M")
        self.assertEqual(tuple(rows[0]), (local, "saved", "note", "useful"))
        self.assertEqual(vm.system["project_memory"]["order"], ("actual-project",))
        self.assertNotIn("memory_summary", vm.system)

    def test_first_tick_process_row_is_kept_and_gpu_processes_use_renderer_record_shape(self):
        snapshot = {"sessions": [{"harness": "codex", "pid": 44, "proc_start": 100,
                                  "session_id": "sid-exact", "session_tag": "a3",
                                  "cwd": "/work/SR_ModelA-wt/branch", "liveness": "working"}],
                    "resources": [], "compute_hosts": {"hosts": [{"host": "host-a", "gpus": [
                        {"index": 0, "processes": [{"command": "python -B /tmp/resume.py --request-sha256 0123456789abcdef",
                            "used_memory_mib": 2560, "elapsed_s": 3600,
                            "cwd": "/work/SR_ModelA-wt/branch",
                            "session_owner": {"harness": "codex", "session_id": "sid-exact"}}]}]}]},
                    "unregistered_gpu": []}
        vm = build_view_model(snapshot, now=2_000_000_000)
        self.assertEqual(len(vm.sessions), 1)
        self.assertEqual(vm.system["gpu_processes"][0]["session_id"], "sid-exact")
        self.assertEqual(vm.system["gpu_processes"][0]["session_tag"], "a3")
        self.assertEqual(vm.system["gpu_processes"][0]["command"], "resume.py")
        self.assertEqual(vm.system["gpu_processes"][0]["vram"], "2.5 GB")
        self.assertEqual(vm.system["gpu_processes"][0]["age"], "1h 0m")

    def test_unmatched_gpu_owner_keeps_exact_id_but_has_no_display_tag(self):
        snapshot = {"sessions": [], "resources": [], "compute_hosts": {"hosts": [{"host": "host-a", "gpus": [
            {"index": 0, "processes": [{"command": "python train.py", "used_memory_mib": 1024,
                "elapsed_s": 60, "cwd": "/work/repo",
                "session_owner": {"harness": "codex", "session_id": "departed-session"}}]}]}]},
            "unregistered_gpu": []}

        vm = build_view_model(snapshot, now=2_000_000_000)
        process = vm.system["gpu_processes"][0]

        self.assertEqual(process["session_id"], "departed-session")
        self.assertIsNone(process["session_tag"])

    def test_compute_host_uses_per_thread_cpu_and_swap_telemetry(self):
        host = {"host": "host-a", "self": True, "reachable": True, "cpu_count": 4,
                "cpu_utilization_pct": 10, "cpu_thread_utilization_pct": [50, 25, 0, 25],
                "memory_total_mib": 8192, "memory_used_mib": 4096,
                "swap_total_mib": 4096, "swap_used_mib": 1024, "gpus": []}
        vm = build_view_model({"sessions": [], "resources": [],
                               "compute_hosts": {"hosts": [host]}}, now=10)
        self.assertEqual(tuple(vm.compute[0][2]), (1.0, 4))
        self.assertEqual(tuple(vm.compute[0][4]), (1.0, 4.0))


class RealDataFidelity(unittest.TestCase):
    """What the default board decides about a row, the explorer's records say the same."""

    def build(self, *sessions, **extra):
        return build_view_model({"sessions": list(sessions), "project_of": lambda cwd: "synthetic-repo",
                                 **extra}, now=2_000_000_000)

    def test_model_effort_and_context_resolve_like_the_default_board_for_every_harness(self):
        rows = [Session(harness="claude", pid=1, session_tag="c1", cwd="/x/repo", liveness="idle",
                        model="Opus 5.5 (1M context)", effort="xhigh", active_context_tokens=250_000,
                        context_window_tokens=1_000_000),
                Session(harness="codex", pid=2, session_tag="c2", cwd="/x/repo", liveness="idle",
                        model="gpt-6.1-sol", effort="high", active_context_tokens=100_000,
                        context_window_tokens=400_000),
                Session(harness="opencode", pid=3, session_tag="c3", cwd="/x/repo", liveness="idle",
                        model="opencode-go/glm-5.2", effort="runtime-default", effort_default=True),
                Session(harness="claude", pid=4, session_tag="c4", cwd="/x/repo", liveness="stale",
                        model="Opus 5.5", effort="high", ctx_pct=40)]
        vm = self.build(*rows)
        by_tag = {row["tag"]: row for row in vm.sessions}
        self.assertEqual(tuple(by_tag["c1"]["model"]), ("Opus 5.5", "fam_opus"))
        self.assertEqual(by_tag["c1"]["effort"], "xhigh")
        self.assertEqual((by_tag["c1"]["context"], by_tag["c1"]["window"]), (25, 1.0))
        from tools.fleet import render
        self.assertEqual(by_tag["c2"]["context"], render._native_context_gauge(rows[1])[0])   # window-normalised
        self.assertEqual(by_tag["c3"]["model"][0],
                         render._clean_model(render._dispatch_display_model("opencode-go/glm-5.2")))
        self.assertEqual(by_tag["c3"]["effort"], "default")          # the runtime's own default, not a level
        # a stale row has no live telemetry: the bare harness, no context
        self.assertEqual((by_tag["c4"]["model"], by_tag["c4"]["effort"], by_tag["c4"]["context"]),
                         (None, None, None))

    def test_a_folder_inside_the_home_folder_is_never_grouped_as_the_home_folder(self):
        from unittest import mock
        from tools.fleet.explorer import live
        # a data root that carries the user's folder name is the home folder for this purpose
        with mock.patch.object(live, "_HOME_FORMS", ("/home/alice", "/data/alice")):
            names = {live._repo_of(lambda _cwd: "alice", cwd) for cwd in
                     ("/data/alice/work/projects/2026-08_keyword", "/data/alice")}
            self.assertEqual(names, {"2026-08_keyword", live.HOME_LABEL})
            self.assertEqual(live._repo_of(lambda _cwd: "tool", "/data/alice/Project_Zoo/tool"), "tool")
            self.assertEqual(live._repo_of(lambda _cwd: "alice-site", "/data/alice/alice-site"), "alice-site")

    def test_branch_is_the_known_branch_or_nothing(self):
        vm = self.build(Session(harness="claude", pid=1, session_tag="b1", cwd="/x/repo", liveness="idle",
                                branch="fix/thing"),
                        Session(harness="claude", pid=2, session_tag="b2", cwd="/x/repo", liveness="idle"))
        self.assertEqual([row["branch"] for row in vm.sessions], ["fix/thing", None])

    def test_install_version_and_usage_freshness_reach_the_top_line(self):
        usage = {"claude": {"payload": {"rl_5h": 10, "rl_7d": 20}, "observed_at": 1_999_999_000}}
        vm = self.build(Session(harness="claude", pid=1, session_tag="u1", cwd="/x/repo", liveness="idle"),
                        hearting={"version": "v3.42.0-2-gdc406c55a-dirty", "install_method": "unmanaged"},
                        usage_snapshots=usage)
        self.assertEqual(vm.system["install"]["version"], "v3.42.0-2-gdc406c55a-dirty")
        self.assertEqual(vm.hearting, {"version": "v3.42.0-2-gdc406c55a-dirty",
                                       "install_method": "unmanaged"})
        self.assertRegex(vm.system["usage_api"]["freshness"], r"^\d\d:\d\d$")

    def test_route_uses_recorded_result_name_and_default_version_helper_keeps_build(self):
        from tools.fleet.explorer import live
        snapshot = {"resources": (), "sessions": (), "jobs": (), "route_entities": ()}
        collected = {"route-x": {"view": {"nodes": [], "slug": "route-slug"},
                                  "record": {"slug": "route-slug", "result": {
                                      "name": "recorded task name", "result": "success"}},
                                  "jobs": [], "sessions": []}}
        with mock.patch.object(live, "_collect_routes", return_value=collected):
            routes, _nodes = live._route_records(snapshot, [], {}, lambda _cwd: None, 10)
        self.assertEqual(routes[0]["title"], "recorded task name")
        install = live._install_record({"hearting": {"version": "v7.8.9-4-gabc123-dirty"}})
        self.assertEqual(install["version"], "v7.8.9-4-gabc123-dirty")

    def test_peer_endpoint_matches_a_resumed_session_alias_and_newest_first(self):
        from tools.fleet.explorer.live import _peer_records
        sessions = [
            {"tag": "new", "harness": "claude", "repo": "repo"},
            {"tag": "sender", "harness": "codex", "repo": "repo"},
        ]
        index = {
            "new": {"sid": "sid-new", "ids": ("sid-new", "sid-old"),
                    "session": {"peer_last_recv": {"from_session_id": "sid-sender",
                                                      "from_harness": "codex", "kind": "steer",
                                                      "age_min": 20}}},
            "sender": {"sid": "sid-sender", "ids": ("sid-sender",),
                       "session": {"peer_last_sent": {"to_session_id": "sid-old",
                                                       "to_harness": "claude", "kind": "handoff",
                                                       "age_min": 2}}},
        }
        _nodes, edges = _peer_records(sessions, index, 100)
        self.assertEqual([link[1] for link in edges["links"]], [["new", "claude"], ["new", "claude"]])
        self.assertEqual([link[2] for link in edges["links"]], ["handoff", "steer"])
        self.assertEqual(len(edges["links"]), 2)

    def test_decision_records_become_the_answered_list_and_the_feed_line(self):
        memory = {"recent": [{"ts": "2026-10-23T12:00:00+00:00", "action": "decision-record", "type": "decision",
                              "snippet": "pick the plain name", "cwd": "/x/repo", "sid": ""}],
                  "by_repo": {"repo": [{"ts": "2026-10-23T12:00:00+00:00", "action": "decision-record",
                                        "type": "decision", "snippet": "pick the plain name"}]}}
        vm = self.build(Session(harness="claude", pid=1, session_tag="m1", cwd="/x/repo", liveness="idle"),
                        memory=memory)
        self.assertEqual(vm.answered[0][0], "repo")
        self.assertEqual(vm.answered[0][1][0][1], "pick the plain name")
        self.assertEqual(vm.system["answered_total"], 1)
        self.assertEqual(vm.events[0][1], "m1")            # the session that works in that folder
        self.assertEqual(vm.events[0][5], "pick the plain name")


class SourceBackedRecords(unittest.TestCase):
    """Fields the 22:53 parity ledger moved from the reference's hand values to their sources."""

    @staticmethod
    def vm(sessions=(), now=1_000_000.0, **extra):
        return build_view_model({"sessions": list(sessions), "project_of": lambda _cwd: "synthetic-repo",
                                 **extra}, now=now)

    def test_running_line_names_the_executing_child_and_how_long(self):
        session = {"harness": "claude", "session_id": "sid-a", "session_tag": "aa", "cwd": "/synthetic/repo",
                   "exec_child": {"comm": "python3", "leaf_etime_s": 4897}, "liveness": "working"}
        self.assertEqual(self.vm([session]).sessions[0]["running"], (("⚙ python3", "soft"), (" · 1h 21m", "dim")))

    def test_pane_falls_back_to_the_identity_evidence_pane(self):
        session = {"harness": "claude", "session_id": "sid-a", "session_tag": "aa", "cwd": "/synthetic/repo",
                   "session_identity_evidence": {"pane": "w1:p2"}}
        self.assertEqual(self.vm([session]).sessions[0]["pane"], "w1:p2")

    def test_a_steward_leads_its_repository_whatever_the_natural_order(self):
        def session(tag, mtime, steward=False, cwd="/synthetic/repo"):
            return {"harness": "claude", "session_id": "sid-" + tag, "session_tag": tag, "cwd": cwd,
                    "liveness": "idle", "mtime": mtime, "steward": steward}
        rows = [session("aa", 30), session("bb", 20), session("st", 10, steward=True)]
        self.assertEqual([row["tag"] for row in self.vm(rows).sessions], ["st", "aa", "bb"])
        ordered = self.vm(rows, session_order=["sid-aa", "sid-bb", "sid-st"]).sessions
        self.assertEqual([row["tag"] for row in ordered], ["st", "aa", "bb"])
        self.assertTrue(ordered[0]["steward"])

    def test_a_waiting_session_reads_answer_needed_not_the_raw_kind(self):
        session = {"harness": "codex", "session_id": "sid-a", "session_tag": "aa", "cwd": "/synthetic/repo",
                   "liveness": "blocked", "interaction_state": {"kind": "decision"}}
        self.assertEqual(self.vm([session]).sessions[0]["ask"], "answer needed")

    def test_usage_windows_sit_in_their_slots_five_hour_week_then_the_rest(self):
        session = {"harness": "codex", "session_id": "sid-a", "session_tag": "aa", "cwd": "/synthetic/repo",
                   "rl_5h": None, "rl_7d": 33, "rl_rs": (None, 1_000_000.0 + 3600 * 40)}
        windows = self.vm([session]).usage[0]["windows"]
        self.assertIsNone(windows[0])
        self.assertEqual(tuple(windows[1]), ("week", 33, "1d 16h"))
        self.assertIsNone(windows[2])

    def test_install_flags_and_runtimes_come_from_the_install_record(self):
        vm = self.vm(hearting={"version": "v3.41.1", "install_method": "managed/pinned",
                               "source": "distribution", "runtimes": ["claude", "codex"]})
        self.assertEqual(vm.hearting, {"version": "v3.41.1", "install_method": "managed/pinned"})
        self.assertEqual(vm.system["install"]["managed"], True)
        self.assertEqual(vm.system["install"]["pinned"], True)
        self.assertEqual(vm.system["install"]["distribution"], True)
        self.assertEqual(tuple(vm.system["runtimes"]), ("claude", "codex"))

    def test_unbounded_configured_host_stays_full_in_compute_projection(self):
        host_name = "trainer-west-gpu-node-01"
        host = {"host": host_name, "self": True, "reachable": True, "cpu_count": 4,
                "cpu_utilization_pct": 25, "memory_total_mib": 8192,
                "memory_used_mib": 4096, "gpus": []}
        vm = self.vm(hearting={"version": "v3.50.3-20-gad247e2b6",
                               "install_method": "managed/stable"},
                     compute_hosts={"hosts": [host]})

        self.assertEqual(vm.hosts[0]["name"], host_name)
        self.assertEqual(vm.compute[0][0], host_name)
        self.assertEqual(vm.hearting, {"version": "v3.50.3-20-gad247e2b6",
                                       "install_method": "managed/stable"})

    def test_memory_rows_drop_bookkeeping_and_the_provenance_header(self):
        memory = {"by_repo": {"demo-app": [
            {"ts": "2026-10-21T22:51:22", "action": "tidy-add", "type": "decision",
             "snippet": "2026-10-21 밤 [0c](claude 442122b8): 승인 5단계 중 ②를 마쳤다"},
            {"ts": "2026-10-21T22:51:22", "action": "supersede", "type": "decision",
             "snippet": "superseded_by=decision_x"},
            {"ts": "2026-10-21T21:00:00", "action": "decision-record", "type": "decision",
             "snippet": "[결정] 이대로 갈까요?"}]}}
        rows = self.vm(memory=memory, now=datetime.fromisoformat("2026-10-21T22:51:22").timestamp() + 60).memory
        self.assertEqual([tuple(r[:3]) for r in rows[0][1]],
                         [("22:51", "saved", "decision"), ("21:00", "saved", "decision")])
        self.assertEqual(rows[0][1][0][3], "승인 5단계 중 ②를 마쳤다")

    def test_memory_time_cells_date_rows_that_are_not_today(self):
        memory = {"by_repo": {"demo-app": [
            {"id": "today", "ts": "2026-10-23T12:00:00", "action": "decision-record", "type": "decision",
             "snippet": "[결정] 오늘 답"},
            {"id": "older", "ts": "2026-10-22T09:30:00", "action": "decision-record", "type": "decision",
             "snippet": "[결정] 어제 답"},
            {"id": "oldest", "ts": "2026-10-21T22:51:22", "action": "add", "type": "note",
             "snippet": "이틀 전 기록"}]}}
        vm = self.vm(memory=memory, now=datetime.fromisoformat("2026-10-23T13:00:00").timestamp())
        cells = {r[3]: r[0] for r in vm.memory[0][1]}
        self.assertEqual(cells["[결정] 오늘 답"], "12:00")
        self.assertEqual(cells["[결정] 어제 답"], "10-22")
        self.assertEqual(cells["이틀 전 기록"], "10-21")
        answered = {r[1]: r[0] for r in vm.answered[0][1]}
        self.assertEqual(answered, {"오늘 답": "12:00", "어제 답": "10-22"})

    def test_gpu_rows_carry_the_card_class_and_the_session_that_uses_it(self):
        host = {"host": "h1", "self": True, "reachable": True, "cpu_count": 8, "cpu_utilization_pct": 10,
                "memory_total_mib": 8192, "memory_used_mib": 1024,
                "gpus": [{"index": 0, "name": "NVIDIA RTX 6000 Ada Generation", "utilization_gpu_pct": 50,
                          "memory_used_mib": 2048, "memory_total_mib": 49152,
                          "processes": [{"pid": 5, "process_name": "python", "used_memory_mib": 2048,
                                         "elapsed_s": 7200,
                                         "session_owner": {"harness": "claude", "id": "sid-a"}}]}]}
        session = {"harness": "claude", "session_id": "sid-a", "session_tag": "aa", "cwd": "/synthetic/repo"}
        vm = self.vm([session], compute_hosts={"hosts": [host]})
        gpu = vm.compute[0][5][0]
        self.assertEqual((gpu[2], gpu[3], tuple(gpu[7])), ("RTX 6000 Ada", "gpu_ada", ("aa",)))
        line = vm.sessions[0]["gpu"][0]
        self.assertEqual("".join(text for text, _key in line), "● h1:0  RTX 6000 Ada  2.0 GB  python  · 2h")


class CountWords(unittest.TestCase):
    def test_every_count_word_is_singular_for_one(self):
        self.assertEqual(count_word(1, "question"), "1 question")
        self.assertEqual(count_word(2, "question"), "2 questions")
        self.assertEqual(count_word(0, "session"), "0 sessions")
        self.assertEqual(count_word(1, "harness", "harnesses"), "1 harness")
        self.assertEqual(count_word(3, "harness", "harnesses"), "3 harnesses")


class ProjectMemoryAndCurrentTaskProjection(unittest.TestCase):
    @staticmethod
    def open_record(rid, headline, rtype="project", updated="2026-10-30", **extra):
        return {"id": rid, "type": rtype, "headline": headline, "updated": updated,
                "tags": (), "links": (), "artifact_refs": (), **extra}

    def test_open_stage_reads_the_headline_marker_and_exact_route_links(self):
        from tools.fleet.explorer.live import _open_stage

        routes = [{"id": "rt-current", "state": "working", "outcome": None, "stages": []},
                  {"id": "rt-settled", "state": "done", "outcome": "success", "stages": []},
                  {"id": "rt-failed", "state": "done", "outcome": "failed", "stages": []}]
        cases = {
            "[진행 중·79 담당] marked doing": "doing",
            "[수리 진행] repair under way": "doing",
            "[설치 완료·실사용 미확인] installed": "waiting",
            "[부분 설치] partly installed": "waiting",
            "[백로그·결함] backlog defect": "open",
            "[미해결] unresolved": "open",
            "[해결 확인] resolved": None,
            "[0c] a session tag is no work marker": None,
            "plain project fact": None,
        }
        for headline, expected in cases.items():
            with self.subTest(headline=headline):
                self.assertEqual(_open_stage(self.open_record("r", headline), routes), expected)
        obligation = self.open_record("o", "no marker", rtype="unresolved-obligation")
        self.assertEqual(_open_stage(obligation, routes), "open")       # never assumed in progress
        tagged = self.open_record("t", "tagged", tags=('"backlog"]',))   # tolerant tag decoding
        self.assertEqual(_open_stage(tagged, routes), "open")
        for link, expected in (("rt-current", "doing"), ("rt-settled", "waiting"), ("rt-failed", "open")):
            with self.subTest(link=link):
                linked = self.open_record("l", "linked", rtype="backlog", links=(link,))
                self.assertEqual(_open_stage(linked, routes), expected)
        resolved = self.open_record("x", "[해결 확인] done", rtype="unresolved-obligation")
        self.assertIsNone(_open_stage(resolved, routes))
        # Finished kinds and resolved tags are not open work even when tagged backlog.
        finished = (self.open_record("c", "closed 17", rtype="completion", tags=("backlog",)),
                    self.open_record("a", "pointer", rtype="artifact-pointer", tags=("backlog",)),
                    self.open_record("b", "[관찰] seen", rtype="observation"),
                    self.open_record("p", "tidy note", tags=('["resolved"', '"backlog"]')))
        for record in finished:
            with self.subTest(record=record["id"]):
                self.assertIsNone(_open_stage(record, routes))
        self.assertEqual(_open_stage(self.open_record("q", "[백로그] cleanup", tags=("backlog",)), routes),
                         "open")

    def test_project_memory_groups_decisions_open_work_and_last_change_per_project(self):
        from tools.fleet.explorer.live import _journal_groups, _project_memory

        memory = {
            "by_repo": {"journal-only": [{"id": "j1", "ts": "2026-11-02T09:00:00", "action": "add",
                                          "type": "note", "snippet": "journal row"}],
                        "busy": [{"id": "j2", "ts": "2026-11-02T08:00:00", "action": "add",
                                  "type": "decision", "snippet": "body text",
                                  "headline": "busy headline"}]},
            "database": {"state": "success", "by_repo": {
                "busy": {"updated": "2026-11-02", "decisions_today": 2, "decisions": [
                    {"id": "d1", "headline": "결정: 첫 결정 → 예", "updated": "2026-11-02"},
                    {"id": "d2", "headline": "[e4] 2026-11-01 둘째 결정", "updated": "2026-11-01"}],
                    "open": [self.open_record("o1", "older open", rtype="backlog", updated="2026-10-01"),
                             self.open_record("o2", "[진행 중] doing", updated="2026-09-01"),
                             self.open_record("o3", "newer open", rtype="unresolved-obligation",
                                              updated="2026-10-20"),
                             self.open_record("o4", "[해결 확인] closed", updated="2026-10-21")]},
                "quiet": {"updated": "2026-08-10", "decisions_today": 0, "open": [], "decisions": [
                    {"id": "q1", "headline": "quiet decision", "updated": "2026-08-10"}]},
                "live": {"updated": "2026-07-01", "decisions_today": 0, "open": [], "decisions": []}}}}
        snapshot = {"memory": memory}
        journal = _journal_groups(snapshot, datetime(2026, 11, 2, 12, 0).timestamp())
        result = _project_memory(snapshot, [], [{"repo_id": "live"}], journal)
        self.assertEqual(result["order"], ["live", "journal-only", "busy", "quiet"])
        busy = result["projects"]["busy"]
        self.assertEqual(busy["decisions"], [["11-02", "첫 결정 → 예"], ["11-01", "둘째 결정"]])
        self.assertEqual([item[:3] for item in busy["open"]],
                         [["doing", "09-01", "[진행 중] doing"], ["open", "10-01", "older open"],
                          ["open", "10-20", "newer open"]])
        self.assertEqual(busy["counts"], {"doing": 1, "waiting": 0, "open": 2})
        self.assertEqual(busy["last"], "08:00")
        self.assertEqual(result["projects"]["quiet"]["last"], "08-10")
        self.assertEqual(result["projects"]["journal-only"]["decisions"], [])
        self.assertEqual((result["decisions_today"], result["doing"]), (2, 1))
        self.assertEqual(journal["busy"][0][1][3], "busy headline")

    def test_project_memory_without_a_database_keeps_counts_unknown_or_marks_last_good(self):
        from tools.fleet.explorer.live import _project_memory

        unknown = _project_memory({"memory": {"by_repo": {}}}, [], [], {})
        self.assertEqual((unknown["state"], unknown["decisions_today"], unknown["doing"]),
                         ("unknown", None, None))
        failed = _project_memory({"memory": {"database": {
            "state": "failed", "last_good": True, "by_repo": {},
            "last_good_by_repo": {"repo": {"updated": "2026-10-01", "decisions": [], "open": [
                self.open_record("t", "last good task", rtype="backlog")]}}}}}, [], [], {})
        self.assertEqual((failed["state"], failed["stale"]), ("failed", True))
        self.assertEqual(failed["projects"]["repo"]["open"][0][2], "last good task")

    def test_current_task_summary_uses_the_exact_focused_session_and_stage_states(self):
        from types import SimpleNamespace
        from tools.fleet.explorer.live import _current_task_summary

        focus = SimpleNamespace(key=SimpleNamespace(harness="claude", sid="sid-a"),
                                obligations=({"state": "running", "attempt_id": "attempt-1"},),
                                health=SimpleNamespace(state="success"))
        detail = SimpleNamespace(focus=focus, seats={}, config=None, panes={})
        session_index = {"a1": {"sid": "sid-a", "ids": ("sid-a",),
                                 "session": {"harness": "claude"}}}
        routes = [{"id": "rt-current", "session_tag": "a1", "state": "working",
                   "stages": [{"name": "plan", "st": "done"},
                              {"name": "execute", "st": "active"},
                              {"name": "test", "st": "todo"}]}]
        summary = _current_task_summary(detail, (), session_index, routes)
        self.assertEqual(summary["session_tag"], "a1")
        self.assertIn("test", summary["todo"])
        self.assertIn("execute", summary["doing"])
        self.assertIn("plan", summary["done"])
        self.assertTrue(any("attempt-" in value for value in summary["doing"]))

    @staticmethod
    def memory_vm(project_memory, memory=None):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        return ExplorerViewModel.from_mapping({"memory": memory, "system": {"project_memory": project_memory}})

    @mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})
    def test_memory_page_marks_an_unread_database_and_shows_last_good(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState

        unavailable = self.memory_vm({"state": "unavailable", "stale": False, "decisions_today": None,
                                      "doing": None, "order": ["repo"], "projects": {}},
                                     memory=[["repo", [["09:00", "saved", "note", "journal row"]]]])
        text = "\n".join(draw(unavailable, NavState(view="MEMORY"), 140, 45).text_rows())
        self.assertIn("메모리 DB unavailable · 결정·남은 일 모름", text)
        self.assertIn("journal row", text)
        self.assertIn("프로젝트 1 · 메모리 DB unavailable", text)
        self.assertNotIn("진행 중 0", text)

        stale = self.memory_vm({"state": "failed", "stale": True, "decisions_today": 0, "doing": 0,
                                "order": ["repo"], "projects": {"repo": {
                                    "last": "10-01", "decisions": [], "limited": False,
                                    "open": [["open", "10-01", "last good task", "t"]],
                                    "counts": {"doing": 0, "waiting": 0, "open": 1}}}})
        stale_text = "\n".join(draw(stale, NavState(view="MEMORY"), 140, 45).text_rows())
        self.assertIn("메모리 DB failed · 마지막으로 읽은 값", stale_text)
        self.assertIn("기록상 열림 1", stale_text)
        self.assertIn("last good task", stale_text)

    @mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})
    def test_memory_page_cards_fold_open_work_per_project(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState, reduce

        items = [["doing", "10-26", "doing task", "a"], ["waiting", "10-27", "waiting task", "b"],
                 ["open", "10-30", "open task", "c"]]
        vm = self.memory_vm({"state": "success", "stale": False, "decisions_today": 1, "doing": 1,
                             "order": ["repo"], "projects": {"repo": {
                                 "last": "21:36", "decisions": [["11-02", "the decision"]],
                                 "open": items, "limited": True,
                                 "counts": {"doing": 1, "waiting": 1, "open": 1}}}},
                            memory=[["repo", [["21:36", "saved", "note", "recent change"]]]])
        state = NavState(view="MEMORY")
        frame = draw(vm, state, 140, 45)
        text = "\n".join(frame.text_rows())
        for expected in ("repo/", "마지막 변화 21:36", "결정", "the decision", "남은 일",
                         "진행 중 1 · 확인 대기 1 · 기록상 열림 1+", "▶ 펼치기", "doing task",
                         "최근", "recent change", "프로젝트 1 · 오늘 결정 1 · 진행 중 1"):
            self.assertIn(expected, text)
        self.assertNotIn("open task", text)
        hit = next(item for item in frame.hits if item.action == ("fold", "memory:open", "repo"))
        state, _ = reduce(state, {"type": "click", "action": hit.action}, frame)
        expanded = "\n".join(draw(vm, state, 140, 45).text_rows())
        for expected in ("▼ 접기", "확인 대기  waiting task", "열림  open task"):
            self.assertIn(expected, expanded)
        from tools.fleet.tests.parity_matrix import own_trace_defects
        for width, counts in ((80, "진행 1 · 대기 1 · 열림 1+"),
                              (100, "진행 중 1 · 확인 대기 1 · 기록상 열림 1+")):
            with self.subTest(width=width):
                narrow, defects = own_trace_defects(vm, state, width)
                self.assertEqual(defects, [])
                self.assertIn(counts, "\n".join(narrow.text_rows()))

    @mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "English"})
    def test_memory_page_reads_in_english_without_a_korean_declaration(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState, reduce
        from tools.fleet.tests.parity_matrix import own_trace_defects

        unread = self.memory_vm({"state": "unavailable", "stale": False, "decisions_today": None,
                                 "doing": None, "order": ["repo"], "projects": {}})
        text = "\n".join(draw(unread, NavState(view="MEMORY"), 140, 45).text_rows())
        self.assertIn("memory database unavailable · decisions and to-dos unknown", text)
        self.assertIn("projects 1 · memory database unavailable", text)
        items = [["doing", "10-26", "doing task", "a"], ["waiting", "10-27", "waiting task", "b"],
                 ["open", "10-30", "open task", "c"]]
        vm = self.memory_vm({"state": "failed", "stale": True, "decisions_today": 1, "doing": 1,
                             "order": ["repo"], "projects": {"repo": {
                                 "last": "21:36", "decisions": [["11-02", "the decision"]],
                                 "open": items, "limited": True,
                                 "counts": {"doing": 1, "waiting": 1, "open": 1}}}},
                            memory=[["repo", [["21:36", "saved", "note", "recent change"]]]])
        state = NavState(view="MEMORY")
        frame = draw(vm, state, 140, 45)
        text = "\n".join(frame.text_rows())
        for expected in ("memory database failed · last good values", "last change 21:36", "decisions  11-02",
                         "the decision", "to do", "in progress 1 · to verify 1 · open on record 1+",
                         "▶ expand", "in progress  doing task", "recent", "recent change",
                         "projects 1 · decisions today 1 · in progress 1 · database failed"):
            self.assertIn(expected, text)
        self.assertIsNone(__import__("re").search(r"[가-힣]", text), text)
        hit = next(item for item in frame.hits if item.action == ("fold", "memory:open", "repo"))
        state, _ = reduce(state, {"type": "click", "action": hit.action}, frame)
        expanded = "\n".join(draw(vm, state, 140, 45).text_rows())
        for expected in ("▼ collapse", "to verify  waiting task", "open  open task"):
            self.assertIn(expected, expanded)
        for width, counts in ((80, "doing 1 · verify 1 · open 1+"),
                              (100, "in progress 1 · to verify 1 · open on record 1+")):
            with self.subTest(width=width):
                narrow, defects = own_trace_defects(vm, state, width)
                self.assertEqual(defects, [])
                self.assertIn(counts, "\n".join(narrow.text_rows()))

    def test_peer_count_stays_unknown_or_marks_last_good_after_empty_unhealthy_source(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.live import build_view_model
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        unknown = build_view_model({"sessions": [], "resources": [],
                                    "observations": {"optional:peers": {"state": "unknown"}}})
        unknown_text = "\n".join(draw(unknown, NavState(view="PEERS"), 140, 45).text_rows())
        self.assertIn("? links", unknown_text)
        self.assertIn("observation unknown", unknown_text)
        self.assertNotIn("0 links", unknown_text)

        stale = ExplorerViewModel.from_mapping({
            "peer_edges": {"links": [[ ["a1", "claude"], ["b1", "codex"], "steer", "1m", ""]]},
            "system": {"peer_observation": {"state": "unknown", "last_good": True}},
        })
        stale_text = "\n".join(draw(stale, NavState(view="PEERS"), 140, 45).text_rows())
        self.assertIn("1 link · last good · observation unknown", stale_text)


class ScreenWordLanguage(unittest.TestCase):
    """Route-page and seat words read the shared catalog: English unless Korean is declared."""

    def test_a_missing_catalog_never_blocks_a_screen(self):
        from tools.fleet import config
        from tools.fleet.explorer.runtime import ExplorerRuntime
        with mock.patch.object(config, "_ui_catalog", side_effect=FileNotFoundError("gone")):
            self.assertEqual(config.ui_text("fleet.memory.count_doing", count=3), "count doing 3")
            self.assertIn("loading", ExplorerRuntime().loading_frame(40, 10).text_rows()[5])

    def test_earlier_rounds_note(self):
        from tools.fleet.explorer.live import _node_detail
        node = {"id": "execute", "state": "active", "route_attempt_round": 3}
        with mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"}):
            self.assertEqual(_node_detail(node, 0)["rounds_note"], "R3 · 앞 2회는 이 기록 밖")
        with mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "English"}):
            self.assertEqual(_node_detail(node, 0)["rounds_note"], "R3 · earlier rounds outside this record: 2")
            self.assertEqual(_node_detail({**node, "route_attempt_round": 1}, 0)["rounds_note"], "")

    def test_seat_card_note_without_card_text(self):
        from tools.fleet.explorer.grid import CellGrid
        from tools.fleet.explorer.history import draw_seat_expand
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        for language, system, expected in (
                ("Korean", {"usage_api": {"freshness": "21:36"}}, "21:36 전에 남은 카드 본문이 없습니다."),
                ("Korean", {}, "읽은 카드 본문이 없습니다."),
                ("English", {"usage_api": {"freshness": "21:36"}}, "no card text was kept before 21:36."),
                ("English", {}, "no card text was read.")):
            with self.subTest(language=language, system=system), \
                    mock.patch.dict("os.environ", {"FLEET_NOW_LANG": language}):
                grid = CellGrid(160, 6)
                vm = ExplorerViewModel.from_mapping({"system": system})
                draw_seat_expand(grid, vm, NavState(view="HISTORY"), "a1", 0, 150, 0, 5,
                                 ("a1", "claude", 2, "10:00 · x"), "claude")
                self.assertIn(expected, "\n".join(grid.rows_text()))


class ParallelRouteProjection(unittest.TestCase):
    def test_idle_owner_state_stays_idle_while_active_route_stage_is_running(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fleet.explorer import live

        owner = SimpleNamespace(depth=1, route_node=None, liveness="idle", route_id="rt-owner",
                                harness="codex", model="gpt-6.1-sol", effort="high",
                                cwd="/synthetic/repo", attempt_id="owner-attempt",
                                work_projection=SimpleNamespace(result=None), slug="owner")
        slot = {"view": {"nodes": [{"id": "execute", "state": "active",
                                     "unit": "dev/backend"}],
                         "execution_topology": "staged", "capability": "autopilot-code",
                         "cwd": "/synthetic/repo", "slug": "owner"},
                "record": {"result": None, "slug": "owner"}, "jobs": [owner], "sessions": []}
        with patch.object(live, "_collect_routes", return_value={"rt-owner": slot}):
            routes, _nodes = live._route_records(
                {"resources": []}, [], {}, lambda _cwd: "repo", 1000)
        self.assertEqual(routes[0]["state"], "idle")
        self.assertEqual(routes[0]["activity"], "running")
        self.assertEqual(routes[0]["worker_stage"], "execute")

    def test_parallel_groups_and_connectors_follow_recorded_dependencies(self):
        from tools.fleet.explorer.live import _parallel_stage_details
        from tools.fleet.explorer.grid import CellGrid
        from tools.fleet.explorer.pages import _stage_grid

        source = [
            {"id": "prep", "state": "done", "depends_on": []},
            {"id": "worker-a", "state": "active", "depends_on": ["prep"],
             "parallel_group": "workers"},
            {"id": "worker-b", "state": "active", "depends_on": ["prep"],
             "parallel_group": "workers"},
            {"id": "finish", "state": "todo", "depends_on": ["worker-a", "worker-b"]},
        ]
        stages = _parallel_stage_details(source, 1000)
        self.assertEqual(len(stages), 3)
        self.assertEqual(stages[1]["members"], ("worker-a", "worker-b"))
        self.assertEqual(stages[1]["depends_on"], ("prep",))
        grid = CellGrid(140, 30)
        _stage_grid(grid, 2, 1, stages, 120, 27)
        text = "\n".join(grid.rows_text())
        self.assertIn("∥ 2-way", text)
        self.assertTrue(any(cell[0] in ("▶", "◀", "▼")
                            for row in grid.cells for cell in row))

    def test_recorded_route_graph_keeps_horizontal_flow_and_stacks_parallel_stages(self):
        from tools.fleet.explorer.live import _parallel_stage_details
        from tools.fleet.explorer.grid import CellGrid
        from tools.fleet.explorer.pages import _stage_grid

        def tops(text, names):
            found = {}
            for row, line in enumerate(text):
                for name in names:
                    column = line.find("┌─ " + name + " ")
                    if column >= 0 and name not in found:
                        found[name] = (row, column)
            return found

        serial = _parallel_stage_details(
            [{"id": name, "state": "done", "depends_on": [before] if before else []}
             for name, before in (("plan", None), ("execute", "plan"), ("test", "execute"),
                                  ("report", "test"))], 1000)
        grid = CellGrid(280, 40)
        _stage_grid(grid, 2, 1, serial, 262, 38)
        boxes = tops(grid.rows_text(), ("plan", "execute", "test", "report"))
        self.assertEqual({row for row, _column in boxes.values()}, {1})
        self.assertEqual(sorted(boxes, key=lambda name: boxes[name][1]),
                         ["plan", "execute", "test", "report"])
        self.assertEqual(sum(row.count("▶") for row in grid.rows_text()), 3)
        self.assertFalse(any("▼" in row for row in grid.rows_text()))

        framed = _parallel_stage_details([
            {"id": "frame", "state": "done", "depends_on": []},
            {"id": "frame-alternative", "state": "done", "depends_on": []},
            {"id": "plan", "state": "active", "depends_on": ["frame", "frame-alternative"]},
            {"id": "execute", "state": "todo", "depends_on": ["plan"]},
        ], 1000)
        grid = CellGrid(150, 30)
        _stage_grid(grid, 2, 1, framed, 140, 28)
        text = grid.rows_text()
        boxes = tops(text, ("frame", "frame-alternative", "plan", "execute"))
        self.assertEqual(boxes["frame"][1], boxes["frame-alternative"][1])
        self.assertLess(boxes["frame"][0], boxes["frame-alternative"][0])
        self.assertEqual(boxes["plan"][0], boxes["execute"][0])
        self.assertLess(boxes["frame"][1], boxes["plan"][1])
        joint = "".join(line[boxes["plan"][1] - 3] for line in text)
        self.assertIn("┐", joint)
        self.assertIn("├", joint)
        self.assertIn("┘", joint)

    def test_stage_array_order_without_dependency_evidence_draws_no_arrows(self):
        from tools.fleet.explorer.live import _parallel_stage_details
        from tools.fleet.explorer.grid import CellGrid
        from tools.fleet.explorer.pages import _stage_grid

        stages = _parallel_stage_details(
            [{"id": name, "state": "todo"} for name in ("one", "two", "three")], 1000)
        grid = CellGrid(140, 30)
        _stage_grid(grid, 2, 1, stages, 120, 27)
        self.assertFalse(any(cell[0] in ("▶", "◀", "▼")
                             for row in grid.cells for cell in row))


if __name__ == "__main__":
    unittest.main()
