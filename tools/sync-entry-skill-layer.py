#!/usr/bin/env python3
"""Generate compact entry routers and Claude mirrors from canonical Skills.

The first write captures the existing canonical procedure in a one-level owner
reference.  Later runs preserve that reference and deterministically project
the compact router and its reference tree to Claude.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import harness_manifest


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        raise harness_manifest.ManifestError("Skill lacks frontmatter")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise harness_manifest.ManifestError("Skill frontmatter is unterminated")
    return "---" + parts[1] + "---", parts[2].lstrip("\n")


def router(identifier: str) -> str:
    return f'''# {identifier}

This is a compact pre-approval entry router. Its manifest-owned frontmatter is
the authoritative discovery metadata; it intentionally contains no execution
procedure.

## Pre-approval boundary

Use only this router, `harness-manifest.json`, and `core/WORKFLOW.md §0.2` to
propose a route. Present the one-time confirmation in `core/WORKFLOW.md §0.4`
unless the same route and scope are already approved. Do not load references
before approval.

## Post-approval owner contract

After approval, direct/quick sessions and the `standard+` dispatch-depth-1 owner load
`capabilities/{identifier}.md`, then use the Reference Index below. Assigned
stage workers load only their assigned stage contracts.

## Reference Index

| File | Load when | Obligation |
|---|---|---|
| [`references/owner-execution.md`](references/owner-execution.md) | After approval, by the selected direct/quick session or dispatch-depth-1 owner | Read the complete execution procedure before material work. This is the router's only post-approval reference edge. |

## Guard pointer

Follow the portable artifact, worktree, role, and verification guards in the
selected owner contract. Before the first durable capability artifact, compile
and bind the checked route even for direct intensity; direct is a compiled
inline node, not route absence. A restriction on native subagents/agents is
surface-local and never selects direct execution. Runtime projections must
report unsupported mechanics and must not claim physical instruction masking,
token, billing, or cost savings without verified evidence.
'''


def relocate_owner_links(text: str) -> str:
    """Adjust paths copied from ``skills/<entry>/SKILL.md`` into references.

    Owner content moves down exactly one directory.  Keep its existing
    one-level reference tree: sibling references lose ``references/`` while
    portable-root links use the runtime-stable ``<agent-home>`` form and
    sibling-Skill links gain one parent traversal.
    """
    text = re.sub(r"\]\(references/", "](", text)
    text = re.sub(r"`references/([^`]+)`", r"`\1`", text)
    text = re.sub(
        r"conventions/(common|doc|paper|presentation)\.md",
        r"convention-\1.md",
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(<agent-home>/(core|roles|capabilities)/([^)]+)\)",
        r"\1 (`<agent-home>/\2/\3`)",
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(\.\./(?:\.\./)?(?:\.\./)?(core|roles|capabilities)/([^)]+)\)",
        r"\1 (`<agent-home>/\2/\3`)",
        text,
    )
    return re.sub(r"\]\(\.\./(?!\.\./)", "](../../", text)


def expected(manifest: dict) -> dict[Path, str]:
    out: dict[Path, str] = {}
    for identifier, spec in manifest["capabilities"].items():
        if spec["invocation"]["class"] != "entry-router":
            continue
        skill = ROOT / "skills" / identifier / "SKILL.md"
        front, body = split_frontmatter(skill.read_text(encoding="utf-8"))
        owner = skill.parent / "references" / "owner-execution.md"
        if not owner.exists():
            out[owner] = relocate_owner_links(body)
        else:
            out[owner] = relocate_owner_links(owner.read_text(encoding="utf-8"))
        out[skill] = front + "\n\n" + router(identifier)
    return out


def copy_tree(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    manifest = harness_manifest.load()
    files = expected(manifest)
    stale = [path for path, text in files.items() if not path.exists() or path.read_text(encoding="utf-8") != text]
    entries = [name for name, spec in manifest["capabilities"].items() if spec["invocation"]["class"] == "entry-router"]
    # Mirror EVERY capability tree, not only entry routers: sub-skill projections went
    # stale during the 2026-07-22 재홈 because only the 13 entry trees were owned here.
    mirrors = [name for name in manifest["capabilities"] if (ROOT / "skills" / name).is_dir()]
    for identifier in mirrors:
        source = ROOT / "skills" / identifier
        dest = ROOT / "adapters" / "claude" / "skills" / identifier
        source_files = {path.relative_to(source) for path in source.rglob("*") if path.is_file()}
        dest_files = {path.relative_to(dest) for path in dest.rglob("*") if path.is_file()} if dest.exists() else set()
        if source_files != dest_files or any(
            path.read_bytes() != (dest / path.relative_to(source)).read_bytes()
            for path in source.rglob("*") if path.is_file()
        ):
            stale.append(dest)
    if args.check:
        if stale:
            print("entry Skill layer is stale:", file=sys.stderr)
            for path in sorted(set(stale)):
                print(f"  {path.relative_to(ROOT)}", file=sys.stderr)
            return 1
        return 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for identifier in mirrors:
        copy_tree(ROOT / "skills" / identifier, ROOT / "adapters" / "claude" / "skills" / identifier)
    print(f"generated compact routers and owner references for {len(entries)} entry Skills; mirrored {len(mirrors)} Skill trees")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
