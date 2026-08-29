#!/usr/bin/env python3
"""Detect whether a diff fires an installed-layout regression trigger (C-4).

Reads the closed rule set from tools/installed-layout-triggers.tsv (data
owner; do not duplicate the rule list here or in core/CONVENTIONS.md) and
evaluates it against `git diff --name-only <base>..<head>` (path rules,
projection rules) and `git diff -U0 <base>..<head>` (diff_symbol rules).

Exit 0 always (this is a report, not a gate) unless --assert-ran is given
and no rule fired, in which case CI should treat the missing installed-
layout regression run as a hard failure.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRIGGERS = ROOT / "tools" / "installed-layout-triggers.tsv"


def load_rules() -> list[dict[str, str]]:
    rules = []
    header_seen = False
    with TRIGGERS.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if not header_seen:
                header_seen = True
                continue
            rules.append(dict(zip(["rule_id", "kind", "pattern", "rationale"], cols)))
    return rules


def changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", f"{base}..{head}"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def diff_text(base: str, head: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "-U0", f"{base}..{head}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def path_matches(pattern: str, path: str) -> bool:
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.rstrip("/*") + "/*")


def evaluate(base: str, head: str) -> list[dict[str, str]]:
    rules = load_rules()
    paths = changed_paths(base, head)
    fired = []
    diff_cache: str | None = None

    for rule in rules:
        kind, pattern = rule["kind"], rule["pattern"]
        if kind in ("path", "projection"):
            alts = pattern.split("|")
            if any(path_matches(alt, p) for alt in alts for p in paths):
                fired.append(rule)
        elif kind == "resolver":
            alts = pattern.split("|")
            if any(p in alts for p in paths):
                fired.append(rule)
        elif kind == "diff_symbol":
            if diff_cache is None:
                diff_cache = diff_text(base, head)
            added_removed = [
                line[1:] for line in diff_cache.splitlines()
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            ]
            regex = re.compile(pattern)
            if any(regex.search(line) for line in added_removed):
                fired.append(rule)
    return fired


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--assert-ran", default=None)
    args = ap.parse_args(argv)

    fired = evaluate(args.base, args.head)
    if fired:
        for rule in fired:
            print(f"fired={rule['rule_id']} kind={rule['kind']}")
    else:
        print("fired=none")

    if args.assert_ran and not fired:
        print(f"FATAL: {args.assert_ran} required but no installed-layout trigger fired", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
