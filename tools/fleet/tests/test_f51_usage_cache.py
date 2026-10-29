import os
import sys
import tempfile
import time
import unittest
import json
import threading
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import usage_cache, usage_api


class F51UsageCacheTest(unittest.TestCase):
    def setUp(self):
        # Usage fixtures have no native pane source; do not start a live Herdr
        # observation transport while testing account-cache/opt-out behavior.
        pane_patch = mock.patch("fleet.collectors.herdr.enrich", return_value=None)
        pane_patch.start()
        self.addCleanup(pane_patch.stop)
        self.credential_patch = mock.patch.object(
            usage_api, "_credential", return_value=usage_api._Credential("fixture", "fixture-generation"))
        self.credential_patch.start()
        self.addCleanup(self.credential_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.dispatch_tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("FLEET_USAGE_STATE_DIR")
        self.old_dispatch = os.environ.get("AGENT_DISPATCH_JOBS")
        self.old_resource_index = os.environ.get("AGENT_RESOURCE_RUN_INDEX")
        os.environ["FLEET_USAGE_STATE_DIR"] = self.tmp.name
        jobs = os.path.join(self.dispatch_tmp.name, "jobs.log")
        open(jobs, "w").close()
        os.environ["AGENT_DISPATCH_JOBS"] = jobs
        os.environ["AGENT_RESOURCE_RUN_INDEX"] = os.path.join(
            self.dispatch_tmp.name, "resource-runs.index.json")
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
        for key, value in (
            ("AGENT_DISPATCH_JOBS", self.old_dispatch),
            ("AGENT_RESOURCE_RUN_INDEX", self.old_resource_index),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()
        self.dispatch_tmp.cleanup()

    def test_record_and_widened_read_serve_ad_hoc_probes(self):
        # 2026-09-16: dispatch placement writes its own live reading through and
        # reads the last-good one back beyond the Fleet freshness window.
        self.assertTrue(usage_cache.record("codex", {"windows": [["7d", 40, None]]}, now=1000.0))
        self.assertEqual(usage_cache.read("codex", now=1000.0)["freshness"], "fresh")
        self.assertEqual(usage_cache.read("codex", now=1000.0 + 1200)["freshness"], "unknown")
        widened = usage_cache.read("codex", now=1000.0 + 1200, stale_max=3600)
        self.assertEqual(widened["freshness"], "stale")
        self.assertEqual(widened["payload"]["windows"][0][1], 40)
        self.assertFalse(usage_cache.record("codex", None))
        self.assertTrue(usage_cache.record_attempt("codex", now=1000.0 + 1300))
        after = usage_cache.read("codex", now=1000.0 + 1300, stale_max=3600)
        self.assertEqual(after["attempted_at"], 2300.0)
        self.assertEqual(after["observed_at"], 1000.0)
        self.assertEqual(after["payload"]["windows"][0][1], 40)

    def test_provider_rejection_does_not_refresh_quota_and_is_account_scoped(self):
        self.assertTrue(usage_cache.record("opencode", {
            "rl_5h": 5, "rl_windows": [["5h", 5, 9000.0]]}, now=1000.0))
        self.assertTrue(usage_cache.record_provider(
            "opencode", "opencode-go", "*", "account:one", "unavailable",
            reason="credential-expired", now=1100.0))
        snapshot = usage_cache.read("opencode", now=1500.0, stale_max=3600)
        self.assertEqual(snapshot["observed_at"], 1000.0)
        self.assertEqual(snapshot["payload"]["rl_5h"], 5)
        self.assertEqual(usage_cache.read_provider(
            "opencode", "opencode-go", "*", "account:one", now=1500)["reason"],
            "credential-expired")
        self.assertEqual(usage_cache.read_provider(
            "opencode", "opencode-go", "*", "account:two", now=1500)["state"], "unknown")
        self.assertEqual(usage_cache.provider_records("opencode")[0]["state"], "unavailable")

    def test_error_marker_survives_without_refreshing_quota_age(self):
        usage_cache._write("opencode", {"error": "missing-credential",
                                         "reason": "credential-not-found"},
                           0.0, 1000.0)
        snapshot = usage_cache.read("opencode", now=1001.0)
        self.assertIsNone(snapshot["payload"])
        self.assertEqual(snapshot["freshness"], "unknown")
        self.assertEqual(snapshot["error_observation"]["error"], "missing-credential")
        expired = usage_cache.read("opencode", now=1000.0 + usage_cache.STALE_MAX + 1)
        self.assertIsNone(expired["error_observation"])

    def test_diagnostics_validate_schema_and_independent_bounded_time(self):
        path = os.path.join(self.tmp.name, "opencode.json")
        for observed_at in (True, float("inf"), float("nan"), 1001.0):
            item = {"schema_version": 1, "harness": "opencode", "fetched_at": 0.0,
                    "attempted_at": 1000.0, "payload": {},
                    "error_observation": {"error": "missing-credential",
                                           "reason": "credential-not-found",
                                           "observed_at": observed_at}}
            with open(path, "w") as fh:
                json.dump(item, fh)
            self.assertIsNone(usage_cache.read("opencode", now=1000.0)["error_observation"])
        item["schema_version"] = 2
        item["error_observation"]["observed_at"] = 999.0
        with open(path, "w") as fh:
            json.dump(item, fh)
        self.assertIsNone(usage_cache.read("opencode", now=1000.0)["error_observation"])

        self._write_cache("opencode", 1000.0, {"rl_5h": 7},
                          error_observation={"error": "auth", "reason": "credential-expired",
                                             "observed_at": 900.0})
        snap = usage_cache.read("opencode", now=1100.0, stale_max=3600)
        self.assertEqual(snap["payload"]["rl_5h"], 7)
        self.assertEqual(snap["error_observation"]["observed_at"], 900.0)
        self.assertIsNone(usage_cache.read("opencode", now=1100.0,
                                           stale_max=100)["error_observation"])

    def test_legacy_error_marker_keeps_original_time_through_none_and_attempt(self):
        usage_cache._write("opencode", {"error": "missing-credential",
                                         "reason": "credential-not-found"},
                           0.0, 1000.0)
        self.assertEqual(usage_cache.read("opencode", now=1010)["error_observation"]["observed_at"],
                         1000.0)
        usage_cache.FETCHERS["opencode"] = lambda: None
        with mock.patch.object(usage_cache.time, "time", return_value=1100.0):
            usage_cache._worker("opencode")
        self.assertEqual(usage_cache.read("opencode", now=1100)["error_observation"]["observed_at"],
                         1000.0)
        usage_cache.record_attempt("opencode", now=1200.0)
        self.assertEqual(usage_cache.read("opencode", now=1200)["error_observation"]["observed_at"],
                         1000.0)
        self.assertIsNone(usage_cache.read(
            "opencode", now=1000.0 + usage_cache.STALE_MAX + 1)["error_observation"])

    def test_rejection_does_not_merge_other_account_quota_or_store_raw_detail(self):
        usage_cache._write("opencode", {"rl_5h": 23, "account_scope": "scope:A"},
                           1000.0, 1000.0)
        usage_cache.FETCHERS["opencode"] = lambda: {
            "error": "auth", "reason": "credential-expired", "detail": "raw-secret",
            "account_scope": "scope:B"}
        with mock.patch.object(usage_cache.time, "time", return_value=1100.0):
            usage_cache._worker("opencode")
        path = os.path.join(self.tmp.name, "opencode.json")
        with open(path, encoding="utf-8") as fh:
            persisted = json.load(fh)
        self.assertNotIn("rl_5h", persisted["payload"])
        self.assertEqual(persisted["payload"]["account_scope"], "scope:B")
        raw = json.dumps(persisted)
        self.assertNotIn("raw-secret", raw)

        usage_cache._write("opencode", {"rl_5h": 8, "account_scope": "scope:B"},
                           1200.0, 1200.0)
        usage_cache.FETCHERS["opencode"] = lambda: {
            "error": "auth", "reason": "credential-expired", "detail": "raw-secret",
            "account_scope": "scope:B"}
        with mock.patch.object(usage_cache.time, "time", return_value=1300.0):
            usage_cache._worker("opencode")
        with open(path, encoding="utf-8") as fh:
            same_account = json.load(fh)
        self.assertEqual(same_account["payload"]["rl_5h"], 8)
        self.assertEqual(same_account["fetched_at"], 1200.0)
        self.assertNotIn("raw-secret", json.dumps(same_account))

    def test_new_same_account_success_clears_only_the_matching_scope(self):
        usage_cache.record_provider("opencode", "opencode-go", "*", "account:one",
                                    "unavailable", reason="credential-expired", now=1000)
        usage_cache.record_provider("opencode", "opencode-go", "*", "account:one",
                                    "usable", now=1100)
        self.assertEqual(usage_cache.read_provider(
            "opencode", "opencode-go", "*", "account:one", now=1100)["state"], "usable")
        usage_cache.record_provider("opencode", "opencode-go", "*", "account:one",
                                    "unavailable", reason="subscription-missing", now=1200)
        self.assertEqual(usage_cache.read_provider(
            "opencode", "opencode-go", "*", "account:one", now=1200)["reason"],
            "subscription-missing")

    def test_credential_generation_scopes_evidence_and_old_rows_keep_their_meaning(self):
        usage_cache.record_provider("opencode", "openai", "gpt", "account:one", "unavailable",
                                    reason="authentication-rejected", now=1000,
                                    credential_generation="gen:first")
        read = lambda generation: usage_cache.read_provider(
            "opencode", "openai", "gpt", "account:one", now=1100,
            credential_generation=generation)
        self.assertEqual(read("gen:first")["reason"], "authentication-rejected")
        self.assertNotIn("credential_generation", read("gen:first"))
        # Re-login under the same row id: same account scope, new credential.
        self.assertEqual((read("gen:second")["state"], read("gen:second")["reason"]),
                         ("unknown", "credential-generation-changed"))
        # A reader that cannot name its generation, and a row recorded before
        # generations existed, both keep today's meaning.
        self.assertEqual(read(None)["state"], "unavailable")
        usage_cache.record_provider("opencode", "openai", "gpt", "account:one", "unavailable",
                                    reason="authentication-rejected", now=1000)
        self.assertEqual(read("gen:second")["state"], "unavailable")
        self.assertNotIn("credential_generation",
                         json.dumps(usage_cache.provider_records("opencode")))

    def test_cache_only_never_calls_fetcher(self):
        calls = []
        usage_cache.FETCHERS["claude"] = lambda _credential: calls.append(1)
        self.assertEqual(usage_cache.account_usage("claude")["freshness"], "unknown")
        self.assertEqual(calls, [])

    def test_refresh_is_single_flight_and_atomic(self):
        calls = []
        usage_cache.FETCHERS["claude"] = lambda _credential: (calls.append(1) or {"rl_5h": 12})
        self.assertTrue(usage_cache.request_refresh("claude"))
        thread = usage_cache._THREADS.get("claude")
        self.assertIsNotNone(thread, "request_refresh did not register its worker thread")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "worker thread did not finish within timeout")
        self.assertEqual(len(calls), 1)
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 12)

    def test_fresh_cache_suppresses_refresh_until_harness_window_expires(self):
        calls = []
        usage_cache.FETCHERS["claude"] = lambda _credential: calls.append(1)
        self._write_cache("claude", 1000)
        self.assertFalse(usage_cache.request_refresh("claude", now=1179))
        self.assertEqual(calls, [])
        self.assertNotIn("claude", usage_cache._THREADS)

    def _write_cache(self, harness, fetched_at, payload=None, **extra):
        item = {"schema_version": usage_cache.SCHEMA_VERSION, "harness": harness,
                "fetched_at": fetched_at, "attempted_at": fetched_at,
                "payload": payload or {"rl_5h": 42}}
        if harness == "claude":
            item["_credential_generation"] = "fixture-generation"
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
            usage_cache._write("claude", {"value": value}, 1000, 1000,
                               credential_generation="fixture-generation")
        stop.set(); thread.join()
        self.assertTrue(seen)
        self.assertNotIn("unknown", seen)

    def test_eight_concurrent_refresh_requests_have_one_fetch(self):
        calls = []
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        results = []

        def fetch(_credential):
            calls.append(1)
            fetch_started.set()
            if not release_fetch.wait(timeout=5):
                raise RuntimeError("concurrency fixture did not release fetch")
            return {"rl_5h": 9}

        usage_cache.FETCHERS["claude"] = fetch
        threads = [
            threading.Thread(
                target=lambda: results.append(usage_cache.request_refresh("claude"))
            )
            for _ in range(8)
        ]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(fetch_started.wait(timeout=5), "refresh worker did not start")
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "request thread did not finish")
        finally:
            release_fetch.set()
        worker = usage_cache._THREADS.get("claude")
        if worker is not None:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "worker thread did not finish within timeout")
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len([x for x in os.listdir(self.tmp.name) if x.startswith(".claude.lease")]), 0)

    def test_stale_lease_older_than_60s_is_reclaimed_by_the_next_request(self):
        """A10a: a lease file left behind by a thread that was killed (SIGKILL, crash) never
        clears itself — the NEXT `request_refresh` call must reclaim it once `LEASE_MAX`
        (60s) has elapsed, rather than backing off forever."""
        calls = []
        usage_cache.FETCHERS["claude"] = lambda _credential: (calls.append(1) or {"rl_5h": 5})
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
                usage_cache.FETCHERS["claude"] = lambda _credential: {"rl_5h": 1}
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

    def test_collect_all_confirms_only_current_local_missing_credential(self):
        from fleet import collectors
        from fleet.collectors import procscan
        import importlib
        from types import SimpleNamespace
        utilities = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                                 "utilities"))
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        runtime = importlib.import_module("opencode_runtime")
        jobs_path = os.path.join(self.tmp.name, "jobs.log")
        open(jobs_path, "w").close()
        unavailable = SimpleNamespace(state="unavailable", reason="credential-not-found",
                                      provider_scope=None, opencode_scope=None,
                                      entitlement_scope=None)
        snapshot = {"payload": None, "freshness": "unknown", "observed_at": None,
                    "error_observation": None}
        with mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32"}), \
             mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch("fleet.collectors.dispatch.collect", return_value=[]), \
             mock.patch.object(usage_cache, "account_usage", return_value=snapshot), \
             mock.patch.object(runtime, "is_v2", return_value=True), \
             mock.patch.object(runtime, "selected_go_usage_observation", return_value=unavailable), \
             mock.patch.object(runtime, "account_scope_observation",
                               return_value=SimpleNamespace(scope=None)):
            sessions, _jobs = collectors.collect_all(harness_filter={"opencode"},
                                                      jobs_path=jobs_path)
        self.assertEqual(sessions, [])
        observed = collectors.collect_all.last_usage_snapshots["opencode"]["error_observation"]
        self.assertEqual((observed["error"], observed["reason"]),
                         ("missing-credential", "credential-not-found"))
        self.assertNotIn("credential-not-found", json.dumps(snapshot.get("payload")))

    def test_restored_go_credentials_discard_old_unscoped_denial(self):
        from fleet import collectors
        from fleet.collectors import procscan
        import importlib
        from types import SimpleNamespace
        utilities = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                                 "utilities"))
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        runtime = importlib.import_module("opencode_runtime")
        jobs_path = os.path.join(self.tmp.name, "jobs.log")
        open(jobs_path, "w").close()
        selected = SimpleNamespace(state="usable", reason=None, provider_scope="scope:A",
                                   opencode_scope="scope:A", entitlement_scope="scope:A")
        snapshot = {"payload": None, "freshness": "unknown", "observed_at": None,
                    "error_observation": {"error": "missing-credential",
                                          "reason": "credential-not-found",
                                          "observed_at": time.time()}}
        with mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": "2.0.32"}), \
             mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch("fleet.collectors.dispatch.collect", return_value=[]), \
             mock.patch.object(usage_cache, "account_usage", return_value=snapshot), \
             mock.patch.object(runtime, "is_v2", return_value=True), \
             mock.patch.object(runtime, "selected_go_usage_observation", return_value=selected), \
             mock.patch.object(runtime, "account_scope_observation",
                               return_value=SimpleNamespace(scope="scope:A")):
            collectors.collect_all(harness_filter={"opencode"}, jobs_path=jobs_path)
        self.assertIsNone(collectors.collect_all.last_usage_snapshots["opencode"]
                          ["error_observation"])

    def test_no_installation_evidence_does_not_fabricate_local_error_row(self):
        from fleet import collectors
        from fleet.collectors import procscan
        import importlib
        from types import SimpleNamespace
        utilities = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                                 "utilities"))
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        runtime = importlib.import_module("opencode_runtime")
        jobs_path = os.path.join(self.tmp.name, "jobs-no-install.log")
        open(jobs_path, "w").close()
        snapshot = {"payload": None, "freshness": "unknown", "observed_at": None,
                    "error_observation": None}
        with mock.patch.dict(os.environ, {"OPENCODE_RUNTIME_VERSION": ""}), \
             mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch("fleet.collectors.dispatch.collect", return_value=[]), \
             mock.patch.object(usage_cache, "account_usage", return_value=snapshot), \
             mock.patch.object(runtime, "is_v2", return_value=False), \
             mock.patch.object(runtime, "selected_go_usage_observation",
                               return_value=SimpleNamespace(state="unavailable",
                                                            reason="credential-not-found")):
            collectors.collect_all(harness_filter={"opencode"}, jobs_path=jobs_path)
        self.assertIsNone(collectors.collect_all.last_usage_snapshots["opencode"]
                          ["error_observation"])

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
        usage_cache.FETCHERS["claude"] = lambda _credential: (gate.wait(), {"rl_5h": 77})[1]
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
