#!/usr/bin/env python3
"""F-18 (loop·drill·mem-워커 귀속 정밀화) — hermetic unit tests.

Runnable directly or via `python3 -m unittest fleet.tests.test_f18_attribution -v` (from `tools/`).
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import model                        # noqa: E402
from fleet import render                       # noqa: E402
from fleet.collectors import claude as claude_collector  # noqa: E402
from fleet.collectors import codex, dispatch, procscan  # noqa: E402
from fleet.model import DispatchJob, Session, project_of    # noqa: E402


class ProcscanTaggingTest(unittest.TestCase):
    """F-18b: scan() environ 마커 → Session.mem_worker."""

    def _scan_one(self, environ):
        ps_line = "123\tclaude\t00:05\t/usr/bin/claude"
        with mock.patch.object(procscan, "_ps_lines", return_value=[ps_line]), \
             mock.patch.object(procscan, "_read_cwd", return_value=("/home/u/proj", False)), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
             mock.patch.object(procscan, "_is_detached", return_value=False), \
             mock.patch.object(procscan, "read_environ", return_value=environ):
            sessions = procscan.scan()
        self.assertEqual(len(sessions), 1)
        return sessions[0]

    def test_mem_distill_marker_tags_true(self):
        s = self._scan_one({"MEM_DISTILL": "1"})
        self.assertTrue(s.mem_worker)

    def test_title_refresh_marker_tags_true(self):
        s = self._scan_one({"FLEET_TITLE_REFRESH": "1"})
        self.assertTrue(s.mem_worker)

    def test_no_marker_tags_false(self):
        s = self._scan_one({})
        self.assertFalse(s.mem_worker)

    def test_environ_read_failure_degrades_to_false(self):
        # read_environ() already returns {} on OSError — same code path as no-marker.
        s = self._scan_one({})
        self.assertFalse(s.mem_worker)

    def test_cross_harness_dispatch_markers_hide_children_but_not_ordinary_codex(self):
        self.assertTrue(self._scan_one({"AGENT_SESSION_ROLE": "worker"}).is_child)
        s = self._scan_one({"AGENT_DISPATCH_CHILD": "1"})
        self.assertTrue(s.is_child)
        self.assertTrue(self._scan_one({"AGENT_DISPATCH_DEPTH": "2"}).is_child)
        self.assertFalse(self._scan_one({}).is_child)


class CodexRolloutAttributionTest(unittest.TestCase):
    def test_prepare_tick_pairs_same_cwd_roots_by_exact_process_start(self):
        """2026-08-10 Invest incident: two direct TUIs must not stay anonymous.

        Root rollout timestamps uniquely fit the two /proc start identities, while a
        later subagent rollout in the same cwd is never a TUI fallback candidate.
        """
        first = Session(harness="codex", pid=10, cwd="/work/repo", proc_start="100")
        second = Session(harness="codex", pid=20, cwd="/work/repo", proc_start="200")
        one = "/r/rollout-2026-08-10T00-00-02-11111111-1111-1111-1111-111111111111.jsonl"
        two = "/r/rollout-2026-08-10T00-00-42-22222222-2222-2222-2222-222222222222.jsonl"
        child = "/r/rollout-2026-08-10T00-01-00-33333333-3333-3333-3333-333333333333.jsonl"
        meta = {
            one: {"cwd": "/work/repo", "timestamp": "2026-08-10T00:00:02Z", "source": "vscode"},
            two: {"cwd": "/work/repo", "timestamp": "2026-08-10T00:00:42Z", "source": "vscode"},
            child: {"cwd": "/work/repo", "timestamp": "2026-08-10T00:01:00Z",
                    "source": {"subagent": {"thread_spawn": {}}}},
        }
        with mock.patch.object(codex, "_proc_rollout", return_value=None), \
             mock.patch.object(codex, "_index", return_value={"/work/repo": [child, two, one]}), \
             mock.patch.object(codex, "_rollout_meta", side_effect=lambda path: meta[path]), \
             mock.patch.object(codex, "_process_started_at",
                               side_effect=lambda sess: {10: 1786320000.0, 20: 1786320040.0}[sess.pid]), \
             mock.patch.object(codex, "_tick_subagents", return_value={}):
            tick = codex.prepare_tick([first, second])
        self.assertEqual(tick.proc_paths, {10: one, 20: two})

    def test_prepare_tick_refuses_non_unique_exact_start_pairing(self):
        sessions = [
            Session(harness="codex", pid=10, cwd="/work/repo", proc_start="100"),
            Session(harness="codex", pid=20, cwd="/work/repo", proc_start="200"),
        ]
        one = "/r/rollout-2026-08-10T00-00-02-11111111-1111-1111-1111-111111111111.jsonl"
        two = "/r/rollout-2026-08-10T00-00-02-22222222-2222-2222-2222-222222222222.jsonl"
        with mock.patch.object(codex, "_proc_rollout", return_value=None), \
             mock.patch.object(codex, "_index", return_value={"/work/repo": [two, one]}), \
             mock.patch.object(codex, "_rollout_meta", return_value={
                 "cwd": "/work/repo", "timestamp": "2026-08-10T00:00:02Z", "source": "vscode"}), \
             mock.patch.object(codex, "_process_started_at", return_value=1786320000.0), \
             mock.patch.object(codex, "_tick_subagents", return_value={}):
            tick = codex.prepare_tick(sessions)
        self.assertEqual(tick.proc_paths, {})

    def test_two_same_cwd_sessions_remain_unknown_when_fallback_is_ambiguous(self):
        sessions = [Session(harness="codex", pid=1, cwd="/work/repo", elapsed_min=1),
                    Session(harness="codex", pid=2, cwd="/work/repo", elapsed_min=1)]
        with mock.patch.object(codex, "_index", return_value={"/work/repo": ["/r/one", "/r/two"]}), \
             mock.patch.object(codex, "_sid", side_effect=lambda p: {"/r/one": "one", "/r/two": "two"}[p]), \
             mock.patch.object(codex, "_rollout_meta", return_value={"timestamp": "2024-07-13T00:00:00Z"}), \
             mock.patch.object(codex.time, "time", return_value=1720828860):
            codex._FALLBACK_CLAIMS.update(ts=0, sids=set())
            self.assertEqual([codex._fallback_rollout(s, "/home/codex") for s in sessions], [None, None])

    def test_prepare_tick_reserves_owned_rollout_before_pid_ordered_fallback(self):
        older = Session(harness="codex", pid=10, cwd="/work/repo", elapsed_min=1)
        owner = Session(harness="codex", pid=20, cwd="/work/repo", elapsed_min=1)
        path = "/r/rollout-2026-07-15T00-00-00-11111111-1111-1111-1111-111111111111.jsonl"
        with mock.patch.object(codex, "_proc_rollout", side_effect=lambda pid, cwd, home: path if pid == 20 else None), \
             mock.patch.object(codex, "_index", return_value={"/work/repo": [path]}), \
             mock.patch.object(codex, "_rollout_meta", return_value={"timestamp": "2026-07-15T00:00:00Z"}), \
             mock.patch.object(codex.time, "time", return_value=1784073600):
            codex.prepare_tick([older, owner])
            self.assertIsNone(codex._fallback_rollout(older, "/home/codex"))
            self.assertEqual(codex._PROC_PATHS[20], path)

    def test_root_rollout_beats_open_subagent_rollout(self):
        fd_links = {"3": "/home/codex/sessions/a/rollout-2026-07-13T00-00-00-11111111-1111-1111-1111-111111111111.jsonl",
                    "4": "/home/codex/sessions/a/rollout-2026-07-13T00-00-00-22222222-2222-2222-2222-222222222222.jsonl"}
        with mock.patch.object(codex.os, "listdir", return_value=list(fd_links)), \
             mock.patch.object(codex.os, "readlink", side_effect=lambda p: fd_links[p.rsplit("/", 1)[-1]]), \
             mock.patch.object(codex.os.path, "realpath", side_effect=lambda p: p), \
             mock.patch.object(codex, "_rollout_meta", side_effect=lambda p: {
                 "cwd": "/work/repo",
                 "source": {"subagent": {"thread_spawn": {}}} if "2222" in p else "cli",
             }):
            path = codex._proc_rollout(99, "/work/repo", "/home/codex")
        self.assertIn("11111111", path)


class RenderDuplicateParentTest(unittest.TestCase):
    def test_duplicate_session_id_renders_child_tree_once(self):
        sessions = [Session(harness="codex", pid=1, cwd="/work/repo", session_id="same", slug="repo", liveness="working"),
                    Session(harness="codex", pid=2, cwd="/work/repo", session_id="same", slug="repo", liveness="working")]
        job = DispatchJob(key="autopilot-code", slug="child", cwd="/work/child", parent_sid="same", is_child=True,
                          harness="codex", capability_mode="dev", liveness="working")
        lines = render._build_lines(sessions, [job], section="both", narrow=False, malformed=0, layout="wide")
        text = "\n".join("".join(part for part, _key in line) for line in lines if line)
        self.assertEqual(text.count("dev"), 1)


class RenderMemExclusionTest(unittest.TestCase):
    """F-18b: render._build_lines 기본 제외·요약·`a`-토글 노출."""

    def tearDown(self):
        render.set_show_all(False)

    def _sessions(self):
        mem = Session(harness="claude", pid=1, cwd="/home/u/proj", slug="mem-worker",
                      title="distiller", liveness="working", mem_worker=True)
        normal = Session(harness="claude", pid=2, cwd="/home/u/proj", slug="work",
                         title="normal work", liveness="working")
        return mem, normal

    def _text(self, lines):
        return "\n".join("".join(t for t, _k in ln) for ln in lines if ln)

    def test_default_excludes_mem_from_project_but_shows_system_row(self):
        mem, normal = self._sessions()
        render.set_show_all(False)
        lines = render._build_lines([mem, normal], [], section="fleet", narrow=False,
                                    malformed=0, layout="wide")
        text = self._text(lines)
        self.assertIn("1 working", text)
        self.assertIn("⚙ system", text)
        self.assertIn("distiller", text)
        self.assertIn("🧠", text)   # legend/group badge summary still present

    def test_show_all_reveals_mem_row(self):
        mem, normal = self._sessions()
        render.set_show_all(True)
        lines = render._build_lines([mem, normal], [], section="fleet", narrow=False,
                                    malformed=0, layout="wide")
        text = self._text(lines)
        self.assertIn("mem ", text)

    def test_pure_mem_only_group_folds_but_legend_shows_total(self):
        mem = Session(harness="claude", pid=1, cwd="/home/u/onlymem", slug="mem-worker",
                     title="distiller", liveness="working", mem_worker=True)
        render.set_show_all(False)
        lines = render._build_lines([mem], [], section="fleet", narrow=False,
                                    malformed=0, layout="wide")
        text = self._text(lines)
        self.assertIn("🧠 1", text)


class DrillCaseExtractionTest(unittest.TestCase):
    """F-18a: registry slug / tmp cwd → drill case name."""

    def test_case_from_slug(self):
        self.assertEqual(
            dispatch._drill_case_from_slug("drill-claude-g_stage_dispatch-20260711160000-12345"),
            "g_stage_dispatch")

    def test_case_from_cwd(self):
        self.assertEqual(
            dispatch._drill_case_from_cwd("/tmp/drill-g_stage_dispatch-Ab3d/repo"),
            "g_stage_dispatch")

    def test_growing_case_cwd_normalizes_to_registry_case(self):
        self.assertEqual(
            dispatch._drill_case_from_cwd("/tmp/drill-growing_g_stage_dispatch-Ab3d/repo"),
            "g_stage_dispatch")

    def test_non_drill_slug_returns_none(self):
        self.assertIsNone(dispatch._drill_case_from_slug("fleet-ui-v2-execute"))

    def test_non_drill_cwd_returns_none(self):
        self.assertIsNone(dispatch._drill_case_from_cwd("/home/u/proj/repo"))

    def test_named_worktree_is_not_a_tmp_drill_fixture(self):
        cwd = "/home/u/agent_setting-wt/drill-fail-followup"
        self.assertIsNone(dispatch._drill_case_from_cwd(cwd))
        self.assertEqual(project_of(cwd), "agent_setting")


class DrillReconcileTest(unittest.TestCase):
    """F-18a: registry row(정본) + proc drill loop job → 1행 병합.

    F-25 이후 병합은 **증거 병합**이고 최종 판정은 단일 분류기(model.classify_job)가 내린다.
    따라서 흡수 결과는 `_reconcile_drill_rows` 직후의 `.liveness` 가 아니라 분류기를 통과시킨
    상태로 단언한다 — 같은 행동(live proc = ground truth)을 종단으로 검증한다.
    """

    def setUp(self):
        model.reset_state_tracker()

    def test_merges_matching_case_and_absorbs_proc_liveness(self):
        registry = DispatchJob(key="drill", slug="drill-claude-CASE-20260711160000-12345",
                               cwd="/tmp/drill-CASE-x/repo", pid=None, source="jobs",
                               liveness="queued")
        proc = DispatchJob(key="drill", slug="drill", worker_role="CASE",
                           cwd="/tmp/drill-CASE-x/repo", pid=999, liveness="working",
                           source="proc")
        jobs = dispatch._reconcile_drill_rows([registry, proc])
        self.assertEqual(len(jobs), 1)
        self.assertIs(jobs[0], registry)
        self.assertEqual(jobs[0].pid, 999)
        self.assertEqual(jobs[0]._proc_liveness, "working")     # tier-2 증거로 흡수됨
        # 분류기가 tier-2 증거를 mtime 유도값보다 우선해 working 을 낸다.
        self.assertEqual(dispatch._dispatch_liveness(jobs[0], now=0), "working")
        self.assertEqual(jobs[0].state_evidence["tier"], 2)

    def test_case_mismatch_keeps_both_rows(self):
        registry = DispatchJob(key="drill", slug="drill-claude-CASEA-20260711160000-12345",
                               cwd="/tmp/drill-CASEA-x/repo", pid=None, source="jobs",
                               liveness="queued")
        proc = DispatchJob(key="drill", slug="drill", worker_role="CASEB",
                           cwd="/tmp/drill-CASEB-y/repo", pid=999, liveness="working",
                           source="proc")
        jobs = dispatch._reconcile_drill_rows([registry, proc])
        self.assertEqual(len(jobs), 2)

    def test_runner_pid_merges_when_shell_cwd_has_no_case_signal(self):
        registry = DispatchJob(key="drill", slug="drill-codex-CASE-20260711160000-777",
                               cwd="/tmp/drill-CASE-x/repo", pid=None, source="jobs",
                               liveness="queued")
        proc = DispatchJob(key="drill", slug="drill", worker_role=None,
                           cwd="/home/u/agent_setting-wt/drill-fixes", pid=777,
                           liveness="working", source="proc")
        jobs = dispatch._reconcile_drill_rows([registry, proc])
        self.assertEqual(jobs, [registry])
        self.assertEqual(registry.pid, 777)
        self.assertEqual(registry._proc_liveness, "working")     # tier-2 증거로 흡수됨
        self.assertEqual(dispatch._dispatch_liveness(registry, now=0), "working")

    def test_registry_cwd_not_tmp_drill_skips_merge(self):
        registry = DispatchJob(key="drill", slug="drill-claude-CASE-20260711160000-12345",
                               cwd="/home/u/other/repo", pid=None, source="jobs",
                               liveness="queued")
        proc = DispatchJob(key="drill", slug="drill", worker_role="CASE",
                           cwd="/tmp/drill-CASE-x/repo", pid=999, liveness="working",
                           source="proc")
        jobs = dispatch._reconcile_drill_rows([registry, proc])
        self.assertEqual(len(jobs), 2)


class OrcaRelayDetachTest(unittest.TestCase):
    """Orca relay panes with no connected client → detached (third detached signal).

    Observed 2026-07-15: a closed remote client left a 33h claude "Sonnet 5" and an
    untitled codex TUI alive under `relay.js --detached`; both rendered as active idle
    top-level sessions."""

    def _scan_one(self, environ, dead):
        ps_line = "123\tclaude\t00:05\t/usr/bin/claude"
        with mock.patch.object(procscan, "_ps_lines", return_value=[ps_line]), \
             mock.patch.object(procscan, "_read_cwd", return_value=("/home/u/proj", False)), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
             mock.patch.object(procscan, "_is_detached", return_value=False), \
             mock.patch.object(procscan, "_orca_dead_socks",
                               side_effect=lambda socks: {s for s in socks if s in dead}), \
             mock.patch.object(procscan, "read_environ", return_value=environ):
            sessions = procscan.scan()
        self.assertEqual(len(sessions), 1)
        return sessions[0]

    def test_disconnected_relay_marks_detached(self):
        s = self._scan_one({"ORCA_RELAY_SOCKET_PATH": "/tmp/r.sock"}, dead={"/tmp/r.sock"})
        self.assertTrue(s.detached)

    def test_connected_relay_stays_attached(self):
        s = self._scan_one({"ORCA_RELAY_SOCKET_PATH": "/tmp/r.sock"}, dead=set())
        self.assertFalse(s.detached)

    def test_non_orca_session_unaffected(self):
        s = self._scan_one({}, dead={"/tmp/r.sock"})
        self.assertFalse(s.detached)

    def test_dead_socks_parses_ss_established_output(self):
        fake = mock.Mock(stdout="u_str ESTAB 0 0 /tmp/live.sock 123 * 456\n")
        with mock.patch.object(procscan.subprocess, "run", return_value=fake):
            self.assertEqual(procscan._orca_dead_socks({"/tmp/live.sock", "/tmp/dead.sock"}),
                             {"/tmp/dead.sock"})

    def test_dead_socks_fails_open_when_ss_unavailable(self):
        with mock.patch.object(procscan.subprocess, "run", side_effect=OSError):
            self.assertEqual(procscan._orca_dead_socks({"/tmp/x.sock"}), set())

    def test_dead_socks_empty_input_probes_nothing(self):
        with mock.patch.object(procscan.subprocess, "run",
                               side_effect=AssertionError("must not probe")) :
            self.assertEqual(procscan._orca_dead_socks(set()), set())


class ClaudeTranscriptAttributionTest(unittest.TestCase):
    """A known sid whose transcript is missing must not borrow a neighbor's .jsonl —
    the stolen mtime/title painted a dead same-cwd session as just-active."""

    def _proj(self, home, cwd="/w/repo"):
        proj = os.path.join(home, "projects", claude_collector._enc_cwd(cwd))
        os.makedirs(proj, exist_ok=True)
        return proj

    def test_known_sid_missing_transcript_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            proj = self._proj(home)
            with open(os.path.join(proj, "neighbor.jsonl"), "w") as f:
                f.write("{}\n")
            self.assertIsNone(claude_collector._newest_transcript_path(home, "/w/repo", "sid-a"))

    def test_known_sid_with_transcript_returns_it(self):
        with tempfile.TemporaryDirectory() as home:
            proj = self._proj(home)
            own = os.path.join(proj, "sid-a.jsonl")
            with open(own, "w") as f:
                f.write("{}\n")
            self.assertEqual(claude_collector._newest_transcript_path(home, "/w/repo", "sid-a"), own)

    def test_unknown_sid_still_falls_back_to_newest(self):
        with tempfile.TemporaryDirectory() as home:
            proj = self._proj(home)
            older, newer = os.path.join(proj, "a.jsonl"), os.path.join(proj, "b.jsonl")
            for p in (older, newer):
                with open(p, "w") as f:
                    f.write("{}\n")
            os.utime(older, (1, 1))
            self.assertEqual(claude_collector._newest_transcript_path(home, "/w/repo", None), newer)


if __name__ == "__main__":
    unittest.main()
