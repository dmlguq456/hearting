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

    def test_single_candidate_is_neutral_for_all_weights(self):
        for weight in (0, .5, 1.0):
            for exponent in (1, 2):
                self.assertEqual(C.allocation_deficit({"claude": 80}, {"claude": 4}, ["claude"],
                    preferred="claude", affinity_weight=weight, headroom_exponent=exponent), {"claude": 1.0})


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
