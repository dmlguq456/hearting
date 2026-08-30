#!/usr/bin/env python3
"""A-8: chain-scoped handoff missing/attempt-mismatch/manifest-mismatch/
mtime-inversion each classify as a hard stop, never `ok`."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_subsession_handoff as HANDOFF  # noqa: E402


class HandoffTest(unittest.TestCase):
    def _flush(self, path: Path, *, predecessor_attempt_id="att-1", manifest_sha256="sha-1"):
        HANDOFF.flush_handoff(
            path,
            predecessor_attempt_id=predecessor_attempt_id,
            predecessor_subsession_id="ss-1",
            manifest_sha256=manifest_sha256,
            completed_items=["did the thing"],
            next_command="python3 utilities/dispatch-node.py --action start",
            invariants=["never touch baseline"],
            forbidden_files=["tools/run-tests.py"],
        )

    def test_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chain.handoff.md"
            result = HANDOFF.classify_handoff(
                path, predecessor_attempt_id_expected="att-1",
                manifest_sha256_expected="sha-1", predecessor_terminal_at_ns=0,
            )
            self.assertEqual(result, "subsession-handoff-missing")

    def test_attempt_id_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chain.handoff.md"
            self._flush(path, predecessor_attempt_id="att-wrong")
            result = HANDOFF.classify_handoff(
                path, predecessor_attempt_id_expected="att-1",
                manifest_sha256_expected="sha-1", predecessor_terminal_at_ns=0,
            )
            self.assertEqual(result, "subsession-handoff-stale")

    def test_manifest_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chain.handoff.md"
            self._flush(path, manifest_sha256="sha-wrong")
            result = HANDOFF.classify_handoff(
                path, predecessor_attempt_id_expected="att-1",
                manifest_sha256_expected="sha-1", predecessor_terminal_at_ns=0,
            )
            self.assertEqual(result, "subsession-handoff-stale")

    def test_mtime_inversion(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chain.handoff.md"
            self._flush(path)
            # Force the handoff's mtime BEFORE the predecessor's terminal
            # moment -- it was written before completion, so it cannot
            # describe that completion.
            past = time.time() - 3600
            os.utime(path, (past, past))
            terminal_at_ns = int(time.time() * 1_000_000_000)
            result = HANDOFF.classify_handoff(
                path, predecessor_attempt_id_expected="att-1",
                manifest_sha256_expected="sha-1",
                predecessor_terminal_at_ns=terminal_at_ns,
            )
            self.assertEqual(result, "subsession-handoff-stale")

    def test_ok(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chain.handoff.md"
            self._flush(path)
            result = HANDOFF.classify_handoff(
                path, predecessor_attempt_id_expected="att-1",
                manifest_sha256_expected="sha-1", predecessor_terminal_at_ns=0,
            )
            self.assertEqual(result, "ok")

    def test_handoff_path_is_artifact_root_scoped(self):
        path = HANDOFF.handoff_path("/artifact-root", "rt-1", "ssc-1")
        self.assertEqual(
            path, Path("/artifact-root/.runtime/stage-sessions/rt-1/ssc-1.handoff.md")
        )


if __name__ == "__main__":
    unittest.main()
