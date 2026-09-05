#!/usr/bin/env python3
"""W7H: dispose of the legacy top-level residue that W7C/W7G never migrated.

After W7C (write cutover), W7G (lump resplit + retire) and W7I (readable
relayout) every fleet root still carries files directly under its artifact root
that no contract owned: stage internals left behind by migrated cycles,
project materials, pre-`.runtime` route containers, sealed W7 evidence, and a
little runtime trash. `fleet_cutover_gate` never asked whether the top level
was *empty*; this module makes that a typed, journaled question.

Shapes and dispositions (D-23 table + this module's extension, reported as
spec-impact where D-23 has no row):

  support-residue    `<bucket>/<d1>/{_internal,evidence,RELOCATED-*}` whose
                     `<bucket>/<d1>` resolves through the compat chain to a
                     migrated cycle -> one sealed *residue cycle per origin
                     cycle* in the origin's campaign, files under
                     `artifacts/_internal/<bucket>/<d1>/...` with manifest role
                     `support`, record `residue_of` citing the origin (the W7G
                     "separate sealed record cites the original" pattern;
                     sealed manifests are never rewritten, D-6/D-11/D-71).
  root-support       top-level `_internal/`, `shards/`, `reviews/`, `dev_logs/`,
                     ... with no origin -> one residue cycle per root
                     (campaign `legacy-support-residue`, W7F precedent).
  bucket-cycle       `<bucket>/<d1>` (plans/research/experiments/documents/
                     designs) that never migrated -> one cycle per d1
                     (D-23 boundary), dated from the directory prefix.
  project-material   `papers/`, `refs/`, `_preserved/`, `rebuttal/`, `notes/`,
                     `analysis_project/`, top-level files, ... -> one
                     `documents` cycle per shape (per paper for `papers/<d1>`),
                     campaign `legacy-project-material`. D-23 has no row for
                     these shapes: spec-impact.
  route-container    `routes/`, `_routes/`, top-level `*route*.json` ->
                     journaled rename to `.runtime/routes/legacy/<rel>`.
  sealed-evidence    `SEALED_EVIDENCE_PATHS` -> one sealed evidence cycle per
                     declared path; a compat map file that moves gets its
                     `compat.json` `maps[].path` re-pointed (sha unchanged,
                     `relocated_from` recorded). D-82 preserves map rows
                     byte-for-byte, so the re-point is spec-impact.
  trash              `.gitkeep`, nested `.agent_reports|.claude_reports/.runtime`
                     -> deletion only through `retire-trash --approval`
                     (backup tar + inventory digest, W7F/D-84 pattern).
  deferred           `spec/` (consumers still read the legacy PRD: cairn's
                     nightly backfill, `spec-skill-gate` `find_prd`) and files
                     whose locator cannot be a D-6 locator -> typed, left in
                     place, counted in the gate as `deferred`.

Mechanics reuse W7G/W7I: same-filesystem rename only, per-operation inverse
rows, monotone journal under `.runtime/artifact-producer/v1/migrations/
<stamp>-residue/`, rollback before the `sealed` commit point and roll-forward
after it, D-82 compat append, typed `residue-in-progress` hold.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime
import hashlib
import json
import os
import re
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_cutover as C  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_manifest  # noqa: E402
import artifact_admission  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_relayout as RL  # noqa: E402
import artifact_resplit as RS  # noqa: E402

RESIDUE_ALGORITHM_VERSION = "w7h-residue/v1"
RUN_SUFFIX = "-residue"
TRASH_SUFFIX = "-residue-trash"
JOURNAL_NAME = "journal-residue.json"
INVERSE_NAME = "inverse.jsonl"
INVENTORY_NAME = "inventory.json"
INVENTORY_LOCATOR = "_internal/residue-inventory.json"
PLAN_NAME = "plan.json"
REPORT_NAME = "report.json"
MAP_NAME = "compatibility-map.jsonl"
STATE_NAME = "residue.json"
LOCK_NAME = "residue.lock"
INVERSE_SCHEMA = "artifact-residue-inverse-row/v1"
PLAN_KIND = "w7h-residue-plan"
REPORT_KIND = "w7h-residue-report"
TRASH_APPROVAL_KIND = "w7h-residue-trash-approval"
HOLD_EXIT = 65

PHASES = ("planned", "cycles-begun", "renaming", "renamed", "witnessed", "sealing", "sealed",
          "compat-reissued", "indexed", "complete")
ROLLBACK_PHASES = frozenset({"planned", "cycles-begun", "renaming", "renamed", "witnessed"})
ROLL_FORWARD_PHASES = frozenset({"sealing", "sealed", "compat-reissued", "indexed"})
TERMINAL_PHASES = frozenset({"complete", "rolled-back", "no-op"})

SUPPORT_TOPS = frozenset({"_internal", "shards", "reviews", "dev_logs", "test_logs", "evidence", "proposals",
                          "research-alternative", "spec-research-alternative"})
BUCKET_TOPS = frozenset(C.CYCLE_BUCKETS)  # plans documents designs research experiments
ROUTE_TOPS = frozenset({"routes", "_routes"})
MATERIAL_BUCKET = "documents"
DEFERRED_TOPS = frozenset({"spec"})
CAMPAIGN_KEYS = {
    "root-support": ("legacy-support-residue", "legacy support residue",
                     "support internals left at the artifact root by pre-cutover stages"),
    "project-material": ("legacy-project-material", "legacy project material",
                         "papers, references, deliverables and notes that predate the producer contract"),
    "sealed-evidence": ("legacy-sealed-evidence", "sealed W7 evidence",
                        "W7/W7C/W7F sealed evidence relocated out of the legacy top level"),
}
_ROUTEISH = re.compile(r"(route\.json|route\.outcome\.json|\.outcome\.json|dispatch-evidence\.json|owner-prompt\.md)$")
_DATE_PREFIX = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})[_-]")


class ResidueError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return C._now()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    return "sha256:" + C._sha(path)


def _rel(root: Path, path: Path) -> str:
    return os.path.relpath(str(path), str(root))


def _canonical_digest(body: Mapping[str, Any]) -> str:
    return RS._canonical_digest(dict(body))


def run_dirs(root: Path) -> List[Path]:
    mdir = C.migrations_dir(Path(root).resolve())
    if not mdir.is_dir():
        return []
    return sorted((p for p in mdir.iterdir() if p.is_dir() and p.name.endswith(RUN_SUFFIX)),
                  key=lambda p: p.name)


def state_path(root: Path) -> Path:
    return P.producer_dir(Path(root)) / STATE_NAME


def residue_hold(root: Path) -> Optional[Dict[str, Any]]:
    holds = []
    for run_dir in run_dirs(root):
        journal = P._read_json(run_dir / JOURNAL_NAME)
        if journal is None:
            continue
        if journal.get("phase") not in TERMINAL_PHASES:
            holds.append({"code": "residue-in-progress", "journal": str(run_dir / JOURNAL_NAME),
                          "phase": journal.get("phase"), "gate": "residue", "started_at": journal.get("started_at")})
    if not holds:
        return None
    holds.sort(key=lambda h: h["journal"])
    return holds[0]


def migration_hold(root: Path) -> Optional[Dict[str, Any]]:
    """Resplit, relayout, or residue hold -- oldest contract first."""
    return RL.migration_hold(root) or residue_hold(root)


def _is_runtime_owned(name: str) -> bool:
    return P._is_runtime_owned_top_level(name) or name in {"campaigns", "shared"}


def _date_from_prefix(name: str) -> Optional[str]:
    match = _DATE_PREFIX.match(name)
    if not match:
        return None
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _oldest_mtime_date(paths: Sequence[Path]) -> Optional[str]:
    stamps = []
    for path in paths:
        try:
            stamps.append(path.lstat().st_mtime)
        except OSError:
            continue
    if not stamps:
        return None
    return datetime.datetime.fromtimestamp(min(stamps), datetime.timezone.utc).date().isoformat()


def _epoch(date_iso: str) -> float:
    return datetime.datetime.fromisoformat(date_iso + "T00:00:00+00:00").timestamp()


def _root_slug(root: Path) -> str:
    try:
        return RS._default_root_slug(root)
    except Exception:  # pragma: no cover - slug rules belong to resplit
        return "root"


# ---------------------------------------------------------------------------
# classification (pure)
# ---------------------------------------------------------------------------


def iter_residue_files(root: Path) -> Iterator[str]:
    """Every regular file at the legacy top level, root-relative, sorted."""
    root = Path(root).resolve()
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if _is_runtime_owned(entry.name):
            continue
        if entry.is_symlink() or entry.is_file():
            yield entry.name
            continue
        for path in P._walk_files(entry):
            if path.is_symlink() or path.is_file():
                yield _rel(root, path)


_MAP_SOURCES: Dict[str, List[Tuple[str, str]]] = {}


def _map_sources(root: Path) -> List[Tuple[str, str]]:
    """Every (source, target) row of the compat chain, latest map last (cached per compat.json version)."""
    compat = C.compat_path(root)
    try:
        st = compat.stat()
        key = f"{root}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = f"{root}|absent"
    if key not in _MAP_SOURCES:
        _MAP_SOURCES.clear()
        rows: List[Tuple[str, str]] = []
        for _name, table in C._load_maps(root):
            rows.extend(table.items())
        _MAP_SOURCES[key] = rows
    return _MAP_SOURCES[key]


def _origin_cycle(root: Path, rel_dir: str) -> Optional[Dict[str, Any]]:
    """The migrated cycle a legacy `<bucket>/<d1>` directory resolves to, if any.

    W7C wrote one map row per *file*, so the directory itself is rarely a row;
    any row whose source lies under the directory names the cycle its durable
    rows went to.
    """
    resolved = C.resolve_legacy(root, rel_dir)
    target = resolved.get("target") if resolved.get("resolution") in {"mapped", "mapped-ancestor"} else None
    if target is None:
        prefix = rel_dir + "/"
        for source, mapped in reversed(_map_sources(root)):
            if source.startswith(prefix) and mapped.startswith("campaigns/"):
                target = mapped
                break
    if target is None:
        return None
    probe = Path(root) / str(target)
    for _ in range(8):
        if (probe / "manifest.json").is_file():
            manifest = P._read_json(probe / "manifest.json") or {}
            cycle = manifest.get("cycle") or {}
            campaign = manifest.get("campaign") or {}
            cycle_id, campaign_id = cycle.get("cycle_id"), campaign.get("campaign_id")
            if not isinstance(cycle_id, str) or not isinstance(campaign_id, str):
                return None
            record = P.read_cycle_record(root, cycle_id) or {}
            return {"cycle_id": cycle_id, "campaign_id": campaign_id, "cycle_dir": _rel(root, probe),
                    "title": record.get("title") or cycle_id, "slug": record.get("slug") or "cycle",
                    "started_on": record.get("resplit_started_on") or record.get("started_on") or cycle.get("started_on")}
        if probe == Path(root) or probe.parent == probe:
            break
        probe = probe.parent
    return None


_BAD_COMPONENT_CHAR = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_component(component: str) -> str:
    """A D-6-safe display component for a name the manifest cannot carry.

    Bytes are untouched; only the locator changes. The change is reversible
    through the cycle's `_internal/residue-inventory.json` primary and through
    the compat row whose `source_locator` keeps the original path. A short
    digest of the original keeps two different originals from colliding.
    """
    if P._unmanifestable_reason(component) is None and component not in artifact_manifest._RESERVED_LOCATOR_NAMES:
        return component
    tag = hashlib.sha256(component.encode("utf-8")).hexdigest()[:8]
    stem, dot, ext = component.rpartition(".")
    if not dot or not stem.strip(".") or len(ext) > 8 or _BAD_COMPONENT_CHAR.search(ext):
        stem, ext = component, ""
    stem = re.sub(r"_+", "_", _BAD_COMPONENT_CHAR.sub("_", stem)).strip("._-") or "file"
    cleaned = f"{stem}-{tag}" + (f".{ext}" if ext else "")
    return cleaned[:128]


def sanitize_locator(rel: str) -> Tuple[str, bool]:
    """A locator the manifest accepts: safe components, no reserved leaf name,
    at most `_MAX_LOCATOR_COMPONENTS` components and `_MAX_LOCATOR_LENGTH` bytes."""
    parts = rel.split("/")
    cleaned = [sanitize_component(part) for part in parts]
    limit = artifact_manifest._MAX_LOCATOR_COMPONENTS - 4  # room for artifacts/_internal/<bucket>/<d1>
    if len(cleaned) > limit:
        tag = hashlib.sha256("/".join(parts).encode("utf-8")).hexdigest()[:8]
        cleaned = cleaned[:limit - 1] + [f"{tag}-{cleaned[-1]}"[:128]]
    joined = "/".join(cleaned)
    if len(joined) > artifact_manifest._MAX_LOCATOR_LENGTH - 96:
        tag = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:8]
        joined = "/".join(cleaned[:2] + [f"{tag}-{cleaned[-1]}"[:128]])
    return joined, joined != rel


def classify(root: Path, rel: str, *, origin_cache: Dict[str, Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    parts = rel.split("/")
    top, name = parts[0], parts[-1]
    row: Dict[str, Any] = {"path": rel, "top": top}
    link = Path(root) / rel
    if link.is_symlink():
        target = os.readlink(link)
        resolved = Path(target) if os.path.isabs(target) else link.parent / target
        row.update(shape="symlink", disposition="deferred", group=None, target=None, reason="symlink",
                   link_target=target, dangling=not resolved.exists())
        return row
    if name == ".gitkeep" or any(p in {".agent_reports", ".claude_reports", ".runtime"} for p in parts[1:]):
        row.update(shape="trash", disposition="retire-with-approval", group=None, target=None)
        return row
    safe_rel, renamed = sanitize_locator(rel)
    if renamed:
        row["locator_renamed_from"] = rel
    evidence_prefix = next((s for s in C.SEALED_EVIDENCE_PATHS if rel == s or rel.startswith(s + "/")), None)
    if evidence_prefix is not None:
        row.update(shape="sealed-evidence", disposition="evidence-cycle", group=f"evidence:{evidence_prefix}",
                   target=f"artifacts/{safe_rel}", evidence_prefix=evidence_prefix)
        return row
    if top in DEFERRED_TOPS:
        row.update(shape="spec", disposition="deferred", group=None, target=None, reason="spec-consumer-pinned")
        return row
    if top in ROUTE_TOPS or (len(parts) == 1 and _ROUTEISH.search(name)):
        row.update(shape="route-container", disposition="runtime-routes-legacy", group="routes",
                   target=f".runtime/routes/legacy/{rel}")
        return row
    parts = safe_rel.split("/")
    rel = safe_rel
    if top in BUCKET_TOPS and len(parts) >= 3:
        d1 = parts[1]
        key = f"{top}/{d1}"
        if key not in origin_cache:
            origin_cache[key] = _origin_cycle(root, key)
        origin = origin_cache[key]
        rest = "/".join(parts[2:])
        if origin is not None:
            row.update(shape="support-residue", disposition="origin-residue-cycle",
                       group=f"origin:{origin['cycle_id']}", target=f"artifacts/_internal/{top}/{d1}/{rest}",
                       origin=origin)
            return row
        row.update(shape="bucket-cycle", disposition="new-bucket-cycle", group=f"bucket:{top}/{d1}",
                   target=f"artifacts/{top}/{d1}/{rest}", bucket=top, depth1=d1)
        return row
    if top in SUPPORT_TOPS:
        row.update(shape="root-support", disposition="root-support-cycle", group="support:root",
                   target=f"artifacts/_internal/{rel}")
        return row
    if top == "papers" and len(parts) >= 3:
        d1 = parts[1]
        row.update(shape="project-material", disposition="material-cycle", group=f"material:papers/{d1}",
                   target=f"artifacts/{MATERIAL_BUCKET}/papers/{d1}/{'/'.join(parts[2:])}", depth1=d1)
        return row
    if len(parts) == 1:
        row.update(shape="project-material", disposition="material-cycle", group="material:_root",
                   target=f"artifacts/{MATERIAL_BUCKET}/_root/{name}")
        return row
    row.update(shape="project-material", disposition="material-cycle", group=f"material:{top}",
               target=f"artifacts/{MATERIAL_BUCKET}/{rel}")
    return row


# ---------------------------------------------------------------------------
# plan (pure read)
# ---------------------------------------------------------------------------


def _cycle_spec(root: Path, group: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Campaign/cycle naming for one group of files; the source of the date is journaled."""
    kind, _sep, ident = group.partition(":")
    paths = [Path(root) / r["path"] for r in rows]
    if kind == "origin":
        origin = rows[0]["origin"]
        date = artifact_locator.date_part(origin["started_on"]) if origin.get("started_on") else _oldest_mtime_date(paths)
        return {"campaign_id": origin["campaign_id"], "campaign_key": None, "campaign_title": None,
                "campaign_goal": None, "title": f"{origin['title']} — legacy support residue",
                "slug": f"{origin['slug']}-residue", "date": date, "date_source": "origin-cycle",
                "residue_of": {"cycle_id": origin["cycle_id"], "campaign_id": origin["campaign_id"],
                               "cycle_dir": origin["cycle_dir"]},
                "support_all": True, "bucket": "_internal"}
    if kind == "bucket":
        top, _s, d1 = ident.partition("/")
        date = _date_from_prefix(d1)
        return {"campaign_id": None, "campaign_key": f"legacy-residue:{top}",
                "campaign_title": f"legacy {top} residue", "campaign_goal": f"{top} cycles that predate the producer contract",
                "title": d1, "slug": d1, "date": date or _oldest_mtime_date(paths),
                "date_source": "directory-date-prefix" if date else "oldest-file-mtime",
                "residue_of": None, "support_all": False, "bucket": top}
    if kind == "material":
        key, title, goal = CAMPAIGN_KEYS["project-material"]
        d1 = ident.split("/", 1)[1] if ident.startswith("papers/") else None
        date = _date_from_prefix(d1) if d1 else None
        return {"campaign_id": None, "campaign_key": key, "campaign_title": title, "campaign_goal": goal,
                "title": d1 or ident, "slug": (d1 or ident).replace("/", "-"),
                "date": date or _oldest_mtime_date(paths),
                "date_source": "directory-date-prefix" if date else "oldest-file-mtime",
                "residue_of": None, "support_all": False, "bucket": MATERIAL_BUCKET}
    if kind == "support":
        key, title, goal = CAMPAIGN_KEYS["root-support"]
        return {"campaign_id": None, "campaign_key": key, "campaign_title": title, "campaign_goal": goal,
                "title": f"legacy support residue ({_root_slug(root)})", "slug": "support-residue",
                "date": _oldest_mtime_date(paths), "date_source": "oldest-file-mtime",
                "residue_of": None, "support_all": True, "bucket": "_internal"}
    if kind == "evidence":
        key, title, goal = CAMPAIGN_KEYS["sealed-evidence"]
        leaf = ident.split("/")[1] if "/" in ident else ident
        date = _date_from_prefix(leaf)
        return {"campaign_id": None, "campaign_key": key, "campaign_title": title, "campaign_goal": goal,
                "title": ident, "slug": leaf, "date": date or _oldest_mtime_date(paths),
                "date_source": "directory-date-prefix" if date else "oldest-file-mtime",
                "residue_of": None, "support_all": False, "bucket": ident.split("/")[0], "evidence_prefix": ident}
    raise ResidueError("residue-group-unknown", group)


