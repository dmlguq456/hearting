#!/usr/bin/env python3
"""F-50 (PRD v33) — openai-codex plugin-queue jobs as a read-only additive surface.

Fixture-based and hermetic: every case builds its own `plugins/data/codex-openai-codex/state`
tree under a temp home, so nothing here reads or touches the real plugin queue. The two
process cases use a real short-lived child process (the only honest way to exercise a
`/proc` existence + comm + `--job-id` check) and reap it in tearDown.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import model, render  # noqa: E402
from fleet.model import DispatchJob, Session  # noqa: E402
from fleet.collectors import codex_companion as cc  # noqa: E402
from fleet.collectors.dispatch import DONE_AFTERGLOW_MIN  # noqa: E402

_SID = "201f59a8-21e7-4778-9f6f-c1b56c24e7ff"


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            ).isoformat().replace("+00:00", "Z")


def _job(**over):
    """A record shaped exactly like an observed one (task-msctc65b-8gt40r)."""
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
        "summary": "<task> 저장소: /home/u/cairn-wt/x — Milkdown 빈 문단 직렬화를 추적하라.",
        "write": False,
        "sessionId": _SID,
        "status": "running",
        "phase": "running",
        "pid": None,
        "logFile": "/tmp/does-not-exist.log",
        "threadId": "6f0a1d5c-0000-4000-8000-000000000001",
        "turnId": "turn-1",
        "request": {"cwd": "/home/u/cairn", "model": None, "effort": None,
                    "prompt": "SECRET PROMPT BODY"},
    }
    record.update(over)
    return record


class _QueueFixture(unittest.TestCase):
    def setUp(self):
        model.reset_state_tracker()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.state_dir = os.path.join(
            self.home, "plugins", "data", "codex-openai-codex", "state", "cairn-abc")
        os.makedirs(self.state_dir)
        self._procs = []

    def tearDown(self):
        for proc in self._procs:
            proc.kill()
            proc.wait()
        self._tmp.cleanup()
        model.reset_state_tracker()

    def write_state(self, jobs, version=1, extra=None, broker=None):
        state = {"version": version, "config": {"stopReviewGate": True}, "jobs": jobs}
        if extra:
            state.update(extra)
        with open(os.path.join(self.state_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        if broker is not None:
            with open(os.path.join(self.state_dir, "broker.json"), "w", encoding="utf-8") as f:
                json.dump(broker, f)

    def write_raw_state(self, text):
        with open(os.path.join(self.state_dir, "state.json"), "w", encoding="utf-8") as f:
            f.write(text)

    def collect(self):
        return cc.collect(home=self.home)

    def spawn(self, marker):
        """A live process whose argv carries `marker` — a real pid to verify against."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", marker],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        with open("/proc/%d/comm" % proc.pid, encoding="utf-8") as f:
            comm = f.read().strip()
        return proc.pid, comm


