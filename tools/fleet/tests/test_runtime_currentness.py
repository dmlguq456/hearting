#!/usr/bin/env python3
"""Runtime-currentness regression tests (2026-07-13).

Codex wham/usage no longer guarantees that `primary_window` means 5h. Fleet must label
windows from `limit_window_seconds` when present and keep the old 5h/7d mapping only as
legacy fallback.
"""
import json
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render  # noqa: E402
from fleet.collectors import codex  # noqa: E402
from fleet.model import Session  # noqa: E402


class CodexDynamicWindowTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.dict(codex._ACCT, {"ts": 0.0, "data": None})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_api_and_rollout_preserve_credits_from_their_own_source(self):
        credits = {"has_credits": True, "unlimited": False, "balance": "56446.1701575000"}
        window = {"used_percent": 100, "window_minutes": 10080, "reset_at": 9000}
        metadata = {"credits": credits, "spend_control_reached": None}
        api = {"rate_limit": {"primary_window": window}, **metadata}
        with mock.patch.object(codex, "_home", return_value="/no-home"), \
                mock.patch("builtins.open", mock.mock_open(read_data='{"tokens":{"access_token":"tok"}}')), \
                mock.patch.object(codex.urllib.request, "urlopen",
                                  return_value=io.BytesIO(json.dumps(api).encode())):
            live, reason = codex._api_usage_detail()
        self.assertEqual(reason, "live")
        line = json.dumps({"payload": {"rate_limits": {"primary": window, **metadata}}})
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(codex, "_home", return_value=home), \
                mock.patch.object(codex, "_api_usage", return_value=None), \
                mock.patch.object(codex, "_tail_token_count", return_value=line), \
                mock.patch.object(codex.time, "time", return_value=1000):
            os.makedirs(os.path.join(home, "sessions"))
            with open(os.path.join(home, "sessions", "rollout.jsonl"), "w"):
                pass
            rollout = codex.account_usage()
        for data in (live, rollout):
            self.assertEqual(data["windows"], [["7d", 100, 9000.0]])
            self.assertIsNone(data["rl_5h"])
            self.assertEqual(data["rl_7d"], 100)
            self.assertEqual({k: data[k] for k in metadata}, metadata)
            self.assertTrue(codex.credits_usable(data))

    def test_api_priority_ttl_and_new_success_do_not_borrow_old_credits(self):
        first = {"windows": [["7d", 100, 9000]], "credits": {"has_credits": True}}
        new = {"windows": [["7d", 100, 10000]]}
        with mock.patch.object(codex, "_api_usage", side_effect=[first, new]) as api, \
                mock.patch.object(codex.os, "walk", side_effect=AssertionError("API precedes rollout")), \
                mock.patch.object(codex.time, "time", side_effect=[1000, 1060, 1061]):
            self.assertEqual(codex.account_usage(), first)
            self.assertEqual(codex.account_usage(), first)
            self.assertEqual(codex.account_usage(), new)
        self.assertEqual(api.call_count, 2)
        self.assertNotIn("credits", codex._ACCT["data"])

    def test_credit_predicate_requires_boolean_flags_and_no_spend_hold(self):
        for credits in ({"has_credits": True}, {"unlimited": True},
                        {"has_credits": True, "balance": "0"}):
            for spend in ({}, {"spend_control_reached": None}, {"spend_control_reached": False}):
                with self.subTest(credits=credits, spend=spend):
                    self.assertTrue(codex.credits_usable({"credits": credits, **spend}))
        for payload in (None, [], {}, {"credits": None}, {"credits": []},
                        {"credits": {"has_credits": False, "unlimited": False}},
                        {"credits": {"has_credits": "true", "unlimited": 1}},
                        {"credits": {"has_credits": 1, "unlimited": "true"}},
                        {"credits": {"balance": "56446.1701575000"}}):
            with self.subTest(payload=payload):
                self.assertFalse(codex.credits_usable(payload))
        for spend in (True, "false", "true", 0, 1, [], {}):
            with self.subTest(spend=spend):
                self.assertFalse(codex.credits_usable(
                    {"credits": {"unlimited": True}, "spend_control_reached": spend}))

    def test_expired_rollout_keeps_three_tuple_and_zeroes_usage(self):
        with mock.patch.object(codex.time, "time", return_value=1000):
            result = codex._rates_from_payload({"rate_limits": {"primary": {
                "used_percent": 100, "window_minutes": 10080, "resets_at": 999}}})
        self.assertEqual(result, (None, 0, [["7d", 0, 999.0]]))

    def test_wham_primary_604800_labels_as_7d_not_5h(self):
        with mock.patch("fleet.collectors.codex.time.time", return_value=1000.0):
            with mock.patch("fleet.collectors.codex._home", return_value="/no-home"), \
                 mock.patch("builtins.open", mock.mock_open(read_data='{"tokens":{"access_token":"tok","account_id":"acct"}}')), \
                 mock.patch("fleet.collectors.codex.urllib.request.urlopen") as m_url:
                m_url.return_value.__enter__.return_value.read.return_value = b""
                # json.load consumes the response object directly; keep the mock simple by
                # patching json.load at the collector boundary.
                with mock.patch("fleet.collectors.codex.json.load", side_effect=[
                    {"tokens": {"access_token": "tok", "account_id": "acct"}},
                    {"rate_limit": {
                        "primary_window": {
                            "used_percent": 10,
                            "limit_window_seconds": 604800,
                            "reset_at": 2000,
                        },
                        "secondary_window": None,
                    }},
                ]):
                    data = codex._api_usage()

        self.assertEqual(data["windows"], [["7d", 10, 2000.0]])
        self.assertIsNone(data["rl_5h"])
        self.assertEqual(data["rl_7d"], 10)

    def test_legacy_payload_without_duration_preserves_primary_5h_secondary_7d(self):
        payload = {"rate_limits": {
            "primary": {"used_percent": 11},
            "secondary": {"used_percent": 22},
        }}
        p5, p7, windows = codex._rates_from_payload(payload)

        self.assertEqual((p5, p7), (11, 22))
        self.assertEqual(windows, [["5h", 11, None], ["7d", 22, None]])

    def test_unknown_duration_is_honest_duration_label(self):
        self.assertEqual(codex._duration_label(12345), "12345s")

    def test_rollout_fallback_keeps_nonlegacy_dynamic_window(self):
        payload = {"rate_limits": {
            "primary": {"used_percent": 7, "limit_window_seconds": 86400},
        }}
        line = json.dumps({"payload": payload})
        with tempfile.TemporaryDirectory() as home, \
             mock.patch("fleet.collectors.codex._home", return_value=home), \
             mock.patch("fleet.collectors.codex._api_usage", return_value=None), \
             mock.patch("fleet.collectors.codex._tail_token_count", return_value=line):
            sessions = os.path.join(home, "sessions")
            os.makedirs(sessions)
            path = os.path.join(sessions, "rollout.jsonl")
            with open(path, "w", encoding="utf-8"):
                pass
            codex._ACCT.update(ts=0.0, data=None)
            data = codex.account_usage()

        self.assertEqual(data["windows"], [["24h", 7, None]])
        self.assertIsNone(data["rl_5h"])
        self.assertIsNone(data["rl_7d"])


class RenderDynamicWindowTest(unittest.TestCase):

    def test_render_uses_dynamic_windows_over_legacy_slots(self):
        sess = Session(harness="codex", pid=1, cwd="/repo", slug="codex", liveness="idle",
                       rl_5h=99, rl_7d=None, rl_windows=[["7d", 10, None]])
        lines = render._build_lines([sess], [], section="fleet", narrow=False, malformed=0,
                                    layout="wide")
        text = "\n".join("".join(part for part, _key in line) for line in lines if line)

        self.assertIn("week", text)
        self.assertNotIn("7d", text)
        self.assertIn("10%", text)
        self.assertNotIn("5h", text)
        self.assertNotIn("99%", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
