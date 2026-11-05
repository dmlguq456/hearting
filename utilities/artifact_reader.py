#!/usr/bin/env python3
"""W7D read-side resolver for the artifact root after the write cutover.

Readers that used to open `<artifact-root>/<bucket>/...` directly resolve
their inputs here instead.  The canonical layout is:

- cycle output: `campaigns/<campaign-locator>/<cycle-locator>/artifacts/<bucket>/...`
- legacy cycle output: `campaigns/<camp-id>/cycles/<cycle-id>/artifacts/<bucket>/...`
- shared references: `shared/<kind>/<ref>/revisions/<rrev>/...` (latest
  revision per `reference.json`)
- legacy top-level buckets (`plans/`, `spec/`, ...): READ-ONLY fallback.
  They are write-denied while the cutover is active and hold only the
  entries the retirement gate excluded; a path that no longer exists there
  resolves through the compatibility maps (`artifact_cutover.resolve_legacy`).

This module never writes under the artifact root.
"""
from __future__ import annotations

from runtime_observation import input_path_call
import argparse
import contextvars
import fnmatch
import json
import os
import stat
import sys
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, str(input_path_call(Path(__file__), 'resolve').parent))
import artifact_cutover as C  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_resplit as RS  # noqa: E402
import artifact_relayout as RL  # noqa: E402
import artifact_residue as RES  # noqa: E402

LEGACY_BUCKETS = ("plans", "spec", "research", "documents", "analysis_project", "experiments", "designs")
SHARED_KIND_FOR_BUCKET = {"spec": "spec", "analysis_project": "analysis", "research": "research"}
LAYOUT_CYCLE = "cycle"
LAYOUT_SHARED = "shared"
LAYOUT_LEGACY = "legacy-readonly"
_FLAT_DESIGN_ANCHORS = frozenset({
    "01_refs", "02_tokens", "03_components", "04_review", "05_handoff",
})


def _is_real_dir(path: Path) -> bool:
    return input_path_call(path, 'is_dir') and not os.path.islink(str(path))


def is_flat_design_layout(path: Path) -> bool:
    """Whether a designs bucket itself is the cycle-local flat output root."""
    path = Path(path)
    try:
        return any(_is_real_dir(path / name) for name in _FLAT_DESIGN_ANCHORS)
    except OSError:
        return False


def _cycle_state(root: Path, campaign_dir: Path, cycle_dir: Path) -> str:
    return "sealed" if artifact_locator._exact_cycle_manifest(root, campaign_dir, cycle_dir) is not None else "open"


class _ReadScope:
    """Memo for ONE read pass (see `read_scope`); never outlives its `with` block."""

    __slots__ = ("scan_index", "cycle_buckets", "cache")

    def __init__(self, cache=None) -> None:
        self.cache = cache
        self.scan_index: Dict[str, Tuple[Dict[str, str], Dict[str, Dict[str, str]]]] = {}
        self.cycle_buckets: Dict[Tuple[str, str], List[Tuple[Path, Dict[str, str]]]] = {}


_READ_SCOPE: contextvars.ContextVar = contextvars.ContextVar("artifact_reader_read_scope", default=None)


def _record_stamp(root: Path):
    """Stat only locator inputs, never payload trees or generated INDEX contents.

    Directory stamps catch additions/removes/renames; file size/mtime/ctime and
    inode catch edits and atomic replacements. Bucket children and QA contents
    remain live reads, so their edits do not require an inventory rescan.
    """
    stamps = {}

    def stamp(path, observed=None):
        from runtime_observation import observe_input
        try:
            s = observed if observed is not None else input_path_call(path, 'lstat')
            observe_input(path)
            stamps[str(path)] = (s.st_dev, s.st_ino, s.st_mode, s.st_size,
                                 s.st_mtime_ns, s.st_ctime_ns)
            return stat.S_ISDIR(s.st_mode)
        except FileNotFoundError:
            observe_input(path)
            stamps[str(path)] = None
            return False

    def directory(path):
        if not stamp(path):
            return []
        dirs = []
        with os.scandir(path) as entries:
            for entry in entries:
                p = path / entry.name
                try:
                    s = input_path_call(entry, 'stat', follow_symlinks=False)
                except FileNotFoundError:
                    stamps[str(p)] = None
                    continue
                if stamp(p, s) and not entry.name.startswith("."):
                    dirs.append(p)
        return dirs

    stamp(root)
    for campaign in directory(root / "campaigns"):
        for child in directory(campaign):
            if child.name == "campaign.events":
                directory(child)
            elif child.name == "cycles":
                for cycle in directory(child):
                    directory(cycle)
            else:
                directory(child)
    directory(root / ".runtime" / "artifact-producer" / "v1" / "cycles")
    return stamps


