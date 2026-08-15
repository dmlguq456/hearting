#!/usr/bin/env python3
"""F-46 (PRD v29) — done afterglow.

A `done` registry row lingers for 15 minutes as a display-only row: dim `✓ done <elapsed>`,
no blink, and excluded from every working/idle/job census. `killed`/`cancelled` keep the
existing terminal path. Hermetic: timestamps are synthesized relative to the real clock, so
nothing here touches a live registry.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import model, render  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402


def _ts(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _row(status, slug, minutes_ago, pipe="capability=autopilot-code,harness=claude,depth=1"):
    return "\t".join([_ts(minutes_ago), status, "repo", "-", slug, pipe]) + "\n"


def _scan(rows):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "jobs.log")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(rows)
        return dispatch._scan_jobs_log(path, set())


class DoneAfterglowCollectorTest(unittest.TestCase):
    def test_recent_done_row_is_accepted_and_flagged(self):
        jobs, malformed = _scan([_row("done", "fresh-done", 5)])
        self.assertEqual(malformed, 0)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertTrue(job.afterglow)
        self.assertEqual(job.status, "done")     # registry word preserved verbatim
        self.assertEqual(job.elapsed_min, 5)     # elapsed measured from completion

    def test_done_row_past_the_window_is_dropped(self):
        jobs, _malformed = _scan([_row("done", "old-done", dispatch.DONE_AFTERGLOW_MIN + 1)])
        self.assertEqual(jobs, [])

    def test_killed_and_cancelled_never_afterglow(self):
        jobs, _malformed = _scan([_row("killed", "k", 1), _row("cancelled", "c", 1)])
        self.assertEqual(jobs, [])

    def test_running_row_is_unaffected(self):
        jobs, _malformed = _scan([_row("running", "live", 3)])
        self.assertEqual(len(jobs), 1)
        self.assertFalse(jobs[0].afterglow)

    def test_done_supersedes_the_earlier_running_row_for_the_same_slug(self):
        jobs, _malformed = _scan([_row("running", "one", 9), _row("done", "one", 4)])
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].afterglow)
        self.assertEqual(jobs[0].elapsed_min, 4)

    def test_afterglow_row_never_triggers_quick_multiple_live(self):
        pipe = ("capability=autopilot-code,harness=claude,depth=1,intensity=quick,"
                "route_id=rt-x,route_node=one-shot,execution_surface=registered-headless,"
                "registered_worker=1,fallback_hop=same-harness-headless,schema_version=3")
        jobs, _malformed = _scan([_row("done", "attempt-1", 2, pipe=pipe),
                                  _row("running", "attempt-2", 1, pipe=pipe)])
        live = [j for j in jobs if not j.afterglow]
        self.assertEqual(len(live), 1)
        self.assertNotEqual(live[0].attempt_contract_status, "invalid:quick-multiple-live")


class DoneAfterglowRenderTest(unittest.TestCase):
    def _job(self, afterglow=True, liveness="done"):
        return model.DispatchJob(key="autopilot-code", slug="fleet-glow-row",
                                 status="done" if afterglow else "running",
                                 afterglow=afterglow, liveness=liveness,
                                 harness="claude", elapsed_min=4, cwd="")

    def test_wide_row_shows_dim_done_with_elapsed_and_no_blink(self):
        segs = render._dispatch_row(self._job())
        text = "".join(part for part, _key in segs)
        # F-78: the state token is the word alone; the elapsed rides F-68's inline tag at
        # the end of the row, so it appears exactly once instead of twice.
        self.assertIn("done ✓", text)
        self.assertNotIn("done ✓ 4m", text)
        self.assertEqual(text.count("4m"), 1)
        status_keys = [key for part, key in segs if "done" in part]
        self.assertEqual(status_keys, ["dim"])
        # the glyph cell is the dim ✓, never a spinner frame or the live green key
        self.assertEqual([key for part, key in segs if part == "✓"], ["dim"])

    def test_narrow_row_keeps_identity_and_swaps_the_stage_slot(self):
        # F-64b: L2 keeps its leading elapsed + model cell; only the stage slot
        # becomes the steady `✓ done` token.
        _l1, l2 = render._dispatch_row_2line(self._job())
        text = "".join(part for part, _key in l2)
        self.assertIn("done ✓", text)
        self.assertIn("4m", text)

    def _depth2_job(self, **kw):
        return model.DispatchJob(key="code-execute", slug="stage-leg", depth=2,
                                 harness="claude", elapsed_min=4, cwd="", **kw)

    def test_depth2_afterglow_swaps_the_running_token_for_a_bare_check_done(self):
        # F-64a (v49 정정, user 2026-08-05 "depth=2에서 running 점멸만 done으로 바꾸고
        # 체크표시 하나"): the finished depth-2 worker shows a steady `✓ done` in the
        # micro-status slot — no elapsed duplication (the time column carries it) and
        # never a blinking frame.
        job = self._depth2_job(status="done", afterglow=True, liveness="done")
        segs = render._dispatch_row(job)
        text = "".join(part for part, _key in segs)
        self.assertIn("done ✓", text)
        self.assertNotIn("done ✓ 4m", text)
        self.assertEqual([key for part, key in segs if "done" in part], ["dim"])

    def test_depth2_stale_takes_the_same_minimal_check_done(self):
        job = self._depth2_job(status="running", liveness="stale")
        segs = render._dispatch_row(job)
        text = "".join(part for part, _key in segs)
        self.assertIn("done ✓", text)
        self.assertNotIn("done 4m", text)

    def test_depth1_afterglow_keeps_its_elapsed(self):
        """F-46 gave a finished depth-1 row its own counting-up clock, back when only such
        rows had one. F-68 later gave EVERY row an inline elapsed tag, so carrying it in the
        token too printed it twice (measured at 200 columns: `: done ✓ 8m  8m`) and, at 140,
        clipped the token itself to `done …`. The elapsed is kept — as the row's one tag."""
        segs = render._dispatch_row(self._job())
        text = "".join(part for part, _key in segs)
        self.assertEqual(text.count("4m"), 1)
        self.assertTrue(text.rstrip().endswith("4m"))

    def test_depth2_narrow_card_drops_the_elapsed_too(self):
        job = self._depth2_job(status="done", afterglow=True, liveness="done")
        _l1, l2 = render._dispatch_row_2line(job)
        text = "".join(part for part, _key in l2)
        self.assertIn("done ✓", text)
        self.assertNotIn("done ✓ 4m", text)

    def test_afterglow_is_excluded_from_the_pulse_census(self):
        live = model.DispatchJob(key="autopilot-code", slug="live", liveness="working",
                                 harness="claude")
        pulse = "".join(part for part, _key in render._pulse_segs([], [live, self._job()]))
        self.assertIn("↳ 1 job (1 working)", pulse)

    def test_afterglow_alone_counts_as_zero_jobs(self):
        pulse = "".join(part for part, _key in render._pulse_segs([], [self._job()]))
        self.assertNotIn("↳", pulse)


if __name__ == "__main__":
    unittest.main()
