#!/usr/bin/env python3
"""Codex PreToolUse(request_user_input): register the question as this thread's Hearting question
and refuse the native call (utilities/native_question_redirect.py). When nothing was registered
the native question goes through."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REDIRECT = ROOT / "utilities" / "native_question_redirect.py"

if __name__ == "__main__":
    if not REDIRECT.is_file():
        raise SystemExit(0)
    os.execv(sys.executable, [sys.executable, str(REDIRECT), "--codex"])
