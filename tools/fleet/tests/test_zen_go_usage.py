import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet import collectors
from fleet.collectors import dispatch
from fleet.collectors import usage_cache
from fleet.collectors import zen_go_usage


PROBED_BODY = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-04T10:48:37.338Z"},
        "weekly": {"status": "ok", "percent": 2, "resetsAt": "2026-09-07T00:00:00.338Z"},
        "monthly": {"status": "ok", "percent": 1, "resetsAt": "2026-09-28T04:51:28.338Z"},
    }
}


class OpenCodeV1Test(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        auth_path = str(Path(root.name) / "auth.json")
        version = mock.patch.dict(os.environ, {
            "OPENCODE_RUNTIME_VERSION": "1.19.0", "OPENCODE_AUTH": auth_path})
        version.start()
        self.addCleanup(version.stop)


class ZenGoTokenTest(OpenCodeV1Test):
    def test_token_reads_opencode_go_key_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w") as fh:
                json.dump({"opencode-go": {"type": "api", "key": "sk-go-test"}}, fh)
            with mock.patch.object(zen_go_usage._runtime(), "credential_store_present", return_value=False), \
                 mock.patch.object(zen_go_usage, "_auth_path", return_value=path):
                self.assertEqual(zen_go_usage._token(), "sk-go-test")

    def test_token_missing_entry_or_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w") as fh:
                json.dump({"opencode-go": {"type": "oauth"}}, fh)
            with mock.patch.object(zen_go_usage._runtime(), "credential_store_present", return_value=False), \
                 mock.patch.object(zen_go_usage, "_auth_path", return_value=path):
                self.assertIsNone(zen_go_usage._token())
            with mock.patch.object(zen_go_usage._runtime(), "credential_store_present", return_value=False), \
                 mock.patch.object(zen_go_usage, "_auth_path",
                                   return_value=os.path.join(tmp, "absent.json")):
                self.assertIsNone(zen_go_usage._token())


class ZenGoFetchTest(OpenCodeV1Test):
    def setUp(self):
        super().setUp()
        self.cache = {"ts": 0.0, "ok_ts": 0.0, "data": None,
                      "scope": zen_go_usage._usage_account_scope()}
        cache_patcher = mock.patch.object(zen_go_usage, "_cache", self.cache)
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)
        store_patcher = mock.patch.object(zen_go_usage._runtime(), "credential_store_present",
                                          return_value=False)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

    def _fetch_with(self, body):
        opener = mock.MagicMock()
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(body).encode()
        opener.return_value = resp
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen", opener):
            return zen_go_usage._fetch(), opener

    def _fetch_body(self, body):
        out, opener = self._fetch_with(body)
        return out

    def test_fetch_sends_explicit_user_agent(self):
        # Cloudflare bans the default Python-urllib UA with 403/1010 (measured
        # 2026-09-04) — the explicit User-Agent is load-bearing, not cosmetic.
        _out, opener = self._fetch_with(PROBED_BODY)
        headers = opener.call_args[0][0].headers
        ua = {k.lower(): v for k, v in headers.items()}.get("user-agent")
        self.assertTrue(ua and ua != "Python-urllib/3.12",
                        "fetch must send an explicit User-Agent")

    def test_fetch_maps_probed_shape(self):
        from datetime import datetime, timezone
        out = self._fetch_body(PROBED_BODY)
        self.assertEqual(out["rl_5h"], 0)
        self.assertEqual(out["rs_5h"],
                         datetime(2026, 9, 4, 10, 48, 37, 338000,
                                  tzinfo=timezone.utc).timestamp())
        labels = [w[0] for w in out["rl_windows"]]
        self.assertEqual(labels, ["5h", "wk", "mo"])
        pcts = {w[0]: w[1] for w in out["rl_windows"]}
        self.assertEqual(pcts, {"5h": 0, "wk": 2, "mo": 1})
        self.assertTrue(all(w[2] is not None for w in out["rl_windows"]))
        # weekly is a Mon 00:00 window — it must NOT claim the rolling 7d slot
        self.assertIsNone(out["rl_7d"])
        self.assertIsNone(out["rs_7d"])
        self.assertEqual(out["rl_ms"], [])

    def test_fetch_empty_or_bad_body_is_none(self):
        self.assertIsNone(self._fetch_body({}))
        self.assertIsNone(self._fetch_body({"usage": {}}))
        self.assertIsNone(self._fetch_body("not-a-dict"))

    def test_fetch_without_key_is_none_and_never_hits_network(self):
        opener = mock.MagicMock()
        with mock.patch.object(zen_go_usage, "_token", return_value=None), \
             mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(zen_go_usage._fetch())
        opener.assert_not_called()

    def test_fetch_transport_failure_is_none(self):
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertIsNone(zen_go_usage._fetch())

    def test_fetch_auth_rejection_is_error_marker_not_none(self):
        # Only a typed rejection is definitive. A generic 403 remains unknown.
        import urllib.error
        for code in (401, 403):
            with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
                 mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.HTTPError(
                                "url", code, "Forbidden", None, None)):  # noqa: SIM115
                out = zen_go_usage._fetch()
                if code == 401:
                    self.assertEqual(("auth", "authentication-rejected"),
                                     (out["error"], out["reason"]))
                else:
                    self.assertIsNone(out)
        entitlement = {"error": {"type": "EntitlementError",
                                 "message": "OpenCode Go subscription required."}}
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "url", 403, "Forbidden", None,
                            __import__("io").BytesIO(json.dumps(entitlement).encode()))):
            out = zen_go_usage._fetch()
            self.assertEqual(("auth", "subscription-missing"),
                             (out["error"], out["reason"]))
        # other HTTP failures stay transient
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "url", 500, "Server Error", None, None)):  # noqa: SIM115
            self.assertIsNone(zen_go_usage._fetch())

    def test_confirmed_auth_error_surfaces_immediately(self):
        now0 = time.time()
        good = {"rl_5h": 3, "rl_7d": None, "rl_ms": [], "rs_5h": 1.0, "rs_7d": None,
                "rl_windows": [["5h", 3, 1.0]]}
        auth = {"error": "auth", "detail": 401, "reason": "credential-expired"}
        with mock.patch.object(zen_go_usage, "_fetch", return_value=good):
            self.assertEqual(zen_go_usage.account_usage(), good)
        with mock.patch.object(zen_go_usage, "_fetch", return_value=auth):
            zen_go_usage._cache["ts"] = now0 - zen_go_usage._TTL - 1
            result = zen_go_usage.account_usage()
            self.assertEqual(result["error"], "auth")
            self.assertEqual(result["reason"], "credential-expired")
            self.assertEqual(result["rl_windows"], good["rl_windows"])


