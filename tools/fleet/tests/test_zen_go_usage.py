import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import usage_cache
from fleet.collectors import zen_go_usage


PROBED_BODY = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-04T10:48:37.338Z"},
        "weekly": {"status": "ok", "percent": 2, "resetsAt": "2026-09-07T00:00:00.338Z"},
        "monthly": {"status": "ok", "percent": 1, "resetsAt": "2026-09-28T04:51:28.338Z"},
    }
}


class ZenGoTokenTest(unittest.TestCase):
    def test_token_reads_opencode_go_key_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w") as fh:
                json.dump({"opencode-go": {"type": "api", "key": "sk-go-test"}}, fh)
            with mock.patch.object(zen_go_usage, "_auth_path", return_value=path):
                self.assertEqual(zen_go_usage._token(), "sk-go-test")

    def test_token_missing_entry_or_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with open(path, "w") as fh:
                json.dump({"opencode-go": {"type": "oauth"}}, fh)
            with mock.patch.object(zen_go_usage, "_auth_path", return_value=path):
                self.assertIsNone(zen_go_usage._token())
            with mock.patch.object(zen_go_usage, "_auth_path",
                                   return_value=os.path.join(tmp, "absent.json")):
                self.assertIsNone(zen_go_usage._token())


class ZenGoFetchTest(unittest.TestCase):
    def setUp(self):
        self.cache = {"ts": 0.0, "ok_ts": 0.0, "data": None}
        with mock.patch.object(zen_go_usage, "_cache", self.cache):
            pass

    def _fetch_with(self, body):
        opener = mock.MagicMock()
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(body).encode()
        opener.return_value = resp
        with mock.patch.object(zen_go_usage, "_token", return_value="sk-go-test"), \
             mock.patch("urllib.request.urlopen", opener):
            return zen_go_usage._fetch()

    def test_fetch_maps_probed_shape(self):
        from datetime import datetime, timezone
        out = self._fetch_with(PROBED_BODY)
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
        self.assertIsNone(self._fetch_with({}))
        self.assertIsNone(self._fetch_with({"usage": {}}))
        self.assertIsNone(self._fetch_with("not-a-dict"))

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


class ZenGoAccountUsageTest(unittest.TestCase):
    def setUp(self):
        self.cache = {"ts": 0.0, "ok_ts": 0.0, "data": None}
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


if __name__ == "__main__":
    unittest.main()
