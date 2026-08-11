#!/usr/bin/env python3
"""Pre-push census completeness for utilities/* (generate.py battery member).

The projected/deferred decision for a new non-test utility is made by the agent
adding the file; enforcement lives in tools/check-adaptation-boundary.sh. That
guard, however, is not part of every session's habitual battery, so a forgotten
census row historically surfaced only on CI after push
(2026-07-23 dispatch_parent_context_conformance.test.py incident — and the same
class before it). This checker runs the SAME census membership rule in the
standard ``generate.py --check`` battery, so the acting agent is asked to record
its judgment BEFORE push. Single source of truth stays the boundary script: the
lists are parsed from it, never duplicated here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / "tools" / "check-adaptation-boundary.sh"
# Mirrors the boundary census: test files are DERIVED-deferred.
TEST_PATTERNS = (".test.py", ".test.sh")


def census_scopes(text: str) -> list[tuple[str, set[str]]]:
    projected = re.findall(r'^\s*UTILITY_PROJECTED="([^"]*)"', text, re.M)
    deferred = re.findall(r'^\s*UTILITY_DEFERRED="([^"]*)"', text, re.M)
    shared = re.findall(
        r'^\s*SHARED_UTILITY_DEFERRED="([^"]*)"', text, re.M
    )
    if len(projected) != 2 or len(deferred) != 2 or len(shared) != 1:
        raise SystemExit(
            "check-utility-census: expected exactly 2 UTILITY_PROJECTED and 2 "
            "UTILITY_DEFERRED lists plus 1 SHARED_UTILITY_DEFERRED list in "
            f"{BOUNDARY.name}, found {len(projected)}/{len(deferred)}/{len(shared)} "
            "— realign this parser with the guard"
        )
    shared_members = set(shared[0].split())
    scopes = []
    for label, p, d in (("codex", projected[0], deferred[0]),
                        ("opencode", projected[1], deferred[1])):
        scopes.append((label, set(p.split()) | set(d.split()) | shared_members))
    return scopes


def main() -> int:
    # --check and write mode behave identically: this tool only verifies.
    scopes = census_scopes(BOUNDARY.read_text(encoding="utf-8"))
    missing: list[str] = []
    for path in sorted((ROOT / "utilities").iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(TEST_PATTERNS):
            continue
        for label, members in scopes:
            if name not in members:
                missing.append(f"  utilities/{name} — {label} census")
    if missing:
        print("utility census rows missing (decide projected|deferred and add the", file=sys.stderr)
        print("name to UTILITY_PROJECTED or UTILITY_DEFERRED in", file=sys.stderr)
        print(f"{BOUNDARY.relative_to(ROOT)}; *.test.py/*.test.sh auto-defer):", file=sys.stderr)
        for row in missing:
            print(row, file=sys.stderr)
        return 1
    print("utility census complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
