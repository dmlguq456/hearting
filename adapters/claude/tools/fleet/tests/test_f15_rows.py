#!/usr/bin/env python3
"""F-15 (분사 row 재설계) / F-16 (세션 표시명 짧게) — hermetic unit tests.

Runnable directly or via `python3 -m unittest fleet.tests.test_f15_rows -v` (from `tools/`).
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render               # noqa: E402
from fleet.collectors import dispatch  # noqa: E402
from fleet.model import DispatchJob, Session  # noqa: E402


class SlugStemTest(unittest.TestCase):

    def test_strips_execute_suffix(self):
        self.assertEqual(dispatch._slug_stem("fleet-ui-v2-execute"), "fleet-ui-v2")

    def test_strips_code_plan_suffix(self):
        self.assertEqual(dispatch._slug_stem("x-code-plan"), "x")

    def test_unmatched_slug_unchanged(self):
        self.assertEqual(dispatch._slug_stem("already"), "already")

    def test_no_over_strip_of_multiple_hyphen_tokens(self):
        self.assertEqual(dispatch._slug_stem("foo-bar-baz-test"), "foo-bar-baz")
        self.assertEqual(dispatch._slug_stem("foo-bar-baz"), "foo-bar-baz")


class DedupKeyTest(unittest.TestCase):

    def test_same_cwd_same_stem_merges_to_one_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = os.path.join(tmp, "wtA")
            os.makedirs(wt, exist_ok=True)
            jobs_log = os.path.join(tmp, "jobs.log")
            with open(jobs_log, "w") as f:
                f.write("\t".join([
                    "2026-07-10T01:00:00+00:00", "open", "repo", wt,
                    "fleet-ui-v2-execute",
                    "capability=autopilot-code,mode=dev,qa=standard",
                ]) + "\n")
            proc_job = DispatchJob(key="code", slug="fleet-ui-v2", cwd=wt, pid=1, harness="claude")
            with mock.patch.object(dispatch, "_scan_processes", return_value=[proc_job]), \
                 mock.patch.object(dispatch, "_live_claude_cwds", return_value={}), \
                 mock.patch.object(dispatch, "_dispatch_liveness",
                                   side_effect=lambda *a, **k: "working"):
                jobs = dispatch.collect(jobs_path=jobs_log)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].slug, "fleet-ui-v2")

    def test_different_cwd_same_stem_keeps_both_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt_a = os.path.join(tmp, "wtA")
            wt_b = os.path.join(tmp, "wtB")
            os.makedirs(wt_a, exist_ok=True)
            os.makedirs(wt_b, exist_ok=True)
            jobs_log = os.path.join(tmp, "jobs.log")
            with open(jobs_log, "w") as f:
                f.write("\t".join([
                    "2026-07-10T01:00:00+00:00", "open", "repo", wt_b,
                    "fleet-ui-v2-execute",
                    "capability=autopilot-code,mode=dev,qa=standard",
                ]) + "\n")
            proc_job = DispatchJob(key="code", slug="fleet-ui-v2", cwd=wt_a, pid=1, harness="claude")
            with mock.patch.object(dispatch, "_scan_processes", return_value=[proc_job]), \
                 mock.patch.object(dispatch, "_live_claude_cwds", return_value={}), \
                 mock.patch.object(dispatch, "_dispatch_liveness",
                                   side_effect=lambda *a, **k: "working"):
                jobs = dispatch.collect(jobs_path=jobs_log)
            self.assertEqual({j.slug for j in jobs},
                              {"fleet-ui-v2", "fleet-ui-v2-execute"})


class QueuedLivenessTest(unittest.TestCase):

    def _job(self, elapsed_min):
        return DispatchJob(key="code", slug="s", cwd="/tmp/x", source="jobs", status="open",
                           elapsed_min=elapsed_min)

    def test_open_no_transcript_small_elapsed_is_queued(self):
        job = self._job(2)
        with mock.patch.object(dispatch, "_job_liveness", return_value="dead"):
            self.assertEqual(dispatch._dispatch_liveness(job, now=0), "queued")

    def test_open_no_transcript_large_elapsed_is_dead(self):
        job = self._job(60)
        with mock.patch.object(dispatch, "_job_liveness", return_value="dead"):
            self.assertEqual(dispatch._dispatch_liveness(job, now=0), "dead")

    def test_open_with_transcript_activity_is_working(self):
        job = self._job(2)
        with mock.patch.object(dispatch, "_job_liveness", return_value="working"):
            self.assertEqual(dispatch._dispatch_liveness(job, now=0), "working")


class WorkingRegistryStageInductionTest(unittest.TestCase):

    def test_working_registry_row_stage_is_live_derived_not_raw_status(self):
        # v16: dispatch.collect() no longer calls live_stage() as an independent stage
        # authority (plan Step 2.2.1) — a working registry-only row's stage is cleared to
        # None here rather than showing the raw jobs.log status word ("open"), and its real
        # derivation happens later, once, in projection.py's WorkProjection resolver.
        with tempfile.TemporaryDirectory() as tmp:
            jobs_log = os.path.join(tmp, "jobs.log")
            wt = os.path.join(tmp, "wt")
            os.makedirs(wt, exist_ok=True)
            with open(jobs_log, "w") as f:
                f.write("\t".join([
                    "2026-07-10T01:00:00+00:00", "open", "repo", wt,
                    "docs-sync",
                    "capability=autopilot-draft,mode=default,qa=light",
                ]) + "\n")
            with mock.patch.object(dispatch, "_scan_processes", return_value=[]), \
                 mock.patch.object(dispatch, "_live_claude_cwds", return_value={}), \
                 mock.patch.object(dispatch, "_job_liveness", return_value="working"), \
                 mock.patch.object(dispatch, "live_stage", return_value="analyze") as m_live:
                jobs = dispatch.collect(jobs_path=jobs_log)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].liveness, "working")
            self.assertIsNone(jobs[0].stage)
            self.assertNotEqual(jobs[0].stage, "open")
            m_live.assert_not_called()

    def test_non_code_plans_path_never_derives_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, ".agent_reports", "plans", "nearby_docs")
            os.makedirs(os.path.join(plan, "test_logs"))
            self.assertEqual(dispatch.live_stage(tmp, "nearby-docs", "default", "autopilot-draft"), "default")

    def test_code_worker_derives_stage_from_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, ".agent_reports", "plans", "x_code-run")
            os.makedirs(os.path.join(plan, "test_logs"))
            with open(os.path.join(plan, "test_logs", "focused.log"), "w") as f:
                f.write("ok\n")
            self.assertEqual(dispatch.live_stage(tmp, "code-run", "code-test", "autopilot-code", "code-test"), "test")

    def test_source_only_worktree_stage_derives_via_artifact_root(self):
        # user 2026-07-20 "fleet이 여전히 pre에만 깜빡이는거야?" — a source-only worktree
        # (OPERATIONS §5.10) has no reports dir of its own; the registry's artifact_root
        # meta must win over the cwd heuristic or an inline conductor never leaves `pre`.
        with tempfile.TemporaryDirectory() as tmp:
            wt = os.path.join(tmp, "wt")            # source-only: NO .agent_reports here
            os.makedirs(wt)
            root = os.path.join(tmp, "primary", ".claude_reports")
            plan = os.path.join(root, "plans", "2026-07-20_reading-face-r8", "plan")
            os.makedirs(plan)
            with open(os.path.join(plan, "plan.md"), "w") as f:
                f.write("p\n")
            self.assertEqual(
                dispatch.live_stage(wt, "v95-reading-face-r8-resume", "code",
                                    "autopilot-code", artifact_root=root),
                "plan")
            # without the meta the old heuristic still falls back to the argv key
            self.assertEqual(
                dispatch.live_stage(wt, "v95-reading-face-r8-resume", "code",
                                    "autopilot-code"),
                "code")


class WideRowNoParentheticalTagTest(unittest.TestCase):

    def test_wide_row_name_zone_has_no_bracket_option_tag(self):
        job = DispatchJob(key="code", slug="top-conductor", depth=1, liveness="working",
                          mode="dev", qa="standard", qa_source="argv")
        segs = render._dispatch_row(job)
        name_text = next(text for text, key in segs if key in render.NAME_KEYS)
        self.assertNotIn("(", name_text)
        self.assertNotIn(")", name_text)

    def test_wide_row_child_name_zone_shows_stage_label(self):
        child = DispatchJob(key="code-execute", slug="fleet-ui-v2-execute", depth=2,
                            parent_slug="fleet-ui-v2", worker_role="code-execute",
                            liveness="working")
        segs = render._dispatch_row(child)
        text = "".join(t for t, _k in segs)
        self.assertIn("exec fleet-ui-v2-execute", text)

    def test_wide_row_child_does_not_duplicate_parent_breadcrumb(self):
        child = DispatchJob(key="code-execute", slug="fleet-ui-v2-execute", depth=2,
                            parent_slug="fleet-ui-v2", worker_role="code-execute",
                            liveness="working", stage="exec")
        segs = render._dispatch_row(child)
        text = "".join(t for t, _k in segs)
        self.assertNotIn("plan", text)
        self.assertNotIn("test", text)
        self.assertIn("running", text)


class FoldingTest(unittest.TestCase):

    def _emit(self, conductor, children, show_all=False):
        render.set_show_all(show_all)
        try:
            lines = render._build_lines([], [conductor] + children, section="dispatch",
                                        narrow=False, malformed=0, layout="wide")
        finally:
            render.set_show_all(False)
        return "\n".join("".join(t for t, _k in ln) for ln in lines if ln)

    def test_nonterminal_children_get_rows_and_done_child_folds(self):
        conductor = DispatchJob(key="code", slug="fleet-ui-v2", depth=1, liveness="idle",
                                stage="exec", worker_role="capability-owner")
        plan_c = DispatchJob(key="code-plan", slug="fleet-ui-v2-plan", depth=2,
                             parent_slug="fleet-ui-v2", worker_role="code-plan", liveness="done")
        exec_c = DispatchJob(key="code-execute", slug="fleet-ui-v2-execute", depth=2,
                             parent_slug="fleet-ui-v2", worker_role="code-execute",
                             liveness="working")
        test_c = DispatchJob(key="code-test", slug="fleet-ui-v2-test", depth=2,
                             parent_slug="fleet-ui-v2", worker_role="code-test",
                             liveness="queued")
        text = self._emit(conductor, [plan_c, exec_c, test_c])
        self.assertIn("exec fleet-ui-v2-execute", text)
        self.assertNotIn("plan fleet-ui-v2-plan", text)
        self.assertIn("test fleet-ui-v2-test", text)

        for state in ("idle", "unknown"):
            child = DispatchJob(key="code-test", slug="fleet-ui-v2-" + state,
                                depth=2, parent_slug="fleet-ui-v2",
                                worker_role="code-test", liveness=state)
            self.assertIn("fleet-ui-v2-" + state,
                          self._emit(conductor, [child]), state)

    def test_portable_persona_child_is_visible_with_exec_hue_without_owner_track(self):
        conductor = DispatchJob(key="code", slug="agent-home-code-owner", depth=1,
                                liveness="working", stage="plan",
                                worker_role="capability-owner")
        child = DispatchJob(key="code-execute", slug="agent-first-home-code-execute",
                            depth=2, parent_slug="agent-home-code-owner",
                            mode="dev/refactor", intensity="strong",
                            worker_role="development", liveness="working")
        render.set_show_all(False)
        try:
            lines = render._build_lines([], [conductor, child], section="dispatch",
                                        narrow=False, malformed=0, layout="wide",
                                        term_width=240)
        finally:
            render.set_show_all(False)
        text = "\n".join("".join(t for t, _key in line) for line in lines if line)
        running_keys = [key for line in lines if line for t, key in line if t == "running"]
        preparing_keys = [key for line in lines if line for t, key in line if t == "preparing…"]
        self.assertIn("exec agent-first-home-c", text)
        self.assertIn("code-execute(strong)", text)
        self.assertNotIn("dev/refactor·strong·development", text)
        self.assertTrue(any(key in ("stg1_on", "stg1_off") for key in running_keys))
        self.assertTrue(any(key in ("stg0_on", "stg0_off") for key in preparing_keys))
        self.assertNotIn("plan › exec › test", text)

    def test_dead_children_fold_without_integrated_alert_by_default(self):
        conductor = DispatchJob(key="code", slug="fleet-ui-v2", depth=1, liveness="idle",
                                stage="exec", worker_role="capability-owner")
        dead_c = DispatchJob(key="code-test", slug="fleet-ui-v2-test", depth=2,
                             parent_slug="fleet-ui-v2", worker_role="code-test",
                             liveness="dead")
        text = self._emit(conductor, [dead_c])
        self.assertNotIn("test fleet-ui-v2-test", text)
        self.assertNotIn("dead fleet-ui-v2-test", text)
        self.assertIn("fleet-ui-v2", text)

        all_text = self._emit(conductor, [dead_c], show_all=True)
        self.assertIn("test fleet-ui-v2-test", all_text)

    def test_stale_children_stay_visible(self):
        conductor = DispatchJob(key="code", slug="fleet-ui-v2", depth=1, liveness="idle",
                                stage="exec", worker_role="capability-owner")
        stale_c = DispatchJob(key="code-test", slug="fleet-ui-v2-test", depth=2,
                              parent_slug="fleet-ui-v2", worker_role="code-test",
                              liveness="stale")
        text = self._emit(conductor, [stale_c])
        self.assertIn("test fleet-ui-v2-test", text)

    def test_show_all_restores_folded_children(self):
        conductor = DispatchJob(key="code", slug="fleet-ui-v2", depth=1, liveness="idle",
                                stage="exec", worker_role="capability-owner")
        plan_c = DispatchJob(key="code-plan", slug="fleet-ui-v2-plan", depth=2,
                             parent_slug="fleet-ui-v2", worker_role="code-plan", liveness="done")
        text = self._emit(conductor, [plan_c], show_all=True)
        self.assertIn("plan fleet-ui-v2-plan", text)

    def test_unmatched_depth_two_parent_stays_visible_as_orphan(self):
        child = DispatchJob(key="code-plan", slug="live-depth-two", depth=2,
                            parent_slug="missing-owner", worker_role="code-plan",
                            liveness="working", cwd="/tmp/project")
        render.set_show_all(False)
        lines = render._build_lines([], [child], section="dispatch", narrow=False,
                                    malformed=0, layout="wide")
        text = "\n".join("".join(t for t, _k in line) for line in lines if line)
        self.assertIn("plan live-depth-two", text)


class StageDoneMarkerTest(unittest.TestCase):

    def test_stage_segs_marks_past_stages_done(self):
        segs = render._stage_segs("code", "exec", working=False)
        text = "".join(t for t, _k in segs)
        self.assertIn("plan✓", text)
        self.assertNotIn("exec✓", text)
        self.assertNotIn("test✓", text)


class LayoutCutoffTest(unittest.TestCase):

    def test_narrow_below_138(self):
        self.assertEqual(render._layout_mode(120), "narrow")

    def test_wide_at_or_above_138(self):
        self.assertEqual(render._layout_mode(140), "wide")

    def test_stack_below_70(self):
        self.assertEqual(render._layout_mode(60), "stack")


class TitleCapTest(unittest.TestCase):

    def test_session_title_keeps_legacy_cap_without_terminal_width(self):
        long_title = "a" * 60
        sess = Session(harness="claude", pid=1, cwd="", slug="s",
                       title=long_title, liveness="idle")
        segs = render._session_row(sess, narrow=False)
        # Select the name by segment KEY, not position: index 4 is the harness-field
        # PADDING, which used to be 22 cells (_HMW 33 - "claude code") and so slipped
        # under _TITLE_MAX by accident. F-54 widened it to 27 and exposed the mis-index —
        # the legacy 24-cell title cap itself never moved.
        name_seg = next(t for t, k in segs
                        if k in render.NAME_KEYS)
        self.assertLessEqual(render._dw(name_seg), render._TITLE_MAX)

    def test_wide_session_title_expands_with_terminal_width(self):
        long_title = "responsive session title " * 8
        sess = Session(harness="codex", pid=1, cwd="", slug="s",
                       title=long_title, liveness="idle")
        name_width = render._wide_name_width(168)
        segs = render._session_row(sess, narrow=False, name_width=name_width)
        name_text = next(t for t, k in segs
                         if k in render.NAME_KEYS)
        self.assertGreater(name_width, render._NW_S)
        self.assertGreater(render._dw(name_text), render._TITLE_MAX)
        self.assertLessEqual(render._dw(name_text), name_width)

    def test_wide_session_title_clips_cjk_on_display_cell_boundary(self):
        sess = Session(harness="codex", pid=1, cwd="", slug="s",
                       title="반응형세션제목" * 20, liveness="idle")
        name_width = render._wide_name_width(168)
        segs = render._session_row(sess, narrow=False, name_width=name_width)
        name_text = next(t for t, k in segs
                         if k in render.NAME_KEYS)
        self.assertLessEqual(render._dw(name_text), name_width)
        self.assertNotEqual(name_text[-1:], "")

    def test_narrow_session_title_reserves_suffixes(self):
        sess = Session(harness="codex", pid=1, cwd="", branch="feature/long",
                       slug="s", title="responsive session title " * 8, liveness="idle")
        l1, _l2 = render._session_row_2line(sess, term_width=60)
        self.assertLessEqual(sum(render._dw(text) for text, _key in l1), 54)
        text = "".join(part for part, _key in l1)
        self.assertIn("(feature/long)", text)

    def test_dispatch_composed_label_plus_slug_uses_the_real_wide_zone(self):
        # F-64 (v49, user 2026-08-05 "줄임 표시가 너무 널널한데? 공간이 떠"): with a known
        # terminal width the composed label+slug fills the responsive name zone like a
        # session row does — the old unconditional `_TITLE_MAX` clamp ellipsized at 24
        # cells and left the rest of the column blank before ` (branch)`.
        job = DispatchJob(key="code-execute", slug="a-very-long-dispatch-session-slug-name",
                          depth=2, parent_slug="p", worker_role="code-execute",
                          liveness="working")
        nw = render._wide_name_width(168)
        segs = render._dispatch_row(job, name_width=nw)
        name_text = next(text for text, key in segs if key in render.NAME_KEYS)
        self.assertLessEqual(len(name_text), nw)
        self.assertGreater(len(name_text), render._TITLE_MAX)

    def test_dispatch_name_keeps_the_compact_cap_without_a_terminal_width(self):
        # Hermetic/legacy callers (name_width=None) keep the F-15 24-cell compact cap.
        job = DispatchJob(key="code-execute", slug="a-very-long-dispatch-session-slug-name",
                          depth=2, parent_slug="p", worker_role="code-execute",
                          liveness="working")
        segs = render._dispatch_row(job)
        name_text = next(text for text, key in segs if key in render.NAME_KEYS)
        self.assertLessEqual(len(name_text), render._TITLE_MAX)


class OptsDialHierarchyTest(unittest.TestCase):
    """user 2026-07-20: "계층적으로 code (mode inten) / boot 순" — the dial reads
    capability (behaviour knobs) / environment, not a flat '·' chain mixing the axes."""

    @staticmethod
    def _dial(j):
        segs, w = render._opts_segs(j)
        text = "".join(t for t, _k in segs)
        assert w == len(text), "declared width must match rendered text"
        return text

    def test_full_hierarchy(self):
        j = DispatchJob(key="code", slug="s", depth=1, mode="dev",
                        intensity="standard", profile="boot")
        self.assertEqual(self._dial(j), "code(dev·std) / boot")

    def test_depth2_worker_shows_assigned_skill_not_parent_mode(self):
        j = DispatchJob(key="code-test", slug="s", depth=2, mode="dev/refactor",
                        intensity="strong", worker_role="qa")
        self.assertEqual(self._dial(j), "code-test(strong)")

    def test_depth2_route_node_is_the_assigned_skill(self):
        j = DispatchJob(key="spec", slug="s", depth=2, mode="dev/refactor",
                        intensity="strong", worker_role="deep maker",
                        route_node="research")
        self.assertEqual(self._dial(j), "research(strong)")
        self.assertEqual(render._dispatch_stage_label(j), "research")

    def test_authoritative_contract_and_worker_type_replace_legacy_role(self):
        child = DispatchJob(
            key="code",
            slug="s",
            depth=2,
            intensity="strong",
            worker_type="stage",
            assigned_contract="code-test",
            worker_role="deep maker",
            capability_owner="autopilot-code",
            route_node="test",
        )
        owner = DispatchJob(
            key="code",
            slug="o",
            depth=1,
            capability_mode="dev",
            intensity="strong",
            worker_type="owner",
            worker_role="deep maker",
            capability_owner="autopilot-code",
        )
        self.assertEqual(self._dial(child), "code-test(strong)")
        self.assertEqual(self._dial(owner), "code(dev·strong·owner)")

    def test_unit_is_a_distinct_dim_tail_not_a_skill_or_worker_role(self):
        child = DispatchJob(
            key="code",
            slug="s",
            depth=2,
            intensity="strong",
            worker_type="stage",
            assigned_contract="code-test",
            unit="verify",
            worker_role="deep maker",
            capability_owner="autopilot-code",
            route_node="test",
        )
        # F-78 dropped the `unit:` prefix — a unit is recognizable by its own shape, and
        # the five cells bought nothing on a dial measured at 50+ cells.
        self.assertEqual(self._dial(child), "code-test(strong) / verify")
        self.assertEqual(render._dispatch_stage_label(child), "test")

    def test_reserved_unit_namespace_underscore_is_hidden_only_in_display(self):
        owner = DispatchJob(
            key="code",
            slug="o",
            depth=1,
            capability_mode="dev",
            worker_type="owner",
            assigned_contract="autopilot-code",
            unit="_kernel/owner",
            capability_owner="autopilot-code",
        )
        # F-78: `_kernel/owner` beside the `·owner` knob is the row saying "owner" twice,
        # so the unit drops from the DISPLAY entirely. The reserved `_` namespace is still
        # never rewritten in the data — which is what this case exists to pin.
        self.assertEqual(self._dial(owner), "code(dev·owner)")
        self.assertEqual(owner.unit, "_kernel/owner")
        self.assertEqual(owner.to_dict()["unit"], "_kernel/owner")

    def test_route_node_compact_label_includes_unit(self):
        text, _key, _mark = render._route_node_text({
            "id": "test", "unit": "verify", "state": "pending",
        })
        self.assertEqual(text, "test[verify] ○")

    def test_entry_and_environment_tail_without_knobs(self):
        j = DispatchJob(key="code", slug="s", depth=1, profile="layer2")
        self.assertEqual(self._dial(j), "code / layer2")

    def test_intensity_left_the_profile_tail(self):
        # pre-hierarchy the tail rendered 'boot/std' — the knob now rides in the parens only.
        j = DispatchJob(key="code", slug="s", depth=1, intensity="standard", profile="boot")
        self.assertEqual(self._dial(j), "code(std) / boot")

    def test_role_is_a_knob_not_an_environment_tail(self):
        # user 2026-07-20: "owner의 위치가 애매" — the worker role acts like mode/intensity
        # (behaviour), so it rides the paren group's last slot, not 'boot/planner'.
        j = DispatchJob(key="code", slug="s", depth=1, mode="dev", intensity="standard",
                        worker_role="planner", profile="boot")
        self.assertEqual(self._dial(j), "code(dev·std·planner) / boot")

    def test_codex_orchestrator_phrase_normalizes_to_owner(self):
        # user 2026-07-20: "codex랑 claude가 서로 다르게 뜨는데" — the codex writer puts the
        # model-role phrase in worker_role; the conductor reads 'owner' on both harnesses.
        codex = DispatchJob(key="code", slug="s", depth=1, harness="codex",
                            capability_mode="dev", intensity="strong",
                            worker_type="owner", worker_role="deep orchestrator",
                            capability_owner="autopilot-code")
        claude = DispatchJob(key="code", slug="s", depth=1, harness="claude",
                             capability_mode="dev", intensity="strong",
                             worker_type="owner", worker_role="autopilot-code-owner",
                             capability_owner="autopilot-code")
        self.assertEqual(self._dial(codex), "code(dev·strong·owner)")
        self.assertEqual(self._dial(codex), self._dial(claude))

    def test_legacy_team_metadata_and_maker_phrase_drop_from_knobs(self):
        # Legacy 'plan-team' restates the stage and 'deep maker' restates the model cell;
        # neither is a current worker identity on the dial.
        j = DispatchJob(key="code-plan", slug="s", depth=2, harness="codex",
                        mode="dev/refactor", intensity="strong", worker_role="plan-team",
                        capability_owner="autopilot-code")
        self.assertEqual(self._dial(j), "code-plan(strong)")
        self.assertEqual(render._dispatch_stage_label(j), "plan")
        j2 = DispatchJob(key="code", slug="s", depth=1, worker_role="deep maker",
                         capability_owner="autopilot-code", mode="dev")
        self.assertEqual(self._dial(j2), "code(dev)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
