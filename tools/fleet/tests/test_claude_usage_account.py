"""Private credentials, fake time and event-controlled requests; never live OAuth."""
import io
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet.collectors import usage_api, usage_cache


def _process_refresh(state, auth, start, fetched, release, results, now, value):
    os.environ["FLEET_USAGE_STATE_DIR"] = state
    os.environ["CLAUDE_CONFIG_DIR"] = auth

    def fetch(_credential):
        fetched.set()
        if not release.wait(20):
            raise RuntimeError("fixture release missing")
        return {"rl_5h": value}

    usage_cache.FETCHERS["claude"] = fetch
    with mock.patch.object(usage_cache.time, "time", return_value=now):
        if not start.wait(20):
            raise RuntimeError("fixture start missing")
        started = usage_cache.request_refresh("claude")
        results.put(started)
        release.wait(20)
        thread = usage_cache._THREADS.get("claude")
        if thread is not None:
            thread.join(20)
            if thread.is_alive():
                raise RuntimeError("fixture worker still alive")


class ClaudeUsageAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.state = self.root / "usage"
        self.environment = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.auth), "FLEET_USAGE_STATE_DIR": str(self.state),
            "AGENT_DISPATCH_JOBS": str(self.root / "jobs.log"),
            "AGENT_RESOURCE_RUN_INDEX": str(self.root / "resources.json"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.now = 10000.0
        self.clock = mock.patch.object(usage_cache.time, "time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.network = mock.patch.object(usage_api.urllib.request, "urlopen",
                                         side_effect=AssertionError("live network forbidden"))
        self.urlopen = self.network.start()
        self.addCleanup(self.network.stop)
        self.original_fetchers = dict(usage_cache.FETCHERS)
        self.workers = []
        self.releases = []
        self.credential("A")

    def tearDown(self):
        for event in self.releases:
            event.set()
        for worker in self.workers:
            worker.join(10)
            self.assertFalse(worker.is_alive(), "fixture worker did not settle")
        usage_cache.FETCHERS.clear()
        usage_cache.FETCHERS.update(self.original_fetchers)
        self.assertNotIn("claude", usage_cache._THREADS)

    def credential(self, name):
        # Deliberately no account ID. A profile shared by A/B is not evidence.
        (self.auth / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "private-fixture-" + name}}))
        return usage_api._credential()

    def cached(self, payload=None, observed=None, error=None, credential=None):
        credential = credential or usage_api._credential()
        observed = self.now if observed is None else observed
        usage_cache._write("claude", payload or {"rl_5h": 12}, observed, observed,
                           error_observation=error, credential_generation=credential.generation)

    def start(self, payload, *, blocked=False):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def fetch(credential):
            self.workers.append(threading.current_thread())
            entered.set()
            if not release.wait(10):
                raise RuntimeError("fixture release missing")
            return payload(credential) if callable(payload) else payload

        usage_cache.FETCHERS["claude"] = fetch
        self.assertTrue(usage_cache.request_refresh("claude"))
        self.assertTrue(entered.wait(10))
        worker = self.workers[-1]
        if not blocked:
            release.set()
            worker.join(10)
            self.assertFalse(worker.is_alive())
        return worker, release

    def test_same_generation_success_and_public_redaction(self):
        self.start({"rl_5h": 17, "rl_7d": 32})
        snapshot = usage_cache.read("claude")
        self.assertEqual(snapshot["freshness"], "fresh")
        self.assertEqual(snapshot["payload"]["rl_7d"], 32)
        self.assertEqual(snapshot["observed_at"], self.now)
        public = json.dumps(snapshot)
        self.assertNotIn("generation", public)
        self.assertNotIn("private-fixture", public)
        self.assertNotIn(usage_api._credential().generation, public)
        self.assertEqual((self.state / "claude.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(usage_cache.request_refresh("claude"))

    def test_fresh_A_does_not_block_B_or_same_profile_token_rotation(self):
        (self.auth / "profile.json").write_text('{"profile":"unchanged-fixture"}')
        self.cached()
        self.credential("B")
        snapshot = usage_cache.read("claude")
        self.assertIsNone(snapshot["payload"])
        self.assertIsNone(snapshot["attempted_at"])
        self.assertEqual(snapshot["error_observation"]["reason"], "credential-changed")
        self.start({"rl_5h": 27})
        self.credential("B-rotated")
        self.assertIsNone(usage_cache.read("claude")["payload"])
        self.start({"rl_5h": 38})
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 38)

    def test_missing_invalid_unreadable_and_legacy_unbound_cache_are_unknown(self):
        self.cached()
        for content, reason in ((None, "credential-not-found"), ("{", "credential-invalid"),
                                ('{"claudeAiOauth":{"accessToken":false}}', "credential-invalid")):
            path = self.auth / ".credentials.json"
            if content is None:
                path.unlink()
            else:
                path.write_text(content)
            snapshot = usage_cache.read("claude")
            self.assertIsNone(snapshot["payload"])
            self.assertEqual(snapshot["error_observation"]["reason"], reason)
            self.assertFalse(usage_cache.request_refresh("claude"))
        with mock.patch("builtins.open", side_effect=PermissionError):
            self.assertEqual(usage_api._credential().reason, "credential-store-read-failed")
        self.credential("A")
        usage_cache._write("claude", {"rl_5h": 99}, self.now, self.now)
        self.assertIsNone(usage_cache.read("claude")["payload"])
        self.start({"rl_5h": 40})
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 40)

    def test_old_auth_error_and_backoff_cannot_block_new_credentials(self):
        for error in ("auth", "rate-limit"):
            self.start({"error": error, "reason": "authentication-rejected" if error == "auth"
                        else "usage-rate-limited", "retry_at": self.now + 3000})
            self.assertFalse(usage_cache.request_refresh("claude"))
            self.credential(error)
            self.assertIsNone(usage_cache.read("claude")["error_observation"].get("retry_at"))
            self.start({"rl_5h": 15})
            self.now += 181

    def test_failure_keeps_last_good_age_and_long_retry_after_outlives_quota(self):
        self.cached(observed=self.now - 200)
        self.start({"error": "rate-limit", "reason": "usage-rate-limited",
                    "retry_at": self.now + 3000, "detail": "private-body"})
        snapshot = usage_cache.read("claude")
        self.assertEqual(snapshot["freshness"], "stale")
        self.assertEqual(snapshot["observed_at"], 9800)
        self.assertEqual(snapshot["payload"]["rl_5h"], 12)
        self.assertNotIn("private-body", (self.state / "claude.json").read_text())
        self.now += 2000
        snapshot = usage_cache.read("claude")
        self.assertIsNone(snapshot["payload"])
        self.assertEqual(snapshot["error_observation"]["observed_at"], 10000)
        self.assertEqual(snapshot["error_observation"]["retry_at"], 13000)
        self.assertFalse(usage_cache.request_refresh("claude"))
        self.now = 13000
        self.start({"rl_5h": 24})
        self.assertIsNone(usage_cache.read("claude")["error_observation"])

    def test_short_retry_after_keeps_the_existing_minimum_retry_interval(self):
        self.start({"error": "rate-limit", "reason": "usage-rate-limited",
                    "retry_at": self.now + 5})
        self.now += 4
        self.assertFalse(usage_cache.request_refresh("claude"))
        self.now += 1
        self.assertFalse(usage_cache.request_refresh("claude"))
        self.assertEqual(usage_cache.read("claude")["error_observation"]["retry_at"], 10060)
        self.now = 10060
        self.start({"rl_5h": 6})

    def test_late_A_success_error_and_ABA_cannot_publish_or_clear_new_lease(self):
        for old_payload in ({"rl_5h": 91}, {"error": "auth", "retry_at": 20000}):
            for aba in (False, True):
                with self.subTest(error="error" in old_payload, aba=aba):
                    self.credential("A")
                    # Previous iteration's same-token cache must not skip this request.
                    self.now += 181
                    old, release_old = self.start(old_payload, blocked=True)
                    self.credential("B")
                    new, release_new = self.start({"rl_5h": 22}, blocked=True)
                    lease_before = (self.state / ".claude.lease").read_bytes()
                    if aba:
                        self.credential("A")
                    release_old.set()
                    old.join(10)
                    self.assertFalse(old.is_alive())
                    self.assertEqual((self.state / ".claude.lease").read_bytes(), lease_before)
                    self.assertIs(usage_cache._THREADS["claude"], new)
                    release_new.set()
                    new.join(10)
                    self.assertFalse(new.is_alive())
                    if aba:
                        self.assertIsNone(usage_cache.read("claude")["payload"])
                        self.start({"rl_5h": 23})
                    else:
                        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 22)

    def test_late_success_or_error_does_not_overwrite_B_data_or_backoff(self):
        case = 0
        for old_payload in ({"rl_5h": 98}, {"error": "auth", "retry_at": 20000}):
            for new_payload in ({"rl_5h": 8}, {"error": "rate-limit",
                                               "reason": "usage-rate-limited", "retry_at": 30000}):
                case += 1
                self.credential("A-" + str(case))
                self.now += 181
                old, release = self.start(old_payload, blocked=True)
                self.credential("B-" + str(case))
                self.start(new_payload)
                persisted = (self.state / "claude.json").read_bytes()
                release.set()
                old.join(10)
                self.assertEqual((self.state / "claude.json").read_bytes(), persisted)

    def test_expired_same_generation_lease_reclaims_without_old_cleanup(self):
        old, release_old = self.start({"rl_5h": 91}, blocked=True)
        self.now += usage_cache.LEASE_MAX + 1
        new, release_new = self.start({"rl_5h": 19}, blocked=True)
        lease_before = (self.state / ".claude.lease").read_bytes()
        release_old.set()
        old.join(10)
        self.assertEqual((self.state / ".claude.lease").read_bytes(), lease_before)
        release_new.set()
        new.join(10)
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 19)

    def test_actual_request_uses_captured_credential_during_ABA(self):
        captured = usage_api._credential()
        self.credential("B")
        seen = []
        def respond(request, **_kwargs):
            seen.append(request.get_header("Authorization") == "Bearer " + captured.token)
            self.credential("A")
            return io.BytesIO(b'{"five_hour":{"utilization":14}}')
        self.urlopen.side_effect = respond
        self.assertEqual(usage_api._fetch(captured)["rl_5h"], 14)
        self.assertEqual(seen, [True])
        self.assertNotIn(captured.token, repr(captured))
        self.assertNotIn(captured.generation, repr(captured))

    def test_change_during_cache_read_does_not_expose_old_payload(self):
        self.cached()
        a = usage_api._credential()
        b = self.credential("B")
        with mock.patch.object(usage_api, "_credential", side_effect=[a, b]):
            self.assertIsNone(usage_cache.read("claude")["payload"])

    def test_HTTP_errors_timeout_network_and_incomplete_responses_are_distinct(self):
        for status, error in ((401, "auth"), (403, "auth"), (429, "rate-limit"), (503, "unknown")):
            self.urlopen.side_effect = urllib.error.HTTPError(
                "https://fixture.invalid", status, "private-body", {"Retry-After": "600"},
                io.BytesIO(b'private-body'))
            result = usage_api._fetch(usage_api._credential())
            self.assertEqual(result["error"], error)
            self.assertEqual(result["retry_at"], self.now + 600)
            self.assertNotIn("private-body", json.dumps(result))
            self.assertNotIn("rl_5h", result)
        for exception, error in ((TimeoutError("private-body"), "timeout"),
                                 (urllib.error.URLError(TimeoutError()), "timeout"),
                                 (urllib.error.URLError("private-body"), "network"),
                                 (http.client.IncompleteRead(b"private-body"), "response-invalid")):
            self.urlopen.side_effect = exception
            self.assertEqual(usage_api._fetch(usage_api._credential())["error"], error)
        self.urlopen.side_effect = None
        for body in (b'{}', b'[]', b'{', b'{"five_hour":{"utilization":false}}',
                     b'{"limits":"invalid","seven_day":5}',
                     b'{"five_hour":{"utilization":NaN}}'):
            self.urlopen.return_value = io.BytesIO(body)
            self.assertEqual(usage_api._fetch(usage_api._credential())["error"], "response-invalid")

    def test_ad_hoc_probe_preserves_quota_dict_or_none_compatibility(self):
        # tools/release/first-journey.py calls _fetch() without an owned binding.
        self.urlopen.side_effect = None
        self.urlopen.return_value = io.BytesIO(b'{"five_hour":{"utilization":16}}')
        self.assertEqual(usage_api._fetch()["rl_5h"], 16)
        self.urlopen.return_value = io.BytesIO(b'{}')
        self.assertIsNone(usage_api._fetch())
        self.urlopen.side_effect = urllib.error.HTTPError(
            "https://fixture.invalid", 429, "private-body", {"Retry-After": "600"}, None)
        self.assertIsNone(usage_api._fetch())
        self.assertEqual(usage_api._fetch(usage_api._credential())["error"], "rate-limit")

    def test_Retry_After_seconds_date_invalid_and_absent(self):
        from email.utils import formatdate
        for value, expected in (("600", 10600), (formatdate(10900, usegmt=True), 10900),
                                ("0", 10000), (formatdate(9000, usegmt=True), 10000),
                                (None, 10060), ("-1", 10060), ("1.5", 10060),
                                ("NaN", 10060), ("Infinity", 10060), ("bad", 10060),
                                ("9" * 400, 10060)):
            self.assertEqual(usage_api._retry_at(value, self.now), expected)

    def test_partial_success_does_not_make_missing_old_buckets_fresh(self):
        self.cached({"rl_5h": 12, "rl_ms": [["model", 91]], "rs_7d": 20000},
                    observed=self.now - 200)
        self.start(usage_api._quota({"five_hour": {"utilization": 0}}))
        payload = usage_cache.read("claude")["payload"]
        self.assertEqual(payload["rl_5h"], 0)  # Actual observed zero is legitimate.
        self.assertEqual(payload["rl_ms"], [])
        self.assertIsNone(payload["rs_7d"])

    def test_legacy_account_usage_shares_the_owned_cache_and_does_not_fetch_directly(self):
        self.cached()
        self.assertEqual(usage_api.account_usage()["rl_5h"], 12)
        self.credential("B")
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def fetch(_credential):
            self.workers.append(threading.current_thread())
            entered.set()
            release.wait(10)
            return {"rl_5h": 18}
        usage_cache.FETCHERS["claude"] = fetch
        self.assertIsNone(usage_api.account_usage())
        self.assertTrue(entered.wait(10))
        self.assertIsNone(usage_api.account_usage())
        self.assertEqual(len(self.workers), 1)
        release.set()
        self.workers[0].join(10)
        self.assertEqual(usage_api.account_usage()["rl_5h"], 18)
        self.urlopen.assert_not_called()
        self.assertFalse(usage_cache.record("claude", {"rl_5h": 99}))
        self.assertFalse(usage_cache.record_attempt("claude"))
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 18)

    def test_unbound_native_tap_cannot_resurrect_quota_but_keeps_other_telemetry(self):
        from fleet.collectors import claude
        from fleet.model import Session
        session = Session(harness="claude", pid=1, session_id="fixture-session",
                          rl_5h=11, rl_7d=33, rl_rs=(20000, 30000), rl_ms=[["model", 9]])
        claude._apply_statusline(session, {"session_id": "fixture-session", "model": {"id": "fixture"},
            "cost": {"total_cost_usd": 1.2}, "rate_limits": {
                "five_hour": {"used_percentage": 99}, "seven_day": {"used_percentage": 98}}})
        self.assertIsNone(session.rl_5h)
        self.assertIsNone(session.rl_7d)
        self.assertIsNone(session.rl_ms)
        self.assertIsNone(session.rl_rs)
        self.assertEqual(session.model, "fixture")
        self.assertEqual(session.cost, 1.2)

    def test_blocked_Claude_does_not_hold_shared_cache_lock(self):
        _worker, release = self.start({"rl_5h": 1}, blocked=True)
        self.assertTrue(usage_cache.record("codex", {"rl_7d": 44}))
        self.assertTrue(usage_cache.record("opencode", {"rl_5h": 33}))
        self.assertEqual(usage_cache.read("codex")["payload"]["rl_7d"], 44)
        self.assertEqual(usage_cache.read("opencode")["payload"]["rl_5h"], 33)
        release.set()

    def test_cross_process_single_flight_uses_one_request(self):
        ctx = multiprocessing.get_context("spawn")
        start, fetched, release = ctx.Event(), ctx.Event(), ctx.Event()
        results = ctx.Queue()
        processes = [ctx.Process(target=_process_refresh,
            args=(str(self.state), str(self.auth), start, fetched, release, results, self.now, 21))
                     for _ in range(2)]
        try:
            for process in processes:
                process.start()
            start.set()
            self.assertTrue(fetched.wait(20))
            started = [results.get(timeout=20) for _ in processes]
            self.assertEqual(started.count(True), 1)
        finally:
            release.set()
            for process in processes:
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
            results.close()
            results.join_thread()
        self.assertEqual([p.exitcode for p in processes], [0, 0])
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 21)

    def test_cross_process_late_A_cannot_overwrite_completed_B(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        start.set()
        results = ctx.Queue()
        releases, processes = [], []
        try:
            for name, value in (("A", 91), ("B", 25)):
                self.credential(name)
                fetched, release = ctx.Event(), ctx.Event()
                releases.append(release)
                process = ctx.Process(target=_process_refresh, args=(str(self.state), str(self.auth),
                    start, fetched, release, results, self.now, value))
                processes.append(process)
                process.start()
                self.assertTrue(fetched.wait(20))
                self.assertTrue(results.get(timeout=20))
            releases[1].set()
            processes[1].join(20)
            persisted = (self.state / "claude.json").read_bytes()
            releases[0].set()
            processes[0].join(20)
            self.assertEqual((self.state / "claude.json").read_bytes(), persisted)
        finally:
            for release in releases:
                release.set()
            for process in processes:
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
            results.close()
            results.join_thread()
        self.assertEqual([p.exitcode for p in processes], [0, 0])
        self.assertEqual(usage_cache.read("claude")["payload"]["rl_5h"], 25)


if __name__ == "__main__":
    unittest.main()
