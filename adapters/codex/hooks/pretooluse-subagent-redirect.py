#!/usr/bin/env python3
"""Codex PreToolUse(spawn_agent): start a person's session's native subagent call as a Hearting unit
route when that is certain, and refuse the native call (utilities/native_subagent_redirect.py). When the
route did not start the native subagent runs as before; `run-hook.sh` turns any failure of this bridge
into a silent exit 0."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REDIRECT = ROOT / "utilities" / "native_subagent_redirect.py"

if __name__ == "__main__":
    if not REDIRECT.is_file():
        raise SystemExit(0)
    os.execv(sys.executable, [sys.executable, str(REDIRECT), "--codex"])
