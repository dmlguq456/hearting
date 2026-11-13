#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

import resource_metric_sources as metrics
import resource_progress as progress


def vi(value):
    out = bytearray()
    while value > 0x7f:
        out.append((value & 0x7f) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, wire, value):
    tag = vi(number * 8 + wire)
    if wire == 0:
        return tag + vi(value)
    if wire == 2:
        return tag + vi(len(value)) + value
    return tag + value


def event(step, tag, tensor, dtype=1):
    shape = b""  # TensorShapeProto with no dimensions: scalar.
    tensor_proto = field(1, 0, dtype) + field(2, 2, shape) + tensor
    value = field(1, 2, tag.encode()) + field(8, 2, tensor_proto)
    summary = field(1, 2, value)
    return field(2, 0, step) + field(5, 2, summary)


class MetricSourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_fixed_json_sources_decode_and_keep_progress_separate(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "epochs.jsonl").write_text(
            '{"epoch":1,"split":"valid","losses":{"L_se":-9.0}}\n'
            '{"epoch":2,"train":{"losses":{"loss":0.4}},"valid":{"losses":{"loss":0.3}}}\n'
            '{"step":2,"split":"train","name":"loss","value":0.5}\n')
        (self.root / "valid-epoch-0031.json").write_text(
            json.dumps({"epoch": 31, "losses": {"L_se": -8.77, "SISNRi": 14.62},
                        "seconds": 848.9}))
        (self.root / "train-epoch-0031.json").write_text(
            json.dumps({"epoch": 31, "losses": {"L_se": -9.1}, "seconds": 800.0}))
        (self.root / "progress.json").write_text(
            '{"reached_epoch":31,"total_epochs":100,"best_updated":true}')
        value = metrics.read_sources(self.root)
        valid = next(row for row in value["samples"]
                     if row["split"] == "valid" and row["epoch"] == 31)
        self.assertEqual(valid["metrics"], {"L_se": -8.77, "SISNRi": 14.62})
        self.assertEqual(valid["elapsed_seconds"], 848.9)
        self.assertEqual(value["progress"]["total_epochs"], 100)
        self.assertEqual({row["split"] for row in value["samples"]}, {"valid", "train"})
        self.assertEqual(next(row for row in value["samples"]
                              if row["split"] == "valid" and row["epoch"] == 2)["metrics"],
                         {"loss": 0.3})
        self.assertEqual(next(row for row in value["samples"]
                              if row["split"] == "valid" and row["epoch"] == 1)["format"],
                         "epochs-jsonl")
        self.assertIn("epoch-json-files", {row["format"] for row in value["sources"]})
        self.assertNotIn("metrics.jsonl", [row["path"] for row in value["sources"]])

    def test_jsonl_partial_tail_is_not_consumed_until_newline(self):
        logs = self.root / "logs"
        logs.mkdir()
        path = logs / "epochs.jsonl"
        path.write_bytes(b'{"step":1,"split":"valid","name":"loss","value":2}\n'
                         b'{"step":2,"split":"valid","name":"loss","value":1}')
        samples, cursor, warnings, paths = metrics.decode_jsonl(self.root)
        self.assertEqual([item["epoch"] for item in samples], [1])
        self.assertIn("epochs-jsonl-partial-record", warnings)
        path.write_bytes(path.read_bytes() + b"\n")
        samples2, cursor2, _, _ = metrics.decode_jsonl(self.root, cursor)
        self.assertEqual([item["epoch"] for item in samples2], [2])
        self.assertGreater(cursor2["offset"], cursor["offset"])

    def test_tensorboard_bytes_decode_simple_and_tensorproto_scalars(self):
        directory = self.root / "tensorboard"
        directory.mkdir()
        path = directory / "events.out.tfevents.fixture"
        rows = [
            metrics.encode_tfrecord(event(4, "valid/L_se", field(5, 5, struct.pack("<f", -8.5)))),
            metrics.encode_tfrecord(event(5, "train/accuracy", field(5, 5, struct.pack("<f", 0.75)))),
            metrics.encode_tfrecord(event(6, "valid/score", field(6, 1, struct.pack("<d", 12.25)), 2)),
            metrics.encode_tfrecord(event(7, "valid/count", field(10, 0, 42), 9)),
            metrics.encode_tfrecord(event(8, "valid/content", field(4, 2, struct.pack("<f", 3.5)))),
        ]
        path.write_bytes(b"".join(rows))
        samples, warnings, paths, _cursor = metrics.decode_tfevents(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual(len(samples), 5)
        values = {(row["epoch"], row["split"], next(iter(row["metrics"].items())))
                  for row in samples}
        self.assertIn((4, "valid", ("L_se", -8.5)), values)
        self.assertIn((5, "train", ("accuracy", 0.75)), values)
        self.assertIn((6, "valid", ("score", 12.25)), values)
        self.assertIn((7, "valid", ("count", 42.0)), values)
        self.assertIn((8, "valid", ("content", 3.5)), values)
        self.assertEqual(paths, [str(path)])

    def test_crc_bad_record_and_symlinked_source_are_reported(self):
        directory = self.root / "tensorboard"
        directory.mkdir()
        path = directory / "events.out.tfevents.fixture"
        raw = bytearray(metrics.encode_tfrecord(event(1, "valid/loss",
                                      field(5, 5, struct.pack("<f", 1.0)))))
        raw[-1] ^= 1
        path.write_bytes(raw)
        samples, warnings, _, _cursor = metrics.decode_tfevents(self.root)
        self.assertEqual(samples, [])
        self.assertIn("tfevents-data-crc-invalid", warnings)
        path.unlink()
        outside = self.root / "outside"
        outside.write_text('{"step":1,"split":"valid","name":"loss","value":1}\n')
        (self.root / "logs").symlink_to(self.root)
        _, _, warnings, _ = metrics.decode_jsonl(self.root)
        self.assertTrue(warnings)

    def test_tfevents_cursor_resumes_partial_record_and_resets_on_rotation(self):
        directory = self.root / "tensorboard"
        directory.mkdir()
        path = directory / "events.out.tfevents.fixture"
        first = metrics.encode_tfrecord(event(1, "valid/loss", field(5, 5, struct.pack("<f", 2.0))))
        second = metrics.encode_tfrecord(event(2, "valid/loss", field(5, 5, struct.pack("<f", 1.0))))
        path.write_bytes(first + second[:8])
        samples, warnings, _, cursor = metrics.decode_tfevents(self.root)
        self.assertEqual([row["epoch"] for row in samples], [1])
        self.assertIn("tfevents-partial-record", warnings)
        path.write_bytes(first + second)
        samples, _, _, cursor = metrics.decode_tfevents(self.root, cursor)
        self.assertEqual([row["epoch"] for row in samples], [2])
        replacement = path.with_suffix(".rotated")
        replacement.write_bytes(first)
        replacement.replace(path)
        samples, warnings, _, reset_cursor = metrics.decode_tfevents(self.root, cursor)
        self.assertEqual([row["epoch"] for row in samples], [1])
        self.assertTrue(any("generation-changed" in warning for warning in warnings))
        self.assertEqual(reset_cursor[path.name]["offset"], len(first))

    def test_admission_reader_binds_run_content_and_deduplicates_snapshots_and_leases(self):
        root = self.root / "registry-parent"
        bundle = root / "arbitrary-bundle-name"
        bundle.mkdir(parents=True)
        registry = root / "registry.json"
        registry.write_text("{}\n")
        (bundle / "execution-record.data").write_text(json.dumps({
            "run_id": "run-a", "started_at": 50, "row_id": "row-a"}))
        admission = {
            "row_id": "row-a",
            "admitted": False,
            "snapshot": {"t": 100},
            "first_snapshot": {"t": 20},
            "reasons": ["gpu capacity", "gpu capacity"],
            "preceding_evaluation_leases": [
                {"task": "evaluation", "host": "host-a", "gpu": 0},
                {"task": "evaluation", "host": "host-a", "gpu": 0},
                {"kind": "shared_admission", "task": "shared"},
            ],
        }
        (bundle / "unrelated-file-prefix").write_text(json.dumps(admission))
        result = metrics.read_admission_sources([root], run_id="run-a", row_key="row-a",
            registry_path=registry, started_at=50, now=110)
        self.assertEqual(result["coverage"], "observed")
        sample, = result["samples"]
        self.assertEqual((sample["state"], sample["at"]), ("denied", 100.0))
        self.assertEqual(sample["reasons"], ["gpu capacity"])
        self.assertEqual(sample["priority_blockers"], [
            {"task": "evaluation", "host": "host-a", "gpu": 0}])
        self.assertNotIn("wait_duration_seconds", sample)
        self.assertNotIn("time_ratio", sample)
        replay = metrics.read_admission_sources([root], run_id="run-a", row_key="row-a",
            registry_path=registry, started_at=50, now=111, cursor=result["cursor"])
        self.assertEqual(replay["samples"], [])
        self.assertEqual(replay["latest"]["sample_id"], sample["sample_id"])

    def test_row_query_does_not_inherit_run_wide_decision_from_execution_record(self):
        root = self.root / "row-with-run-wide-decision"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "execution.json").write_text(json.dumps({
            "run_id": "run-1", "row_id": "row-1", "started_at": 0}))
        (bundle / "admission.json").write_text(json.dumps({
            "run_id": "run-1", "admitted": False, "snapshot": {"t": 20},
            "reasons": ["run-wide capacity"]}))

        row = metrics.read_admission_sources([root], run_id="run-1", row_key="row-1",
            started_at=0, now=21)
        self.assertEqual(row["coverage"], "observed")
        self.assertEqual(row["latest"]["state"], "unknown")
        self.assertEqual([sample["state"] for sample in row["samples"]], ["unknown"])
        self.assertEqual(row["latest"]["reasons"], [])

        run = metrics.read_admission_sources([root], run_id="run-1", started_at=0, now=21)
        self.assertEqual(run["coverage"], "observed")
        self.assertEqual(run["latest"]["state"], "denied")
        self.assertEqual([sample["state"] for sample in run["samples"]], ["denied"])
        self.assertEqual(run["latest"]["reasons"], ["run-wide capacity"])

    def test_row_query_accepts_decision_with_its_own_exact_row_identity(self):
        root = self.root / "exact-row-decision"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "admission.json").write_text(json.dumps({
            "run_id": "run-1", "row_id": "row-1", "admitted": False,
            "snapshot": {"t": 20}}))

        row = metrics.read_admission_sources([root], run_id="run-1", row_key="row-1",
            started_at=0, now=21)
        self.assertEqual(row["coverage"], "observed")
        self.assertEqual(row["latest"]["state"], "denied")
        self.assertEqual(len(row["samples"]), 1)

    def test_row_queries_select_each_decision_identity_from_one_bundle(self):
        root = self.root / "multiple-row-decisions"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "admissions.json").write_text(json.dumps([
            {"run_id": "run-1", "row_id": "row-1", "admitted": False,
             "snapshot": {"t": 20}},
            {"run_id": "run-1", "row_id": "row-2", "admitted": True,
             "snapshot": {"t": 21}},
        ]))

        first = metrics.read_admission_sources([root], run_id="run-1", row_key="row-1",
            started_at=0, now=22)
        second = metrics.read_admission_sources([root], run_id="run-1", row_key="row-2",
            started_at=0, now=22)
        self.assertEqual((first["latest"]["state"], len(first["samples"])), ("denied", 1))
        self.assertEqual((second["latest"]["state"], len(second["samples"])), ("admitted", 1))
        self.assertNotEqual(first["latest"]["sample_id"], second["latest"]["sample_id"])

    def test_admission_reader_rejects_missing_nested_and_foreign_bindings(self):
        wrong = self.root / "wrong"
        (wrong / "bundle").mkdir(parents=True)
        (wrong / "bundle" / "record.json").write_text('{"started_at":0}')
        (wrong / "bundle" / "admission.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        result = metrics.read_admission_sources([wrong], run_id="target", now=20)
        self.assertEqual(result["coverage"], "unknown")
        self.assertIsNone(result["latest"])
        self.assertEqual(result["samples"], [])

        nested_reference = self.root / "nested-reference"
        (nested_reference / "bundle").mkdir(parents=True)
        (nested_reference / "bundle" / "record.json").write_text(
            '{"other":{"run_id":"target"}}')
        (nested_reference / "bundle" / "admission.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        result = metrics.read_admission_sources([nested_reference], run_id="target", now=20)
        self.assertEqual(result["coverage"], "unknown")
        self.assertIsNone(result["latest"])
        self.assertEqual(result["samples"], [])

        foreign = self.root / "foreign"
        (foreign / "bundle").mkdir(parents=True)
        (foreign / "bundle" / "record.json").write_text('{"run_id":"other"}')
        (foreign / "bundle" / "admission.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        result = metrics.read_admission_sources([foreign], run_id="target", now=20)
        self.assertEqual(result["coverage"], "unknown")
        self.assertIsNone(result["latest"])
        self.assertEqual(result["samples"], [])

        foreign_row = self.root / "foreign-row"
        (foreign_row / "bundle").mkdir(parents=True)
        (foreign_row / "bundle" / "record.json").write_text(
            '{"run_id":"target","row_id":"row-other","started_at":0}')
        (foreign_row / "bundle" / "admission.json").write_text(
            '{"admitted":false,"row_id":"row-other","snapshot":{"t":10}}')
        result = metrics.read_admission_sources([foreign_row], run_id="target",
            row_key="row-target", started_at=0, now=20)
        self.assertEqual((result["coverage"], result["latest"]), ("unknown", None))

    def test_admission_reader_reports_invalid_links_and_json_as_partial_unknown(self):
        root = self.root / "malformed"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text('{"run_id":"target","started_at":0}')
        (bundle / "duplicate-keys.json").write_text(
            '{"admitted":false,"admitted":true,"snapshot":{"t":10}}')
        (bundle / "nonfinite.json").write_text(
            '{"admitted":false,"snapshot":{"t":NaN}}')
        result = metrics.read_admission_sources([root], run_id="target", started_at=0, now=20)
        self.assertEqual(result["coverage"], "partial")
        self.assertIsNone(result["latest"])
        self.assertTrue(any("duplicate-key" in warning for warning in result["warnings"]))
        self.assertTrue(any("nonfinite" in warning for warning in result["warnings"]))
        link_root = self.root / "link-root"
        link_root.symlink_to(root, target_is_directory=True)
        linked = metrics.read_admission_sources([link_root], run_id="target", now=20)
        self.assertEqual(linked["coverage"], "partial")
        self.assertTrue(any("symlink" in warning for warning in linked["warnings"]))

    def test_admission_reader_resumes_fairly_and_selects_exact_run(self):
        root = self.root / "many-bundles"
        for name, run_id, stamp in (("bundle-a", "other", 10),
                                     ("bundle-b", "target", 20)):
            bundle = root / name
            bundle.mkdir(parents=True)
            (bundle / "record.json").write_text(json.dumps({
                "run_id": run_id, "started_at": 0}))
            (bundle / "decision.json").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": stamp}}))
        cursor = None
        original_scandir = os.scandir
        pass_yields = [0]

        class OrderedScandir:
            def __init__(self, entries, counter):
                self.entries = entries
                self.counter = counter
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.index >= len(self.entries):
                    raise StopIteration
                entry = self.entries[self.index]
                self.index += 1
                self.counter[0] += 1
                return entry

        for reverse_roots in (False, True):
            for reverse_records in (False, True):
                cursor = None
                samples = {}
                def ordered_scandir(path):
                    entries = list(original_scandir(path))
                    path = Path(path)
                    reverse = ((path == root and reverse_roots)
                               or (path.parent == root and reverse_records))
                    entries.sort(key=lambda entry: entry.name, reverse=reverse)
                    return OrderedScandir(entries, pass_yields)
                with self.subTest(reverse_roots=reverse_roots,
                                  reverse_records=reverse_records), \
                        mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 3), \
                        mock.patch.object(metrics.os, "scandir", ordered_scandir):
                    for _ in range(8):
                        pass_yields[0] = 0
                        result = metrics.read_admission_sources([root], run_id="target",
                            started_at=0, now=30, cursor=cursor)
                        cursor = result["cursor"]
                        for sample in result["samples"]:
                            samples[sample["at"]] = sample
                        self.assertEqual(result["entries"], pass_yields[0])
                        self.assertLessEqual(result["entries"], 3)
                    self.assertEqual(set(samples), {20.0})

    def test_admission_reader_does_not_inherit_partial_binding_after_bundle_replacement(self):
        root = self.root / "partial-replacement"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "01-record.json").write_text(
            '{"run_id":"target","started_at":0}')
        (bundle / "02-decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        with mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 4), \
                mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 45):
            partial = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20)
            self.assertEqual(partial["coverage"], "partial")
            self.assertEqual(partial["samples"], [])
            partial_state = partial["cursor"]["scan"][str(root)]
            self.assertTrue(partial_state["bundle_cursors"]["bundle"]["last_file"])

            stale = root / "bundle-old"
            bundle.rename(stale)
            for previous_file in stale.iterdir():
                previous_file.unlink()
            stale.rmdir()
            bundle.mkdir()
            (bundle / "01-record.json").write_text(
                '{"run_id":"other","started_at":0}')
            (bundle / "02-decision.json").write_text(
                '{"admitted":false,"snapshot":{"t":15}}')
            changed = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20, cursor=partial["cursor"])
            cursor = changed["cursor"]
            emitted = []
            latest = None
            for _ in range(8):
                page = metrics.read_admission_sources([root], run_id="target",
                    started_at=0, now=20, cursor=cursor)
                emitted.extend(page["samples"])
                latest = page.get("latest") or latest
                cursor = page["cursor"]
        self.assertEqual(changed["samples"], [])
        self.assertIn("admission-bundle-generation-changed", changed["warnings"])
        self.assertEqual(emitted, [])
        self.assertIsNone(latest)

    def test_admission_reader_keeps_binding_when_record_and_decision_are_on_separate_pages(self):
        root = self.root / "split-binding-pages"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        for run_first in (True, False):
            for path in bundle.iterdir():
                path.unlink()
            names = (("01-run.json", "02-decision.json") if run_first
                     else ("02-run.json", "01-decision.json"))
            (bundle / names[0]).write_text('{"run_id":"target","started_at":0}')
            (bundle / names[1]).write_text(
                '{"admitted":false,"snapshot":{"t":10}}')
            with self.subTest(run_first=run_first), \
                    mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 4), \
                    mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 45):
                first = metrics.read_admission_sources([root], run_id="target",
                    started_at=0, now=20)
                self.assertEqual(first["coverage"], "partial")
                self.assertEqual(first["samples"], [])
                expected_first = min(names)
                self.assertEqual(first["cursor"]["scan"][str(root)]
                    ["bundle_cursors"]["bundle"]["last_file"], expected_first)
                cursor = first["cursor"]
                pages = []
                for _ in range(4):
                    page = metrics.read_admission_sources([root], run_id="target",
                        started_at=0, now=20, cursor=cursor)
                    pages.append(page)
                    cursor = page["cursor"]
                    if page["coverage"] == "observed":
                        break
                emitted = [sample for page in pages for sample in page["samples"]]
                self.assertEqual([sample["state"] for sample in emitted], ["denied"])
                self.assertEqual(pages[-1]["latest"]["state"], "denied")

    def test_initial_binding_continues_across_additions_before_and_after_cursor(self):
        root = self.root / "changing-initial-binding"
        bundle = root / "arbitrary-bundle"
        bundle.mkdir(parents=True)
        run_id = "fixture-run-41"
        for index in range(40):
            (bundle / f"decision-{index:03d}.data").write_text(json.dumps({
                "admitted": index % 3 != 0, "sample_index": index,
                "snapshot": {"t": 100 + index}}))
        (bundle / "zzzzzz-run-context.data").write_text(json.dumps({
            "run_id": run_id, "started_at": 1}))

        additions = 0
        expected_admitted = sum(index % 3 != 0 for index in range(40))

        def add_decision(name, index):
            nonlocal additions, expected_admitted
            admitted = index % 3 != 0
            (bundle / name).write_text(json.dumps({
                "admitted": admitted, "sample_index": index,
                "snapshot": {"t": 1000 + index}}))
            additions += 1
            expected_admitted += admitted

        cursor = None
        seen = {}
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 160):
            page = metrics.read_admission_sources([root], run_id=run_id,
                started_at=1, now=10000, cursor=cursor)
            self.assertEqual(page["coverage"], "partial")
            initial_state = page["cursor"]["scan"][str(root)]["bundle_cursors"][
                bundle.name]["binding_state"]
            self.assertFalse(initial_state["target_seen"])
            file_cursor = page["cursor"]["scan"][str(root)]["bundle_cursors"][
                bundle.name]["last_file"]
            self.assertTrue(file_cursor)
            add_decision("!before-cursor.data", 100)
            add_decision("zzzz-000-after-cursor.data", 101)

            for turn in range(1, 120):
                bundle_cursor = page["cursor"]["scan"][str(root)]["bundle_cursors"][
                    bundle.name]
                binding = bundle_cursor["binding_state"]
                if not binding["target_seen"]:
                    add_decision(f"zzzz-{turn:03d}-live-addition.data", 1000 + turn)
                page = metrics.read_admission_sources([root], run_id=run_id,
                    started_at=1, now=10000, cursor=page["cursor"])
                seen.update({row["sample_id"]: row for row in page["samples"]})
                if page["coverage"] == "observed":
                    break

        self.assertEqual(page["coverage"], "observed")
        final_binding = page["cursor"]["scan"][str(root)]["bundle_cursors"][
            bundle.name]["binding_state"]
        self.assertTrue(final_binding["target_seen"])
        self.assertTrue(final_binding["binding_complete"])
        self.assertFalse(final_binding["foreign_seen"])
        self.assertGreaterEqual(additions, 20)
        expected_total = 40 + additions
        self.assertEqual(len(seen), expected_total)
        self.assertEqual(sum(row["state"] == "admitted" for row in seen.values()),
                         expected_admitted)
        self.assertTrue(all(row["state"] in {"admitted", "denied"}
                            for row in seen.values()))
        replay = metrics.read_admission_sources([root], run_id=run_id,
            started_at=1, now=10001, cursor=page["cursor"])
        self.assertEqual(replay["coverage"], "observed")
        self.assertEqual(replay["samples"], [])
        self.assertEqual(replay["latest"], page["latest"])

    def test_new_foreign_binding_before_cursor_blocks_exact_attribution(self):
        root = self.root / "new-foreign-context-before-cursor"
        bundle = root / "ordinary-child"
        bundle.mkdir(parents=True)
        for index in range(12):
            (bundle / f"decision-{index:03d}.data").write_text(json.dumps({
                "admitted": True, "sample_index": index,
                "snapshot": {"t": 100 + index}}))
        (bundle / "zzzz-target-context.data").write_text(
            '{"run_id":"selected-identity","started_at":1}')

        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 120):
            page = metrics.read_admission_sources([root], run_id="selected-identity",
                started_at=1, now=1000)
            self.assertEqual(page["coverage"], "partial")
            self.assertFalse(page["cursor"]["scan"][str(root)]["bundle_cursors"]
                [bundle.name]["binding_state"]["target_seen"])
            (bundle / "!new-context.data").write_text(
                '{"run_id":"foreign-identity","started_at":1}')
            seen = {}
            for _ in range(40):
                page = metrics.read_admission_sources([root], run_id="selected-identity",
                    started_at=1, now=1000, cursor=page["cursor"])
                seen.update({row["sample_id"]: row for row in page["samples"]})
                binding = page["cursor"]["scan"][str(root)]["bundle_cursors"][
                    bundle.name]["binding_state"]
                if page["coverage"] == "unknown" and binding["binding_complete"]:
                    break

        self.assertEqual(page["coverage"], "unknown")
        binding = page["cursor"]["scan"][str(root)]["bundle_cursors"][
            bundle.name]["binding_state"]
        self.assertTrue(binding["target_seen"])
        self.assertTrue(binding["foreign_seen"])
        self.assertTrue(binding["binding_complete"])
        self.assertEqual(len(seen), 12)
        self.assertTrue(seen)
        self.assertTrue(all(row["state"] == "unknown" for row in seen.values()))

    def test_admission_reader_invalidates_replaced_foreign_binding_file(self):
        root = self.root / "replaced-binding-file"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        binding = bundle / "01-run.json"
        binding.write_text('{"run_id":"target","started_at":0}')
        (bundle / "02-decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        with mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 4), \
                mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 45):
            partial = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20)
            binding.write_text('{"run_id":"foreign","started_at":0}')
            changed = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20, cursor=partial["cursor"])
            resolved = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=20, cursor=changed["cursor"])
        self.assertIn("admission-bundle-generation-changed", changed["warnings"])
        self.assertEqual(changed["samples"], [])
        self.assertEqual(resolved["coverage"], "unknown")
        self.assertEqual(resolved["samples"], [])
        self.assertIsNone(resolved["latest"])

    def test_admission_reader_keeps_decision_pages_out_of_binding_state(self):
        root = self.root / "bounded-binding-summary"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "01-run.json").write_text(
            '{"run_id":"target","started_at":0}')
        for index in range(8):
            (bundle / f"{index + 2:02d}-decision.json").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": 10 + index}}))
        result = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=30)
        self.assertEqual(result["coverage"], "observed")
        self.assertNotIn("admission-binding-summary-limit", result["warnings"])
        self.assertEqual(len(result["samples"]), 8)
        binding = (result["cursor"]["scan"][str(root)]["bundle_cursors"]["bundle"]
                   ["binding_state"])
        self.assertNotIn("decisions", binding)

    def test_slow_cached_listing_revalidation_resumes_after_persisted_prefix(self):
        root = self.root / "slow-cached-revalidation"
        bundle = root / "selected-bundle"
        sibling = root / "competing-sibling"
        bundle.mkdir(parents=True)
        sibling.mkdir()
        (sibling / "unrelated.md").write_text("foreign sibling context\n")
        registry = root / "resource-runs.json"
        run_id = "fixture-run-0068__a8"
        registry.write_text(json.dumps({"runs": {run_id: {
            "run_id": run_id, "started_at": 1}}}) + "\n")
        for index in range(4):
            (bundle / f"admission-000{index}-00.json").write_text(json.dumps({
                "admitted": index % 2 == 0,
                "snapshot": {"t": 100 + index},
                "shared_admission": {"authorized_run_id": "other-smoke-run"},
                "preceding_evaluation_leases": [{"run_id": "unrelated-lease-run",
                    "task": "evaluation"}],
            }))
        (bundle / "entry.json").write_text(json.dumps({
            "run_id": run_id, "selection_sha256": "a" * 64,
            "selection_replay_sha256": "b" * 64}))

        seed = metrics.read_admission_sources([root], run_id=run_id, row_key="run",
            registry_path=registry, started_at=1, now=200)
        self.assertEqual(seed["coverage"], "observed")
        root_key = str(root)
        cursor = copy.deepcopy(seed["cursor"])
        root_state = cursor["scan"][root_key]
        bundle_progress = root_state["bundle_cursors"][bundle.name]
        listing = bundle_progress["listing"]
        binding = bundle_progress["binding_state"]
        bundle_identity = bundle_progress["identity"]
        bundle_progress["last_file"] = ""
        bundle_progress["binding_state"] = {
            "bundle": bundle.name, "identity": bundle_identity,
            "target_seen": False, "foreign_seen": False,
            "generation_fields": [], "source_files": {}, "binding_sources": {},
            "phase": "binding", "page_decisions": [], "latest": None,
            "recovery_latest": None, "decisions_seen": False,
            "binding_complete": False, "partial": False, "overflow": False,
        }
        self.assertTrue(binding["target_seen"])
        for index, item in enumerate(listing["files"]):
            item.pop("p", None)
            item.pop("d", None)
            if index < 2:
                item["p"] = 1
        cursor["sample_ids"] = []
        cursor["source_latest"] = {}
        cursor["latest_at"] = None
        cursor["historical_latest_at"] = None
        cursor["historical_latest_ids"] = []

        # Model the selected installed cursor's JSON state round-trip before
        # each normal reader continuation/re-adoption.
        cursor = progress._restore_special_numbers(json.loads(json.dumps(
            progress._json_safe(cursor))))
        clock = [0.0]
        original_lstat = Path.lstat

        def slow_cached_lstat(path):
            result = original_lstat(path)
            if (sys._getframe(1).f_code.co_name == "revalidate_listing_entries"
                    and path.parent == bundle):
                clock[0] += 0.1
            return result

        previous = {}
        trace = []
        samples = {}
        result = None
        with mock.patch.object(Path, "lstat", slow_cached_lstat):
            for turn in range(6):
                clock[0] = 0.0
                result = metrics.read_admission_sources([root], run_id=run_id,
                    row_key="run", registry_path=registry, started_at=1,
                    now=201 + turn, cursor=cursor, monotonic=lambda: clock[0],
                    time_budget=0.25)
                _summary, state = progress._admission_summary(previous, result,
                    201 + turn, run_id, "run", 1)
                previous = {**previous, **state}
                samples.update({sample["sample_id"]: sample
                                for sample in result["samples"]})
                cursor = progress._restore_special_numbers(json.loads(json.dumps(
                    progress._json_safe(state["admission_cursor"]))))
                saved = cursor["scan"][root_key]["bundle_cursors"][bundle.name]
                saved_binding = saved["binding_state"]
                trace.append({"turn": turn, "coverage": result["coverage"],
                    "revalidation_after": saved["listing"].get("revalidation_after"),
                    "last_file": saved.get("last_file"),
                    "target_seen": saved_binding.get("target_seen"),
                    "foreign_seen": saved_binding.get("foreign_seen"),
                    "binding_complete": saved_binding.get("binding_complete"),
                    "samples": len(result["samples"])})
                if turn == 0:
                    changed_decision = bundle / "admission-0003-00.json"
                    replacement = json.loads(changed_decision.read_text())
                    replacement["admitted"] = True
                    changed_decision.write_text(json.dumps(replacement))
                if (result["coverage"] == "observed"
                        and saved_binding.get("target_seen") is True
                        and len(samples) == 4):
                    break

        self.assertEqual(result["coverage"], "observed",
            f"warnings={result['warnings']} continuation={trace!r}")
        self.assertEqual(len(trace), 2,
            f"unexpected bounded poll count: {trace!r}")
        self.assertEqual([item["coverage"] for item in trace],
                         ["partial", "observed"])
        self.assertEqual([item["samples"] for item in trace], [0, 4])
        self.assertFalse(trace[0]["target_seen"])
        final_binding = cursor["scan"][root_key]["bundle_cursors"][bundle.name][
            "binding_state"]
        self.assertTrue(final_binding["target_seen"])
        self.assertFalse(final_binding["foreign_seen"])
        self.assertTrue(final_binding["binding_complete"])
        self.assertEqual(len(samples), 4,
            f"warnings={result['warnings']} continuation={trace!r}")
        self.assertEqual({sample["state"] for sample in samples.values()},
                         {"admitted", "denied"})
        self.assertEqual(previous["admission_counts"],
                         {"admitted": 3, "denied": 1, "unknown": 0})
        self.assertTrue(trace[0]["revalidation_after"])
        self.assertEqual(trace[0]["revalidation_after"], "admission-0002-00.json")
        self.assertTrue(trace[1]["target_seen"])
        self.assertFalse(trace[1]["foreign_seen"])
        self.assertTrue(trace[1]["binding_complete"])
        self.assertIsNone(trace[1]["revalidation_after"])
        listing_row = next(item for item in cursor["scan"][root_key][
            "bundle_cursors"][bundle.name]["listing"]["files"]
            if item["name"] == "admission-0003-00.json")
        self.assertEqual(listing_row["token"], metrics._admission_stat_token(
            (bundle / "admission-0003-00.json").lstat()))

    def test_resumed_listing_revalidates_prefix_before_current_admission(self):
        root = self.root / "slow-cached-prefix-mutation"
        bundle = root / "selected-bundle"
        bundle.mkdir(parents=True)
        registry = root / "resource-runs.json"
        run_id = "fixture-run-0068__a8"
        registry.write_text(json.dumps({"runs": {run_id: {
            "run_id": run_id, "started_at": 1}}}) + "\n")
        for index in range(4):
            (bundle / f"admission-{index:04d}.json").write_text(json.dumps({
                "admitted": index in {1, 2},
                "snapshot": {"t": 10 + index},
                "shared_admission": {"authorized_run_id": "other-smoke-run"},
            }))
        (bundle / "entry.json").write_text(json.dumps({
            "run_id": run_id, "selection_sha256": "a" * 64,
            "selection_replay_sha256": "b" * 64}))

        cursor = metrics.read_admission_sources([root], run_id=run_id,
            row_key="run", registry_path=registry, started_at=1, now=200)["cursor"]
        previous = {}
        clock = [0.0]
        original_lstat = Path.lstat

        def slow_cached_lstat(path):
            result = original_lstat(path)
            if (sys._getframe(1).f_code.co_name == "revalidate_listing_entries"
                    and path.parent == bundle):
                clock[0] += 0.1
            return result

        trace = []
        results = []
        with mock.patch.object(Path, "lstat", slow_cached_lstat):
            for turn in range(6):
                clock[0] = 0.0
                result = metrics.read_admission_sources([root], run_id=run_id,
                    row_key="run", registry_path=registry, started_at=1,
                    now=201 + turn, cursor=cursor, monotonic=lambda: clock[0],
                    time_budget=0.25)
                summary, state = progress._admission_summary(previous, result,
                    201 + turn, run_id, "run", 1)
                previous = {**previous, **state}
                results.append(result)
                trace.append({"turn": turn, "coverage": result["coverage"],
                    "latest": result["latest"], "current": summary["state"],
                    "freshness_seconds": summary["freshness_seconds"],
                    "revalidation_after": state["admission_cursor"]["scan"][
                        str(root)]["bundle_cursors"][bundle.name]["listing"].get(
                            "revalidation_after")})
                # Re-adopt exactly as resource_progress does at each poll boundary.
                cursor = progress._restore_special_numbers(json.loads(json.dumps(
                    progress._json_safe(state["admission_cursor"]))))
                if turn == 0:
                    self.assertEqual(result["coverage"], "partial")
                    listing = cursor["scan"][str(root)]["bundle_cursors"][
                        bundle.name]["listing"]
                    self.assertEqual(listing.get("revalidation_after"),
                                     "admission-0002.json")
                    directory_token = metrics._admission_stat_token(bundle.lstat())
                    prefix = bundle / "admission-0000.json"
                    replacement = json.loads(prefix.read_text())
                    replacement["admitted"] = True
                    replacement["snapshot"]["t"] = 99
                    prefix.write_text(json.dumps(replacement))
                    self.assertEqual(metrics._admission_stat_token(bundle.lstat()),
                                     directory_token)

                latest = result.get("latest")
                if (result["coverage"] == "observed"
                        and isinstance(latest, dict)
                        and latest.get("state") == "admitted"
                        and latest.get("at") == 99):
                    break

        resumed = results[1]
        self.assertEqual(len(results), 2,
            f"continuation did not retain bounded progress: {trace!r}")
        resumed_summary, _ = progress._admission_summary({}, resumed,
            202, run_id, "run", 1)
        self.assertFalse(resumed["coverage"] == "observed"
            and resumed.get("latest") is not None
            and resumed["latest"].get("state") == "denied"
            and resumed["latest"].get("at") == 13
            and resumed_summary["state"] == "waiting"
            and resumed_summary["freshness_seconds"] == 202 - 13,
            f"stale prefix decision escaped continuation: {trace!r}")
        self.assertNotEqual((resumed_summary["state"],
                             resumed_summary["freshness_seconds"]),
                            ("waiting", 202 - 13),
            f"stale current freshness escaped continuation: {trace!r}")
        self.assertEqual(results[-1]["coverage"], "observed",
            f"bounded prefix continuation did not finish: {trace!r}")
        self.assertEqual(resumed["coverage"], "observed")
        self.assertEqual(results[-1]["latest"]["state"], "admitted",
            f"current prefix decision was not selected: {trace!r}")
        self.assertEqual(results[-1]["latest"]["at"], 99,
            f"prefix rewrite was not adopted: {trace!r}")
        self.assertEqual(previous["admission_current_state"], "admitted")
        self.assertEqual(resumed_summary["state"], "admitted")
        self.assertEqual(resumed_summary["freshness_seconds"], 202 - 99)
        final_listing = cursor["scan"][str(root)]["bundle_cursors"][
            bundle.name]["listing"]
        prefix_entry = next(item for item in final_listing["files"]
                            if item["name"] == "admission-0000.json")
        self.assertEqual(prefix_entry["token"], metrics._admission_stat_token(
            (bundle / "admission-0000.json").lstat()))
        emitted_ids = [sample["sample_id"] for result in results
                       for sample in result["samples"]]
        self.assertEqual(len(emitted_ids), len(set(emitted_ids)),
            f"continued prefix sample was emitted more than once: {trace!r}")

    def test_root_listing_prefix_invalidates_cached_latest_and_recovery(self):
        root = self.root / "flat-root-cached-prefix-mutation"
        root.mkdir()
        registry = root / "resource-runs.json"
        run_id = "fixture-run-0068__a8"
        registry.write_text(json.dumps({"runs": {run_id: {
            "run_id": run_id, "row_key": "run", "started_at": 1}}}) + "\n")
        (root / "entry.json").write_text(json.dumps({
            "run_id": run_id, "selection_sha256": "a" * 64,
            "selection_replay_sha256": "b" * 64}))
        (root / "command-01.json").write_text(json.dumps({"argv": [
            "runner", "--run-id", run_id, "--raw-results", str(root)]}))
        for index, stamp in enumerate((110, 80, 70, 60, 50)):
            (root / f"admission-{index:04d}.json").write_text(json.dumps({
                "admitted": index == 0,
                "snapshot": {"t": stamp},
                "shared_admission": {"authorized_run_id": "other-smoke-run"},
            }))

        seed = metrics.read_admission_sources([root], run_id=run_id,
            row_key="run", registry_path=registry, started_at=1, now=200)
        self.assertEqual(seed["coverage"], "observed")
        self.assertEqual(seed["latest"]["at"], 110)
        self.assertEqual(seed["latest"]["source_file"], "admission-0000.json")
        _previous_summary, previous = progress._admission_summary({}, seed,
            200, run_id, "run", 1)
        cursor = progress._restore_special_numbers(json.loads(json.dumps(
            progress._json_safe(previous["admission_cursor"]))))
        root_state = cursor["scan"][str(root)]
        root_cursor = root_state["bundle_cursors"]["@root"]
        binding = root_cursor["binding_state"]
        self.assertTrue(binding["target_seen"])
        self.assertFalse(binding["foreign_seen"])

        # Retain a plausible old latest and a generation-recovery candidate
        # for the exact same flat-root source, as a normal continued cursor can.
        stale = copy.deepcopy(seed["latest"])
        binding["latest"] = copy.deepcopy(stale)
        binding["recovery_latest"] = copy.deepcopy(stale)
        cursor["generation_revalidation_required"] = True
        cursor["generation_baseline_at"] = 100
        cursor["generation_baseline_ids"] = ["f" * 16]
        cursor["generation_baseline_overflow"] = False
        cursor["generation_baseline_ready"] = True
        cursor["historical_latest_at"] = 100
        cursor["historical_latest_ids"] = ["f" * 16]
        cursor["historical_latest_overflow"] = False
        cursor["latest_at"] = 100
        cursor["source_latest"] = {}
        # Re-adopt the exact JSON-safe normalizer representation before the
        # saved listing marker is produced by a bounded ordinary reader poll.
        cursor = progress._restore_special_numbers(json.loads(json.dumps(
            progress._json_safe(cursor))))

        clock = [0.0]
        original_lstat = Path.lstat
        trace = []
        results = []
        directory_token = metrics._admission_stat_token(root.lstat())

        def slow_root_cached_lstat(path):
            result = original_lstat(path)
            if (sys._getframe(1).f_code.co_name == "revalidate_listing_entries"
                    and path.parent == root):
                clock[0] += 0.1
            return result

        with mock.patch.object(Path, "lstat", slow_root_cached_lstat):
            for turn in range(8):
                clock[0] = 0.0
                result = metrics.read_admission_sources([root], run_id=run_id,
                    row_key="run", registry_path=registry, started_at=1,
                    now=201 + turn, cursor=cursor, monotonic=lambda: clock[0],
                    time_budget=0.25)
                summary, state = progress._admission_summary(previous, result,
                    201 + turn, run_id, "run", 1)
                previous = {**previous, **state}
                results.append(result)
                cursor = progress._restore_special_numbers(json.loads(json.dumps(
                    progress._json_safe(state["admission_cursor"]))))
                saved_root = cursor["scan"][str(root)]
                saved_listing = saved_root["root_listing"]
                saved_binding = saved_root["bundle_cursors"]["@root"][
                    "binding_state"]
                trace.append({"turn": turn, "coverage": result["coverage"],
                    "latest": result.get("latest"), "current": summary["state"],
                    "freshness_seconds": summary["freshness_seconds"],
                    "revalidation_after": saved_listing.get("revalidation_after"),
                    "latest_cache": saved_binding.get("latest"),
                    "recovery_latest": saved_binding.get("recovery_latest")})
                if turn == 0:
                    self.assertEqual(result["coverage"], "partial")
                    marker = saved_listing.get("revalidation_after")
                    self.assertEqual(marker, "admission-0002.json")
                    self.assertLessEqual("admission-0000.json", marker)
                    source = root / "admission-0000.json"
                    replacement = json.loads(source.read_text())
                    replacement["snapshot"]["t"] = 90
                    source.write_text(json.dumps(replacement))
                    self.assertEqual(metrics._admission_stat_token(root.lstat()),
                                     directory_token)
                if result["coverage"] == "observed":
                    break

        self.assertEqual(metrics._admission_stat_token(root.lstat()), directory_token)
        self.assertEqual(results[-1]["coverage"], "observed",
            f"flat-root bounded continuation did not finish: {trace!r}")
        final = results[-1]
        summary, _state = progress._admission_summary({}, final,
            201 + len(results) - 1, run_id, "run", 1)
        self.assertEqual(summary["state"], "unknown",
            f"flat-root source regression did not stay unknown: {trace!r}")
        self.assertEqual(final["latest"]["state"], "unknown")
        self.assertEqual(final["latest"]["at"], 90)
        self.assertEqual(summary["freshness_seconds"],
            201 + len(results) - 1 - 90,
            f"flat-root stale evidence supplied freshness: {trace!r}")
        self.assertFalse(final.get("latest", {}).get("at") == 110
            and final["latest"].get("state") in {"admitted", "denied"},
            f"stale flat-root latest authorized current state: {trace!r}")
        self.assertFalse(summary["state"] in {"admitted", "denied"}
            and summary["freshness_seconds"] == 201 + len(results) - 1 - 110,
            f"stale flat-root latest authorized freshness: {trace!r}")
        final_binding = cursor["scan"][str(root)]["bundle_cursors"]["@root"][
            "binding_state"]
        self.assertTrue(final_binding["target_seen"])
        self.assertFalse(final_binding["foreign_seen"])
        recovery_latest = final_binding.get("recovery_latest")
        self.assertFalse(isinstance(recovery_latest, dict)
                         and recovery_latest.get("at") == 110,
            f"stale flat-root recovery evidence survived: {trace!r}")
        self.assertTrue(cursor["generation_revalidation_required"],
            f"stale recovery evidence completed generation validation: {trace!r}")
        emitted_ids = [sample["sample_id"] for item in results
                       for sample in item["samples"]]
        self.assertEqual(len(emitted_ids), len(set(emitted_ids)),
            f"flat-root continuation duplicated samples: {trace!r}")

    def test_evicted_listing_cursor_resumes_exact_binding_without_restarting(self):
        root = self.root / "installed-cursor-without-bundle-cache"
        bundle = root / "r2-launch-a8"
        bundle.mkdir(parents=True)
        registry = root / "resource-runs.json"
        run_id = "fixture-run-0068__a8"
        registry.write_text(json.dumps({"runs": {run_id: {
            "run_id": run_id, "row_key": "run", "started_at": 1,
            "log": str(self.root / "eval-run-r2-a8.log")}}}) + "\n")
        (self.root / "eval-run-r2-a8.log").write_text("fixture log identity\n")

        admitted = {"admitted": True, "shared_admission": {
            "authorized_run_id": "fixture-run-0929"},
            "snapshot": {"t": 100}}
        denied = {"admitted": False, "shared_admission": {
            "authorized_run_id": "fixture-run-0929"},
            "preceding_evaluation_leases": [{"run_id": "preceding-run",
                "task": "evaluation"}], "snapshot": {"t": 101},
            "reasons": ["other evaluation has priority"]}
        (bundle / "admission-0000-00.json").write_text(json.dumps(admitted))
        (bundle / "admission-0000-01.json").write_text(json.dumps(denied))
        expected = {"admitted": 1, "denied": 1}
        for index in range(8):
            value = {"admitted": index % 2 == 0,
                "shared_admission": {"authorized_run_id":
                    "fixture-run-0929"},
                "snapshot": {"t": 200 + index}, "padding": "x" * 1200}
            (bundle / f"admission-005{index + 1}-00.json").write_text(
                json.dumps(value))
            expected["admitted" if value["admitted"] else "denied"] += 1
        (bundle / "command-01.json").write_text(json.dumps({"argv": [
            "runner", "--run-id", run_id, "--raw-results", str(root)]}))
        (bundle / "entry.json").write_text(json.dumps({"run_id": run_id,
            "selection_sha256": "a" * 64,
            "selection_replay_sha256": "b" * 64}))

        root_info, bundle_info = root.lstat(), bundle.lstat()
        root_key = str(root)
        bundle_identity = f"{bundle_info.st_dev}:{bundle_info.st_ino}"
        binding = {"bundle": bundle.name, "identity": bundle_identity,
            "target_seen": False, "foreign_seen": False, "generation_fields": [],
            "source_files": {}, "binding_sources": {}, "phase": "binding",
            "page_decisions": [], "latest": None, "recovery_latest": None,
            "decisions_seen": True, "binding_complete": False,
            "partial": False, "overflow": False}
        cursor = {"schema_version": 5,
            "root_generations": {root_key: f"{root_info.st_dev}:{root_info.st_ino}"},
            "scan": {root_key: {"last_bundle": "effective-bundle",
                "last_file": "condition-registry.json", "last_root_entry": "vstar.json",
                "bundle_cursors": {bundle.name: {
                    "last_file": "admission-0050-01.json",
                    "identity": bundle_identity, "binding_state": binding}}}},
            "sample_ids": [], "uncertainty_reason": "source-invalid"}

        samples = {}
        trace = []
        first_page = None
        cache_limit_seen = False
        result = None
        with mock.patch.object(metrics, "MAX_ADMISSION_CACHED_ENTRIES", 2), \
                mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 5000):
            for turn in range(32):
                result = metrics.read_admission_sources([root], run_id=run_id,
                    row_key="run", registry_path=registry, started_at=1,
                    now=1000 + turn, cursor=cursor)
                cursor = result["cursor"]
                saved = cursor["scan"][root_key]["bundle_cursors"][bundle.name]
                saved_binding = saved["binding_state"]
                trace.append((turn, result["coverage"], saved.get("last_file"),
                    saved_binding.get("phase"), saved_binding.get("binding_complete"),
                    saved_binding.get("target_seen"), len(result["samples"])))
                if turn == 0:
                    first_page = (saved.get("last_file"), saved.get("scan_prefix"))
                cache_limit_seen = (cache_limit_seen or
                    "admission-listing-cache-limit" in result["warnings"])
                samples.update({sample["sample_id"]: sample
                                for sample in result["samples"]})
                if result["coverage"] == "observed":
                    break

        self.assertIsNotNone(result)
        self.assertTrue(first_page[0].startswith("admission-"))
        self.assertNotEqual(first_page[0], "admission-0050-01.json")
        self.assertTrue(first_page[1])
        self.assertTrue(cache_limit_seen)
        self.assertEqual(result["coverage"], "observed",
            f"coverage={result['coverage']} warnings={result['warnings']} "
            f"trace={trace!r} "
            f"binding={cursor['scan'][root_key]['bundle_cursors'][bundle.name]['binding_state']!r}")
        self.assertEqual(len(samples), sum(expected.values()))
        self.assertEqual({sample["state"] for sample in samples.values()},
                         {"admitted", "denied"})
        final_binding = cursor["scan"][root_key]["bundle_cursors"][bundle.name][
            "binding_state"]
        self.assertTrue(final_binding["target_seen"])
        self.assertFalse(final_binding["foreign_seen"])
        self.assertTrue(final_binding["binding_complete"])
        self.assertNotIn("admission-run-binding-ambiguous", result["warnings"])

    def test_shared_admission_lease_of_the_authorizing_run_is_not_a_foreign_identity(self):
        # Synthetic shared-admission shape: each sample's shared_admission
        # names the smoke run whose GPU lease it shares, with that run's pid and
        # start time. That reference must not make the bundle ambiguous.
        root = self.root / "raw-results"
        bundle = root / "r2-launch-a7"
        bundle.mkdir(parents=True)
        run_id = "fixture-run-0068__a7"
        smoke = "fixture-run-0929"
        lease = {"gpus": ["0"], "host": "gpu-host-a", "pid": 2087782,
                 "pid_namespace": "pid:[4026531836]", "run_id": smoke, "share": True,
                 "started_at": 1792840758.0865579, "starttime": "121090704",
                 "state": "S", "task": smoke, "token": "t" * 32}
        expected = {"admitted": 0, "denied": 0}
        for index in range(6):
            admitted = index % 3 == 0
            expected["admitted" if admitted else "denied"] += 1
            (bundle / f"admission-{index:04d}-00.json").write_text(json.dumps({
                "admitted": admitted, "reasons": [] if admitted else
                    ["other GPU0 evaluation reservation has priority; no preemption"],
                "snapshot": {"t": 100 + index, "processes": [{"pid": 2087889}]},
                "preceding_evaluation_leases": [{"run_id": "other-eval", "pid": 7,
                    "started_at": 50, "task": "evaluation"}],
                "shared_admission": {"authorized_run_id": smoke, "leases": [lease],
                    "matched_process_pids": [2087889],
                    "numeric_thresholds_unchanged": True, "original_reasons": []}}))
        (bundle / "entry.json").write_text(json.dumps({"run_id": run_id, "share": True}))

        result = metrics.read_admission_sources([root], run_id=run_id, started_at=1, now=500)
        self.assertNotIn("admission-run-binding-ambiguous", result["warnings"])
        self.assertEqual(result["coverage"], "observed", result["warnings"])
        states = [sample["state"] for sample in result["samples"]]
        self.assertEqual({"admitted": states.count("admitted"),
                          "denied": states.count("denied")}, expected)
        self.assertNotIn("unknown", states)
        # A bundle that names a different run as its own identity stays ambiguous.
        (bundle / "other-entry.json").write_text(json.dumps({"run_id": smoke, "pid": 2087782}))
        mixed = metrics.read_admission_sources([root], run_id=run_id, started_at=1, now=600)
        self.assertIn("admission-run-binding-ambiguous", mixed["warnings"])

    def test_authorizing_run_named_only_in_shared_admission_is_not_bound_to_the_bundle(self):
        # Before the fix the smoke run's lease inside shared_admission was read as a
        # run identity, so an observer of the smoke run took another evaluation's
        # admission samples (synthetic raw-results/admission bundle) as its own.
        root = self.root / "raw-results"
        bundle = root / "admission"
        bundle.mkdir(parents=True)
        smoke = "fixture-run-0929"
        lease = {"host": "gpu-host-a", "pid": 2087782, "run_id": smoke,
                 "started_at": 1792840758.0865579, "starttime": "121090704",
                 "state": "S", "task": smoke}
        for index in range(4):
            (bundle / f"asr__dev-root__attempt-{index:02d}.json").write_text(json.dumps({
                "admitted": index % 2 == 0, "snapshot": {"t": 100 + index},
                "shared_admission": {"authorized_run_id": smoke, "leases": [lease]}}))

        cursor = None
        for now in (500, 560, 620):
            result = metrics.read_admission_sources([root], run_id=smoke, started_at=1,
                                                    now=now, cursor=cursor)
            cursor = result["cursor"]
            self.assertEqual([sample for sample in result["samples"]
                              if sample["state"] in {"admitted", "denied"}], [])
            self.assertNotEqual(result["coverage"], "observed")

    def test_admission_reader_observes_320_separate_decisions_across_bounded_pages(self):
        root = self.root / "320-separate-admission-files"
        bundle = root / "one-bundle"
        bundle.mkdir(parents=True)
        (bundle / "run-record.json").write_text(json.dumps({
            "run_id": "run-320", "started_at": 1, "row_id": "row-320"}))
        for index in range(320):
            admitted = index < 9
            (bundle / f"admission-{index:03d}.json").write_text(json.dumps({
                "row_id": "row-320",
                "admitted": admitted,
                "snapshot": {"t": 100 + index},
                "reasons": ([] if admitted else ["evaluation GPU capacity is occupied"]),
                "preceding_evaluation_leases": ([] if admitted else [{
                    "task": "evaluation", "host": "gpu-node-a", "gpu": 0,
                    "owner": "evaluation-queue-17"}]),
            }))

        input_bytes = sum(path.stat().st_size for path in bundle.iterdir())
        self.assertLess(input_bytes, metrics.MAX_ADMISSION_PENDING_BYTES)

        def assert_complete_observation(result):
            self.assertEqual(result["coverage"], "observed")
            self.assertNotIn("admission-binding-summary-limit", result["warnings"])
            self.assertEqual(len(result["samples"]), 320)
            denied = [sample for sample in result["samples"]
                      if sample["state"] == "denied"]
            admitted = [sample for sample in result["samples"]
                        if sample["state"] == "admitted"]
            self.assertEqual((len(denied), len(admitted)), (311, 9))
            self.assertEqual(len(denied) / len(result["samples"]), 311 / 320)
            latest = result["latest"]
            self.assertEqual((latest["state"], latest["at"]), ("denied", 419.0))
            self.assertEqual(latest["reasons"], ["evaluation GPU capacity is occupied"])
            self.assertEqual(latest["priority_blockers"], [{
                "task": "evaluation", "host": "gpu-node-a", "gpu": 0,
                "owner": "evaluation-queue-17"}])
            for key in ("progress", "rate", "eta", "eta_seconds",
                        "wait_duration_seconds", "wait_seconds", "time_ratio"):
                self.assertNotIn(key, result)
            for sample in result["samples"]:
                for key in ("progress", "rate", "eta", "eta_seconds",
                            "wait_duration_seconds", "wait_seconds", "time_ratio"):
                    self.assertNotIn(key, sample)

        default = metrics.read_admission_sources([root], run_id="run-320",
            row_key="row-320", started_at=1, now=500)
        self.assertEqual(default["bytes"], input_bytes)
        self.assertLessEqual(default["bytes"], metrics.MAX_ADMISSION_BYTES_PER_PASS)
        assert_complete_observation(default)

        cursor = None
        paged = None
        accumulated = {}
        saw_partial_page = False
        page_byte_limit = 12 * 1024
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", page_byte_limit):
            for _ in range(32):
                paged = metrics.read_admission_sources([root], run_id="run-320",
                    row_key="row-320", started_at=1, now=500, cursor=cursor)
                self.assertLessEqual(paged["bytes"], page_byte_limit)
                accumulated.update({row["sample_id"]: row for row in paged["samples"]})
                scan_state = paged["cursor"]["scan"].get(str(root), {})
                binding_state = scan_state.get("bundle_cursors", {}).get(
                    "one-bundle", {}).get("binding_state")
                if binding_state is not None:
                    summary_bytes = len(json.dumps(binding_state, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
                    self.assertLessEqual(summary_bytes, metrics.MAX_ADMISSION_PENDING_BYTES)
                    self.assertLessEqual(len(binding_state["source_files"]),
                                         metrics.MAX_ADMISSION_SOURCE_FINGERPRINTS)
                    self.assertNotIn("decisions", binding_state)
                if paged["coverage"] == "observed":
                    break
                self.assertEqual(paged["coverage"], "partial")
                saw_partial_page = True
                cursor = paged["cursor"]
        self.assertTrue(saw_partial_page)
        paged_complete = {**paged, "samples": list(accumulated.values())}
        assert_complete_observation(paged_complete)

    def test_multi_sibling_listing_keeps_identity_last_target_cache_and_all_samples(self):
        root = self.root / "sibling-listing-cost"
        target = root / "00-selected-context"
        target.mkdir(parents=True)
        registry = root / "registry.data"
        registry.write_text("{}\n")
        for index in range(270):
            (target / f"decision-{index:03d}.data").write_text(json.dumps({
                "admitted": index >= 7, "sample_index": index,
                "snapshot": {"t": 1000 + index}}))
        (target / "zz-plain-run-declaration.data").write_text(
            json.dumps({"run_id": "selected-run", "started_at": 1}))
        for sibling_index in range(8):
            sibling = root / f"10-sibling-{sibling_index:02d}"
            sibling.mkdir()
            for item_index in range(24):
                (sibling / f"decision-{item_index:02d}.data").write_text(json.dumps({
                    "admitted": False, "sample_index": item_index,
                    "snapshot": {"t": 10 + item_index}}))

        clock = [0.0]
        listing_calls = {}
        original_scandir = os.scandir

        class TimedScandir:
            def __init__(self, entries, path):
                self.entries = entries
                self.path = str(path)
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.index >= len(self.entries):
                    raise StopIteration
                item = self.entries[self.index]
                self.index += 1
                clock[0] += 0.0007
                return item

        def timed_scandir(path):
            listing_calls[str(path)] = listing_calls.get(str(path), 0) + 1
            return TimedScandir(list(original_scandir(path)), path)

        cursor = None
        accumulated = {}
        page_count = 0
        with mock.patch.object(metrics.os, "scandir", timed_scandir), \
                mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 8 * 1024), \
                mock.patch.object(metrics, "MAX_ADMISSION_CACHED_ENTRIES", 350):
            for page_count in range(1, 65):
                before = clock[0]
                result = metrics.read_admission_sources([root], run_id="selected-run",
                    registry_path=registry, started_at=1, now=2000, cursor=cursor,
                    monotonic=lambda: clock[0], time_budget=0.25)
                self.assertLessEqual(clock[0] - before, 0.2507)
                self.assertLessEqual(result["bytes"], 8 * 1024)
                self.assertLessEqual(result["entries"], metrics.MAX_ADMISSION_ENTRIES)
                accumulated.update({row["sample_id"]: row for row in result["samples"]})
                cursor = result["cursor"]
                if page_count >= 2:
                    self.assertEqual(listing_calls[str(target)], 1,
                        "a sibling turn must not discard the target's listing")
                if result["coverage"] == "observed":
                    break

        self.assertLessEqual(page_count, 64)
        self.assertEqual(len(accumulated), 270)
        self.assertEqual(sum(row["state"] == "denied" for row in accumulated.values()), 7)
        self.assertEqual(sum(row["state"] == "admitted" for row in accumulated.values()), 263)
        self.assertGreater(sum(listing_calls.get(str(root / f"10-sibling-{i:02d}"), 0)
                               for i in range(8)), 0)

    def test_listing_cache_entry_and_byte_limits_apply_across_bundle_cursors(self):
        root = self.root / "bounded-cross-bundle-cache"
        for bundle_index in range(2):
            bundle = root / f"context-{bundle_index}"
            bundle.mkdir(parents=True)
            for item_index in range(4):
                (bundle / f"sample-{item_index}.data").write_text(json.dumps({
                    "admitted": bool(item_index % 2), "snapshot": {"t": 10 + item_index}}))

        with mock.patch.object(metrics, "MAX_ADMISSION_CACHED_ENTRIES", 6), \
                mock.patch.object(metrics, "MAX_ADMISSION_PENDING_BYTES", 4096):
            result = metrics.read_admission_sources([root], run_id="unrelated-run", now=50)
        scan = result["cursor"]["scan"][str(root)]
        root_listing = scan.get("root_listing", {})
        cached_entries = len(root_listing.get("entries", []))
        cached_bytes = len(json.dumps(root_listing, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()) if root_listing else 0
        bundle_cursors = scan.get("bundle_cursors", {})
        for progress_state in bundle_cursors.values():
            listing = progress_state.get("listing") if isinstance(progress_state, dict) else None
            if isinstance(listing, dict):
                cached_entries += len(listing.get("files", []))
                cached_bytes += len(json.dumps(listing, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False).encode())
            binding = progress_state.get("binding_state", {}) \
                if isinstance(progress_state, dict) else {}
            self.assertLessEqual(len(json.dumps(binding, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()), 4096)
        self.assertLessEqual(cached_entries, 6)
        self.assertLessEqual(cached_bytes, 4096)
        self.assertLessEqual(len(bundle_cursors), metrics.MAX_ADMISSION_ENTRIES)

    def test_ambiguous_population_resolves_once_across_exact_source_pages(self):
        root = self.root / "paged-ambiguity-resolution"
        for bundle_name, first_stamp in (("candidate-a", 100), ("candidate-b", 200)):
            bundle = root / bundle_name
            bundle.mkdir(parents=True)
            (bundle / "run-context.data").write_text(
                json.dumps({"run_id": "ambiguous-run", "started_at": 1}))
            for index in range(8):
                (bundle / f"sample-{index:02d}.data").write_text(json.dumps({
                    "admitted": index % 3 != 0, "sample_index": index,
                    "snapshot": {"t": first_stamp + index}}))

        ambiguous = metrics.read_admission_sources([root], run_id="ambiguous-run",
            started_at=1, now=500)
        self.assertEqual(ambiguous["coverage"], "unknown")
        self.assertEqual(len(ambiguous["samples"]), 16)
        self.assertEqual({row["state"] for row in ambiguous["samples"]}, {"unknown"})
        replay = metrics.read_admission_sources([root], run_id="ambiguous-run",
            started_at=1, now=501, cursor=ambiguous["cursor"])
        self.assertEqual(replay["samples"], [])

        removed = root / "candidate-b"
        for path in removed.iterdir():
            path.unlink()
        removed.rmdir()
        previous = {"admission_counts": {"admitted": 0, "denied": 0, "unknown": 16},
            "admission_generation": ambiguous["generation"],
            "admission_cursor": ambiguous["cursor"]}
        cursor = ambiguous["cursor"]
        pages, seen = [], {}
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 384):
            for index in range(32):
                page = metrics.read_admission_sources([root], run_id="ambiguous-run",
                    started_at=1, now=510 + index, cursor=cursor)
                summary, update = __import__("resource_progress")._admission_summary(
                    previous, page, 510 + index, "ambiguous-run", "run", 1)
                previous.update(update)
                cursor = page["cursor"]
                pages.append((page, summary))
                seen.update({row["sample_id"]: row for row in page["samples"]})
                if page["coverage"] == "observed":
                    break
        self.assertEqual(len(seen), 8)
        self.assertEqual(pages[0][1]["unknown_samples"], 0)
        exact_generation = pages[0][0]["generation"]
        self.assertNotEqual(exact_generation, ambiguous["generation"])
        counts = [page[1]["sample_count"] for page in pages]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], 8)
        self.assertTrue(all(page[0]["generation"] == exact_generation for page in pages))
        reread = metrics.read_admission_sources([root], run_id="ambiguous-run",
            started_at=1, now=550, cursor=cursor)
        stable, stable_state = __import__("resource_progress")._admission_summary(
            previous, reread, 550, "ambiguous-run", "run", 1)
        self.assertEqual(reread["samples"], [])
        self.assertEqual(stable["sample_count"], 8)
        self.assertEqual(stable_state["admission_counts"], previous["admission_counts"])

    def test_delivery_envelope_mirror_is_not_a_new_admission_and_nested_samples_survive(self):
        root = self.root / "delivery-envelope"
        bundle = root / "arbitrary-context"
        bundle.mkdir(parents=True)
        (bundle / "context.data").write_text(json.dumps({"run_id": "receipt-run"}))
        source = {"admitted": False, "snapshot": {"t": 30}, "sample_key": "origin"}
        (bundle / "observation.data").write_text(json.dumps(source))
        (bundle / "receipt-copy.data").write_text(json.dumps({
            "delivered": True, "error": None, "evidence": source,
            "message": "delivery receipt", "run_id": "receipt-run",
            "admitted": False, "snapshot": {"t": 30}}))
        (bundle / "nested-observation.data").write_text(json.dumps({
            "payload": {"admission": {"state": "denied", "snapshot": {"t": 31},
                                       "sample_key": "nested-origin"}}}))

        result = metrics.read_admission_sources([root], run_id="receipt-run",
            started_at=1, now=40)
        self.assertEqual(len(result["samples"]), 2)
        self.assertEqual({row["at"] for row in result["samples"]}, {30.0, 31.0})
        self.assertEqual({row["state"] for row in result["samples"]}, {"denied"})

    def test_equal_time_independent_and_row_scoped_samples_remain_distinct(self):
        root = self.root / "equal-time-row-samples"
        bundle = root / "context"
        bundle.mkdir(parents=True)
        (bundle / "context.data").write_text(json.dumps({"run_id": "row-run"}))
        for row_id, token in (("row-a", "alpha"), ("row-a", "beta"), ("row-b", "gamma")):
            (bundle / f"sample-{token}.data").write_text(json.dumps({
                "row_id": row_id, "admitted": True, "snapshot": {"t": 70},
                "independent_sample": token}))

        run = metrics.read_admission_sources([root], run_id="row-run",
            started_at=1, now=80)
        row_a = metrics.read_admission_sources([root], run_id="row-run", row_key="row-a",
            started_at=1, now=80)
        row_b = metrics.read_admission_sources([root], run_id="row-run", row_key="row-b",
            started_at=1, now=80)
        self.assertEqual(len(run["samples"]), 3)
        self.assertEqual(len({row["sample_id"] for row in run["samples"]}), 3)
        self.assertEqual({row["at"] for row in run["samples"]}, {70.0})
        self.assertEqual(len(row_a["samples"]), 2)
        self.assertEqual(len(row_b["samples"]), 1)
        self.assertTrue(all(row["state"] == "admitted"
                            for row in row_a["samples"] + row_b["samples"]))

    def test_rootless_target_survives_poisoned_cursor_large_changing_sibling(self):
        root = self.root / "registry-parent-without-output-root"
        registry = root / "resource-runs.json"
        target = root / "arbitrary-target-child"
        sibling = root / "old-unbound-child"
        target.mkdir(parents=True)
        sibling.mkdir()
        registry.write_text("{}\n")
        for index in range(320):
            (sibling / f"decision-{index:03d}.json").write_text(json.dumps({
                "admitted": False, "snapshot": {"t": index + 1}}))
        (sibling / "notes.md").write_text("[not an admission document]\n")
        (sibling / "unrelated.json").write_text(
            '{"padding":"' + "x" * (metrics.MAX_ADMISSION_RECORD_BYTES + 64) + '"}')
        for index in range(320):
            (target / f"decision-{index:03d}.json").write_text(json.dumps({
                "admitted": index >= 9,
                "snapshot": {"t": 1000 + index}}))
        (target / "zz-arbitrary-run-record.data").write_text(
            json.dumps({"run_id": "exact-run"}))
        root_info = root.lstat()
        old_cursor = {"schema_version": 3,
            "root_generations": {str(root): f"{root_info.st_dev}:{root_info.st_ino}"},
            "scan": {str(root): {"root_blocked_generation":
                metrics._admission_stat_token(root_info), "last_bundle": sibling.name,
                "last_file": "decision-255.json", "blocked_bundles": {
                    target.name: "poisoned-generation"}}}, "sample_ids": []}
        cursor = old_cursor
        accumulated = {}
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 12 * 1024):
            for turn in range(64):
                changing = sibling / "decision-000.json"
                changing.write_text(json.dumps({"admitted": False,
                    "snapshot": {"t": 1500 + turn}}))
                result = metrics.read_admission_sources([root], run_id="exact-run",
                    registry_path=registry, started_at=1, now=2000, cursor=cursor)
                self.assertLessEqual(result["bytes"], 12 * 1024)
                self.assertLessEqual(result["entries"], metrics.MAX_ADMISSION_ENTRIES)
                cursor = result["cursor"]
                accumulated.update({row["sample_id"]: row for row in result["samples"]})
                if result["coverage"] == "observed":
                    break
        self.assertEqual(len(accumulated), 320)
        self.assertEqual(sum(row["state"] == "denied" for row in accumulated.values()), 9)
        self.assertEqual(sum(row["state"] == "admitted" for row in accumulated.values()), 311)
        self.assertEqual({row["state"] for row in accumulated.values()}, {"denied", "admitted"})
        self.assertEqual((result["latest"]["state"], result["latest"]["at"]),
                         ("admitted", 1319.0))
        self.assertNotIn("admission-binding-summary-limit", result["warnings"])
        self.assertNotIn("admission-run-binding-missing", result["warnings"])
        self.assertNotIn("root_blocked_generation", cursor["scan"][str(root)])

    def test_rootless_direct_run_declaration_without_optional_keys_has_no_row_authority(self):
        root = self.root / "registry-parent-direct-run-declaration"
        bundle = root / "arbitrary-child-name"
        bundle.mkdir(parents=True)
        registry = root / "resource-runs.json"
        registry.write_text("{}\n")
        (bundle / "00-execution-manifest.data").write_text(
            json.dumps({"run_id": "direct-run"}))
        (bundle / "unrelated-admission-name.data").write_text(json.dumps({
            "admitted": False, "snapshot": {"t": 10}, "reasons": ["capacity"]}))

        run = metrics.read_admission_sources([root], run_id="direct-run",
            registry_path=registry, started_at=5, now=20)
        self.assertEqual(run["coverage"], "observed")
        self.assertEqual((run["latest"]["state"], run["latest"]["at"]),
                         ("denied", 10.0))
        binding = (run["cursor"]["scan"][str(root)]["bundle_cursors"]
                   [bundle.name]["binding_state"])
        self.assertTrue(binding["target_seen"])
        self.assertEqual(binding["generation_fields"], [{"run_id": "direct-run"}])

        row = metrics.read_admission_sources([root], run_id="direct-run", row_key="row-a",
            registry_path=registry, started_at=5, now=20)
        self.assertEqual(row["coverage"], "observed")
        self.assertEqual(row["latest"]["state"], "unknown")
        self.assertEqual(row["latest"]["reasons"], [])
        self.assertEqual([sample["state"] for sample in row["samples"]], ["unknown"])

    def test_validated_target_continues_without_broad_root_listing(self):
        root = self.root / "root-with-unrelated-siblings"
        target = root / "arbitrary-target"
        target.mkdir(parents=True)
        (target / "identity.data").write_text(
            '{"run_id":"direct-run","started_at":1}')
        (target / "decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":100}}')
        for index in range(12):
            (root / f"changing-sibling-{index:02d}").mkdir()
        first = metrics.read_admission_sources([root], run_id="direct-run",
            started_at=1, now=110)
        self.assertEqual(first["coverage"], "observed")
        original_scandir = os.scandir

        def unavailable_root_listing(path):
            if Path(path) == root:
                raise OSError("unrelated broad listing is unavailable")
            return original_scandir(path)

        with mock.patch.object(metrics.os, "scandir", unavailable_root_listing):
            continued = metrics.read_admission_sources([root], run_id="direct-run",
                started_at=1, now=111, cursor=first["cursor"])
        self.assertEqual(continued["coverage"], "observed")
        self.assertEqual(continued["latest"]["state"], "denied")
        self.assertEqual(continued["samples"], [])

    def test_v4_single_slot_listing_and_sample_dedup_are_adopted(self):
        root = self.root / "v4-cache-adoption"
        target = root / "selected-child"
        target.mkdir(parents=True)
        registry = root / "resource-runs.data"
        registry.write_text("{}\n")
        identity = target / "identity.data"
        identity.write_text('{"run_id":"legacy-run","started_at":1}')
        decision = target / "unrelated-decision.data"
        decision_doc = {"admitted": False, "snapshot": {"t": 20}}
        decision.write_text(json.dumps(decision_doc))
        first = metrics.read_admission_sources([root], run_id="legacy-run",
            registry_path=registry, started_at=1, now=30)
        cursor = first["cursor"]
        root_state = cursor["scan"][str(root)]
        bundle_progress = root_state["bundle_cursors"][target.name]
        listing = bundle_progress.pop("listing")
        root_state["bundle_listing"] = {"bundle": target.name, **listing}
        cursor["schema_version"] = 4
        cursor.pop("dedup_population", None)
        raw_sample_id = metrics._admission_decisions([decision_doc])[0][0]["sample_id"]
        cursor["sample_ids"] = [hashlib.sha256(
            (first["generation"] + ":" + raw_sample_id).encode()).hexdigest()]

        original_scandir = os.scandir
        def no_target_relisting(path):
            if Path(path) == target:
                raise OSError("the saved v4 listing should be adopted")
            return original_scandir(path)

        with mock.patch.object(metrics.os, "scandir", no_target_relisting):
            adopted = metrics.read_admission_sources([root], run_id="legacy-run",
                registry_path=registry, started_at=1, now=31, cursor=cursor)
        self.assertEqual(adopted["samples"], [])
        self.assertEqual(adopted["generation"], first["generation"])
        self.assertEqual(adopted["cursor"]["schema_version"], 5)
        migrated = adopted["cursor"]["scan"][str(root)]
        self.assertNotIn("bundle_listing", migrated)
        self.assertIn("listing", migrated["bundle_cursors"][target.name])

    def test_multiple_exact_target_bundles_are_unknown_until_one_remains(self):
        root = self.root / "duplicate-exact-bindings"
        for name, admitted, stamp in (("first", False, 10), ("second", True, 11)):
            bundle = root / name
            bundle.mkdir(parents=True)
            (bundle / "identity.data").write_text(json.dumps({
                "run_id": "target-run", "started_at": 1}))
            (bundle / "decision.json").write_text(json.dumps({
                "admitted": admitted, "snapshot": {"t": stamp}}))
        ambiguous = metrics.read_admission_sources([root], run_id="target-run",
            started_at=1, now=12)
        self.assertEqual(ambiguous["coverage"], "unknown")
        self.assertIsNone(ambiguous["latest"])
        self.assertEqual({row["state"] for row in ambiguous["samples"]}, {"unknown"})
        self.assertNotEqual(ambiguous["generation"], None)

        for path in (root / "second").iterdir():
            path.unlink()
        (root / "second").rmdir()
        resolved = metrics.read_admission_sources([root], run_id="target-run",
            started_at=1, now=13, cursor=ambiguous["cursor"])
        self.assertEqual(resolved["coverage"], "observed")
        self.assertEqual(resolved["latest"]["state"], "denied")
        self.assertEqual([row["state"] for row in resolved["samples"]], ["denied"])

    def test_admission_oversized_root_and_child_lists_retry_partial_inventory(self):
        original_scandir = os.scandir

        class CountingScandir:
            def __init__(self, entries, counter):
                self.entries = entries
                self.counter = counter
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.index >= len(self.entries):
                    raise StopIteration
                entry = self.entries[self.index]
                self.index += 1
                self.counter[0] += 1
                return entry

        for listing_kind in ("root", "child"):
            for reverse in (False, True):
                root = self.root / f"oversized-{listing_kind}-{reverse}"
                if listing_kind == "root":
                    scan_path = root
                    root.mkdir()
                    for index in range(5):
                        (root / f"entry-{index}.json").write_text("{}")
                else:
                    bundle = root / "bundle"
                    bundle.mkdir(parents=True)
                    scan_path = bundle
                    for index in range(5):
                        (bundle / f"entry-{index}.json").write_text("{}")
                calls = {}
                yielded = [0]

                def ordered_scandir(path):
                    path = Path(path)
                    calls[str(path)] = calls.get(str(path), 0) + 1
                    entries = list(original_scandir(path))
                    entries.sort(key=lambda entry: entry.name, reverse=reverse)
                    return CountingScandir(entries, yielded)

                cursor = None
                results = []
                with self.subTest(listing_kind=listing_kind, reverse=reverse), \
                        mock.patch.object(metrics, "MAX_ADMISSION_ENTRIES", 3), \
                        mock.patch.object(metrics.os, "scandir", ordered_scandir):
                    for _ in range(4):
                        yielded[0] = 0
                        result = metrics.read_admission_sources([root], run_id="target",
                            now=20, cursor=cursor)
                        cursor = result["cursor"]
                        results.append(result)
                        self.assertEqual(result["entries"], yielded[0])
                        self.assertLessEqual(yielded[0], 3)

                expected_warning = ("admission-root-listing-limit"
                    if listing_kind == "root" else "admission-bundle-listing-limit")
                self.assertTrue(any(expected_warning in item["warnings"] for item in results))
                self.assertEqual(results[-1]["coverage"], "partial")
                self.assertGreater(calls.get(str(scan_path), 0), 1)
                for item in results:
                    root_state = item["cursor"]["scan"].get(str(root), {})
                    self.assertNotIn("root_blocked_generation", root_state)
                    self.assertFalse(root_state.get("blocked_bundles"))

    def test_admission_reader_keeps_backward_time_unknown_until_new_valid_sample(self):
        root = self.root / "time-order"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text(
            '{"run_id":"target","started_at":0}')
        decision = bundle / "decision.json"
        decision.write_text('{"admitted":false,"snapshot":{"t":100}}')
        first = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110)
        self.assertEqual(first["latest"]["state"], "denied")
        import shutil
        shutil.rmtree(bundle)
        vanished = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=first["cursor"])
        self.assertIsNone(vanished["latest"])
        self.assertTrue(vanished["cursor"]["generation_revalidation_required"])
        self.assertEqual(vanished["cursor"]["historical_latest_at"], 100.0)
        bundle.mkdir()
        (bundle / "record.json").write_text(
            '{"run_id":"target","started_at":0}')
        decision.write_text('{"admitted":false,"snapshot":{"t":100}}')
        reappeared = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=112, cursor=vanished["cursor"])
        self.assertEqual(reappeared["latest"]["state"], "unknown")
        self.assertTrue(reappeared["cursor"]["generation_revalidation_required"])
        self.assertEqual(reappeared["cursor"]["historical_latest_at"], 100.0)
        decision.write_text('{"admitted":true,"snapshot":{"t":50}}')
        backward = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110, cursor=reappeared["cursor"])
        self.assertEqual(backward["latest"]["state"], "unknown")
        repeat = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=backward["cursor"])
        self.assertEqual(repeat["latest"]["state"], "unknown")
        replacement = bundle / "replacement.json"
        replacement.write_text('{"admitted":true,"snapshot":{"t":120}}')
        replacement.replace(decision)
        recovered = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=121, cursor=repeat["cursor"])
        self.assertEqual(recovered["latest"]["state"], "admitted")
        self.assertFalse(recovered["cursor"]["generation_revalidation_required"])
        self.assertEqual(recovered["cursor"]["historical_latest_at"], 120.0)
        self.assertIsNone(recovered["cursor"]["uncertainty_reason"])

    def test_same_stat_replay_after_transition_stays_unknown_until_fresh_equal_sample(self):
        root = self.root / "same-stat-replay"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        identity = bundle / "identity.json"
        decision = bundle / "decision.json"
        identity.write_text('{"run_id":"target","started_at":0}')
        decision.write_text('{"admitted":false,"snapshot":{"t":100}}')
        cursor = None
        first = None
        for poll in range(16):
            first = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=110 + poll, cursor=cursor)
            cursor = first["cursor"]
            if first["coverage"] == "observed":
                break
        self.assertEqual(first["coverage"], "observed")
        self.assertEqual(first["latest"]["state"], "denied")
        source_tokens = {path.name: metrics._admission_stat_token(path.lstat())
                         for path in (identity, decision)}

        # Model an already observed disappearance/replacement whose filesystem
        # reuses every stat token and whose old JSON has the same timestamp.
        transitioned = copy.deepcopy(first["cursor"])
        transitioned.update(uncertainty_reason="generation-changed",
            generation_revalidation_required=True,
            generation_baseline_at=100.0,
            generation_baseline_ids=list(first["cursor"]["historical_latest_ids"]),
            generation_baseline_overflow=False, generation_baseline_ready=True)
        replay = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=transitioned)
        self.assertEqual(replay["latest"]["state"], "unknown")
        self.assertTrue(replay["cursor"]["generation_revalidation_required"])
        self.assertEqual(replay["cursor"]["historical_latest_at"], 100.0)
        self.assertEqual(source_tokens, {path.name: metrics._admission_stat_token(path.lstat())
                                        for path in (identity, decision)})

        fresh = bundle / "independent-equal-sample.json"
        fresh.write_text('{"admitted":true,"sample_key":"fresh",'
                         '"snapshot":{"t":100}}')
        recovered = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=112, cursor=replay["cursor"])
        self.assertEqual(recovered["latest"]["state"], "admitted")
        self.assertEqual(recovered["latest"]["at"], 100.0)
        self.assertFalse(recovered["cursor"]["generation_revalidation_required"])

    def test_equal_time_revalidation_keeps_historical_ids_within_existing_sample_cap(self):
        root = self.root / "equal-time-transition-history"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "identity.json").write_text(
            '{"run_id":"target","started_at":0}')
        for index in range(140):
            (bundle / f"sample-{index:03d}.json").write_text(json.dumps({
                "admitted": index % 2 == 0, "sample_index": index,
                "snapshot": {"t": 100}}))
        cursor = None
        first = None
        for poll in range(16):
            first = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=110 + poll, cursor=cursor)
            cursor = first["cursor"]
            if first["coverage"] == "observed":
                break
        self.assertEqual(first["coverage"], "observed")
        self.assertEqual(first["latest"]["at"], 100.0)
        self.assertEqual(len(first["cursor"]["historical_latest_ids"]), 140)

        transitioned = copy.deepcopy(first["cursor"])
        transitioned.update(uncertainty_reason="generation-changed",
            generation_revalidation_required=True,
            generation_baseline_at=100.0,
            generation_baseline_ids=list(first["cursor"]["historical_latest_ids"]),
            generation_baseline_overflow=False, generation_baseline_ready=True)
        (bundle / "sample-fresh-equal.json").write_text(json.dumps({
            "admitted": True, "sample_index": 1000, "snapshot": {"t": 100}}))
        cursor = transitioned
        recovered = None
        for poll in range(16):
            recovered = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=130 + poll, cursor=cursor)
            cursor = recovered["cursor"]
            if not cursor["generation_revalidation_required"]:
                break
        self.assertEqual(recovered["latest"]["state"], "admitted")
        self.assertEqual(recovered["latest"]["at"], 100.0)
        self.assertFalse(recovered["cursor"]["generation_revalidation_required"])

    def test_validated_target_recovers_after_fair_sibling_turn_without_new_sample(self):
        root = self.root / "fair-sibling-replay"
        target = root / "z-target"
        target.mkdir(parents=True)
        (target / "identity.json").write_text(
            '{"run_id":"target","started_at":0}')
        (target / "decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":100}}')
        first = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110)
        self.assertEqual(first["latest"]["state"], "denied")
        (root / "a-sibling").mkdir()
        (root / "a-sibling" / "noise.data").write_text(
            '{"padding":"' + "x" * 240 + '"}')

        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 64):
            sibling_turn = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=111, cursor=first["cursor"])
            self.assertIsNone(sibling_turn["latest"])
            self.assertIn(sibling_turn["cursor"]["uncertainty_reason"],
                          {None, "partial"})
            self.assertFalse(sibling_turn["cursor"]["generation_revalidation_required"])
            resumed = metrics.read_admission_sources([root], run_id="target",
                started_at=0, now=112, cursor=sibling_turn["cursor"])

        self.assertEqual(resumed["coverage"], "observed")
        self.assertEqual(resumed["samples"], [])
        self.assertEqual(resumed["latest"]["state"], "denied")
        self.assertIsNone(resumed["cursor"]["uncertainty_reason"])

    def test_replaced_latest_decision_stays_unknown_until_new_equal_evidence(self):
        root = self.root / "latest-decision-replacement"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "identity.json").write_text(
            '{"run_id":"target","started_at":0}')
        decision = bundle / "decision.json"
        old = '{"admitted":false,"snapshot":{"t":100}}'
        decision.write_text(old)
        first = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110)
        old_token = metrics._admission_stat_token(decision.lstat())
        self.assertEqual(first["latest"]["state"], "denied")

        decision.write_text(old)
        self.assertNotEqual(metrics._admission_stat_token(decision.lstat()), old_token)
        replay = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=first["cursor"])
        self.assertEqual(replay["latest"]["state"], "unknown")
        self.assertTrue(replay["cursor"]["generation_revalidation_required"])
        self.assertIn("admission-bundle-generation-changed", replay["warnings"])

        decision.write_text('{"admitted":true,"sample_key":"fresh-equal",'
                            '"snapshot":{"t":100}}')
        recovered = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=112, cursor=replay["cursor"])
        self.assertEqual(recovered["latest"]["state"], "admitted")
        self.assertEqual(recovered["latest"]["at"], 100.0)
        self.assertFalse(recovered["cursor"]["generation_revalidation_required"])

    def test_older_timestamp_in_a_different_file_is_not_a_counter_regression(self):
        root = self.root / "historical-file-order"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "run.json").write_text('{"run_id":"target","started_at":0}')
        (bundle / "a-newer.json").write_text(
            '{"admitted":false,"snapshot":{"t":100}}')
        (bundle / "z-older.json").write_text(
            '{"admitted":true,"snapshot":{"t":90}}')
        result = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=110)
        self.assertEqual(result["coverage"], "observed")
        self.assertEqual({sample["state"] for sample in result["samples"]},
                         {"admitted", "denied"})
        self.assertEqual(result["latest"]["at"], 100.0)

    def test_admission_reader_rejects_boolean_future_oversized_and_linked_records(self):
        root = self.root / "invalid-samples"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text(
            '{"run_id":"target","started_at":0}')
        decision = bundle / "decision.json"
        decision.write_text('{"admitted":false,"snapshot":{"t":true}}')
        boolean = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=100)
        self.assertEqual(boolean["latest"]["state"], "unknown")
        decision.write_text('{"admitted":false,"snapshot":{"t":1000}}')
        future = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=100, cursor=boolean["cursor"])
        self.assertEqual(future["latest"]["state"], "unknown")
        decision.write_text('{"admitted":true,"snapshot":{"t":110}}')
        valid = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=111, cursor=future["cursor"])
        self.assertEqual(valid["latest"]["state"], "admitted")

        outside = self.root / "outside.json"
        outside.write_text('{"admitted":true,"snapshot":{"t":111}}')
        (bundle / "linked.json").symlink_to(outside)
        (bundle / "oversized.json").write_text('{"padding":"' + "x" * 70000 + '"}')
        try:
            os.mkfifo(bundle / "device.json")
        except (AttributeError, NotImplementedError, OSError):
            pass
        damaged = metrics.read_admission_sources([root], run_id="target",
            started_at=0, now=112, cursor=valid["cursor"])
        self.assertEqual(damaged["coverage"], "partial")
        self.assertEqual(damaged["latest"]["state"], "unknown")
        self.assertTrue(any("symlink" in warning for warning in damaged["warnings"]))
        self.assertTrue(any("unbounded" in warning for warning in damaged["warnings"]))

    def test_admission_reader_obeys_time_and_byte_budgets(self):
        root = self.root / "bounded"
        bundle = root / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "record.json").write_text('{"run_id":"target","started_at":0}')
        (bundle / "decision.json").write_text(
            json.dumps({"admitted": False, "snapshot": {"t": 10}, "padding": "x" * 400}))
        clock = [0.0]
        def monotonic():
            clock[0] += 0.0002
            return clock[0]
        timed = metrics.read_admission_sources([root], run_id="target", started_at=0,
            now=20, monotonic=monotonic, time_budget=0.0008)
        self.assertEqual(timed["coverage"], "partial")
        self.assertLessEqual(timed["entries"], metrics.MAX_ADMISSION_ENTRIES)
        with mock.patch.object(metrics, "MAX_ADMISSION_BYTES_PER_PASS", 128):
            sized = metrics.read_admission_sources([root], run_id="target", started_at=0, now=20)
        self.assertLessEqual(sized["bytes"], 128)
        self.assertEqual(sized["coverage"], "partial")

    def test_transient_slow_listing_recovers_on_same_generation(self):
        root = self.root / "slow-listing-recovery"
        bundle = root / "child"
        bundle.mkdir(parents=True)
        registry = root / "resource-runs.json"
        registry.write_text("{}\n")
        (bundle / "01-identity.json").write_text(
            '{"run_id":"slow-run","started_at":1}')
        (bundle / "02-decision.json").write_text(
            '{"admitted":false,"snapshot":{"t":10}}')
        ticks = [0]

        def temporarily_slow_clock():
            ticks[0] += 1
            return 0.0 if ticks[0] <= 5 else 1.0

        partial = metrics.read_admission_sources([root], run_id="slow-run",
            registry_path=registry, started_at=1, now=20,
            monotonic=temporarily_slow_clock, time_budget=0.25)
        self.assertEqual(partial["coverage"], "partial")
        self.assertNotIn("root_blocked_generation", partial["cursor"]["scan"][str(root)])
        recovered = metrics.read_admission_sources([root], run_id="slow-run",
            registry_path=registry, started_at=1, now=20, cursor=partial["cursor"])
        self.assertEqual(recovered["coverage"], "observed")
        self.assertEqual(recovered["latest"]["state"], "denied")
        self.assertEqual([row["state"] for row in recovered["samples"]], ["denied"])


if __name__ == "__main__":
    unittest.main()
