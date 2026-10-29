"""Focused F-39 quota, OpenCode read-only source, and no-provider guards."""
import json
import os
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import refresh_title as rt  # noqa: E402
from fleet import titles  # noqa: E402
from fleet.model import Session  # noqa: E402


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        preferences = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": self.tmp.name + "/claude",
            "XDG_CONFIG_HOME": self.tmp.name + "/config"})
        preferences.start()
        self.addCleanup(preferences.stop)
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

    def _run_as_non_group_actor(self):
        # Budget API cases need no process-group drain. As in the governor's
        # own API tests, use a real child so unrelated procfs churn cannot
        # retain already-returned leases and mask the rolling-budget result.
        if os.getpid() != os.getpgrp():
            return False
        result = rt.subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             type(self).__name__ + "." + self._testMethodName],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return True

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
        self.assertEqual(rt.WORKING_DEBOUNCE_SEC, 300)
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

    def test_detached_worker_receives_existing_priority_and_phase(self):
        for harness, phase in (("claude", "initial"), ("codex", "initial"), ("opencode", "final")):
            with self.subTest(harness=harness):
                with mock.patch.object(rt.subprocess, "Popen", return_value=object()) as popen:
                    self.assertTrue(rt.maybe_spawn(
                        harness, "forward-" + harness, self.transcript,
                        priority=True, quota_class=phase))
                argv = popen.call_args.args[0]
                self.assertIn("--priority", argv)
                self.assertEqual(argv[argv.index("--quota-class") + 1], phase)
                for flag in ("--slotdir", "--lockdir"):
                    rt._remove_empty_dir(argv[argv.index(flag) + 1])

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

    def test_real_governor_reserves_first_now_and_existing_retry_recovers(self):
        if self._run_as_non_group_actor():
            return
        now = [1700000000.0]
        answer = "TITLE: 제목 한도 확인\nNOW: 첫 요약 확인 중"
        # Only the remote provider is stubbed. Governor admission, sidecar
        # writing and retry are real; the caller already holds Fleet capacity.
        with mock.patch.dict(os.environ, {"AGENT_MODEL_WORKER_START_BUDGET_TITLE": "6",
                                          "FLEET_NOW_LANG": "Korean"}), \
                mock.patch.object(rt.time, "time", side_effect=lambda: now[0]), \
                mock.patch.object(rt, "run_provider_cascade", return_value=(answer, 0)) as provider:
            for _ in range(2):
                self.assertTrue(rt.run_worker("periodic", capacity_held=True))
            box = {}
            self.assertFalse(rt.run_worker("periodic", capacity_held=True, provider_box=box))
            self.assertIn("_StartBudgetReached", box["error"])
            self.assertEqual(provider.call_count, 2)
            path = os.path.join(self.tmp.name, "codex.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps({"type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "제목 첫 요약을 확인해 주세요"}]}}) + "\n")
            args = ["--harness", "codex", "--sid", "dispatch-first-now",
                    "--transcript", path, "--slotdir", os.path.join(self.tmp.name, "slot")]
            rt.main(args)
            self.assertEqual(titles.read("dispatch-first-now", "codex")["summary"], "첫 요약 확인 중")
            for _ in range(3):
                self.assertTrue(rt.run_worker("initial", capacity_held=True, priority=True))
            args[args.index("--sid") + 1] = "dispatch-window-retry"
            rt.main(args)
            blocked = titles.read("dispatch-window-retry", "codex")
            self.assertIn("_StartBudgetReached", blocked["summary_error"])
            self.assertEqual(blocked["offset"], 0)
            self.assertEqual(provider.call_count, 6)
            now[0] += 601
            rt.main(args)
            recovered = titles.read("dispatch-window-retry", "codex")
            self.assertEqual(recovered["summary"], "첫 요약 확인 중")
            self.assertNotIn("summary_error", recovered)
            self.assertEqual(provider.call_count, 7)

    def test_periodic_retry_and_language_repair_cannot_spend_first_now_reserve(self):
        if self._run_as_non_group_actor():
            return
        now = [1700000000.0]
        answer = "TITLE: 제목 한도 확인\nNOW: 첫 요약 확인 중"
        path = Path(self.tmp.name) / "claude.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {
            "role": "user", "content": "제목 한도 보호를 확인해 주세요"}}) + "\n")
        with mock.patch.dict(os.environ, {"AGENT_MODEL_WORKER_START_BUDGET_TITLE": "6",
                                          "FLEET_NOW_LANG": "Korean"}), \
                mock.patch.object(rt.time, "time", side_effect=lambda: now[0]), \
                mock.patch.object(rt, "run_provider_cascade", return_value=(answer, 0)) as provider:
            for _ in range(2):
                self.assertTrue(rt.run_worker("periodic", capacity_held=True))
            args = ["--harness", "claude", "--sid", "existing-now", "--transcript", str(path)]
            titles.write("existing-now", "기존 제목", harness="claude", summary="기존 작업 중",
                         offset=0, now=now[0] - 1000)
            rt.main(args)  # Real Fleet slot/start admission and governor refusal.
            failed = titles.read("existing-now", "claude")
            self.assertIn("_StartBudgetReached", failed["summary_error"])
            shell = (Path(__file__).resolve().parents[3] / "adapters/claude/statusline.sh").read_text()
            block = shell[shell.index("          priority_arg="):shell.index("          FLEET_TITLE_REFRESH=1")]
            result = rt.subprocess.run(
                ["sh", "-c", block + '\nprintf "%s\\n%s\\n" "$priority_arg" "$quota_arg"'],
                capture_output=True, text=True, check=True,
                env=dict(os.environ, scts=str(int(failed["ts"])), sf=str(failed["summary_failures"])))
            retry_flags = result.stdout.split()
            self.assertEqual(retry_flags, ["--priority"])
            now[0] += 31
            rt.main(args + retry_flags)
            retried = titles.read("existing-now", "claude")
            self.assertIn("_StartBudgetReached", retried["summary_error"])
            self.assertEqual((retried["summary"], retried["offset"]), ("기존 작업 중", 0))
            titles.write("language-now", "Existing Title", harness="claude", summary="기존 작업 중",
                         offset=0, now=now[0] - 1000)
            args[args.index("--sid") + 1] = "language-now"
            rt.main(args)
            repaired = titles.read("language-now", "claude")
            self.assertIn("_StartBudgetReached", repaired["summary_error"])
            self.assertEqual((repaired["summary"], repaired["offset"]), ("기존 작업 중", 0))
            self.assertEqual(titles.last_title("language-now", "claude"), "Existing Title")
            # A failed language repair preserves the last successful title.
            # Its next retry remains ordinary despite the language mismatch.
            now[0] += 61
            rt.main(args + retry_flags)
            repaired_again = titles.read("language-now", "claude")
            self.assertIn("_StartBudgetReached", repaired_again["summary_error"])
            self.assertEqual((repaired_again["summary"], repaired_again["offset"]),
                             ("기존 작업 중", 0))
            self.assertEqual(provider.call_count, 2)
            args[args.index("--sid") + 1] = "dispatch-new-worker"
            rt.main(args + ["--priority", "--quota-class", "initial"])
            self.assertEqual(titles.read("dispatch-new-worker", "claude")["summary"], "첫 요약 확인 중")
            self.assertEqual(provider.call_count, 3)


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