def build_plan(root: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = P.artifact_lifecycle.read_root_identity(root)
    origin_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    rows = [classify(root, rel, origin_cache=origin_cache) for rel in iter_residue_files(root)]
    for row in rows:
        st = (root / row["path"]).lstat()
        row["size"] = st.st_size
        row["inode"] = st.st_ino
    by_disposition: collections.Counter = collections.Counter(r["disposition"] for r in rows)
    by_shape: collections.Counter = collections.Counter(r["shape"] for r in rows)
    groups: Dict[str, List[Dict[str, Any]]] = collections.OrderedDict()
    for row in rows:
        if row["group"] and row["disposition"] != "runtime-routes-legacy":
            groups.setdefault(row["group"], []).append(row)
    cycles = []
    for group, grows in groups.items():
        spec = _cycle_spec(root, group, grows)
        cycles.append({**spec, "group": group, "cycle_key": f"residue:{_root_slug(root)}:{group}",
                       "files": [{"path": r["path"], "target": r["target"], "size": r["size"], "inode": r["inode"],
                                  **({"locator_renamed_from": r["locator_renamed_from"]} if r.get("locator_renamed_from") else {})}
                                 for r in grows],
                       "bytes": sum(r["size"] for r in grows),
                       "renamed_locators": sum(1 for r in grows if r.get("locator_renamed_from"))})
    routes = [{"path": r["path"], "target": r["target"], "size": r["size"], "inode": r["inode"]}
              for r in rows if r["disposition"] == "runtime-routes-legacy"]
    trash = [{"path": r["path"], "size": r["size"]} for r in rows if r["disposition"] == "retire-with-approval"]
    deferred = [{"path": r["path"], "shape": r["shape"], "reason": r["reason"],
                 **({"link_target": r["link_target"], "dangling": r["dangling"]} if r["shape"] == "symlink" else {})}
                for r in rows if r["disposition"] == "deferred"]
    map_state = C.load_map_state(root) if C.compat_path(root).is_file() else {"maps": [], "missing": [], "drifted": []}
    moved_maps = []
    for entry in map_state["maps"]:
        try:
            map_rel = Path(entry["path"]).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        for cycle in cycles:
            for f in cycle["files"]:
                if f["path"] == map_rel:
                    moved_maps.append({"path": entry["path"], "rel": map_rel, "sha256": entry["recorded_sha256"],
                                       "group": cycle["cycle_key"], "target": f["target"]})
    empty_dirs = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if _is_runtime_owned(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        if not any(p.is_file() or p.is_symlink() for p in P._walk_files(entry)):
            empty_dirs.append(entry.name)
    spec_impact = []
    if any(r["disposition"] == "material-cycle" for r in rows):
        spec_impact.append({"id": "D-23-material-shapes", "detail": "papers/, refs/, _preserved/, rebuttal/, notes/, "
                            "analysis_project/ residue and top-level files have no D-23 row; disposed as `documents` cycles"})
    if moved_maps:
        spec_impact.append({"id": "D-82-map-path-repoint", "detail": f"{len(moved_maps)} compat map file(s) move with sealed "
                            "evidence; compat.json maps[].path is re-pointed with sha256 unchanged and relocated_from recorded"})
    if any(r.get("locator_renamed_from") for r in rows):
        spec_impact.append({"id": "D-6-sanitized-locator", "detail": "files whose name cannot be a D-6 locator "
                            "(non-ASCII or hidden component) move under a sanitized display locator; bytes unchanged, "
                            "original name kept in the cycle's _internal/residue-inventory.json and in the compat row source"})
    if any(r["disposition"] == "origin-residue-cycle" for r in rows):
        spec_impact.append({"id": "D-79-residue-cycle", "detail": "support residue attaches to its migrated origin through a "
                            "separate sealed residue cycle (`residue_of`) in the origin campaign, never by rewriting the sealed manifest"})
    return {
        "schema_version": 1, "kind": PLAN_KIND, "algorithm_version": RESIDUE_ALGORITHM_VERSION,
        "artifact_root": str(root), "artifact_root_id": identity.artifact_root_id if identity else None,
        "root_slug": _root_slug(root),
        "totals": {"files": len(rows), "bytes": sum(r["size"] for r in rows), "cycles": len(cycles),
                   "route_files": len(routes), "trash": len(trash), "deferred": len(deferred),
                   "movable": len(rows) - len(trash) - len(deferred), "empty_dirs": len(empty_dirs),
                   "renamed_locators": sum(c["renamed_locators"] for c in cycles),
                   "symlinks": sum(1 for r in rows if r["shape"] == "symlink"),
                   "symlinks_dangling": sum(1 for r in rows if r["shape"] == "symlink" and r.get("dangling"))},
        "by_disposition": dict(sorted(by_disposition.items())), "by_shape": dict(sorted(by_shape.items())),
        "cycles": cycles, "routes": routes, "trash": trash, "deferred": deferred, "empty_dirs": empty_dirs,
        "moved_compat_maps": moved_maps,
        "compat_state": {"maps": len(map_state["maps"]), "missing": map_state["missing"], "drifted": map_state["drifted"]},
        "spec_impact": spec_impact,
    }


def _public(plan: Mapping[str, Any]) -> Dict[str, Any]:
    body = json.loads(json.dumps(plan))
    for cycle in body["cycles"]:
        for f in cycle["files"]:
            f.pop("inode", None)
    for r in body["routes"]:
        r.pop("inode", None)
    return body


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _write_journal(run_dir: Path, journal: Dict[str, Any]) -> None:
    P._write_atomic(run_dir / JOURNAL_NAME, P._json_bytes(journal))


def _inverse_rows(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / INVERSE_NAME
    return C._read_jsonl(path) if path.is_file() else []


def _append_inverse(run_dir: Path, rows: List[Dict[str, Any]], row: Dict[str, Any]) -> None:
    row = {"schema_version": 1, "kind": INVERSE_SCHEMA, "ordinal": len(rows), **row}
    rows.append(row)
    C._write_jsonl(run_dir / INVERSE_NAME, rows)


def _crash_after(journal: Mapping[str, Any], phase: str) -> None:
    if journal.get("crash_after_phase") == phase:
        raise ResidueError("crash-fixture", phase)


def _crash(journal: Mapping[str, Any], point: str) -> None:
    if journal.get("crash_at") == point:
        raise ResidueError("crash-fixture", point)


def _acquire(root: Path, run_dir: Path) -> None:
    lock = P.producer_dir(root) / LOCK_NAME
    body = P._read_json(lock)
    if body is not None and body.get("run_dir") != str(run_dir):
        raise ResidueError("residue-locked", body.get("run_dir", ""))
    P._write_atomic(lock, P._json_bytes({"run_dir": str(run_dir), "pid": os.getpid(), "at": _now()}), 0o600)


def _release(root: Path) -> None:
    lock = P.producer_dir(root) / LOCK_NAME
    if lock.is_file():
        lock.unlink()


def _remember_campaign(root: Path, pre: Dict[str, Any], run_dir: Path, journal: Dict[str, Any],
                       campaign: Optional[Mapping[str, Any]]) -> None:
    if campaign is None:
        return
    crel = _rel(root, P.campaign_dir(root, campaign["campaign_id"], campaign) / "campaign.json")
    if crel in {e["path"] for e in pre["campaign_records"]}:
        return
    pre["campaign_records"].append({"path": crel, "bytes_b64": _b64((root / crel).read_bytes())})
    _write_journal(run_dir, journal)


def _phase_begin(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Dict[str, Any], route_file: Path) -> None:
    """Derive one route per residue cycle (D-77-b), begin the cycle, record pre-images."""
    pre = journal.setdefault("pre_image", {})
    for key in ("created_routes", "created_cycle_records", "created_campaigns", "campaign_records", "created_cycle_dirs"):
        pre.setdefault(key, [])
    route = P.load_route(root, P.resolve_route_argument(root, Path(route_file)))
    ledger = RS.ledger_resplit_routes(root)
    begun_by_key = journal.setdefault("begun", {})
    for cycle in plan["cycles"]:
        key = cycle["cycle_key"]
        if key in begun_by_key:
            continue
        capability = route["capability"]
        derived, derived_file, created = RS._per_cycle_route(root, route, key, capability, cycle["slug"], run_dir, ledger)
        if created:
            pre["created_routes"].append({"route_id": derived["route_id"], "route_file": _rel(root, derived_file)})
            _write_journal(run_dir, journal)
        campaign_id = cycle.get("campaign_id")
        if campaign_id is not None:
            existing = P.read_campaign(root, campaign_id)
            if existing is None:
                raise ResidueError("residue-origin-campaign-missing", campaign_id)
        else:
            existing = P.find_campaign_by_key(root, cycle["campaign_key"])
        _remember_campaign(root, pre, run_dir, journal, existing)
        # `begin` runs on the real clock: its `now` also drives the admission
        # lock deadline, so a back-dated value would collapse the 30 s wait.
        begun = P.begin(root, route_file=derived_file, capability=capability, intensity=RS.RESPLIT_CYCLE_INTENSITY,
                        campaign_id=campaign_id, campaign_key=cycle.get("campaign_key"),
                        title=cycle["title"], goal=cycle.get("campaign_goal"))
        if begun.get("status") != "begun":
            raise ResidueError("residue-begin-failed", str(begun.get("status")))
        begun = _date_cycle(root, begun, cycle)
        if begun.get("campaign_created"):
            pre["created_campaigns"].append(begun["campaign_id"])
            if cycle.get("campaign_title"):
                camp = P.read_campaign(root, begun["campaign_id"])
                camp["title"] = cycle["campaign_title"]
                P._write_campaign(root, camp, exclusive=False)
        pre["created_cycle_records"].append(begun["cycle_id"])
        pre["created_cycle_dirs"].append(_rel(root, Path(begun["cycle_dir"])))
        _write_journal(run_dir, journal)
        record = P.read_cycle_record(root, begun["cycle_id"])
        record["residue_group"] = cycle["group"]
        record["residue_algorithm"] = RESIDUE_ALGORITHM_VERSION
        record["started_on_source"] = cycle.get("date_source")
        record["residue_run_at"] = journal["started_at"]
        if cycle.get("residue_of"):
            record["residue_of"] = cycle["residue_of"]
        P._write_cycle_record(root, record, exclusive=False)
        begun_by_key[key] = {"cycle_id": begun["cycle_id"], "campaign_id": begun["campaign_id"],
                             "cycle_dir": _rel(root, Path(begun["cycle_dir"])), "route_id": derived["route_id"],
                             "route_file": _rel(root, derived_file)}
        _write_journal(run_dir, journal)
        _crash(journal, "begin:after-first-cycle")


def _date_cycle(root: Path, begun: Dict[str, Any], cycle: Mapping[str, Any]) -> Dict[str, Any]:
    """Give a freshly begun residue cycle the work's own date (D-79 spirit):
    record `started_on` first, then the display locator, records winning."""
    date = cycle.get("date")
    record = P.read_cycle_record(root, begun["cycle_id"])
    if not date or record is None or record.get("started_on", "")[:10] == date:
        return begun
    campaign = P.read_campaign(root, begun["campaign_id"])
    campaign_path = P.campaign_dir(root, begun["campaign_id"], campaign)
    started_on = f"{date}T00:00:00Z"
    locator, suffix = artifact_locator.allocate_locator(campaign_path, started_on, record["slug"])
    old_dir = Path(begun["cycle_dir"])
    new_dir = campaign_path / locator
    record.update({"started_on": started_on, "locator": locator, "locator_suffix": suffix})
    P._write_cycle_record(root, record, exclusive=False)
    os.rename(old_dir, new_dir)
    artifact_locator.rebuild_indexes(root)
    return {**begun, "cycle_dir": str(new_dir)}


def _rename(root: Path, run_dir: Path, rows: List[Dict[str, Any]], source: Path, target: Path) -> None:
    if target.exists() and not source.exists():
        return
    if target.exists():
        raise ResidueError("residue-target-exists", _rel(root, target))
    if not source.is_file() or source.is_symlink():
        raise ResidueError("residue-source-missing", _rel(root, source))
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)
    _append_inverse(run_dir, rows, {"action": "rename_back", "source": _rel(root, source), "target": _rel(root, target)})


def _phase_rename(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Dict[str, Any]) -> None:
    rows = _inverse_rows(run_dir)
    first = True
    for cycle in plan["cycles"]:
        base = root / journal["begun"][cycle["cycle_key"]]["cycle_dir"]
        for f in cycle["files"]:
            _rename(root, run_dir, rows, root / f["path"], base / f["target"])
            if first:
                first = False
                _crash(journal, "rename:after-first-file")
    for r in plan["routes"]:
        _rename(root, run_dir, rows, root / r["path"], root / r["target"])
    created = journal.setdefault("created_sidecars", [])
    for cycle in plan["cycles"]:
        # The cycle's primary artifact: what moved in, from where, under which
        # locator, and which sealed cycle it is residue of. A support-only
        # cycle has no other primary, and the completion rule needs one.
        base = root / journal["begun"][cycle["cycle_key"]]["cycle_dir"]
        sidecar = base / "artifacts" / INVENTORY_LOCATOR
        if not sidecar.is_file():
            body = {"schema_version": 1, "kind": "w7h-residue-inventory",
                    "algorithm_version": RESIDUE_ALGORITHM_VERSION, "group": cycle["group"],
                    "residue_of": cycle.get("residue_of"), "date_source": cycle.get("date_source"),
                    "files": [{"source_locator": f["path"], "locator": f["target"][len("artifacts/"):],
                               "size": f["size"], **({"locator_renamed_from": f["locator_renamed_from"]}
                                                     if f.get("locator_renamed_from") else {})}
                              for f in cycle["files"]]}
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            P._write_atomic(sidecar, P._json_bytes(body))
            created.append(_rel(root, sidecar))
            _write_journal(run_dir, journal)


def _witness(root: Path, journal: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    mismatches = []
    checked = 0

    def check(path: Path, expected: Mapping[str, Any], label: str) -> None:
        nonlocal checked
        try:
            st = path.lstat()
        except FileNotFoundError:
            mismatches.append({"path": label, "reason": "missing"})
            return
        if st.st_size != expected["size"] or st.st_ino != expected["inode"]:
            mismatches.append({"path": label, "reason": "changed"})
        checked += 1

    for cycle in plan["cycles"]:
        base = root / journal["begun"][cycle["cycle_key"]]["cycle_dir"]
        for f in cycle["files"]:
            check(base / f["target"], f, f["target"])
    for r in plan["routes"]:
        check(root / r["target"], r, r["target"])
    if mismatches:
        raise ResidueError("residue-witness-mismatch", json.dumps(mismatches[:3], sort_keys=True))
    return {"files_checked": checked}


def _close_route(root: Path, run_dir: Path, cycle_id: str, group: str) -> None:
    record = P.read_cycle_record(root, cycle_id)
    if record is None or record.get("state") != "open":
        return
    route_file = Path(record["route_file"])
    route = P.load_route(root, route_file)
    if P.route_is_closed(root, route):
        return
    route_module = P.artifact_lifecycle._load_capability_route()
    evidence = run_dir / "routes" / f"{route['route_id']}.completion-evidence.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    if not evidence.is_file():
        P._write_atomic(evidence, P._json_bytes({"schema_version": 1, "kind": "w7h-residue-cycle-seal",
                                                 "cycle_id": cycle_id, "group": group, "run_dir": str(run_dir)}))
    for node in route.get("nodes", []):
        if node.get("terminal") is True:
            route_module.write_completion_marker(route, node, node["id"], evidence)
    outcome, _ = route_module.close_route(route, route_file, summary=f"residue cycle sealed: {group}")
    if outcome.get("terminal_gate_proven") is not True:
        raise ResidueError("residue-route-terminal-unproven", route["route_id"])


def _phase_seal(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Dict[str, Any]) -> None:
    sealed = journal.setdefault("sealed_cycles", {})
    for cycle in plan["cycles"]:
        key = cycle["cycle_key"]
        if key in sealed:
            continue
        begun = journal["begun"][key]
        _close_route(root, run_dir, begun["cycle_id"], cycle["group"])
        support = [f["target"][len("artifacts/"):] for f in cycle["files"]
                   if cycle["support_all"] or f["target"].startswith("artifacts/_internal/")]
        result = P.finalize(root, cycle_id=begun["cycle_id"], state="completed", support_locators=support,
                            primary=INVENTORY_LOCATOR, exclude_hidden=True)
        if result.get("status") not in {"sealed", "already-sealed"}:
            raise ResidueError("residue-seal-failed", str(result.get("status")))
        sealed[key] = {"cycle_id": begun["cycle_id"], "manifest_digest": result.get("manifest_digest"),
                       "artifact_count": result.get("artifact_count")}
        _write_journal(run_dir, journal)
        _crash(journal, "seal:after-first-cycle")


def _phase_compat(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Dict[str, Any]) -> None:
    pre = journal.setdefault("pre_image", {})
    if "written_map_files" in pre:
        return
    compat_path = C.compat_path(root)
    if not compat_path.is_file():
        raise ResidueError("compat-map-missing", str(compat_path))
    map_path = run_dir / MAP_NAME
    rows = []
    for cycle in plan["cycles"]:
        begun = journal["begun"][cycle["cycle_key"]]
        manifest = P._read_json(root / begun["cycle_dir"] / "manifest.json")
        digests = RL._manifest_digests(manifest)
        for f in cycle["files"]:
            target = f"{begun['cycle_dir']}/{f['target']}"
            sha = digests.get(f["target"]) or _sha_file(root / target)
            rows.append({"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": f["path"],
                         "target_locator": target, "sha256": sha, "identity_refs": [begun["cycle_id"]]})
    for r in plan["routes"]:
        rows.append({"schema_version": C.MAP_SCHEMA, "kind": "file", "source_locator": r["path"],
                     "target_locator": r["target"], "sha256": _sha_file(root / r["target"]), "identity_refs": []})
    pre["compat_json"] = {"bytes_b64": _b64(compat_path.read_bytes())}
    _write_journal(run_dir, journal)
    # A sealed-evidence map file that moved is re-pointed before the append so
    # `compat_append`'s existence check sees it at its new home (sha unchanged).
    compat = P._read_json(compat_path) or {}
    repointed = []
    for entry in compat.get("maps", []):
        for moved in plan["moved_compat_maps"]:
            if entry.get("path") == moved["path"]:
                begun = journal["begun"][moved["group"]]
                new_path = str(root / begun["cycle_dir"] / moved["target"])
                if _sha_file(Path(new_path)).split(":", 1)[1] != entry.get("sha256"):
                    raise ResidueError("compat-map-drifted", new_path)
                entry["relocated_from"] = entry["path"]
                entry["path"] = new_path
                repointed.append({"from": moved["path"], "to": new_path})
    if repointed:
        P._write_atomic(compat_path, P._json_bytes(compat), 0o600)
    if rows:
        C._write_jsonl(map_path, rows)
        C.compat_append(root, maps=[map_path], supersedes=[])
    pre["written_map_files"] = [_rel(root, map_path)] if rows else []
    journal["compat"] = {"rows": len(rows), "repointed_maps": repointed}
    _write_journal(run_dir, journal)


def _prune_empty(root: Path) -> List[str]:
    pruned = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if _is_runtime_owned(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        if any(p.is_file() or p.is_symlink() for p in P._walk_files(entry)):
            continue
        for current, _dirs, _files in os.walk(str(entry), topdown=False):
            try:
                os.rmdir(current)
                pruned.append(_rel(root, Path(current)))
            except OSError:
                pass
    return pruned


def _execute(root: Path, run_dir: Path, journal: Dict[str, Any], plan: Dict[str, Any], route_file: Optional[Path]) -> None:
    phase = journal["phase"]
    if phase == "planned":
        if route_file is None:
            raise ResidueError("residue-route-required")
        journal["phase"] = "cycles-begun"
        _phase_begin(root, run_dir, journal, plan, route_file)
        _write_journal(run_dir, journal)
        _crash_after(journal, "cycles-begun")
        phase = "cycles-begun"
    if phase == "cycles-begun":
        journal["phase"] = "renaming"
        _write_journal(run_dir, journal)
        phase = "renaming"
    if phase == "renaming":
        _phase_rename(root, run_dir, journal, plan)
        journal["phase"] = "renamed"
        _write_journal(run_dir, journal)
        _crash_after(journal, "renamed")
        phase = "renamed"
    if phase == "renamed":
        journal["witness"] = _witness(root, journal, plan)
        journal["phase"] = "witnessed"
        _write_journal(run_dir, journal)
        _crash_after(journal, "witnessed")
        phase = "witnessed"
    if phase == "witnessed":
        # The first `finalize` commits a manifest with O_EXCL; from here on the
        # run can only move forward, so the phase says so before it happens.
        journal["phase"] = "sealing"
        _write_journal(run_dir, journal)
        phase = "sealing"
    if phase == "sealing":
        _phase_seal(root, run_dir, journal, plan)
        journal["phase"] = "sealed"
        _write_journal(run_dir, journal)
        _crash_after(journal, "sealed")
        phase = "sealed"
    if phase == "sealed":
        _phase_compat(root, run_dir, journal, plan)
        journal["phase"] = "compat-reissued"
        _write_journal(run_dir, journal)
        _crash_after(journal, "compat-reissued")
        phase = "compat-reissued"
    if phase == "compat-reissued":
        artifact_locator.rebuild_indexes(root)
        journal["pruned_dirs"] = _prune_empty(root)
        journal["phase"] = "indexed"
        _write_journal(run_dir, journal)
        _crash_after(journal, "indexed")
        phase = "indexed"
    if phase == "indexed":
        state = {"schema_version": 1, "contract": P.CONTRACT, "algorithm_version": RESIDUE_ALGORITHM_VERSION,
                 "state": "complete", "completed_at": _now(), "run_dir": str(run_dir),
                 "deferred": plan["deferred"], "trash_pending": plan["trash"]}
        P._write_atomic(state_path(root), P._json_bytes(state), 0o600)
        journal["phase"] = "complete"
        journal["completed_at"] = _now()
        _write_journal(run_dir, journal)


def _rollback(root: Path, run_dir: Path, journal: Dict[str, Any]) -> None:
    if journal.get("sealed_cycles"):
        raise ResidueError("residue-past-commit-point", "a residue cycle is already sealed; resume rolls forward")
    for begun in (journal.get("begun") or {}).values():
        if (root / begun["cycle_dir"] / "manifest.json").exists():
            raise ResidueError("residue-past-commit-point", begun["cycle_dir"])
    for row in sorted(_inverse_rows(run_dir), key=lambda r: -r["ordinal"]):
        if row["action"] == "rename_back":
            target, source = root / row["target"], root / row["source"]
            if target.is_file() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, source)
    for rel in journal.get("created_sidecars", []):
        if (root / rel).is_file():
            (root / rel).unlink()
    pre = journal.get("pre_image") or {}
    for cycle_id in pre.get("created_cycle_records", []):
        path = P.cycle_record_path(root, cycle_id)
        if path.is_file():
            path.unlink()
    created_dirs = list(pre.get("created_cycle_dirs", []))
    created_dirs += [b["cycle_dir"] for b in (journal.get("begun") or {}).values() if b["cycle_dir"] not in created_dirs]
    for rel in sorted(created_dirs, key=lambda p: -p.count("/")):
        path = root / rel
        marker = path / artifact_locator.CYCLE_BINDING
        if marker.is_file():
            marker.unlink()
        if path.is_dir():
            try:
                RS._prune_empty_tree(path)
            except RS.ResplitError as exc:
                raise ResidueError("residue-rollback-residue", str(exc))
    for entry in pre.get("campaign_records", []):
        P._write_atomic(root / entry["path"], base64.b64decode(entry["bytes_b64"]))
    for campaign_id in pre.get("created_campaigns", []):
        found = artifact_locator.find_path_by_id(root, campaign_id)
        if found is not None and (found / "campaign.json").is_file():
            (found / "campaign.json").unlink()
            RS._prune_empty_tree(found)
    RS._release_created_routes(root, pre.get("created_routes", []))
    compat_pre = pre.get("compat_json")
    if compat_pre is not None:
        P._write_atomic(C.compat_path(root), base64.b64decode(compat_pre["bytes_b64"]), 0o600)
    for rel in pre.get("written_map_files", []):
        if (root / rel).is_file():
            (root / rel).unlink()
    # Directories created only to receive moved files are empty again now.
    for current, _dirs, _files in os.walk(str(root / ".runtime" / "routes" / "legacy"), topdown=False):
        try:
            os.rmdir(current)
        except OSError:
            pass
    inverse = run_dir / INVERSE_NAME
    if inverse.is_file():
        inverse.unlink()
    artifact_locator.rebuild_indexes(root)
    journal["phase"] = "rolled-back"
    journal["rolled_back_at"] = _now()
    _write_journal(run_dir, journal)


def _open_run(root: Path) -> Optional[Path]:
    for run_dir in run_dirs(root):
        journal = P._read_json(run_dir / JOURNAL_NAME)
        if journal is not None and journal.get("phase") not in TERMINAL_PHASES:
            return run_dir
    return None


def _new_run_dir(root: Path, suffix: str) -> Path:
    base = C.migrations_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    stamp = C._stamp()
    run_dir = base / f"{stamp}{suffix}"
    ordinal = 1
    while run_dir.exists():
        ordinal += 1
        run_dir = base / f"{stamp}-{ordinal}{suffix}"
    run_dir.mkdir(exist_ok=False)
    return run_dir


def _report(root: Path, run_dir: Optional[Path], journal: Mapping[str, Any], plan: Mapping[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    begun = journal.get("begun") or {}
    body = {
        "schema_version": 1, "kind": REPORT_KIND, "algorithm_version": RESIDUE_ALGORITHM_VERSION,
        "artifact_root": str(root), "artifact_root_id": plan.get("artifact_root_id"), "dry_run": dry_run,
        "run_dir": None if run_dir is None else str(run_dir), "phase": journal.get("phase"),
        "plan_digest": journal.get("plan_digest"), "totals": plan["totals"], "by_disposition": plan["by_disposition"],
        "by_shape": plan["by_shape"], "deferred": plan["deferred"], "trash": plan["trash"],
        "spec_impact": plan["spec_impact"], "witness": journal.get("witness"), "compat": journal.get("compat"),
        "sealed_cycles": journal.get("sealed_cycles"), "pruned_dirs": journal.get("pruned_dirs"),
        "cycles": [{"group": c["group"], "title": c["title"], "date": c["date"], "date_source": c["date_source"],
                    "files": len(c["files"]), "bytes": c["bytes"], "residue_of": c.get("residue_of"),
                    **({"cycle_id": begun[c["cycle_key"]]["cycle_id"], "cycle_dir": begun[c["cycle_key"]]["cycle_dir"]}
                       if c["cycle_key"] in begun else {})}
                   for c in plan["cycles"]],
        "routes": len(plan["routes"]),
    }
    body["digest"] = _canonical_digest(body)
    if run_dir is not None and not dry_run:
        P._write_atomic(run_dir / REPORT_NAME, P._json_bytes(body))
    return body


def apply(root: Path, *, route_file: Optional[Path] = None, dry_run: bool = False,
          crash_at: Optional[str] = None, crash_after_phase: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    C._require_active(root)
    other = RL.migration_hold(root)
    if other is not None:
        raise ResidueError(other["code"], other["journal"])
    open_run = _open_run(root)
    if open_run is not None:
        if dry_run:
            journal = P._read_json(open_run / JOURNAL_NAME) or {}
            return {"status": "hold", "code": "residue-in-progress", "run_dir": str(open_run), "phase": journal.get("phase")}
        return resume(root, run_dir=open_run, route_file=route_file, crash_at=crash_at, crash_after_phase=crash_after_phase)
    plan = build_plan(root)
    if plan["compat_state"]["missing"] or plan["compat_state"]["drifted"]:
        raise ResidueError("compat-map-missing" if plan["compat_state"]["missing"] else "compat-map-drifted",
                           json.dumps(plan["compat_state"], sort_keys=True))
    public = _public(plan)
    plan_digest = _canonical_digest(public)
    if plan["totals"]["movable"] == 0:
        return {"status": "no-op", "dry_run": dry_run, "plan_digest": plan_digest,
                "report": _report(root, None, {"phase": "no-op", "plan_digest": plan_digest}, plan, dry_run=True)}
    if dry_run:
        return {"status": "dry-run", "plan_digest": plan_digest,
                "report": _report(root, None, {"phase": "dry-run", "plan_digest": plan_digest}, plan, dry_run=True)}
    if route_file is None:
        raise ResidueError("residue-route-required")
    if not C.compat_path(root).is_file():
        raise ResidueError("compat-map-missing", str(C.compat_path(root)))
    run_dir = _new_run_dir(root, RUN_SUFFIX)
    _acquire(root, run_dir)
    try:
        P._write_atomic(run_dir / PLAN_NAME, P._json_bytes({**public, "digest": plan_digest}))
        P._write_atomic(run_dir / INVENTORY_NAME, P._json_bytes(
            {"cycles": {c["cycle_key"]: c["files"] for c in plan["cycles"]}, "routes": plan["routes"]}))
        journal = {"schema_version": 1, "algorithm_version": RESIDUE_ALGORITHM_VERSION, "phase": "planned",
                   "started_at": _now(), "plan_digest": plan_digest, "route_file": str(Path(route_file).resolve()),
                   "crash_at": crash_at, "crash_after_phase": crash_after_phase, "pre_image": {}, "begun": {}}
        _write_journal(run_dir, journal)
        try:
            _execute(root, run_dir, journal, plan, Path(route_file))
        except Exception as exc:  # noqa: BLE001 - every failure is journaled before it propagates
            if isinstance(exc, ResidueError) and exc.code == "crash-fixture":
                raise
            journal["error"] = {"code": getattr(exc, "code", type(exc).__name__), "detail": str(exc)}
            _write_journal(run_dir, journal)
            if journal["phase"] in ROLLBACK_PHASES:
                try:
                    _rollback(root, run_dir, journal)
                except Exception as rollback_exc:  # noqa: BLE001
                    journal["rollback_error"] = {"code": getattr(rollback_exc, "code", type(rollback_exc).__name__),
                                                 "detail": str(rollback_exc)}
                    _write_journal(run_dir, journal)
            raise
        report = _report(root, run_dir, journal, plan, dry_run=False)
        return {"status": "complete", "run_dir": str(run_dir), "report": report}
    finally:
        _release(root)


def _load_run(root: Path, run_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    journal = P._read_json(run_dir / JOURNAL_NAME)
    plan = P._read_json(run_dir / PLAN_NAME)
    inventory = P._read_json(run_dir / INVENTORY_NAME) or {}
    if journal is None or plan is None:
        raise ResidueError("residue-run-unreadable", str(run_dir))
    for cycle in plan["cycles"]:
        cycle["files"] = inventory.get("cycles", {}).get(cycle["cycle_key"], cycle["files"])
    plan["routes"] = inventory.get("routes", plan["routes"])
    return journal, plan


def resume(root: Path, *, run_dir: Optional[Path] = None, route_file: Optional[Path] = None,
           crash_at: Optional[str] = None, crash_after_phase: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    run_dir = Path(run_dir) if run_dir is not None else _open_run(root)
    if run_dir is None:
        return {"status": "no-op", "reason": "no-open-run"}
    _acquire(root, run_dir)
    try:
        journal, plan = _load_run(root, run_dir)
        journal["crash_at"] = crash_at
        journal["crash_after_phase"] = crash_after_phase
        phase = journal.get("phase")
        if phase in TERMINAL_PHASES:
            return {"status": phase, "run_dir": str(run_dir)}
        if phase in ROLLBACK_PHASES:
            _rollback(root, run_dir, journal)
            return {"status": "rolled-back", "run_dir": str(run_dir), "resumed_from": phase}
        if phase not in ROLL_FORWARD_PHASES:
            raise ResidueError("residue-phase-unknown", str(phase))
        _execute(root, run_dir, journal, plan, Path(route_file or journal["route_file"]))
        report = _report(root, run_dir, journal, plan, dry_run=False)
        return {"status": "complete", "run_dir": str(run_dir), "resumed_from": phase, "report": report}
    finally:
        _release(root)


def rollback(root: Path, *, run_dir: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    run_dir = Path(run_dir) if run_dir is not None else _open_run(root)
    if run_dir is None:
        return {"status": "no-op", "reason": "no-open-run"}
    _acquire(root, run_dir)
    try:
        journal, _plan = _load_run(root, run_dir)
        if journal.get("phase") not in ROLLBACK_PHASES:
            raise ResidueError("residue-past-commit-point", str(journal.get("phase")))
        _rollback(root, run_dir, journal)
        return {"status": "rolled-back", "run_dir": str(run_dir)}
    finally:
        _release(root)


# ---------------------------------------------------------------------------
# trash retirement (approval-gated deletion, W7F/D-84 pattern)
# ---------------------------------------------------------------------------


def trash_inventory(root: Path, *, include_symlinks: bool = False,
                    include_dangling_symlinks: bool = False) -> Dict[str, Any]:
    """Deletion candidates: trash always; symlinks only on request -- every
    symlink with `include_symlinks`, or only those whose target no longer
    exists with `include_dangling_symlinks` (a link into live data such as a
    corpus wav is a reference the operator keeps)."""
    root = Path(root).resolve()
    identity = P.artifact_lifecycle.read_root_identity(root)
    origin_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    rows = []
    for rel in iter_residue_files(root):
        row = classify(root, rel, origin_cache=origin_cache)
        is_trash = row["disposition"] == "retire-with-approval"
        is_symlink = row["shape"] == "symlink"
        wanted_symlink = is_symlink and (include_symlinks or (include_dangling_symlinks and row.get("dangling")))
        if is_trash or wanted_symlink:
            path = root / rel
            if is_symlink:
                rows.append({"path": rel, "size": 0, "sha256": _sha_bytes(os.readlink(path).encode("utf-8")),
                             "reason": "symlink-dangling" if row.get("dangling") else "symlink",
                             "link_target": os.readlink(path)})
            else:
                rows.append({"path": rel, "size": path.stat().st_size, "sha256": _sha_file(path), "reason": "trash"})
    body = {"schema_version": 1, "kind": "w7h-residue-trash-inventory",
            "artifact_root_id": identity.artifact_root_id if identity else None, "entries": rows,
            "entry_count": len(rows), "byte_size": sum(r["size"] for r in rows)}
    body["digest"] = _canonical_digest({"entries": rows})
    return body


def trash_approval_package(root: Path, *, backup_root: Path, include_symlinks: bool = False,
                           include_dangling_symlinks: bool = False) -> Dict[str, Any]:
    inventory = trash_inventory(root, include_symlinks=include_symlinks,
                                include_dangling_symlinks=include_dangling_symlinks)
    return {"schema_version": 1, "kind": TRASH_APPROVAL_KIND, "authorized": False,
            "body": {"artifact_root_id": inventory["artifact_root_id"], "repo_path": str(Path(root).resolve()),
                     "trash_inventory_sha256": inventory["digest"], "entry_count": inventory["entry_count"],
                     "byte_size": inventory["byte_size"], "backup_root": str(Path(backup_root).expanduser().resolve()),
                     "entries": inventory["entries"]}}


def retire_trash(root: Path, *, approval_path: Optional[Path], backup_root: Path, dry_run: bool = False,
                 include_symlinks: bool = False, include_dangling_symlinks: bool = False) -> Dict[str, Any]:
    root = Path(root).resolve()
    C._require_active(root)
    inventory = trash_inventory(root, include_symlinks=include_symlinks,
                                include_dangling_symlinks=include_dangling_symlinks)
    if dry_run or not inventory["entries"]:
        return {"status": "dry-run" if dry_run else "no-op", "inventory": inventory}
    if approval_path is None:
        raise ResidueError("trash-approval-required")
    approval = P._read_json(Path(approval_path))
    if not isinstance(approval, dict) or approval.get("authorized") is not True or approval.get("kind") != TRASH_APPROVAL_KIND:
        raise ResidueError("approval-not-authorized")
    body = approval.get("body") or {}
    if body.get("artifact_root_id") != inventory["artifact_root_id"]:
        raise ResidueError("approval-root-mismatch")
    if body.get("trash_inventory_sha256") != inventory["digest"]:
        raise ResidueError("approval-stale", "trash-inventory-digest")
    backup_root = Path(backup_root).expanduser().resolve()
    if backup_root == root or str(backup_root).startswith(str(root) + os.sep):
        raise ResidueError("backup-root-inside-artifact-root", str(backup_root))
    run_dir = _new_run_dir(root, TRASH_SUFFIX)
    backup_dir = backup_root / str(inventory["artifact_root_id"]) / run_dir.name
    backup_dir.mkdir(parents=True, exist_ok=False)
    archive = backup_dir / "retired-trash.tar"
    with tarfile.open(str(archive), "w") as tar:
        for row in inventory["entries"]:
            tar.add(str(root / row["path"]), arcname=row["path"], recursive=False)
    with archive.open("rb") as handle:
        os.fsync(handle.fileno())
    with tarfile.open(str(archive), "r") as tar:
        for row in inventory["entries"]:
            member = tar.getmember(row["path"])
            if row.get("reason", "").startswith("symlink"):
                if not member.issym() or _sha_bytes(member.linkname.encode("utf-8")) != row["sha256"]:
                    raise ResidueError("backup-incomplete", row["path"])
                continue
            handle = tar.extractfile(member)
            if handle is None or _sha_bytes(handle.read()) != row["sha256"]:
                raise ResidueError("backup-incomplete", row["path"])
    seal = {"schema_version": 1, "kind": "w7h-residue-trash-backup-seal", "archive": str(archive),
            "archive_sha256": _sha_file(archive), "inventory_sha256": inventory["digest"],
            "file_count": inventory["entry_count"], "byte_size": inventory["byte_size"], "created_at": _now()}
    P._write_atomic(backup_dir / "backup-seal.json", P._json_bytes(seal))
    P._write_atomic(run_dir / "trash-inventory.json", P._json_bytes(inventory))
    journal_rows = []
    for ordinal, row in enumerate(inventory["entries"]):
        path = root / row["path"]
        current = _sha_bytes(os.readlink(path).encode("utf-8")) if path.is_symlink() else _sha_file(path)
        if current != row["sha256"]:
            raise ResidueError("approval-stale", row["path"])
        path.unlink()
        journal_rows.append({"schema_version": "artifact-residue-trash-journal-row/v1", "ordinal": ordinal,
                             "action": "retire_trash", "source_locator": row["path"], "sha256": row["sha256"],
                             "backup_archive": str(archive), "commit_state": "committed"})
    pruned = _prune_empty(root)
    report = {"schema_version": 1, "kind": "w7h-residue-trash-report", "run_dir": str(run_dir),
              "backup_seal": seal, "retired_files": len(journal_rows), "pruned_dirs": pruned,
              "journal_sha256": C._write_jsonl(run_dir / "journal.jsonl", journal_rows), "created_at": _now()}
    P._write_atomic(run_dir / REPORT_NAME, P._json_bytes(report))
    return {"status": "complete", "report": report}


# ---------------------------------------------------------------------------
# status (gate surface)
# ---------------------------------------------------------------------------


def status(root: Path) -> Dict[str, Any]:
    """Read-only: what is still at the legacy top level and why."""
    root = Path(root).resolve()
    hold = residue_hold(root)
    error = None
    counts: collections.Counter = collections.Counter()
    deferred = []
    total = 0
    try:
        origin_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        for rel in iter_residue_files(root):
            row = classify(root, rel, origin_cache=origin_cache)
            counts[row["disposition"]] += 1
            total += 1
            if row["disposition"] == "deferred":
                deferred.append({"path": rel, "reason": row["reason"],
                                 **({"link_target": row["link_target"], "dangling": row["dangling"]} if row["shape"] == "symlink" else {})})
    except (ResidueError, artifact_locator.LocatorError) as exc:
        error = {"code": exc.code, "detail": exc.detail}
    except OSError as exc:
        error = {"code": "residue-scan-failed", "detail": str(exc)}
    state = P._read_json(state_path(root)) or {"state": "pending"}
    if hold is not None:
        layout = "in-progress"
    elif error is not None:
        layout = "invalid"
    elif total == 0:
        layout = "empty"
    elif total == len(deferred):
        layout = "deferred-only"
    else:
        layout = "residue"
    return {"legacy_top_level": layout, "legacy_top_level_files": total, "by_disposition": dict(sorted(counts.items())),
            "symlinks": sum(1 for d in deferred if d["reason"] == "symlink"),
            "symlinks_dangling": sum(1 for d in deferred if d.get("dangling")),
            "deferred": deferred, "trash_pending": counts.get("retire-with-approval", 0), "residue_hold": hold,
            "residue_state": state.get("state"), "error": error}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--artifact-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_p = sub.add_parser("plan", help="pure read: classification and disposition of every residue file")
    plan_p.add_argument("--full", action="store_true")
    apply_p = sub.add_parser("apply", help="journaled disposal (moves only; trash needs retire-trash)")
    apply_p.add_argument("--route-file", help="the caller's route; one route per residue cycle is derived from it")
    apply_p.add_argument("--dry-run", action="store_true")
    apply_p.add_argument("--crash-at")
    apply_p.add_argument("--crash-after-phase")
    resume_p = sub.add_parser("resume")
    resume_p.add_argument("--run-dir")
    resume_p.add_argument("--route-file")
    rollback_p = sub.add_parser("rollback")
    rollback_p.add_argument("--run-dir")
    trash_p = sub.add_parser("trash-approval-package", help="approval body for retire-trash (authorized:false)")
    trash_p.add_argument("--backup-root", required=True)
    trash_p.add_argument("--include-symlinks", action="store_true")
    trash_p.add_argument("--include-dangling-symlinks", action="store_true")
    retire_p = sub.add_parser("retire-trash", help="delete trash after backup; needs an authorized approval file")
    retire_p.add_argument("--approval")
    retire_p.add_argument("--backup-root", required=True)
    retire_p.add_argument("--dry-run", action="store_true")
    retire_p.add_argument("--include-symlinks", action="store_true")
    retire_p.add_argument("--include-dangling-symlinks", action="store_true",
                          help="also delete symlinks whose target no longer exists (live links stay deferred)")
    sub.add_parser("hold")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    root = Path(args.artifact_root).resolve()
    try:
        if args.command == "plan":
            plan = _public(build_plan(root))
            if not args.full:
                plan = {k: v for k, v in plan.items() if k not in {"cycles", "routes", "trash"}}
            _print(plan)
        elif args.command == "apply":
            result = apply(root, route_file=Path(args.route_file) if args.route_file else None, dry_run=args.dry_run,
                           crash_at=args.crash_at, crash_after_phase=args.crash_after_phase)
            _print(result)
            return 0 if result.get("status") in {"complete", "dry-run", "no-op"} else HOLD_EXIT
        elif args.command == "resume":
            _print(resume(root, run_dir=Path(args.run_dir) if args.run_dir else None,
                          route_file=Path(args.route_file) if args.route_file else None))
        elif args.command == "rollback":
            _print(rollback(root, run_dir=Path(args.run_dir) if args.run_dir else None))
        elif args.command == "trash-approval-package":
            _print(trash_approval_package(root, backup_root=Path(args.backup_root),
                                          include_symlinks=args.include_symlinks,
                                          include_dangling_symlinks=args.include_dangling_symlinks))
        elif args.command == "retire-trash":
            _print(retire_trash(root, approval_path=Path(args.approval) if args.approval else None,
                                backup_root=Path(args.backup_root), dry_run=args.dry_run,
                                include_symlinks=args.include_symlinks,
                                include_dangling_symlinks=args.include_dangling_symlinks))
        elif args.command == "hold":
            hold = residue_hold(root)
            _print({"hold": hold})
            return 0 if hold is None else HOLD_EXIT
        elif args.command == "status":
            _print(status(root))
    except (ResidueError, C.CutoverError, P.ProducerError, artifact_admission.AdmissionBusy) as exc:
        code = getattr(exc, "code", None) or (str(exc.args[0]) if exc.args else type(exc).__name__)
        detail = getattr(exc, "detail", None) or (str(exc.args[1]) if len(exc.args) > 1 else "")
        _print({"status": "blocked", "code": code, "detail": detail})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
