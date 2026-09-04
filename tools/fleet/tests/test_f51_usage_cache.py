import os
import sys
import tempfile
import time
import unittest
import json
import threading
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import usage_cache


class F51UsageCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("FLEET_USAGE_STATE_DIR")
        os.environ["FLEET_USAGE_STATE_DIR"] = self.tmp.name
        self.old_fetchers = dict(usage_cache.FETCHERS)
        # A prior test/cycle must not retain a worker keyed to a real state directory.
        for thread in list(usage_cache._THREADS.values()):
            thread.join(timeout=5)
        usage_cache._THREADS.clear()
        self.assertEqual(usage_cache.state_dir(), self.tmp.name)

    def tearDown(self):
        for thread in list(usage_cache._THREADS.values()):
            thread.join(timeout=5)
        usage_cache._THREADS.clear()
        usage_cache.FETCHERS.clear(); usage_cache.FETCHERS.update(self.old_fetchers)
        if self.old is None:
            os.environ.pop("FLEET_USAGE_STATE_DIR", None)
        else:
            os.environ["FLEET_USAGE_STATE_DIR"] = self.old
        self.tmp.cleanup()

    def test_cache_only_never_calls_fetcher(self):
        calls = []
        usage_cache.FETCHERS["claude"] = lambda: calls.append(1)
        self.assertEqual(usage_cache.account_usage("claude")["freshness"], "unknown")
        self.assertEqual(calls, [])

    def test_refresh_is_single_flight_and_atomic(self):
        calls = []
        usage_cache.FETCHERS["claude"] = lambda: (calls.append(1) or {"rl_5h": 12})
        self.assertTrue(usage_cache.request_refresh("claude"))
        thread = usage_cache._THREADS.get("claude")
        self.assertIsNotNone(thread, "request_refresh did not register its worker thread")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "worker thread did not finish within timeout")
        self.assertEqual(len(calls), 1)
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 12)

    def test_fresh_cache_suppresses_refresh_until_harness_window_expires(self):
        calls = []
        usage_cache.FETCHERS["claude"] = lambda: calls.append(1)
        self._write_cache("claude", 1000)
        self.assertFalse(usage_cache.request_refresh("claude", now=1179))
        self.assertEqual(calls, [])
        self.assertNotIn("claude", usage_cache._THREADS)

    def _write_cache(self, harness, fetched_at, payload=None, **extra):
        item = {"schema_version": usage_cache.SCHEMA_VERSION, "harness": harness,
                "fetched_at": fetched_at, "attempted_at": fetched_at,
                "payload": payload or {"rl_5h": 42}}
        item.update(extra)
        with open(os.path.join(self.tmp.name, harness + ".json"), "w") as fh:
            json.dump(item, fh)

    def test_fresh_stale_unknown_windows_and_invalid_files_are_preserved(self):
        for harness, fresh in (("claude", 180), ("codex", 60)):
            for age, label in ((fresh - 1, "fresh"), (fresh + 1, "stale"), (901, "unknown")):
                self._write_cache(harness, 1000)
                self.assertEqual(usage_cache.read(harness, now=1000 + age)["freshness"], label)
        for item in ({"schema_version": 999}, {"schema_version": 1, "harness": "claude",
                                              "fetched_at": 1, "attempted_at": 1}, None):
            path = os.path.join(self.tmp.name, "claude.json")
            if item is None:
                open(path, "w").close()
            else:
                with open(path, "w") as fh:
                    json.dump(item, fh)
            self.assertEqual(usage_cache.read("claude", now=1000)["freshness"], "unknown")
            self.assertTrue(os.path.exists(path))
        self._write_cache("claude", 1200)
        self.assertEqual(usage_cache.read("claude", now=1000)["freshness"], "unknown")

    def test_atomic_reader_never_observes_partial_json(self):
        self._write_cache("claude", 1000)
        stop = threading.Event(); seen = []
        def reader():
            while not stop.is_set():
                seen.append(usage_cache.read("claude", now=1000)["freshness"])
        thread = threading.Thread(target=reader); thread.start()
        for value in (1, 2, 3, 4):
            usage_cache._write("claude", {"value": value}, 1000, 1000)
        stop.set(); thread.join()
        self.assertTrue(seen)
        self.assertNotIn("unknown", seen)

    def test_eight_concurrent_refresh_requests_have_one_fetch(self):
        calls = []
        usage_cache.FETCHERS["claude"] = lambda: (calls.append(1) or {"rl_5h": 9})
        threads = [threading.Thread(target=usage_cache.request_refresh, args=("claude", 1000))
                   for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        worker = usage_cache._THREADS.get("claude")
        if worker is not None:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "worker thread did not finish within timeout")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([x for x in os.listdir(self.tmp.name) if x.startswith(".claude.lease")]), 0)

    def test_stale_lease_older_than_60s_is_reclaimed_by_the_next_request(self):
        """A10a: a lease file left behind by a thread that was killed (SIGKILL, crash) never
        clears itself — the NEXT `request_refresh` call must reclaim it once `LEASE_MAX`
        (60s) has elapsed, rather than backing off forever."""
        calls = []
        usage_cache.FETCHERS["claude"] = lambda: (calls.append(1) or {"rl_5h": 5})
        lease = os.path.join(self.tmp.name, ".claude.lease")
        fd = os.open(lease, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        old_time = time.time() - usage_cache.LEASE_MAX - 1
        os.utime(lease, (old_time, old_time))
        self.assertTrue(usage_cache.request_refresh("claude"))
        thread = usage_cache._THREADS.get("claude")
        self.assertIsNotNone(thread, "request_refresh did not register its worker thread")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "worker thread did not finish within timeout")
        self.assertEqual(len(calls), 1)
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 5)

    def test_usage_lease_never_touches_the_f39_title_lease_pool(self):
        """A10b: the usage-cache lease is its own file (`.claude.lease` under
        FLEET_USAGE_STATE_DIR) — a usage refresh must leave the F-39 title worker's lease
        pool (FLEET_TITLE_STATE_DIR) completely untouched (entry count unchanged)."""
        from fleet import titles
        with tempfile.TemporaryDirectory() as title_root:
            old_title_root = os.environ.get("FLEET_TITLE_STATE_DIR")
            os.environ["FLEET_TITLE_STATE_DIR"] = title_root
            try:
                self.assertEqual(titles.state_root(), title_root)
                before = len(os.listdir(title_root))
                usage_cache.FETCHERS["claude"] = lambda: {"rl_5h": 1}
                self.assertTrue(usage_cache.request_refresh("claude"))
                thread = usage_cache._THREADS.get("claude")
                if thread is not None:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive(), "worker thread did not finish within timeout")
                after = len(os.listdir(title_root))
                self.assertEqual(before, after)
            finally:
                if old_title_root is None:
                    os.environ.pop("FLEET_TITLE_STATE_DIR", None)
                else:
                    os.environ["FLEET_TITLE_STATE_DIR"] = old_title_root

    def test_collect_all_cache_only_default_never_touches_network_or_walks_disk(self):
        """A11: `collect_all()`'s default (`usage="cache-only"`) path must never call the
        real fetchers, never hit the network, and never walk the filesystem looking for
        rollouts/transcripts to satisfy a usage snapshot — cache-only means cache-only. Called
        with NO harness filter (the default, real-world invocation): codex's rollout-index
        `prepare_tick` scan must not run when the process-table backbone finds zero codex
        sessions, not merely when codex is filtered out. procscan is mocked to a fixed,
        codex-free session list, and `jobs_path` points at an empty temp file so this
        machine's own real jobs.log (dispatch.py's own, separately-scoped codex rollout
        index — out of this fix's scope) can never leak a real codex job's os.walk into the
        assertion; the assertion stays about what F-51b actually owns."""
        from fleet import collectors
        from fleet.collectors import procscan, usage_api, dispatch
        from fleet.collectors import codex as codex_mod
        with tempfile.TemporaryDirectory() as jobs_tmp:
            jobs_path = os.path.join(jobs_tmp, "jobs.log")
            # An existing empty registry is authoritative. Without the file, dispatch's
            # tolerant migration fallback may inspect installed runtime registries.
            open(jobs_path, "w").close()
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": jobs_path}), \
                 mock.patch.object(procscan, "scan", return_value=[]), \
                 mock.patch.object(dispatch, "collect", return_value=[]), \
                 mock.patch.object(usage_api, "account_usage") as api_spy, \
                 mock.patch.object(codex_mod, "account_usage") as codex_spy, \
                 mock.patch("urllib.request.urlopen") as urlopen_spy, \
                 mock.patch("os.walk") as walk_spy:
                collectors.collect_all(jobs_path=jobs_path)
        api_spy.assert_not_called()
        codex_spy.assert_not_called()
        urlopen_spy.assert_not_called()
        walk_spy.assert_not_called()

    def test_collect_all_preserves_account_snapshot_without_a_session(self):
        """Account quota survives the collector boundary even with no process row."""
        from fleet import collectors
        from fleet.collectors import procscan
        snapshots = {
            "claude": {"payload": {"rl_5h": 31, "rl_7d": 81},
                       "freshness": "fresh", "observed_at": 1000},
            "codex": {"payload": None, "freshness": "unknown", "observed_at": None},
            "opencode": {"payload": None, "freshness": "unknown", "observed_at": None},
        }
        with tempfile.TemporaryDirectory() as jobs_tmp, \
             mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch.object(usage_cache, "account_usage",
                               side_effect=lambda harness, **_kwargs: snapshots[harness]):
            sessions, _jobs = collectors.collect_all(
                jobs_path=os.path.join(jobs_tmp, "jobs.log"))
        self.assertEqual([], sessions)
        self.assertEqual(
            {"rl_5h": 31, "rl_7d": 81},
            collectors.collect_all.last_usage_snapshots["claude"]["payload"],
        )
        self.assertIn("opencode", collectors.collect_all.last_usage_snapshots)

    def test_collect_all_replaces_stale_codex_window_label_from_account_cache(self):
        from fleet import collectors
        from fleet.collectors import procscan
        from fleet.collectors import codex as codex_mod
        from fleet.model import Session
        session = Session(harness="codex", pid=1, cwd=None, liveness="idle",
                          rl_5h=48, rl_windows=[["5h", 48, None]])
        snapshots = {
            "claude": {"payload": None, "freshness": "unknown", "observed_at": None},
            "codex": {"payload": {"rl_5h": None, "rl_7d": 48,
                                   "windows": [["7d", 48, 2000]]},
                      "freshness": "fresh", "observed_at": 1000},
            "opencode": {"payload": None, "freshness": "unknown", "observed_at": None},
        }
        with tempfile.TemporaryDirectory() as jobs_tmp, \
             mock.patch.object(procscan, "scan", return_value=[session]), \
             mock.patch.object(codex_mod, "prepare_tick", return_value={}), \
             mock.patch.object(codex_mod, "enrich"), \
             mock.patch.object(usage_cache, "account_usage",
                               side_effect=lambda harness, **_kwargs: snapshots[harness]):
            sessions, _jobs = collectors.collect_all(
                jobs_path=os.path.join(jobs_tmp, "jobs.log"))
        self.assertEqual([["7d", 48, 2000]], sessions[0].rl_windows)
        self.assertEqual(48, sessions[0].rl_7d)

    def test_refresh_returns_before_permanently_blocked_fetcher_and_then_reads_new_cache(self):
        gate = threading.Event()
        usage_cache.FETCHERS["claude"] = lambda: (gate.wait(), {"rl_5h": 77})[1]
        result = usage_cache.account_usage("claude", usage="refresh", now=1000)
        self.assertEqual(result["freshness"], "unknown")
        worker = usage_cache._THREADS.get("claude")
        self.assertIsNotNone(worker, "refresh did not register its worker thread")
        gate.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "worker thread did not finish within timeout")
        self.assertEqual(usage_cache.account_usage("claude")["payload"]["rl_5h"], 77)


if __name__ == "__main__":
    unittest.main()
