#!/usr/bin/env python3
"""Codex PreToolUse(request_user_input[_async]): carry the outcome of
utilities/native_question_redirect.py. A registered, already-open or unknown question is refused
with its one line; a question that goes through (registration failed before any write, no same
question read) carries its private reference on this thread's Fleet decision marker, so the shared
wait list counts it until the native question's own PostToolUse or the next prompt clears it."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REDIRECT = ROOT / "utilities" / "native_question_redirect.py"

if __name__ == "__main__":
    if not REDIRECT.is_file():
        raise SystemExit(0)
    os.execv(sys.executable, [sys.executable, str(REDIRECT), "--codex"])
