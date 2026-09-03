#!/usr/bin/env python3
"""W7D read-side resolver for the artifact root after the write cutover.

Readers that used to open `<artifact-root>/<bucket>/...` directly resolve
their inputs here instead.  The canonical layout is:

- cycle output: `campaigns/<camp>/cycles/<cyc>/artifacts/<bucket>/...`
- shared references: `shared/<kind>/<ref>/revisions/<rrev>/...` (latest
  revision per `reference.json`)
- legacy top-level buckets (`plans/`, `spec/`, ...): READ-ONLY fallback.
  They are write-denied while the cutover is active and hold only the
  entries the retirement gate excluded; a path that no longer exists there
  resolves through the compatibility maps (`artifact_cutover.resolve_legacy`).

This module never writes under the artifact root.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_cutover as C  # noqa: E402
import artifact_resplit as RS  # noqa: E402

LEGACY_BUCKETS = ("plans", "spec", "research", "documents", "analysis_project", "experiments", "designs")
SHARED_KIND_FOR_BUCKET = {"spec": "spec", "analysis_project": "analysis", "research": "research"}
LAYOUT_CYCLE = "cycle"
LAYOUT_SHARED = "shared"
LAYOUT_LEGACY = "legacy-readonly"


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not os.path.islink(str(path))


def _cycle_state(cycle_dir: Path) -> str:
    return "sealed" if (cycle_dir / "manifest.json").is_file() else "open"


def cycle_bucket_dirs(root: Path, bucket: str) -> List[Tuple[Path, Dict[str, str]]]:
    """Every `campaigns/*/cycles/*/artifacts/<bucket>` directory, sorted by path."""
    root = Path(root)
    out: List[Tuple[Path, Dict[str, str]]] = []
    campaigns = root / "campaigns"
    if not _is_real_dir(campaigns):
        return out
    for camp in sorted(campaigns.iterdir()):
        cycles = camp / "cycles"
        if not _is_real_dir(cycles):
            continue
        for cyc in sorted(cycles.iterdir()):
            target = cyc / "artifacts" / bucket
            if _is_real_dir(target):
                out.append((target, {"layout": LAYOUT_CYCLE, "campaign_id": camp.name, "cycle_id": cyc.name,
                                     "cycle_state": _cycle_state(cyc)}))
    return out


def latest_shared_dir(root: Path, kind: str) -> Optional[Path]:
    return C.latest_shared_revision(Path(root), kind)


def legacy_bucket_dir(root: Path, bucket: str) -> Optional[Path]:
    """The legacy top-level bucket if it still exists (read-only fallback)."""
    path = Path(root) / bucket
    return path if _is_real_dir(path) else None


def bucket_dirs(root: Path, bucket: str, *, include_shared: bool = True,
                include_legacy: bool = True) -> List[Tuple[Path, Dict[str, str]]]:
    """Ordered read candidates for one bucket: cycle dirs, latest shared revision, legacy fallback."""
    rows = cycle_bucket_dirs(root, bucket)
    if include_shared and bucket in SHARED_KIND_FOR_BUCKET:
        shared = latest_shared_dir(root, SHARED_KIND_FOR_BUCKET[bucket])
        if shared is not None:
            rows.append((shared, {"layout": LAYOUT_SHARED, "kind": SHARED_KIND_FOR_BUCKET[bucket],
                                  "revision_id": shared.name}))
    if include_legacy:
        legacy = legacy_bucket_dir(root, bucket)
        if legacy is not None:
            rows.append((legacy, {"layout": LAYOUT_LEGACY}))
    return rows


def iter_bucket_children(root: Path, bucket: str, **kw) -> Iterator[Tuple[Path, Dict[str, str]]]:
    """Direct child directories of every bucket candidate (a cycle/component each)."""
    for base, meta in bucket_dirs(root, bucket, **kw):
        for child in sorted(base.iterdir()):
            if _is_real_dir(child) and not child.name.startswith("."):
                yield child, meta


def glob_bucket(root: Path, bucket: str, pattern: str, **kw) -> List[Path]:
    """`<bucket>/<pattern>` across every layout (replacement for `glob(root/bucket/pattern)`)."""
    return [child for child, _ in iter_bucket_children(root, bucket, **kw) if fnmatch.fnmatch(child.name, pattern)]


def spec_dir(root: Path, *, open_cycle_dir: Optional[str] = None) -> Optional[Tuple[Path, str]]:
    """The spec tree a reader should consult.

    Order: the open producer cycle handed in (a writer's own in-progress tree),
    the latest shared/spec revision (canonical), then the legacy `spec/` only
    when it still carries `prd.md` or `pipeline_state.yaml`.
    """
    root = Path(root)
    hint = open_cycle_dir or os.environ.get("AGENT_ARTIFACT_CYCLE_DIR")
    if hint:
        candidate = Path(hint) / "artifacts" / "spec"
        if _is_real_dir(candidate):
            return candidate, LAYOUT_CYCLE
    shared = latest_shared_dir(root, "spec")
    if shared is not None:
        return shared, LAYOUT_SHARED
    legacy = root / "spec"
    if _is_real_dir(legacy) and ((legacy / "prd.md").is_file() or (legacy / "pipeline_state.yaml").is_file()):
        return legacy, LAYOUT_LEGACY
    return None


def resolve_path(root: Path, rel: str) -> Dict[str, object]:
    """Resolve a legacy root-relative path: present, compat-mapped, or unresolved."""
    return C.resolve_legacy(Path(root), rel)


HOLD_EXIT = 65


def resplit_hold(root: Path) -> Optional[Dict[str, object]]:
    """D-77-a: thin re-export of `artifact_resplit.resplit_hold` for readers."""
    return RS.resplit_hold(Path(root))


def _emit(payload) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("bucket-dirs", help="ordered read candidates for a bucket")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--bucket", required=True, choices=LEGACY_BUCKETS)
    p.add_argument("--no-legacy", action="store_true")
    p = sub.add_parser("glob", help="child directories matching a pattern across layouts")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--bucket", required=True, choices=LEGACY_BUCKETS)
    p.add_argument("--pattern", required=True)
    p = sub.add_parser("spec-dir", help="the spec tree to read (cycle > shared > legacy)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle-dir")
    p = sub.add_parser("resolve", help="resolve a legacy root-relative path")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--path", required=True)
    p = sub.add_parser("hold", help="nonterminal resplit journal hold, if any (D-77-a)")
    p.add_argument("--artifact-root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.artifact_root).resolve()
    if args.command == "bucket-dirs":
        _emit([{"path": str(p), **m} for p, m in bucket_dirs(root, args.bucket, include_legacy=not args.no_legacy)])
    elif args.command == "glob":
        _emit([str(p) for p in glob_bucket(root, args.bucket, args.pattern)])
    elif args.command == "spec-dir":
        found = spec_dir(root, open_cycle_dir=args.cycle_dir)
        if found is None:
            _emit({"path": None, "layout": None})
            return 1
        _emit({"path": str(found[0]), "layout": found[1]})
    elif args.command == "hold":
        hold = resplit_hold(root)
        if hold is None:
            _emit({"hold": None})
            return 0
        _emit({"hold": hold})
        return HOLD_EXIT
    else:
        _emit(resolve_path(root, args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
