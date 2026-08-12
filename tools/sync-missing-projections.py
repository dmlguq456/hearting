#!/usr/bin/env python3
"""Fill missing Claude adapter counterparts for mirrored source domains.

Root-cause automation (2026-07-22): the "new file in a mirrored domain →
forgotten adapters/claude counterpart → boundary guard fails on CI after push"
class recurred (loops/, scaffolds/, and now tools/memory test files). This tool
makes the counterpart DERIVED instead of remembered:

- ``--check``: report every missing counterpart and exit 1 (wired into
  tools/generate.py, so the standard local ``generate.py --check`` battery
  catches the gap BEFORE push).
- write mode: create only counterparts that do not exist — intentional
  adapter-owned divergence is never overwritten.

Domains (mirroring tools/check-adaptation-boundary.sh expectations):
- ``loops/``      → copy into  adapters/claude/loops/
- ``scaffolds/``  → copy into  adapters/claude/scaffolds/
- ``tools/memory``→ symlink    adapters/claude/tools/memory/<f>
                    → ../../../../tools/memory/<f> (established projection style)
- ``tools/install``→ symlink   adapters/claude/tools/install/<f>
                    → ../../../../tools/install/<f>
- ``tools/integrations``→ symlink adapters/claude/tools/integrations/<f>
                    (relative link depth follows the file's nesting)

``utilities/`` needs no counterpart automation: `adapters/claude/utilities` is a
whole-layer symlink to the shared portable layer (2026-07-22 collapse), so every
utility — including a newly added one — resolves for Claude with zero per-file
mirror work. `check_claude_utility_projection` enforces that collapse.

Detection ownership stays with check-adaptation-boundary.sh; this is the
matching prevention/recovery command.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def gitignored(path: Path) -> bool:
    return subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def walk(domain: Path):
    for path in sorted((ROOT / domain).rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(ROOT / domain).parts):
            continue
        if gitignored(path):
            continue
        yield path


def sync(check: bool) -> int:
    missing: list[str] = []
    created = 0

    def counterpart_copy(domain: str) -> int:
        nonlocal created
        n = 0
        for src in walk(Path(domain)):
            rel = src.relative_to(ROOT / domain)
            dst = ROOT / "adapters" / "claude" / domain / rel
            if dst.exists() or dst.is_symlink():
                continue
            if check:
                missing.append(str(dst.relative_to(ROOT)))
                continue
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            else:
                continue
            print(f"created {dst.relative_to(ROOT)}")
            created += 1
            n += 1
        return n

    def counterpart_symlink(domain: str) -> int:
        nonlocal created
        n = 0
        for src in walk(Path(domain)):
            if not src.is_file():
                continue
            rel = src.relative_to(ROOT / domain)
            dst = ROOT / "adapters" / "claude" / domain / rel
            if dst.exists() or dst.is_symlink():
                continue
            if check:
                missing.append(str(dst.relative_to(ROOT)))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Depth-aware relative link: flat files keep the established
            # ../../../../<domain>/<f> shape and nested files gain the extra hops.
            os.symlink(os.path.relpath(src, dst.parent), dst)
            print(f"linked {dst.relative_to(ROOT)}")
            created += 1
            n += 1
        return n

    counterpart_copy("loops")
    counterpart_copy("scaffolds")
    counterpart_symlink("tools/memory")
    counterpart_symlink("tools/install")
    counterpart_symlink("tools/integrations")

    if check:
        if missing:
            print("missing Claude counterparts (run tools/sync-missing-projections.py to fill):",
                  file=sys.stderr)
            for item in missing:
                print(f"  {item}", file=sys.stderr)
            return 1
        print("all Claude mirrored-domain counterparts present")
        return 0
    if created == 0:
        print("all Claude mirrored-domain counterparts already present")
    else:
        print(f"filled {created} missing counterpart(s); review and commit them with the source change")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report missing counterparts, create nothing")
    args = parser.parse_args()
    return sync(args.check)


if __name__ == "__main__":
    raise SystemExit(main())
