"""Fleet audit T2/R1/R4: event time and generated text never mint current facts."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import sys
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import render, route, route_chain, titles, refresh_title
from fleet.model import DispatchJob, ResourceJob, Session, SubAgent, WorkProjection


def text(lines):
    return "\n".join("".join(t for t, _ in row) for row in lines if row)


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        render.set_show_all(True)
        self.addCleanup(render.set_show_all, False)
        self.now = time.time()

    def session(self, **kw):
        values = dict(harness="claude", pid=123, cwd="/work/project", session_id="sid",
                      liveness="idle", title="학습 구조 설명", mtime=self.now - 360)
        values.update(kw)
        return Session(**values)

    def lines(self, sessions=(), jobs=(), resources=(), width=160):
        with mock.patch.object(render, "_COMPUTE_HOSTS", None):
            return render._build_lines(list(sessions), list(jobs), "both", False, 0,
                                       term_width=width, resources=list(resources))

    def header(self, lines):
        return next(row for row in lines if row and any(t == "project" for t, _ in row))

    def test_transcript_mtime_is_file_update_never_completion(self):
        header = text([self.header(self.lines([self.session()]))])
        self.assertNotIn("✓", header)
        self.assertNotIn("파일 갱신", header)
        self.assertTrue(header.endswith("6m ago"))

    def test_idle_owner_running_resource_makes_project_active(self):
        owner = DispatchJob(key="autopilot-lab", slug="train", cwd="/work/project",
                            harness="codex", liveness="idle", attempt_id="at-one",
                            parent_sid="sid", is_child=True,
                            work_projection=WorkProjection(source="none"))
        child = ResourceJob(run_id="run", cwd=owner.cwd, project="project",
                            liveness="working", parent_attempt_id="at-one")
        owner.resource_children = [child]
        header = self.header(self.lines([self.session()], [owner], [child]))
        self.assertTrue(any(t == "●" and k.startswith("g_work") for t, k in header))
        self.assertNotIn("✓", text([header]))

    def test_recorded_result_names_task_and_event_age(self):
        outcome = {"present": True, "match": True, "terminal_gate_proven": True,
                   "closed_at": "2026-10-10T00:00:00Z"}
        result = route.result_projection({"slug": "모델 설명 자료", "route_id": "rt-one"}, outcome)
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["name"], "모델 설명 자료")
        session = self.session()
        session.route_chain = {"nodes": [{"result": result}]}
        header = text([self.header(self.lines([session]))])
        self.assertNotIn("최근 결과", header)
        self.assertIn("✓", header)
        self.assertIn("모델 설명 자료", header)

    def test_unproven_mismatched_or_pending_outcome_is_not_a_result(self):
        base = {"present": True, "match": True, "terminal_gate_proven": True,
                "closed_at": "2026-10-10T00:00:00Z"}
        for update in ({"terminal_gate_proven": False}, {"match": False},
                       {"finish_pending": True}, {"closed_at": "bad"},
                       {"terminal_gate_proven": 1}):
            with self.subTest(update=update):
                self.assertIsNone(route.result_projection({"slug": "task"}, dict(base, **update)))

    def test_long_result_name_preserves_result_and_event_age(self):
        s = self.session(liveness="working", work_projection=WorkProjection(
            result={"name": "매우 긴 작업 이름 " * 20, "result": "success", "at": self.now - 3600}))
        with mock.patch("time.time", return_value=self.now):
            for width in (80, 100, 160):
                with self.subTest(width=width):
                    header = text([self.header(self.lines([s], width=width))])
                    self.assertIn('✓', header)
                    self.assertIn('1h ago', header)
                    self.assertLessEqual(render._dw(header), width)

    def test_old_summary_keeps_its_age_without_repeating_idle(self):
        s = self.session(summary="보완 작업이 진행 중", summary_ts=self.now - 12 * 3600)
        row = text(render._context_detail_row(s, term_width=180))
        self.assertNotIn("대기", row)
        self.assertNotIn("마지막 요약", row)
        self.assertIn('12h ago', row)
        self.assertNotIn(" · · ", row)

    def test_unknown_summary_time_stays_historical_and_exec_comes_first(self):
        s = self.session(liveness="working", summary="작업 완료",
                         exec_tool={"name": "exec_command", "command": "python train.py"})
        row = text(render._context_detail_row(s, term_width=180))
        self.assertTrue(row.endswith("작업 완료 · age unknown"))
        self.assertLess(row.index("⚙"), row.index("작업 완료"))

    def test_old_completion_title_is_previous_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            s = self.session(harness=harness, title="하팅 작업 완료")
            s.title_ts = self.now - 48 * 3600
            with self.subTest(harness=harness):
                self.assertEqual(render._display_session_subject(s), s.title)
                self.assertEqual(render._subject_name_key(s, "nm_codex"), render._NAME_KEY_DIM[s.harness])
                s.runtime_name = "사용자 지정 이름"
                self.assertEqual(render._session_name(s), s.runtime_name)

    def test_sidecar_timestamp_only_attaches_to_matching_exact_text(self):
        s = self.session(title="하팅 작업 완료")
        with mock.patch.object(titles, "read", return_value={"title": s.title, "title_ts": self.now - 7200}):
            titles.annotate([s])
        self.assertEqual(s.title_ts, self.now - 7200)
        with mock.patch.object(titles, "read", return_value={"title": "다른 작업", "title_ts": self.now}):
            titles.annotate([s])
        self.assertIsNone(s.title_ts)

    def test_fresh_summary_is_historical_without_repeating_state(self):
        for state in ("working", "idle", "detached", "unknown"):
            s = self.session(liveness=state, summary="새 상태 설명", summary_ts=self.now)
            row = text(render._context_detail_row(s, term_width=180))
            with self.subTest(state=state):
                self.assertNotIn("마지막 요약", row)
                self.assertIn("새 상태 설명", row)
                for word in ("작업 중", "대기", "분리됨", "상태 미확인"):
                    self.assertNotIn(word, row)

    def test_new_route_boundary_makes_previous_title_historical(self):
        s = self.session(liveness="working")
        s.title_ts = self.now - 30
        s.route_chain = {"current": {"ts": self.now - 10}}
        self.assertEqual(render._display_session_subject(s), s.title)
        self.assertEqual(render._subject_name_key(s, "nm_codex"), render._NAME_KEY_DIM[s.harness])

    def test_current_title_needs_no_prefix_or_working_state(self):
        s = self.session(liveness="working", title="하팅 작업 완료", title_ts=self.now - 60)
        for state in ("working", "idle", "detached", "unknown"):
            for age in (60, 901, 12 * 3600, 24 * 3600):
                s.liveness, s.title_ts = state, self.now - age
                with self.subTest(state=state, age=age), mock.patch("time.time", return_value=self.now):
                    self.assertEqual(render._display_session_subject(s), s.title)

    def test_recorded_resource_failure_names_its_scope_not_workflow_success(self):
        child = ResourceJob(run_id="run", node="full-run", liveness="exited",
                            exit_code=2, ended_at=self.now - 360)
        result = render._group_last_result([], [], [child])
        self.assertEqual((result["name"], result["result"]), ("resource full run", "failure"))
        child.ended_at = None
        self.assertIsNone(render._group_last_result([], [], [child]))

    def test_route_chain_keeps_result_event_separate_from_route_start(self):
        record = {"route_id": "rt-one", "slug": "문서 수정", "capability": "autopilot-code"}
        outcome = {"present": True, "match": True, "terminal_gate_proven": True,
                   "closed_at": "2026-10-10T00:00:00Z"}
        chain = route_chain.assemble([
            {"route_id": "rt-one", "route_file": "/route.json", "ts": 123}],
            load_record=lambda *args: record, load_outcome=lambda *args: outcome)
        node = chain["current"]
        self.assertEqual(node["ts"], 123)
        self.assertGreater(node["result"]["at"], node["ts"])

    def test_failed_refresh_keeps_successful_title_time(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "FLEET_TITLE_STATE_DIR": tmp}), mock.patch.object(refresh_title, "run_worker", return_value=""):
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text('{"message":"please keep working"}\n')
            titles.write("sid", "Previous work complete", now=self.now - 48 * 3600)
            refresh_title.main(["--sid", "sid", "--transcript", str(transcript)])
            data = titles.read("sid")
            self.assertEqual(data["title_ts"], self.now - 48 * 3600)
            s = self.session(title=data["title"], liveness="working")
            titles.annotate([s])
            self.assertEqual(render._display_session_subject(s), s.title)
            self.assertEqual(render._subject_name_key(s, "nm_codex"), render._NAME_KEY_DIM[s.harness])

    def test_noop_refresh_keeps_successful_title_time(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FLEET_TITLE_STATE_DIR": tmp}):
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text('{"message":"please keep working"}\n')
            titles.write("sid", "Previous work complete", offset=transcript.stat().st_size,
                         now=self.now - 48 * 3600, cursor_kind="byte-offset-v1")
            refresh_title.main(["--sid", "sid", "--transcript", str(transcript)])
            self.assertEqual(titles.read("sid")["title_ts"], self.now - 48 * 3600)

    def test_legacy_sidecar_write_time_is_not_title_generation_time(self):
        s = self.session(liveness="working")
        with mock.patch.object(titles, "read", return_value={"title": s.title, "ts": self.now}):
            titles.annotate([s])
        self.assertIsNone(s.title_ts)
        self.assertEqual(render._display_session_subject(s), s.title)

    def test_invalid_title_time_cannot_prove_a_previous_subject(self):
        s = self.session()
        for timestamp in (None, True, "old", float("nan"), float("inf"), self.now + 1):
            s.title_ts = timestamp
            with self.subTest(timestamp=timestamp):
                self.assertEqual(render._display_session_subject(s), s.title)

    def test_age_suffix_survives_long_summary_in_both_render_paths(self):
        summary = "한글 요약을 그대로 보존하며 길게 설명합니다. " * 30
        s = self.session(summary=summary, summary_ts=self.now - 65)
        with mock.patch("time.time", return_value=self.now):
            for width in (80, 100, 200):
                for rows in (render._summary_row(summary, term_width=width, summary_ts=s.summary_ts),
                             render._context_detail_row(s, term_width=width)):
                    row = text(rows)
                    self.assertTrue(row.endswith(" · 1m ago"), row)
                    self.assertIn("한글", row)
                    self.assertLessEqual(render._dw(row), width)
                    self.assertNotIn("마지막 요약", row)

    def test_time_units_and_invalid_times_are_consistent(self):
        for minutes, expected in ((0, "0m ago"), (1, "1m ago"), (59, "59m ago"),
                                  (60, "1h ago"), (1439, "23h ago"), (1440, "1d ago"),
                                  (4320, "3d ago")):
            self.assertEqual(render._text_age(self.now - minutes * 60, self.now), expected)
        for timestamp in (None, True, -1, self.now + 1, float("nan"), float("inf")):
            self.assertEqual(render._text_age(timestamp, self.now), "age unknown")

    def test_long_project_name_preserves_file_age_and_result_at_80_columns(self):
        project = "한글프로젝트" * 9
        with mock.patch("time.time", return_value=self.now):
            for result in (None, {"name": "완료한 작업 " * 20, "result": "success",
                                  "at": self.now - 3600}):
                for tinted in (False, True):
                    with self.subTest(result=result, tinted=tinted), mock.patch.object(render, "_TINT_OK", tinted):
                        s = self.session(cwd="/work/" + project,
                                         work_projection=WorkProjection(result=result))
                        rows = self.lines([s], width=80)
                        header = next(render._plain(row) for row in rows if row and
                                      any(key in ("grp", "grp_cool", "grp_hot") for _, key in row))
                        self.assertTrue(header.endswith("1h ago" if result else "6m ago"), header)
                        self.assertLessEqual(render._dw(header), 79)
                        if result:
                            self.assertIn("✓", header)
                            self.assertIn("완료", header)

    def test_failed_refresh_header_preserves_age_after_final_clipping(self):
        health = {"snapshot": {"state": "failed", "age": 65,
                               "last_error": "snapshot-reader-failed-with-a-long-cause"},
                  "details": {"state": "failed", "last_error": "detail-reader-failed-with-a-long-cause"},
                  "compute_hosts": {"state": "stalled"}}
        with mock.patch("time.time", return_value=self.now), \
             mock.patch.object(render, "_REFRESH_HEALTH", health), \
             mock.patch.object(render, "_HEARTING", {"version": "v3.27.2", "install_method": "managed-release"}), \
             mock.patch.object(render, "_COMPUTE_HOSTS", None):
            for narrow in (False, True):
                rows = render._build_lines([], [], "both", narrow, 0, term_width=80)
                header = next(render._plain(row) for row in rows if row and
                              any(key == "hearting_name" for _, key in row))
                self.assertTrue(header.endswith(" · 1m ago"), header)
                self.assertIn("error:", header)
                self.assertLessEqual(render._dw(header), 79)

    def test_completion_observation_ages_preserve_execution_durations(self):
        with mock.patch("time.time", return_value=self.now):
            sa = SubAgent(agent_type="한글작업" * 20, active=False,
                          started_at=self.now - 106 * 60, ended_at=self.now - 102 * 60)
            strip = text(render._subagent_strip([sa], term_width=80))
            self.assertTrue(strip.endswith(" · 1h ago"), strip)
            normal = text(render._subagent_strip([SubAgent(agent_type="review", active=False,
                          started_at=sa.started_at, ended_at=sa.ended_at)]))
            self.assertIn("4m", normal)
            self.assertTrue(normal.endswith(" · 1h ago"), normal)
            for state in ("dead", "stale"):
                for width in (80, 200):
                    s = self.session(liveness=state, mtime=self.now - 225 * 60, elapsed_min=16)
                    visible = text(self.lines([s], width=width))
                    self.assertRegex(visible, r"done · 3h ago(?:\n|$)")
                    self.assertIn("16m", visible)
            degraded, _, _ = render._route_node_text({"id": "execute", "state": "degraded",
                "depends_on": ["plan"], "degradation": {"ts": self.now - 65,
                "fallback_hop": "inline", "reason": "capacity"}})
            self.assertTrue(degraded.endswith(" · 1m ago"), degraded)
            self.assertIn("←{plan}", degraded)

    def test_known_herdr_observation_labels_use_english_without_rewriting_other_errors(self):
        with mock.patch("time.time", return_value=self.now):
            for error, expected in (("herdr 조회 불가", "herdr unavailable"),
                                    ("pane 조회 불가", "pane unavailable"),
                                    ("사용자가 쓴 오류", "사용자가 쓴 오류")):
                visible = text(render._observation_lines({"herdr": {
                    "state": "failed", "age": 65, "last_error": error}}, term_width=80))
                self.assertIn(expected, visible)
                self.assertTrue(visible.endswith(" · 1m ago"), visible)

    def test_compact_route_states_do_not_add_a_row_to_an_existing_flow(self):
        labels = ("plan-check", "execute", "impl-review", "test", "report")
        for state, old_status in (("recovering", "…복구 대기 3m"),
                                  ("reconciling", "…종료 확인 3m"),
                                  ("attention", "! 확인 필요")):
            nodes = [{"id": name, "level": level, "state": state if not level else "planned",
                      "elapsed_min": 3 if not level else None, "depends_on": []}
                     for level, name in enumerate(labels)]
            baseline = labels[0] + " " + old_status + " › " + " › ".join(
                name + " ○" for name in labels[1:])
            room = render._dw(baseline)
            with self.subTest(state=state, width=room):
                rows = render._route_card_l2({"nodes": nodes}, max_width=room)
                self.assertEqual(len(rows), 1, text(rows))
                self.assertTrue(text(rows).endswith("report ○"), text(rows))
                self.assertLessEqual(render._dw(text(rows)), room)

    def test_owner_card_preserves_age_suffix_in_plain_and_tinted_80_columns(self):
        owner = DispatchJob(key="code", slug="owner", harness="codex", cwd="/work/project",
                            worker_type="owner", depth=1, liveness="working", is_child=True,
                            parent_sid="sid",
                            summary="이 요약은 매우 길어서 먼저 잘려야 합니다. " * 20,
                            summary_ts=self.now - 70)
        with mock.patch("time.time", return_value=self.now):
            for tinted in (False, True):
                with mock.patch.object(render, "_TINT_OK", tinted):
                    rows = self.lines([self.session(liveness="working")], jobs=[owner], width=80)
                    detail = next(render._plain(row) for row in rows if row and "1m ago" in render._plain(row))
                    self.assertIn("│", detail)
                    self.assertRegex(detail.rstrip(), r"1m ago +│$")

    def test_repeated_nonblocking_diagnostics_fold_but_blocking_sources_survive(self):
        diagnostics = [dict(kind="missing-registry", blocking=False, reason="registered-path-absent",
                            path="/work/%d/.agent_reports/jobs.log" % i) for i in range(6)]
        diagnostics += [dict(kind="missing-registry", blocking=True, path="/work/blocked-%d" % i)
                        for i in range(2)]
        diagnostics.append(dict(kind="malformed-index", blocking=False, reason="invalid", path="/work/index"))
        render.set_show_all(True)
        self.addCleanup(render.set_show_all, False)
        for width in (80, 200):
            rows = render._diagnostic_rows(diagnostics, term_width=width)
            self.assertEqual(len(rows), 4)
            self.assertIn("missing reference ×6 · nonblocking", text(rows[:1]))
            for i in range(2):
                self.assertIn("blocked-%d" % i, text(rows))
            for row in rows:
                self.assertLessEqual(render._dw(render._plain(row)), width)


if __name__ == "__main__":
    unittest.main()