class ReadCache:
    """Optional bounded, process-local reuse between observer ticks.

    Ordinary readers keep their existing fresh-pass semantics. A cached index
    is derived from records, with stat invalidation; INDEX deletion, artifact
    movement and a missing cache never become an approval or repair step.
    """

    def __init__(self):
        self.entries = OrderedDict()

    def scan(self, root):
        key = str(input_path_call(Path(root), 'resolve'))
        root = Path(key)
        try:
            before = _record_stamp(root)
        except OSError:
            self.entries.pop(key, None)
            return artifact_locator.scan_index(root)
        hit = self.entries.get(key)
        if hit is not None and hit[0] == before:
            self.entries.move_to_end(key)
            return hit[1]
        self.entries.pop(key, None)
        result = artifact_locator.scan_index(root)
        try:
            after = _record_stamp(root)
        except OSError:
            return result
        # A concurrent edit during the scan cannot be retained for a later tick.
        if before == after:
            self.entries[key] = (after, result)
            if len(self.entries) > 16:
                self.entries.popitem(last=False)
        return result


@contextmanager
def read_scope(cache=None):
    """Reuse locator scans inside one read pass.

    Fleet's projection asks `glob_bucket` once per entity x root: dozens of
    identical `artifact_locator.scan_index` walks of the same NFS tree in one
    collector tick (2026-09-08 audit: 80 walks, 23.6 s of a ~40 s tick against
    a 2 s refresh interval). Inside this scope the first scan of a root serves
    every later call of the same pass. The memo exists only on the context
    stack between entry and exit. Normally the next pass scans records again;
    observers may supply a ReadCache to reuse unchanged stat-checked records.
    Sealed identity comes from campaign/manifest/open-cycle records, never
    from a persisted cache or the rebuildable INDEX files.
    A nested scope joins the enclosing pass instead of starting a fresh memo.
    """
    if _READ_SCOPE.get() is not None:
        yield
        return
    token = _READ_SCOPE.set(_ReadScope(cache))
    try:
        yield
    finally:
        _READ_SCOPE.reset(token)


def _scan_index(root: Path) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    scope = _READ_SCOPE.get()
    if scope is None:
        return artifact_locator.scan_index(root)
    key = str(input_path_call(Path(root), 'resolve'))
    hit = scope.scan_index.get(key)
    if hit is None:
        hit = scope.scan_index[key] = (scope.cache.scan(root) if scope.cache is not None
                                      else artifact_locator.scan_index(root))
    return hit


def cycle_bucket_dirs(root: Path, bucket: str) -> List[Tuple[Path, Dict[str, str]]]:
    """Every recorded cycle's artifact bucket, across new and legacy layouts."""
    scope = _READ_SCOPE.get()
    if scope is None:
        return _cycle_bucket_dirs(root, bucket)
    root = Path(root)
    key = (str(input_path_call(root, 'resolve')), str(bucket))
    hit = scope.cycle_buckets.get(key)
    if hit is None:
        # Keyed by the physical root, stored relative to it: a second spelling of
        # the same directory (a symlink alias) gets paths under ITS spelling, as
        # the unmemoised call would return. Callers extend the returned list
        # (`bucket_dirs`) and read the metadata dicts, so the memo hands out copies.
        hit = scope.cycle_buckets[key] = [
            (path.relative_to(root), meta) for path, meta in _cycle_bucket_dirs(root, bucket)
        ]
    return [(root / relative, dict(meta)) for relative, meta in hit]


