"""Focused F-39 quota, OpenCode read-only source, and no-provider guards."""
import json
import os
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import refresh_title as rt  # noqa: E402
from fleet import titles  # noqa: E402
from fleet.model import Session  # noqa: E402


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in (
            "FLEET_TITLE_STATE_DIR", "FLEET_TITLE_COMMAND", "FLEET_TITLE_CONCURRENCY",
            "FLEET_TITLE_MAX_STARTS", "FLEET_TITLE_PRIORITY_MAX_STARTS",
            "FLEET_TITLE_DISABLE", "AGENT_ARTIFACT_ROOT", "AGENT_HOME",
            "AGENT_MODEL_GOVERNOR_ROOT", "AGENT_MODEL_WORKER_TOTAL",
            "AGENT_MODEL_WORKER_START_BUDGET", "AGENT_MODEL_WORKERS_DISABLED",
        )}
        os.environ["FLEET_TITLE_STATE_DIR"] = os.path.join(self.tmp.name, "state")
        os.environ["AGENT_MODEL_GOVERNOR_ROOT"] = os.path.join(self.tmp.name, "governor")
        # `run_worker` resolves the governor script through `AGENT_HOME`, which
        # in a real session points at the installed release, not this checkout
        # -- pin it here so the test exercises this worktree's governor instead
        # of whatever happens to be installed.
        os.environ["AGENT_HOME"] = os.path.join(os.path.dirname(__file__), "..", "..", "..")
        os.environ["AGENT_MODEL_WORKER_TOTAL"] = "5"
        os.environ["AGENT_MODEL_WORKER_START_BUDGET"] = "20"
        os.environ.pop("AGENT_ARTIFACT_ROOT", None)
        os.environ["FLEET_TITLE_COMMAND"] = sys.executable + " -c pass"
        os.environ["FLEET_TITLE_CONCURRENCY"] = "4"
        os.environ.pop("FLEET_TITLE_MAX_STARTS", None)
        os.environ.pop("FLEET_TITLE_DISABLE", None)
        os.environ.pop("AGENT_MODEL_WORKERS_DISABLED", None)
        self.transcript = os.path.join(self.tmp.name, "conversation.jsonl")
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write('{"message":"work"}\n')

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_limits_and_fake_clock_bound_backlog_then_reopen_window(self):
        self.assertEqual((rt.DEFAULT_CONCURRENCY, rt.MAX_CONCURRENCY), (3, 4))
        self.assertEqual((rt.DEFAULT_START_LIMIT, rt.MAX_START_LIMIT, rt.START_WINDOW_SEC), (4, 4, 60))
        self.assertEqual((rt.DEFAULT_PRIORITY_START_LIMIT, rt.MAX_PRIORITY_START_LIMIT), (2, 2))
        now = [1000.0]
        starts = []
        original = rt.subprocess.Popen
        def fake_popen(argv, **kwargs):
            starts.append(argv)
            for flag in ("--slotdir", "--lockdir"):
                path = argv[argv.index(flag) + 1]
                try:
                    os.rmdir(path)
                except OSError:
                    pass
            return object()
        rt.subprocess.Popen = fake_popen
        try:
            with mock.patch.object(rt.time, "time", side_effect=lambda: now[0]):
                results = [rt.maybe_spawn("claude", "sid-%d" % i, self.transcript) for i in range(200)]
                self.assertEqual(sum(results), 4)
                now[0] += 61
                self.assertTrue(rt.maybe_spawn("claude", "after-window", self.transcript))
        finally:
            rt.subprocess.Popen = original
        self.assertEqual(len(starts), 5)

    def test_debounce_and_child_debounce_are_the_approved_values(self):
        self.assertEqual(rt.DEBOUNCE_SEC, 600)
        self.assertEqual(rt.WORKING_DEBOUNCE_SEC, 120)
        self.assertEqual(rt.CHILD_DEBOUNCE_SEC, 600)
        self.assertEqual(rt.SUMMARY_RETRY_DELAYS, (30, 60, 120))
        # A dispatched child's periodic title refresh and an ordinary owner's
        # periodic summary refresh (dispatch_summary.py) now share the same
        # 10-minute cadence -- two literals that must never drift apart.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "utilities"))
        import dispatch_summary
        self.assertEqual(rt.CHILD_DEBOUNCE_SEC, dispatch_summary.DEFAULT_PERIODIC_DEBOUNCE)

    def test_priority_recovery_adds_only_two_starts_after_regular_budget_is_full(self):
        now = 2000.0
        starts = []

        def fake_popen(argv, **_kwargs):
            starts.append(argv)
            for flag in ("--slotdir", "--lockdir"):
                rt._remove_empty_dir(argv[argv.index(flag) + 1])
            return object()

        with mock.patch.object(rt.subprocess, "Popen", side_effect=fake_popen):
            regular = [rt.maybe_spawn("claude", "regular-%d" % i, self.transcript,
                                      now=now) for i in range(4)]
            priority = [rt.maybe_spawn("claude", "priority-%d" % i, self.transcript,
                                       now=now, priority=True) for i in range(3)]
        self.assertEqual(sum(regular), 4)
        self.assertEqual(sum(priority), 2)
        self.assertEqual(len(starts), 6)

    def test_initial_and_final_each_get_one_session_ticket_after_regular_budget(self):
        now = 2500.0
        starts = []

        def fake_popen(argv, **_kwargs):
            starts.append(argv)
            for flag in ("--slotdir", "--lockdir"):
                rt._remove_empty_dir(argv[argv.index(flag) + 1])
            return object()

        with mock.patch.object(rt.subprocess, "Popen", side_effect=fake_popen):
            for index in range(4):
                self.assertTrue(rt.maybe_spawn(
                    "claude", "ordinary-%d" % index, self.transcript, now=now))
            self.assertTrue(rt.maybe_spawn(
                "claude", "ticketed", self.transcript, now=now,
                quota_class="initial"))
            self.assertFalse(rt.maybe_spawn(
                "claude", "ticketed", self.transcript, now=now,
                quota_class="initial"))
            self.assertTrue(rt.maybe_spawn(
                "claude", "ticketed", self.transcript, now=now,
                quota_class="final"))
        self.assertEqual(len(starts), 6)

    def test_session_ticket_never_bypasses_hard_concurrency(self):
        now = 2700.0
        slot_root = os.path.join(titles.state_root(), ".refresh-workers")
        os.makedirs(slot_root, exist_ok=True)
        for index in range(rt.MAX_CONCURRENCY):
            path = os.path.join(slot_root, "slot-held-%d" % index)
            os.mkdir(path)
            os.utime(path, (now, now))
        with mock.patch.object(rt.subprocess, "Popen") as popen:
            self.assertFalse(rt.maybe_spawn(
                "claude", "capacity", self.transcript, now=now,
                quota_class="initial"))
        popen.assert_not_called()

    def test_failed_spawn_releases_session_ticket_for_exact_retry(self):
        now = 2800.0
        for index in range(4):
            root = os.path.join(titles.state_root(), ".refresh-budget")
            os.makedirs(root, exist_ok=True)
            path = os.path.join(root, "start-held-%d" % index)
            os.mkdir(path)
            os.utime(path, (now, now))
        with mock.patch.object(rt.subprocess, "Popen", side_effect=OSError("no spawn")):
            self.assertFalse(rt.maybe_spawn(
                "claude", "retry-ticket", self.transcript, now=now,
                quota_class="initial"))
        started = []

        def fake_popen(argv, **_kwargs):
            started.append(argv)
            for flag in ("--slotdir", "--lockdir"):
                rt._remove_empty_dir(argv[argv.index(flag) + 1])
            return object()

        with mock.patch.object(rt.subprocess, "Popen", side_effect=fake_popen):
            self.assertTrue(rt.maybe_spawn(
                "claude", "retry-ticket", self.transcript, now=now,
                quota_class="initial"))
        self.assertEqual(len(started), 1)

    def test_summary_failure_backoff_uses_30_60_120_seconds_and_keeps_same_delta_eligible(self):
        starts = []

        def fake_popen(argv, **_kwargs):
            starts.append(argv)
            for flag in ("--slotdir", "--lockdir"):
                rt._remove_empty_dir(argv[argv.index(flag) + 1])
            return object()

        with mock.patch.object(rt.subprocess, "Popen", side_effect=fake_popen):
            for index, delay in enumerate(rt.SUMMARY_RETRY_DELAYS, 1):
                base = 1000.0 * index
                sid = "retry-%d" % index
                titles.write(sid, "Retry", offset=0, now=base,
                             summary_failures=index)
                self.assertFalse(rt.maybe_spawn("claude", sid, self.transcript,
                                                now=base + delay, priority=True))
                self.assertTrue(rt.maybe_spawn("claude", sid, self.transcript,
                                               now=base + delay + 1, priority=True))
        self.assertEqual(len(starts), 3)

    def test_direct_worker_uses_same_start_budget_and_provider_is_not_reached_after_limit(self):
        os.environ["FLEET_TITLE_CONCURRENCY"] = "1"
        os.environ["FLEET_TITLE_MAX_STARTS"] = "1"
        calls = []
        class Done:
            returncode = 0
            stdout = "Fleet title work"
        with mock.patch.object(rt.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or Done()):
            self.assertEqual(rt.run_worker("prompt"), "Fleet title work")
            self.assertEqual(rt.run_worker("prompt"), "")
        self.assertEqual(len(calls), 1)

    def test_title_worker_records_session_label(self):
        class Done:
            returncode = 0
            stdout = "Fleet title work"
        with mock.patch.object(rt.subprocess, "run", side_effect=lambda *a, **k: Done()):
            output = rt.run_worker("prompt", capacity_held=True, label="dispatch-att-x")
        self.assertEqual(output, "Fleet title work")
        governor_root = os.environ["AGENT_MODEL_GOVERNOR_ROOT"]
        with open(os.path.join(governor_root, "state.json"), encoding="utf-8") as stream:
            state = json.loads(stream.read())
        records = [record for record in state["start_records"] if record["class"] == "title"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["label"], "dispatch-att-x")


