#!/usr/bin/env python3
"""Fail-closed guard: every <...> placeholder token used inside
capabilities/topologies.json write_scope/spec_scope values is registered in
the closed vocabulary file tools/scope-placeholders.tsv (scope_valid=yes).

This is the build-time counterpart of hooks/artifact-guard.sh's runtime
substitution (re.sub(r"<[a-z_]+>", "*", scope)): the runtime is fail-safe
(substitutes any token), this checker is fail-closed (rejects any token that
was never registered as scope-valid).

Exit 1 on any unregistered token; exit 0 otherwise.
`--census` prints distinct/occurrence counts and the token distribution
(diagnostic only, always exit 0).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGIES = ROOT / "capabilities" / "topologies.json"
VOCAB = ROOT / "tools" / "scope-placeholders.tsv"

TOKEN_RE = re.compile(r"<[a-z_]+>")


def load_vocab() -> dict[str, str]:
    vocab: dict[str, str] = {}
    with VOCAB.open(encoding="utf-8") as fh:
        header_seen = False
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if not header_seen:
                header_seen = True
                continue
            if len(cols) < 2:
                continue
            token, scope_valid = cols[0], cols[1]
            vocab[token] = scope_valid
    return vocab


def collect_scope_values(node) -> list[str]:
    values: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("write_scope", "spec_scope") and isinstance(v, list):
                    values.extend(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(node)
    return values


def main(argv: list[str]) -> int:
    census = "--census" in argv
    data = json.loads(TOPOLOGIES.read_text(encoding="utf-8"))
    values = collect_scope_values(data)

    distinct = sorted(set(values))
    token_counts: Counter[str] = Counter()
    for value in values:
        for token in TOKEN_RE.findall(value):
            token_counts[token] += 1

    if census:
        print(f"distinct_scope_values={len(distinct)}")
        print(f"occurrences={len(values)}")
        for token, count in sorted(token_counts.items()):
            print(f"token={token} occurrences={count}")
        return 0

    vocab = load_vocab()
    violations: list[tuple[str, str, int]] = []
    for token, count in sorted(token_counts.items()):
        scope_valid = vocab.get(token)
        if scope_valid != "yes":
            violations.append((token, "unregistered" if token not in vocab else "scope_valid!=yes", count))

    if violations:
        for token, reason, count in violations:
            print(f"unregistered scope placeholder token: {token} ({reason}), occurrences={count}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
