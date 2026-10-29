#!/usr/bin/env python3
"""Bounded regressions for the four backlog performance consumers."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import dispatch_capacity_evidence as CAPACITY
import artifact_admission as ADMISSION
import artifact_index as INDEX
import artifact_producer as PRODUCER

ROOT = Path(__file__).resolve().parents[1]
GOVERNOR_PATH = ROOT / "utilities" / "model-worker-governor.py"
SPEC = importlib.util.spec_from_file_location("model_worker_governor_backlog", GOVERNOR_PATH)
GOVERNOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GOVERNOR)


class BacklogPerformanceTest(unittest.TestCase):
    def test_admission_finalize_verify_and_retry_consume_large_indexes_once(self):
        fixture_path = ROOT / "utilities" / "artifact_producer.test.py"
        fixture_spec = importlib.util.spec_from_file_location("producer_backlog_fixture", fixture_path)
        fixture_module = importlib.util.module_from_spec(fixture_spec)
        assert fixture_spec.loader is not None
        fixture_spec.loader.exec_module(fixture_module)

        for stable_count in (1_000, 10_000, 176_423):
            with self.subTest(stable_rows=stable_count):
                fixture = fixture_module.ProducerTestBase("runTest")
                fixture.setUp()
                try:
                    fixture.activate()
                    event_count = min(58_711, stable_count // 3) if stable_count == 176_423 else 0
                    base = INDEX.empty(fixture_module.ROOT_ID)
                    index = INDEX.IndexDocument(
                        schema_version=base.schema_version,
                        artifact_root_id=base.artifact_root_id,
                        stable_ids={f"stable-{i:06d}": {
                            "kind": "artifact", "cycle_id": f"old-cycle-{i:06d}",
                            "manifest_id": f"old-manifest-{i:06d}"}
                            for i in range(stable_count)},
                        routes={},
                        event_ids={f"event-{i:06d}": {
                            "stream_id": f"stream-{i:06d}", "stream_sequence": 1,
                            "cycle_id": f"old-cycle-{i:06d}"}
                            for i in range(event_count)},
                        streams={f"stream-{i:06d}": {
                            "last_sequence": 1, "cycle_id": f"old-cycle-{i:06d}",
                            "last_event_id": f"event-{i:06d}"}
                            for i in range(event_count)},
                        manifests={}, cycles={},
                    )
                    ADMISSION._write_index(fixture.root, index)
                    input_bytes = ADMISSION._index_path(fixture.root).stat().st_size
                    route, route_file, result = fixture.begin(campaign_key=f"backlog-{stable_count}")
                    fixture.write_output(result)
                    fixture.close(route, route_file)

                    metrics = {"loads": 0, "parses": 0, "row_visits": 0,
                               "canonicalizations": 0, "writes": 0, "write_bytes": 0,
                               "lock_held_seconds": 0.0}
                    lock_started = {}
                    old_load = ADMISSION.load_index
                    parse_name = "_parse_owned" if hasattr(INDEX, "_parse_owned") else "parse"
                    old_parse = getattr(INDEX, parse_name)
                    old_canonical, old_write = INDEX.canonical_bytes, ADMISSION._write_index
                    old_acquire, old_release = ADMISSION._acquire_lock, ADMISSION._release_lock

                    def load(root):
                        metrics["loads"] += 1
                        return old_load(root)

                    def parse(payload):
                        metrics["parses"] += 1
                        metrics["row_visits"] += sum(
                            len(payload.get(section, {})) if isinstance(payload.get(section), dict) else 0
                            for section in ("stable_ids", "event_ids", "streams", "manifests", "cycles")
                        ) + sum(len(rows) for rows in payload.get("routes", {}).values()
                                if isinstance(rows, dict))
                        return old_parse(payload)

                    def canonical(index_doc):
                        metrics["canonicalizations"] += 1
                        return old_canonical(index_doc)

                    def write(root, index_doc):
                        written = old_write(root, index_doc)
                        metrics["writes"] += 1
                        metrics["write_bytes"] += ADMISSION._index_path(root).stat().st_size
                        return written

                    def acquire(root, timeout, now=None):
                        fd = old_acquire(root, timeout, now)
                        lock_started[fd] = time.monotonic()
                        return fd

                    def release(root, fd):
                        metrics["lock_held_seconds"] += time.monotonic() - lock_started.pop(fd)
                        return old_release(root, fd)

                    with mock.patch.object(ADMISSION, "load_index", side_effect=load), \
                         mock.patch.object(INDEX, parse_name, side_effect=parse), \
                         mock.patch.object(INDEX, "canonical_bytes", side_effect=canonical), \
                         mock.patch.object(ADMISSION, "_write_index", side_effect=write), \
                         mock.patch.object(ADMISSION, "_acquire_lock", side_effect=acquire), \
                         mock.patch.object(ADMISSION, "_release_lock", side_effect=release):
                        sealed = PRODUCER.finalize(fixture.root, cycle_id=result["cycle_id"])
                        self.assertEqual(sealed["status"], "sealed")
                        seal_metrics = dict(metrics)
                        record = PRODUCER.read_cycle_record(fixture.root, result["cycle_id"])
                        verified = PRODUCER.verify_finalized_cycle(
                            fixture.root, cycle_id=result["cycle_id"],
                            expected_binding={"cycle_id": result["cycle_id"],
                                              "producer_id": record["producer_id"]},
                        )
                        verify_metrics = {k: metrics[k] - seal_metrics[k] for k in metrics}
                        self.assertTrue(verified["verified"] if "verified" in verified else verified)
                        before_retry = dict(metrics)
                        retried = PRODUCER.finalize(fixture.root, cycle_id=result["cycle_id"])
                        retry_metrics = {k: metrics[k] - before_retry[k] for k in metrics}
                        self.assertIn(retried["status"], {"already-sealed", "sealed"})
                    self.assertLessEqual(seal_metrics["loads"], 1, seal_metrics)
                    self.assertLessEqual(seal_metrics["parses"], 1, seal_metrics)
                    self.assertLessEqual(seal_metrics["writes"], 1, seal_metrics)
                    self.assertEqual(retry_metrics["writes"], 0, retry_metrics)
                    print("M2_METRIC", json.dumps({
                        "stable_rows": stable_count, "event_stream_rows": event_count,
                        "index_bytes_before": input_bytes, "seal": seal_metrics,
                        "verify": verify_metrics, "retry": retry_metrics,
                    }, sort_keys=True))
                finally:
                    fixture.doCleanups()

    def test_capacity_snapshot_rechecks_same_size_rewrite_with_fixed_mtime(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            jobs = Path(temp_dir) / "jobs.log"
            first_line = "2026-10-03T00:00:00Z\tdone\ta\tb\tc\tharness=codex,note=dead-usage-limit,reset=2099-01-01T00:00:00Z\n"
            second_line = "2026-10-03T00:00:00Z\tdone\ta\tb\tc\tharness=codex,note=dead-usage-limit,reset=2099-01-02T00:00:00Z\n"
            self.assertEqual(len(first_line), len(second_line))
            jobs.write_text(first_line, encoding="utf-8")
            frozen_mtime = jobs.stat().st_mtime_ns
            with mock.patch.object(CAPACITY, "_settled", return_value=True), \
                 mock.patch.object(CAPACITY, "_DISK_CACHE", True):
                before = CAPACITY._snapshot(jobs, 1790985600)
                self.assertEqual(before["legacy"][0]["r"], "2099-01-01T00:00:00Z")
                CAPACITY._persist(before)
                original_key = CAPACITY._stat_key(jobs)
                jobs.write_text(second_line, encoding="utf-8")
                os.utime(jobs, ns=(frozen_mtime, frozen_mtime))
                changed_key = CAPACITY._stat_key(jobs)
                self.assertNotEqual(original_key, changed_key)
                after = CAPACITY._snapshot(jobs, 1790985600)
            self.assertEqual(after["legacy"][0]["r"], "2099-01-02T00:00:00Z")

    def test_governor_default_root_resolves_artifact_root_environment(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            root = Path(temp_dir) / ".agent_reports"
            root.mkdir()
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(root)}, clear=False):
                os.environ.pop("AGENT_MODEL_GOVERNOR_ROOT", None)
                self.assertEqual(
                    GOVERNOR.default_root(),
                    root / ".runtime" / "model-worker-governor",
                )

    def test_governor_relative_artifact_hint_uses_canonical_project_root(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            reports = repo / ".agent_reports"
            reports.mkdir()
            utilities = repo / "utilities"
            utilities.mkdir()
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": ".agent_reports"}, clear=False):
                os.environ.pop("AGENT_MODEL_GOVERNOR_ROOT", None)
                with mock.patch.object(GOVERNOR.Path, "cwd", return_value=utilities):
                    self.assertEqual(
                        GOVERNOR.default_root(),
                        reports / ".runtime" / "model-worker-governor",
                    )
                self.assertFalse((utilities / ".agent_reports").exists())

    def test_governor_cli_explicit_root_works_with_relative_artifact_environment(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            fixture = Path(temp_dir)
            explicit_root = fixture / "explicit-governor"
            env = {
                "AGENT_ARTIFACT_ROOT": ".agent_reports",
                "HOME": str(fixture / "home"),
                "PATH": os.defpath,
            }
            result = subprocess.run(
                [sys.executable, str(GOVERNOR_PATH), "--root", str(explicit_root), "status"],
                cwd=fixture,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"leases": {}', result.stdout)
            self.assertTrue((explicit_root / "state.json").is_file())

    def test_governor_cli_help_works_with_relative_artifact_environment(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            fixture = Path(temp_dir)
            env = {
                "AGENT_ARTIFACT_ROOT": ".agent_reports",
                "HOME": str(fixture / "home"),
                "PATH": os.defpath,
            }
            result = subprocess.run(
                [sys.executable, str(GOVERNOR_PATH), "--help"],
                cwd=fixture,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout.lower())
            self.assertFalse((fixture / ".agent_reports").exists())

    def test_governor_cli_implicit_root_resolves_relative_artifact_hint(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            fixture = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(fixture)], check=True)
            reports = fixture / ".agent_reports"
            reports.mkdir()
            caller = fixture / "utilities"
            caller.mkdir()
            env = {
                "AGENT_ARTIFACT_ROOT": ".agent_reports",
                "HOME": str(fixture / "home"),
                "PATH": os.defpath,
            }
            result = subprocess.run(
                [sys.executable, str(GOVERNOR_PATH), "status"],
                cwd=caller,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((reports / ".runtime" / "model-worker-governor" / "state.json").is_file())
            self.assertFalse((caller / ".agent_reports").exists())

    def test_governor_linked_caller_uses_primary_root_and_legacy_fallback(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            base = Path(temp_dir)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            subprocess.run(["git", "init", "-q", str(primary)], check=True)
            subprocess.run(["git", "-C", str(primary), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(primary), "config", "user.name", "Backlog Test"], check=True)
            (primary / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(primary), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(primary), "commit", "-qm", "fixture"], check=True)
            primary_reports = primary / ".agent_reports"
            primary_reports.mkdir()
            subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked", str(linked)], check=True)
            utilities = linked / "utilities"
            utilities.mkdir()
            for caller in (primary, utilities):
                for hint in (None, ".agent_reports"):
                    with self.subTest(caller=caller, artifact_hint=hint):
                        with mock.patch.dict(os.environ, {}, clear=False):
                            os.environ.pop("AGENT_MODEL_GOVERNOR_ROOT", None)
                            if hint is None:
                                os.environ.pop("AGENT_ARTIFACT_ROOT", None)
                            else:
                                os.environ["AGENT_ARTIFACT_ROOT"] = hint
                            with mock.patch.object(GOVERNOR.Path, "cwd", return_value=caller):
                                linked_root = GOVERNOR.default_root()
                                self.assertEqual(linked_root, primary_reports / ".runtime" / "model-worker-governor")
                                GOVERNOR.acquire(linked_root, "dispatch", total=5, budget=10)
                                self.assertTrue((linked_root / "state.json").is_file())
                                self.assertFalse((utilities / ".agent_reports").exists())

            legacy = base / "legacy"
            legacy.mkdir()
            (legacy / ".agent-workspace").write_text("fixture\n", encoding="utf-8")
            legacy_reports = legacy / ".claude_reports"
            legacy_reports.mkdir()
            child = legacy / "utilities"
            child.mkdir()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENT_ARTIFACT_ROOT", None)
                os.environ.pop("AGENT_MODEL_GOVERNOR_ROOT", None)
                with mock.patch.object(GOVERNOR.Path, "cwd", return_value=child):
                    self.assertEqual(
                        GOVERNOR.default_root(),
                        legacy_reports / ".runtime" / "model-worker-governor",
                    )

    def test_governor_standalone_override_is_absolute_and_stable(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp_dir:
            override = Path(temp_dir) / "isolated" / "governor"
            with mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_ROOT": str(override)}, clear=False):
                os.environ.pop("AGENT_ARTIFACT_ROOT", None)
                self.assertEqual(GOVERNOR.default_root(), override.resolve())
            with mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_ROOT": "relative-governor"}, clear=False):
                self.assertEqual(GOVERNOR.default_root(), (Path.cwd() / "relative-governor").resolve())


if __name__ == "__main__":
    unittest.main()
