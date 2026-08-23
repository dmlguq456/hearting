#!/usr/bin/env python3
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

P = Path(__file__).with_name("artifact-quiescence.py")
S = importlib.util.spec_from_file_location("artifact_quiescence_tested", P)
Q = importlib.util.module_from_spec(S)
S.loader.exec_module(Q)


class QuiescenceTest(unittest.TestCase):
    def fixture(self, base: Path):
        artifact_root = base / "artifacts"
        (artifact_root / ".runtime" / "routes").mkdir(parents=True)
        index = base / "resource-runs.index.json"
        index.write_text(json.dumps({"schema_version": 1, "registries": {}}), encoding="utf-8")
        jobs = base / "jobs.log"
        jobs.write_text("", encoding="utf-8")
        return Q.fixture_config(str(artifact_root), str(index), str(jobs))

    def test_zero_pair_is_independent_atomic_and_brackets_fold(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            before = base / "before.json"
            after = base / "after.json"
            first = Q.publish(str(before), config, now - timedelta(seconds=2))
            second = Q.publish(str(after), config, now)
            self.assertTrue(first["proven"] and second["proven"])
            self.assertEqual(first["pending"], sum(first[key] for key in Q.COUNT_KEYS))
            self.assertNotEqual(first["observation_id"], second["observation_id"])
            self.assertFalse(list(base.glob("*.tmp")))
            proof = Q.pair(
                str(before), str(after),
                (now - timedelta(seconds=1.5)).isoformat(),
                (now - timedelta(seconds=.5)).isoformat(),
                now=now, allow_fixture=True,
            )
            self.assertTrue(proof["proven"], proof)
            self.assertFalse(Q.pair(str(before), str(before), now.isoformat(), now.isoformat(),
                                    now=now, allow_fixture=True)["proven"])

    def test_each_open_dimension_prevents_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            route = Path(config["artifact_root"]) / ".runtime" / "routes" / "rt-open.json"
            route.write_text(json.dumps({"route_id": "rt-open", "nodes": []}), encoding="utf-8")
            route_payload = Q.publish(str(base / "route.json"), config, now)
            self.assertEqual(route_payload["open_routes"], 1)
            self.assertFalse(Q.validate(str(base / "route.json"), now=now, allow_fixture=True)["proven"])
            route.unlink()

            identity = Q.RESOURCES.proc_identity(os.getpid())
            registry = base / "resource.json"
            registry.write_text(json.dumps({"schema_version": 1, "runs": {
                "unrelated": {**identity, "status": "running", "started_at": now.timestamp()}
            }}), encoding="utf-8")
            Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1, "registries": {
                "fixture": {"path": str(registry), "registered_at": now.timestamp(), "updated_at": now.timestamp()}
            }}), encoding="utf-8")
            job_payload = Q.publish(str(base / "job.json"), config, now)
            self.assertEqual(job_payload["open_jobs"], 1)
            self.assertFalse(job_payload["proven"])
            Path(config["resource_index"]).write_text(json.dumps({"schema_version": 1, "registries": {}}), encoding="utf-8")

            Path(config["dispatch_jobs"]).write_text(
                "2026-08-21T00:00:00Z\topen\t/repo\t/worktree\tfixture\t"
                "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,route_id=rt-fixture,route_node=test,"
                "attempt_id=att-fixture-open\n", encoding="utf-8")
            dispatch_payload = Q.publish(str(base / "dispatch.json"), config, now)
            self.assertEqual(dispatch_payload["open_dispatch_attempts"], 1, dispatch_payload)
            self.assertFalse(dispatch_payload["proven"])

    def test_missing_malformed_stale_future_offset_counts_and_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            now = datetime.now(timezone.utc)
            evidence = base / "evidence.json"
            original = Q.publish(str(evidence), config, now)
            self.assertTrue(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"])

            cases = []
            malformed = dict(original); malformed["schema_version"] = 999; cases.append(malformed)
            stale = dict(original); stale["observed_at"] = (now - timedelta(hours=1)).isoformat(); cases.append(stale)
            future = dict(original); future["observed_at"] = (now + timedelta(minutes=2)).isoformat(); cases.append(future)
            offsetless = dict(original); offsetless["observed_at"] = now.replace(tzinfo=None).isoformat(); cases.append(offsetless)
            negative = dict(original); negative["open_jobs"] = -1; cases.append(negative)
            wrong_sum = dict(original); wrong_sum["pending"] = 1; cases.append(wrong_sum)
            for payload in cases:
                evidence.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"], payload)

            evidence.write_text(json.dumps(original), encoding="utf-8")
            Path(config["dispatch_jobs"]).write_text("malformed\n", encoding="utf-8")
            self.assertFalse(Q.validate(str(evidence), now=now, allow_fixture=True)["proven"])
            self.assertFalse(Q.validate(str(base / "missing.json"), now=now, allow_fixture=True)["proven"])

    def test_missing_authoritative_source_publishes_false(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            Path(config["resource_index"]).unlink()
            result = Q.publish(str(base / "evidence.json"), config)
            self.assertFalse(result["observation_valid"])
            self.assertFalse(result["proven"])
            self.assertEqual(result["pending"], 0)

    def test_unrelated_root_json_is_evidence_but_malformed_route_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = self.fixture(base)
            artifact_root = Path(config["artifact_root"])
            (artifact_root / "inventory.json").write_text('{"kind":"not-a-route"}', encoding="utf-8")
            valid = Q.publish(str(base / "valid.json"), config)
            self.assertTrue(valid["observation_valid"], valid)
            self.assertTrue(valid["proven"], valid)
            (artifact_root / ".runtime" / "routes" / "broken.json").write_text('{', encoding="utf-8")
            invalid = Q.publish(str(base / "invalid.json"), config)
            self.assertFalse(invalid["observation_valid"], invalid)
            self.assertFalse(invalid["proven"], invalid)


if __name__ == "__main__":
    unittest.main()
