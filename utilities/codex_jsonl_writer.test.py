import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("writer", ROOT / "utilities" / "codex-jsonl-writer.py")
WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRITER)


class CodexJsonlWriterTests(unittest.TestCase):
    def test_timestamps_json_and_preserves_non_json_and_exit(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.jsonl"
            code = "import sys; print('{\\\"type\\\":\\\"event\\\"}'); print('plain'); sys.exit(3)"
            rc = WRITER.run(log, "att-1", [sys.executable, "-c", code])
            self.assertEqual(rc, 3)
            lines = log.read_text().splitlines()
            event = json.loads(lines[0])
            self.assertIn("timestamp", event)
            self.assertEqual(lines[1], "plain")

    def test_existing_timestamp_and_usage_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.jsonl"
            payload = '{"timestamp":"old","usage":{"input_tokens":2,"cached_input_tokens":0,"output_tokens":3}}'
            code = f"print({payload!r})"
            self.assertEqual(WRITER.run(log, "att-2", [sys.executable, "-c", code]), 0)
            event = json.loads(log.read_text())
            self.assertEqual(event["timestamp"], "old")
            sidecar = Path(td) / "x.att-2.usage.json"
            self.assertEqual(json.loads(sidecar.read_text())["usage"]["cached_input"], 0)


if __name__ == "__main__":
    unittest.main()
