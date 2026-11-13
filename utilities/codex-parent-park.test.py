#!/usr/bin/env python3
"""Regression tests for the retired Codex all-tool parent park."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "adapters" / "codex" / "hooks" / "hooks.json"


class ParentParkRetirementTest(unittest.TestCase):
    def test_hook_manifest_has_no_wildcard_parent_park(self):
        config = json.loads(HOOKS.read_text(encoding="utf-8"))
        entries = config["hooks"].get("PreToolUse", [])
        self.assertFalse([entry for entry in entries if entry.get("matcher") == "*"])
        rendered = json.dumps(entries)
        self.assertNotIn("AGENT_PARENT_PARK_ONLY", rendered)
        self.assertNotIn("parent-park", rendered)


if __name__ == "__main__":
    unittest.main()
