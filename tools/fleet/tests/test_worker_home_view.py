"""Fleet marks only displayed attempts' homes, before any native read (issue #564).

A fixture uses a temporary dispatch state root; no live registry or home is read.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "utilities"))

import worker_home_retention as retention  # noqa: E402
from fleet import details, projection, titles  # noqa: E402
from fleet.collectors import dispatch  # noqa: E402
from fleet.model import DispatchJob  # noqa: E402
from fleet.refresh import LiveSnapshot  # noqa: E402
from worker_runtime_home import _builder  # noqa: E402

RETENTION_NS = retention.IDLE_SECONDS * 10**9
CONTRACT = ("attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,worker_type=stage")


class WorkerHomeViewTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.rows = []
        self.now = time.time_ns()

    def home(self, attempt, status, extra=""):
        key = _builder.worker_home_key(str(ROOT), "claude", "stage", attempt)
        path = self.root / "homes" / "workers" / key
        path.mkdir(parents=True)
        (path / "worker-home.json").write_text(json.dumps({
            "harness": "claude", "worker_type": "stage", "source": str(ROOT), "profile": None,
            "schema": 2, "identity": attempt, "key": key, "destination": False}))
        stamp = "2026-10-15T00:00:00Z" if status == "open" else "2026-08-01T00:00:00Z"
        process = (",pid=99999992,pid_start=1,pgid=99999992,pid_scope=namespace-local,"
                   "launch_lifecycle=foreground-scoped" if status == "open"
                   else ",launch_outcome=never-launched")
        self.rows.append(f"{stamp}\t{status}\t/repo\t/wt\t{attempt}\t{CONTRACT},attempt_id={attempt},"
                         f"harness=claude,runtime_home={path}{process}{extra}")
        self.jobs.write_text("\n".join(self.rows) + "\n")
        return path, key

    def mark(self, key):
        return self.root / "homes/worker-retention/views" / key

    def job(self, path, attempt="att-shown"):
        job = DispatchJob(key="code", slug="worker", harness="claude", attempt_id=attempt)
        job._registry_metadata = {"runtime_home": str(path)}
        job._registry_path = str(self.jobs)
        return job

    def test_a_displayed_row_is_marked_once_and_a_missing_home_writes_nothing(self):
        path, key = self.home("att-shown", "open")
        job = self.job(path)
        dispatch._note_home_view(job)
        self.assertTrue(self.mark(key).is_file())
        first = self.mark(key).stat().st_mtime_ns
        dispatch._note_home_view(job)
        self.assertEqual(self.mark(key).stat().st_mtime_ns, first)
        gone = self.job(self.root / "homes/workers" / ("e" * 32))
        dispatch._note_home_view(gone)
        self.assertFalse(self.mark("e" * 32).exists())
        self.assertFalse((self.root / "homes/workers" / ("e" * 32)).exists())

    def test_shared_outside_and_unrecorded_homes_are_ignored(self):
        shared = self.root / "homes/codex" / ("a" * 32)
        shared.mkdir(parents=True)
        for value in (str(shared), "/elsewhere/" + "a" * 32, None):
            job = self.job(value or "")
            if value is None:
                job._registry_metadata = {}
            dispatch._note_home_view(job)
        self.assertFalse((self.root / "homes/worker-retention/views").exists())

    def test_excluded_homes_admit_native_consumers_without_retention_writes(self):
        shared = self.root / "homes/codex/shared"
        foreign = self.root / "foreign-root/homes/workers" / ("b" * 32)
        for home in (shared, foreign):
            home.mkdir(parents=True)
            job = self.job(home)
            job.harness = "codex"
            job.cwd = str(self.root / "worktree")
            with self.subTest(home=home), mock.patch.object(retention, "_Layout", side_effect=OSError("writes unavailable")), \
                    mock.patch.object(dispatch.os, "walk", return_value=iter(())) as walk:
                self.assertTrue(dispatch._native_home_admitted(job))
                self.assertEqual(dispatch._codex_job_liveness(job.cwd, time.time(), job=job), "dead")
                walk.assert_called()
                with self.fleet_detail_pass(job):
                    pass
            self.assertFalse((self.root / "homes/worker-retention").exists())

    def test_nonhex_directory_directly_under_workers_is_excluded_and_admitted(self):
        workers = self.root / "homes/workers"
        nonhex = workers / "not-a-candidate"
        nonhex.mkdir(parents=True)
        candidate = workers / ("c" * 32)
        candidate.mkdir()
        job = self.job(nonhex)
        job.harness = "codex"
        job.cwd = str(self.root / "worktree")
        with mock.patch.object(retention, "_Layout", side_effect=OSError("writes unavailable")), \
                mock.patch.object(dispatch.os, "walk", return_value=iter(())) as walk:
            self.assertTrue(dispatch._native_home_admitted(job))
            self.assertEqual(dispatch._codex_job_liveness(job.cwd, time.time(), job=job), "dead")
            walk.assert_called()
            with self.fleet_detail_pass(job):
                pass
            # A real candidate whose marker cannot be secured is still denied.
            self.assertFalse(dispatch._native_home_admitted(self.job(candidate)))
            # Only an existing non-symlink directory is excluded-and-admitted.
            self.assertFalse(retention.note_view(self.jobs, str(workers / "missing-name")))
            link = workers / "linked-name"
            link.symlink_to(nonhex, target_is_directory=True)
            self.assertFalse(retention.note_view(self.jobs, str(link)))
            (workers / "plain-file").write_text("")
            self.assertFalse(retention.note_view(self.jobs, str(workers / "plain-file")))
        self.assertFalse((self.root / "homes/worker-retention").exists())

    def test_excluded_codex_rollout_and_context_policy_use_real_admission(self):
        home = self.root / "homes/codex/shared"
        home.mkdir(parents=True)
        job = self.job(home)
        job.harness = "codex"
        job.cwd = str(self.root / "worktree")
        log = self.root / "logs/worker.att-shown.codex.jsonl"
        log.parent.mkdir()
        log.write_text("{}\n")
        with mock.patch.object(dispatch, "_owned_attempt_log_path", return_value=str(log)), \
                mock.patch.object(dispatch, "_parse_codex_attempt_tail", return_value={
                    "thread_id": "thread-one", "thread_ambiguity": False}), \
                mock.patch.object(dispatch, "_codex_attempt_rollout", return_value=None) as rollout, \
                mock.patch.object(retention, "_Layout", side_effect=OSError("writes unavailable")):
            dispatch._enrich_codex_attempt_session(job)
        rollout.assert_called_once_with(job, "thread-one")
        self.assertIsNotNone(job.context_policy)
        self.assertFalse((self.root / "homes/worker-retention").exists())

    def test_candidate_aliases_and_invalid_identities_fail_closed(self):
        path, _ = self.home("att-shown", "open")
        alias = self.root / "workers-alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        self.assertTrue(retention.note_view(self.jobs, str(alias / path.name)))
        self.assertFalse(retention.note_view(self.jobs, str(alias / "invalid")))
        self.assertFalse(retention.note_view(self.jobs, str(path.parent / ("f" * 32))))
        link = path.parent / ("e" * 32)
        link.symlink_to(path, target_is_directory=True)
        self.assertFalse(retention.note_view(self.jobs, str(link)))
        self.assertFalse(retention.note_view(self.jobs, "relative/home"))
        with mock.patch.object(Path, "resolve", side_effect=OSError("unresolvable")):
            self.assertFalse(retention.note_view(self.jobs, str(path)))
        with mock.patch.object(retention, "_Layout", side_effect=OSError("writes unavailable")):
            self.assertFalse(dispatch._native_home_admitted(self.job(path)))

    def collect(self):
        with mock.patch.object(dispatch, "_scan_processes", return_value=[]), \
                mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()), \
                mock.patch.object(dispatch, "_attempt_heartbeat", side_effect=lambda job: {
                    "updated_at": 9999999999, "attempt_id": job.attempt_id, "route_id": job.route_id,
                    "route_node": job.route_node, "phase": "tool", "sequence": 1}), \
                mock.patch.object(dispatch, "_job_transcript_signal", return_value="working"), \
                mock.patch.object(dispatch, "_attempt_terminal_observation", return_value=None):
            return dispatch.collect(jobs_path=str(self.jobs))

    def test_collect_marks_displayed_rows_and_historical_rows_never_pin_a_home(self):
        shown, shown_key = self.home("att-shown", "open")
        old, old_key = self.home("att-historical", "done")
        displayed = {job.attempt_id for job in self.collect()}
        self.assertIn("att-shown", displayed)
        self.assertNotIn("att-historical", displayed)
        self.assertTrue(self.mark(shown_key).is_file())
        self.assertFalse(self.mark(old_key).exists())
        # The unviewed historical home is therefore still eligible to age out.
        retention.sweep(self.jobs, now_ns=self.now, force=True)
        result = retention.sweep(self.jobs, now_ns=self.now + int(8 / 7 * RETENTION_NS), force=True)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(shown.is_dir())

    def test_the_first_fast_display_admits_native_reads(self):
        path, key = self.home("att-shown", "open")
        with mock.patch.object(dispatch, "_scan_processes", return_value=[]), \
                mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()), \
                mock.patch.object(dispatch, "_job_transcript_signal", return_value="working"):
            dispatch.collect(jobs_path=str(self.jobs), fast_first=True)
        self.assertTrue(self.mark(key).is_file())

    def test_failed_mark_and_missing_helper_use_retained_display_data(self):
        path, _ = self.home("att-shown", "open")
        def failed(*args):
            raise OSError("marker-write-failed")
        for marker in (None, lambda *args: False, failed):
            with self.subTest(marker=marker), \
                    mock.patch.object(dispatch, "_note_worker_home_view", marker), \
                    mock.patch.object(dispatch, "_supervised_claude_transcript") as native:
                self.collect()
                native.assert_not_called()
                with mock.patch.object(dispatch, "_scan_processes", return_value=[]), \
                        mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()):
                    dispatch.collect(jobs_path=str(self.jobs), fast_first=True)
                native.assert_not_called()
                with self.fleet_detail_pass(self.job(path)):
                    pass
                native.assert_not_called()

    def test_denied_codex_liveness_never_walks_private_sessions(self):
        log = self.root / "logs" / "worker.att-shown.codex.jsonl"
        log.parent.mkdir()
        log.write_text("{}\n")
        path, _ = self.home("att-shown", "open", extra=f",log_file={log}")
        job = self.job(path)
        job.harness = "codex"
        job.cwd = str(self.root / "worktree")
        sessions = str(path / "sessions")
        with mock.patch.object(dispatch, "_native_home_admitted", return_value=False), \
                mock.patch.object(dispatch.os, "walk") as walk:
            self.assertEqual(dispatch._codex_job_liveness(job.cwd, time.time(),
                             codex_index=None, job=job), "unknown")
            walk.assert_not_called()
        job._registry_metadata = {}
        with mock.patch.object(dispatch.os, "walk", return_value=iter(())) as walk:
            self.assertEqual(dispatch._codex_job_liveness(job.cwd, time.time(),
                             codex_index=None, job=job), "dead")
            self.assertTrue(walk.called)
        job._registry_metadata = {"runtime_home": str(path)}

        self.rows[-1] = self.rows[-1].replace("harness=claude", "harness=codex")
        self.rows.append(f"2026-10-15T00:00:00Z\topen\t/repo\t/wt\tlegacy-codex\t"
                         f"harness=codex,runtime_home={path}")
        self.jobs.write_text("\n".join(self.rows) + "\n")
        with mock.patch.object(dispatch, "_build_codex_rollout_index", side_effect=OSError("index")), \
                mock.patch.object(dispatch, "_note_worker_home_view", return_value=False), \
                mock.patch.object(dispatch, "_codex_job_liveness", wraps=dispatch._codex_job_liveness) as liveness, \
                mock.patch.object(dispatch.os, "walk") as walk, \
                mock.patch.object(dispatch, "_scan_processes", return_value=[]), \
                mock.patch.object(dispatch, "_live_attempt_ids", return_value=set()), \
                mock.patch.object(dispatch, "_attempt_terminal_observation", return_value=None), \
                mock.patch.object(titles, "fresh_title", return_value="Worker title"), \
                mock.patch.object(titles, "fresh_summary_with_ts", return_value=("Worker summary", time.time())):
            rows = dispatch.collect(jobs_path=str(self.jobs))
            self.assertTrue(any(call.kwargs.get("codex_index") is None for call in liveness.call_args_list))
            walk.assert_not_called()
        shown = next(row for row in rows if row.attempt_id == "att-shown")
        self.assertEqual(shown.title, "Worker title")
        self.assertEqual(shown.summary, "Worker summary")
        self.assertEqual(shown._transcript_path, str(log))

    def test_failed_mark_keeps_attempt_summary_in_collection_and_detail(self):
        log = self.root / "logs" / "worker.att-shown.claude.jsonl"
        log.parent.mkdir()
        log.write_text("{}\n")
        path, _ = self.home("att-shown", "open", extra=f",log_file={log}")
        job = self.job(path)
        job._log_file = str(log)
        with mock.patch.object(dispatch, "_note_worker_home_view", return_value=False), \
                mock.patch.object(titles, "fresh_title", return_value="Worker title"), \
                mock.patch.object(titles, "fresh_summary_with_ts", return_value=("Worker summary", time.time())):
            rows = self.collect()
            shown = next(row for row in rows if row.attempt_id == "att-shown")
            self.assertEqual(shown.summary, "Worker summary")
            self.assertEqual(shown.title, "Worker title")
            self.assertEqual(shown._transcript_path, str(log))
            with self.fleet_detail_pass(job) as refreshed:
                detail = refreshed.snapshot.jobs[0]
                self.assertEqual(detail.summary, "Worker summary")
                self.assertEqual(detail._transcript_path, str(log))

    def test_direct_stream_enrichers_keep_attempt_identity_without_native_reads(self):
        path, _ = self.home("att-shown", "open")
        logs = self.root / "logs"
        logs.mkdir()
        cases = (
            ("claude", "_enrich_claude_stream_session", "_parse_claude_stream_tail",
             {"session_id": "session-x"}, "_supervised_claude_transcript"),
            ("codex", "_enrich_codex_attempt_session", "_parse_codex_attempt_tail",
             {"thread_id": "thread-x", "thread_ambiguity": False}, "_codex_attempt_rollout"),
            ("opencode", "_enrich_opencode_attempt_session", "_parse_opencode_attempt_tail",
             {"session_id": "session-x"}, "_opencode_db"),
        )
        for harness, enrich, parser, parsed, native in cases:
            with self.subTest(harness=harness):
                log = logs / f"worker.att-shown.{harness}.jsonl"
                log.write_text("{}\n")
                job = self.job(path)
                job.harness = harness
                job._log_file = str(log)
                with mock.patch.object(dispatch, "_note_worker_home_view", return_value=False), \
                     mock.patch.object(dispatch, parser, return_value=parsed), \
                     mock.patch.object(dispatch, native) as native_read, \
                     mock.patch.object(titles, "fresh_summary_with_ts", return_value=("Stream summary", time.time())):
                    getattr(dispatch, enrich)(job)
                    dispatch._enrich_attempt_summary(job)
                self.assertEqual(job._runtime_session_id, parsed.get("session_id", parsed.get("thread_id")))
                self.assertEqual(job.summary, "Stream summary")
                self.assertEqual(job._transcript_path, str(log))
                native_read.assert_not_called()

    @contextlib.contextmanager
    def fleet_detail_pass(self, job):
        """One real details pass over a displayed snapshot; bookkeeping reads stubbed."""
        with mock.patch.object(dispatch, "_pending_delivery_counts", return_value=None), \
                mock.patch.object(dispatch, "_scan_degradations", return_value={}), \
                mock.patch.object(projection, "_spec_marker_index", return_value={}), \
                mock.patch.object(projection, "_capability_grounding_index", return_value={}), \
                mock.patch("fleet.collectors.peer_messages.collect", return_value=None):
            yield details.enrich(LiveSnapshot(jobs=[job]))

    def test_a_detail_pass_that_lands_just_before_the_detach_keeps_the_home(self):
        path, key = self.home("att-shown", "done")
        retention.sweep(self.jobs, now_ns=self.now, force=True)
        job = self.job(path)
        original = retention._registry_lock
        landed = []

        @contextlib.contextmanager
        def lock(jobs):
            if not landed:
                landed.append(True)
                # The detail pass runs on the same (simulated) clock as the sweep.
                with mock.patch.object(retention.time, "time", return_value=later / 1e9), \
                        self.fleet_detail_pass(job):
                    pass
            with original(jobs):
                yield

        later = self.now + int(8 / 7 * RETENTION_NS)
        with mock.patch.object(retention, "_registry_lock", lock):
            result = retention.sweep(self.jobs, now_ns=later, force=True)
        self.assertEqual(landed, [True])
        self.assertTrue(self.mark(key).is_file())
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["kept"].get("recent-view"), 1)
        self.assertTrue(path.is_dir())

    def test_recent_cached_view_marker_survives_detach_boundary(self):
        path, key = self.home("att-shown", "done")
        retention.sweep(self.jobs, now_ns=self.now, force=True)
        job = self.job(path)
        later = self.now + int(8 / 7 * RETENTION_NS)
        original_detach = retention._detach
        marker_before = []
        admissions = []

        def record_admission(jobs, home):
            admitted = retention.note_view(jobs, home)
            admissions.append(admitted)
            return admitted

        def land_cached_marker(ctx, home_key, ident, attempt, raw, first_ns, deadline=None,
                               _rechecks=0, binding=None):
            marker = self.mark(key)
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker.write_text("")
            mark_time = later / 1e9 - (retention.VIEW_REFRESH_SECONDS - 1)
            os.utime(marker, (mark_time, mark_time))
            marker_before.append(marker.stat().st_mtime_ns)
            with mock.patch.object(retention.time, "time", return_value=later / 1e9), \
                    mock.patch.object(dispatch, "_note_worker_home_view", side_effect=record_admission):
                with self.fleet_detail_pass(job):
                    pass
                native_read_allowed = dispatch._native_home_admitted(job)
            marker_after = marker.stat().st_mtime_ns
            self.assertEqual(marker_after, marker_before[-1])
            self.assertTrue(native_read_allowed)
            return original_detach(ctx, home_key, ident, attempt, raw, first_ns,
                                   deadline=deadline, _rechecks=_rechecks, binding=binding)

        with mock.patch.object(retention, "_detach", side_effect=land_cached_marker):
            result = retention.sweep(self.jobs, now_ns=later, force=True)
        self.assertEqual(admissions, [True, True])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["kept"].get("recent-view"), 1)
        self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
