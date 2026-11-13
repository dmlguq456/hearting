"""Exact receiving-turn activity and one-pass live snapshots (2026-09-14)."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import fleet, model, projection
from fleet.collectors import dispatch


class OwnerActivityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "logs" / "owner.att-exact.codex.jsonl"
        self.path.parent.mkdir()
        self.job = model.DispatchJob(key="code", slug="owner", harness="codex",
                                    pid=123, proc_start="456", attempt_id="att-exact")
        self.job._registry_path = str(self.root / "jobs.log")
        self.job._log_file = str(self.path)

    def usage(self, thread="thread-exact", turn="turn-new", at=1000):
        return {"type": "dispatch.supervisor.token_usage", "thread_id": thread,
                "turn_id": turn, "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                "token_usage": {"last": {"total_tokens": 10}, "model_context_window": 100}}

    def classify(self, events, now=1001, observed="parked-supervised", **overrides):
        self.path.write_text("".join(json.dumps(event) + "\n" for event in events))
        dispatch._enrich_codex_attempt_session(self.job)
        evidence = {"attempt_id": "att-exact", "pid": 123, "proc_start": "456",
                    "harness": "codex", "status": "open",
                    "observed_liveness": {"state": observed, "reason": "supervisor-deliverable",
                                          "process_state": "live", "process_reason": "supervisor-lease-held"},
                    "runtime_session_id": getattr(self.job, "_runtime_session_id", None),
                    "runtime_activity": getattr(self.job, "_runtime_activity", None), **overrides}
        result = model.classify_attempt_evidence(evidence, now)
        return result["state"], result

    def test_old_supervisor_usage_proves_active_thread_without_thread_started(self):
        state, evidence = self.classify([self.usage()])
        self.assertEqual(state, "working")
        self.assertEqual(evidence["source"], "shared-observer+runtime")
        self.assertEqual(self.job._runtime_session_id, "thread-exact")

    def test_terminal_and_uncertain_observation_are_not_promoted_by_activity(self):
        for observed, expected in [("terminal", "done"), ("reconcile-needed", "stale")]:
            with self.subTest(observed=observed):
                self.assertEqual(self.classify([self.usage()], observed=observed)[0], expected)

    def test_stale_future_foreign_and_wrong_harness_cannot_promote(self):
        for kwargs in [dict(now=1061), dict(now=999), dict(attempt_id="att-foreign"),
                       dict(runtime_session_id="foreign"), dict(harness="claude"),
                       dict(harness="opencode")]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.classify([self.usage()], **kwargs)[0], "idle")

    def test_completed_parked_or_failed_turn_clears_activity(self):
        for kind in ["dispatch.supervisor.turn.completed", "dispatch.supervisor.parked",
                     "dispatch.supervisor.error", "turn.completed"]:
            with self.subTest(kind=kind):
                self.assertEqual(self.classify([self.usage(), {"type": kind, "turn_id": "turn-new"}])[0], "idle")

    def test_late_usage_of_completed_turn_does_not_reopen_it(self):
        self.assertEqual(self.classify([self.usage(),
            {"type": "dispatch.supervisor.turn.completed", "turn_id": "turn-new"},
            self.usage(at=1001)])[0], "idle")

    def test_start_of_next_turn_invalidates_previous_activity(self):
        self.assertEqual(self.classify([self.usage(),
            {"type": "dispatch.supervisor.turn.started", "turn_id": "turn-next"},
            self.usage(at=1001)])[0], "idle")

    def test_conflicting_thread_ids_in_head_and_tail_fail_closed(self):
        with mock.patch.object(dispatch, "_CLAUDE_SUPERVISOR_HEAD_BYTES", 512), \
             mock.patch.object(dispatch, "_CLAUDE_STREAM_TAIL_BYTES", 512):
            events = [{"type": "thread.started", "thread_id": "foreign"},
                      {"type": "padding", "text": "x" * 3000}, self.usage()]
            self.assertEqual(self.classify(events)[0], "idle")
            self.assertEqual(self.job.association_ambiguity, "multiple-attempt-thread-ids")

    def test_long_old_stream_binds_usage_head_and_fresh_tail(self):
        with mock.patch.object(dispatch, "_CLAUDE_SUPERVISOR_HEAD_BYTES", 512), \
             mock.patch.object(dispatch, "_CLAUDE_STREAM_TAIL_BYTES", 512):
            self.assertEqual(self.classify([self.usage(at=100),
                {"type": "padding", "text": "x" * 3000}, self.usage()])[0], "working")

    def test_log_outside_attempt_owned_root_is_not_activity_evidence(self):
        self.job._log_file = str(self.root / "foreign.codex.jsonl")
        Path(self.job._log_file).write_text(json.dumps(self.usage()))
        self.assertEqual(self.classify([self.usage()])[0], "idle")


class SnapshotProjectionTest(unittest.TestCase):
    def test_activity_read_during_slow_collection_is_not_a_future_event(self):
        clock = [1000.0]
        job = model.DispatchJob(key="code", slug="owner", source="proc", harness="codex",
                                cwd="/fixture", pid=123, proc_start="456", attempt_id="att-exact")
        def enrich(row):
            clock[0] = 1002.0
            row._runtime_activity = {"source": "codex-attempt-stream", "attempt_id": "att-exact",
                                     "thread_id": "thread", "turn_id": "turn", "observed_at": 1002.0}
        def classify(row, now, **kwargs):
            return model.classify_attempt_evidence({**vars(row), "runtime_session_id": "thread",
                "runtime_activity": row._runtime_activity,
                "observed_liveness": {"state": "parked-supervised"}}, now)["state"]
        with mock.patch.object(dispatch, "_scan_processes", return_value=[job]), \
             mock.patch.object(dispatch, "_candidate_jobs_paths", return_value=[]), \
             mock.patch.object(dispatch, "_build_codex_rollout_index", return_value={}), \
             mock.patch.object(dispatch, "_reconcile_drill_rows", return_value=[job]), \
             mock.patch.object(dispatch, "_enrich_codex_attempt_session", side_effect=enrich), \
             mock.patch.object(dispatch, "_dispatch_liveness", side_effect=classify), \
             mock.patch.object(dispatch.time, "time", side_effect=lambda: clock[0]):
            rows = dispatch.collect()
        self.assertEqual(rows[0].liveness, "working")

    def test_exact_attempts_do_not_build_an_unused_cwd_rollout_index(self):
        exact = model.DispatchJob(key="code", slug="owner", harness="codex", cwd="/nas/repo",
                                  pid=123, proc_start="456", attempt_id="att-exact")
        with mock.patch.object(dispatch.os, "walk", side_effect=AssertionError("history scan")):
            self.assertEqual(dispatch._build_codex_rollout_index([exact]), {})

    def test_only_routes_referenced_by_live_rows_or_parent_edges_are_opened(self):
        entities = [model.Session(harness="codex", pid=99, session_id="parent", slug="owner")]
        nodes = {"rt-old": {"test": {"route_file": "/nas/old.json", "parent": "retired"}},
                 "rt-child": {"test": {"route_file": "/nas/child.json", "parent": "parent"}}}
        from fleet import route
        with mock.patch.object(route, "load", return_value={"route_id": "rt-child"}) as load:
            records = projection._load_evidence_records(nodes, {}, entities)
        self.assertEqual(set(records), {"rt-child"})
        load.assert_called_once_with("/nas/child.json", expect_hash=None, expect_id="rt-child")

    def test_json_consumer_preserves_collector_projection_without_second_pass(self):
        session = model.Session(harness="codex", pid=99, cwd="/tmp/fleet-fixture")
        session.work_projection = model.WorkProjection(source="route-exact", route_id="rt-exact")
        def collected(harness_filter=None):
            return [session], []
        collected.last_usage = {}
        with mock.patch.object(fleet, "collect_all", side_effect=collected) as collect, \
             mock.patch.object(fleet.installinfo, "collect", return_value={}), \
             mock.patch.object(fleet.compute_hosts, "collect", return_value={}), \
             mock.patch.object(fleet, "_arm_stall_dump"), \
             mock.patch.object(projection, "attach_projections", side_effect=AssertionError("duplicate projection")), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            # Function attributes are part of the collector snapshot contract.
            collect.last_usage = {}
            collect.last_resource_jobs = []
            collect.last_resource_malformed = 0
            collect.last_usage_snapshots = {}
            self.assertEqual(fleet.main(["--json", "--no-usage-api"]), 0)
        collect.assert_called_once()
        public = json.loads(output.getvalue())
        self.assertEqual(public["sessions"][0]["work_projection"]["route_id"], "rt-exact")


if __name__ == "__main__":
    unittest.main()
