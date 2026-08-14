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
                _row("done", "honest-done-old", dispatch.DONE_AFTERGLOW_MIN + 1, _PIPE),
                _row("done", "honest-done-fresh", 5, _PIPE),
                _row("killed", "honest-killed", 1, _PIPE),
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

    def test_current_and_successor_survive_any_budget(self):
        text, _width = self._crumb(10)
        self.assertIn("frame(3-way)", text)
        self.assertIn("plan(2-way)", text)

    def test_fitting_route_is_untouched(self):
        text, _width = self._crumb(500)
        self.assertIn("report", text)
        self.assertNotIn("+", text.replace("(2-way)", "").replace("(3-way)", ""))
