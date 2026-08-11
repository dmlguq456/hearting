#!/usr/bin/env python3
"""F-50e/F-50f (PRD v34) — plugin-queue phase micro-status and threadId→rollout telemetry.

Hermetic: every case builds BOTH homes under temp dirs — the plugin state tree and a Codex
`sessions/YYYY/MM/DD/` tree — so nothing here reads the real plugin queue or the real
`~/.codex`. The join is exercised through `collect(home=..., codex_home=...)` end to end.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import model, projection, render  # noqa: E402
from fleet.collectors import codex_companion as cc  # noqa: E402

_SID = "201f59a8-21e7-4778-9f6f-c1b56c24e7ff"
_THREAD = "019f8cac-25cd-7cc2-89fa-7ed97b2f3eb9"
_WINDOW = 272000
_ACTIVE = 92000                       # (92000-12000)/(272000-12000) → 31%
_EXPECT_PCT = 31


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            ).isoformat().replace("+00:00", "Z")


def _job(**over):
    record = {
        "createdAt": _iso(10),
        "updatedAt": _iso(1),
        "startedAt": _iso(9),
        "id": "task-msctc65b-8gt40r",
        "kind": "task",
        "kindLabel": "rescue",
        "title": "Codex Task",
        "workspaceRoot": "/home/u/cairn",
        "jobClass": "task",
        "summary": "<task> Milkdown 빈 문단 직렬화를 추적하라.",
        "write": False,
        "sessionId": _SID,
        "status": "running",
        "phase": "investigating",
        "pid": None,
        "threadId": _THREAD,
        "turnId": "turn-1",
    }
    record.update(over)
    return record


class _Fixture(unittest.TestCase):
    def setUp(self):
        model.reset_state_tracker()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self._tmp.name, "claude")
        self.codex_home = os.path.join(self._tmp.name, "codex")
        self.state_dir = os.path.join(
            self.home, "plugins", "data", "codex-openai-codex", "state", "cairn-abc")
        os.makedirs(self.state_dir)

    def tearDown(self):
        self._tmp.cleanup()
        model.reset_state_tracker()

    def write_state(self, jobs):
        with open(os.path.join(self.state_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "config": {}, "jobs": jobs}, f)

    def write_rollout(self, thread_id=_THREAD, day=("2026", "08", "03"), token_count=True,
                      active=_ACTIVE, window=_WINDOW):
        """One rollout file named exactly like Codex names it."""
        day_dir = os.path.join(self.codex_home, "sessions", *day)
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(
            day_dir, "rollout-%s-%s-%sT00-00-00-%s.jsonl" % (day + (thread_id,)))
        lines = [{"type": "session_meta", "payload": {"cwd": "/home/u/cairn"}}]
        if token_count:
            lines.append({"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"model_context_window": window,
                         "last_token_usage": {"total_tokens": active},
                         "total_token_usage": {"total_tokens": active + 5000}}}})
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        return path

    def collect(self):
        return cc.collect(home=self.home, codex_home=self.codex_home)

    def row(self, **over):
        """One collected row with projections attached, exactly as a tick builds it."""
        self.write_state([_job(**over)])
        jobs = self.collect()
        projection.attach_projections([], jobs)
        return jobs[0]


class ThreadRolloutJoinTest(_Fixture):
    """F-50f — exact-1 sid match or an honest gap; never a guess."""

    def test_exact_one_match_fills_context_and_token_telemetry(self):
        path = self.write_rollout()
        job = self.row()
        self.assertEqual(job.plugin_telemetry["rollout"], path)
        self.assertEqual(job.plugin_telemetry["thread_id"], _THREAD)
        self.assertEqual(job.plugin_telemetry["context_used_pct"], _EXPECT_PCT)
        self.assertEqual(job.plugin_telemetry["active_context_tokens"], _ACTIVE)
        self.assertEqual(job.plugin_telemetry["context_window_tokens"], _WINDOW)
        self.assertEqual(job.context.used_pct, _EXPECT_PCT)
        self.assertEqual(job.context.source, "codex-rollout")

    def test_no_matching_rollout_is_an_honest_gap(self):
        self.write_rollout(thread_id="019f8cac-0000-4000-8000-000000000999")
        job = self.row()
        self.assertIsNone(job.plugin_telemetry)
        self.assertIsNone(job.context)

    def test_two_rollouts_with_the_same_sid_refuse_to_join(self):
        self.write_rollout(day=("2026", "08", "02"))
        self.write_rollout(day=("2026", "08", "03"))
        self.assertEqual(len(cc._rollout_for_thread(_THREAD, home=self.codex_home) or ""), 0)
        job = self.row()
        self.assertIsNone(job.plugin_telemetry)
        self.assertIsNone(job.context)

    def test_rollout_without_a_token_count_event_is_an_honest_gap(self):
        self.write_rollout(token_count=False)
        job = self.row()
        self.assertIsNone(job.plugin_telemetry)
        self.assertIsNone(job.context)

    def test_unparseable_token_count_line_is_an_honest_gap(self):
        path = self.write_rollout()
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"payload": broken "token_count"\n')
        job = self.row()
        self.assertIsNone(job.plugin_telemetry)

    def test_missing_or_non_sid_thread_id_never_reaches_the_filesystem(self):
        self.write_rollout()
        for thread_id in (None, "", "not-a-sid", "*", "../../etc"):
            self.assertIsNone(cc._rollout_for_thread(thread_id, home=self.codex_home))

    def test_a_registered_dispatch_row_still_has_no_context_window(self):
        # F-38 stays intact: the exception is scoped to the plugin-queue source alone.
        job = model.DispatchJob(key="code", slug="worker")
        job._context_evidence = model.ContextEvidence(used_pct=85, source="legacy")
        projection.attach_projections([], [job])
        self.assertIsNone(job.context)
        self.assertIsNone(job._context_evidence)


class LifecycleNonInterventionTest(_Fixture):
    """F-50f — telemetry is display-layer additive; the F-50b verdict never moves."""

    def _liveness(self, with_rollout, **over):
        if with_rollout:
            self.write_rollout()
        job = self.row(**over)
        return job.liveness, job.state_evidence

    def test_running_row_keeps_its_verdict_with_and_without_telemetry(self):
        bare, bare_ev = self._liveness(False)
        self.tearDown(); self.setUp()
        joined, joined_ev = self._liveness(True)
        self.assertEqual(bare, joined)
        self.assertEqual(bare_ev["inputs"], joined_ev["inputs"])
        self.assertEqual(bare_ev["rule"], joined_ev["rule"])

    def test_queued_row_keeps_its_verdict_with_telemetry(self):
        self.write_rollout()
        job = self.row(status="queued", phase="queued")
        self.assertEqual(job.liveness, "queued")
        self.assertIsNotNone(job.plugin_telemetry)

    def test_state_evidence_never_carries_the_telemetry(self):
        self.write_rollout()
        job = self.row()
        self.assertNotIn("context_used_pct", json.dumps(job.state_evidence))


class PhaseMicroStatusTest(_Fixture):
    """F-50e — the plugin's phase word verbatim, dim, in the micro-status slot only."""

    def _stage_text(self, job):
        segs = render._dispatch_stage_segs(job, job.key, job.stage, job.title)
        return "".join(text for text, _key in segs)

    def test_phase_renders_verbatim(self):
        job = self.row()
        self.assertEqual(self._stage_text(job), "investigating")

    def test_unknown_phase_word_is_shown_as_observed_not_mapped(self):
        job = self.row(phase="triaging-shards")
        self.assertEqual(self._stage_text(job), "triaging-shards")

    def test_phase_is_dim_only(self):
        job = self.row()
        self.assertEqual({key for _text, key in render._dispatch_stage_segs(
            job, job.key, job.stage, job.title)}, {"dim"})

    def test_absent_or_blank_phase_shows_nothing(self):
        for record in ({"phase": None}, {"phase": "   "}, {"phase": 7}):
            self.tearDown(); self.setUp()
            job = self.row(**record)
            self.assertEqual(self._stage_text(job), "")

    def test_phase_is_never_promoted_to_a_stage_or_breadcrumb(self):
        job = self.row()
        self.assertIsNone(job.stage)
        self.assertNotEqual(getattr(job.work_projection, "stage_label", None), "investigating")
        self.assertEqual(job.work_projection.source, "none")


