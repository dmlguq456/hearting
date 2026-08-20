"""F-82 bounded head+tail read for the codex attempt-log thread_id.

``thread.started`` — the only App Server event carrying ``thread_id`` — is written once,
on the log's first line. A tail-only read (the pre-F-82 shape) permanently loses it once
the log outgrows the tail window; ``6474f645``/``8c2d67eb`` were correct against short
logs and missed this because their fixtures never crossed the size threshold. Every
oversize fixture below is generated at test time above the 512 KiB tail bound — a
fixture under that threshold cannot catch this regression.
"""

import builtins
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import dispatch as d


def _line(payload):
    return json.dumps(payload) + "\n"


def _padding_line(kind, n):
    if kind == "item.started":
        return _line({"type": "item.started", "item": {
            "id": f"cmd-{n}", "type": "command_execution", "command": f"echo {n}"}})
    return _line({"type": "item.completed", "item": {"id": f"cmd-{n}"}})


def _write_oversize_log(path, thread_id, target_bytes, second_thread_id=None):
    """First line is ``thread.started``; pad with item rows past ``target_bytes``."""
    with open(path, "w") as fh:
        fh.write(_line({"type": "thread.started", "thread_id": thread_id}))
        if second_thread_id:
            fh.write(_line({"type": "turn.started"}))
        n = 0
        while fh.tell() < target_bytes:
            fh.write(_padding_line("item.started", n))
            fh.write(_padding_line("item.completed", n))
            n += 1
        if second_thread_id:
            fh.write(_line({"type": "thread.started", "thread_id": second_thread_id}))
        fh.write(_line({"type": "turn.completed"}))


class F82CodexAttemptHeadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d._CODEX_ATTEMPT_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_thread_id_survives_oversize_log(self):
        # 1.5 MB comfortably clears the 512 KiB tail window; a fixture at or below that
        # threshold would pass even against the pre-F-82 tail-only implementation and
        # would not catch this regression.
        path = self._path("oversize.jsonl")
        _write_oversize_log(path, "thread-oversize-1", 1_500_000)
        parsed = d._parse_codex_attempt_tail(path)
        self.assertEqual(parsed["thread_id"], "thread-oversize-1")
        self.assertFalse(parsed["thread_ambiguity"])

    def test_read_bytes_do_not_scale_with_file_size(self):
        small_path = self._path("small_oversize.jsonl")
        large_path = self._path("large_oversize.jsonl")
        _write_oversize_log(small_path, "thread-a", 1_500_000)
        _write_oversize_log(large_path, "thread-b", 6_000_000)

        budget = d._CLAUDE_SUPERVISOR_HEAD_BYTES + d._CLAUDE_STREAM_TAIL_BYTES
        real_open = open
        reads = {"small": 0, "large": 0}

        def _tracking_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if path == small_path:
                key = "small"
            elif path == large_path:
                key = "large"
            else:
                return handle
            orig_read = handle.read

            def _read(size=-1, *a, **kw):
                data = orig_read(size, *a, **kw)
                reads[key] += len(data)
                return data
            handle.read = _read
            return handle

        with mock.patch.object(builtins, "open", side_effect=_tracking_open):
            d._CODEX_ATTEMPT_CACHE.clear()
            d._parse_codex_attempt_tail(small_path)
            d._CODEX_ATTEMPT_CACHE.clear()
            d._parse_codex_attempt_tail(large_path)

        self.assertLessEqual(reads["small"], budget)
        self.assertLessEqual(reads["large"], budget)

    def test_ambiguity_preserved_across_head_and_tail(self):
        path = self._path("ambiguous.jsonl")
        _write_oversize_log(
            path, "thread-head", 1_500_000, second_thread_id="thread-tail")
        parsed = d._parse_codex_attempt_tail(path)
        self.assertTrue(parsed["thread_ambiguity"])
        self.assertIsNone(parsed["thread_id"])

    def test_malformed_first_line(self):
        path = self._path("malformed.jsonl")
        with open(path, "w") as fh:
            fh.write('{"type": "thread.started", "thread_id": \n')  # truncated/invalid JSON
            n = 0
            while fh.tell() < 1_500_000:
                fh.write(_padding_line("item.started", n))
                fh.write(_padding_line("item.completed", n))
                n += 1
            fh.write(_line({"type": "turn.completed"}))
        parsed = d._parse_codex_attempt_tail(path)
        self.assertIsNone(parsed["thread_id"])
        self.assertFalse(parsed["thread_ambiguity"])

    def test_small_log_single_read(self):
        path = self._path("small.jsonl")
        with open(path, "w") as fh:
            fh.write(_line({"type": "thread.started", "thread_id": "thread-small"}))
            fh.write(_line({"type": "turn.completed"}))
        real_open = open
        calls = []

        def _tracking_open(p, *args, **kwargs):
            if p == path:
                calls.append(1)
            return real_open(p, *args, **kwargs)

        with mock.patch.object(builtins, "open", side_effect=_tracking_open):
            parsed = d._parse_codex_attempt_tail(path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(parsed["thread_id"], "thread-small")


if __name__ == "__main__":
    unittest.main()
