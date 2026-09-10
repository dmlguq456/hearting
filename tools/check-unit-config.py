#!/usr/bin/env python3
"""Fail-closed guard: the roles/units/** catalog honors the unit-def schema.

The unit catalog (roles/units/_schema.md) is the single declaration of each
dispatchable behavior atom. This guard enforces, stdlib-only:

1. roles/units/** contains no concrete model literals (portable roles only;
   concrete models resolve via each adapter's config/models.conf).
2. Unit frontmatter never declares a concrete `write_scope` (scope is NODE-owned
   in the topology registry, never unit-owned).
3. Every unit file carries the required frontmatter keys
   (unit/family/role/worker_type/floor/read_only/io) with schema-valid values,
   its `unit` id matches its path, and `stance` / `io.return` refs that look
   like paths resolve to existing files.
4. Consumer surfaces (repo-root skills/** and capabilities/*.md) no longer
   reference the retired persona paths `roles/modes/` and `agent-modes/`
   (adapters/claude/agent-modes/) — those re-homed into roles/units/.

Exit 1 on any violation; `-v` lists every scanned file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "roles" / "units"

# Surfaces that must not reference the retired persona paths. Consumer-facing .md
# trees only (2026-07-22 verify finding: adapter Skill projections, plugin mirrors,
# profiles, loops, and scaffolds escaped the original skills/+capabilities/ scan).
RETIRED_REF_DIRS = [
    "skills",
    "capabilities",
    "adapters/claude/skills",
    "adapters/codex/skills",
    "adapters/opencode/skills",
    "adapters/codex/modes",
    "adapters/claude/plugin-marketplace/plugins/hearting-claude/skills",
    "adapters/codex/plugins/hearting-codex/skills",
    "profiles",
    "loops",
    "scaffolds",
]
RETIRED_PATH_PATTERNS = [
    re.compile(r"roles/modes/"),
    re.compile(r"agent-modes/"),  # covers adapters/claude/agent-modes/ and bare refs
]

# Concrete model-ID patterns — mirrors tools/check-model-config.py.
MODEL_PATTERNS = [
    re.compile(r"gpt-5\.\d"),                                   # codex: gpt-5.6-sol, ...
    re.compile(r"claude-(?:opus|sonnet|haiku|fable)-\d"),       # claude versioned full ids
    re.compile(r"opencode-go/[a-z0-9]"),                        # opencode-go provider model-id
    re.compile(r"\bglm-\d"),                                    # opencode glm-5.2
    re.compile(r"\bdeepseek-v\d"),                              # opencode deepseek-v4-*
    re.compile(r"model:\s*(?:opus|sonnet|haiku|fable)\b"),
    re.compile(r":-\s*(?:opus|sonnet|haiku|fable)\b"),
    re.compile(r"=\s*[\"']?(?:opus|sonnet|haiku|fable)[\"']?\s*(?:;|$)"),
]

REQUIRED_KEYS = ("unit", "family", "role", "worker_type", "floor", "read_only", "io")
FORBIDDEN_KEYS = ("write_scope",)
WORKER_TYPES = {"owner", "stage", "review", "support", "frame"}
FLOORS = {"near-zero", "low", "moderate", "high", "highest"}
BOOLS = {"true", "false"}

TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")
NESTED_KEY = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")
INLINE_COMMENT = re.compile(r"\s+#.*$")


def clean_value(raw: str) -> str:
    return INLINE_COMMENT.sub("", raw).strip()


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def parse_frontmatter(text: str):
    """Return (fields, nested, fm_line_span) or None when no frontmatter block.

    fields: top-level key -> (value, lineno); nested: "parent.child" -> (value, lineno).
    Regex/line technique only — the dispatch hot path has no YAML dependency and
    neither does this guard.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, tuple[str, int]] = {}
    nested: dict[str, tuple[str, int]] = {}
    current_top = None
    for i, line in enumerate(lines[1:], 2):
        if line.strip() == "---":
            return fields, nested, (1, i)
        m = TOP_KEY.match(line)
        if m:
            current_top = m.group(1)
            fields[current_top] = (clean_value(m.group(2)), i)
            continue
        m = NESTED_KEY.match(line)
        if m and current_top:
            nested[f"{current_top}.{m.group(1)}"] = (clean_value(m.group(2)), i)
    return None  # unterminated block


def looks_like_path(value: str) -> bool:
    v = value.strip().strip("\"'")
    return "/" in v or v.endswith(".md")


def resolve_ref(unit_file: Path, value: str) -> bool:
    v = value.strip().strip("\"'")
    return (UNITS / v).is_file() or (unit_file.parent / v).is_file()


