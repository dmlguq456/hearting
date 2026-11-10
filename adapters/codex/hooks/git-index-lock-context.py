#!/usr/bin/env python3
"""Codex PostToolUse(Bash) bridge to the shared index-lock hint in hooks/git-index-lock-context.py."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / "hooks" / "git-index-lock-context.py"

if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, str(BRIDGE), "--harness", "codex"])