class ZenGoV2CredentialPrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "opencode.db"
        self.data = root / "opencode-data"
        self.data.mkdir()
        self.legacy_auth = self.data / "auth.json"
        self.usage_state = root / "usage-state"
        self.dispatch = root / "dispatch"
        self.dispatch.mkdir()
        self.jobs = self.dispatch / "jobs.log"
        self.jobs.touch()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, "
                "value TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)")
        environment = mock.patch.dict(os.environ, {
            "OPENCODE_RUNTIME_VERSION": "2.0.32",
            "OPENCODE_DB": str(self.db),
            "OPENCODE_DATA_DIR": str(self.data),
            "OPENCODE_AUTH": str(self.legacy_auth),
            "FLEET_USAGE_STATE_DIR": str(self.usage_state),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_RESOURCE_RUN_INDEX": str(
                self.dispatch / "resource-runs.index.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        cache_patcher = mock.patch.object(
            zen_go_usage, "_cache", {"ts": 0.0, "ok_ts": 0.0, "data": None, "scope": None})
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

    def add_credential(self, identifier, integration, value, active, created):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                (identifier, integration, identifier, json.dumps(value), active, created, created))

    def activate_opencode(self, identifier):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE credential SET active=CASE WHEN id=? THEN 1 ELSE 0 END "
                "WHERE integration_id='opencode'", (identifier,))

    def test_present_store_query_failure_blocks_stale_native_and_legacy_credentials(self):
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "selected-token"}, 1, 1)
        self.add_credential("old-go-key", "opencode-go", {
            "type": "key", "key": "stale-native-go-key"}, 1, 2)
        self.legacy_auth.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "stale-legacy-go-key"}}))
        runtime = zen_go_usage._runtime()
        real_connect = sqlite3.connect

        class FailSelectedQuery:
            def __init__(self, connection):
                object.__setattr__(self, "connection", connection)

            def __getattr__(self, key):
                return getattr(self.connection, key)

            def __setattr__(self, key, value):
                setattr(self.connection, key, value)

            def execute(self, sql, *args):
                if ("FROM credential WHERE" in sql and args
                        and args[0][0] == "opencode"):
                    raise sqlite3.OperationalError("database is locked")
                return self.connection.execute(sql, *args)

        cached = {"rl_5h": 17, "rl_windows": [["5h", 17, 123.0]]}
        zen_go_usage._cache.update(ts=0.0, ok_ts=time.time(), data=cached,
                                   scope="account:previous-known-account")
        with mock.patch.object(
                runtime.sqlite3, "connect",
                side_effect=lambda *args, **kwargs:
                FailSelectedQuery(real_connect(*args, **kwargs))), \
             mock.patch.object(zen_go_usage, "_request_usage") as request, \
             mock.patch.object(zen_go_usage, "_record_status") as record:
            selected = runtime.selected_go_usage_observation()
            result = zen_go_usage.account_usage()

        self.assertEqual(selected.state, "unavailable")
        self.assertEqual(selected.reason, "credential-store-read-failed")
        self.assertEqual((result["error"], result["reason"]),
                         ("unknown", "credential-store-read-failed"))
        request.assert_not_called()
        record.assert_not_called()
        self.assertIs(zen_go_usage._cache["data"], cached)

    def test_success_stays_on_selected_account_when_native_account_switches_in_flight(self):
        self.add_credential("account-a", "opencode", {
            "type": "oauth", "access": "account-a-token"}, 1, 1)
        self.add_credential("account-b", "opencode", {
            "type": "oauth", "access": "account-b-token"}, 0, 2)
        runtime = zen_go_usage._runtime()
        scope_a = runtime.selected_go_usage_observation().provider_scope
        switched = []

        def switch_then_success(token):
            if not switched:
                self.assertEqual(token, "account-a-token")
                self.activate_opencode("account-b")
                switched.append(True)
            else:
                self.assertEqual(token, "account-b-token")
            return PROBED_BODY, None

        with mock.patch.object(zen_go_usage, "_request_usage",
                               side_effect=switch_then_success) as request:
            first = zen_go_usage.account_usage()
            self.assertEqual(first["account_scope"], scope_a)
            self.assertEqual(first["provider_availability"]["opencode-go"]["account_scope"],
                             scope_a)
            self.assertEqual((first["credential_integration_id"],
                              first["credential_source"], first["credential_type"]),
                             ("opencode", "opencode-sqlite", "oauth"))
            self.assertEqual(zen_go_usage._cache["scope"], scope_a)
            self.assertEqual(runtime.model_availability(
                "opencode-go/deepseek-v4.1-flash")["state"], "unknown")

            second = zen_go_usage.account_usage()

        scope_b = runtime.selected_go_usage_observation().provider_scope
        self.assertNotEqual(scope_a, scope_b)
        self.assertEqual(request.call_args_list,
                         [mock.call("account-a-token"), mock.call("account-b-token")])
        self.assertEqual(second["account_scope"], scope_b)
        self.assertEqual(zen_go_usage._cache["scope"], scope_b)
        self.assertEqual(runtime.model_availability(
            "opencode-go/deepseek-v4.1-flash")["state"], "usable")

    def test_entitlement_denial_stays_on_a_and_fleet_hides_its_quota_after_switch(self):
        self.add_credential("account-a", "opencode", {
            "type": "oauth", "access": "account-a-token"}, 1, 1)
        self.add_credential("account-b", "opencode", {
            "type": "oauth", "access": "account-b-token"}, 0, 2)
        runtime = zen_go_usage._runtime()
        selection_a = runtime.selected_go_usage_observation()
        scope_a = selection_a.provider_scope
        denial = (403, json.dumps({"error": {"type": "EntitlementError",
                                              "message": "Go subscription required"}}).encode())
        previous = {"rl_5h": 19, "rl_7d": None, "rl_ms": [], "rs_5h": 50.0,
                    "rs_7d": None, "rl_windows": [["5h", 19, 50.0]],
                    "account_scope": scope_a}
        zen_go_usage._cache.update(ts=0.0, ok_ts=time.time(), data=previous, scope=scope_a)

        switched = []

        def switch_then_deny(token):
            if not switched:
                self.assertEqual(token, "account-a-token")
                self.activate_opencode("account-b")
                switched.append(True)
                return None, denial
            self.assertEqual(token, "account-b-token")
            return PROBED_BODY, None

        with mock.patch.object(zen_go_usage, "_request_usage",
                               side_effect=switch_then_deny) as request:
            first = zen_go_usage.account_usage()
            self.assertEqual(first["error"], "subscription-missing")
            self.assertEqual(first["account_scope"], scope_a)
            self.assertEqual(first["rl_windows"], previous["rl_windows"])
            self.assertEqual(first["provider_availability"]["opencode-go"]["account_scope"],
                             scope_a)
            self.assertEqual(runtime.model_availability(
                "opencode-go/deepseek-v4.1-flash")["state"], "unknown")

            empty_jobs = Path(self.temp.name) / "jobs.log"
            empty_jobs.touch()
            prior_cache_payload = dict(first)
            prior_cache_payload.pop("account_scope")
            snapshot = {"payload": prior_cache_payload, "freshness": "fresh",
                        "observed_at": time.time()}
            with mock.patch.object(collectors.procscan, "scan", return_value=[]), \
                 mock.patch.object(dispatch, "collect", return_value=[]), \
                 mock.patch.object(usage_cache, "account_usage", return_value=snapshot):
                collectors.collect_all(harness_filter={"opencode"},
                                       jobs_path=str(empty_jobs))
            fleet_snapshot = collectors.collect_all.last_usage_snapshots["opencode"]
            fleet_payload = fleet_snapshot["payload"]
            self.assertNotIn("rl_windows", fleet_payload)
            self.assertNotIn("opencode-go", fleet_payload["provider_availability"])
            self.assertNotIn("opencode-go-entitlement", fleet_payload["provider_availability"])
            self.assertNotIn(scope_a, {
                row.get("account_scope") for row in fleet_snapshot["providers"]})

            second = zen_go_usage.account_usage()

        scope_b = runtime.selected_go_usage_observation().provider_scope
        self.assertNotEqual(scope_a, scope_b)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1], mock.call("account-b-token"))
        self.assertEqual(second["account_scope"], scope_b)
        self.assertEqual(runtime.model_availability(
            "opencode-go/deepseek-v4.1-flash")["state"], "usable")

    def test_active_default_oauth_beats_inactive_personal_and_expired_api_key(self):
        runtime = zen_go_usage._runtime()
        self.add_credential("personal", "opencode", {
            "type": "oauth", "access": "inactive-personal-token"}, 0, 1)
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "active-default-token"}, 1, 2)
        self.add_credential("old-go-key", "opencode-go", {
            "type": "key", "key": "expired-go-key"}, 1, 3)
        stale_key_scope = "account:expired-go-key-from-older-selection"
        usage_cache.record_provider("opencode", "opencode-go", "*", stale_key_scope,
                                    "unavailable", reason="credential-expired")
        with mock.patch.object(zen_go_usage, "_request_usage",
                               return_value=(PROBED_BODY, None)) as request:
            result = zen_go_usage._fetch_v2()
        self.assertEqual(request.call_args_list, [mock.call("active-default-token")])
        self.assertEqual(result["rl_5h"], 0)
        self.assertEqual(result["provider_availability"]["opencode-go"]["state"], "usable")
        selected_scope = runtime.provider_account_scope("opencode-go")
        self.assertNotEqual(selected_scope, stale_key_scope)
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "usable")

    def test_valid_non_oauth_opencode_row_allows_go_key_tier(self):
        self.add_credential("opencode-api", "opencode", {
            "type": "api", "key": "opencode-personal-api-key"}, 1, 1)
        self.add_credential("go-key", "opencode-go", {
            "type": "key", "key": "selected-go-api-key"}, 1, 2)
        self.legacy_auth.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "legacy-go-key"}}))
        runtime = zen_go_usage._runtime()
        selected = runtime.selected_go_usage_credential()
        self.assertEqual(selected["integration_id"], "opencode-go")
        self.assertEqual(runtime.provider_account_scope("opencode-go"),
                         runtime._scope_for_row("opencode-go", selected["credential"]))
        old_oauth_scope = runtime.account_scope("opencode")
        usage_cache.record_provider("opencode", "opencode-go", "*", old_oauth_scope,
                                    "unavailable", reason="subscription-missing")
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "unknown")
        with mock.patch.object(zen_go_usage, "_request_usage",
                               return_value=(PROBED_BODY, None)) as request:
            result = zen_go_usage._fetch_v2()
        self.assertEqual(request.call_args_list, [mock.call("selected-go-api-key")])
        self.assertNotIn("opencode-go-entitlement", result["provider_availability"])
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "usable")

    def test_active_oauth_rejection_does_not_fall_back_to_api_key_or_legacy(self):
        self.add_credential("personal", "opencode", {
            "type": "oauth", "access": "inactive-personal-token"}, 0, 1)
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "expired-active-token"}, 1, 2)
        self.add_credential("old-go-key", "opencode-go", {
            "type": "key", "key": "expired-go-key"}, 1, 3)
        runtime = zen_go_usage._runtime()
        stale_key_scope = runtime._scope_for_row("opencode-go", runtime.read_credential("opencode-go"))
        usage_cache.record_provider("opencode", "opencode-go", "*", stale_key_scope,
                                    "usable", now=time.time(), source="old-go-key-success")
        self.legacy_auth.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "legacy-go-key"}}))
        rejection = (401, json.dumps({"error": {"code": "api_key_expired"}}).encode())
        with mock.patch.object(zen_go_usage, "_request_usage", return_value=(None, rejection)) as request:
            result = zen_go_usage._fetch_v2()
        self.assertEqual(request.call_args_list, [mock.call("expired-active-token")])
        self.assertEqual((result["error"], result["reason"]), ("auth", "credential-expired"))
        self.assertEqual(runtime.provider_account_scope("opencode-go"),
                         runtime.account_scope("opencode"))
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["reason"],
                         "credential-expired")

    def test_present_malformed_oauth_is_unknown_and_does_not_fall_back(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("broken-oauth", "opencode", "Default", "not-json", 1, 2, 2))
        self.add_credential("old-go-key", "opencode-go", {
            "type": "key", "key": "expired-go-key"}, 1, 3)
        with mock.patch.object(zen_go_usage, "_request_usage") as request:
            result = zen_go_usage._fetch_v2()
        request.assert_not_called()
        self.assertEqual((result["error"], result["reason"]), ("unknown", "credential-invalid"))
        self.assertEqual(zen_go_usage._runtime().provider_account_scope("opencode-go"),
                         zen_go_usage._runtime().account_scope("opencode"))

    def test_absent_oauth_uses_native_key_then_legacy_file(self):
        self.add_credential("go-key", "opencode-go", {
            "type": "key", "key": "native-go-key"}, 1, 1)
        self.legacy_auth.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "legacy-go-key"}}))
        with mock.patch.object(zen_go_usage, "_request_usage",
                               return_value=(PROBED_BODY, None)) as request:
            self.assertIn("rl_5h", zen_go_usage._fetch_v2())
        self.assertEqual(request.call_args_list, [mock.call("native-go-key")])
        runtime = zen_go_usage._runtime()
        native_scope = runtime.provider_account_scope("opencode-go")
        self.assertEqual(native_scope,
                         runtime._scope_for_row("opencode-go", runtime.read_credential("opencode-go")))
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM credential")
        with mock.patch.object(zen_go_usage, "_request_usage",
                               return_value=(PROBED_BODY, None)) as request:
            self.assertIn("rl_5h", zen_go_usage._fetch_v2())
        self.assertEqual(request.call_args_list, [mock.call("legacy-go-key")])
        self.assertEqual(runtime.provider_account_scope("opencode-go"),
                         runtime._scope_for_row("opencode-go", runtime.read_legacy_credential(
                             "opencode-go", auth_path=self.legacy_auth)))

    def test_oauth_entitlement_flows_through_fleet_filter_render_and_model_eligibility(self):
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "active-oauth-token"}, 1, 1)
        self.add_credential("go-key", "opencode-go", {
            "type": "key", "key": "stale-go-key"}, 1, 2)
        self.legacy_auth.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": "legacy-go-key"}}))
        runtime = zen_go_usage._runtime()
        entitlement_scope = runtime.account_scope("opencode")
        provider_scope = runtime.provider_account_scope("opencode-go")
        self.assertEqual(entitlement_scope, runtime._scope_for_row(
            "opencode", runtime.read_credential("opencode")))
        self.assertEqual(provider_scope, entitlement_scope)
        stale = {"state": "unavailable", "reason": "credential-expired",
                 "account_scope": "account:previous-selection", "observed_at": time.time() + 1}
        denial = (403, json.dumps({"error": {"type": "EntitlementError",
                                              "message": "Go subscription required"}}).encode())
        with mock.patch.object(zen_go_usage, "_request_usage", return_value=(None, denial)) as request:
            payload = zen_go_usage._fetch_v2()
        self.assertEqual(request.call_args_list, [mock.call("active-oauth-token")])
        go_row = payload["provider_availability"]["opencode-go"]
        entitlement_row = payload["provider_availability"]["opencode-go-entitlement"]
        self.assertEqual(go_row["account_scope"], provider_scope)
        self.assertEqual(entitlement_row["account_scope"], entitlement_scope)
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["reason"],
                         "subscription-missing")
        usage_cache.record_provider("opencode", "opencode-go-entitlement", "*",
                                    stale["account_scope"], "unavailable",
                                    reason=stale["reason"], now=stale["observed_at"])
        fleet_payload = {**payload, "provider_availability": {
            **payload["provider_availability"],
            "openai": {"state": "unavailable", "reason": "authentication-rejected",
                       "account_scope": stale["account_scope"]},
        }}
        snapshot = {"payload": fleet_payload, "freshness": "fresh", "observed_at": time.time()}
        empty_jobs = Path(self.temp.name) / "jobs.log"
        empty_jobs.touch()
        with mock.patch.object(collectors.procscan, "scan", return_value=[]), \
             mock.patch.object(dispatch, "collect", return_value=[]), \
             mock.patch.object(usage_cache, "account_usage", return_value=snapshot):
            collectors.collect_all(harness_filter={"opencode"}, jobs_path=str(empty_jobs))
        filtered = collectors.collect_all.last_usage_snapshots["opencode"]
        current_payload = filtered["payload"]["provider_availability"]
        self.assertNotIn("account_scope", current_payload["opencode-go-entitlement"])
        self.assertNotIn("account_scope", filtered["payload"])
        self.assertNotIn("openai", current_payload)
        self.assertNotIn("account:previous-selection",
                         {row.get("account_scope") for row in filtered["providers"]})
        from fleet import render
        from fleet.model import Session
        session = Session(harness="opencode", pid=1, cwd="/x", liveness="idle", mtime=1000)
        rendered = render._usage_header_rows([session], layout="wide",
                                              usage_snapshots={"opencode": filtered})
        text = "\n".join("".join(part for part, _style in row) for row in rendered)
        self.assertIn("Go usage unavailable · Go subscription missing", text)
        for detail in ("entitlement", "Zen", "OpenAI"):
            self.assertNotIn(detail, text)

    def test_active_account_change_does_not_inherit_old_entitlement_denial(self):
        runtime = zen_go_usage._runtime()
        self.add_credential("personal", "opencode", {
            "type": "oauth", "access": "personal-token"}, 0, 1)
        self.add_credential("default", "opencode", {
            "type": "oauth", "access": "default-token"}, 1, 2)
        old_scope = runtime.provider_account_scope("opencode-go")
        usage_cache.record_provider("opencode", "opencode-go", "*", old_scope,
                                    "unavailable", reason="subscription-missing")
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "unavailable")
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE credential SET active=1 WHERE id='personal'")
            connection.execute("UPDATE credential SET active=0 WHERE id='default'")
        new_scope = runtime.provider_account_scope("opencode-go")
        self.assertNotEqual(old_scope, new_scope)
        self.assertEqual(runtime.model_availability("opencode-go/deepseek-v4.1-flash")["state"],
                         "unknown")
        zen_go_usage._cache.update(ts=time.time(), ok_ts=time.time(), data={
            "rl_5h": 1, "provider_availability": {"opencode-go": {
                "state": "unavailable", "reason": "subscription-missing",
                "account_scope": old_scope}}}, scope=old_scope)
        recovered = {"rl_5h": 0, "provider_availability": {"opencode-go": {
            "state": "usable", "reason": None, "account_scope": new_scope}}}
        with mock.patch.object(zen_go_usage, "_fetch_v2", return_value=recovered) as fetch:
            self.assertEqual(zen_go_usage.account_usage(), recovered)
        fetch.assert_called_once()