def _cycle_bucket_dirs(root: Path, bucket: str) -> List[Tuple[Path, Dict[str, str]]]:
    root = Path(root)
    out: List[Tuple[Path, Dict[str, str]]] = []
    # The rebuildable INDEX files are deliberately not consulted here. Stable
    # identity comes from campaign/manifest/open-cycle records on every scan;
    # locator basenames are display values and may be renamed after sealing.
    mapping, _rows = _scan_index(root)
    identities = {relative: identifier for identifier, relative in mapping.items()}
    for camp in artifact_locator.iter_campaign_dirs(root):
        campaign_rel = input_path_call(camp, 'resolve').relative_to(input_path_call(root, 'resolve')).as_posix()
        campaign_id = identities.get(campaign_rel)
        if campaign_id is None:
            continue
        for cyc, _physical_layout in artifact_locator.iter_cycle_dirs(camp):
            cycle_rel = input_path_call(cyc, 'resolve').relative_to(input_path_call(root, 'resolve')).as_posix()
            cycle_id = identities.get(cycle_rel)
            if cycle_id is None:
                continue
            target = cyc / "artifacts" / bucket
            if _is_real_dir(target):
                out.append((target, {"layout": LAYOUT_CYCLE, "campaign_id": campaign_id, "cycle_id": cycle_id,
                                     "cycle_state": _cycle_state(root, camp, cyc)}))
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
        if bucket == "designs" and is_flat_design_layout(base):
            # The producer cycle is already the outer boundary. Treat the
            # flat anchors as one design payload instead of five cycles.
            yield base, {**meta, "design_layout": "flat"}
            continue
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue  # ordinary removal/move after inventory observation
        for child in children:
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
    if _is_real_dir(legacy) and (input_path_call(legacy / "prd.md", 'is_file') or input_path_call(legacy / "pipeline_state.yaml", 'is_file')):
        return legacy, LAYOUT_LEGACY
    return None


def resolve_path(root: Path, rel: str) -> Dict[str, object]:
    """Resolve a legacy root-relative path: present, compat-mapped, or unresolved."""
    return C.resolve_legacy(Path(root), rel)


HOLD_EXIT = 65


def resplit_hold(root: Path) -> Optional[Dict[str, object]]:
    """D-77-a: thin re-export of `artifact_resplit.resplit_hold` for readers."""
    return RS.resplit_hold(Path(root))


def migration_hold(root: Path) -> Optional[Dict[str, object]]:
    """Any nonterminal migration journal: W7G resplit (D-77-a), W7I relayout
    (A-17.8) or W7H residue. Readers and gates treat all as typed `in-progress`."""
    return RES.migration_hold(Path(root))


def _emit(payload) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in {"bucket-dirs", "glob", "spec-dir", "resolve", "hold"}:
        explicit_root = None
        for index, value in enumerate(values):
            if value == "--artifact-root" and index + 1 < len(values):
                explicit_root = values[index + 1]
                break
            if value.startswith("--artifact-root="):
                explicit_root = value.split("=", 1)[1]
                break
        env_root = os.environ.get("AGENT_ARTIFACT_ROOT")
        if not explicit_root or (env_root and input_path_call(Path(explicit_root).expanduser(), 'resolve', strict=False)
                                 == input_path_call(Path(env_root).expanduser(), 'resolve', strict=False)):
            from artifact_context import resolve_context
            context = resolve_context(environ=os.environ,
                explicit={"artifact_root": explicit_root} if explicit_root else None)
            if context.route and context.artifact_root:
                if not explicit_root:
                    values[1:1] = ["--artifact-root", str(context.artifact_root)]
                if (values[0] == "spec-dir" and context.cycle_dir
                        and "--cycle-dir" not in values
                        and not any(value.startswith("--cycle-dir=") for value in values)):
                    position = values.index("--artifact-root") + 2 if "--artifact-root" in values else 1
                    values[position:position] = ["--cycle-dir", str(context.cycle_dir)]
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
    address = p.add_mutually_exclusive_group(required=True)
    address.add_argument("--path")
    address.add_argument("--cycle")
    address.add_argument("--campaign")
    p = sub.add_parser("hold", help="nonterminal resplit (D-77-a) or relayout (A-17.8) journal hold, if any")
    p.add_argument("--artifact-root", required=True)
    args = parser.parse_args(values)
    root = input_path_call(Path(args.artifact_root), 'resolve')
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
        hold = migration_hold(root)
        if hold is None:
            _emit({"hold": None})
            return 0
        _emit({"hold": hold})
        return HOLD_EXIT
    else:
        if args.path is not None:
            _emit(resolve_path(root, args.path))
        else:
            import artifact_locator
            _emit(artifact_locator.resolve_historical(root, args.cycle or args.campaign))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
