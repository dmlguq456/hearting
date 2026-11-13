#!/usr/bin/env python3
"""Production-disabled token self-regulation Phase 3 tests."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet.token_experiment import (  # noqa: E402
    ARMS,
    EXCLUSION_REASONS,
    _exclusion_for,
    canonical_json,
    config_fingerprint,
    evaluate,
    manifest_sha256,
    replay_all,
    replay_policy,
)


ROOT = Path(_TOOLS_DIR).parent
FIXTURES = Path(__file__).parent / "fixtures" / "token_experiment"
CLI = ROOT / "utilities" / "token-budget-experiment.py"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ForecastReplayTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = load("manifest.json")
        cls.replay = load("replay.json")

    def test_fixture_decisions_episode_suppression_reopen_and_unknown(self):
        result = replay_all(self.replay, self.manifest)
        dynamic = result["arms"]["dynamic"]
        expected = load("replay_expected.json")
        self.assertEqual([item["directive_id"] for item in dynamic["decisions"]],
                         expected["dynamic_directive_ids"])
        self.assertEqual([item["reason"] for item in dynamic["decisions"]],
                         expected["dynamic_reasons"])
        self.assertEqual(dynamic["emissions"], expected["dynamic_emissions"])
        self.assertFalse(result["production_enabled"])
        self.assertTrue(result["synthetic_non_evidentiary"])

    def test_unknown_never_emits_early_and_nondecision_fields_are_ignored(self):
        records = [
            {"context_used_pct": 60, "status": "observed"},
            {"context_used_pct": 69, "status": "degraded"},
        ]
        first = replay_policy(records, self.manifest, arm="dynamic")
        records[1].update(session_token_delta=999999, hook_invocations=999,
                          directive_utf8_bytes_total=888)
        second = replay_policy(records, self.manifest, arm="dynamic")
        self.assertEqual(first, second)
        self.assertIsNone(first["decisions"][1]["directive_id"])

    def test_cli_replay_is_byte_identical(self):
        command = [sys.executable, str(CLI), "replay", "--manifest",
                   str(FIXTURES / "manifest.json"), "--input",
                   str(FIXTURES / "replay.json")]
        first = subprocess.run(command, text=True, capture_output=True, check=True).stdout
        second = subprocess.run(command, text=True, capture_output=True, check=True).stdout
        self.assertEqual(first, second)
        self.assertEqual(first, canonical_json(json.loads(first)))


class EvaluatorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manifest = load("manifest.json")
        cls.manifest_hash = manifest_sha256(cls.manifest)

    def workload(self, index, *, stratum="code", control_delta=120,
                 static_delta=100, dynamic_delta=80, control_quality=0.9,
                 static_quality=0.9, dynamic_quality=0.9):
        experiment_id = "synthetic-phase3"
        workload_id = f"workload-{index:03d}"
        declaration = {
            "experiment_id": experiment_id,
            "workload_id": workload_id,
            "stratum": stratum,
            "prompt_sha256": "1" * 64,
            "artifact_bundle_sha256": "2" * 64,
            "rubric_version": "rubric-v1",
            "rubric_sha256": "3" * 64,
            "required_checks": ["required-v1"],
            "safety_checks": ["safety-v1"],
            "model_id": "model-exact",
            "runtime_id": "harness-1",
            "runtime_config_sha256": "4" * 64,
            "reasoning_effort": "high",
            "intensity": "thorough",
            "dispatch_depth": 2,
            "qa_contract": "thorough-code",
            "seed": None,
            "arm_order": list(ARMS),
        }
        fingerprint = config_fingerprint(declaration)
        deltas = {"control": control_delta, "static": static_delta, "dynamic": dynamic_delta}
        qualities = {"control": control_quality, "static": static_quality, "dynamic": dynamic_quality}
        results = []
        for arm in ARMS:
            emissions = 0 if arm == "control" else 1
            results.append({
                "experiment_id": experiment_id,
                "workload_id": workload_id,
                "arm": arm,
                "manifest_sha256": self.manifest_hash,
                "config_fingerprint": fingerprint,
                "status": "complete",
                "exclusion_reason": None,
                "session_counter_status": "observed",
                "session_total_tokens_start": 1000,
                "session_total_tokens_end": 1000 + deltas[arm],
                "session_token_delta": deltas[arm],
                "hook_invocations": 2,
                "zero_injections": 2 - emissions,
                "emissions": emissions,
                "directive_utf8_bytes_total": 0 if arm == "control" else 154,
                "directive_exact_tokens": None,
                "exact_tokenizer_provenance": None,
                "required_checks_pass": True,
                "safety_checks_pass": True,
                "hard_regression": False,
                "quality_score": qualities[arm],
                "quality_evaluator_id": "frozen-rubric-v1",
            })
        return {"declaration": declaration, "results": results}

    def payload(self, count=30, **kwargs):
        return {"experiment_id": "synthetic-phase3",
                "workloads": [self.workload(index, **kwargs) for index in range(count)]}

    def test_all_gates_eligible_but_adoption_stays_pending(self):
        payload = self.payload()
        result = evaluate(payload, self.manifest)
        self.assertEqual(result["verdict"], "eligible_for_user_review")
        self.assertTrue(all(value == "pass" for value in result["gates"].values()))
        self.assertEqual(result["adoption_decision"], "pending_user_decision")
        self.assertFalse(result["production_dynamic_enabled"])
        self.assertEqual(result["observed_delta_metric_label"],
                         "observed session-token delta difference (non-billing)")
        self.assertEqual(result["metrics"]["directive_utf8_bytes_by_arm"],
                         {"control": 0, "static": 4620, "dynamic": 4620})

    def test_minimum_and_per_stratum_gate(self):
        self.assertEqual(evaluate(self.payload(29), self.manifest)["verdict"], "insufficient")
        payload = self.payload(30)
        for index, workload in enumerate(payload["workloads"]):
            workload["declaration"]["stratum"] = "docs" if index < 5 else "code"
        self.assertEqual(evaluate(payload, self.manifest)["gates"]["G1_sample"], "fail")

    def test_safety_quality_and_both_observed_delta_gates(self):
        unsafe = self.payload()
        unsafe["workloads"][0]["results"][0]["safety_checks_pass"] = False
        self.assertEqual(evaluate(unsafe, self.manifest)["gates"]["G3_safety_required"], "fail")
        quality = self.payload(dynamic_quality=0.85)
        self.assertEqual(evaluate(quality, self.manifest)["gates"]["G4_quality"], "fail")
        control = self.payload(control_delta=80)
        self.assertEqual(evaluate(control, self.manifest)["gates"]["G5_control_confidence"], "fail")
        static = self.payload(static_delta=80)
        self.assertEqual(evaluate(static, self.manifest)["gates"]["G6_static_comparison"], "fail")

    def test_exclusion_enum_priority_and_counter_degradation(self):
        base = self.workload(0)
        mutations = {}
        value = copy.deepcopy(base); value["results"].pop(); mutations["missing_arm"] = value
        value = copy.deepcopy(base); value["results"][0]["config_fingerprint"] = "x" * 64; mutations["pairing_fingerprint_mismatch"] = value
        value = copy.deepcopy(base); value["results"][0]["session_counter_status"] = "degraded"; mutations["counter_unknown_or_degraded"] = value
        value = copy.deepcopy(base); value["results"][0]["session_total_tokens_end"] = 900; value["results"][0]["session_token_delta"] = 0; mutations["counter_decreased"] = value
        value = copy.deepcopy(base); del value["results"][0]["quality_score"]; mutations["required_output_missing"] = value
        value = copy.deepcopy(base); value["results"][0]["status"] = "failed"; mutations["runner_failure"] = value
        value = copy.deepcopy(base); value["results"][0]["quality_evaluator_id"] = "changed-reviewer"; mutations["rubric_missing_or_changed"] = value
        value = copy.deepcopy(base); value["results"][0]["manifest_sha256"] = "0" * 64; mutations["manifest_changed"] = value
        for expected in EXCLUSION_REASONS:
            with self.subTest(expected=expected):
                reason, triplet = _exclusion_for(mutations[expected], self.manifest_hash)
                self.assertEqual(reason, expected)
                self.assertIsNone(triplet)

    def test_strict_schema_and_invalid_arm_never_enters_triplet(self):
        base = self.workload(0)

        invalid = copy.deepcopy(base)
        invalid["results"][0]["status"] = "invalid"
        reason, triplet = _exclusion_for(invalid, self.manifest_hash)
        self.assertEqual(reason, "required_output_missing")
        self.assertIsNone(triplet)

        extra_result = copy.deepcopy(base)
        extra_result["results"][0]["prompt"] = "must-not-be-accepted"
        reason, triplet = _exclusion_for(extra_result, self.manifest_hash)
        self.assertEqual(reason, "required_output_missing")
        self.assertIsNone(triplet)

        extra_declaration = copy.deepcopy(base)
        extra_declaration["declaration"]["prompt"] = "must-not-be-accepted"
        reason, triplet = _exclusion_for(extra_declaration, self.manifest_hash)
        self.assertEqual(reason, "pairing_fingerprint_mismatch")
        self.assertIsNone(triplet)

        blank_provenance = copy.deepcopy(base)
        blank_provenance["results"][1]["directive_exact_tokens"] = 1
        blank_provenance["results"][1]["exact_tokenizer_provenance"] = ""
        reason, triplet = _exclusion_for(blank_provenance, self.manifest_hash)
        self.assertEqual(reason, "required_output_missing")
        self.assertIsNone(triplet)

        malformed_provenance = copy.deepcopy(base)
        malformed_provenance["results"][1]["directive_exact_tokens"] = 1
        malformed_provenance["results"][1]["exact_tokenizer_provenance"] = "unstructured"
        reason, triplet = _exclusion_for(malformed_provenance, self.manifest_hash)
        self.assertEqual(reason, "required_output_missing")
        self.assertIsNone(triplet)

        impossible_bytes = copy.deepcopy(base)
        impossible_bytes["results"][1]["directive_utf8_bytes_total"] = 999999
        reason, triplet = _exclusion_for(impossible_bytes, self.manifest_hash)
        self.assertEqual(reason, "required_output_missing")
        self.assertIsNone(triplet)

    def test_duplicate_workload_ids_never_satisfy_sample_gate(self):
        duplicate = self.workload(0)
        payload = {
            "experiment_id": "synthetic-phase3",
            "workloads": [copy.deepcopy(duplicate) for _ in range(30)],
        }
        result = evaluate(payload, self.manifest)
        self.assertEqual(result["complete_triplets"], 0)
        self.assertEqual(result["gates"]["G1_sample"], "fail")
        self.assertEqual(result["verdict"], "insufficient")
        self.assertEqual(
            result["exclusion_reason_counts"]["pairing_fingerprint_mismatch"], 30)

    def test_sorted_input_has_byte_identical_evaluator_output(self):
        first = self.payload()
        second = copy.deepcopy(first)
        second["workloads"].reverse()
        for workload in second["workloads"]:
            workload["results"].reverse()
        self.assertEqual(canonical_json(evaluate(first, self.manifest)),
                         canonical_json(evaluate(second, self.manifest)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