class ZenGoAccountUsageTest(OpenCodeV1Test):
    def setUp(self):
        super().setUp()
        self.cache = {"ts": 0.0, "ok_ts": 0.0, "data": None,
                      "scope": zen_go_usage._usage_account_scope()}
        self.saved = dict(zen_go_usage._cache)
        zen_go_usage._cache.clear()
        zen_go_usage._cache.update(self.cache)
        self.addCleanup(lambda: (zen_go_usage._cache.clear(),
                                 zen_go_usage._cache.update(self.saved)))

    def test_failure_keeps_stale_then_drops_after_stale_max(self):
        now0 = time.time()
        good = {"rl_5h": 3, "rl_7d": None, "rl_ms": [], "rs_5h": 1.0, "rs_7d": None,
                "rl_windows": [["5h", 3, 1.0]]}
        with mock.patch.object(zen_go_usage, "_fetch", return_value=good):
            self.assertEqual(zen_go_usage.account_usage(), good)
        with mock.patch.object(zen_go_usage, "_fetch", return_value=None) as fetch:
            still = zen_go_usage.account_usage()
            self.assertEqual(still, good)
            fetch.assert_not_called()          # inside the TTL the endpoint is not re-hit
            # beyond the TTL a failure keeps the last-good payload...
            zen_go_usage._cache["ts"] = now0 - zen_go_usage._TTL - 1
            self.assertEqual(zen_go_usage.account_usage(), good)
            # ...until the staleness ceiling (keyed on the last-good anchor) passes.
            zen_go_usage._cache["ts"] = now0 - zen_go_usage._TTL - 1
            zen_go_usage._cache["ok_ts"] = now0 - zen_go_usage._STALE_MAX - 1
            self.assertIsNone(zen_go_usage.account_usage())