def check_unit_file(path: Path, violations: list[str]) -> None:
    """Frontmatter contract for one unit definition file."""
    text = path.read_text(encoding="utf-8")
    parsed = parse_frontmatter(text)
    if parsed is None:
        violations.append(f"{rel(path)}:1: missing or unterminated frontmatter block")
        return
    fields, nested, _ = parsed

    for key in REQUIRED_KEYS:
        if key not in fields:
            violations.append(f"{rel(path)}:1: required frontmatter key missing: {key}")
    for key in FORBIDDEN_KEYS:
        if key in fields:
            _, ln = fields[key]
            violations.append(
                f"{rel(path)}:{ln}: forbidden frontmatter key '{key}' "
                f"(write scope is node-owned, never unit-owned)")

    expected_id = f"{path.parent.name}/{path.stem}"
    if "unit" in fields:
        value, ln = fields["unit"]
        if value != expected_id:
            violations.append(
                f"{rel(path)}:{ln}: unit id '{value}' does not match path "
                f"(expected '{expected_id}')")
        if not re.fullmatch(r"[a-z-]+/[a-z-]+", value):
            violations.append(
                f"{rel(path)}:{ln}: unit id '{value}' violates ^[a-z-]+/[a-z-]+$")
    if "family" in fields:
        value, ln = fields["family"]
        if value != path.parent.name:
            violations.append(
                f"{rel(path)}:{ln}: family '{value}' does not match directory "
                f"'{path.parent.name}'")
    if "worker_type" in fields:
        value, ln = fields["worker_type"]
        if value not in WORKER_TYPES:
            violations.append(
                f"{rel(path)}:{ln}: worker_type '{value}' not in "
                f"{sorted(WORKER_TYPES)}")
    if "floor" in fields:
        value, ln = fields["floor"]
        if value not in FLOORS:
            violations.append(f"{rel(path)}:{ln}: floor '{value}' not in {sorted(FLOORS)}")
    if "read_only" in fields:
        value, ln = fields["read_only"]
        if value not in BOOLS:
            violations.append(f"{rel(path)}:{ln}: read_only '{value}' must be true or false")
    if "io" in fields:
        for sub in ("verdict", "return"):
            if f"io.{sub}" not in nested:
                violations.append(
                    f"{rel(path)}:{fields['io'][1]}: io.{sub} missing from io block")
    if "stance" in fields:
        value, ln = fields["stance"]
        if value != "none" and looks_like_path(value) and not resolve_ref(path, value):
            violations.append(
                f"{rel(path)}:{ln}: stance ref '{value}' does not resolve to a file")
    if "io.return" in nested:
        value, ln = nested["io.return"]
        if looks_like_path(value) and not resolve_ref(path, value):
            violations.append(
                f"{rel(path)}:{ln}: io.return ref '{value}' does not resolve to a file")


def scan_model_literals(path: Path, violations: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    for i, line in enumerate(text.splitlines(), 1):
        for pat in MODEL_PATTERNS:
            if pat.search(line):
                violations.append(
                    f"{rel(path)}:{i}: concrete model literal: {line.strip()[:120]}")
                break


def scan_retired_refs(path: Path, violations: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    for i, line in enumerate(text.splitlines(), 1):
        for pat in RETIRED_PATH_PATTERNS:
            if pat.search(line):
                violations.append(
                    f"{rel(path)}:{i}: retired persona path reference "
                    f"({pat.pattern.rstrip('/')}): {line.strip()[:120]}")
                break


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    violations: list[str] = []

    if not UNITS.is_dir():
        print(f"check-unit-config: unit catalog missing: {rel(UNITS)}", file=sys.stderr)
        return 1

    for path in sorted(UNITS.rglob("*")):
        if not path.is_file():
            continue
        if verbose:
            print(f"scan (catalog): {rel(path)}")
        # The model-literal scan covers every catalog file, including _NOTES.md
        # authoring residue (2026-07-22 verify finding): describe legacy models in
        # words there instead of quoting concrete IDs. _NOTES.md stays exempt from
        # the frontmatter/schema contract below — it is not a unit.
        scan_model_literals(path, violations)
        # Frontmatter contract applies to unit definition files only:
        # <family>/<unit>.md where neither segment is an underscore-prefixed
        # helper (_schema.md, _shared/*, _NOTES.md, _voice.md, ...).
        if (path.suffix == ".md" and path.parent != UNITS
                and not path.name.startswith("_")
                and not path.parent.name.startswith("_")):
            check_unit_file(path, violations)

    for d in RETIRED_REF_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            if not path.is_file():
                continue
            sp = str(path)
            if "/node_modules/" in sp or ".test." in path.name:
                continue
            if d == "capabilities" and path.parent != base:
                continue  # contract scans capabilities/*.md only
            if verbose:
                print(f"scan (refs): {rel(path)}")
            scan_retired_refs(path, violations)

    if violations:
        print("check-unit-config: unit-catalog contract violations:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(f"\n{len(violations)} violation(s). See roles/units/_schema.md for the "
              f"authoring contract; persona refs must point at roles/units/.",
              file=sys.stderr)
        return 1
    print("check-unit-config: OK — unit catalog and persona references are clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
