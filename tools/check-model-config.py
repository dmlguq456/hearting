#!/usr/bin/env python3
"""Fail-closed guard: concrete model IDs must live only in adapters/*/config/models.conf.

Each adapter declares its concrete model names and default efforts in a single
config source of truth (core/ADAPTATION.md §3, roles/README Adapter Requirements).
Every other adapter/core/roles/tools surface must derive from that config. A
concrete model ID appearing outside the config (and generated regions) is a
violation. Display-only model-family palettes and test fixtures are exempt.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Surfaces that are scanned for stray concrete model IDs.
# `tools` is here because leaving it out is what actually hid the Fleet title worker's
# `haiku` pin for a month: the blanket `/tools/fleet/` entry below reads like the reason,
# but the canonical copy was never scanned at all — only its Claude mirror was.
# `utilities` and `hooks` joined the list on 2026-09-09: they hold the model-selection
# consumers themselves (the profile resolver, the capacity cascade, the native-subagent
# admission hook), which is the last place a stray pin should be able to hide. Both were
# already clean when they were added, so this widened the guard without a single waiver.
SCAN_DIRS = ["adapters", "core", "roles", "tools", "utilities", "hooks"]

# The single sources of truth (allowed to contain concrete model IDs).
CONFIG_SUFFIX = "config/models.conf"

# Whole-file exemptions (generated projections, display palettes, tests, fixtures).
EXEMPT_SUBSTRINGS = (
    "/config/models.conf",              # the SoT itself
    "/agents/memory-scout.md",          # kernel helper (claude flat layout)
    "/agents/memory-scout.toml",        # kernel helper (codex layout)
    "/agents/memory-scout/",            # kernel helper (opencode dir layout); exact, no -evil lookalikes
    # Native subagent type catalog — GENERATED from each adapter's models.conf
    # by the three sync-native-agents.py generators and verified by generate.py
    # --check, so every model pin below is derived, not stray. The trailing "."
    # / "/" keeps lookalikes (light-evil.md) out. Covers the claude .md files,
    # their plugin-marketplace copies, the codex .toml files, and the opencode
    # dir layout. Together with the memory-scout kernel helper above these are
    # the ONLY agents exemptions.
    "/agents/general-purpose.",
    "/agents/general-purpose/",
    "/agents/balanced.",
    "/agents/balanced/",
    "/agents/light.",
    "/agents/light/",
    "/agents/deep.",
    "/agents/deep/",
    "/node_modules/",                   # vendored third-party code, not a harness surface
    "/roles/units/",                    # unit catalog owned by check-unit-config.py
    "/statusline.sh",                   # model-family -> color palette (display only)
    # Fleet: display palettes and fixtures only. Deliberately NOT the whole tree —
    # a blanket `/tools/fleet/` exemption is what let `refresh_title.py` pin a concrete
    # model as its title-worker default and keep it for a month after the config SoT
    # landed. Everything else under tools/fleet/ is scanned like any other surface, so a
    # new execution pin there fails the guard instead of hiding behind "display only".
    "/tools/fleet/render.py",           # model-family -> color palette
    "/tools/fleet/render_cells.py",     # the same palette, split out of render.py
    "/tools/fleet/demo.py",             # synthetic fixture rows
    "/tools/fleet/projection.py",       # display projection of collected values
    "/tools/fleet/collectors/claude.py",  # /api/oauth/usage bucket keys + their labels
    "/tools/render-fleet-svg.py",       # static SVG mockup of the dashboard
    # INERT today: `loops` is not in SCAN_DIRS, so this entry waives nothing. Kept as the
    # waiver that would be needed if `loops` is ever scanned — but do not read this list as
    # an inventory of what IS scanned. SCAN_DIRS above is that inventory; capabilities/,
    # skills/, scaffolds/, loops/, docs/, the three *_setting/ trees and every top-level
    # file are unscanned (surveyed 2026-09-09: only docs/fleet.svg, a generated mockup,
    # would match).
    "/loops/drill/",                    # drill fixtures pin explicit models on purpose
    ".test.",                           # test fixtures
    "/tests/",
    "/utilities/fixtures/",             # read-only captured registry rows; the header already declares fixtures exempt
    "check-model-config.py",            # this guard's own doc/patterns
    "check-unit-config.py",             # sibling guard's mirrored patterns (incl. adapter projection symlink)
    "check-adaptation-boundary.sh",     # boundary guard references models in comments/tests
)

# Concrete model-ID patterns (unambiguous identifiers + alias-as-model-value).
PATTERNS = [
    re.compile(r"\bgpt-\d+\.\d"),                               # codex versioned ids: gpt-6.1-sol, gpt-5.4-mini, ...
    # codex: gpt-6-astra and any later gpt-<major>-<name>.
    re.compile(r"claude-(?:opus|sonnet|haiku|fable)-\d"),       # claude versioned full ids
    re.compile(r"(?<![\w./-])openai/gpt-\d+(?:\.\d+)?-[a-z]"), # OpenAI provider/model reference
    re.compile(r"\bgpt-\d+-[a-z]"),                             # Codex unversioned full ids
    re.compile(r"opencode-go/[a-z0-9]"),                        # opencode-go provider model-id
    re.compile(r"(?<![\w./@-])opencode/[a-z0-9][a-z0-9.]*-[a-z0-9][a-z0-9.-]*(?![\w./-])"),
    re.compile(r"\bglm-\d"),                                    # opencode glm-5.2
    re.compile(r"\bdeepseek-v\d"),                              # opencode deepseek-v4-*
    # Claude short alias used as a concrete model VALUE (frontmatter / :- default / assignment).
    re.compile(r"model:\s*(?:opus|sonnet|haiku|fable)\b"),
    re.compile(r":-\s*(?:opus|sonnet|haiku|fable)\b"),
    re.compile(r"=\s*[\"']?(?:opus|sonnet|haiku|fable)[\"']?\s*(?:;|$)"),
    # A bare quoted alias anywhere in code. The three patterns above all anchor on an
    # assignment shape, so none of them saw `os.environ.get("FLEET_TITLE_MODEL", "haiku")`
    # — the alias sat in a call argument, which is exactly where a default belongs and
    # exactly what the guard was blind to. Display/label surfaces that legitimately hold
    # these words are named in EXEMPT_SUBSTRINGS rather than carved out of the pattern.
    re.compile(r"[\"'](?:opus|sonnet|haiku|fable)[\"']"),
]

GEN_OPEN = re.compile(r"<!--\s*GENERATED")
GEN_CLOSE = re.compile(r"<!--\s*END GENERATED")


def is_exempt(path: Path) -> bool:
    p = str(path).replace(str(ROOT), "")
    return any(s in p for s in EXEMPT_SUBSTRINGS)


def scan_file(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    in_generated = False
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return hits
    for i, line in enumerate(text.splitlines(), 1):
        if GEN_OPEN.search(line):
            in_generated = True
        if GEN_CLOSE.search(line):
            in_generated = False
            continue
        if in_generated:
            continue
        for pat in PATTERNS:
            if pat.search(line):
                hits.append((i, line.strip()[:120]))
                break
    return hits


def main() -> int:
    violations: list[str] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or is_exempt(path):
                continue
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".db", ".lock"}:
                continue
            for lineno, snippet in scan_file(path):
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {snippet}")

    if violations:
        print("check-model-config: concrete model IDs found outside config/models.conf:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(f"\n{len(violations)} violation(s). Move the model ID into the adapter's "
              f"config/models.conf and derive the surface from it.", file=sys.stderr)
        return 1
    print("check-model-config: OK — no concrete model IDs outside config/models.conf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
