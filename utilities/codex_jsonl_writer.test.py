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

    def test_usage_sidecar_path_for_codex_log_naming_convention(self):
        # Real dispatch call sites name the log "<slug>.<attempt>.codex.jsonl";
        # the sidecar must be "<slug>.<attempt>.codex.usage.json", not
        # "<slug>.<attempt>.codex.<attempt>.usage.json" (C-17-3 regression).
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "my-slug.att-3.codex.jsonl"
            payload = '{"usage":{"input_tokens":1,"output_tokens":1}}'
            code = f"print({payload!r})"
            self.assertEqual(WRITER.run(log, "att-3", [sys.executable, "-c", code]), 0)
            expected = Path(td) / "my-slug.att-3.codex.usage.json"
            self.assertTrue(expected.exists(), f"expected sidecar at {expected}")
            duplicated = Path(td) / "my-slug.att-3.codex.att-3.usage.json"
            self.assertFalse(duplicated.exists())
            self.assertEqual(json.loads(expected.read_text())["attempt_id"], "att-3")

    def test_usage_sidecar_rerun_overwrites_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "my-slug.att-4.codex.jsonl"
            sidecar = Path(td) / "my-slug.att-4.codex.usage.json"
            code1 = "print('{\"usage\":{\"input_tokens\":1}}')"
            self.assertEqual(WRITER.run(log, "att-4", [sys.executable, "-c", code1]), 0)
            self.assertEqual(json.loads(sidecar.read_text())["usage"]["input"], 1)
            code2 = "print('{\"usage\":{\"input_tokens\":9}}')"
            self.assertEqual(WRITER.run(log, "att-4", [sys.executable, "-c", code2]), 0)
            self.assertEqual(json.loads(sidecar.read_text())["usage"]["input"], 9)
            tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
            self.assertFalse(tmp.exists())

    def test_stderr_is_merged_into_log(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.jsonl"
            code = "import sys; sys.stderr.write('boom\\n'); print('ok')"
            self.assertEqual(WRITER.run(log, "", [sys.executable, "-c", code]), 0)
            lines = log.read_text().splitlines()
            self.assertIn("boom", lines)
            self.assertIn("ok", lines)

    def test_appends_to_existing_log_without_truncating(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.jsonl"
            log.write_text("previous-line\n")
            code = "print('new-line')"
            self.assertEqual(WRITER.run(log, "", [sys.executable, "-c", code]), 0)
            lines = log.read_text().splitlines()
            self.assertEqual(lines, ["previous-line", "new-line"])

    def test_invalid_utf8_line_is_preserved_raw(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.jsonl"
            code = "import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\n'); sys.stdout.buffer.flush()"
            self.assertEqual(WRITER.run(log, "", [sys.executable, "-c", code]), 0)
            self.assertEqual(log.read_bytes(), b"\xff\xfe\n")

    def test_final_partial_line_without_trailing_newline_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "x.jsonl"
            code = "import sys; sys.stdout.write('no-newline-tail'); sys.stdout.flush()"
            self.assertEqual(WRITER.run(log, "", [sys.executable, "-c", code]), 0)
            self.assertEqual(log.read_bytes(), b"no-newline-tail")


if __name__ == "__main__":
    unittest.main()
