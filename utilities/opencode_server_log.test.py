#!/usr/bin/env python3
"""plan.md item 5: exact-session-bound OpenCode server log evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opencode_server_log as OSL


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


class OpencodeServerLogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.attempt_log = self.base / "att.opencode.jsonl"
        self.server_log = (
            self.base / ".dispatch" / "opencode-runtime" / "att-1"
            / "data" / "opencode" / "log" / "opencode.log"
        )

    def _metadata(self, **overrides) -> dict:
        metadata = {
            "harness": "opencode",
            "attempt_id": "att-1",
            "worktree": str(self.base),
            "log_file": str(self.attempt_log),
            "started_at": "2026-09-24T00:00:00Z",
        }
        metadata.update(overrides)
        return metadata

    def test_exact_session_binding(self):
        _write_jsonl(self.attempt_log, [
            {"type": "step_start", "sessionID": "ses_x111"},
            {"type": "text", "sessionID": "ses_x111", "part": {"text": "hi"}},
        ])
        # X and its parent Y hit the same wall-clock second -- exact session
        # matching, not proximity, must pick X's line.
        _write_jsonl(self.server_log, [
            {"level": "ERROR", "time": "2026-09-24T00:05:00Z",
             "session": {"id": "ses_y999"},
             "error": {"error": "Monthly usage limit reached. Resets in 13 days"}},
            {"level": "ERROR", "time": "2026-09-24T00:05:00Z",
             "session": {"id": "ses_x111"},
             "error": {"error": "Monthly usage limit reached. Resets in 13 days"}},
        ])
        found = OSL.session_error(self._metadata())
        self.assertIsNotNone(found)
        message, path = found
        self.assertEqual(message, "Monthly usage limit reached. Resets in 13 days")
        self.assertEqual(path, self.server_log)

    def test_ambiguous_attempt_log_session_is_none(self):
        _write_jsonl(self.attempt_log, [
            {"type": "step_start", "sessionID": "ses_x111"},
            {"type": "step_start", "sessionID": "ses_x222"},
        ])
        _write_jsonl(self.server_log, [
            {"level": "ERROR", "time": "2026-09-24T00:05:00Z",
             "session": {"id": "ses_x111"}, "error": {"error": "usage limit"}},
        ])
        self.assertIsNone(OSL.bind_session(self.attempt_log))
        self.assertIsNone(OSL.session_error(self._metadata()))

    def test_error_before_started_at_is_none(self):
        _write_jsonl(self.attempt_log, [{"type": "step_start", "sessionID": "ses_x111"}])
        _write_jsonl(self.server_log, [
            {"level": "ERROR", "time": "2026-09-23T23:00:00Z",
             "session": {"id": "ses_x111"}, "error": {"error": "usage limit"}},
        ])
        self.assertIsNone(OSL.session_error(self._metadata(started_at="2026-09-24T00:00:00Z")))

    def test_no_server_log_present_is_none(self):
        _write_jsonl(self.attempt_log, [{"type": "step_start", "sessionID": "ses_x111"}])
        self.assertIsNone(OSL.session_error(self._metadata()))

    def test_inherited_xdg_path_is_tried_when_nested_is_absent(self):
        _write_jsonl(self.attempt_log, [{"type": "step_start", "sessionID": "ses_x111"}])
        xdg_log = self.base / "xdg-data" / "opencode" / "log" / "opencode.log"
        _write_jsonl(xdg_log, [
            {"level": "ERROR", "time": "2026-09-24T00:05:00Z",
             "session": {"id": "ses_x111"}, "error": {"error": "usage limit"}},
        ])
        old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.base / "xdg-data")
        try:
            found = OSL.session_error(self._metadata())
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old_xdg
        self.assertIsNotNone(found)
        self.assertEqual(found[1], xdg_log)


if __name__ == "__main__":
    unittest.main()