class ZenGoCacheIntegrationTest(unittest.TestCase):
    """usage_cache wires "opencode" like claude/codex: fetcher registered, refresh works,
    fresh window 180s."""

    def setUp(self):
        import tempfile as _tf
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FLEET_USAGE_STATE_DIR"] = self.tmp.name
        self.old_fetchers = dict(usage_cache.FETCHERS)
        for thread in list(usage_cache._THREADS.values()):
            thread.join(timeout=5)
        usage_cache._THREADS.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for thread in list(usage_cache._THREADS.values()):
            thread.join(timeout=5)
        usage_cache._THREADS.clear()
        usage_cache.FETCHERS.clear()
        usage_cache.FETCHERS.update(self.old_fetchers)
        self.tmp.cleanup()

    def test_request_refresh_opencode_writes_cache(self):
        calls = []
        usage_cache.FETCHERS["opencode"] = lambda: (
            calls.append(1) or {"rl_5h": 0, "rl_windows": [["5h", 0, 1.0]]})
        self.assertTrue(usage_cache.request_refresh("opencode"))
        thread = usage_cache._THREADS.get("opencode")
        self.assertIsNotNone(thread)
        thread.join(timeout=5)
        self.assertEqual(usage_cache.read("opencode")["payload"]["rl_5h"], 0)

    def test_read_freshness_window_opencode_is_180s(self):
        usage_cache._write("opencode", {"rl_5h": 1}, 1000.0, 1000.0)
        self.assertEqual(usage_cache.read("opencode", now=1000 + 180)["freshness"], "fresh")
        self.assertEqual(usage_cache.read("opencode", now=1000 + 181)["freshness"], "stale")


