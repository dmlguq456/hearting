#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conversation_reader as reader
import tidy_transcripts


class ReaderTests(unittest.TestCase):
    def test_native_reads_forward_through_empty_intermediate_chunk(self):
        entry = {"harness": "claude", "sid": "s", "transcript": {"path": "/tmp/transcript"}}
        chunks = [
            tidy_transcripts.Chunk("claude", "/tmp/transcript", 0, 4, eof=False),
            tidy_transcripts.Chunk("claude", "/tmp/transcript", 4, 8, eof=False, text="first"),
            tidy_transcripts.Chunk("claude", "/tmp/transcript", 8, 9, eof=True, text="last"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript"
            path.write_text("fixture")
            entry["transcript"]["path"] = str(path)
            with patch.object(reader.tidy_transcripts, "read_chunk", side_effect=chunks) as read:
                result = reader.read_conversation(entry)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.text, "first\nlast")
        self.assertEqual([call.kwargs["cursor"] for call in read.call_args_list], [0, 4, 8])

    def test_missing_and_unreadable_are_honest(self):
        entry = {"harness": "claude", "sid": "s", "transcript": {"path": "/missing/transcript"}}
        self.assertEqual(reader.read_conversation(entry).status, "missing")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript"
            path.mkdir()
            entry["transcript"]["path"] = str(path)
            self.assertEqual(reader.read_conversation(entry).status, "unreadable")

    def test_cursor_stall_is_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript"
            path.write_text("fixture")
            entry = {"harness": "claude", "sid": "s", "transcript": {"path": str(path)}}
            stalled = tidy_transcripts.Chunk("claude", str(path), 0, 0, eof=False)
            with patch.object(reader.tidy_transcripts, "read_chunk", return_value=stalled):
                self.assertEqual(reader.read_conversation(entry).status, "partial")

    def test_native_display_strips_terminal_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript"
            path.write_text("fixture")
            entry = {"harness": "claude", "sid": "s", "transcript": {"path": str(path)}}
            chunk = tidy_transcripts.Chunk("claude", str(path), 0, 1, eof=True,
                                            text="[user] safe\x1b[31mhidden")
            with patch.object(reader.tidy_transcripts, "read_chunk", return_value=chunk):
                result = reader.read_conversation(entry)
        self.assertEqual(result.text, "[user] safehidden")

    def test_large_native_transcript_reaches_both_ends_without_writing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.jsonl"
            rows = [
                {"type": "user", "message": {"content": [{"type": "text", "text": "FIRST-MARKER" + "x" * 300_000}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "LAST-MARKER"}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            before = path.read_bytes()
            result = reader.read_conversation({"harness": "claude", "sid": "s",
                                               "transcript": {"path": str(path)}})
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result.status, "available")
        self.assertIn("FIRST-MARKER", result.text)
        self.assertIn("LAST-MARKER", result.text)

    def test_attempt_log_uses_existing_formatter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".att-example.claude.jsonl"
            path.write_text(json.dumps({"type": "user", "message": {"content": "marker"}}) + "\n")
            entry = {"harness": "claude", "sid": "s", "transcript": {"path": str(path)}}
            result = reader.read_conversation(entry)
        self.assertEqual(result.status, "available")
        self.assertIn("marker", result.text)


if __name__ == "__main__":
    unittest.main()
