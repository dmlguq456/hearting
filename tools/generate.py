#!/usr/bin/env python3
"""Build or verify every core generated projection.

This is the single maintainer entrypoint. Runtime-specific generators remain
small adapter-owned components, but users and CI do not need to know or invoke
their historical `sync-native-*` names.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATORS = [
    ("utility-census", "tools/check-utility-census.py"),
    ("sd-procedure-hooks", "tools/check-sd-procedure-hooks.py"),
    ("runtime-memory-boundary", "tools/check-runtime-memory-boundary.py"),
    ("missing-projections", "tools/sync-missing-projections.py"),
    ("entry-skill-layer", "tools/sync-entry-skill-layer.py"),
    ("skill-invocation-policy", "tools/sync-skill-invocation-policy.py"),
    ("manifest-and-catalogs", "tools/build-manifest.py"),
    ("hub-page", "tools/render-hub.py"),
    ("landing-page", "tools/render-landing.py"),
    ("fleet-svg", "tools/render-fleet-svg.py"),
    ("claude-skill-metadata", "adapters/claude/bin/sync-native-metadata.py"),
    ("claude-agents", "adapters/claude/bin/sync-native-agents.py"),
    ("claude-plugin", "adapters/claude/bin/sync-native-plugin.py"),
    ("codex-skills", "adapters/codex/bin/sync-native-skills.py"),
    ("codex-agents", "adapters/codex/bin/sync-native-agents.py"),
    ("codex-modes", "adapters/codex/bin/sync-native-modes.py"),
    ("codex-plugin", "adapters/codex/bin/sync-native-plugin.py"),
    ("opencode-skills", "adapters/opencode/bin/sync-native-skills.py"),
    ("opencode-commands", "adapters/opencode/bin/sync-native-commands.py"),
    ("opencode-agents", "adapters/opencode/bin/sync-native-agents.py"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()

    failed: list[str] = []
    for name, relative in GENERATORS:
        command = [sys.executable, str(ROOT / relative)]
        if args.check:
            command.append("--check")
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0:
            failed.append(name)
            print(f"FAILED: {name} (exit {result.returncode})", file=sys.stderr)

    if failed:
        print("generated projection failures: " + ", ".join(failed), file=sys.stderr)
        return 1
    action = "checked" if args.check else "generated"
    print(f"{action} {len(GENERATORS)} core projection groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
