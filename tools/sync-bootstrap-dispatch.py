#!/usr/bin/env python3
"""Project the shared bootstrap body into the three adapter bootstraps.

`core/fragments/bootstrap-body.md` is the one source for the purpose,
judgment, start/follow/finish, where-to-look, and response text every
bootstrap carries. Each bootstrap holds it, byte for byte, between its
BEGIN/END markers; everything outside the markers (harness-root resolution
and runtime notes) stays adapter-owned. `--check` reports a stale copy
without writing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("adapters/claude/CLAUDE.md", "adapters/codex/AGENTS.md", "adapters/opencode/AGENTS.md")
BLOCKS = (
    ("core/fragments/bootstrap-body.md", "tools/sync-bootstrap-dispatch.py"),
)


def _markers(source: str, tool: str) -> tuple[str, str]:
    begin = f"<!-- BEGIN generated from {source} by {tool}; edit the source -->"
    end = f"<!-- END generated from {source} -->"
    return begin, end


def render(text: str, body: str, target: str, begin: str, end: str) -> str:
    if text.count(begin) != 1 or text.count(end) != 1 or text.index(begin) > text.index(end):
        raise ValueError(f"{target}: needs exactly one generated-block marker pair for {begin[:60]}...")
    head, rest = text.split(begin, 1)
    _old, tail = rest.split(end, 1)
    return f"{head}{begin}\n{body.rstrip()}\n{end}{tail}"


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args(argv)
    bodies = [(src, tool, (root / src).read_text(encoding="utf-8")) for src, tool in BLOCKS]
    stale = []
    for target in TARGETS:
        path = root / target
        text = path.read_text(encoding="utf-8")
        try:
            wanted = text
            for src, tool, body in bodies:
                begin, end = _markers(src, tool)
                wanted = render(wanted, body, target, begin, end)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if wanted == text:
            continue
        stale.append(target)
        if not args.check:
            path.write_text(wanted, encoding="utf-8")
    if args.check and stale:
        print("stale bootstrap blocks: " + ", ".join(stale), file=sys.stderr)
        return 1
    print(f"bootstrap blocks {'checked' if args.check else 'generated'} in {len(TARGETS)} bootstraps ({len(BLOCKS)} blocks each)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