class SchemaGuardTest(_QueueFixture):
    """F-50a — version guard and tolerant parsing; a skip is counted, never raised."""

    def test_unknown_version_skips_the_whole_file(self):
        self.write_state([_job()], version=2)
        self.assertEqual(self.collect(), [])
        self.assertEqual(cc.collect.last_malformed, 1)

    def test_unparseable_state_file_is_a_counted_skip(self):
        self.write_raw_state("{not json")
        self.assertEqual(self.collect(), [])
        self.assertEqual(cc.collect.last_malformed, 1)

    def test_malformed_row_is_skipped_and_counted_while_siblings_survive(self):
        self.write_state([_job(), "not-a-dict", {"id": "x"}])
        jobs = self.collect()
        self.assertEqual([j.slug for j in jobs], ["task-msctc65b-8gt40r"])
        self.assertEqual(cc.collect.last_malformed, 2)

    def test_missing_jobs_array_is_a_counted_skip(self):
        self.write_state(None)
        self.assertEqual(self.collect(), [])
        self.assertEqual(cc.collect.last_malformed, 1)

    def test_absent_plugin_tree_is_silent(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.assertEqual(cc.collect(home=empty.name), [])
        self.assertEqual(cc.collect.last_malformed, 0)


class RequiredFieldsTest(_QueueFixture):
    """F-50a — {id, status, workspaceRoot|request.cwd, createdAt} is the whole contract."""

    def test_each_missing_required_field_drops_the_row(self):
        for field in ("id", "status", "createdAt"):
            with self.subTest(field=field):
                record = _job()
                record.pop(field)
                self.write_state([record])
                self.assertEqual(self.collect(), [])
                self.assertEqual(cc.collect.last_malformed, 1)

    def test_missing_cwd_and_workspace_root_drops_the_row(self):
        record = _job(workspaceRoot=None)
        record["request"] = {"model": None}
        self.write_state([record])
        self.assertEqual(self.collect(), [])
        self.assertEqual(cc.collect.last_malformed, 1)

    def test_request_cwd_is_optional_and_workspace_root_is_the_fallback(self):
        record = _job()
        record.pop("request")
        self.write_state([record])
        job = self.collect()[0]
        self.assertEqual(job.cwd, "/home/u/cairn")

    def test_request_cwd_outranks_workspace_root(self):
        record = _job(workspaceRoot="/home/u/cairn")
        record["request"] = {"cwd": "/home/u/cairn-wt/branch"}
        self.write_state([record])
        job = self.collect()[0]
        self.assertEqual(job.cwd, "/home/u/cairn-wt/branch")


class RowModelTest(_QueueFixture):
    """F-50b — row identity, and the read-only-observer boundary."""

    def test_row_shape(self):
        self.write_state([_job()])
        job = self.collect()[0]
        self.assertEqual(job.source, "plugin-queue")
        self.assertEqual(job.surface_kind, "plugin-agent")
        self.assertEqual(job.harness, "codex")
        self.assertEqual(job.slug, "task-msctc65b-8gt40r")
        self.assertEqual(job.key, "rescue")            # kindLabel
        self.assertEqual(job.status, "running")        # verbatim plugin word
        self.assertEqual(job.elapsed_min, 9)           # startedAt, not createdAt
        self.assertEqual(job.parent_sid, _SID)
        self.assertTrue(job.is_child)

    def test_missing_kind_label_falls_back_to_codex_task(self):
        self.write_state([_job(kindLabel=None)])
        self.assertEqual(self.collect()[0].key, "codex-task")

    def test_row_carries_no_kill_target_identity(self):
        # A third-party queue's worker is not fleet's to signal (F-27 needs pid+proc_start).
        pid, comm = self.spawn("task-msctc65b-8gt40r")
        self.write_state([_job(pid=pid)])
        with unittest.mock.patch.object(cc, "_PLUGIN_COMMS", (comm,)):
            job = self.collect()[0]
        self.assertIsNone(job.pid)
        self.assertIsNone(job.proc_start)
        self.assertEqual(job.state_evidence["inputs"]["plugin_pid"], pid)

    def test_elapsed_uses_created_at_when_started_at_is_absent(self):
        record = _job()
        record.pop("startedAt")
        self.write_state([record])
        self.assertEqual(self.collect()[0].elapsed_min, 10)


class RunningLivenessTest(_QueueFixture):
    """F-50b — running is decided by pid evidence, with `~` (tier 3) when unverifiable."""

    def test_live_job_pid_with_matching_job_id_is_tier_2(self):
        pid, comm = self.spawn("task-msctc65b-8gt40r")
        self.write_state([_job(pid=pid)])
        with unittest.mock.patch.object(cc, "_PLUGIN_COMMS", (comm,)):
            job = self.collect()[0]
        self.assertEqual(job.liveness, "working")
        self.assertEqual(job.state_evidence["tier"], 2)
        self.assertFalse(job.state_evidence["derived"])
        self.assertEqual(job.state_evidence["inputs"]["pid_verified"], "job")

    def test_recycled_pid_without_the_job_id_falls_through_to_the_broker(self):
        other_pid, comm = self.spawn("some-other-job")
        broker_pid, _comm = self.spawn("broker")
        self.write_state([_job(pid=other_pid)], broker={"pid": broker_pid})
        with unittest.mock.patch.object(cc, "_PLUGIN_COMMS", (comm,)):
            job = self.collect()[0]
        self.assertEqual(job.liveness, "working")
        self.assertEqual(job.state_evidence["tier"], 2)
        self.assertEqual(job.state_evidence["inputs"]["pid_verified"], "broker")

    def test_no_verifiable_pid_is_a_derived_value(self):
        self.write_state([_job(pid=4000000000)])       # far above any live pid
        job = self.collect()[0]
        self.assertEqual(job.liveness, "working")
        self.assertEqual(job.state_evidence["tier"], 3)
        self.assertTrue(job.state_evidence["derived"])  # render marks this `~`
        self.assertIsNone(job.state_evidence["inputs"]["pid_verified"])

    def test_wrong_comm_at_that_pid_is_not_a_verification(self):
        pid, _comm = self.spawn("task-msctc65b-8gt40r")
        self.write_state([_job(pid=pid)])
        with unittest.mock.patch.object(cc, "_PLUGIN_COMMS", ("definitely-not-this",)):
            job = self.collect()[0]
        self.assertEqual(job.state_evidence["tier"], 3)

    def test_unreadable_broker_json_is_silent(self):
        self.write_state([_job(pid=None)], broker={"endpoint": "unix:/tmp/x"})  # no pid key
        job = self.collect()[0]
        self.assertEqual(job.state_evidence["tier"], 3)


class TerminalStatusTest(_QueueFixture):
    """F-50b — completed reuses the F-46 afterglow window; failures keep the dead path."""

    def _completed(self, minutes_ago, status="completed"):
        return _job(status=status, phase="done",
                    completedAt=_iso(minutes_ago), updatedAt=_iso(minutes_ago), pid=None)

    def test_completed_inside_the_window_is_a_dim_afterglow_row(self):
        self.write_state([self._completed(5)])
        job = self.collect()[0]
        self.assertTrue(job.afterglow)
        self.assertEqual(job.liveness, "done")
        self.assertEqual(job.status, "completed")      # plugin word preserved verbatim
        self.assertEqual(job.elapsed_min, 5)           # measured from completion (F-46)
        self.assertEqual(job.state_evidence["tier"], 1)

    def test_completed_past_the_window_disappears_and_is_not_malformed(self):
        self.write_state([self._completed(DONE_AFTERGLOW_MIN + 1)])
        self.assertEqual(self.collect(), [])
        self.assertEqual(cc.collect.last_malformed, 0)

    def test_failed_row_takes_the_dead_path_and_never_glows(self):
        self.write_state([self._completed(2, status="failed")])
        job = self.collect()[0]
        self.assertEqual(job.liveness, "dead")
        self.assertFalse(job.afterglow)

    def test_cancelled_row_maps_to_killed(self):
        self.write_state([self._completed(2, status="cancelled")])
        self.assertEqual(self.collect()[0].liveness, "killed")

    def test_queued_row_is_queued(self):
        self.write_state([_job(status="queued", phase="queued", pid=None)])
        self.assertEqual(self.collect()[0].liveness, "queued")

    def test_unknown_status_word_gets_no_synthesized_meaning(self):
        self.write_state([_job(status="reticulating", pid=None)])
        job = self.collect()[0]
        self.assertEqual(job.liveness, "unknown")
        self.assertEqual(job.state_evidence["raw_status"], "reticulating")


class NestingTest(_QueueFixture):
    """F-50c — exact `sessionId` == `Session.session_id`, else the orphan rule."""

    def _render(self, sessions, jobs, section="both"):
        lines = render._build_lines(sessions, jobs, section=section, narrow=False,
                                    malformed=0, layout="wide")
        return "\n".join("".join(part for part, _key in line) for line in lines if line)

    def _row(self):
        self.write_state([_job(pid=None)])
        return self.collect()[0]

    def _session(self, session_id, cwd="/home/u/cairn"):
        return Session(harness="claude", pid=101, cwd=cwd, session_id=session_id,
                       slug="cairn", title="parent", liveness="working")

    def test_exact_session_id_match_nests_the_row(self):
        text = self._render([self._session(_SID)], [self._row()])
        self.assertIn("⚡codex task", text)
        self.assertNotIn("↳", text)
        self.assertNotIn("dispatch", text)
        self.assertNotIn("(orphan)", text)

    def test_same_cwd_without_the_session_id_stays_an_orphan(self):
        # The plugin workspace root is NOT an attribution path (misattribution guard).
        text = self._render([self._session("a-different-session-id")], [self._row()])
        self.assertIn("(orphan)", text)

    def test_fleet_section_owns_plugin_agents_and_dispatch_section_does_not(self):
        parent = self._session(_SID)
        job = self._row()
        self.assertIn("⚡codex task", self._render([parent], [job], section="fleet"))
        self.assertNotIn("⚡codex task", self._render([parent], [job], section="dispatch"))

    def test_collect_all_never_reclassifies_a_same_cwd_session_as_a_child(self):
        from fleet import collectors
        session = self._session("a-different-session-id")
        collectors._mark_dispatch_child_sessions([session], [self._row()])
        self.assertFalse(session.is_child)

    def test_plugin_row_never_adopts_another_child_session_title(self):
        from fleet import collectors
        child = Session(harness="codex", pid=7, cwd="/home/u/cairn", session_id="child",
                        slug="cairn", title="borrowed title", liveness="working",
                        is_child=True)
        job = self._row()
        collectors._adopt_child_titles([child], [job])
        self.assertNotEqual(job.title, "borrowed title")
        self.assertIsNone(job.summary)


class DisplayAndJsonTest(_QueueFixture):
    """F-50d — summary head in the name zone, prompt never carried, `--json` verbatim."""

    def test_name_zone_uses_the_summary_head(self):
        self.write_state([_job(pid=None)])
        job = self.collect()[0]
        self.assertTrue(job.title.startswith("<task> 저장소:"))
        segs, _l2 = render._dispatch_row_2line(job)
        text = "".join(part for part, _key in segs)
        self.assertIn("<task> 저장소", text)           # head kept, tail clipped

    def test_long_summary_is_cut_from_the_tail(self):
        self.write_state([_job(summary="HEAD " + "x" * 500, pid=None)])
        job = self.collect()[0]
        self.assertTrue(job.title.startswith("HEAD "))
        self.assertLessEqual(len(job.title), cc._TITLE_MAX)

    def test_summary_newlines_collapse_to_one_line(self):
        self.write_state([_job(summary="first line\n\nsecond line", pid=None)])
        self.assertEqual(self.collect()[0].title, "first line second line")

    def test_json_preserves_the_observed_fields(self):
        record = _job(pid=None)
        self.write_state([record])
        payload = self.collect()[0].to_dict()
        observed = payload["plugin_job"]
        for name, value in record.items():
            if name == "request":
                continue
            self.assertEqual(observed[name], value, name)
        self.assertEqual(payload["source"], "plugin-queue")
        self.assertEqual(payload["surface_kind"], "plugin-agent")

    def test_json_omits_the_prompt_body(self):
        self.write_state([_job(pid=None)])
        payload = self.collect()[0].to_dict()
        request = payload["plugin_job"]["request"]
        self.assertNotIn("prompt", request)
        self.assertTrue(request["prompt_omitted"])
        self.assertNotIn("SECRET PROMPT BODY", json.dumps(payload, ensure_ascii=False))


class SurfaceBoundaryTest(_QueueFixture):
    """F-35e/F-50c — plugin-queue rows never merge with jobs.log attempts."""

    def test_a_jobs_log_row_with_the_same_cwd_stays_a_separate_row(self):
        self.write_state([_job(pid=None)])
        plugin_row = self.collect()[0]
        registry_row = DispatchJob(key="autopilot-code", slug="cairn-x",
                                   cwd="/home/u/cairn", source="jobs", status="running",
                                   harness="codex", liveness="working")
        rows = [registry_row, plugin_row]
        self.assertEqual(len({(j.source, j.slug) for j in rows}), 2)
        self.assertNotEqual(plugin_row.slug, registry_row.slug)


if __name__ == "__main__":
    unittest.main()
