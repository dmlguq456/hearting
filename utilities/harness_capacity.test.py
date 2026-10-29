#!/usr/bin/env python3
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


MODULE = Path(__file__).with_name("harness-capacity.py")
SPEC = importlib.util.spec_from_file_location("harness_capacity_under_test", MODULE)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


_STATE_TMP = None
_STATE_OLD = None


def setUpModule():
    # Every networked probe writes through to Fleet's shared usage cache; keep
    # test fixtures (fake wham payloads) out of the live ~/.local/state store.
    global _STATE_TMP, _STATE_OLD
    _STATE_TMP = tempfile.TemporaryDirectory()
    _STATE_OLD = os.environ.get("FLEET_USAGE_STATE_DIR")
    os.environ["FLEET_USAGE_STATE_DIR"] = _STATE_TMP.name


def tearDownModule():
    if _STATE_OLD is None:
        os.environ.pop("FLEET_USAGE_STATE_DIR", None)
    else:
        os.environ["FLEET_USAGE_STATE_DIR"] = _STATE_OLD
    _STATE_TMP.cleanup()


class OwnerPreferenceTests(unittest.TestCase):
    def choose(self, *, states=None, scores=None, policy=None, ordered=True):
        names = ["claude", "opencode", "codex"]
        return C.select(
            policy or {"primary": names, "relief": [], "last_resort": [],
                       "promote_relief_below": 0},
            states or {h: "ok" for h in names},
            {"claude": 20, "opencode": 9, "codex": 0}, names,
            scores or {h: 80 for h in names}, strategy="balanced",
            harness_weights={"claude": .5, "opencode": .3},
            preference_order=names if ordered else None)[0]

    def test_owner_order_and_hard_limit_fallback_ignore_worker_share_deficit(self):
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": ""}):
            self.assertEqual(self.choose(), "claude")
            self.assertEqual(self.choose(states={"claude": "limited", "opencode": "ok", "codex": "ok"}), "opencode")
            self.assertEqual(self.choose(states={"claude": "limited", "opencode": "limited", "codex": "ok"}), "codex")
            self.assertEqual(self.choose(ordered=False), "codex")

    def test_usage_gate_all_gated_recovery_and_unknown_keep_existing_meaning(self):
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": ""}):
            self.assertEqual(self.choose(scores={"claude": 5, "opencode": 80, "codex": 90}), "opencode")
            self.assertEqual(self.choose(scores={"claude": 5, "opencode": 6, "codex": 9}), "codex")
            self.assertEqual(self.choose(states={"claude": "limited", "opencode": "unknown", "codex": "ok"},
                                         scores={"claude": 0, "opencode": None, "codex": 90}), "opencode")

    def test_quality_band_and_explicit_bias_keep_precedence(self):
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": ""}):
            self.assertEqual(self.choose(states={"claude": "limited", "opencode": "ok", "codex": "ok"},
                policy={"primary": ["claude", "codex"], "relief": [],
                        "last_resort": ["opencode"], "promote_relief_below": 0}), "codex")
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": "codex"}):
            self.assertEqual(self.choose(), "codex")


