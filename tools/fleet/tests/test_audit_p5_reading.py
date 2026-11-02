"""Fleet 감사 P5: 이미 관측한 이름·호출자와 좁은 화면의 소비자 경계."""
import sys
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
from fleet import collectors, demo, fleet, model, render
from fleet.model import DispatchJob, Session
import session_tidy_runner as runner


class ReadingTest(unittest.TestCase):
    def setUp(self):
        from fleet.collectors import dispatch
        patch = mock.patch.object(dispatch.collect, "last_route_nodes", {})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(render, "_ROUTE_FOLD", {})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(render.set_show_all, False)
        self.addCleanup(render.set_process_view, False)
        self.addCleanup(render.set_compute_hosts, None)
        model.reset_parent_edge_tracker()
        self.addCleanup(model.reset_parent_edge_tracker)

    def test_r2_folded_gpu_keeps_config_name(self):
        resource = dict(host="mgpu-a1", index=0, model="RTX A6000", processes=[
            dict(pid=101, proc_start=11, command="python run.py --config AMI_8ch_varying_0_3spk_v3.yaml")])
        with mock.patch.object(render, "_ROUTE_FOLD", {}):
            text = "\n".join(render._plain(x) for x in render._gpu_resource_strip([resource], 80))
        self.assertIn("AMI_8ch_varying_0_3spk_v3", text)
        self.assertNotIn("--config", text)

    def test_r2_gpu_dedup_compares_entire_observed_name(self):
        name = "Acoustic Model alpha"
        resource = dict(host="h", index=0, processes=[dict(
            pid=101, proc_start=11, command="python train.py --name '" + name + "'")])
        titles = ((name, False), (name.upper(), False),
                  ("Acoustic Model beta", True), ("Acoustic Model alphabeta", True),
                  ("Acoustic Model alpha v2", True), ("Unrelated Work", True))
        for title, keeps_gpu_name in titles:
            child = model.ResourceJob(run_id="fixture-run", node="train", liveness="working",
                                      display_title=title, elapsed_min=3)
            for folded in (False, True):
                for width in (80, 100, 160, 168):
                    with self.subTest(title=title, folded=folded, width=width), \
                         mock.patch.object(render, "_gpu_commands_folded", return_value=folded):
                        rows = render._gpu_resource_strip([resource], term_width=width,
                                                          resource_children=[child])
                        text = "\n".join(render._plain(row) for row in rows)
                        self.assertIn(title, text)
                        self.assertEqual("(" + name + ")" in text, keeps_gpu_name, text)
                        if not keeps_gpu_name:
                            self.assertEqual(text.casefold().count(name.casefold()), 1)
                        self.assertTrue(all(render._dw(render._plain(row)) <= width - 1 for row in rows))

    def test_r2_unnamed_command_dedup_uses_complete_script_label(self):
        resource = dict(host="h", index=0, processes=[dict(
            pid=101, proc_start=11, command="python train.py")])
        for title, keeps_command in (("train.py", False), ("train.py v2", True),
                                      ("train.pyx", True)):
            child = model.ResourceJob(run_id="fixture-run", node="train", liveness="working",
                                      display_title=title, elapsed_min=3)
            for width in (80, 100, 160, 168):
                with self.subTest(title=title, width=width), \
                     mock.patch.object(render, "_gpu_commands_folded", return_value=False):
                    rows = render._gpu_resource_strip([resource], term_width=width,
                                                      resource_children=[child])
                    text = "\n".join(render._plain(row) for row in rows)
                    self.assertIn(title, text)
                    self.assertEqual("(python train.py)" in text, keeps_command, text)
                    self.assertTrue(all(render._dw(render._plain(row)) <= width - 1 for row in rows))

    def test_r2_gpu_owner_adds_person_not_repeated_execution_id(self):
        from fleet.collectors import compute_hosts
        for kind, identifier in (("run", "mgpu-a1-20261008-203517-css-AMI8var-v3"),
                                 ("job", "training-owner")):
            label = kind + ":" + identifier
            process = dict(pid=101, proc_start=11, command="python run.py --config AMI_8ch_varying_0_3spk_v3.yaml",
                           cwd="/work/project", owner=dict(kind=kind, id=identifier, label=label))
            snapshot = dict(configured=True, hosts=[dict(host="mgpu-a1", reachable=True,
                            gpus=[dict(index=0, processes=[process])])])
            entry = compute_hosts.unregistered_gpu(snapshot)[0]
            for show_all in (False, True):
                render.set_show_all(show_all)
                for width in (80, 100, 168):
                    row = render._plain(render._gpu_work_row(entry, width))
                    strip = "\n".join(render._plain(x) for x in render._gpu_work_strip([entry], width))
                    for text in (row, strip):
                        self.assertIn("AMI_8ch_varying_0_3spk_v3", text)
                        self.assertNotIn(label, text)
                        self.assertNotIn("미등록", text)
                    self.assertLessEqual(render._dw(row), width)
            with mock.patch.object(fleet, "_collect_memory", return_value=None), \
                 mock.patch.object(fleet, "_collect_governor", return_value=None):
                payload = json.loads(fleet._snapshot_json([], [], compute_host_snapshot=snapshot))
            self.assertEqual(payload["unregistered_gpu"][0]["owner_label"], label)
            self.assertEqual(payload["compute_hosts"]["hosts"], snapshot["hosts"])
            entry["owner_label"] = "[4d]"
            self.assertIn("[4d]", render._plain(render._gpu_work_row(entry, 168)))
            self.assertIn("[4d]", "\n".join(render._plain(x) for x in render._gpu_work_strip([entry], 168)))

    def test_r5_process_header_names_work_project_and_fleet(self):
        owner = DispatchJob(key="lab", cwd="/work/TF-Rehancer", slug="resume-training", pid=2,
                            proc_start="22", harness="codex", worker_type="owner", parent_sid="sid")
        parent = Session(harness="claude", pid=1, cwd=owner.cwd, session_id="sid", session_tag="4d")
        owner._parent_edge_sid = "sid"
        view = dict(key="route", route_id="rt-53b700ec81f04dc7", capability="autopilot-lab",
                    capability_mode="setup", effective_intensity="standard", nodes=[
                        dict(id="resume-run", state="active", level=0, job=owner, unit="_kernel/resource")])
        lines, _ = render._route_card(view, {(1, None): parent}, 100, 0, resource_owners=[owner])
        header = render._plain(lines[0])
        for token in ("TF-Rehancer", "resume-training", "[4d]"):
            self.assertIn(token, header)
        self.assertNotIn("rt-53b", header)

    def test_r5_worktree_uses_shared_project_identity(self):
        from fleet.display import project
        for cwd in (str(ROOT), "/work/hearting-wt/a-reading-fix"):
            self.assertEqual(project(cwd), model.project_of(cwd))
        self.assertEqual(project("/work/hearting-wt/a-reading-fix"), "hearting")

    def test_r5_rejected_parent_is_not_restored_in_header(self):
        for harness, state in (("claude", "working"), ("codex", "dead")):
            model.reset_parent_edge_tracker()
            parent = Session(harness=harness, pid=1, proc_start="1", cwd="/work/a",
                             session_id="same", session_tag="4d", liveness=state)
            owner = DispatchJob(key="lab", cwd=parent.cwd, slug="training", pid=2,
                                proc_start="2", harness="codex", worker_type="owner",
                                parent_sid="same", is_child=True)
            owner._registry_metadata = dict(parent_sid="same", parent_harness="codex")
            owner._registry_path = "/fixture/jobs.log"
            collectors.resolve_parent_edges([parent], [owner])
            self.assertIsNone(owner._parent_edge_sid)
            view = dict(key="route", capability="lab", nodes=[dict(id="train", state="active", level=0, job=owner)])
            header = render._plain(render._route_card(view, {(1, "1"): parent}, 100, 0, display_owner=owner)[0][0])
            self.assertNotIn("[4d]", header)

    def test_r7_header_retains_elapsed_and_folded_failure(self):
        view = dict(key="route", cwd="/work/a", slug="학습", capability="lab",
                    nodes=[dict(id="train", state="active", level=0, elapsed_min=120)])
        header = render._plain(render._route_card(view, {}, 168, 0)[0][0])
        self.assertIn("2h 00m", header)
        view["slug"] = "긴 실험 제목" * 30
        view["nodes"][0]["state"] = "failed"
        with mock.patch.object(render, "_ROUTE_FOLD", {"route": True}):
            for width in (80, 100, 168):
                header = render._plain(render._route_card(view, {}, width, 0)[0][0])
                self.assertIn('stage failed', header)
                self.assertLessEqual(render._dw(header), width)

    def test_r7_tinted_now_matches_actual_draw(self):
        class Screen:
            def __init__(self):
                self.calls = []
            def addstr(self, row, col, text, attr):
                self.calls.append((col, text))
        session = Session(harness="codex", pid=1, cwd="/work/a", session_id="s", ctx_pct=74,
                          liveness="working", summary="현재 실행 결과를 확인하며 사용자 승인 필요")
        with mock.patch.object(render, "_TINT_OK", True), mock.patch.object(render, "_key_attr", return_value=0):
            for width in (80, 100):
                lines = render._build_lines([session], [], "both", False, 0, term_width=width)
                row = next(x for x in lines if x and any(k == "now_main" for _t, k in x))
                plain = render._plain(row)
                screen = Screen()
                render._addline(screen, 0, row, width)
                drawn = "".join(text for _col, text in screen.calls).strip()
                self.assertEqual(drawn, plain.strip())
                self.assertTrue("승인 필요" in drawn or "…" in drawn)
                self.assertLessEqual(max(col + render._dw(text) for col, text in screen.calls), width)

    def test_r5_human_input_uses_short_english(self):
        self.assertEqual(render._INTERACTION_LABEL["decision"], 'answer needed')
        self.assertEqual(render._INTERACTION_LABEL["permission"], 'approval needed')

    def test_r5_normal_chain_keeps_identifiers_and_user_names(self):
        render.set_show_all(False)
        node = dict(label="route-frame", state="open", mode="debug", intensity="standard")
        text = render._plain(render._route_chain_node_segs(node, True, {}, True, True))
        self.assertIn("route-frame", text)
        self.assertNotIn("debug", text)
        node["label"] = "AMI_8ch_fix_2spk_v3"
        self.assertIn(node["label"], render._plain(render._route_chain_node_segs(node, True, {})))

    def test_r7_footer_preserves_keys_at_80_and_100(self):
        for width in (80, 100):
            segs = render._footer_segs(False, [], width)
            text = render._plain(segs)
            self.assertLessEqual(render._dw(text), width - 1)
            for key in ("q", "r", "a", "c", "w", "p", "jk", "s", "g/G"):
                self.assertIn(key, text)
            for word in ("선택", "이동", "갱신"):
                self.assertNotIn(word, text)
        text = render._plain(render._footer_segs(False, [], 168))
        for word in ("select", "scroll", "refresh", "p process"):
            self.assertIn(word, text)

    def test_r7_all_rows_fit_shared_width(self):
        sessions, jobs = demo.collect()
        memory = dict(recent=[dict(ts="2026-10-10T09:00:00", action="decision-record",
                                  tier="working", type="decision", actor="tidy-applier",
                                  snippet="긴 기억 내용 " * 20)], by_repo={}, totals={})
        render.set_show_all(True)
        for process in (False, True):
            render.set_process_view(process)
            for width in (80, 100):
                lines = render._build_lines(sessions, jobs, "both", False, 0,
                                            memory=memory, term_width=width)
                overflow = [render._plain(x) for x in lines if x and render._dw(render._plain(x)) > width]
                self.assertEqual(overflow, [], (process, width, overflow))

    def test_r8_context_unlabelled_and_symbol_legend_preserved(self):
        session = Session(harness="codex", pid=1, cwd="/work/a", session_id="s", ctx_pct=74,
                          liveness="working", summary="현재 작업")
        detail = "\n".join(render._plain(x) for x in render._context_detail_row(session, term_width=80))
        self.assertNotIn("문맥", detail)
        self.assertNotIn("작업 중", detail)
        self.assertIn("74%", detail)
        render.set_process_view(True)
        text = "\n".join(render._plain(x) for x in render._build_lines([], [], "both", False, 0, term_width=80))
        for word in ("session", "resource", "stage"):
            self.assertIn(word, text)
        self.assertIn("project: ● active", text)
        self.assertIn("↳ command", text)
        self.assertNotIn("문맥", text)
        render.set_show_all(True)
        render.set_process_view(False)
        session.liveness, session.session_tag = "dead", "4d"
        session.steward, session.herdr_attached = True, True
        for width in (80, 100):
            text = "\n".join(render._plain(x) for x in render._build_lines(
                [session], [], "both", False, 0, term_width=width))
            self.assertIn("0 working", text)
            self.assertIn("0 idle", text)
            legend = text[text.index("session:"):]
            for word in ("exited", "[id]", "steward", "pane", "project: ● active", "↳ command"):
                self.assertIn(word, legend)
            for word in ("문맥", "계획 검토", "좌석"):
                self.assertNotIn(word, legend)
        sessions = [Session(harness="codex", pid=i, cwd="/work/a", liveness=state)
                    for i, state in ((1, "working"), (2, "unused"))]
        jobs = [DispatchJob(key="code", slug="job-%d" % i, cwd="/work/a", liveness="working")
                for i in range(4)]
        lines = render._build_lines(sessions, jobs, "both", False, 0, term_width=60)
        pulse = next(render._plain(x) for x in lines if x and "  fleet " in render._plain(x))
        self.assertIn("1 working", pulse)
        self.assertIn("◌ 1 unused", pulse)
        self.assertRegex(pulse, r"↳ 4 jobs \(\S+ 4\)")
        self.assertLessEqual(render._dw(pulse), 59)

    def test_r8_unknown_gpu_symbol_has_meaning_when_inline_note_drops(self):
        resource = dict(host="mgpu-a1", index=0, model="RTX 6000 Ada Gen.",
                        access_only=True, telemetry_unknown=True, processes=[dict(
                            pid=101, proc_start=11,
                            command="python train.py --config AMI_8ch_varying_0_3spk_v3.yaml --engine_mode train")])
        for session_legend in (False, True):
            for folded in (False, True):
                for width in (80, 100, 160, 168):
                    with self.subTest(session=session_legend, folded=folded, width=width), \
                         mock.patch.object(render, "_gpu_commands_folded", return_value=folded):
                        rows = render._gpu_resource_strip([resource], term_width=width)
                        legend = render._reading_legend(width, session=session_legend)
                        line = "\n".join(render._plain(row) for row in rows)
                        explanation = "\n".join(render._plain(row) for row in legend)
                        self.assertIn("◇", line)
                        self.assertIn("AMI_8ch_varying_0_3spk_v3", line)
                        self.assertIn("◇ unknown", explanation)
                        self.assertTrue(all(render._dw(render._plain(row)) <= width - 1
                                            for row in rows + legend))
        # Unknown telemetry with no confirmed process must not claim access.
        gpu = dict(index=0, observation_source="fixture", processes=[])
        self.assertEqual(render._gpu_state(gpu), "unknown")
        token = render._plain(render._gpu_token(gpu, 79))
        self.assertIn('UTIL/VRAM unknown', token)
        self.assertNotIn('check device access', token)

    def test_h1_observation_links_only_confirmed_caller_continuity(self):
        parent = Session(harness="claude", pid=1, cwd="/work/a", session_id="new", liveness="working",
                         session_aliases=["old"])
        other = Session(harness="claude", pid=2, cwd=parent.cwd, session_id="other", liveness="working")
        job = DispatchJob(key="support", slug="helper", worker_type="support", cwd=parent.cwd)
        job.caller_sid, job.caller_harness, job.caller_pane = "old", "claude", "wB:p3N"
        collectors.resolve_parent_edges([parent, other], [job])
        self.assertEqual(getattr(job, "_parent_edge_sid", None), "new")
        self.assertIsNone(job.parent_sid)  # observation grants no dispatch/delivery authority
        job.caller_sid = "unconfirmed"
        collectors.resolve_parent_edges([parent, other], [job])
        self.assertIsNone(getattr(job, "_parent_edge_sid", None))

    def test_h1_unmatched_support_explicitly_project_level(self):
        support = DispatchJob(key="session-tidy", slug="helper", worker_type="support",
                              registered_worker=True, cwd="/work/a", liveness="working")
        parent = Session(harness="claude", pid=1, cwd=support.cwd, session_id="s", liveness="working")
        text = "\n".join(render._plain(x) for x in render._build_lines([parent], [support], "both", False, 0, term_width=100))
        self.assertIn('project support', text)

    def test_h1_runner_publishes_caller_as_observation(self):
        item = dict(harness="codex", sid="caller-sid", cwd="/work/a", seat=dict(kind="pane", pane="wB:p3N"))
        receipt = dict(job_registry="/test/jobs.log", attempt_id="att-helper")
        with mock.patch("dispatch_contract.annotate_attempt_row") as annotate:
            runner.record_caller_observation(item, receipt)
        self.assertEqual(annotate.call_args.args[2], dict(caller_harness="codex", caller_sid="caller-sid",
                                                        caller_pane="wB:p3N", caller_cwd="/work/a"))


if __name__ == "__main__":
    unittest.main()
