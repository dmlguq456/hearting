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


class ExperimentObservationTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
