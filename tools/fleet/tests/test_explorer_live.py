"""VM mapping keeps unknowns explicit and freezes nested input."""
import unittest
import json
from datetime import datetime
from pathlib import Path
from unittest import mock

from tools.fleet.explorer.live import build_view_model
from tools.fleet.explorer.chrome import count_word
from tools.fleet.explorer.view_model import load_fixture
from tools.fleet.model import Session


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
        self.assertEqual(vm.system["gpu_processes"][0]["command"], "resume.py")
        self.assertEqual(vm.system["gpu_processes"][0]["vram"], "2.5 GB")
        self.assertEqual(vm.system["gpu_processes"][0]["age"], "1h 0m")

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
        self.assertEqual(vm.system["install"]["managed"], True)
        self.assertEqual(vm.system["install"]["pinned"], True)
        self.assertEqual(vm.system["install"]["distribution"], True)
        self.assertEqual(tuple(vm.system["runtimes"]), ("claude", "codex"))

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


if __name__ == "__main__":
    unittest.main()
