#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
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
            {"claude": 20, "codex": 30, "opencode": None},
        )
        self.assertEqual((chosen, band, promoted), ("opencode", "relief", True))

    def test_unknown_primary_gauges_do_not_promote_relief(self):
        chosen, band, _ranks, promoted = C.select(
            self.policy, self.states, self.counts, C.HARNESSES,
            {name: None for name in C.HARNESSES},
        )
        self.assertEqual((chosen, band, promoted), ("claude", "primary", False))

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
            {"claude": 90, "codex": 90, "opencode": None},
        )
        self.assertEqual((chosen, band), ("opencode", "relief"))

    def test_manual_bias_never_crosses_quality_band(self):
        with mock.patch.dict(os.environ, {"HARNESS_CAPACITY_BIAS": "opencode"}):
            chosen, band, _ranks, _promoted = C.select(
                self.policy, self.states, self.counts, C.HARNESSES,
                {name: 80 for name in C.HARNESSES},
            )
        self.assertEqual((chosen, band), ("claude", "primary"))


if __name__ == "__main__":
    unittest.main()
