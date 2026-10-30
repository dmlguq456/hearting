#!/usr/bin/env python3
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import resource_progress as progress
import resource_metric_sources as metric_sources


def _cairn_problems(record_path):
    """Python port of the Cairn run-discovery constraints (read-only reference:
    cairn/lib/experiments/run-discovery.ts, file size :76/:275, schema_version :566,
    output_dir :571-573, metrics :354-413, epoch-json-files folders :385-397)."""
    import re
    problems = []
    raw = Path(record_path).read_bytes()
    if len(raw) > 16 * 1024:
        problems.append(f"size {len(raw)} > 16384")
    try:
        record = json.loads(raw.decode("utf-8"))
    except ValueError:
        return problems + ["not-json"]
    if record.get("schema_version") not in (None, 1):
        problems.append("schema_version")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(record.get("run_id"))):
        problems.append("run_id")
    output_dir = record.get("output_dir")
    if not (isinstance(output_dir, str) and os.path.isabs(output_dir) and os.path.isdir(output_dir)):
        problems.append("output_dir")
    metrics = record.get("metrics")
    if not isinstance(metrics, list) or len(metrics) > 128:
        return problems + ["metrics"]
    merged, normalized = set(), 0
    for item in metrics:
        if item.get("format") not in {"epochs-jsonl", "epoch-json-files", "tfevents"}:
            problems.append("format")
        for key in item.get("keys") or []:
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                problems.append("key")
        path = item.get("path")
        if isinstance(path, list):
            if len(path) > 1024:
                problems.append("path-array")
            merged.add((item.get("format"), output_dir))
            continue
        normalized += 1
        if not isinstance(path, str) or not os.path.exists(path):
            problems.append("path-missing:" + str(path))
        elif item["format"] == "epoch-json-files":
            if not os.path.isdir(path) or not any(
                    re.fullmatch(r"(train|valid)-epoch-\d+\.json", name) for name in os.listdir(path)):
                problems.append("epoch-folder:" + str(path))
        elif item["format"] == "epochs-jsonl" and not os.path.isfile(path):
            problems.append("jsonl-not-file")
    if normalized + len(merged) > 16:
        problems.append("normalized>16")
    return problems


def _legacy_epoch_run(base, name_pad=100):
    """A run folder with 170 epoch files and a tensorboard folder."""
    run = Path(base) / ("run-" + "x" * name_pad)
    (run / "tensorboard").mkdir(parents=True)
    for epoch in range(1, 171):
        for split in ("train", "valid"):
            (run / f"{split}-epoch-{epoch:04d}.json").write_text(json.dumps(
                {"epoch": epoch, "losses": {"L_se": -epoch / 10, "SISNRi": epoch / 5},
                 "seconds": 3.0}))
    (run / "tensorboard" / "events.out.tfevents.1.host").write_bytes(b"x")
    return run


class ResourceProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "configs").mkdir(parents=True)
        self.config = self.repo / "configs" / "a.json"
        self.config.write_text(json.dumps({"training": {"attempts": 400000}}))
        self.digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.directory = self.root / "runs" / "first"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "progress.json"
        self.counter = {"attempt": 27346, "successful": 27342, "skipped": 4,
                        "last": {"attempt": 27346, "metrics": {"loss": 0.0048248}}}
        self.path.write_text(json.dumps(self.counter))
        self.now = time.time()
        os.utime(self.path, (self.now - 600, self.now - 600))
        self.wrapper = {"pid": 4, "starttime": "10", "command_hash": "a" * 64,
                        "process_group": 4, "cwd": str(self.repo), "argv": ["wrapper"]}
        self.child = {"pid": 42, "starttime": "11", "command_hash": "b" * 64,
                      "process_group": 4, "cwd": str(self.repo),
                      "argv": ["python", "run.py", "--config", str(self.config)]}
        self.run = dict(self.wrapper)
        self.arm = {"name": "first", "state": "training-updates", "directory": str(self.directory),
                    "config_sha256": self.digest, "process_identity": {
                        "pid": 42, "starttime": 11, "cmdline_sha256": "b" * 64}}
        self.metadata = {"arms": [self.arm]}
        self.write_metadata()

    def write_metadata(self):
        (self.root / "run.json").write_text(json.dumps(self.metadata))

    def reader(self, pid):
        return copy.deepcopy({4: self.wrapper, 42: self.child}.get(pid))

    def collect(self, reader=None):
        return progress.collect(self.run, self.root / "resource-runs.json", self.now,
                                process_reader=reader or self.reader)

    def set_training(self, training):
        self.config.write_text(json.dumps({"training": training}))
        self.arm["config_sha256"] = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.write_metadata()

    def test_explicit_schedule_keeps_attempt_success_skip_loss_and_json_precision(self):
        self.set_training({"attempts": 400000, "epochs": 20,
                           "blocks_per_epoch": 1000, "updates_per_block": 20})
        loss = 0.00711713331369082
        self.counter = {"attempt": 59415, "successful": 59397, "skipped": 18,
                        "last": {"attempt": 59415, "metrics": {"loss": loss}}}
        self.path.write_text(json.dumps(self.counter))
        value = self.collect()
        self.assertEqual(value["schedule_epoch"], {
            "current": 3, "total": 20, "attempts_per_epoch": 20000,
            "completed": 2, "attempt_in_epoch": 19415, "state": "partial"})
        self.assertEqual((value["attempt"], value["successful"], value["skipped"]),
                         (59415, 59397, 18))
        self.assertEqual(json.loads(json.dumps(value))["loss"], loss)
        self.assertEqual(value["loss_kind"], "last-batch")
        self.assertEqual(value["config_sha256"], self.arm["config_sha256"])

    def test_schedule_start_partial_boundaries_and_complete(self):
        self.set_training({"attempts": 400000, "epochs": 20,
                           "blocks_per_epoch": 1000, "updates_per_block": 20})
        cases = ((0, 0, 0, 0, "start"), (1, 1, 0, 1, "partial"),
                 (19999, 1, 0, 19999, "partial"), (20000, 1, 1, 0, "boundary"),
                 (20001, 2, 1, 1, "partial"), (400000, 20, 20, 0, "complete"))
        for attempt, current, completed, position, state in cases:
            with self.subTest(attempt=attempt):
                self.counter.update(attempt=attempt, successful=attempt, skipped=0,
                                    last={"attempt": attempt, "metrics": {"loss": .001}})
                self.path.write_text(json.dumps(self.counter))
                epoch = self.collect()["schedule_epoch"]
                self.assertEqual((epoch["current"], epoch["completed"],
                                  epoch["attempt_in_epoch"], epoch["state"]),
                                 (current, completed, position, state))

    def test_missing_ambiguous_and_mismatched_cadence_keeps_step_and_loss(self):
        exact = {"attempts": 400000, "epochs": 20,
                 "blocks_per_epoch": 1000, "updates_per_block": 20}
        cases = [{key: value for key, value in exact.items() if key != missing}
                 for missing in ("epochs", "blocks_per_epoch", "updates_per_block")]
        cases += [{**exact, key: value} for key in ("epochs", "blocks_per_epoch", "updates_per_block")
                  for value in (0, -1, True, "20", 20.0, 2**63)]
        cases.append({**exact, "epochs": 21})
        for training in cases:
            with self.subTest(training=training):
                self.set_training(training)
                value = self.collect()
                self.assertIsNotNone(value)
                self.assertNotIn("schedule_epoch", value)
                self.assertEqual((value["attempt"], value["attempt_total"], value["loss"]),
                                 (27346, 400000, .0048248))

    def test_attempt_denominator_last_batch_and_stale_progress_are_independent(self):
        value = self.collect()
        self.assertEqual((value["attempt"], value["attempt_total"], value["successful"], value["skipped"]),
                         (27346, 400000, 27342, 4))
        self.assertAlmostEqual(value["percent"], 6.8365)
        self.assertEqual((value["loss"], value["loss_kind"]), (0.0048248, "last-batch"))
        self.assertAlmostEqual(value["progress_age_s"], 600, places=4)
        self.assertLess(self.now - value["metadata_updated_at"], 2)
        self.assertEqual(value["config_sha256"], self.digest)
        self.assertEqual(value["config_ref"], "config:a.json")
        self.assertNotIn("eta", value)
        self.assertNotIn("epoch", value)

    def test_next_arm_uses_its_actual_config_instead_of_registered_baseline(self):
        other = self.repo / "configs" / "next.json"
        other.write_text(json.dumps({"training": {"attempts": 90000}}))
        self.child["argv"] = ["python", "run.py", "--config=" + str(other)]
        self.arm["config_sha256"] = hashlib.sha256(other.read_bytes()).hexdigest()
        self.arm["name"] = "next"
        self.write_metadata()
        value = self.collect()
        self.assertEqual((value["arm"], value["attempt_total"], value["config_ref"]),
                         ("next", 90000, "config:next.json"))

    def test_hash_mismatch_and_unbound_config_fail_soft(self):
        self.config.write_text(json.dumps({"training": {"attempts": 800000}}))
        self.assertIsNone(self.collect())
        self.child["argv"] = ["python", "run.py"]
        self.assertIsNone(self.collect())
        self.child["argv"] = ["python", "run.py", "--config", str(self.config), "--config=a.json"]
        self.assertIsNone(self.collect())

    def test_config_outside_normal_resolver_root_is_not_read(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"training": {"attempts": 123}}))
        self.child["argv"][-1] = str(outside)
        self.arm["config_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
        self.write_metadata()
        self.assertIsNone(self.collect())

    def test_successful_target_is_not_an_attempt_target(self):
        for training in ({"successful_updates": 400000}, {"epochs": 20, "updates_per_block": 20},
                         {"attempts": True}, {"attempts": 2}):
            with self.subTest(training=training):
                self.config.write_text(json.dumps({"training": training}))
                self.arm["config_sha256"] = hashlib.sha256(self.config.read_bytes()).hexdigest()
                self.write_metadata()
                self.assertIsNone(self.collect())

    def test_loss_from_a_different_attempt_is_omitted(self):
        self.counter["last"]["attempt"] -= 1
        self.path.write_text(json.dumps(self.counter))
        value = self.collect()
        self.assertIsNotNone(value)
        self.assertIsNone(value["loss"])
        self.assertIsNone(value["loss_kind"])

    def test_contradictory_counter_and_nonfinite_json_are_unsupported(self):
        self.counter["successful"] += 1
        self.path.write_text(json.dumps(self.counter))
        self.assertIsNone(self.collect())
        self.counter["successful"] -= 1
        self.counter["last"]["metrics"]["loss"] = float("nan")
        self.path.write_text(json.dumps(self.counter))
        self.assertIsNone(self.collect())

    def test_reused_child_changed_group_and_wrapper_change_do_not_attach(self):
        for key, value in (("starttime", "12"), ("command_hash", "c" * 64), ("process_group", 5)):
            old = self.child[key]
            self.child[key] = value
            self.assertIsNone(self.collect())
            self.child[key] = old
        calls = 0
        def raced(pid):
            nonlocal calls
            if pid == 4:
                calls += 1
                if calls > 1:
                    return {**self.wrapper, "starttime": "20"}
            return self.reader(pid)
        self.assertIsNone(self.collect(raced))

    def test_symlink_oversize_and_duplicate_metadata_do_not_attach(self):
        raw = self.path.read_bytes()
        self.path.unlink()
        other = self.root / "other.json"
        other.write_bytes(raw)
        self.path.symlink_to(other)
        self.assertIsNone(self.collect())
        self.path.unlink()
        self.path.write_bytes(b" " * (progress.MAX_BYTES + 1))
        self.assertIsNone(self.collect())
        self.path.write_bytes(raw)
        (self.root / "run.json").write_text('{"arms":[],"arms":[]}')
        self.assertIsNone(self.collect())

    def test_foreign_progress_directory_and_ambiguous_arms_fail_soft(self):
        self.arm["directory"] = str(self.repo.parent / ".." / "other")
        self.write_metadata()
        self.assertIsNone(self.collect())
        self.arm["directory"] = str(self.directory)
        self.metadata["arms"].append(copy.deepcopy(self.arm))
        self.write_metadata()
        self.assertIsNone(self.collect())


class ResourceProgressProductionTest(unittest.TestCase):
    """Bridge arms[].production: local unwrap vs remote-origin guard.

    Mirrors tf-rehancer.canonical-resource-bridge/v1 (cnn T/B arms): child
    identity/directory/config live inside arm["production"] with an explicit
    host. Remote-origin arms must never be resolved in local /proc and their
    metadata alone never proves live/done.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "configs").mkdir(parents=True)
        self.config = self.repo / "configs" / "a.json"
        self.config.write_text(json.dumps({"training": {"attempts": 400000}}))
        self.digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.directory = self.root / "runs" / "first"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "progress.json"
        self.path.write_text(json.dumps({
            "attempt": 27346, "successful": 27342, "skipped": 4,
            "last": {"attempt": 27346, "metrics": {"loss": 0.0048248}}}))
        self.now = time.time()
        os.utime(self.path, (self.now - 600, self.now - 600))
        self.wrapper = {"pid": 4, "starttime": "10", "command_hash": "a" * 64,
                        "process_group": 4, "cwd": str(self.repo), "argv": ["wrapper"]}
        self.child = {"pid": 42, "starttime": "11", "command_hash": "b" * 64,
                      "process_group": 4, "cwd": str(self.repo),
                      "argv": ["python", "run.py", "--config", str(self.config)]}
        self.run = dict(self.wrapper)
        self.lookups = []

    def write_metadata(self, metadata):
        (self.root / "run.json").write_text(json.dumps(metadata))

    def reader(self, pid):
        self.lookups.append(pid)
        return copy.deepcopy({4: self.wrapper, 42: self.child}.get(pid))

    def collect(self, metadata):
        self.lookups = []
        self.write_metadata(metadata)
        return progress.collect(self.run, self.root / "resource-runs.json", self.now,
                                process_reader=self.reader)

    def local_nested(self):
        return {"capacity": "T", "state": "training-updates",
                "run_id": "local-20261006-t",
                "production": {
                    "schema": "tf-rehancer.cnn-production/v1",
                    "capacity": "T", "gpu_index": 0,
                    "directory": str(self.directory),
                    "config_sha256": self.digest, "state": "training-updates",
                    "run_name": "t_local",
                    "compute_run_id": "local-20261006-t",
                    "process_identity": {"pid": 42, "starttime": 11,
                                         "cmdline_sha256": "b" * 64}}}

    def remote_arm(self, pid=3945450, start=565464077, capacity="T", gpu=0,
                   run="t_baseline_20261006", receipt_host="cnn"):
        return {"capacity": capacity, "state": "training-updates",
                "run_id": "cnn-20261006-015922-tf-canonical-%s-scratch400k-a1"
                          % capacity.lower(),
                "compute_host_receipt": {
                    "host": receipt_host, "gpus": str(gpu),
                    "run_id": "cnn-20261006-015922-tf-canonical-%s-scratch400k-a1"
                              % capacity.lower()},
                "production": {
                    "schema": "tf-rehancer.cnn-production/v1", "host": "cnn",
                    "capacity": capacity, "gpu_index": gpu,
                    "directory": "/home/nas/user/Uihyeop/NN_Zoo/TF-Rehancer_artifacts"
                                 "/cnn_canonical_20261006/recovery-a1/%s/runs/%s"
                                 % (capacity.lower(), run),
                    "config_sha256": "9" * 64, "state": "training-updates",
                    "run_name": run,
                    "compute_run_id": "cnn-20261006-015922-tf-canonical-%s-scratch400k-a1"
                                      % capacity.lower(),
                    "wrapper_identity": {"pid": 3945423, "starttime": 565463952,
                                         "cmdline_sha256": "c" * 64},
                    "process_identity": {"pid": pid, "starttime": start,
                                         "cmdline_sha256": "d" * 64},
                    "progress": {"attempt": 43915, "successful": 43904,
                                 "skipped": 11}}}

    def test_local_nested_production_unwraps_with_origin(self):
        value = self.collect({"arms": [self.local_nested()]})
        self.assertIsNotNone(value)
        self.assertEqual((value["arm"], value["attempt"], value["config_sha256"]),
                         ("t_local", 27346, self.digest))
        origin = value.get("production_origin")
        self.assertIsNotNone(origin)
        self.assertEqual(origin.get("compute_run_id"), "local-20261006-t")
        self.assertNotIn("host", origin)
        self.assertEqual(set(self.lookups), {4, 42})

    def test_remote_nested_production_never_looked_up(self):
        arm = self.remote_arm()
        self.assertIsNone(self.collect({"arms": [arm]}))
        self.assertEqual(self.lookups, [4])
        self.assertNotIn(3945450, self.lookups)
        self.assertNotIn(3945423, self.lookups)
        # Receipt-only host marks remote origin too.
        arm = self.local_nested()
        arm["compute_host_receipt"] = {"host": "cnn"}
        arm["production"] = {**arm["production"],
                             "process_identity": {"pid": 3945450, "starttime": 565464077,
                                                  "cmdline_sha256": "d" * 64}}
        self.assertIsNone(self.collect({"arms": [arm]}))
        self.assertNotIn(3945450, self.lookups)

    def test_two_remote_arms_fail_soft_without_lookup(self):
        metadata = {"arms": [self.remote_arm(pid=3945450, start=565464077,
                                             capacity="T", gpu=0,
                                             run="t_baseline_20261006"),
                             self.remote_arm(pid=3945451, start=565464078,
                                             capacity="B", gpu=1,
                                             run="b_baseline_20261006")]}
        self.assertIsNone(self.collect(metadata))
        self.assertEqual(self.lookups, [4])
        self.assertNotIn(3945450, self.lookups)
        self.assertNotIn(3945451, self.lookups)

    def test_mixed_local_and_remote_attaches_local_only(self):
        plain = {"name": "first", "state": "training-updates",
                 "directory": str(self.directory), "config_sha256": self.digest,
                 "process_identity": {"pid": 42, "starttime": 11,
                                      "cmdline_sha256": "b" * 64}}
        value = self.collect({"arms": [plain, self.remote_arm()]})
        self.assertIsNotNone(value)
        self.assertEqual(value["arm"], "first")
        self.assertNotIn("production_origin", value)
        self.assertNotIn(3945450, self.lookups)

    def test_plain_arm_with_remote_receipt_never_looked_up(self):
        arm = {"name": "foreign-receipt-arm", "state": "training-updates",
               "directory": str(self.directory), "config_sha256": self.digest,
               "process_identity": {"pid": 42, "starttime": 11,
                                    "cmdline_sha256": "b" * 64},
               "compute_host_receipt": {"host": "cnn", "gpus": "0"}}
        self.assertIsNone(self.collect({"arms": [arm]}))
        self.assertEqual(self.lookups, [4])
        self.assertNotIn(42, self.lookups)

    def test_duplicate_local_nested_arms_stay_ambiguous(self):
        arm = self.local_nested()
        self.assertIsNone(self.collect({"arms": [arm, copy.deepcopy(arm)]}))


class RemoteCandidatesTest(unittest.TestCase):
    """Display-only remote arms: multi-arm join input, never liveness proof.

    Mirrors the cnn T/B bridge: producer identity/directory/config live in
    arm["production"] with an explicit host, progress/config files stay
    hash-checked, and no local /proc lookup can occur (no reader exists).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "configs").mkdir(parents=True)
        self.cfg = self.root / "outside-cfg"
        self.cfg.mkdir()
        self.config = self.cfg / "scratch_t.json"
        self.config.write_text(json.dumps({"training": {"attempts": 400000, "epochs": 20,
                                                         "blocks_per_epoch": 1000,
                                                         "updates_per_block": 20}}))
        self.digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.now = time.time()
        self.run = {"pid": 4, "starttime": "10", "command_hash": "a" * 64,
                    "process_group": 4, "cwd": str(self.repo)}

    def arm(self, capacity="T", pid=3945450, start=565464077, host="cnn",
            run="t_baseline_20261006", gpu=0, attempt=67117, successful=67096,
            skipped=21, loss=0.00985, set_config=True):
        directory = self.root / ("runs-%s" % capacity.lower()) / run
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "progress.json").write_text(json.dumps({
            "attempt": attempt, "successful": successful, "skipped": skipped,
            "last": {"attempt": attempt, "metrics": {"loss": loss}}}))
        os.utime(directory / "progress.json", (self.now - 600, self.now - 600))
        config_sha = self.digest if set_config else "0" * 64
        return {"capacity": capacity, "state": "training-updates",
                "run_id": "cnn-20261006-%s" % capacity.lower(),
                "compute_host_receipt": {"host": host},
                "production": {
                    "schema": "tf-rehancer.cnn-production/v1", "host": host,
                    "capacity": capacity, "gpu_index": gpu,
                    "directory": str(directory), "config_sha256": config_sha,
                    "state": "training-updates", "run_name": run,
                    "compute_run_id": "cnn-20261006-%s" % capacity.lower(),
                    "command": ["/bin/tf-rehancer-train", "--config", str(self.config)],
                    "process_identity": {"pid": pid, "starttime": start,
                                         "cmdline_sha256": "d" * 64}}}

    def collect_remote(self, metadata):
        (self.root / "run.json").write_text(json.dumps(metadata))
        return progress.remote_candidates(self.run, self.root / "resource-runs.json",
                                          self.now)

    def test_two_remote_arms_yield_two_display_candidates(self):
        found = self.collect_remote({"arms": [self.arm("T"), self.arm(
            "B", pid=3945451, start=565464078, run="b_baseline_20261006", gpu=1,
            successful=67100, skipped=17)]})
        self.assertEqual(len(found), 2)
        by_cap = {item["capacity"]: item for item in found}
        self.assertEqual((by_cap["T"]["attempt"], by_cap["T"]["attempt_total"],
                          by_cap["T"]["successful"], by_cap["T"]["skipped"]),
                         (67117, 400000, 67096, 21))
        self.assertEqual(by_cap["T"]["schedule_epoch"]["current"], 4)
        self.assertEqual(by_cap["T"]["loss_kind"], "last-batch")
        for item in found:
            self.assertTrue(item["remote"])
            self.assertEqual(item["host"], "cnn")
            self.assertEqual(item["config_ref"], "config:scratch_t.json")
            self.assertIsNotNone(item["production_origin"])

    def test_counter_config_and_local_shapes_fail_soft(self):
        good = self.arm("T")
        bad_counter = self.arm("B", pid=3945451, start=565464078,
                               run="b_baseline_20261006", gpu=1,
                               successful=67100, skipped=18)
        bad_config = self.arm("T", pid=3945452, start=565464079)
        bad_config["production"] = {**bad_config["production"]}
        bad_config["production"]["config_sha256"] = "0" * 64
        plain = {"name": "first", "state": "training-updates",
                 "directory": str(self.root), "config_sha256": self.digest,
                 "process_identity": {"pid": 42, "starttime": 11,
                                      "cmdline_sha256": "b" * 64}}
        found = self.collect_remote({"arms": [good, bad_counter, bad_config, plain]})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["capacity"], "T")

    def test_same_pid_on_other_hosts_stays_host_qualified(self):
        first = self.arm("T", pid=42, start=11)
        second = self.arm("B", pid=42, start=11, host="moving4",
                          run="b_baseline_20261006", gpu=1)
        found = self.collect_remote({"arms": [first, second]})
        self.assertEqual([(item["host"], item["pid"]) for item in found],
                         [("cnn", 42), ("moving4", 42)])


class DeclaredResourceProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "logs" / "eval.log.progress.json"
        self.run = {"run_id": "eval-1", "node": "eval-run", "progress_file": str(self.path)}
        self.env = progress.environment(self.run)

    def write(self, completed=12, total=50, unit="epoch", detail=None):
        with mock.patch.dict(os.environ, self.env), mock.patch.object(progress.time, "time", return_value=100.0):
            return progress.write_progress(completed, unit, total=total, detail=detail)

    def test_atomic_roundtrip_and_old_update_age(self):
        self.assertTrue(self.write(detail="validation"))
        value = progress.read_progress(self.run, 220.0)
        self.assertEqual((value["run_id"], value["node"], value["completed"], value["total"], value["unit"]),
                         ("eval-1", "eval-run", 12, 50, "epoch"))
        self.assertEqual((value["updated_at"], value["age_s"], value["detail"]), (100.0, 120.0, "validation"))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertTrue(self.write(13, total=None))
        self.assertNotIn("total", progress.read_progress(self.run, 10000.0))
        self.assertEqual(progress.read_progress(self.run, 10000.0)["age_s"], 9900.0)

    def test_absent_partial_and_broken_json_are_only_unavailable(self):
        self.assertIsNone(progress.read_progress(self.run, 220))
        self.path.parent.mkdir()
        for raw in (b"", b"{", b'{"completed": 12,', b"not json", b"[]", b"\xff",
                    b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assertIsNone(progress.read_progress(self.run, 220))

    def test_wrong_run_or_node_is_not_attributed(self):
        self.assertTrue(self.write())
        for wrong in ({**self.run, "run_id": "other"}, {**self.run, "node": "full-run"}):
            self.assertIsNone(progress.read_progress(wrong, 220))

    def test_invalid_numbers_or_multiline_text_are_unavailable(self):
        self.assertTrue(self.write())
        valid = json.loads(self.path.read_text())
        for key, bad in (("completed", True), ("completed", -1), ("completed", 1.5),
                         ("completed", 2**63), ("total", 1), ("total", False),
                         ("updated_at", float("nan")), ("updated_at", float("inf")),
                         ("updated_at", -1), ("unit", "epoch\nnext"), ("unit", "epoch\u2028next"),
                         ("detail", "x" * 241), ("schema_version", True)):
            with self.subTest(key=key, bad=bad):
                self.path.write_text(json.dumps({**valid, key: bad}))
                self.assertIsNone(progress.read_progress(self.run, 220))

    def test_declared_file_only_bounded_and_regular(self):
        self.assertTrue(self.write())
        with mock.patch.object(progress, "_read", wraps=progress._read) as reader:
            self.assertIsNotNone(progress.read_progress(self.run, 220))
            reader.assert_called_once_with(self.path, Path(self.path.anchor))
        self.path.write_bytes(b" " * (progress.MAX_BYTES + 1))
        self.assertIsNone(progress.read_progress(self.run, 220))
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertIsNone(progress.read_progress(self.run, 220))
        self.path.unlink()
        target = self.root / "other.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        self.assertIsNone(progress.read_progress(self.run, 220))

    def test_missing_environment_and_failed_replace_do_not_interrupt_workload(self):
        with mock.patch.dict(os.environ, {key: "" for key in self.env}):
            self.assertFalse(progress.write_progress(1, "item"))
        self.assertFalse(self.path.parent.exists())
        self.assertTrue(self.write())
        before = self.path.read_bytes()
        with mock.patch.object(progress.os, "replace", side_effect=OSError("unwritable")):
            self.assertFalse(self.write(13))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertFalse(self.write(-1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_progress_never_changes_liveness_or_completion(self):
        import resource_run_registry as registry
        identity = {"pid": 99999999, "starttime": "1", "command_hash": "a" * 64}
        run = {**self.run, **identity, "process_group": identity["pid"], "status": "running"}
        self.assertTrue(self.write(50))  # a complete counter is not a successful exit
        with mock.patch.object(progress, "collect", return_value=None), \
             mock.patch.object(progress, "remote_candidates", return_value=[]):
            observed = registry.normalize_run("eval-1", run, self.root / "runs.json",
                                             identity_reader=lambda _pid: identity, now=10000)
        self.assertEqual((observed["liveness"], observed["registry_status"], observed["exit_code"]),
                         ("working", "running", None))
        self.assertEqual(observed["progress"]["age_s"], 9900)
        self.path.write_text("{")
        exited = registry.normalize_run("eval-1", {**run, "status": "succeeded", "exit_code": 0},
                                        self.root / "runs.json", identity_reader=lambda _pid: None, now=10000)
        self.assertEqual((exited["progress"], exited["liveness"], exited["exit_code"]), (None, "exited", 0))

    def test_observer_production_budget_advances_through_large_changing_bundle(self):
        base = self.root / "production-admission-continuation"
        bundle = base / "bundle"
        bundle.mkdir(parents=True)
        registry = base / "resource-runs.json"
        registry.write_text("{}\n")
        state_path = self.root / "production-admission-state" / "observation.json"
        state_path.parent.mkdir()
        run_id = "production-budget-exact-run"
        expected = {"admitted": 0, "denied": 0}

        def write_decision(path, sample_index, stamp, admitted):
            document = {"admitted": admitted, "sample_index": sample_index,
                "snapshot": {"t": stamp}, "padding": "x" * 2200}
            path.write_text(json.dumps(document))
            expected["admitted" if admitted else "denied"] += 1

        for index in range(500):
            write_decision(bundle / f"decision-{index:04d}.json",
                index, 100 + index, index % 10 != 0)
        identity = bundle / "zzzz-exact-identity.json"
        identity.write_text(json.dumps({"run_id": run_id, "started_at": 1}))
        sample_size = (bundle / "decision-0000.json").stat().st_size
        self.assertGreater(sample_size * 500,
                           metric_sources.MAX_ADMISSION_BYTES_PER_PASS)
        self.assertEqual(metric_sources.MAX_ADMISSION_BYTES_PER_PASS, 1024 * 1024)
        self.assertEqual(metric_sources.MAX_ADMISSION_ENTRIES, 4096)
        self.assertEqual(metric_sources.ADMISSION_TIME_BUDGET_SECONDS, 0.250)
        registry_before = registry.read_bytes()
        run = {"run_id": run_id, "started_at": 1}
        summaries = []
        foreign_path = bundle / "!foreign-identity.json"
        foreign_seen = False
        foreign_injected = False

        def observe(poll):
            return progress.observe_experiment(run, registry, 5000 + 60 * poll,
                row_key="run", started_at=1, notify_events=False)

        def current_binding():
            if not state_path.exists():
                return {}
            saved = progress._read_observation_state(state_path)
            cursor = saved.get("admission_cursor", {})
            scan = cursor.get("scan", {}).get(str(base), {})
            return scan.get("bundle_cursors", {}).get("bundle", {}).get(
                "binding_state", {})

        with mock.patch.object(progress, "_observation_state_path",
                                return_value=state_path):
            for poll in range(30):
                write_decision(bundle / f"!decision-before-{poll:03d}.json",
                    1000 + poll * 2, 700 + poll * 2, poll % 2 == 0)
                write_decision(bundle / f"decision-z-after-{poll:03d}.json",
                    1001 + poll * 2, 701 + poll * 2, poll % 2 != 0)
                if poll == 4:
                    identity.unlink()
                elif poll == 5:
                    identity.write_text(json.dumps({"run_id": run_id, "started_at": 1}))
                if foreign_seen:
                    foreign_path.unlink()
                    foreign_seen = False
                elif (poll >= 6 and not foreign_injected and not foreign_path.exists()
                      and current_binding().get("phase") == "decisions"
                      and current_binding().get("binding_complete") is True):
                    foreign_path.write_text(json.dumps({
                        "run_id": "foreign-budget-sibling", "started_at": 1}))
                    foreign_seen = True
                    foreign_injected = True
                result = observe(poll)
                self.assertTrue(result["available"])
                if poll == 4:
                    self.assertNotEqual(result["admission"]["coverage"], "observed")
                    self.assertEqual(result["admission"]["state"], "unknown")
                    invalidated = progress._read_observation_state(state_path)
                    self.assertTrue(invalidated["admission_cursor"][
                        "generation_revalidation_required"])
                    self.assertTrue(any("admission-bundle-generation-changed" in warning
                                        for warning in result["warnings"]))
                if foreign_seen:
                    self.assertEqual(result["admission"]["state"], "unknown")
                    conflicted = progress._read_observation_state(state_path)
                    conflict_binding = conflicted["admission_cursor"]["scan"][
                        str(base)]["bundle_cursors"]["bundle"]["binding_state"]
                    self.assertTrue(conflict_binding["foreign_seen"])
                summaries.append(result["admission"])

            final = summaries[-1]
            for poll in range(30, 60):
                if (final["coverage"] == "observed"
                        and final["sample_count"] == sum(expected.values())):
                    break
                result = observe(poll)
                final = result["admission"]
                summaries.append(final)

            state = progress._read_observation_state(state_path)
            cursor = state["admission_cursor"]
            root_state = cursor["scan"][str(base)]
            bundle_state = root_state["bundle_cursors"]["bundle"]
            binding = bundle_state["binding_state"]
            self.assertEqual(final["coverage"], "observed")
            self.assertEqual(final["sample_count"], sum(expected.values()))
            self.assertEqual(final["admitted_samples"], expected["admitted"],
                             f"summary={final!r} expected={expected!r} "
                             f"persisted={state.get('admission_counts')!r} "
                             f"history={[(row['coverage'], row['sample_count'], row['admitted_samples'], row['denied_samples'], row['unknown_samples']) for row in summaries]!r}")
            self.assertEqual(final["denied_samples"], expected["denied"],
                             f"summary={final!r} expected={expected!r} "
                             f"persisted={state.get('admission_counts')!r} "
                             f"history={[(row['coverage'], row['sample_count'], row['admitted_samples'], row['denied_samples'], row['unknown_samples']) for row in summaries]!r}")
            self.assertEqual(final["unknown_samples"], 0)
            self.assertTrue(binding["target_seen"])
            self.assertFalse(binding["foreign_seen"])
            self.assertEqual(binding["phase"], "decisions")
            self.assertTrue(binding["binding_complete"])
            self.assertGreater(final["sample_count"], summaries[0]["sample_count"])

            stable_ids = list(cursor["sample_ids"])
            replay = observe(len(summaries) + 20)
            replay_state = progress._read_observation_state(state_path)
            self.assertEqual(replay["admission"]["sample_count"], final["sample_count"])
            self.assertEqual(replay["admission"]["admitted_samples"], expected["admitted"])
            self.assertEqual(replay["admission"]["denied_samples"], expected["denied"])
            self.assertEqual(replay_state["admission_cursor"]["sample_ids"], stable_ids)

        self.assertEqual(registry.read_bytes(), registry_before)


class ExperimentObservationTest(unittest.TestCase):
    def test_first_selected_population_drops_unbound_unknown_accumulation(self):
        previous = {"admission_counts": {"admitted": 0, "denied": 0, "unknown": 223},
            "admission_generation": None, "admission_from_at": 10,
            "admission_to_at": 90, "admission_current_state": "unknown"}
        decoded = {"generation": "selected-source", "coverage": "partial",
            "samples": [{"state": "denied", "at": 100, "sample_id": "new-page"}],
            "latest": {"state": "denied", "at": 100, "sample_id": "new-page"},
            "cursor": {"schema_version": 5}, "warnings": []}
        summary, state = progress._admission_summary(previous, decoded, 110,
            "run-a", "run", 0)
        self.assertEqual((summary["admitted_samples"], summary["denied_samples"],
                          summary["unknown_samples"]), (0, 1, 0))
        self.assertEqual((summary["observed_from_at"], summary["observed_to_at"]),
                         (100, 100))
        self.assertEqual(state["admission_generation"], "selected-source")

    def test_admission_generation_replacement_resets_counts_and_survives_unknown_gap(self):
        previous = {"admission_counts": {"admitted": 4, "denied": 6, "unknown": 1},
            "admission_generation": "generation-a", "admission_from_at": 10,
            "admission_to_at": 90, "admission_current_state": "waiting",
            "admission_wait_episode_id": "old-episode"}
        decoded = {"generation": "generation-b", "coverage": "observed",
            "samples": [{"state": "admitted", "at": 1000, "sample_id": "b1"}],
            "latest": {"state": "admitted", "at": 1000, "sample_id": "b1"},
            "cursor": {"schema_version": 4}, "warnings": []}
        summary, state = progress._admission_summary(previous, decoded, 1000,
            "run-a", "row-a", 0)
        self.assertEqual((summary["admitted_samples"], summary["denied_samples"],
                          summary["unknown_samples"]), (1, 0, 0))
        self.assertEqual(state["admission_generation"], "generation-b")
        missing_summary, missing_state = progress._admission_summary(state,
            {"generation": None, "coverage": "partial", "samples": [],
             "latest": None, "cursor": state["admission_cursor"], "warnings": []},
            1001, "run-a", "row-a", 0)
        self.assertEqual(missing_summary["state"], "unknown")
        self.assertEqual(missing_state["admission_generation"], "generation-b")
        next_summary, next_state = progress._admission_summary(missing_state,
            {"generation": "generation-c", "coverage": "observed",
             "samples": [{"state": "denied", "at": 1002, "sample_id": "c1"}],
             "latest": {"state": "denied", "at": 1002, "sample_id": "c1"},
             "cursor": {"schema_version": 4}, "warnings": []},
            1002, "run-a", "row-a", 0)
        self.assertEqual((next_summary["admitted_samples"], next_summary["denied_samples"]),
                         (0, 1))
        self.assertEqual(next_state["admission_generation"], "generation-c")

    def test_total_less_counter_rate_stays_separate_from_eta(self):
        no_total = progress.evaluate_observation(now=300, started_at=0,
            progress={"completed": 3, "unit": "item", "age_s": 0},
            previous_progress={"completed": 2, "total": None, "unit": "item"},
            previous_progress_at=60)
        self.assertEqual(no_total["rate_per_second"], 1 / 240)
        self.assertIsNone(no_total["eta_seconds"])
        self.assertIsNone(no_total["predicted_total_seconds"])
        with_total = progress.evaluate_observation(now=300, started_at=0,
            progress={"completed": 3, "total": 10, "unit": "item", "age_s": 0},
            previous_progress={"completed": 2, "total": 10, "unit": "item"},
            previous_progress_at=60)
        self.assertEqual(with_total["eta_seconds"], 7 * 240)
        stale_pair = progress.evaluate_observation(now=900, started_at=0,
            progress={"completed": 3, "unit": "item", "age_s": 0},
            previous_progress={"completed": 2, "total": None, "unit": "item"},
            previous_progress_at=100)
        self.assertIsNone(stale_pair["rate_per_second"])
        for now, completed, previous_at in ((60, 3, 60), (59, 3, 60), (300, 2, 60)):
            with self.subTest(now=now, completed=completed):
                invalid_pair = progress.evaluate_observation(now=now, started_at=0,
                    progress={"completed": completed, "unit": "item", "age_s": 0},
                    previous_progress={"completed": 2, "total": None, "unit": "item"},
                    previous_progress_at=previous_at)
                self.assertIsNone(invalid_pair["rate_per_second"])

    def test_virtual_progress_tracker_preserves_last_increase_and_starts_new_episodes(self):
        signal = {"completed": 4, "total": 20, "unit": "item",
                  "stream_id": "counter", "generation": "generation-a"}
        tracker, _, _ = progress._advance_progress_tracker(None, signal, 0)
        tracker, _, first_increase = progress._advance_progress_tracker(
            tracker, {**signal, "completed": 5}, 100)
        self.assertIsNone(first_increase["stagnation"])
        self.assertEqual(tracker["last_increase_at"], 100)
        tracker, _, before = progress._advance_progress_tracker(
            tracker, {**signal, "completed": 5}, 7299)
        self.assertEqual(tracker["last_increase_at"], 100)
        self.assertIsNone(before["stagnation"])
        tracker, _, due = progress._advance_progress_tracker(
            tracker, {**signal, "completed": 5}, 7300)
        episode = due["stagnation"]["episode_id"]
        self.assertEqual(due["stagnation"]["since"], "last-increase")
        tracker, _, repeated = progress._advance_progress_tracker(
            tracker, {**signal, "completed": 5}, 7400)
        self.assertEqual(repeated["stagnation"]["episode_id"], episode)
        tracker, _, recovered = progress._advance_progress_tracker(
            tracker, {**signal, "completed": 6}, 7401)
        self.assertIsNone(tracker["active_episode_id"])
        tracker, _, restalled = progress._advance_progress_tracker(
            tracker, {**signal, "completed": 6}, 14601)
        self.assertNotEqual(restalled["stagnation"]["episode_id"], episode)
        reset, _, reset_state = progress._advance_progress_tracker(
            tracker, {**signal, "completed": 2}, 14602)
        self.assertIsNone(reset["last_increase_at"])
        self.assertIsNone(reset_state["stagnation"])
        changed, _, _ = progress._advance_progress_tracker(
            tracker, {**signal, "generation": "generation-b", "completed": 6}, 14603)
        self.assertIsNone(changed["last_increase_at"])
        changed_total, _, _ = progress._advance_progress_tracker(
            tracker, {**signal, "total": 21, "completed": 6}, 14604)
        self.assertIsNone(changed_total["last_increase_at"])
        changed_unit, _, _ = progress._advance_progress_tracker(
            tracker, {**signal, "unit": "epoch", "completed": 6}, 14605)
        self.assertIsNone(changed_unit["last_increase_at"])
        first, _, _ = progress._advance_progress_tracker(None,
            {**signal, "completed": 1}, 1000)
        first, _, before_first = progress._advance_progress_tracker(
            first, {**signal, "completed": 1}, 8199)
        self.assertIsNone(before_first["stagnation"])
        first, _, after_first = progress._advance_progress_tracker(
            first, {**signal, "completed": 1}, 8200)
        self.assertEqual(after_first["stagnation"]["since"], "first-observation")

    def test_metric_stream_uses_step_increase_and_ignores_value_plateau(self):
        def sample(step, loss, accuracy, stamp):
            return {"epoch": step, "unit": "step", "split": "valid",
                "format": "epochs-jsonl", "metrics": {"loss": loss, "accuracy": accuracy},
                "source_updated_at": stamp, "source_generation": "source-a"}
        run = {"run_id": "metric-stream"}
        first = progress._progress_signal(None, [sample(1, 4.0, 0.5, 100)], None,
                                          run, "loss", 100)
        tracker, _, _ = progress._advance_progress_tracker(None, first, 100)
        rewritten = progress._progress_signal(None, [sample(1, 3.0, 0.8, 200)], None,
                                              run, "loss", 200)
        tracker, _, unchanged = progress._advance_progress_tracker(tracker, rewritten, 200)
        self.assertIsNone(tracker["last_increase_at"])
        self.assertIsNone(unchanged["stagnation"])
        advanced = progress._progress_signal(None, [sample(2, 3.0, 0.8, 300)], None,
                                             run, "loss", 300)
        tracker, _, increased = progress._advance_progress_tracker(tracker, advanced, 300)
        self.assertEqual(tracker["last_increase_at"], 300)
        self.assertEqual(increased["rate_per_second"], 1 / 100)
        selected = progress._progress_signal(None,
            [sample(9, 4.0, 0.5, 400), {**sample(3, 2.0, 0.8, 400),
             "unit": "epoch", "source_generation": "other-source"}],
            None, run, None, 400)
        self.assertEqual((selected["completed"], selected["unit"]), (3, "epoch"))

    def test_rootless_admission_updates_public_record_and_notice_without_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "artifact"
            artifact.mkdir()
            registry_dir = base / "registry"
            bundle = registry_dir / "arbitrary-child"
            bundle.mkdir(parents=True)
            registry = registry_dir / "resource-runs.json"
            registry.write_text("{}\n")
            (bundle / "execution-record.json").write_text(json.dumps({
                "run_id": "generic-run"}))
            admission_path = bundle / "any-file-name.json"
            admission_path.write_text(json.dumps({"admitted": False,
                "snapshot": {"t": 900}, "first_snapshot": {"t": 800},
                "reasons": ["capacity"],
                "preceding_evaluation_leases": [{"task": "evaluation", "host": "h1"}]}))
            state = base / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "generic-run", "started_at": 0,
                "artifact_root": str(artifact), "route_id": "route-a", "node": "run",
                "owner_wait": {"harness": "codex", "session_id": "owner-session"}}
            notices = []
            def notify(*args, **kwargs):
                notices.append(kwargs)
                return {"state": "pending"}
            with mock.patch.object(progress, "_observation_state_path", return_value=state), \
                    mock.patch("session_notice.notify", side_effect=notify):
                first = progress.observe_experiment(run, registry, 1000, row_key="run",
                    started_at=0)
                first_state = progress._read_observation_state(state)
                first_public = progress.read_public_run_record(artifact, "generic-run")
                first_episode = first["admission"]["episode_id"]
                first_event = first_state["outbox"][0]["key"]
                self.assertEqual(first_state["admission_counts"],
                    {"admitted": 0, "denied": 1, "unknown": 0})
                self.assertEqual(first_public["admission"]["denied_samples"], 1)
                self.assertIsNone(first_public["progress"]["reached"])
                self.assertEqual(len(first_state["outbox"]), 1)
                second = progress.observe_experiment(run, registry, 1061, row_key="run",
                    started_at=0)
                self.assertEqual(second["admission"]["denied_samples"], 1)
                self.assertEqual(second["admission"]["episode_id"], first_episode)
                self.assertEqual(notices[0]["key"], notices[-1]["key"])
                self.assertIn("sample ratio=1.0", notices[0]["text"])
                self.assertIn("admission=waiting", notices[0]["text"])
                admission_path.write_text(json.dumps({"admitted": False,
                    "snapshot": {"t": 1200}, "first_snapshot": {"t": 800},
                    "reasons": ["capacity"],
                    "preceding_evaluation_leases": [{"task": "evaluation", "host": "h1"}]}))
                repeated = progress.observe_experiment(run, registry, 1201, row_key="run",
                    started_at=0)
                self.assertEqual(repeated["admission"]["denied_samples"], 2)
                self.assertEqual(repeated["admission"]["episode_id"], first_episode)
                self.assertEqual(progress._read_observation_state(state)["outbox"][0]["key"], first_event)
                admission_path.write_text(json.dumps({"admitted": True,
                    "snapshot": {"t": 1260}, "first_snapshot": {"t": 800}}))
                admitted = progress.observe_experiment(run, registry, 1261, row_key="run",
                    started_at=0)
                admitted_public = progress.read_public_run_record(artifact, "generic-run")
                stale = progress.observe_experiment(run, registry, 1861, row_key="run",
                    started_at=0)
            observation = first["observation"]
            self.assertIsNone(observation["reached"])
            self.assertIsNone(observation["rate_per_second"])
            self.assertIsNone(observation["eta_seconds"])
            self.assertEqual(first["admission"]["state"], "waiting")
            self.assertEqual(first["admission"]["valid_samples"], 1)
            self.assertEqual(first["admission"]["denied_sample_ratio"], 1.0)
            self.assertIsNone(first["admission"]["wait_duration_seconds"])
            self.assertIsNone(first["admission"]["time_ratio"])
            self.assertEqual(admitted["admission"]["state"], "admitted")
            self.assertEqual(admitted["admission"]["denied_samples"], 2)
            self.assertEqual(admitted["admission"]["admitted_samples"], 1)
            self.assertAlmostEqual(admitted["admission"]["denied_sample_ratio"], 2 / 3)
            self.assertIsNone(admitted["admission"]["episode_id"])
            self.assertEqual(admitted_public["admission"]["state"], "admitted")
            self.assertEqual(stale["admission"]["state"], "unknown")
            public = progress.read_public_run_record(artifact, "generic-run")
            self.assertEqual(public["admission"]["state"], "unknown")
            self.assertEqual(public["rows"][0]["admission"]["denied_samples"], 2)
            self.assertIsNone(public["output_dir"])

    def test_independent_raw_counter_binding_persists_without_canonical_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "artifact"
            artifact.mkdir()
            registry = base / "registry.json"
            registry.write_text("{}\n")
            source = base / "writer" / "observation.json"
            source.parent.mkdir()
            source.write_text(json.dumps({"observed_at": 950, "finished_cells": 21}))
            proof = base / "proof" / "writer-registration.json"
            proof.parent.mkdir()
            proof.write_text(json.dumps({"target": "target-run", "writer": "writer-run",
                "argv": ["writer", "--target", "target-run"]}))
            script_proof = base / "proof" / "writer.py"
            script_proof.write_bytes(b"def write_observation():\n    return True\n")
            proof_hash = hashlib.sha256(proof.read_bytes()).hexdigest()
            script_hash = hashlib.sha256(script_proof.read_bytes()).hexdigest()
            binding = {"target_run_id": "target-run", "row_key": "run",
                "path": "/nas/writer/observation.json", "writer_run_id": "writer-run",
                "timestamp_pointer": "/observed_at", "scope": "shared-results",
                "counters": [{"name": "finished_cells", "pointer": "/finished_cells",
                              "comparison_eligible": False}],
                "proofs": [{"path": "/nas/proof/writer-registration.json",
                    "sha256": proof_hash,
                    "assertions": [{"pointer": "/target", "equals": "target-run"},
                                   {"pointer": "/writer", "equals": "writer-run"},
                                   {"pointer": "/argv/2", "equals": "target-run"}]},
                    {"path": "/nas/proof/writer.py", "sha256": script_hash,
                     "assertions": []}]}
            state = base / "runtime" / "state.json"
            state.parent.mkdir()
            registry_row = {"run_id": "target-run", "started_at": 100,
                "status": "running", "artifact_root": str(artifact), "route_id": "route-a"}
            registry.write_text(json.dumps({"schema_version": 1,
                "runs": {"target-run": registry_row}}, sort_keys=True))
            registry_before = registry.read_bytes()
            run = {**registry_row,
                "path_namespace": {"/nas": str(base)},
                "observation": {"source_bindings": [binding]},
                "owner_wait": {"harness": "codex", "session_id": "owner-session"}}
            with mock.patch.object(progress, "_observation_state_path", return_value=state), \
                    mock.patch("session_notice.notify", return_value={"state": "pending"}):
                first = progress.observe_experiment(run, registry, 1000,
                    row_key="run", started_at=100)
                first_state = progress._read_observation_state(state)
                self.assertEqual(first["observation"]["source_observations"][0]["value"], 21)
                self.assertEqual(first["observation"]["source_observations"][0]["writer_run_id"],
                                 "writer-run")
                self.assertEqual(first["observation"]["source_observations"][0]["target_run_id"],
                                 "target-run")
                self.assertIsNone(first["observation"]["reached"])
                self.assertIsNone(first["observation"]["total"])
                self.assertIsNone(first["observation"]["unit"])
                self.assertIsNone(first["observation"]["rate_per_second"])
                self.assertIsNone(first["observation"]["eta_seconds"])
                self.assertIsNone(first["observation"]["predicted_total_seconds"])
                self.assertEqual(first["observation"]["stagnation"], {"state": "unknown"})
                self.assertFalse(first["observation"]["final"])
                self.assertEqual(first_state["source_bindings"], [binding])
                persisted = progress.read_public_run_record(artifact, "target-run")
                self.assertEqual(persisted["progress"]["observed_counters"][0]["value"], 21)
                self.assertEqual(persisted["progress"]["reached"], None)
                self.assertEqual(persisted["progress"]["total"], None)
                self.assertEqual(persisted["progress"]["unit"], None)
                self.assertIsNone(persisted["progress"].get("rate_per_second"))
                self.assertIsNone(persisted["progress"].get("eta_seconds"))
                self.assertNotIn("completed", persisted["progress"])
                self.assertEqual(persisted["status"], "running")
                self.assertIsNone(persisted["eta"])
                self.assertEqual(persisted["stagnation"]["state"], "unknown")
                self.assertEqual(persisted["rows"][0]["observed_counters"][0]["name"],
                                 "finished_cells")
                self.assertEqual(persisted["sources"][0]["format"], "independent-counter")
                self.assertEqual(persisted["sources"][0]["writer_run_id"], "writer-run")
                self.assertEqual(persisted["progress"]["observed_counters"][0]["source_path"],
                                 "/nas/writer/observation.json")
                self.assertIn("raw counter=finished_cells:21 scope=shared-results",
                              first_state["outbox"][0]["text"])
                self.assertIn("writer=writer-run target=target-run",
                              first_state["outbox"][0]["text"])
                self.assertIn("ETA 총 미상", first_state["outbox"][0]["text"])
                resumed = progress.observe_experiment({**registry_row,
                    "path_namespace": {"/nas": str(base)}}, registry, 1061,
                    row_key="run", started_at=100)
                self.assertEqual(resumed["observation"]["source_observations"][0]["value"], 21)
                self.assertEqual(registry.read_bytes(), registry_before)
                script_proof.write_bytes(script_proof.read_bytes() + b"# changed\n")
                withdrawn = progress.observe_experiment({**registry_row,
                    "path_namespace": {"/nas": str(base)}}, registry, 1122,
                    row_key="run", started_at=100)
                self.assertEqual(withdrawn["observation"]["source_observations"][0]["state"],
                                 "unknown")
                self.assertIsNone(withdrawn["observation"]["reached"])
                self.assertIsNone(withdrawn["observation"]["eta_seconds"])
                self.assertEqual(registry.read_bytes(), registry_before)

    def test_raw_counter_requires_both_verified_identity_assertions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "artifact"
            artifact.mkdir()
            registry = base / "registry.json"
            registry.write_text("{}\n")
            runtime = base / "runtime"
            runtime.mkdir()
            source = base / "source" / "observation.json"
            source.parent.mkdir()
            source.write_text(json.dumps({"observed_at": 950, "progress_token": 19}))
            proof_dir = base / "proofs"
            proof_dir.mkdir()

            for index, case in enumerate(("empty", "omitted", "non-identity",
                    "target-only", "writer-only", "opaque-with-assertions")):
                with self.subTest(case=case):
                    target, writer = f"target-{index}", f"writer-{index}"
                    identity_doc = json.dumps({"target": target, "writer": writer,
                        "note": "ordinary"}).encode()
                    identity_assertions = [
                        {"pointer": "/target", "equals": target},
                        {"pointer": "/writer", "equals": writer}]
                    if case == "empty":
                        proof_specs = [(identity_doc, [])]
                    elif case == "omitted":
                        proof_specs = [(identity_doc, None)]
                    elif case == "non-identity":
                        proof_specs = [(identity_doc,
                            [{"pointer": "/note", "equals": "ordinary"}])]
                    elif case == "target-only":
                        proof_specs = [(identity_doc, [identity_assertions[0]])]
                    elif case == "writer-only":
                        proof_specs = [(identity_doc, [identity_assertions[1]])]
                    else:
                        proof_specs = [(identity_doc, identity_assertions),
                            (b"def write_observation():\n    return True\n",
                             [{"pointer": "/target", "equals": target}])]

                    proofs = []
                    for proof_index, (raw, assertions) in enumerate(proof_specs):
                        proof_path = proof_dir / f"{index}-{proof_index}.proof"
                        proof_path.write_bytes(raw)
                        proof = {"path": str(proof_path),
                            "sha256": hashlib.sha256(raw).hexdigest()}
                        if assertions is not None:
                            proof["assertions"] = assertions
                        proofs.append(proof)
                    binding = {"target_run_id": target, "row_key": "run",
                        "path": str(source), "writer_run_id": writer,
                        "timestamp_pointer": "/observed_at", "scope": "shared-results",
                        "counters": [{"name": "sample_count", "pointer": "/progress_token",
                                      "comparison_eligible": False}],
                        "proofs": proofs}
                    run = {"run_id": target, "started_at": 100, "status": "running",
                        "artifact_root": str(artifact),
                        "observation": {"source_bindings": [binding]},
                        "owner_wait": {"harness": "codex", "session_id": "fixture"}}
                    state_path = runtime / f"{target}.json"

                    with mock.patch.object(progress, "_observation_state_path",
                            return_value=state_path), \
                            mock.patch("session_notice.notify", return_value={"state": "pending"}):
                        result = progress.observe_experiment(run, registry, 1000,
                            row_key="run", started_at=100)
                        state = progress._read_observation_state(state_path)
                        public = progress.read_public_run_record(artifact, target)

                    observed = result["observation"]["source_observations"][0]
                    self.assertEqual(observed["state"], "unknown")
                    self.assertIsNone(observed["value"])
                    counters = public["progress"]["observed_counters"]
                    self.assertTrue(counters)
                    self.assertTrue(all(item["state"] != "observed"
                                        and item.get("value") is None for item in counters))
                    row_counters = public["rows"][0]["observed_counters"]
                    self.assertTrue(all(item["state"] != "observed"
                                        and item.get("value") is None for item in row_counters))
                    notice_text = "\n".join(item["text"] for item in state["outbox"])
                    self.assertIn("raw counter=sample_count:unknown", notice_text)
                    self.assertNotIn("raw counter=sample_count:19", notice_text)

    def test_admission_recovery_starts_a_new_stagnation_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "artifact"
            artifact.mkdir()
            registry_dir = base / "registry"
            bundle = registry_dir / "bundle"
            bundle.mkdir(parents=True)
            registry = registry_dir / "index.json"
            registry.write_text("{}\n")
            (bundle / "run-record.json").write_text(json.dumps({
                "run_id": "grace-run", "started_at": 0}))
            decision = bundle / "decision.json"
            decision.write_text(json.dumps({"admitted": False,
                "snapshot": {"t": 0}, "first_snapshot": {"t": 0}}))
            state = base / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "grace-run", "started_at": 0,
                   "artifact_root": str(artifact), "status": "running"}
            with mock.patch.object(progress, "_observation_state_path", return_value=state):
                progress.observe_experiment(run, registry, 0, row_key="run", started_at=0,
                    progress={"completed": 4, "total": 20, "unit": "item",
                              "updated_at": 0, "age_s": 0}, notify_events=False)
                decision.write_text(json.dumps({"admitted": False,
                    "snapshot": {"t": 100}, "first_snapshot": {"t": 0}}))
                progress.observe_experiment(run, registry, 100, row_key="run", started_at=0,
                    progress={"completed": 5, "total": 20, "unit": "item",
                              "updated_at": 100, "age_s": 0}, notify_events=False)
                decision.write_text(json.dumps({"admitted": False,
                    "snapshot": {"t": 7300}, "first_snapshot": {"t": 0}}))
                waiting = progress.observe_experiment(run, registry, 7300, row_key="run", started_at=0,
                    progress={"completed": 5, "total": 20, "unit": "item",
                              "updated_at": 7300, "age_s": 0}, notify_events=False)
                decision.write_text(json.dumps({"admitted": True,
                    "snapshot": {"t": 7400}, "first_snapshot": {"t": 0}}))
                recovered = progress.observe_experiment(run, registry, 7400, row_key="run", started_at=0,
                    progress={"completed": 5, "total": 20, "unit": "item",
                              "updated_at": 7400, "age_s": 0}, notify_events=False)
                self.assertEqual(waiting["admission"]["state"], "waiting")
                self.assertNotIn("progress-stagnation",
                    {item["kind"] for item in waiting["observation"]["attention"]})
                self.assertEqual(recovered["admission"]["state"], "admitted")
                self.assertEqual(recovered["observation"]["stagnation"]["state"], "observing")
                after_grace = progress.observe_experiment(run, registry, 14600, row_key="run", started_at=0,
                    progress={"completed": 5, "total": 20, "unit": "item",
                              "updated_at": 14600, "age_s": 0}, notify_events=False)
            self.assertEqual(after_grace["observation"]["stagnation"]["state"], "stagnant")
            self.assertEqual(after_grace["admission"]["state"], "unknown")
            self.assertIn("progress-stagnation",
                {item["kind"] for item in after_grace["observation"]["attention"]})

    def test_virtual_checkpoint_schedule_and_independent_matrix_rows(self):
        rows = []
        for index in range(13):
            started = index * 17
            result = progress.evaluate_observation(
                now=started + 7200, started_at=started, planned_seconds=None,
                progress={"completed": 1, "total": 10, "unit": "epoch"})
            rows.append(result)
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(row["checkpoint_due"] for row in rows))
        self.assertEqual([row["started_at"] for row in rows], [i * 17 for i in range(13)])
        planned = progress.evaluate_observation(
            now=3600, started_at=0, planned_seconds=36000)
        self.assertTrue(planned["checkpoint_due"])
        self.assertEqual(planned["next_checkpoint_at"], 9000)

    def test_ami_thirteen_rows_keep_independent_clocks_and_metric_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "artifact"
            artifact.mkdir()
            runtime = base / "runtime"
            def state_path(_run, _registry, row_key):
                runtime.mkdir(exist_ok=True)
                return runtime / (str(row_key) + ".json")
            rows = []
            with mock.patch.object(progress, "_observation_state_path", side_effect=state_path):
                for index in range(13):
                    output = base / f"row-{index:02d}"
                    (output / "logs").mkdir(parents=True)
                    (output / "logs" / "epochs.jsonl").write_text(
                        json.dumps({"epoch": 1, "split": "valid",
                                    "losses": {"L_se": -10.0 + index}}) + "\n")
                    started = index * 37
                    run = {"run_id": "ami-13", "route_id": "ami-route", "node": "train",
                           "artifact_root": str(artifact), "started_at": started}
                    result = progress.observe_experiment(run, base / "registry.json",
                        started + 7200, root=str(output), row_key=f"arm-{index:02d}",
                        started_at=started)
                    rows.append(result["observation"])
            self.assertTrue(all(item["checkpoint_due"] for item in rows))
            self.assertEqual([item["started_at"] for item in rows], [index * 37 for index in range(13)])
            record = progress.read_public_run_record(artifact, "ami-13")
            self.assertEqual({row["row_id"] for row in record["rows"]},
                             {f"arm-{index:02d}" for index in range(13)})
            self.assertEqual(len(record["metrics"]), 13)

    def test_whamr_nine_epoch_json_runs_identify_five_eta_over_plan(self):
        outcomes = []
        for index in range(9):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "valid-epoch-0003.json").write_text(json.dumps({
                    "epoch": 3, "losses": {"L_se": -8.77, "SISNRi": 14.62}, "seconds": 848.9}))
                (root / "progress.json").write_text(
                    '{"reached_epoch":3,"total_epochs":100,"best_updated":true}')
                decoded = metric_sources.read_sources(root)
                progress_value = {"completed": decoded["progress"]["reached_epoch"],
                                  "total": decoded["progress"]["total_epochs"],
                                  "unit": "epoch", "age_s": 0}
                increment = 1 if index < 5 else 5
                previous = {"completed": 3 - increment, "total": 100, "unit": "epoch"}
                result = progress.evaluate_observation(now=300, started_at=0,
                    planned_seconds=4 * 3600, progress=progress_value,
                    previous_progress=previous, previous_progress_at=60,
                    samples=decoded["samples"])
                outcomes.append(result)
                self.assertEqual(decoded["sources"][0]["format"], "epoch-json-files")
                self.assertEqual(decoded["progress"]["total_epochs"], 100)
        self.assertEqual(sum(any(item["kind"] == "eta-over-plan" for item in row["attention"])
                             for row in outcomes), 5)

    def test_optional_observation_keys_criteria_and_time_budget_are_not_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            artifact.mkdir()
            output = root / "run"
            (output / "logs").mkdir(parents=True)
            (output / "logs" / "epochs.jsonl").write_text(
                '{"epoch":1,"split":"valid","losses":{"L_se":-8.0,"SISNRi":12.0}}\n')
            state = root / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "optional-run", "started_at": 0,
                   "artifact_root": str(artifact), "route_id": "route-optional",
                   "observation": {"keys": {"valid": ["SISNRi"]},
                       "decision_metric": {"key": "SISNRi", "direction": "max"},
                       "proxy": {"key": "L_se", "direction": "min"},
                       "time_budget": {"seconds": 14400}, "comparable_group": "ami"}}
            with mock.patch.object(progress, "_observation_state_path", return_value=state):
                result = progress.observe_experiment(run, root / "registry.json", 60,
                    root=str(output), row_key="row-a")
            record = progress.read_public_run_record(artifact, "optional-run")
            self.assertEqual(record["planned_seconds"], 14400)
            self.assertEqual(record["decision_metric"], {"key": "SISNRi", "direction": "max"})
            self.assertEqual(record["proxy"], {"key": "L_se", "direction": "min"})
            self.assertEqual(record["comparable_group"], "ami")
            self.assertEqual(record["metrics"][0]["keys"], ["SISNRi"])
            self.assertEqual(result["observation"]["summary"]["metric"], "SISNRi")

    def test_valid_l_se_worsening_uses_distinct_epoch_transitions(self):
        samples = [{"epoch": epoch, "split": "valid", "metrics": {"L_se": value}}
                   for epoch, value in enumerate((-10.0, -9.0, -8.0, -7.0), 1)]
        result = progress.evaluate_observation(now=600, started_at=0, samples=samples)
        self.assertEqual(result["summary"]["direction"], "min")
        self.assertEqual(result["summary"]["worsening_transitions"], 3)
        self.assertEqual(result["summary"]["trend"], "악화")
        self.assertEqual(result["attention"][0]["kind"], "validation-worsening")
        replayed = progress.metric_summary(samples + [samples[-1]])
        self.assertEqual(replayed["worsening_transitions"], 3)

    def test_finite_loss_divergence_and_baseline_delta_are_reported(self):
        samples = [{"epoch": 1, "split": "valid", "metrics": {"loss": -1.0}},
                   {"epoch": 2, "split": "valid", "metrics": {"loss": 100.0}}]
        result = progress.evaluate_observation(now=100, started_at=0, samples=samples,
                                               baseline=0.5)
        self.assertIn("metric-divergence", {item["kind"] for item in result["attention"]})
        self.assertEqual(result["summary"]["baseline_delta"], 99.5)

    def test_long_run_and_source_paths_use_bounded_notice_parts(self):
        record_path = "/nas/" + "run-directory-" * 70 + "/resource-runs/run-long.json"
        source_path = "/nas/" + "epoch-output-" * 70 + "/valid-epoch-0001.json"
        text_parts = progress._notice_texts(
            {"run_id": "long-run", "row_id": "arm-01"}, "final",
            {"summary": {"metric": "L_se", "latest": {"epoch": 1, "value": -8.7},
                          "best": {"epoch": 1, "value": -8.7}, "trend": "판정 불가"},
             "elapsed_seconds": 7200, "planned_seconds": None, "attention": []},
            record_path, [{"format": "epoch-json-files", "path": [source_path], "keys": ["L_se"]}])
        self.assertGreater(len(text_parts), 1)
        self.assertTrue(all(len(part) <= 600 for part in text_parts))
        joined = "".join(part.partition(": ")[2] if part.startswith("part ") else part
                         for part in text_parts)
        self.assertIn("resource-runs/run-long.json", joined)
        self.assertIn("valid-epoch-0001.json", joined)

    def test_early_counter_eta_and_elapsed_fallback_boundaries(self):
        early = progress.evaluate_observation(
            now=300, started_at=0, progress={"completed": 3, "total": 1000, "unit": "item"},
            previous_progress={"completed": 2, "total": 1000, "unit": "item"},
            previous_progress_at=60)
        self.assertTrue(early["early_eta_attention"])
        self.assertEqual(early["attention"][0]["kind"], "eta-over-six-hours")
        exact = progress.evaluate_observation(now=21600, started_at=0)
        over = progress.evaluate_observation(now=21601, started_at=0)
        self.assertFalse(exact["attention"])
        self.assertEqual(over["attention"][0]["kind"], "elapsed-over-six-hours")
        planned = progress.evaluate_observation(now=601, started_at=0, planned_seconds=400)
        self.assertEqual(planned["attention"][0]["kind"], "elapsed-over-plan")
        stale = progress.evaluate_observation(
            now=300, started_at=0,
            progress={"completed": 3, "total": 1000, "unit": "item", "age_s": 601},
            previous_progress={"completed": 2, "total": 1000, "unit": "item"},
            previous_progress_at=60)
        self.assertIsNone(stale["rate_per_second"])
        self.assertFalse(stale["early_eta_attention"])
        reset = progress.evaluate_observation(
            now=300, started_at=0,
            progress={"completed": 3, "total": 999, "unit": "item"},
            previous_progress={"completed": 2, "total": 1000, "unit": "item"},
            previous_progress_at=60)
        self.assertIsNone(reset["rate_per_second"])

    def test_nan_rules_final_summary_and_atomic_public_record(self):
        samples = [{"epoch": 1, "split": "valid", "metrics": {"loss": float("nan")}},
                   {"epoch": 2, "split": "valid", "metrics": {"score": 0.2}}]
        result = progress.evaluate_observation(
            now=100, started_at=0, samples=samples,
            optional_rules=[{"metric": "score", "op": "lt", "threshold": 0.3}], final=True)
        self.assertTrue(result["checkpoint_due"])
        self.assertEqual({row["kind"] for row in result["attention"]},
                         {"nonfinite-metric", "declared-rule"})
        run = {"run_id": "run-observation-1", "route_id": "route-1", "node": "eval"}
        record = progress.public_run_record(run, status="running", observation=result,
                                            metrics=[{"format": "epoch-json-files",
                                                      "path": ["/nas/run/valid-epoch-0001.json"],
                                                      "keys": ["loss"]}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = progress.write_public_run_record(root, run["run_id"], record)
            self.assertEqual(path, root / "resource-runs" / "run-observation-1.json")
            self.assertEqual(progress.read_public_run_record(root, run["run_id"]), record)
            replacement = {**record, "status": "finished", "ended_at": 100}
            self.assertEqual(progress.write_public_run_record(root, run["run_id"], replacement), path)
            self.assertEqual(progress.read_public_run_record(root, run["run_id"])["status"], "finished")
            self.assertEqual(len(list(path.parent.glob("*.json"))), 1)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_resource_runner_json_command_matches_stable_public_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "nas-artifacts"
            artifact.mkdir()
            run_id = "cli-run-1"
            run = {"run_id": run_id, "campaign_id": "campaign-a", "cycle_id": "cycle-a",
                   "route_id": "route-a", "node": "eval", "host": None,
                   "output_dir": None, "status": "running", "started_at": None}
            record = progress.public_run_record(run)
            record["metrics"] = [{"format": "epoch-json-files",
                                  "path": ["/nas/exp/valid-epoch-0001.json"],
                                  "keys": ["L_se"]}]
            path = progress.write_public_run_record(artifact, run_id, record)
            registry = root / "resource-runs.json"
            registry.write_text(json.dumps({"runs": {run_id: {
                "run_id": run_id, "artifact_root": str(artifact)}}}))
            command = [sys.executable, str(Path(__file__).with_name("resource-runner.py")),
                       "--registry", str(registry), "observation", "--run-id", run_id, "--json"]
            result = subprocess.run(command, capture_output=True, text=True, check=False,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": "/tmp"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), progress.read_public_run_record(artifact, run_id))
            self.assertEqual(path, artifact / "resource-runs" / f"{run_id}.json")

    def test_config_output_root_requires_one_exact_hash_checked_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            output = project / "out"
            output.mkdir()
            (project / "configs").mkdir()
            config = project / "configs" / "config.json"
            config.write_text(json.dumps({"output_dir": "out", "training": {}}))
            run = {"cwd": str(project), "command": ["python", "train.py", "--config", "configs/config.json"],
                   "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()}
            self.assertEqual(progress.discover_output_root(run), str(output))
            self.assertIsNone(progress.discover_output_root({**run, "config_sha256": "0" * 64}))
            config.write_text(json.dumps({"output_dir": "out", "save_dir": "other"}))
            self.assertIsNone(progress.discover_output_root({**run,
                "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()}))

    def test_remote_config_root_needs_an_explicit_path_namespace_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            nas = Path(tmp) / "nas"
            project = nas / "project"
            (project / "configs").mkdir(parents=True)
            output = project / "out"
            output.mkdir()
            config = project / "configs" / "run.json"
            config.write_text(json.dumps({"output_dir": "/host-ns/project/out"}))
            run = {"cwd": "/host-ns/project", "run_root": "/host-ns/runs",
                   "path_namespace": {"/host-ns": str(nas)},
                   "command": ["python", "train.py", "--config", "configs/run.json"],
                   "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()}
            self.assertEqual(progress.discover_output_root(run), str(output))
            self.assertIsNone(progress.discover_output_root({**run, "path_namespace": None}))

    def test_persisted_observation_keeps_nan_and_suppresses_duplicate_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            (logs / "epochs.jsonl").write_text(
                '{"epoch":1,"split":"valid","losses":{"loss":NaN}}\n')
            artifact = root / "artifact"
            artifact.mkdir()
            state = root / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "persisted-run", "started_at": 0,
                   "artifact_root": str(artifact), "route_id": "route-a",
                   "node": "train", "status": "running"}
            with mock.patch.object(progress, "_observation_state_path", return_value=state):
                first = progress.observe_experiment(run, root / "registry.json", 7200,
                    root=str(root), row_key="arm-1", started_at=0)
                second = progress.observe_experiment(run, root / "registry.json", 7201,
                    root=str(root), row_key="arm-1", started_at=0)
                third = progress.observe_experiment(run, root / "registry.json", 7202,
                    root=str(root), row_key="arm-2", started_at=0)
            self.assertTrue(first["observation"]["checkpoint_due"])
            self.assertTrue(first["observation"]["attention"])
            self.assertFalse(second["observation"]["checkpoint_due"])
            self.assertNotEqual(
                progress._observation_state_path(run, root / "registry.json", "arm-1"),
                progress._observation_state_path(run, root / "registry.json", "arm-2"))
            self.assertTrue(state.exists())
            record = progress.read_public_run_record(artifact, "persisted-run")
            self.assertEqual(record["route_id"], "route-a")
            self.assertTrue(record["metrics"])

    def test_unknown_arm_birth_does_not_inherit_the_wrapper_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            artifact.mkdir()
            output = root / "run"
            (output / "logs").mkdir(parents=True)
            (output / "logs" / "epochs.jsonl").write_text(
                '{"epoch":1,"split":"valid","losses":{"L_se":-8.0}}\n')
            state = root / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "unknown-arm", "started_at": 0,
                   "artifact_root": str(artifact), "route_id": "route-a"}
            with mock.patch.object(progress, "_observation_state_path", return_value=state):
                observed = progress.observe_experiment(run, root / "registry.json", 7200,
                    root=str(output), row_key="arm-reused-label")
            self.assertIn("row-start-unknown", observed["warnings"])
            self.assertIsNone(observed["observation"]["elapsed_seconds"])
            record = progress.read_public_run_record(artifact, "unknown-arm")
            row, = record["rows"]
            self.assertIsNone(row["started_at"])
            self.assertEqual(row["clock_scope"], "unknown")

    def test_checkpoint_uses_existing_notice_queue_for_exact_owner_and_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            artifact.mkdir()
            state = root / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "fanout-run", "started_at": 0,
                   "artifact_root": str(artifact), "route_id": "route-a", "node": "eval",
                   "owner_wait": {"harness": "codex", "session_id": "owner-thread"},
                   "session": {"harness": "claude", "id": "worker-session"}}
            with mock.patch.object(progress, "_observation_state_path", return_value=state), \
                    mock.patch("session_notice.notify", return_value={"state": "pending"}) as notify:
                first = progress.observe_experiment(run, root / "registry.json", 7200)
                first_keys = [call.kwargs["key"] for call in notify.call_args_list]
                notify.reset_mock()
                second = progress.observe_experiment(run, root / "registry.json", 7261)
                second_keys = [call.kwargs["key"] for call in notify.call_args_list]
            self.assertEqual(len(first["delivery"][0]["recipients"]), 2)
            self.assertEqual(len(first_keys), 2)
            self.assertEqual(first_keys, second_keys)
            self.assertTrue(all(call.kwargs["text"].find("record=") >= 0
                                for call in notify.call_args_list))

    def test_checkpoint_notice_records_are_bound_to_both_exact_sessions(self):
        import dispatch_session_sweep as sweep
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            artifact.mkdir()
            jobs = root / "jobs.log"
            jobs.write_text("")
            run = {"run_id": "carrier-run", "started_at": 0,
                   "artifact_root": str(artifact), "route_id": "route-a", "node": "eval",
                   "jobs": str(jobs),
                   "owner_wait": {"harness": "codex", "session_id": "owner-thread"},
                   "session": {"harness": "claude", "id": "worker-session"}}
            state = root / "runtime" / "state.json"
            state.parent.mkdir()
            with mock.patch.object(progress, "_observation_state_path", return_value=state):
                result = progress.observe_experiment(run, root / "registry.json", 7200)
                progress.observe_experiment(run, root / "registry.json", 7261)
            self.assertEqual({tuple((item["harness"], item["state"]))
                              for item in result["delivery"][0]["recipients"]},
                             {("codex", "pending"), ("claude", "pending")})
            for harness, carrier, session_id in (
                    ("codex", "codex-native-queue", "owner-thread"),
                    ("claude", "claude-parent-runtime", "worker-session")):
                records, _ = sweep.addressed_records(root, carrier, session_id)
                self.assertEqual(len(records), 1)
                receipt = records[0]["receipt"]
                self.assertIn("run=carrier-run", receipt["text"])
                self.assertIn("record=" + str(artifact / "resource-runs" / "carrier-run.json"),
                              receipt["text"])

    def test_notice_targets_keep_the_start_binding_and_exact_supervisor_parent_row(self):
        import dispatch_contract
        import dispatch_resource_wait
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            meta = ",".join(("attempt_schema_version=2", "attempt_id=att-owner",
                "dispatch_depth=1", "worker_type=owner", "harness=codex",
                "parent_sid=sid-supervisor", "parent_completion_delivery=claude-parent-runtime"))
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            jobs.write_text("\t".join((stamp, "open", "/repo", "/wt", "owner", meta)) + "\n")
            state = dispatch_contract.dispatch_state_root(jobs) / "supervisor-state" / "att-owner.json"
            state.parent.mkdir(parents=True)
            dispatch_resource_wait._write(state, "att-owner", set(),
                {"session_id": "sid-owner", "delivered": [], "outbox": None}, "running-turn")
            run = {"jobs": str(jobs), "parent_attempt_id": "att-owner",
                   "owner_wait": {"parent_attempt_id": "att-owner", "session_id": "sid-owner"},
                   "session": {"harness": "codex", "id": "sid-owner"}}
            targets = progress._notice_targets(run)
            by_session = {item["session_id"]: item for item in targets}
            self.assertEqual(len(targets), 2, "owner and supervisor sessions are deduplicated")
            self.assertEqual(by_session["sid-owner"], {
                "harness": "codex", "session_id": "sid-owner",
                "lineage": {"kind": "owner-session", "attempt_id": "att-owner",
                            "harness": "codex", "session_id": "sid-owner"}})
            self.assertEqual(by_session["sid-supervisor"], {
                "harness": "claude", "session_id": "sid-supervisor",
                "lineage": {"kind": "attempt-parent", "attempt_id": "att-owner",
                            "harness": "claude", "session_id": "sid-supervisor"}})
            from peer_obligations import _ordinary_notice_target
            self.assertEqual(_ordinary_notice_target({"jobs": str(jobs),
                "storage_recipient": "sid-owner", "lineage": by_session["sid-owner"]["lineage"]},
                "codex-native-queue"), ("codex", "sid-owner", "sid-owner"))
            self.assertEqual(_ordinary_notice_target({"jobs": str(jobs),
                "storage_recipient": "sid-supervisor",
                "lineage": by_session["sid-supervisor"]["lineage"]},
                "claude-parent-runtime"), ("claude", "sid-supervisor", "sid-supervisor"))

    def test_provenance_notice_follows_the_seat_successor_across_harnesses(self):
        """The real ledger reader preserves provenance across a harness handoff."""
        import dispatch_seat_handover as handover
        import session_tidy as tidy
        from peer_obligations import _ordinary_notice_target
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(tidy, "state_root", return_value=Path(temporary)):
            identity = {"jobs": str(Path(temporary) / "jobs.log"), "storage_recipient": "sid-codex",
                        "lineage": {"kind": "provenance", "harness": "codex", "session_id": "sid-codex"}}
            self.assertEqual(handover.session_successor("sid-codex"), ("", ""))
            self.assertEqual(_ordinary_notice_target(identity, "codex-native-queue"),
                             ("codex", "sid-codex", "sid-codex"))
            for row in ({"event": "handover", "from": {"harness": "codex", "sid": "sid-codex"},
                         "sid": "sid-claude", "harness": "claude", "source": "retire", "bindings": [], "ts": 10.0},
                        {"event": "handover", "from": "sid-claude", "sid": "sid-claude-2",
                         "harness": "claude", "source": "clear", "bindings": [], "ts": 20.0}):
                tidy._append_ledger_entry(tidy.Seat("pane", row["sid"]), row)
            self.assertEqual(handover.session_successor("sid-codex"), ("sid-claude-2", "claude"))
            self.assertEqual(_ordinary_notice_target(identity, "codex-native-queue"),
                             ("claude", "sid-claude-2", "sid-codex"))
            with mock.patch.object(handover, "_readable_lineage_signature", side_effect=OSError("ledger")):
                self.assertIsNone(_ordinary_notice_target(identity, "codex-native-queue"))
    def test_terminal_observation_keeps_durable_live_checkpoint_and_adds_one_final_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            artifact.mkdir()
            output = root / "run"
            output.mkdir()
            state = root / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "live-terminal", "started_at": 0,
                   "artifact_root": str(artifact), "route_id": "route-a", "node": "eval",
                   "status": "running", "notify": {"harness": "claude", "id": "owner-sid"}}
            notices = []
            with mock.patch.object(progress, "_observation_state_path", return_value=state), \
                    mock.patch("session_notice.notify", side_effect=lambda *a, **kw:
                               notices.append(kw) or {"state": "pending"}):
                progress.observe_experiment(run, root / "registry.json", 7200,
                    root=str(output), row_key="run", started_at=0)
                terminal = progress.observe_experiment({**run, "status": "succeeded",
                    "ended_at": 7201}, root / "registry.json", 7201,
                    root=str(output), row_key="run", started_at=0, final=True)
            self.assertEqual([item["subject"] for item in notices], [
                "experiment checkpoint (1/1)", "experiment checkpoint (1/1)",
                "experiment final (1/1)"])
            self.assertEqual(notices[0]["key"], notices[1]["key"])
            self.assertNotEqual(notices[1]["key"], notices[2]["key"])
            self.assertIn("지표 없음", notices[2]["text"])
            persisted = progress._read_observation_state(state)
            self.assertEqual([item["kind"] for item in persisted["outbox"]],
                             ["checkpoint", "final"])
            self.assertTrue(terminal["available"])

    def _terminal_source_fixture(self, root, *, run_id="counter-target"):
        artifact = root / "artifact"
        artifact.mkdir()
        source = root / "writer" / "observation.json"
        source.parent.mkdir()
        registration = root / "proofs" / "writer-registration.json"
        registration.parent.mkdir()
        writer_proof = root / "proofs" / "writer.py"
        registry = root / "registry.json"
        registry.write_text("{}\n")
        state = root / "runtime" / "observation.json"
        state.parent.mkdir()
        target_id, writer_id = run_id, "independent-writer"
        registration.write_text(json.dumps({"target": target_id, "writer": writer_id,
            "argv": ["writer", "--target", target_id]}))
        writer_proof.write_bytes(b"def write_observation():\n    return True\n")
        proof_raw = registration.read_bytes()
        writer_raw = writer_proof.read_bytes()
        binding = {"target_run_id": target_id, "row_key": "run",
            "path": "/fixture/writer/observation.json", "writer_run_id": writer_id,
            "timestamp_pointer": "/observed_at", "scope": "shared-results",
            "counters": [{"name": "finished_items", "pointer": "/finished_items",
                          "comparison_eligible": False}],
            "proofs": [{"path": "/fixture/proofs/writer-registration.json",
                "sha256": hashlib.sha256(proof_raw).hexdigest(),
                "assertions": [{"pointer": "/target", "equals": target_id},
                               {"pointer": "/writer", "equals": writer_id},
                               {"pointer": "/argv/2", "equals": target_id}]},
                {"path": "/fixture/proofs/writer.py",
                 "sha256": hashlib.sha256(writer_raw).hexdigest(), "assertions": []}]}
        run = {"run_id": target_id, "started_at": 100, "ended_at": 999,
            "status": "succeeded", "artifact_root": str(artifact),
            "route_id": "fixture-route", "path_namespace": {"/fixture": str(root)},
            "observation": {"source_bindings": [binding]},
            "owner_wait": {"harness": "codex", "session_id": "fixture-owner"}}

        def write_source(value, observed_at):
            source.write_text(json.dumps({"observed_at": observed_at,
                                          "finished_items": value}))

        return {"artifact": artifact, "binding": binding, "registry": registry,
                "run": run, "source": source, "state": state,
                "write_source": write_source}

    @staticmethod
    def _expected_source_attention_key(observation):
        identity = [{key: item.get(key) for key in
            ("kind", "metric", "epoch", "op", "threshold", "episode_id",
             "generation", "since", "source_value_hash")}
            for item in observation["attention"]]
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:16]
        if observation["checkpoint_due"]:
            return f"attention-checkpoint:run:{int(observation['due_checkpoint_at'])}:{digest}"
        return f"attention:run:{digest}"

    def test_final_source_change_creates_attention_and_preserves_pending_or_acked_final(self):
        for final_receipt in ("pending", "acked"):
            with self.subTest(final_receipt=final_receipt), tempfile.TemporaryDirectory() as tmp:
                fixture = self._terminal_source_fixture(Path(tmp))
                fixture["write_source"](21, 950)
                notices = []

                def send(*args, **kwargs):
                    state = (final_receipt if kwargs["subject"].startswith("experiment final ")
                             else "acked")
                    notices.append({"args": args, **kwargs})
                    return {"state": state}

                def observe(value, observed_at, now):
                    fixture["write_source"](value, observed_at)
                    return progress.observe_experiment(fixture["run"], fixture["registry"], now,
                        row_key="run", started_at=100, final=True, status="succeeded")

                with mock.patch.object(progress, "_observation_state_path",
                        return_value=fixture["state"]), \
                        mock.patch("session_notice.notify", side_effect=send):
                    first = observe(21, 950, 1000)
                    first_state = progress._read_observation_state(fixture["state"])
                    first_public = progress.read_public_run_record(
                        fixture["artifact"], fixture["run"]["run_id"])
                    first_final_event = next((item for item in first_state["outbox"]
                                              if item.get("kind") == "final"), None)
                    initial_final_calls = [item.copy() for item in notices
                        if item["subject"].startswith("experiment final ")]
                    final_delivery = next(item for item in first["delivery"]
                                          if item.get("key") == "final:run")

                    changed = observe(22, 1050, 1060)
                    changed_state = progress._read_observation_state(fixture["state"])
                    changed_public = progress.read_public_run_record(
                        fixture["artifact"], fixture["run"]["run_id"])
                    changed_attention_key = self._expected_source_attention_key(
                        changed["observation"])
                    changed_attention_delivery = [item for item in changed["delivery"]
                        if item.get("key", "").startswith("attention")]
                    self.assertEqual(len(changed_attention_delivery), 1)
                    self.assertEqual(changed_attention_delivery[0]["key"],
                                     changed_attention_key)
                    self.assertEqual(changed["observation"]["source_observations"][0]["value"], 22)
                    self.assertEqual(changed_state["source_observations"][0]["value"], 22)
                    self.assertEqual(changed_public["progress"]["observed_counters"][0]["value"], 22)
                    self.assertEqual(changed_public["progress"]["observed_counters"][0]["writer_run_id"],
                                     "independent-writer")
                    self.assertEqual(changed_public["progress"]["observed_counters"][0]["target_run_id"],
                                     fixture["run"]["run_id"])
                    self.assertFalse(changed_public["progress"]["observed_counters"][0][
                                     "comparison_eligible"])
                    attention_notices = [item for item in notices
                        if f":{changed_attention_key}:" in item["key"]]
                    self.assertTrue(attention_notices)
                    self.assertTrue(any("raw counter=finished_items:22" in item["text"]
                                        and "writer=independent-writer" in item["text"]
                                        and f"target={fixture['run']['run_id']}" in item["text"]
                                        for item in attention_notices))

                    first_final = initial_final_calls[0]
                    self.assertEqual(first_final["args"], ("codex", "fixture-owner"))
                    self.assertEqual(first_final["lineage"], {"kind": "provenance",
                        "harness": "codex", "session_id": "fixture-owner"})
                    self.assertEqual(first_final["route_id"], "fixture-route")
                    self.assertEqual(first_final["required_action"], "read-notice")
                    self.assertEqual(final_delivery["recipients"][0]["state"], final_receipt)
                    if final_receipt == "pending":
                        self.assertIsNotNone(first_final_event)
                        self.assertEqual(first_final_event["key"], "final:run")
                        self.assertEqual(first_final_event["recipients"], [{
                            "harness": "codex", "session_id": "fixture-owner",
                            "lineage": {"kind": "provenance", "harness": "codex",
                                         "session_id": "fixture-owner"}}])
                        changed_final_event = next(item for item in changed_state["outbox"]
                                                   if item.get("kind") == "final")
                        self.assertEqual(changed_final_event, first_final_event)
                    else:
                        self.assertIsNone(first_final_event)
                        self.assertIn("final:run", first_state["event_keys"])
                        self.assertIn("final:run", changed_state["event_keys"])
                        self.assertEqual(len([item for item in notices
                            if item["subject"].startswith("experiment final ")]),
                            len(initial_final_calls))

                    first_attention_key = next(item["key"] for item in first["delivery"]
                                               if item.get("key", "").startswith("attention"))
                    self.assertNotEqual(changed_attention_key, first_attention_key)
                    self.assertEqual(changed["observation"]["source_observations"][0][
                                     "source_generation"], first["observation"][
                                     "source_observations"][0]["source_generation"])

                    repeated = observe(22, 1050, 1121)
                    equal_fresh = observe(22, 1170, 1183)
                    final_calls = [item for item in notices
                        if item["subject"].startswith("experiment final ")]
                    initial_final_signatures = [(initial["args"], initial["key"],
                        initial["subject"], initial["text"], initial["route_id"],
                        initial["required_action"], initial["lineage"])
                        for initial in initial_final_calls]
                    self.assertTrue(all((item["args"], item["key"], item["subject"],
                                         item["text"], item["route_id"], item["required_action"],
                                         item["lineage"]) in initial_final_signatures
                                        for item in final_calls))
                    final_state = progress._read_observation_state(fixture["state"])
                    final_public = progress.read_public_run_record(
                        fixture["artifact"], fixture["run"]["run_id"])
                    attention_keys = [key for key in final_state["event_keys"]
                                      if key.startswith("attention")]
                    self.assertEqual(len(attention_keys), 2)
                    self.assertEqual(len(set(attention_keys)), 2)
                    self.assertEqual(final_state["source_observations"][0]["value"], 22)
                    self.assertEqual(final_state["source_observations"][0]["observed_at"], 1170)
                    self.assertEqual(final_public["progress"]["observed_counters"][0]["value"], 22)
                    self.assertFalse(any(item.get("key", "").startswith("attention")
                                         for item in repeated["delivery"]))
                    self.assertFalse(any(item.get("key", "").startswith("attention")
                                         for item in equal_fresh["delivery"]))
                    for result in (first, changed, repeated, equal_fresh):
                        observation = result["observation"]
                        self.assertIsNone(observation.get("reached"))
                        self.assertIsNone(observation.get("total"))
                        self.assertIsNone(observation.get("unit"))
                        self.assertIsNone(observation.get("rate_per_second"))
                        self.assertIsNone(observation.get("eta_seconds"))
                        self.assertIsNone(observation.get("predicted_total_seconds"))
                    self.assertIsNone(final_public["progress"].get("rate_per_second"))
                    self.assertIsNone(final_public["progress"].get("eta_seconds"))
                    self.assertNotIn("completed", final_public["progress"])

    def test_notify_events_false_keeps_final_and_source_events_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._terminal_source_fixture(Path(tmp), run_id="suppressed-target")
            fixture["write_source"](31, 950)
            with mock.patch.object(progress, "_observation_state_path",
                    return_value=fixture["state"]), \
                    mock.patch("session_notice.notify", return_value={"state": "acked"}) as notify:
                suppressed_first = progress.observe_experiment(fixture["run"],
                    fixture["registry"], 1000, row_key="run", started_at=100,
                    final=True, status="succeeded", notify_events=False)
                fixture["write_source"](32, 1050)
                suppressed_change = progress.observe_experiment(fixture["run"],
                    fixture["registry"], 1060, row_key="run", started_at=100,
                    final=True, status="succeeded", notify_events=False)
                suppressed_state = progress._read_observation_state(fixture["state"])
                suppressed_public = progress.read_public_run_record(
                    fixture["artifact"], fixture["run"]["run_id"])
                self.assertEqual(notify.call_count, 0)
                self.assertFalse(suppressed_state["outbox"])
                self.assertFalse(suppressed_state["event_keys"])
                self.assertEqual(suppressed_state["source_observations"][0]["value"], 32)
                self.assertEqual(suppressed_public["progress"]["observed_counters"][0]["value"], 32)
                self.assertTrue(any(item["kind"] == "source-observation"
                                    for item in suppressed_change["observation"]["attention"]))

                enabled = progress.observe_experiment(fixture["run"], fixture["registry"],
                    1121, row_key="run", started_at=100, final=True, status="succeeded")
                enabled_state = progress._read_observation_state(fixture["state"])
                self.assertEqual([item["key"] for item in enabled["delivery"]], ["final:run"])
                self.assertEqual(notify.call_count, 1)
                self.assertTrue(notify.call_args.kwargs["subject"].startswith("experiment final "))
                self.assertIn("raw counter=finished_items:32", notify.call_args.kwargs["text"])
                self.assertEqual(enabled_state["event_keys"], ["final:run"])
                repeated = progress.observe_experiment(fixture["run"], fixture["registry"],
                    1182, row_key="run", started_at=100, final=True, status="succeeded")
                self.assertFalse(repeated["delivery"])
                self.assertEqual(notify.call_count, 1)
                self.assertEqual(progress._read_observation_state(
                    fixture["state"])["event_keys"], ["final:run"])

    def test_unavailable_notice_recipient_does_not_block_the_other_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact"
            artifact.mkdir()
            output = root / "run"
            output.mkdir()
            state = root / "runtime" / "state.json"
            state.parent.mkdir()
            run = {"run_id": "partial-fanout", "started_at": 0,
                   "artifact_root": str(artifact), "route_id": "route-a", "node": "eval",
                   "status": "running",
                   "owner_wait": {"harness": "codex", "session_id": "owner-thread"},
                   "session": {"harness": "claude", "id": "supervisor-session"}}
            with mock.patch.object(progress, "_observation_state_path", return_value=state), \
                    mock.patch("session_notice.notify", side_effect=[OSError("fixture unavailable"),
                                                                     {"state": "acked"}]) as notify:
                result = progress.observe_experiment(run, root / "registry.json", 7200,
                    root=str(output), row_key="run", started_at=0)
            self.assertEqual(notify.call_count, 2)
            recipient_states = {item["state"] for item in result["delivery"][0]["recipients"]}
            self.assertEqual(recipient_states, {"unavailable", "acked"})
            persisted = progress._read_observation_state(state)
            self.assertEqual(len(persisted["outbox"]), 1)

    @property
    def tmp(self):
        if not hasattr(self, "_public_tmp"):
            self._public_tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._public_tmp.cleanup)
        return self._public_tmp

    def _legacy_record(self, run):
        """Legacy-shaped record (>= 967,730 B): list-form declarations and a fat row."""
        files = sorted(str(item) for item in run.glob("*-epoch-*.json"))
        point = {"epoch": 3, "value": 1.5, "nonfinite": False, "path": files[0]}
        summary = {"metric": "L_se", "direction": "min", "latest": point, "best": point,
                   "latest_metrics": {f"k{i}": point for i in range(40)},
                   "best_metrics": {f"k{i}": point for i in range(40)},
                   "trend": "개선", "baseline_delta": None, "nonfinite": False}
        declarations = [{"format": "epoch-json-files", "path": files[:130 + index],
                         "keys": ["L_se", "SISNRi"]} for index in range(37)]
        declarations.append({"format": "tfevents", "keys": ["loss"],
                             "path": [str(run / "tensorboard" / "events.out.tfevents.1.host")]})
        return {"schema_version": 1, "run_id": "legacy-run", "route_id": "route-a", "node": "train",
                "campaign_id": "c", "cycle_id": "y", "host": "h1", "output_dir": str(run),
                "status": "running", "metrics": declarations, "metric_summary": summary,
                "rows": [{"row_id": "run", "output_dir": str(run), "metrics": declarations[:3],
                          "sources": declarations[:3], "metric_summary": summary,
                          "last_checkpoint": {"at": 5.0}, "elapsed_seconds": 5.0,
                          "observed_counters": [], "started_at": 0.0}]}

    def _write_legacy(self, artifact, record):
        target = artifact / "resource-runs" / "legacy-run.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(record, sort_keys=True) + "\n")
        self.assertGreaterEqual(target.stat().st_size, 967730)
        return target

    def test_oversized_legacy_record_shrinks_after_one_write(self):
        base = Path(self.tmp.name) / "d1"
        run = _legacy_epoch_run(base)
        artifact = base / "artifact"
        legacy = self._legacy_record(run)
        target = self._write_legacy(artifact, legacy)
        state = base / "state.json"
        runinfo = {"run_id": "legacy-run", "artifact_root": str(artifact),
                   "route_id": "route-a", "node": "train", "started_at": 0}
        with mock.patch.object(progress, "_observation_state_path", return_value=state):
            progress.observe_experiment(runinfo, base / "registry.json", 60, root=str(run),
                                        row_key="run", started_at=0, notify_events=False)
        self.assertLessEqual(target.stat().st_size, 16384)
        record = json.loads(target.read_text())
        self.assertFalse([item for item in record["metrics"] if isinstance(item["path"], list)])
        epoch = [item for item in record["metrics"] if item["format"] == "epoch-json-files"]
        self.assertEqual([(item["path"], item["file_count"] >= 170) for item in epoch],
                         [(str(run), True)])
        self.assertTrue({"metrics", "sources"}.isdisjoint(record["rows"][0]))
        self.assertEqual(record["output_dir_state"], "present")
        self.assertEqual(_cairn_problems(target), [])
        # the same legacy file through the writer alone, with a minimal incoming record
        other = base / "artifact2"
        target2 = self._write_legacy(other, legacy)
        self.assertIsNotNone(progress.write_public_run_record(
            other, "legacy-run", {"run_id": "legacy-run", "status": "running"}))
        self.assertLessEqual(target2.stat().st_size, 16384)
        self.assertEqual(_cairn_problems(target2), [])
        self.assertEqual(json.loads(target2.read_text())["rows"][0]["row_id"], "run")

    def test_public_record_compaction_is_bounded_deterministic_and_idempotent(self):
        base = Path(self.tmp.name) / "d1b"
        long = "p" * 90

        def extreme(identity):
            summary = {"metric": "L_se", "latest_metrics": {f"m{i:03d}": {"epoch": 1, "value": 1.0}
                                                             for i in range(80)},
                       "best_metrics": {f"m{i:03d}": {"epoch": 1, "value": 1.0} for i in range(80)}}
            return {"run_id": "extreme", "status": "running", "output_dir": str(base),
                    "campaign_id": identity, "host": identity, "node": identity,
                    "cycle_id": identity, "route_id": identity, "observer_of": identity,
                    "metrics": [{"format": "epochs-jsonl", "path": f"/{long}/{i}/logs/epochs.jsonl",
                                 "keys": [f"key-{i}-{n}" for n in range(200)]} for i in range(40)],
                    "source_warnings": [f"warning-{n:04d}" for n in range(500)],
                    "metric_summary": summary,
                    "rows": [{"row_id": f"arm-{n:02d}", "started_at": float(n), "metric_summary": summary,
                              "admission": {"detail": "a" * 600}, "observed_counters": [{"n": "b" * 300}],
                              "last_checkpoint": {"at": float(n), "rule_fired": ["r"] * 30}}
                             for n in range(64)]}
        for identity, expect_rows in (("short", True), ("i" * 2000, False)):
            outputs = []
            for name in ("one", "two"):
                artifact = base / identity[:1] / name
                path = progress.write_public_run_record(artifact, "extreme", extreme(identity))
                self.assertIsNotNone(path)
                self.assertLessEqual(path.stat().st_size, 16384)
                outputs.append(path.read_bytes())
            self.assertEqual(outputs[0], outputs[1])
            record = json.loads(outputs[0])
            self.assertLessEqual(len(record["metrics"]), 16)
            self.assertGreater(record["compaction"]["level"], 0)
            self.assertEqual(bool(record["rows"]), expect_rows)
            if expect_rows:
                self.assertGreaterEqual(record["compaction"]["rows_omitted"], 32)
            else:
                self.assertEqual(record["compaction"]["level"], 7)
                self.assertIn("host", record["compaction"]["oversize_fields"])
            self.assertEqual(record["compaction"]["budget_bytes"], 16384)
            # idempotent: writing the compacted record back keeps every byte
            again = progress.write_public_run_record(artifact, "extreme",
                progress.read_public_run_record(artifact, "extreme"))
            self.assertEqual(again.read_bytes(), outputs[1])

    def test_thirty_two_live_arms_fit_and_keep_every_live_row(self):
        base = Path(self.tmp.name) / "d3arms"
        artifact = base / "artifact"
        for index in range(32):
            output = base / f"row-{index:02d}"
            (output / "logs").mkdir(parents=True)
            (output / "logs" / "epochs.jsonl").write_text(json.dumps(
                {"epoch": 1, "split": "valid", "losses": {"L_se": -1.0 - index, "SISNRi": 1.0 * index}}) + "\n")
            run = {"run_id": "arms-32", "route_id": "r", "node": "train",
                   "artifact_root": str(artifact), "started_at": index}
            with mock.patch.object(progress, "_observation_state_path",
                                   return_value=base / f"state-{index}.json"):
                progress.observe_experiment(run, base / "registry.json", index + 7200,
                    root=str(output), row_key=f"arm-{index:02d}", started_at=index,
                    notify_events=False)
        path = artifact / "resource-runs" / "arms-32.json"
        self.assertLessEqual(path.stat().st_size, 16384)
        record = json.loads(path.read_text())
        self.assertEqual({row["row_id"] for row in record["rows"]},
                         {f"arm-{index:02d}" for index in range(32)})
        self.assertEqual(len(record["metrics"]), 16)
        self.assertEqual(record["compaction"]["metrics_omitted"], 16)
        self.assertEqual(_cairn_problems(path), [])

    def test_output_dir_state_present_none_unknown_with_reason(self):
        base = Path(self.tmp.name) / "d2"
        base.mkdir()
        route = base / "route.json"
        route.write_text(json.dumps({"nodes": [
            {"id": "eval-run", "kind": "resource-runner", "outputs": ["run.json", "raw-results/**"]},
            {"id": "full-run", "kind": "resource-runner",
             "outputs": ["run.json", "logs/**", "experiment-artifact"]}]}))
        link = base / "route-link.json"
        link.symlink_to(route)
        big = base / "route-big.json"
        big.write_text(json.dumps({"nodes": [{"id": "eval-run", "kind": "resource-runner",
                                              "outputs": [], "pad": "x" * (1024 * 1024)}]}))
        output = base / "out"
        (output / "logs").mkdir(parents=True)
        (output / "logs" / "epochs.jsonl").write_text(
            json.dumps({"epoch": 1, "split": "valid", "losses": {"L_se": -1.0}}) + "\n")

        def observe(name, node, route_file, root):
            artifact = base / name
            run = {"run_id": name, "artifact_root": str(artifact), "node": node,
                   "route_id": "r", "started_at": 0}
            if route_file:
                run["route"] = str(route_file)
            with mock.patch.object(progress, "_observation_state_path",
                                   return_value=base / (name + ".state")):
                progress.observe_experiment(run, base / "registry.json", 60, root=root,
                    row_key="run", started_at=0, notify_events=False)
            return progress.read_public_run_record(artifact, name), artifact

        cases = {
            "case-a": ("eval-run", route, None, ("none", "node-declares-no-training-output")),
            "case-b": ("full-run", route, None, ("unknown", "not-declared")),
            "case-c": ("missing-node", route, None, ("unknown", "not-declared")),
            "case-d": ("eval-run", route, str(base / "absent"), ("unknown", "declared-unresolved")),
            "case-e": ("eval-run", route, str(output), ("present", None)),
            "case-g1": ("eval-run", link, None, ("unknown", "not-declared")),
            "case-g2": ("eval-run", big, None, ("unknown", "not-declared")),
        }
        for name, (node, route_file, root, expected) in cases.items():
            record, _artifact = observe(name, node, route_file, root)
            self.assertEqual((record["output_dir_state"], record["output_dir_reason"]), expected, name)
            if expected[0] != "present":
                self.assertIsNone(record["output_dir"], name)
        # (f) a present folder is carried over a later unknown observation
        record, artifact = observe("case-e", "eval-run", route, str(output))
        later = progress.public_run_record({"run_id": "case-e", "node": "eval-run"})
        self.assertEqual(later["output_dir_state"], "unknown")
        self.assertIsNotNone(progress.write_public_run_record(artifact, "case-e", later))
        kept = progress.read_public_run_record(artifact, "case-e")
        self.assertEqual((kept["output_dir_state"], kept["output_dir"]), ("present", str(output)))

    def test_compacted_record_feeds_resource_wait_note_and_runner_observation(self):
        import dispatch_resource_wait
        base = Path(self.tmp.name) / "d3notes"
        run_dir = _legacy_epoch_run(base)
        artifact = base / "artifact"
        self._write_legacy(artifact, self._legacy_record(run_dir))
        runinfo = {"run_id": "legacy-run", "artifact_root": str(artifact),
                   "route_id": "route-a", "node": "train", "started_at": 0}
        with mock.patch.object(progress, "_observation_state_path", return_value=base / "state.json"):
            progress.observe_experiment(runinfo, base / "registry.json", 60, root=str(run_dir),
                                        row_key="run", started_at=0, notify_events=False)
        note = dispatch_resource_wait._experiment_result_note(
            {"run_id": "legacy-run", "artifact_root": str(artifact)})
        self.assertNotIn("metrics unavailable", note)
        for needle in ("row=", "last=", "trend=", "public run record="):
            self.assertIn(needle, note)
        # heavily compacted record (rows dropped to the skeleton) still renders
        skeleton_root = base / "skeleton"
        self.assertIsNotNone(progress.write_public_run_record(skeleton_root, "legacy-run",
            {"run_id": "legacy-run", "host": "h" * 2000, "output_dir": str(run_dir),
             "metric_summary": {"metric": "L_se"}}))
        self.assertIsInstance(dispatch_resource_wait._experiment_result_note(
            {"run_id": "legacy-run", "artifact_root": str(skeleton_root)}), str)
        registry = base / "resource-runs.json"
        registry.write_text(json.dumps({"runs": {"legacy-run": {
            "run_id": "legacy-run", "artifact_root": str(artifact)}}}))
        command = [sys.executable, str(Path(__file__).with_name("resource-runner.py")),
                   "--registry", str(registry), "observation", "--run-id", "legacy-run", "--json"]
        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": "/tmp"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         progress.read_public_run_record(artifact, "legacy-run"))

    def test_private_observation_state_keeps_four_mib_read_limit(self):
        self.assertEqual(progress.MAX_PUBLIC_RUN_RECORD_BYTES, 4 * 1024 * 1024)
        self.assertEqual(progress.MAX_PUBLIC_RUN_RECORD_WRITE_BYTES, 16 * 1024)
        base = Path(self.tmp.name) / "d4"
        base.mkdir()
        medium = base / "medium.json"
        medium.write_text(json.dumps({"run_id": "r", "pad": "x" * (1024 * 1024)}))
        self.assertEqual(progress._read_observation_state(medium)["run_id"], "r")
        huge = base / "huge.json"
        huge.write_bytes(b" " * (4 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            progress._read_observation_state(huge)

    def test_missing_historical_metric_location_is_dropped_not_published(self):
        base = Path(self.tmp.name) / "t7"
        first, second = _legacy_epoch_run(base / "a", 5), _legacy_epoch_run(base / "b", 5)
        artifact = base / "artifact"
        decl = lambda folder: {"format": "epoch-json-files", "path": str(folder), "file_count": 340}
        self.assertIsNotNone(progress.write_public_run_record(artifact, "hist",
            {"output_dir": str(second), "metrics": [decl(first)]}))
        import shutil
        shutil.rmtree(first)
        self.assertIsNotNone(progress.write_public_run_record(artifact, "hist",
            {"output_dir": str(second), "metrics": [decl(second)]}))
        record = progress.read_public_run_record(artifact, "hist")
        self.assertEqual([item["path"] for item in record["metrics"]], [str(second)])
        self.assertGreaterEqual(record["compaction"]["metrics_omitted"], 1)
        self.assertEqual(_cairn_problems(artifact / "resource-runs" / "hist.json"), [])

    def test_metrics_omitted_is_stable_under_repeated_and_recompacted_writes(self):
        base = Path(self.tmp.name) / "omit"
        artifact = base / "artifact"
        decls = []
        for index in range(20):
            folder = base / f"run-{index:02d}"
            folder.mkdir(parents=True)
            decls.append({"format": "epoch-json-files", "path": str(folder), "file_count": 3})
        record = lambda: {"output_dir": str(base / "run-00"), "metrics": [dict(d) for d in decls]}
        seen = []
        for _ in range(4):
            path = progress.write_public_run_record(artifact, "omit", record())
            seen.append((json.loads(path.read_text())["compaction"]["metrics_omitted"],
                         path.read_bytes()))
        self.assertEqual({item[0] for item in seen}, {4})
        self.assertEqual(len({item[1] for item in seen}), 1)
        # read/write idempotence keeps the carried count
        again = progress.write_public_run_record(artifact, "omit",
            progress.read_public_run_record(artifact, "omit"))
        self.assertEqual(again.read_bytes(), seen[-1][1])
        stored = progress.read_public_run_record(artifact, "omit")
        self.assertEqual(progress._compact_public_record(stored), stored)
        self.assertEqual(progress._compact_public_record(stored, stored), stored)
        # a builder-normalized input already carries its own count; merging stays at 4
        normalized = progress._compact_public_record(record())
        self.assertEqual(normalized["compaction"]["metrics_omitted"], 4)
        merged = progress.write_public_run_record(artifact, "omit", normalized)
        self.assertEqual(json.loads(merged.read_text())["compaction"]["metrics_omitted"], 4)
        # a genuinely larger shortfall still raises it
        extra = base / "run-extra"
        extra.mkdir()
        decls.append({"format": "epoch-json-files", "path": str(extra), "file_count": 3})
        grown = progress.write_public_run_record(artifact, "omit", record())
        self.assertEqual(json.loads(grown.read_text())["compaction"]["metrics_omitted"], 5)
        self.assertLessEqual(grown.stat().st_size, 16384)


if __name__ == "__main__":
    unittest.main()
