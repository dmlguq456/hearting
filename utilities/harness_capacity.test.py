#!/usr/bin/env python3
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).with_name("harness-capacity.py")
SPEC = importlib.util.spec_from_file_location("harness_capacity_under_test", MODULE)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


class CapacityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 35,
        }
        self.states = {name: "ok" for name in C.HARNESSES}
        self.counts = {name: 0 for name in C.HARNESSES}

    def test_capacity_reorders_only_primary_peers(self):
        chosen, band, _ranks, promoted = C.select(
            self.policy, self.states, self.counts, C.HARNESSES,
            {"claude": 30, "codex": 80, "opencode": 100},
        )
        self.assertEqual((chosen, band, promoted), ("codex", "primary", False))

    def test_relief_promotes_only_below_declared_threshold(self):
        chosen, band, _ranks, promoted = C.select(
            self.policy, self.states, self.counts, C.HARNESSES,
            {"claude": 20, "codex": 30, "opencode": 100},
        )
        self.assertEqual((chosen, band, promoted), ("opencode", "relief", True))

    def test_unknown_gauges_are_excluded_from_every_band(self):
        # Hardened after the 2026-08-10 incident: treating an absent/stale
        # gauge as a neutral 50 silently redirected owners onto a
        # user-exhausted harness. rank_band excludes unknown gauges from
        # eligibility entirely, so with every gauge unknown no band has a
        # candidate at all.
        chosen, band, _ranks, promoted = C.select(
            self.policy, self.states, self.counts, C.HARNESSES,
            {name: None for name in C.HARNESSES},
        )
        self.assertEqual((chosen, band, promoted), (None, None, False))

    def test_one_unknown_primary_gauge_still_blocks_relief_promotion(self):
        chosen, band, _ranks, promoted = C.select(
            self.policy, self.states, self.counts, C.HARNESSES,
            {"claude": 20, "codex": None, "opencode": None},
        )
        self.assertEqual((chosen, band, promoted), ("claude", "primary", False))

    def test_limit_marker_demotes_before_capacity(self):
        states = {**self.states, "claude": "limited(noon)", "codex": "limited(3pm)"}
        chosen, band, _ranks, promoted = C.select(
            self.policy, states, self.counts, C.HARNESSES,
            {"claude": 90, "codex": 90, "opencode": 90},
        )
        self.assertEqual((chosen, band), ("opencode", "relief"))

    def test_manual_bias_never_crosses_quality_band(self):
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": "opencode"}):
            chosen, band, _ranks, _promoted = C.select(
                self.policy, self.states, self.counts, C.HARNESSES,
                {name: 80 for name in C.HARNESSES},
            )
        self.assertEqual((chosen, band), ("claude", "primary"))

    def test_unknown_or_stale_gauge_is_excluded_from_eligibility(self):
        ranked = C.rank_band(
            list(C.HARNESSES), self.states, self.counts, C.HARNESSES,
            {"claude": 80, "codex": None, "opencode": 40},
        )
        self.assertNotIn("codex", ranked)
        self.assertEqual(set(ranked), {"claude", "opencode"})

    def test_ordering_score_is_neutral_for_unknown_and_never_a_gate(self):
        self.assertEqual(C.ordering_score({}, "opencode"), C.ORDERING_NEUTRAL_SCORE)
        self.assertEqual(C.ordering_score({}, "opencode"), 50.0)
        self.assertEqual(C.ordering_score({"opencode": 73}, "opencode"), 73.0)


class CodexGaugeReaderTests(unittest.TestCase):
    """The codex gauge must not starve on idle: live probe first, rollout second.

    A rollout-only reader self-reinforces (idle codex → stale gauge → unknown →
    never selected → still idle), which skewed capacity-aware placement to
    claude on 2026-08-13 while fleet's live probe showed real headroom."""

    def test_api_probe_precedes_rollout_gauge(self):
        with mock.patch.object(C, "_codex_api_score", return_value=24.0), \
                mock.patch.object(
                    C, "_codex_score",
                    side_effect=AssertionError("rollout must not be read when the probe answers")), \
                mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": ""}):
            self.assertEqual(C.capacity_scores(now=0.0)["codex"], 24.0)

    def test_zero_headroom_probe_answer_does_not_fall_through(self):
        with mock.patch.object(C, "_codex_api_score", return_value=0.0), \
                mock.patch.object(
                    C, "_codex_score",
                    side_effect=AssertionError("0.0 headroom is an answer, not a miss")), \
                mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": ""}):
            self.assertEqual(C.capacity_scores(now=0.0)["codex"], 0.0)

    def test_probe_failure_falls_back_to_rollout_gauge(self):
        with mock.patch.object(C, "_codex_api_score", return_value=None), \
                mock.patch.object(C, "_codex_score", return_value=61.0), \
                mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": ""}):
            self.assertEqual(C.capacity_scores(now=0.0)["codex"], 61.0)

    def test_manual_override_stays_offline(self):
        with mock.patch.object(
                C, "_codex_api_score",
                side_effect=AssertionError("manual override must not probe")), \
                mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": "codex:12"}):
            self.assertEqual(C.capacity_scores(now=0.0)["codex"], 12.0)

    def test_missing_auth_returns_none_before_any_network(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CODEX_HOME": home}), \
                mock.patch.object(
                    C.urllib.request, "urlopen",
                    side_effect=AssertionError("no network without auth.json")):
            self.assertIsNone(C._codex_api_score())

    def test_api_payload_parses_wham_rate_limit_windows(self):
        payload = {"rate_limit": {"primary_window": {"used_percent": 76.0},
                                  "secondary_window": {"used_percent": 40.0}}}
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "t", "account_id": "a"}}),
                encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": home}), \
                    mock.patch.object(
                        C.urllib.request, "urlopen",
                        return_value=io.BytesIO(json.dumps(payload).encode("utf-8"))):
                self.assertEqual(C._codex_api_score(), 24.0)


if __name__ == "__main__":
    unittest.main()
