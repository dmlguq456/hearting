#!/usr/bin/env python3
"""Thin Codex PostToolUse bridge to the shared spec synchronization hook."""
import os
import subprocess
import sys


def main():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    hook = os.path.join(root, "hooks", "spec-sync-nudge.sh")
    try:
        payload = sys.stdin.read()
        result = subprocess.run(
            ["bash", hook, "--format", "claude-json"],
            input=payload,
            text=True,
            capture_output=True,
            cwd=os.getcwd(),
            env={**os.environ, "AGENT_HOME": root},
            timeout=8,
        )
        if result.returncode == 0 and result.stdout:
            sys.stdout.write(result.stdout)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
