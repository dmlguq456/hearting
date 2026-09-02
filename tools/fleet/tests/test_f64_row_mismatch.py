#!/usr/bin/env python3
"""F-64 — process truth outranks a terminal registry word.

A registry row whose newest state is terminal (done/killed/cancelled) but whose
process evidence says the attempt still runs must stay visible as live work:
either the row's own (pid, start) identity is alive, or a live harness process
carries the row's attempt id (a detached launch records the already-exited
wrapper pid, so the second proof is load-bearing). Observed 2026-08-14: a
completion supervisor closed a multi-turn codex attempt at its first
turn.completed and the still-running dispatch vanished from the screen.
Hermetic: registries are temp files, process taps are mocked.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import model  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402


def _ts(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _row(status, slug, minutes_ago, pipe):
    return "\t".join([_ts(minutes_ago), status, "repo", "-", slug, pipe]) + "\n"


_PIPE = ("capability=autopilot-code,harness=codex,depth=1,"
         "pid=4242,pid_start=777,attempt_id=att-f64")


def _pipe_for(attempt_id):
    # F-97e: identity is attempt-scoped, so three logically distinct jobs need
    # three distinct attempt ids even when they otherwise share a pipe shape.
    return _PIPE.replace("attempt_id=att-f64", "attempt_id=" + attempt_id)


def _scan(rows, live_attempt_ids=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "jobs.log")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(rows)
        return dispatch._scan_jobs_log(path, set(), live_attempt_ids=live_attempt_ids)


def _proc_start(value):
    return mock.patch.object(
        dispatch.procscan, "read_proc_start",
        side_effect=lambda pid: value if int(pid) == 4242 else None,
    )


class TerminalRowMismatchCollectorTest(unittest.TestCase):
    def test_done_row_with_live_recorded_pid_survives(self):
        with _proc_start("777"):
            jobs, malformed = _scan([_row("done", "lying-done", 30, _PIPE)])
        self.assertEqual(malformed, 0)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertTrue(job.row_terminal_mismatch)
        self.assertFalse(job.afterglow)
        self.assertEqual(job.status, "done")     # registry word preserved verbatim

    def test_done_row_with_dead_pid_but_live_attempt_process_survives(self):
        # Detached-launch shape: the recorded wrapper pid is gone, the worker
        # process (found by its attempt id env) lives on.
        with _proc_start(None):
            jobs, _malformed = _scan(
                [_row("done", "wrapper-gone", 30, _PIPE)],
                live_attempt_ids={"att-f64"},
            )
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].row_terminal_mismatch)

    def test_killed_row_with_live_pid_survives(self):
        with _proc_start("777"):
            jobs, _malformed = _scan([_row("killed", "killed-but-alive", 1, _PIPE)])
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].row_terminal_mismatch)

    def test_dead_terminal_row_keeps_existing_drop_and_afterglow(self):
        with _proc_start(None):
            jobs, _malformed = _scan([
                _row("done", "honest-done-old", dispatch.DONE_AFTERGLOW_MIN + 1,
                     _pipe_for("att-f64-old")),
                _row("done", "honest-done-fresh", 5, _pipe_for("att-f64-fresh")),
                _row("killed", "honest-killed", 1, _pipe_for("att-f64-killed")),
            ])
        by_slug = {j.slug: j for j in jobs}
        self.assertNotIn("honest-done-old", by_slug)
        self.assertNotIn("honest-killed", by_slug)
        fresh = by_slug["honest-done-fresh"]
        self.assertTrue(fresh.afterglow)
        self.assertFalse(fresh.row_terminal_mismatch)

    def test_start_mismatch_never_revives_on_bare_pid_reuse(self):
        with _proc_start("999"):   # pid reused by an unrelated process
            jobs, _malformed = _scan(
                [_row("done", "pid-reused", dispatch.DONE_AFTERGLOW_MIN + 1, _PIPE)])
        self.assertEqual(jobs, [])


class TerminalRowMismatchClassifierTest(unittest.TestCase):
    def _ev(self, mismatch):
        return {
            "source": "jobs", "key": "code", "harness": "codex",
            "status": "done", "elapsed_min": 30, "slug": "s",
            "row_terminal_mismatch": mismatch,
        }

    def test_contradicted_done_classifies_working(self):
        state, evidence = model.classify_job(self._ev(True), 0)
        self.assertEqual(state, "working")
        self.assertEqual(evidence["raw_status"], "done")

    def test_uncontradicted_done_still_settles_done(self):
        state, _evidence = model.classify_job(self._ev(False), 0)
        self.assertEqual(state, "done")


if __name__ == "__main__":
    unittest.main()


class EarlyStageBreadcrumbTailFoldTest(unittest.TestCase):
    """F-65 — an early-stage long route must fold FUTURE stages to its zone budget.

    A 7-node strong route at its first node has no past stages to fold, overflowed
    its zone by ~45 cells, and the wrapped row visually severed the capsule card's
    ╰─── close rail (user 2026-08-14)."""

    def _crumb(self, max_width, cur=0):
        from fleet import render
        seq = [("frame(3-way)", "active" if cur == 0 else "done"),
               ("plan(2-way)", "active" if cur == 1 else "pending"),
               ("plan-check", "pending"), ("execute", "pending"),
               ("impl-review(2-way)", "pending"), ("test", "pending"),
               ("report", "pending")]
        segs = render._route_stage_segs(seq, working=False, max_width=max_width)
        text = "".join(t for t, _k in segs)
        return text, sum(render._dw(t) for t, _k in segs)

    def test_early_stage_folds_future_to_budget(self):
        text, width = self._crumb(42)
        self.assertLessEqual(width, 42)
        self.assertIn("frame(3-way)", text)      # current stage always survives
        self.assertIn("+", text)                  # folded-tail counter is visible

    def test_current_survives_any_budget_and_totals_stay_bounded(self):
        # F-68 revision: inside a framed card the border is a hard edge, so the
        # successor may fold into the +N counter and an oversized current token
        # ellipsizes — but the fold NEVER exceeds the budget and the current
        # stage stays recognizable at its head.
        text, width = self._crumb(10)
        self.assertLessEqual(width, 10)
        self.assertTrue(text.startswith("frame") or text.startswith("fra"))

    def test_fitting_route_is_untouched(self):
        text, _width = self._crumb(500)
        self.assertIn("report", text)
        self.assertNotIn("+", text.replace("(2-way)", "").replace("(3-way)", ""))


class AttemptAttributionTest(unittest.TestCase):
    """F-71 — one attempt is one row, and a proc row never guesses the harness.

    Observed: an OpenCode dispatch whose prompt merely contained the word
    "claude" was scanned as a hardcoded `harness="claude"` proc job and shown
    alongside its own registry row, so the board displayed three Claude
    sessions where one was OpenCode.
    """

    def test_proc_row_takes_harness_from_process_identity(self):
        line = "4242 opencode 05:00 /usr/bin/opencode run /autopilot-code prompt about claude"
        with mock.patch.object(dispatch.procscan, "_ps_lines", return_value=[line]), \
             mock.patch.object(dispatch.procscan, "read_environ", return_value={}), \
             mock.patch.object(dispatch.procscan, "read_proc_start", return_value="777"), \
             mock.patch.object(dispatch.os, "readlink", return_value="/w/repo"):
            jobs = dispatch._scan_processes()
        self.assertEqual([j.harness for j in jobs], ["opencode"])

    def test_registry_row_wins_an_attempt_shared_with_a_proc_row(self):
        proc = dispatch.DispatchJob(key="code", slug="job-a", cwd="/w/repo", source="proc",
                                    harness="claude", attempt_id="att-x", pid=11,
                                    proc_start="777", liveness="working")
        pipe = ("capability=autopilot-code,harness=opencode,depth=1,"
                "attempt_id=att-x,unit=_kernel/owner")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_row("running", "job-a", 2, pipe))
            rows, _malformed = dispatch._scan_jobs_log(
                path, {"job-a"}, canonical_attempts={"att-x"})
        self.assertEqual(len(rows), 1)          # slug dedup did not hide the twin
        self.assertEqual(rows[0].harness, "opencode")
        self.assertEqual(rows[0].source, "jobs")
        # and the proc twin's live pid identity carries over in collect()'s merge
        registry = {rows[0].attempt_id: rows[0]}
        twin = registry[proc.attempt_id]
        self.assertIsNotNone(twin)
