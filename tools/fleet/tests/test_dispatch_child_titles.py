#!/usr/bin/env python3
"""Hermetic unit tests — child-session sidecar titles adopted onto dispatch rows.

The title refresher schedules dispatched children like main sessions (user 2026-07-16);
`_adopt_child_titles` is the display half: the enriched child Session's title is copied
onto the DispatchJob row that represents it (is_child session rows are hidden), and
`_dispatch_row` prefers that title over the slug — same title → name → slug chain as
session rows. Stdlib unittest, no real ps/proc/home access.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import collectors as fleet_collectors  # noqa: E402
from fleet import render                          # noqa: E402
from fleet import titles                          # noqa: E402
from fleet.collectors import dispatch             # noqa: E402
from fleet.model import DispatchJob, Session      # noqa: E402


def _child(pid, cwd, title, harness="claude", sid=None, proc_start=None):
    return Session(harness=harness, pid=pid, cwd=cwd, session_id=sid or "sid-%d" % pid,
                   slug=os.path.basename(cwd), liveness="working", is_child=True,
                   title=title, proc_start=proc_start)


def _row_text(j, **kw):
    segs = render._dispatch_row(j, **kw)
    return "".join(part for part, _key in segs)


class AdoptChildTitlesTest(unittest.TestCase):
    def test_pid_join_adopts_the_child_title(self):
        # v16: pid alone is never the strong identity — exact (pid, proc_start) is
        # required on both sides (plan Step 2.4.2), so a real match sets proc_start too.
        child = _child(42, "/work/agent_setting-wt/fix-x", "Fix flaky title refresher tests",
                       proc_start="123456")
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/tmp/elsewhere",
                          harness="claude", pid=42, proc_start="123456",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.title, "Fix flaky title refresher tests")

    def test_pid_alone_without_proc_start_does_not_join(self):
        # A pid match with no comparable proc_start on either side must not fall through
        # to a pid-only join (PID reuse guard) — it also fails the cwd fallback here
        # because the job's cwd differs from the child's.
        child = _child(42, "/work/agent_setting-wt/fix-x", "Fix flaky title refresher tests")
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/tmp/elsewhere",
                          harness="claude", pid=42, is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertIsNone(job.title)

    def test_cwd_join_adopts_when_pid_is_unknown(self):
        child = _child(42, "/work/agent_setting-wt/fix-x", "Port memory rows to board")
        job = DispatchJob(key="autopilot-code", slug="fix-x",
                          cwd="/work/agent_setting-wt/fix-x", harness="claude",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.title, "Port memory rows to board")

    def test_cwd_join_requires_matching_harness(self):
        child = _child(42, "/work/agent_setting-wt/fix-x", "Codex owns this cwd",
                       harness="codex")
        job = DispatchJob(key="autopilot-code", slug="fix-x",
                          cwd="/work/agent_setting-wt/fix-x", harness="claude",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertIsNone(job.title)

    def test_ambiguous_cwd_is_refused(self):
        # Two titled children in one cwd: adopting either would be a guess — F-26
        # (misattribution is worse than absence) keeps the slug.
        kids = [_child(1, "/work/shared", "First worker task"),
                _child(2, "/work/shared", "Second worker task")]
        job = DispatchJob(key="autopilot-code", slug="shared", cwd="/work/shared",
                          harness="claude", is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles(kids, [job])
        self.assertIsNone(job.title)

    def test_untitled_children_leave_the_job_untouched(self):
        child = _child(42, "/work/agent_setting-wt/fix-x", None)
        job = DispatchJob(key="autopilot-code", slug="fix-x", pid=42,
                          cwd="/work/agent_setting-wt/fix-x", harness="claude",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertIsNone(job.title)

    def test_main_session_titles_are_never_adopted(self):
        main = Session(harness="claude", pid=42, cwd="/work/agent_setting",
                       session_id="main-sid", slug="agent_setting", liveness="working",
                       title="Main session work")
        job = DispatchJob(key="autopilot-code", slug="fix-x", pid=42,
                          cwd="/work/agent_setting", harness="claude",
                          is_child=True, liveness="working")
        fleet_collectors._adopt_child_titles([main], [job])
        self.assertIsNone(job.title)

    def test_exact_attempt_summary_survives_empty_session_sidecar(self):
        child = _child(42, "/work/fix-x", "Child title", proc_start="123")
        child.summary = None
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/work/fix-x",
                          harness="claude", pid=42, proc_start="123", is_child=True,
                          liveness="working", summary="Attempt-owned NOW")
        job._summary_sid = "dispatch-att-fix-x"
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertEqual(job.summary, "Attempt-owned NOW")
        self.assertTrue(job._child_session_associated)
        self.assertFalse(job._child_refresh_associated)

    def test_persistent_child_marks_refresh_source_as_authoritative(self):
        child = _child(42, "/work/fix-x", "Child title", proc_start="123")
        child._transcript_path = "/runtime/child.jsonl"
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/work/fix-x",
                          harness="claude", pid=42, proc_start="123", is_child=True,
                          liveness="working")
        fleet_collectors._adopt_child_titles([child], [job])
        self.assertTrue(job._child_refresh_associated)


class AttemptSummaryFallbackTest(unittest.TestCase):
    def test_selected_registry_log_directory_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_dir = os.path.join(tmp, ".harness", "dispatch")
            log_dir = os.path.join(registry_dir, "logs")
            os.makedirs(log_dir)
            registry_path = os.path.join(registry_dir, "jobs.log")
            path = os.path.join(log_dir, "child.att-runtime.claude.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{}\n")
            job = DispatchJob(
                key="code-test", slug="child", cwd="/work", harness="claude",
                is_child=True, liveness="working", attempt_id="att-runtime",
            )
            job._registry_path = registry_path
            job._log_file = path
            with mock.patch.object(dispatch, "_registry_home", return_value="/not/the/runtime"):
                self.assertEqual(dispatch._owned_attempt_log_path(job), os.path.realpath(path))

    def test_artifact_root_attempt_log_is_accepted_and_sidecar_is_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "home")
            artifact = os.path.join(tmp, "artifacts")
            state = os.path.join(tmp, "state")
            log_dir = os.path.join(artifact, "plans", "task", "_internal", "stage_logs")
            os.makedirs(log_dir)
            path = os.path.join(log_dir, "child.att-exact.codex.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write('{"type":"thread.started"}\n')
            job = DispatchJob(
                key="code-test", slug="child", cwd="/work", harness="codex",
                is_child=True, liveness="working", attempt_id="att-exact",
                artifact_root=artifact,
            )
            job._log_file = path
            with mock.patch.object(dispatch, "_registry_home", return_value=registry), \
                 mock.patch.dict(os.environ, {"FLEET_TITLE_STATE_DIR": state}):
                titles.write("dispatch-att-exact", "Attempt title", harness="codex",
                             summary="Attempt NOW")
                dispatch._enrich_attempt_summary(job)
            self.assertEqual(job._transcript_path, os.path.realpath(path))
            self.assertEqual(job._summary_sid, "dispatch-att-exact")
            self.assertEqual(job.title, "Attempt title")
            self.assertEqual(job.summary, "Attempt NOW")

    def test_launch_home_dispatch_logs_sibling_of_artifact_root_is_accepted(self):
        # Cross-home regression (2026-08-12): the launcher streamed the worker to
        # $AGENT_HOME/.dispatch/logs while the managed codex gateway registered the
        # row in a DIFFERENT runtime-home registry, so neither the registry-adjacent
        # roots nor the artifact root itself covered the declared log_file.
        with tempfile.TemporaryDirectory() as tmp:
            launch_home = os.path.join(tmp, "checkout")
            artifact = os.path.join(launch_home, ".agent_reports")
            log_dir = os.path.join(launch_home, ".dispatch", "logs")
            os.makedirs(artifact)
            os.makedirs(log_dir)
            registry_dir = os.path.join(tmp, "codex-home", ".harness", "dispatch")
            os.makedirs(registry_dir)
            path = os.path.join(log_dir, "child.att-crosshome.claude.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{}\n")
            job = DispatchJob(
                key="code-plan", slug="child", cwd="/work", harness="claude",
                is_child=True, liveness="working", attempt_id="att-crosshome",
                artifact_root=artifact,
            )
            job._registry_path = os.path.join(registry_dir, "jobs.log")
            job._log_file = path
            with mock.patch.object(dispatch, "_registry_home", return_value="/not/the/runtime"):
                self.assertEqual(dispatch._owned_attempt_log_path(job), os.path.realpath(path))

    def test_launch_home_sibling_outside_dispatch_logs_is_rejected(self):
        # The new root is exactly `<artifact-root parent>/.dispatch/logs`; a file
        # anywhere else in the launch home stays outside the fail-closed allowlist.
        with tempfile.TemporaryDirectory() as tmp:
            launch_home = os.path.join(tmp, "checkout")
            artifact = os.path.join(launch_home, ".agent_reports")
            stray_dir = os.path.join(launch_home, ".dispatch", "homes")
            os.makedirs(artifact)
            os.makedirs(stray_dir)
            path = os.path.join(stray_dir, "child.att-stray.claude.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{}\n")
            job = DispatchJob(
                key="code-plan", slug="child", cwd="/work", harness="claude",
                is_child=True, liveness="working", attempt_id="att-stray",
                artifact_root=artifact,
            )
            job._log_file = path
            with mock.patch.object(dispatch, "_registry_home", return_value="/not/the/runtime"):
                self.assertIsNone(dispatch._owned_attempt_log_path(job))

    def test_attempt_log_outside_allowed_roots_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "home")
            artifact = os.path.join(tmp, "artifacts")
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            path = os.path.join(outside, "child.att-exact.claude.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{}\n")
            job = DispatchJob(
                key="code-test", slug="child", cwd="/work", harness="claude",
                is_child=True, liveness="working", attempt_id="att-exact",
                artifact_root=artifact,
            )
            job._log_file = path
            with mock.patch.object(dispatch, "_registry_home", return_value=registry):
                self.assertIsNone(dispatch._owned_attempt_log_path(job))


class DispatchRowTitleTest(unittest.TestCase):
    def test_wide_row_prefers_the_adopted_title_over_the_slug(self):
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/w/fix-x",
                          harness="claude", is_child=True, liveness="working",
                          title="Fix refresher tests")
        text = _row_text(job)
        self.assertIn("Fix refresher tests", text)
        self.assertNotIn("fix-x", text.split("⎇")[0] if "⎇" in text else text)

    def test_wide_row_falls_back_to_the_slug_without_a_title(self):
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/w/fix-x",
                          harness="claude", is_child=True, liveness="working")
        self.assertIn("fix-x", _row_text(job))

    def test_long_title_clips_inside_the_name_zone(self):
        long_title = "An extremely long haiku title that cannot possibly fit the name zone"
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/w/fix-x",
                          harness="claude", is_child=True, liveness="working",
                          title=long_title)
        with_title = _row_text(job)
        job_slug = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/w/fix-x",
                               harness="claude", is_child=True, liveness="working")
        self.assertNotIn(long_title, with_title)
        self.assertIn("…", with_title)
        # The name zone budget is unchanged: a titled row is as long as a slug row.
        self.assertEqual(len(with_title), len(_row_text(job_slug)))

    def test_narrow_card_prefers_the_adopted_title(self):
        job = DispatchJob(key="autopilot-code", slug="fix-x", cwd="/w/fix-x",
                          harness="claude", is_child=True, liveness="working",
                          title="Narrow card title")
        l1, l2 = render._dispatch_row_2line(job)
        text = "".join(part for part, _key in l1 + l2)
        self.assertIn("Narrow card title", text)


if __name__ == "__main__":
    unittest.main()
