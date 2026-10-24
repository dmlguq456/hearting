#!/usr/bin/env python3
"""Generate OpenCode-native command projections from portable capabilities."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CAPABILITIES = ROOT / "capabilities"
OUT = ROOT / "adapters" / "opencode" / "commands"
sys.path.insert(0, str(ROOT / "tools"))

import harness_manifest


def contract_rows(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2:
            rows[cells[0]] = "|".join(cells[1:]).strip()
    return rows


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).replace('"', "'")


def markdown_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == marker:
            start = index + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return compact("\n".join(body))


def render(identifier: str, spec: dict, capability_file: Path) -> tuple[str, str]:
    source = capability_file.read_text(encoding="utf-8")
    argument_shape = spec["argument_shape"]
    meaning = compact(spec["summary"])
    invocation_semantics = markdown_section(source, "Invocation Semantics")
    portable_contract = ""
    if invocation_semantics:
        portable_contract = f"""
Portable contract excerpt:

- Invocation semantics: {invocation_semantics}
"""
    description = compact(
        f"Run the portable {identifier} capability through the OpenCode adapter. "
        f"Meaning: {meaning}"
    )
    body = f"""---
description: "{description}"
---

Use the OpenCode adapter realization of portable capability `{identifier}`.
This is adapter-owned output generated from `capabilities/{identifier}.md`, not a runtime-specific command copy.

1. Read `capabilities/{identifier}.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info {identifier}` and
   obey `instruction-only`, `tool-contract`, or `unsupported` status. For
   `tool-contract`, report the named `tool_contract`, run any
   `tool_contract_check`, and obey `runtime_surface` / `fallback` before
   claiming full support. For `unsupported`, stop or use the reported
   `fallback`.
3. Before spec-changing work, run
   `adapters/opencode/bin/preflight.sh capability {identifier} [cwd] [session-id]`.
4. If the command receives arguments, map them to the portable argument shape:
   `{argument_shape}`.
{portable_contract}

User arguments from OpenCode: `$ARGUMENTS`

Do not use non-OpenCode command files or runtime-specific slash-command files
as OpenCode-native command source.
"""
    return identifier, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projections")
    args = parser.parse_args()

    manifest = harness_manifest.load()
    expected: dict[Path, str] = {}
    for identifier, spec in harness_manifest.skill_capabilities(manifest).items():
        capability_file = CAPABILITIES / f"{identifier}.md"
        identifier, body = render(identifier, spec, capability_file)
        expected[OUT / f"{identifier}.md"] = body

    stale: list[str] = []
    for path, body in expected.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    existing = sorted(OUT.glob("*.md")) if OUT.exists() else []
    extras = [path for path in existing if path not in expected]
    if args.check:
        stale.extend(str(path.relative_to(ROOT)) for path in extras)
    else:
        for path in extras:
            path.unlink()

    if stale:
        print("OpenCode native command projections are stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1

    if not args.check:
        print(f"generated {len(expected)} OpenCode native command projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
