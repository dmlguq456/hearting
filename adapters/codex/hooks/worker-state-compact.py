#!/usr/bin/env python3
"""Codex bridge for the portable worker state hook."""
from pathlib import Path
import os
import json
import subprocess
import sys

ROOT = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parents[3])
active = os.environ.get("AGENT_DISPATCH_STAGE_AUTHORITY") == "0" or bool(os.environ.get("AGENT_DISPATCH_SUBSESSION_ID"))
action = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities/worker-state-hook.py"), *sys.argv[1:]],
        input=None if active else sys.stdin.read(), text=True, capture_output=True, check=False,
        timeout=None if active else 5,
    )
except subprocess.TimeoutExpired:
    if action == "compact-after" and not active:
        sys.stderr.write("session-tidy: booking=pending reason=compact-bridge-timeout\n")
    raise SystemExit(0 if not active else 2)
except OSError:
    if action == "compact-after" and not active:
        sys.stderr.write("session-tidy: booking=pending reason=compact-bridge-unavailable\n")
    raise SystemExit(0 if not active else 2)
if active:
    sys.stdout.write(result.stdout or "")
    sys.stderr.write(result.stderr or "")
    raise SystemExit(result.returncode)
harness = next((sys.argv[i + 1] for i in range(len(sys.argv) - 1) if sys.argv[i] == "--harness"), "")
if result.returncode == 0 and action == "compact-before" and harness in ("claude", "opencode"):
    sys.stdout.write(result.stdout or "")
elif result.returncode == 0 and action == "compact-after":
    try:
        emitted = json.loads(result.stdout or "{}")
        notice = emitted.get("systemMessage", "")
    except (ValueError, AttributeError):
        notice = ""
    if notice:
        print(json.dumps({"systemMessage": notice}, ensure_ascii=False), flush=True)
        if emitted.get("deferredConsumed"):
            try:
                subprocess.run([sys.executable, str(ROOT / "utilities/session_tidy.py"), "_apply-deferred"],
                               input=json.dumps(emitted["deferredConsumed"]), text=True,
                               capture_output=True, timeout=1, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
    status = next((line.strip() for line in (result.stdout or "").splitlines()
                   if line.strip().startswith("booking=")), "")
    if status:
        sys.stderr.write(f"session-tidy: {status}\n")
raise SystemExit(0)
