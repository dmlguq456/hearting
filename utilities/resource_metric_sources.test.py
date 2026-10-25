#!/usr/bin/env python3
import json
from pathlib import Path
import struct
import tempfile
import unittest

import resource_metric_sources as metrics


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


if __name__ == "__main__":
    unittest.main()