class ZenGoRenderHeaderTest(unittest.TestCase):
    """OpenCode's default usage header is limited to Go quota or one short status."""

    @staticmethod
    def _rows_text(rows):
        return "\n".join("".join(t for t, _k in row) for row in rows)

    def test_provider_diagnostics_stay_out_of_default_header(self):
        from fleet import render
        from fleet.model import Session
        oc = Session(harness="opencode", pid=1, cwd="/x", liveness="idle", mtime=1000)
        snapshots = {"opencode": {
            "payload": {"error": "auth", "detail": 401, "reason": "credential-expired"},
            "providers": [
                {"provider": "opencode-go", "model": "*", "state": "unavailable",
                 "reason": "credential-expired", "observed_at": 1000},
                {"provider": "opencode-go-entitlement", "model": "*", "state": "unavailable",
                 "reason": "subscription-missing", "observed_at": 1000},
                {"provider": "opencode", "model": "beam-free", "state": "usable",
                 "reason": None, "observed_at": 1000},
            ], "freshness": "fresh", "observed_at": 1000}}
        rows = render._usage_header_rows([oc], layout="wide", usage_snapshots=snapshots)
        text = self._rows_text(rows)
        self.assertIn("Go usage unavailable", text)
        for detail in ("credential", "entitlement", "Zen", "OpenAI", "subscription"):
            self.assertNotIn(detail, text)

    def test_error_marker_only_cache_renders_reason_without_sessions(self):
        from fleet import render
        snapshots = {"opencode": {"payload": None, "freshness": "unknown",
                                  "observed_at": None,
                                  "error_observation": {"error": "missing-credential",
                                                        "reason": "credential-not-found",
                                                        "observed_at": 1000}}}
        rows = render._usage_header_rows([], usage_snapshots=snapshots, now=1000)
        text = self._rows_text(rows)
        self.assertIn("opencode", text)
        self.assertEqual(text.count("Go usage unavailable"), 1)
        self.assertIn("Go credential not found", text)
        self.assertNotIn("%", text)

    def test_session_error_marker_renders_short_unavailable_status(self):
        from fleet import render
        from fleet.model import Session
        oc = Session(harness="opencode", pid=1, cwd="/x", liveness="idle", mtime=1000)
        oc._usage_error = "auth"
        rows = render._usage_header_rows([oc], layout="wide")
        self.assertIn("Go usage unavailable", self._rows_text(rows))
        self.assertNotIn("credential", self._rows_text(rows))

    def test_healthy_snapshot_still_renders_gauges(self):
        from fleet import render
        from fleet.model import Session
        oc = Session(harness="opencode", pid=1, cwd="/x", liveness="idle", mtime=1000)
        snapshots = {"opencode": {
            "payload": {"rl_5h": 58, "rl_windows": [["5h", 58, 1.0], ["wk", 25, 2.0]]},
            "freshness": "fresh", "observed_at": 1000}}
        rows = render._usage_header_rows([oc], layout="wide", usage_snapshots=snapshots)
        text = self._rows_text(rows)
        self.assertNotIn("usage api —", text)
        self.assertNotIn("credential", text)
        self.assertNotIn("entitlement", text)
        self.assertNotIn("Zen", text)
        self.assertNotIn("OpenAI", text)
        self.assertIn("58%", text)
        self.assertIn("25%", text)

    def test_go_header_limits_bars_to_5h_week_month_and_keeps_spacing(self):
        from fleet import render
        from fleet.model import Session
        claude = Session(harness="claude", pid=1, cwd="/x", liveness="idle", mtime=1000)
        opencode = Session(harness="opencode", pid=2, cwd="/y", liveness="idle", mtime=1000)
        snapshots = {
            "claude": {"payload": {"rl_5h": 0, "rl_7d": None, "rl_ms": []},
                       "freshness": "fresh", "observed_at": 1000},
            "opencode": {"payload": {"rl_windows": [
                ["5h", 20, 2000], ["week", 30, 3000], ["month", 40, 4000],
                ["year", 90, 5000]], "rl_ms": [["provider-detail", 99]]},
                "providers": [{"provider": "opencode-go", "state": "usable",
                               "reason": None, "observed_at": 1000}],
                "freshness": "fresh", "observed_at": 1000},
        }
        rows = render._usage_header_rows([claude, opencode], usage_snapshots=snapshots,
                                         now=1000, term_width=168)
        text_rows = self._rows_text(rows).splitlines()
        claude_row = next(line for line in text_rows if "claude code" in line)
        opencode_row = next(line for line in text_rows if "opencode go" in line)
        self.assertIn("5h [" + render._BAR_EMPTY * render._GAUGE_W + "   0%]", claude_row)
        self.assertIn("5h", opencode_row)
        self.assertIn("week", opencode_row)
        self.assertIn("month", opencode_row)
        self.assertIn("   week [", opencode_row)
        self.assertIn("   month [", opencode_row)
        self.assertNotIn("year", opencode_row)
        self.assertNotIn("provider-detail", opencode_row)
        joined = "\n".join(text_rows)
        self.assertNotIn("unknown5h", "".join(text_rows).lower())
        self.assertNotIn("credential", joined)
        self.assertNotIn("entitlement", joined)
        self.assertNotIn("Zen", joined)
        self.assertNotIn("OpenAI", joined)
        self.assertEqual(snapshots["opencode"]["providers"][0]["state"], "usable")
        self.assertLessEqual(render._dw(opencode_row), 168)


if __name__ == "__main__":
    unittest.main()