class PluginAgentDisplayTest(_Fixture):
    """F-73 — plugin tasks use one subagent row, never a dispatch/session card."""

    def test_single_agent_row_keeps_phase_but_has_no_dispatch_marker(self):
        job = self.row()
        rows = render._plugin_agent_row(job, term_width=120)
        self.assertEqual(len(rows), 1)
        text = "".join(part for part, _key in rows[0])
        self.assertIn("⚡codex task", text)
        self.assertIn("investigating", text)
        self.assertNotIn("↳", text)

    def test_plugin_agent_never_opens_a_context_detail_row(self):
        self.write_rollout()
        job = self.row()
        self.assertIsNotNone(job.context)
        self.assertEqual(render._dispatch_summary_detail_row(job), [])

    def test_plugin_agent_is_not_a_dispatch_pulse_count(self):
        job = self.row()
        text = "".join(part for part, _key in render._pulse_segs([], [job]))
        self.assertNotIn("job", text)
        self.assertNotIn("↳", text)

    def test_wide_row_shows_the_phase_in_the_stage_zone(self):
        job = self.row()
        text = "".join(t for t, _k in render._plugin_agent_row(job)[0])
        self.assertIn("investigating", text)


class TelemetryRenderTest(_Fixture):
    """F-73 display — rollout telemetry stays JSON-only for plugin subagents."""

    def test_live_row_suppresses_the_context_gauge(self):
        self.write_rollout()
        job = self.row()
        rows = render._dispatch_summary_detail_row(job, depth=1, term_width=120)
        self.assertEqual(rows, [])

    def test_failed_join_also_has_no_session_detail(self):
        job = self.row()
        rows = render._dispatch_summary_detail_row(job, depth=1, term_width=120)
        self.assertEqual(rows, [])

    def test_finished_row_keeps_the_no_live_telemetry_lane(self):
        self.write_rollout()
        job = self.row(status="completed", phase="done", completedAt=_iso(1))
        self.assertTrue(job.afterglow)
        self.assertEqual(render._dispatch_summary_detail_row(job, depth=1, term_width=120), [])


class JsonShapeTest(_Fixture):
    """`--json` keeps the observed record verbatim and exposes telemetry additively."""

    def test_plugin_job_phase_is_preserved_verbatim(self):
        self.write_rollout()
        payload = self.row(phase="triaging-shards").to_dict()
        self.assertEqual(payload["plugin_job"]["phase"], "triaging-shards")
        self.assertEqual(payload["plugin_job"]["threadId"], _THREAD)

    def test_telemetry_is_an_additive_field(self):
        self.write_rollout()
        payload = self.row().to_dict()
        self.assertEqual(payload["plugin_telemetry"]["context_used_pct"], _EXPECT_PCT)
        self.assertEqual(payload["context"]["used_pct"], _EXPECT_PCT)

    def test_json_without_a_join_carries_no_invented_telemetry(self):
        payload = self.row().to_dict()
        self.assertIsNone(payload.get("plugin_telemetry"))
        self.assertIsNone(payload.get("context"))


if __name__ == "__main__":
    unittest.main()