class OpenCodeReadOnlyTest(unittest.TestCase):
    def test_dispatch_jsonl_is_normalized_without_database_access(self):
        raw = "\n".join([
            '{"type":"text","text":"implement owner"}',
            '{"type":"tool","text":"ignored"}',
            '{"type":"step_finish","parts":[{"type":"text","text":"summary ready"}]}',
        ])
        self.assertEqual(rt._delta_text(raw, harness="opencode"),
                         "implement owner\nsummary ready")

    def test_exact_session_delta_advances_integer_cursor_without_db_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "opencode.db")
            con = sqlite3.connect(db)
            con.execute("create table message (session_id text, data text)")
            con.executemany("insert into message values (?, ?)", [
                ("sid", '{"role":"user","text":"first"}'),
                ("other", '{"role":"user","text":"wrong"}'),
                ("sid", '{"role":"tool","text":"ignored"}'),
                ("sid", '{"role":"assistant","text":"second"}'),
            ])
            con.commit(); con.close()
            before = {}
            with open(db, "rb") as stream:
                before["opencode.db"] = stream.read()
            delta, cursor, table = rt.read_opencode_delta(db, "sid", 0)
            self.assertEqual((delta, table), ("first\nsecond", "message"))
            self.assertIsInstance(cursor, int)
            with open(db, "rb") as stream:
                after = {"opencode.db": stream.read()}
            self.assertEqual(before, after)

    def test_live_wal_snapshot_reads_exact_row_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "opencode.db")
            setup = sqlite3.connect(db)
            setup.execute("PRAGMA journal_mode=WAL")
            setup.execute("create table message (session_id text, data text)")
            setup.commit()
            setup.close()

            writer = sqlite3.connect(db)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("insert into message values (?, ?)",
                           ("sid-live", '{"role":"assistant","text":"uncheckpointed"}'))
            writer.commit()  # leave the writer open; this row remains in the WAL
            source_paths = [db, db + "-wal", db + "-shm", db + "-journal"]

            def signature(path):
                try:
                    st = os.stat(path)
                except OSError:
                    return None
                return (st.st_dev, st.st_ino, st.st_size, st.st_mode,
                        st.st_mtime_ns, st.st_ctime_ns)

            def source_state(path):
                if not os.path.exists(path):
                    return None, None
                with open(path, "rb") as stream:
                    return signature(path), stream.read()

            before = {
                path: source_state(path)
                for path in source_paths
            }
            delta, cursor, table = rt.read_opencode_delta(db, "sid-live", 0)
            after = {
                path: source_state(path)
                for path in source_paths
            }
            writer.close()
            self.assertEqual((delta, table), ("uncheckpointed", "message"))
            self.assertGreater(cursor, 0)
            self.assertEqual(before, after)


class CentralGovernorParityTest(unittest.TestCase):
    def test_title_class_admits_four_and_rejects_fifth_with_fleet_ceiling(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "utilities",
                            "model-worker-governor.py")
        sys.path.insert(0, os.path.dirname(path))
        spec = importlib.util.spec_from_file_location("fleet_model_worker_governor", path)
        governor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(governor)
        self.assertEqual(governor.CLASS_LIMITS["title"], rt.MAX_CONCURRENCY)
        with mock.patch.dict(os.environ, {"AGENT_MODEL_WORKERS_DISABLED": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                tokens = [governor.acquire(tmp, "title", total=10, budget=10) for _ in range(4)]
                with self.assertRaises(ValueError):
                    governor.acquire(tmp, "title", total=10, budget=10)
                for token in tokens:
                    governor.release(tmp, token)


if __name__ == "__main__":
    unittest.main()
