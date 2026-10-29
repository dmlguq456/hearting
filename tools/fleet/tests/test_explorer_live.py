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

    def test_memory_lifecycle_is_summary_only_and_records_are_deduplicated(self):
        memory = {"recent": [], "by_repo": {"memory-store": [
            {"id": "a", "ts": "2026-10-24T08:00:00+09:00", "action": "tidy-add",
             "type": "note", "project": "actual-project", "snippet": "2026-10-24 [aa](claude sid): useful"},
            {"id": "a", "ts": "2026-10-24T08:00:00+09:00", "action": "tidy-add",
             "type": "note", "project": "actual-project", "snippet": "duplicate"},
            {"id": "e", "ts": "2026-10-24T07:00:00+09:00", "action": "lifecycle-expire",
             "type": "note", "project": "actual-project", "snippet": "expired row"},
        ]}}
        vm = build_view_model({"sessions": [], "resources": [], "memory": memory}, now=1792800000)
        rows = vm.memory[0][1]
        self.assertEqual(len(rows), 1)
        local = datetime.fromisoformat("2026-10-24T08:00:00+09:00").astimezone().strftime("%H:%M")
        self.assertEqual(tuple(rows[0]), (local, "saved", "note", "useful"))
        self.assertEqual(vm.system["memory_summary"]["expired"], 1)

    def test_first_tick_process_row_is_kept_and_gpu_processes_use_renderer_record_shape(self):
        snapshot = {"sessions": [{"harness": "codex", "pid": 44, "proc_start": 100,
                                  "session_id": "sid-exact", "session_tag": "a3",
                                  "cwd": "/work/SR_CorrNet-wt/branch", "liveness": "working"}],
                    "resources": [], "compute_hosts": {"hosts": [{"host": "host-a", "gpus": [
                        {"index": 0, "processes": [{"command": "python -B /tmp/resume.py --request-sha256 0123456789abcdef",
                            "used_memory_mib": 2560, "elapsed_s": 3600,
                            "cwd": "/work/SR_CorrNet-wt/branch",
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
            self.assertEqual(live._repo_of(lambda _cwd: "tool", "/data/alice/NN_Zoo/tool"), "tool")
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
        self.assertEqual(edges["links"][0][1], ["new", "claude"])
        self.assertEqual(edges["links"][0][2], "handoff")
        self.assertEqual(len(edges["links"]), 1)

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
    def vm(sessions=(), **extra):
        return build_view_model({"sessions": list(sessions), "project_of": lambda _cwd: "synthetic-repo",
                                 **extra}, now=1_000_000.0)

    def test_running_line_names_the_executing_child_and_how_long(self):
        session = {"harness": "claude", "session_id": "sid-a", "session_tag": "aa", "cwd": "/synthetic/repo",
                   "exec_child": {"comm": "python3", "leaf_etime_s": 4897}, "liveness": "working"}
        self.assertEqual(self.vm([session]).sessions[0]["running"], (("⚙ python3", "soft"), (" · 1h 21m", "dim")))

    def test_pane_falls_back_to_the_identity_evidence_pane(self):
        session = {"harness": "claude", "session_id": "sid-a", "session_tag": "aa", "cwd": "/synthetic/repo",
                   "session_identity_evidence": {"pane": "w1:p2"}}
        self.assertEqual(self.vm([session]).sessions[0]["pane"], "w1:p2")

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
        rows = self.vm(memory=memory).memory
        self.assertEqual([tuple(r[:3]) for r in rows[0][1]],
                         [("22:51", "saved", "decision"), ("21:00", "saved", "decision")])
        self.assertEqual(rows[0][1][0][3], "승인 5단계 중 ②를 마쳤다")

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


class BacklogAndCurrentTaskProjection(unittest.TestCase):
    def test_backlog_progress_uses_explicit_tags_exact_route_links_and_success(self):
        from tools.fleet.explorer.live import _memory_backlog

        records = [
            {"id": "todo", "canonical_id": "todo", "type": "project", "status": "active",
             "tags": ("backlog",), "headline": "unlinked task", "links": (), "artifact_refs": ()},
            {"id": "doing", "canonical_id": "doing", "type": "project", "status": "active",
             "tags": ("backlog",), "headline": "current task", "links": ("rt-current",),
             "artifact_refs": ()},
            {"id": "done", "canonical_id": "done", "type": "project", "status": "active",
             "tags": ("backlog",), "headline": "settled task", "links": (),
             "artifact_refs": ("artifact/rt-settled/report.md",)},
            {"id": "failed", "canonical_id": "failed", "type": "project", "status": "active",
             "tags": ("backlog",), "headline": "failed task", "links": ("rt-failed",),
             "artifact_refs": ()},
            {"id": "note", "canonical_id": "note", "type": "fact", "status": "active",
             "tags": (), "headline": "not backlog", "links": (), "artifact_refs": ()},
            {"id": "superseded", "canonical_id": "superseded", "type": "project",
             "status": "superseded", "tags": ("backlog",), "headline": "old", "links": ()},
        ]
        routes = [
            {"id": "rt-current", "state": "working", "outcome": None, "stages": []},
            {"id": "rt-settled", "state": "done", "outcome": "success", "stages": []},
            {"id": "rt-failed", "state": "done", "outcome": "failed", "stages": []},
        ]
        backlog = _memory_backlog({"memory": {"database": {
            "state": "success", "by_repo": {"repo": records}, "limited": False}}}, routes)
        progress = {row["id"]: row["progress"] for row in backlog["by_repo"]["repo"]}
        self.assertEqual(progress, {"todo": "TODO", "doing": "DOING", "done": "DONE",
                                    "failed": "TODO"})

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

    def test_memory_backlog_display_keeps_unavailable_unknown_and_body_free(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.state import NavState, reduce
        from tools.fleet.explorer.view_model import ExplorerViewModel

        unavailable = ExplorerViewModel.from_mapping({
            "system": {"memory_backlog": {"state": "unavailable", "by_repo": {}}}})
        text = "\n".join(draw(unavailable, NavState(view="MEMORY"), 140, 45).text_rows())
        self.assertIn("database unavailable · count unknown", text)
        self.assertNotIn("TODO 0", text)

        stale = ExplorerViewModel.from_mapping({
            "system": {"memory_backlog": {"state": "failed", "stale": True,
                                           "by_repo": {"repo": [{"id": "task-1",
                                                                   "title": "last good task",
                                                                   "progress": "TODO"}]}}}})
        stale_state = NavState(view="MEMORY")
        stale_frame = draw(stale, stale_state, 140, 45)
        stale_hit = next(hit for hit in stale_frame.hits
                         if hit.action == ("fold", "memory:backlog", "repo"))
        stale_state, _ = reduce(stale_state, {"type": "click", "action": stale_hit.action}, stale_frame)
        stale_text = "\n".join(draw(stale, stale_state, 140, 45).text_rows())
        self.assertIn("database failed · showing last good", stale_text)
        self.assertIn("TODO 1", stale_text)
        self.assertIn("last good task", stale_text)

    def test_peer_count_stays_unknown_or_marks_last_good_after_empty_unhealthy_source(self):
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.live import build_view_model
        from tools.fleet.explorer.state import NavState, reduce
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

        vm = ExplorerViewModel.from_mapping({
            "system": {"memory_backlog": {"state": "success", "limited": False,
                                           "by_repo": {"repo": [{"id": "task-1", "title": "Fix repo click",
                                                                   "progress": "TODO"}]}}}})
        state = NavState(view="MEMORY")
        frame = draw(vm, state, 140, 45)
        self.assertNotIn("Fix repo click", "\n".join(frame.text_rows()))
        hit = next(item for item in frame.hits if item.action == ("fold", "memory:backlog", "repo"))
        state, _ = reduce(state, {"type": "click", "action": hit.action}, frame)
        expanded = "\n".join(draw(vm, state, 140, 45).text_rows())
        self.assertIn("Fix repo click", expanded)
        self.assertIn("task-1", expanded)


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
