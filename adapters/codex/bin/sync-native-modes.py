#!/usr/bin/env python3
"""Generate Codex-native mode realization guides from portable mode fragments."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MODES = ROOT / "roles" / "units"
OUT = ROOT / "adapters" / "codex" / "modes"
MODE_MAP = ROOT / "adapters" / "codex" / "bin" / "mode-map.sh"
sys.path.insert(0, str(ROOT / "tools"))

import harness_manifest


def mode_metadata(mode: str) -> dict[str, str]:
    result = subprocess.run(
        [str(MODE_MAP), mode],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr or f"mode-map failed for {mode}")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return fields


def title_for(mode: str) -> str:
    family, name = mode.split("/", 1)
    return f"Codex {family.title()} {name.replace('-', ' ').title()} Mode"


def sanitize_portable_contract(text: str) -> str:
    """Project portable mode text without leaking non-Codex runtime surfaces."""
    text = re.sub(r"`?mcp__design__[A-Za-z_]+(?:\([^`]*\))?`?", "`adapters/codex/bin/preflight.sh visual-harness <file.html>`", text)
    text = re.sub(r"`?mcp__design__\*`?", "`adapters/codex/bin/preflight.sh visual-harness <file.html>`", text)
    text = re.sub(r"`?node <agent-home>/tools/design-mcp/console-check\.mjs <file(?:\.html)?>`?", "`adapters/codex/bin/preflight.sh visual-harness <file.html>`", text)
    text = text.replace("<agent-home>/tools/design-mcp/", "adapters/codex/bin/preflight.sh visual-harness ")
    # Unit-catalog design bodies carry the mined MCP environment table (재홈 2026-07-22):
    # neutralize the remaining Claude-surface tool tokens for the Codex projection.
    text = text.replace("`<agent-home>/tools/design-mcp`", "the Codex visual harness (`adapters/codex/bin/preflight.sh visual-harness`)")
    text = text.replace("tools/design-mcp/console-check.mjs", "adapters/codex/bin/preflight.sh visual-harness")
    text = text.replace("tools/design-mcp", "the Codex visual harness")

    replacements = {
        "<agent-home>/agent-modes/": "roles/units/",
        "agent-modes/": "roles/units/",
        "Design MCP": "Codex visual harness",
        "`node <agent-home>/tools/design-mcp/console-check.mjs <file.html>` 또는 adapter equivalent": "`adapters/codex/bin/preflight.sh visual-harness <file.html>`",
        "`node <agent-home>/tools/design-mcp/console-check.mjs <file>` 또는 adapter equivalent": "`adapters/codex/bin/preflight.sh visual-harness <file.html>`",
        "MCP 있으면 `getConsoleLogs` 동등": "console/page-error evidence is reported by the Codex visual harness",
        "MCP 있으면": "rendered evidence로",
        "MCP 불필요": "adapter wrapper only",
        "MCP-free": "visual-harness",
        "MCP·브라우저": "visual-harness/browser",
        "`eval_js`": "`adapters/codex/bin/preflight.sh visual-harness <file.html>`",
        "eval_js": "visual-harness DOM evidence",
        "`preview({ path })`": "`adapters/codex/bin/preflight.sh visual-harness <file.html>`",
        "`getConsoleLogs`": "`adapters/codex/bin/preflight.sh visual-harness <file.html>`",
        "getConsoleLogs": "visual-harness console report",
        "`screenshot → view_image`": "visual-harness screenshot inspection",
        "screenshot → view_image": "visual-harness screenshot inspection",
        "`screenshot({ savePath, steps })` 로 캡처 후 `view_image({ path })` (또는 Read) 로": "`adapters/codex/bin/preflight.sh visual-harness <file.html>` 로 캡처하고",
        "`view_image`": "screenshot inspection",
        "view_image": "screenshot inspection",
        "Claude Code 내장": "legacy runtime reference",
        "Claude Design": "legacy design guidance",
        "<nas-archive>/claude-meta-spec/reverse_engineering/deep-research.md": "legacy reverse-engineering notes",
        "<nas-archive>/claude-meta-spec/reverse_engineering/security-review.md": "legacy reverse-engineering notes",
        "`skills/draft-strategy/SKILL.md`": "`capabilities/draft-strategy.md`",
        "`adapters/<runtime>/skills/*`": "adapter skill projections",
        "`adapters/<runtime>/agents/*`": "adapter agent projections",
        "adapters/<runtime>/skills/*": "adapter skill projections",
        "adapters/<runtime>/agents/*": "adapter agent projections",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.rstrip()


def render(mode_file: Path) -> tuple[Path, str]:
    rel = mode_file.relative_to(MODES)
    mode = rel.with_suffix("").as_posix()
    fields = mode_metadata(mode)
    source = f"roles/units/{mode}.md"
    status = fields.get("status", "unsupported")
    realization = fields.get("realization", "compat-reference")
    requirement = fields.get("requirement", "")
    note = fields.get("note", "")
    native_mode_path = fields.get("native_mode_path", f"adapters/codex/modes/{mode}.md")

    contract_lines = [
        f"- Status: `{status}`",
        f"- Realization: `{realization}`",
    ]
    for key in ("tool_contract", "tool_contract_check", "report_tool_contract_check", "runtime_surface", "fallback"):
        if fields.get(key):
            contract_lines.append(f"- {key.replace('_', ' ').title()}: `{fields[key]}`")
    if requirement:
        contract_lines.append(f"- Requirement: {requirement}")
    if note:
        contract_lines.append(f"- Note: {note}")

    portable_contract = sanitize_portable_contract(mode_file.read_text(encoding="utf-8"))
    body = f"""# {title_for(mode)}

This is a Codex-native realization guide generated from the portable mode
inventory. It is adapter-owned output, not a legacy runtime mode copy.

## Source Order

1. Read `roles/MODES.md`.
2. Read `{source}` for the portable mode contract.
3. Run `adapters/codex/bin/preflight.sh mode-info {mode}`.
4. Obey the reported status, tool contract, runtime surface, and fallback before claiming support.

## Codex Runtime Mapping

{chr(10).join(contract_lines)}

## Use

- Use Codex file, terminal, approval, sandbox, hook, and skill surfaces.
- For `tool-contract` modes, run the named contract check before claiming the tool-backed result.
- If a required local provider or executable is unavailable, report the unavailable contract instead of silently downgrading.
- Treat `{native_mode_path}` as the adapter-owned mode guide for this runtime.

## Projected Portable Mode Contract

The following contract is projected from `{source}` with non-Codex runtime
surfaces rewritten to Codex-native preflight/tool-contract wording.

{portable_contract}
"""
    return OUT / rel, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated projections")
    args = parser.parse_args()

    manifest = harness_manifest.load()
    # unit-bearing modes only: internal law fragments (e.g. design/_design_rules) have no
    # standalone codex projection — unit bodies reference them by catalog path directly.
    mode_files = [
        MODES / f"{spec['unit']}.md"
        for spec in manifest["modes"].values()
        if isinstance(spec, dict) and spec.get("unit")
    ]
    expected = dict(render(path) for path in mode_files)

    stale: list[str] = []
    for path, body in expected.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    existing = sorted(OUT.glob("*/*.md")) if OUT.exists() else []
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
        print("Codex native mode projections are stale:", file=sys.stderr)
        for item in stale:
            print(f"  {item}", file=sys.stderr)
        return 1

    if not args.check:
        print(f"generated {len(expected)} Codex native mode projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