class SharedCacheFallbackTests(unittest.TestCase):
    """2026-09-16: a codex backend 503 at compose time made the gauge `unknown`
    although Fleet had a true reading minutes earlier. The live probe now
    writes through to the shared cache and placement reads it back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"FLEET_USAGE_STATE_DIR": self.tmp.name,
                                                "HARNESS_CAPACITY_SCORES": ""})
        self.env.start()
        C._LAST_PROBE_REASON.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _cache(self):
        return C._usage_cache()

    def test_live_probe_writes_through_and_reports_live(self):
        payload = {"rate_limit": {"primary_window": {"used_percent": 76.0},
                                  "secondary_window": {"used_percent": 40.0}}}
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "t", "account_id": "a"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": home}), \
                    mock.patch.object(C.urllib.request, "urlopen",
                                      return_value=io.BytesIO(json.dumps(payload).encode("utf-8"))):
                score, source = C._codex_probe(now=1000.0, stale_after=3600)
        self.assertEqual((score, source), (24.0, "live"))
        snapshot = self._cache().read("codex")      # written at wall-clock time
        self.assertEqual(snapshot["freshness"], "fresh")
        self.assertEqual(snapshot["payload"]["windows"][0][1], 76)

    def test_probe_failure_reuses_cached_reading_with_age_and_reason(self):
        cache = self._cache()
        cache.record("codex", {"rl_5h": None, "rl_7d": 100, "windows": [["7d", 100, None]]}, now=700.0)
        with mock.patch.object(C, "_codex_api_score", return_value=None), \
                mock.patch.object(C, "_codex_score", side_effect=AssertionError("cache precedes rollouts")):
            C._LAST_PROBE_REASON["codex"] = "http-503"
            score, source = C._codex_probe(now=1000.0, stale_after=3600)
        self.assertEqual(score, 0.0)
        self.assertEqual(source, "cache:300s;live=http-503")

    def test_gate_release_is_the_last_reset_of_the_windows_at_the_gate(self):
        cache = self._cache()
        cache.record("codex", {"windows": [["5h", 40, 2000.0], ["7d", 90, 5000.0]]}, now=900.0)
        self.assertEqual(C.gate_release_epoch("codex", "live", usage_gate_used_percent=85, now=1000.0), 5000.0)
        self.assertEqual(C.gate_release_epoch("codex", "cache:100s;live=http-503",
                                              usage_gate_used_percent=30, now=1000.0), 5000.0)
        # A score not read from the cache, or a window at the gate without a future reset: unknown.
        self.assertIsNone(C.gate_release_epoch("codex", "rollout;live=timeout", usage_gate_used_percent=85, now=1000.0))
        self.assertIsNone(C.gate_release_epoch("codex", "manual", usage_gate_used_percent=85, now=1000.0))
        self.assertIsNone(C.gate_release_epoch("codex", "live", usage_gate_used_percent=85, now=6000.0))
        cache.record("codex", {"windows": [["7d", 90, None]]}, now=900.0)
        self.assertIsNone(C.gate_release_epoch("codex", "live", usage_gate_used_percent=85, now=1000.0))
        cache.record("opencode", {"rl_windows": [["wk", 88, 7000.0]]}, now=900.0)
        self.assertEqual(C.gate_release_epoch("opencode", "live", usage_gate_used_percent=85, now=1000.0), 7000.0)
        self.assertIsNone(C.gate_release_epoch("opencode", "live", usage_gate_used_percent=95, now=1000.0))

    def test_window_release_epoch_reads_both_harness_windows_at_any_gate(self):
        cache = self._cache()
        cache.record("codex", {"windows": [["5h", 100, 2000.0], ["7d", 90, 9000.0]]}, now=900.0)
        self.assertEqual(C.window_release_epoch("codex", usage_gate_used_percent=100, now=1000.0), 2000.0)
        self.assertEqual(C.window_release_epoch("codex", usage_gate_used_percent=90, now=1000.0), 9000.0)
        # A past reset at the gate and a cache older than the staleness bound are unknown.
        self.assertIsNone(C.window_release_epoch("codex", usage_gate_used_percent=100, now=2500.0))
        self.assertIsNone(C.window_release_epoch("codex", usage_gate_used_percent=100, now=5000.0))
        cache.record("opencode", {"rl_windows": [["wk", 100, 8000.0]]}, now=900.0)
        self.assertEqual(C.window_release_epoch("opencode", usage_gate_used_percent=100, now=1000.0), 8000.0)

    def test_gauge_limit_epoch_skips_manual_claude_and_unknown_answers(self):
        cache = self._cache()
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": ""}):
            cache.record("codex", {"windows": [["7d", 100, 9000.0]]}, now=900.0)
            self.assertEqual(C.gauge_limit_epoch("codex", now=1000.0), 9000.0)
            cache.record("codex", {"windows": [["7d", 99, 9000.0]]}, now=900.0)
            self.assertIsNone(C.gauge_limit_epoch("codex", now=1000.0))
            cache.record("codex", {"windows": [["7d", 100, 9000.0]]}, now=900.0)
            cache.record("claude", {"windows": [["7d", 100, 9000.0]]}, now=900.0)
            self.assertIsNone(C.gauge_limit_epoch("claude", now=1000.0))
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": "codex:0"}):
            self.assertIsNone(C.gauge_limit_epoch("codex", now=1000.0))

    def test_stale_cache_is_not_reused_beyond_stale_after(self):
        cache = self._cache()
        cache.record("codex", {"windows": [["7d", 10, None]]}, now=0.0)
        with mock.patch.object(C, "_codex_api_score", return_value=None), \
                mock.patch.object(C, "_codex_score", return_value=None):
            C._LAST_PROBE_REASON["codex"] = "timeout"
            score, source = C._codex_probe(now=5000.0, stale_after=3600)
        self.assertIsNone(score)
        self.assertEqual(source, "unknown:timeout")

    def test_codex_credit_matrix_lifts_only_hard_hold(self):
        cases = [({"has_credits": True}, None, True),
                 ({"has_credits": True, "balance": "0"}, False, True),
                 ({"has_credits": False, "unlimited": True}, None, True),
                 ({"has_credits": False, "unlimited": False}, None, False),
                 (None, None, False), ([], None, False),
                 ({"has_credits": "true", "unlimited": 1}, None, False),
                 ({"balance": "56446.1701575000"}, None, False),
                 ({"has_credits": True}, True, False),
                 ({"unlimited": True}, "false", False),
                 ({"has_credits": True}, 0, False)]
        cache = self._cache()
        for credits, spend, usable in cases:
            with self.subTest(credits=credits, spend=spend):
                cache.record("codex", {"windows": [["5h", 100, 2000], ["7d", 100, 9000]],
                                       "credits": credits, "spend_control_reached": spend}, now=900)
                self.assertEqual(C.gauge_limit_epoch("codex", now=1000), None if usable else 9000)
                score, source = C._cached_score("codex", 1000, 3600)
                self.assertEqual(score, 0.0)
                self.assertTrue(C.is_gated({"codex": score}, "codex", usage_gate_used_percent=85))
                self.assertEqual(C.gate_release_epoch("codex", source, now=1000,
                                                       usage_gate_used_percent=85), 9000)
                self.assertEqual(C.rank_band(["codex"], {"codex": "ok"}, {}, ["codex"],
                                             {"codex": score}, strategy="capacity-aware"), [])

    def test_credit_judgment_reads_one_fresh_snapshot(self):
        cache = self._cache()
        # A concurrent later refresh must not supply credits for older windows.
        first = {"payload": {"windows": [["7d", 100, 9000]]}}
        later = {"payload": {"credits": {"has_credits": True}}}
        with mock.patch.object(cache, "read", side_effect=[first, later]) as read:
            self.assertEqual(C.gauge_limit_epoch("codex", now=1000, stale_after=123), 9000)
        read.assert_called_once_with("codex", now=1000, stale_max=123)

    def test_credit_metadata_does_not_change_other_gauges_or_unknown_windows(self):
        cache = self._cache()
        metadata = {"credits": {"has_credits": True}}
        cache.record("opencode", {"rl_windows": [["wk", 100, 9000]], **metadata}, now=900)
        cache.record("claude", {"windows": [["7d", 100, 9000]], **metadata}, now=900)
        self.assertEqual(C.gauge_limit_epoch("opencode", now=1000), 9000)
        self.assertIsNone(C.gauge_limit_epoch("claude", now=1000))
        for rows in ([["7d", 99, 9000]], [["7d", 100, 999]],
                     [["7d", 100, None]], [["5h", 100, 9000], ["7d", 100, None]]):
            for credit in ({}, metadata):
                with self.subTest(rows=rows, credit=credit):
                    cache.record("codex", {"windows": rows, **credit}, now=900)
                    self.assertIsNone(C.gauge_limit_epoch("codex", now=1000))
        cache.record("codex", {"windows": [["7d", 100, 9000]], **metadata}, now=900)
        self.assertIsNone(C.gauge_limit_epoch("codex", now=5000))

    def test_manual_zero_and_positive_scores_skip_credit_and_window_reads(self):
        for score in (0, 42):
            with self.subTest(score=score), mock.patch.dict(os.environ, {
                    "HARNESS_CAPACITY_SCORES": f"codex:{score},claude:80,opencode:70",
                    "AGENT_DISPATCH_JOBS": ""}), \
                    mock.patch.object(C, "_usage_cache", side_effect=AssertionError("manual is offline")):
                self.assertIsNone(C.gauge_limit_epoch("codex", now=1000))
                self.assertEqual(C.capacity_report(now=1000)["scores"]["codex"], score)

    def test_capacity_report_names_manual_and_unknown_sources(self):
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": "codex:12",
                                         "AGENT_DISPATCH_JOBS": ""}), \
                mock.patch.object(C, "_opencode_api_score", return_value=None), \
                mock.patch.object(C, "_claude_capacity_reading", return_value=(None, None)):
            report = C.capacity_report(now=0.0)
            self.assertEqual(C.capacity_scores(now=0.0)["codex"], 12.0)
        self.assertEqual(report["sources"]["codex"], "manual")
        self.assertTrue(report["sources"]["opencode"].startswith("unknown:"))
        self.assertEqual(report["sources"]["claude"], "unknown:no-fresh-taps")

    def test_refresh_bumps_attempted_at_on_failure_without_losing_payload(self):
        cache = self._cache()
        cache.record("codex", {"windows": [["7d", 30, None]]}, now=100.0)
        with mock.patch.object(C, "_codex_api_score", return_value=None), \
                mock.patch.object(C, "_codex_score", return_value=None), \
                mock.patch.object(C, "_opencode_api_score", return_value=None), \
                mock.patch.object(C, "_claude_capacity_reading", return_value=(None, None)):
            report = C.refresh_gauges(now=200.0)
        self.assertEqual(report["scores"]["codex"], 70.0)      # cache reused
        snapshot = cache.read("codex", now=200.0)
        self.assertEqual(snapshot["attempted_at"], 200.0)
        self.assertEqual(snapshot["observed_at"], 100.0)
        self.assertEqual(snapshot["payload"]["windows"][0][1], 30)


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

    def test_balanced_gate_demotes_used_percent_at_or_above_ninety(self):
        ranked = C.rank_band(
            ["claude", "codex"], self.states, {"claude": 0, "codex": 1},
            ["claude", "codex"], {"claude": 5, "codex": 80},
            strategy="balanced", usage_gate_used_percent=90,
        )
        self.assertEqual(ranked, ["codex", "claude"])

    def test_balanced_bias_cannot_lift_a_gated_harness_over_an_ungated_one(self):
        # Asymmetric, non-round headroom and counts: a unit inversion (percent
        # vs. headroom) or a naive post-sort "move bias to front" override
        # would both happen to pass with tidy round numbers, so this uses
        # uneven values and an uneven count tiebreak (codex has far more
        # recent attempts yet must still rank first) to make either bug show.
        states = {**self.states, "claude": "ok", "codex": "ok"}
        counts = {"claude": 1, "codex": 9}
        scores = {"claude": 7.5, "codex": 63.2}  # claude 92.5% used (gated), codex 36.8% used (ungated)
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": "claude"}):
            biased = C.rank_band(
                ["claude", "codex"], states, counts, ["claude", "codex"], scores,
                strategy="balanced", usage_gate_used_percent=90,
            )
        unbiased = C.rank_band(
            ["claude", "codex"], states, counts, ["claude", "codex"], scores,
            strategy="balanced", usage_gate_used_percent=90,
        )
        self.assertEqual(biased, ["codex", "claude"])
        self.assertEqual(biased, unbiased)

    def test_balanced_bias_still_reorders_within_the_gated_class(self):
        # The fix must not neuter bias entirely: within a single gate class it
        # should still move the biased harness to the front, same as before.
        states = {**self.states, "claude": "ok", "codex": "ok"}
        counts = {"claude": 4, "codex": 1}
        scores = {"claude": 9.0, "codex": 4.5}  # both gated (>= 90% used)
        unbiased = C.rank_band(
            ["claude", "codex"], states, counts, ["claude", "codex"], scores,
            strategy="balanced", usage_gate_used_percent=90,
        )
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": "codex"}):
            biased = C.rank_band(
                ["claude", "codex"], states, counts, ["claude", "codex"], scores,
                strategy="balanced", usage_gate_used_percent=90,
            )
        self.assertEqual(unbiased, ["claude", "codex"])
        self.assertEqual(biased, ["codex", "claude"])

    def test_balanced_all_gated_uses_maximum_headroom(self):
        ranked = C.rank_band(
            ["claude", "codex", "opencode"], self.states,
            {"claude": 0, "codex": 0, "opencode": 0}, C.HARNESSES,
            {"claude": 9, "codex": 4, "opencode": 1}, strategy="balanced",
        )
        self.assertEqual(ranked, ["claude", "codex", "opencode"])

    def test_balanced_unknown_gauge_is_optimistic_pass(self):
        ranked = C.rank_band(
            ["claude", "codex"], self.states, {"claude": 3, "codex": 0},
            ["claude", "codex"], {"claude": 10, "codex": None},
            strategy="balanced",
        )
        self.assertEqual(ranked, ["codex", "claude"])

    def test_balanced_prefers_clearly_larger_headroom_at_equal_counts(self):
        # The reported symptom (2026-08-20): 58%-headroom claude beating
        # 99%-headroom codex because the general branch never consulted the
        # gauge value, only the 10%-cutoff gate class.
        ranked = C.rank_band(
            ["claude", "codex"], self.states, {"claude": 0, "codex": 0},
            ["claude", "codex"], {"claude": 58, "codex": 99},
            strategy="balanced",
        )
        self.assertEqual(ranked, ["codex", "claude"])

    def test_balanced_reduces_to_round_robin_when_headroom_is_equal(self):
        ranked_a = C.rank_band(
            ["claude", "codex"], self.states, {"claude": 3, "codex": 1},
            ["claude", "codex"], {"claude": 70, "codex": 70},
            strategy="balanced",
        )
        self.assertEqual(ranked_a, ["codex", "claude"])
        ranked_b = C.rank_band(
            ["claude", "codex"], self.states, {"claude": 1, "codex": 3},
            ["claude", "codex"], {"claude": 70, "codex": 70},
            strategy="balanced",
        )
        self.assertEqual(ranked_b, ["claude", "codex"])

    def test_balanced_recent_attempts_still_outweigh_a_modest_headroom_gap(self):
        # 2026-08-13 balanced-first policy must not be defeated by headroom
        # alone: codex already took 20 of the last 30 attempts, so claude is
        # still due even though codex has more fresh headroom.
        ranked = C.rank_band(
            ["claude", "codex"], self.states, {"claude": 10, "codex": 20},
            ["claude", "codex"], {"claude": 58, "codex": 99},
            strategy="balanced",
        )
        self.assertEqual(ranked, ["claude", "codex"])

    def test_balanced_headroom_weight_is_continuous_not_stepped(self):
        counts = {"claude": 0, "codex": 0}
        values = []
        for codex_score in (58, 58.5, 59, 70, 85, 99):
            deficit = C.allocation_deficit(
                {"claude": 58, "codex": codex_score}, counts, ["claude", "codex"],
            )
            values.append(deficit["codex"] - deficit["claude"])
        # Strictly increasing as codex's headroom rises relative to claude's
        # fixed 58 — a stepped/thresholded formula would show a flat run.
        for earlier, later in zip(values, values[1:]):
            self.assertLess(earlier, later)
        ranked = C.rank_band(
            ["claude", "codex"], self.states, counts,
            ["claude", "codex"], {"claude": 58, "codex": 99},
            strategy="balanced",
        )
        self.assertEqual(ranked, ["codex", "claude"])

    def test_balanced_unknown_gauge_takes_the_neutral_share(self):
        ranked = C.rank_band(
            ["claude", "codex"], self.states, {"claude": 0, "codex": 0},
            ["claude", "codex"], {"claude": 99, "codex": None},
            strategy="balanced",
        )
        self.assertEqual(set(ranked), {"claude", "codex"})
        self.assertEqual(ranked[0], "claude")

    def test_allocation_deficit_all_zero_headroom_falls_back_to_round_robin(self):
        deficit = C.allocation_deficit(
            {"a": 0, "b": 0}, {"a": 2, "b": 0}, ["a", "b"],
        )
        self.assertGreater(deficit["b"], deficit["a"])

    def test_balanced_gate_beats_quality_band_across_bands(self):
        # B-1 framing's smallest falsifier: a gated primary must not win over
        # an ungated relief, regardless of band_rank.
        policy = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 0,
        }
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 5, "codex": 5, "opencode": 80},
            strategy="balanced",
        )
        self.assertEqual((chosen, band), ("opencode", "relief"))

    def test_balanced_gate_beats_quality_band_into_last_resort(self):
        policy = {
            "primary": ["claude", "codex"],
            "relief": [],
            "last_resort": ["opencode"],
            "promote_relief_below": 0,
        }
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 0, "codex": 10, "opencode": 60},
            strategy="balanced",
        )
        self.assertEqual((chosen, band), ("opencode", "last_resort"))

    def test_balanced_unknown_usage_outranks_a_gated_primary(self):
        policy = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 0,
        }
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 5, "codex": 5, "opencode": None},
            strategy="balanced",
        )
        self.assertEqual((chosen, band), ("opencode", "relief"))

    def test_balanced_gate_boundary_is_inclusive_across_bands(self):
        policy = {
            "primary": ["claude"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 0,
        }
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 10, "opencode": 11},
            strategy="balanced", usage_gate_used_percent=90,
        )
        self.assertEqual((chosen, band), ("opencode", "relief"))
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 10.1, "opencode": 11},
            strategy="balanced", usage_gate_used_percent=90,
        )
        self.assertEqual((chosen, band), ("claude", "primary"))

    def test_balanced_all_gated_uses_global_max_headroom_before_band(self):
        policy = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 0,
        }
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 4, "codex": 1, "opencode": 9},
            strategy="balanced",
        )
        self.assertEqual((chosen, band), ("opencode", "relief"))

    def test_balanced_ungated_primary_still_wins_over_ungated_relief(self):
        policy = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 0,
        }
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 60, "codex": 40, "opencode": 100},
            strategy="balanced",
        )
        self.assertEqual(band, "primary")
        self.assertIn(chosen, {"claude", "codex"})

    def test_balanced_relief_promotion_survives_the_gate(self):
        policy = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 35,
        }
        chosen, band, _ranks, promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 20, "codex": 30, "opencode": 90},
            strategy="balanced",
        )
        self.assertEqual((chosen, band, promoted), ("opencode", "relief", True))

    def test_capacity_aware_has_no_cross_band_gate(self):
        policy = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
            "promote_relief_below": 0,
        }
        chosen, band, _ranks, _promoted = C.select(
            policy, self.states, self.counts, C.HARNESSES,
            {"claude": 5, "codex": 5, "opencode": 80},
        )
        self.assertEqual(band, "primary")
        self.assertIn(chosen, {"claude", "codex"})

    def test_ordered_candidates_demotes_but_never_drops_gated(self):
        ranks = {
            "primary": ["claude", "codex"],
            "relief": ["opencode"],
            "last_resort": [],
        }
        scores = {"claude": 5, "codex": 5, "opencode": 80}
        flat = C.ordered_candidates(
            ranks, ("primary", "relief", "last_resort"), scores, strategy="balanced",
        )
        self.assertEqual(
            [name for _band, name in flat], ["opencode", "claude", "codex"],
        )

    def test_equal_headroom_neutral_affinity_is_exact_round_robin_for_both_exponents(self):
        for exponent in (1, 2):
            for counts, expected in (({"claude": 3, "codex": 2}, {"claude": 0.0, "codex": 1.0}),
                                     ({"claude": 2, "codex": 3}, {"claude": 1.0, "codex": 0.0})):
                deficit = C.allocation_deficit({"claude": 80, "codex": 80}, counts,
                    ["claude", "codex"], preferred=None, affinity_weight=.5,
                    headroom_exponent=exponent)
                self.assertEqual(deficit, expected)

    def test_depth_affinity_flips_owner_and_worker_preference(self):
        allocation = {"depth_affinity": {"owner": "claude", "worker": "codex"}}
        self.assertEqual(C.preferred_for_depth(allocation, 1), "claude")
        self.assertEqual(C.preferred_for_depth(allocation, 2), "codex")
        for preferred, expected in (("claude", {"claude": 1.25, "codex": -.25}),
                                    ("codex", {"claude": -.25, "codex": 1.25})):
            self.assertEqual(C.allocation_deficit({"claude": 80, "codex": 80}, {"claude": 2, "codex": 2},
                ["claude", "codex"], preferred=preferred, affinity_weight=.65,
                headroom_exponent=2), expected)

    def test_preferred_gated_harness_never_beats_ungated_peer(self):
        ranked = C.rank_band(["claude", "codex"], self.states, self.counts,
            ["claude", "codex"], {"claude": 5, "codex": 60}, strategy="balanced",
            usage_gate_used_percent=90, preferred="claude", affinity_weight=1.0)
        self.assertEqual(ranked, ["codex", "claude"])

    def test_headroom_exponent_sharpens_share_without_forcing_order(self):
        for exponent, expected in ((1, (.387096774194, .612903225806)),
                                    (2, (.285148514851, .714851485149))):
            d = C.allocation_deficit({"claude": 60, "codex": 95}, {"claude": 0, "codex": 0},
                ["claude", "codex"], headroom_exponent=exponent)
            self.assertAlmostEqual(d["claude"], expected[0], places=12)
            self.assertAlmostEqual(d["codex"], expected[1], places=12)
            self.assertEqual(C.rank_band(["claude", "codex"], self.states, self.counts,
                ["claude", "codex"], {"claude": 60, "codex": 95}, strategy="balanced",
                headroom_exponent=exponent), ["codex", "claude"])
        for exponent, expected in ((1, (.539792387543, .460207612457)),
                                   (2, (.425551261650, .574448738350))):
            d = C.allocation_deficit({"claude": 60, "codex": 95}, {"claude": 0, "codex": 0},
                ["claude", "codex"], preferred="claude", affinity_weight=.65,
                headroom_exponent=exponent)
            self.assertAlmostEqual(d["claude"], expected[0], places=12)
            self.assertAlmostEqual(d["codex"], expected[1], places=12)

    def test_capacity_aware_affinity_obeys_margin_and_unknown_exclusion(self):
        kwargs = dict(preferred="codex", affinity_weight=.65)
        self.assertEqual(C.rank_band(["claude", "codex"], self.states, self.counts,
            ["claude", "codex"], {"claude": 80, "codex": 55}, **kwargs), ["codex", "claude"])
        self.assertEqual(C.rank_band(["claude", "codex"], self.states, self.counts,
            ["claude", "codex"], {"claude": 90, "codex": 55}, **kwargs), ["claude", "codex"])
        ranked = C.rank_band(["claude", "codex"], self.states, self.counts,
            ["claude", "codex"], {"claude": 80, "codex": None}, **kwargs)
        self.assertEqual(ranked, ["claude"])

    def test_affinity_never_overrides_large_recent_count_gap(self):
        d = C.allocation_deficit({"claude": 80, "codex": 80}, {"claude": 10, "codex": 0},
            ["claude", "codex"], preferred="claude", affinity_weight=.65, headroom_exponent=2)
        self.assertAlmostEqual(d["claude"], -2.85, places=2)
        self.assertAlmostEqual(d["codex"], 3.85, places=2)
        # The recent-count dominance property lives in the `balanced` deficit.
        # `capacity-aware` has no share arithmetic: there the margin is a
        # deliberate in-class hoist modelled on HARNESS_CAPACITY_BIAS, and
        # adding a count veto to it would be the new soft threshold DP-24
        # forbids. Assert each strategy's own contract.
        self.assertEqual(C.rank_band(["claude", "codex"], self.states, {"claude": 10, "codex": 0},
            ["claude", "codex"], {"claude": 80, "codex": 80}, strategy="balanced",
            preferred="claude", affinity_weight=.65, headroom_exponent=2), ["codex", "claude"])
        self.assertEqual(C.rank_band(["claude", "codex"], self.states, {"claude": 10, "codex": 0},
            ["claude", "codex"], {"claude": 80, "codex": 80},
            preferred="claude", affinity_weight=.65, headroom_exponent=2), ["claude", "codex"])

    def test_harness_weights_default_keeps_the_existing_formula_and_low_weight_ranks_later(self):
        args = ({"claude": 80, "codex": 80, "opencode": 80}, {"claude": 0, "codex": 0, "opencode": 0},
                ["claude", "codex", "opencode"])
        base = C.allocation_deficit(*args)
        self.assertEqual(C.allocation_deficit(*args, harness_weights=None), base)
        self.assertEqual(C.allocation_deficit(*args, harness_weights={}), base)
        self.assertEqual(C.allocation_deficit(*args, harness_weights={"opencode": 1.0}), base)
        weighted = C.allocation_deficit(*args, harness_weights={"opencode": 0.3})
        self.assertLess(weighted["opencode"], weighted["claude"])
        self.assertAlmostEqual(sum(weighted.values()), 1.0, places=6)
        states = {"claude": "ok", "codex": "ok", "opencode": "ok"}
        counts = {"claude": 0, "codex": 0, "opencode": 0}
        scores = {"claude": 80, "codex": 80, "opencode": 80}
        order = ["opencode", "claude", "codex"]  # declared order would favour opencode
        self.assertEqual(C.rank_band(["claude", "codex", "opencode"], states, counts, order, scores,
            strategy="balanced")[0], "opencode")
        self.assertEqual(C.rank_band(["claude", "codex", "opencode"], states, counts, order, scores,
            strategy="balanced", harness_weights={"opencode": 0.3})[-1], "opencode")
        # A weighted harness still wins once the others have used their share.
        busy = {"claude": 5, "codex": 5, "opencode": 0}
        self.assertEqual(C.rank_band(["claude", "codex", "opencode"], states, busy, order, scores,
            strategy="balanced", harness_weights={"opencode": 0.3})[0], "opencode")
        name, _band, _ranks, _promote = C.select(
            {"primary": ["claude", "codex", "opencode"], "relief": [], "last_resort": []},
            states, counts, order, scores, strategy="balanced", harness_weights={"opencode": 0.3})
        self.assertNotEqual(name, "opencode")

    def test_single_candidate_is_neutral_for_all_weights(self):
        for weight in (0, .5, 1.0):
            for exponent in (1, 2):
                self.assertEqual(C.allocation_deficit({"claude": 80}, {"claude": 4}, ["claude"],
                    preferred="claude", affinity_weight=weight, headroom_exponent=exponent), {"claude": 1.0})


class ClaudeAccountAggregationTests(unittest.TestCase):
    """C-6: account-wide claude headroom must merge windows, not pick one file.

    Reproduces the 51<->1 oscillation: two session files are both fresh, but
    each only reports one window, and the previous winner-take-all read of
    "the single most-recent file" could answer 51 or 1 depending purely on
    which file's mtime happened to sort first.
    """

    def _write(self, statusline_dir, name, rate_limits, mtime, **payload_fields):
        path = statusline_dir / name
        payload = {"rate_limits": rate_limits, **payload_fields}
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def _report(self, home, now, scopes=None):
        # Manual non-Claude scores keep all networked provider probes disabled.
        with mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(home),
                "HARNESS_CAPACITY_SCORES": "codex:12,opencode:34",
                "AGENT_DISPATCH_JOBS": "",
        }), mock.patch("dispatch_capacity_evidence._scope_candidates", return_value=scopes or {}):
            return C.capacity_report(now=now)

    def test_merges_windows_across_fresh_sessions_instead_of_picking_one_file(self):
        with tempfile.TemporaryDirectory() as home:
            statusline = Path(home) / ".statusline"
            statusline.mkdir()
            now = 1_000_000.0
            # session A: only reports window "five_hour" at 49% used (51 headroom)
            self._write(statusline, "a.json", {"five_hour": {"used_percentage": 49}}, now - 10)
            # session B, slightly newer: only reports window "week" at 99% used (1 headroom)
            self._write(statusline, "b.json", {"week": {"used_percentage": 99}}, now - 5)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                score = C._claude_score(now, stale_after=3600)
        # Both windows are live and must both count: headroom is the minimum
        # across merged windows, i.e. 100 - max(49, 99) = 1, never a
        # winner-take-all 51 from ignoring session B's window.
        self.assertEqual(score, 1.0)

    def test_stale_file_does_not_contribute_a_window(self):
        with tempfile.TemporaryDirectory() as home:
            statusline = Path(home) / ".statusline"
            statusline.mkdir()
            now = 1_000_000.0
            self._write(statusline, "fresh.json", {"five_hour": {"used_percentage": 49}}, now - 10)
            self._write(statusline, "stale.json", {"week": {"used_percentage": 99}}, now - 10_000)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                score = C._claude_score(now, stale_after=3600)
        self.assertEqual(score, 51.0)

    def test_malformed_file_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as home:
            statusline = Path(home) / ".statusline"
            statusline.mkdir()
            now = 1_000_000.0
            bad = statusline / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            os.utime(bad, (now - 5, now - 5))
            self._write(statusline, "ok.json", {"five_hour": {"used_percentage": 20}}, now - 3)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                score = C._claude_score(now, stale_after=3600)
        self.assertEqual(score, 80.0)

    def test_same_window_key_takes_the_most_recent_mtime_value(self):
        with tempfile.TemporaryDirectory() as home:
            statusline = Path(home) / ".statusline"
            statusline.mkdir()
            now = 1_000_000.0
            # Older file for the same window says 90% used; newer file says 10%.
            self._write(statusline, "older.json", {"five_hour": {"used_percentage": 90}}, now - 20)
            self._write(statusline, "newer.json", {"five_hour": {"used_percentage": 10}}, now - 5)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                score = C._claude_score(now, stale_after=3600)
        self.assertEqual(score, 90.0)

    def test_same_reset_keeps_high_usage_despite_fresher_lower_rewrite(self):
        with tempfile.TemporaryDirectory() as home:
            statusline=Path(home)/'.statusline';statusline.mkdir();now=1_000_000.
            self._write(statusline,'high.json',{'seven_day':{'used_percentage':99,'resets_at':now+3600}},now-20)
            self._write(statusline,'identical.json',{'seven_day':{'used_percentage':99,'resets_at':now+3600}},now-2)
            self._write(statusline,'rewrite.json',{'seven_day':{'used_percentage':69,'resets_at':now+3600}},now-1)
            with mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':home}):
                self.assertEqual(C._claude_score(now,3600),1.)
            report = self._report(home, now)
        self.assertEqual(report["scores"]["claude"], 1.0)
        self.assertEqual(report["sources"]["claude"], "taps")
        self.assertEqual(report["freshness"]["claude"]["observation_time"], "unknown")
        self.assertEqual(report["freshness"]["claude"]["age_seconds"], 20.0)

    def test_fresh_tap_reports_file_age_threshold_and_unknown_observation_time(self):
        with tempfile.TemporaryDirectory() as home:
            statusline = Path(home) / ".statusline"
            statusline.mkdir()
            now = 1_000_000.0
            self._write(statusline, "fresh.json", {
                "five_hour": {"used_percentage": 65, "resets_at": now + 3600},
            }, now - 42.3)
            report = self._report(home, now)
        self.assertEqual(report["scores"]["claude"], 35.0)
        self.assertEqual(report["sources"]["claude"], "taps")
        self.assertEqual(report["freshness"]["claude"], {
            "basis": "file-mtime",
            "age_seconds": 42.3,
            "stale_after_seconds": 3600,
            "observation_time": "unknown",
        })

    def test_stale_future_and_scope_mismatched_taps_do_not_report_freshness(self):
        now = 1_000_000.0
        for name, mtime, fields in (
                ("stale", now - 3601, {}),
                ("future", now + 1, {}),
                ("mismatched", now - 5, {"quota_scope": "foreign", "quota_scope_kind": "account"}),
        ):
            with self.subTest(kind=name), tempfile.TemporaryDirectory() as home:
                statusline = Path(home) / ".statusline"
                statusline.mkdir()
                self._write(statusline, f"{name}.json", {
                    "week": {"used_percentage": 80, "resets_at": now + 3600},
                }, mtime, **fields)
                report = self._report(home, now, scopes={"account": "current"})
                self.assertIsNone(report["scores"]["claude"])
                self.assertEqual(report["sources"]["claude"], "unknown:no-fresh-taps")
                self.assertNotIn("claude", report["freshness"])

    def test_expired_window_does_not_block_real_reset_or_merge_other_window(self):
        with tempfile.TemporaryDirectory() as home:
            statusline=Path(home)/'.statusline';statusline.mkdir();now=1_000_000.
            self._write(statusline,'expired.json',{'five_hour':{'used_percentage':99,'resets_at':now-1}},now-1)
            self._write(statusline,'new.json',{'five_hour':{'used_percentage':2,'resets_at':now+18000},
                                             'seven_day':{'used_percentage':73,'resets_at':now+86400}},now-2)
            with mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':home}):
                self.assertEqual(C._claude_score(now,3600),27.)

    def test_explicit_foreign_account_and_malformed_reset_are_not_current_usage(self):
        with tempfile.TemporaryDirectory() as home:
            statusline=Path(home)/'.statusline';statusline.mkdir();now=1_000_000.
            self._write(statusline,'bad.json',{'seven_day':{'used_percentage':99,'resets_at':'invalid'}},now-1)
            p=self._write(statusline,'foreign.json',{'seven_day':{'used_percentage':99,'resets_at':now+3600}},now-2)
            d=json.loads(p.read_text());d.update(quota_scope='foreign',quota_scope_kind='account');p.write_text(json.dumps(d));os.utime(p,(now-2,now-2))
            self._write(statusline,'current.json',{'seven_day':{'used_percentage':20,'resets_at':now+3600}},now-3)
            with mock.patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':home}), \
                 mock.patch('dispatch_capacity_evidence._scope_candidates',return_value={'account':'current'}):
                self.assertEqual(C._claude_score(now,3600),80.)

    def test_equal_mtime_tie_is_deterministic(self):
        with tempfile.TemporaryDirectory() as home:
            statusline = Path(home) / ".statusline"
            statusline.mkdir()
            now = 1_000_000.0
            tied = now - 5
            self._write(statusline, "b_file.json", {"five_hour": {"used_percentage": 10}}, tied)
            self._write(statusline, "a_file.json", {"five_hour": {"used_percentage": 40}}, tied)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                first = C._claude_score(now, stale_after=3600)
                second = C._claude_score(now, stale_after=3600)
        # Deterministic across repeated calls, and pinned to the name-order
        # tiebreak (alphabetically-first "a_file.json" wins the tie): 60.0.
        self.assertEqual(first, second)
        self.assertEqual(first, 60.0)

    def test_no_files_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".statusline").mkdir()
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                self.assertIsNone(C._claude_score(1_000_000.0, stale_after=3600))

    def test_return_type_is_float_or_none(self):
        with tempfile.TemporaryDirectory() as home:
            statusline = Path(home) / ".statusline"
            statusline.mkdir()
            now = 1_000_000.0
            self._write(statusline, "a.json", {"five_hour": {"used_percentage": 30}}, now - 5)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
                score = C._claude_score(now, stale_after=3600)
        self.assertIsInstance(score, float)


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


class OpencodeGaugeTests(unittest.TestCase):
    """The Go-plan gauge the placement probe ignored until 2026-09-10."""

    def _with_collector(self, payload):
        module = types.ModuleType("fleet.collectors.zen_go_usage")
        module.account_usage = lambda: payload
        fleet = types.ModuleType("fleet")
        collectors = types.ModuleType("fleet.collectors")
        collectors.zen_go_usage = module
        fleet.collectors = collectors
        return mock.patch.dict(sys.modules, {
            "fleet": fleet,
            "fleet.collectors": collectors,
            "fleet.collectors.zen_go_usage": module,
        })

    def test_the_tightest_window_decides_the_headroom(self):
        with self._with_collector({"rl_windows": [["5h", 100, 0], ["wk", 67, 0], ["mo", 53, 0]]}):
            self.assertEqual(C._opencode_api_score(), 0.0)
        with self._with_collector({"rl_windows": [["5h", 12, 0], ["wk", 40, 0]]}):
            self.assertEqual(C._opencode_api_score(), 60.0)

    def test_absence_and_rejection_stay_unknown_never_a_guess(self):
        for payload in (None, {}, {"rl_windows": []}, {"error": "auth", "detail": 401}):
            with self.subTest(payload=payload), self._with_collector(payload):
                self.assertIsNone(C._opencode_api_score())

    def test_capacity_scores_publishes_the_gauge_and_honours_the_manual_override(self):
        with self._with_collector({"rl_windows": [["5h", 30, 0]]}):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HARNESS_CAPACITY_SCORES", None)
                self.assertEqual(C.capacity_scores()["opencode"], 70.0)
            with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_SCORES": "opencode:5"}):
                self.assertEqual(C.capacity_scores()["opencode"], 5.0)


if __name__ == "__main__":
    unittest.main()
