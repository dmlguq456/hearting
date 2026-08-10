#!/usr/bin/env python3
"""Generate OpenCode-native Skill projections from portable capabilities."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CAPABILITIES = ROOT / "capabilities"
OUT = ROOT / "adapters" / "opencode" / "skills"
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
    modes = ", ".join(spec["modes"]) or "none"
    argument_shape = spec["argument_shape"]
    meaning = compact(spec["summary"])
    invocation = spec["invocation"]
    invocation_class = invocation["class"]
    invocation_semantics = markdown_section(source, "Invocation Semantics")
    portable_contract = ""
    if invocation_semantics and invocation_class != "entry-router":
        portable_contract = f"""
## Portable Contract

- Invocation semantics: {invocation_semantics}
"""
    description = compact(f"{invocation['use_when']} {invocation['not_for']}")
    if invocation_class == "entry-router":
        use_steps = f"""1. Before approval, route from this compact metadata and `core/WORKFLOW.md §0.2`; do not read the full portable source merely to propose the route.
2. Present the five-field confirmation card from `core/WORKFLOW.md §0.4` unless the same route and scope are already approved.
3. After approval, direct/quick acting sessions read `capabilities/{identifier}.md`; at `standard+`, the dispatch-depth-1 owner reads it and stage workers read only their assigned contracts.
4. Before the first durable capability artifact, compile and bind the checked route with `preflight.sh route --capability {identifier} ...`. A native-agent restriction never authorizes direct execution.
5. Run `adapters/opencode/bin/preflight.sh capability-info {identifier}` and obey the reported status:"""
        route_guard = f"- Before durable capability output: `adapters/opencode/bin/preflight.sh route --capability {identifier} <complete compile arguments>`"
    else:
        use_steps = f"""1. Read `capabilities/{identifier}.md` for the runtime-neutral contract.
2. Run `adapters/opencode/bin/preflight.sh capability-info {identifier}`.
3. Obey the reported status:"""
        route_guard = ""

    body = f"""---
name: {identifier}
description: "{description}"
metadata:
  portable_source: capabilities/{identifier}.md
  adapter: opencode
  invocation_class: {invocation_class}
---

# {identifier}

This is an OpenCode-native Skill projection generated from the portable
capability contract. It is adapter-owned output, not a legacy compatibility Skill copy.

## Source

- Portable source: `capabilities/{identifier}.md`
- Runtime check: `adapters/opencode/bin/preflight.sh capability-info {identifier}`
- Bootstrap: `adapters/opencode/AGENTS.md`

## Use

{use_steps}
   - `instruction-only`: use this Skill as OpenCode guidance plus explicit preflight guards.
   - `tool-contract`: report the named `tool_contract`, run any `tool_contract_check`, and obey `runtime_surface` / `fallback` before claiming full support.
   - `unsupported`: stop or use the reported `fallback`.

## Shape

- Identifier: `{identifier}`
- Invocation class: `{invocation_class}`
- Supported modes: `{modes}`
- Argument shape: `{argument_shape}`
- Portable meaning: {meaning}
{portable_contract}

## Required Guards

- Before edits: `adapters/opencode/bin/preflight.sh write <file> [session-id]`
{route_guard}
- Before spec-changing work: `adapters/opencode/bin/preflight.sh capability {identifier} [cwd] [session-id]`
- After actually reading a spec PRD: `adapters/opencode/bin/preflight.sh read <prd.md> [session-id]`

Do not use legacy compatibility Skill files or non-native adapter Skill files
as OpenCode-native source. Those files are compatibility/reference surfaces only.
"""
    return identifier, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projections")
    args = parser.parse_args()

    manifest = harness_manifest.load()
    expected: dict[Path, str] = {}
    for identifier, spec in manifest["capabilities"].items():
        capability_file = CAPABILITIES / f"{identifier}.md"
        identifier, body = render(identifier, spec, capability_file)
        expected[OUT / identifier / "SKILL.md"] = body

    stale: list[str] = []
    for path, body in expected.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    existing = sorted(OUT.glob("*/SKILL.md")) if OUT.exists() else []
    extras = [path for path in existing if path not in expected]
    if args.check:
        stale.extend(str(path.relative_to(ROOT)) for path in extras)
    else:
        for path in extras:
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass

    if stale:
        print("OpenCode native skill projections are stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1

    if not args.check:
        print(f"generated {len(expected)} OpenCode native skill projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
