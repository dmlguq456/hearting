#!/usr/bin/env python3
"""Append the terminal result row a one-shot OpenCode V2 run does not emit.

V2's ``run --format json`` stream ends with the final text and no ``step_finish``
stop; the CLI returns only after the native session wait. The owner runtime
(`opencode_session_runtime`) already closes a turn from the exact session, an
error-free final text and exit 0. A stage or review worker writes the raw stream
straight to its log, so this applies the same rule once the command returns and
appends one ``{"type": "result", "runtime": "opencode"}`` row for the shared
terminal reader. It never changes the command's exit status.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opencode_runtime  # noqa: E402


def terminal_row(lines, code: int) -> dict:
    """Build the result row from the raw stream lines and the command's exit code."""
    session = ""
    mismatch = False
    final_text = ""
    runtime_error = None
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue  # Native CLI diagnostics are not result evidence.
        if not isinstance(value, dict):
            continue
        kind = value.get("type")
        native = value.get("sessionID")
        if not native:
            if kind == "error":
                runtime_error = opencode_runtime.safe_provider_error(value.get("error"))
            continue
        if not isinstance(native, str) or not opencode_runtime.SESSION_ID_RE.match(native):
            mismatch = True
            continue
        if session and native != session:
            mismatch = True
        session = session or native
        part = value.get("part") if isinstance(value.get("part"), dict) else {}
        if kind == "text":
            final_text = part.get("text", "") if isinstance(part.get("text"), str) else ""
        elif kind == "step_start":
            final_text = ""
        elif kind == "error":
            runtime_error = opencode_runtime.safe_provider_error(value.get("error"))
    success = bool(code == 0 and session and not mismatch and final_text.strip()
                   and not runtime_error)
    row = {"type": "result", "runtime": "opencode", "session_id": session,
           "subtype": "success" if success else "error_during_execution",
           "is_error": not success, "result": final_text, "exit_code": code}
    if mismatch:
        row["error"] = {"code": "opencode-session-identity-mismatch"}
    elif runtime_error:
        row["error"] = runtime_error
    elif not success:
        row["error"] = {"code": ("opencode-session-not-observed" if not session
                                 else "opencode-final-text-missing" if code == 0
                                 else "opencode-exit-nonzero")}
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    args = parser.parse_args(argv)
    log = Path(args.log)
    try:
        lines = log.read_bytes().splitlines()
    except OSError:
        lines = []
    row = terminal_row(lines, args.exit_code)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
