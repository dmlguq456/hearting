#!/usr/bin/env python3
"""W7G retrospective resplit of W7C lump cycles (PRD v12 §29). Owns R1~R3 and the
D-80 proposal validator; compat append and retire stay in artifact_cutover."""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_cutover as C  # noqa: E402
import artifact_identity  # noqa: E402
import artifact_producer as P  # noqa: E402

RESPLIT_ALGORITHM_VERSION = "w7g-resplit/v1"
INVERSE_SCHEMA = "artifact-resplit-inverse-row/v1"
LUMP_REPORT_KIND = "w7c-delta-migration"
CYCLE_KEY_RE = re.compile(
    r"^legacy:[a-z0-9][a-z0-9-]{0,63}:(plans|documents|designs|research|experiments)/[^/]{1,128}$"
)
# The `<root-slug>` field of `CYCLE_KEY_RE`, isolated so a slug can be rejected
# where it is derived instead of silently producing an unmatchable cycle key.
ROOT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CAMPAIGN_SLUG_RE = re.compile(r"^[a-z0-9-]{3,48}$")
UNASSIGNED_SLUG = "_unassigned"
# The two artifact-root container names (`core/CORE.md` write-cutover rule): the
# canonical `.agent_reports` and the legacy `.claude_reports` fallback.
ARTIFACT_ROOT_DIR_NAMES = (".agent_reports", ".claude_reports")
# D-79 loose files land under the target cycle's `artifacts/<LOOSE_PREFIX>/<source
# locator>` so the origin bucket name stays in the path.
LOOSE_PREFIX = "_internal"
CYCLE_BUCKETS = C.CYCLE_BUCKETS
SHARED_SNAPSHOT = C.SHARED_SNAPSHOT
LANES = ("semantic-boundary", "relationship", "display-quality")
# D-79/D-7: a resplit cycle's capability is a function of its D-23 bucket, never of
# the route that happens to be running the resplit. Inheriting the caller's
# capability made all 16 hearting cycles `autopilot-code` including the research
# ones, which the read projection then reports as code work that never happened.
BUCKET_CAPABILITY = {
    "plans": "autopilot-code",
    "experiments": "autopilot-lab",
    "research": "autopilot-research",
    "documents": "autopilot-draft",
    "designs": "autopilot-design",
}
# The lump record's intensity describes the W7C migration call, not the original
# work, so it is no more honest than the caller's. A retrospective cycle records
# the neutral default instead of claiming a rigor nobody measured.
RESPLIT_CYCLE_INTENSITY = "standard"
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
PROPOSAL_ROW_KEYS = frozenset({
    "proposal_id", "fingerprint", "lane", "target_ids", "cited_evidence_ids", "source_cutoff",
    "producer_version", "projection_version", "policy_version", "proposed_value", "confidence", "rationale",
})

# 🔴4 spec-impact (D-80 rule 6, S2 plan §7 D4): the PRD does not define an
# evidence-id registry or the universe `cited_evidence_ids` must be drawn
# from. This module provisionally treats the lump cycle unit's own file
# locators as that universe (see `_rule_6_evidence_in_cutoff`'s call site
# below) -- not because the contract says so, but because it is the only
# cutoff-bound identifier space this module already has sealed in
# `lump-inventory.json`. When a real evidence-id registry/contract lands,
# this constant name is the single place that needs to change, and the
# validator logic in `_rule_6_evidence_in_cutoff` should be revisited then,
# not before.
RULE_6_EVIDENCE_UNIVERSE_KIND = "lump-cycle-unit-file-locator"
ADMISSION_FILES = (
    "proposal.json", "lump-inventory.json", "loose-inventory.json",
    "retire-inventory.json", "admission-receipt.json",
)
R1_TERMINAL = {"admitted"}
R2_TERMINAL = {"complete", "rolled-back"}
R3_TERMINAL = {"complete", "rolled-back"}
OK, BLOCKED = 0, 65


class ResplitError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# small local helpers (no upstream write reuse -- import only)
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "root"


def _require_root_slug(slug: str, source: str) -> str:
    """A slug that cannot fill `CYCLE_KEY_RE`'s `<root-slug>` field is refused here,
    not carried into a cycle key nothing will ever match. Reachable two ways since
    the slug stopped being the constant `agent-reports`: a repo directory longer
    than 64 characters, and a name with no ASCII alphanumerics at all (which
    `_slugify` would otherwise collapse to the misleading literal `root`)."""
    if not ROOT_SLUG_RE.match(slug or ""):
        raise ResplitError("root-slug-invalid", f"{source}:{slug}")
    return slug


def _default_root_slug(root: Path) -> str:
    """D-79 cycle key `<root-slug>`: the roster `repo_path` basename, slugified.

    The artifact root is `<repo_path>/.agent_reports` (or the legacy
    `.claude_reports`), so the repo basename is the root's *parent* directory
    name -- `_slugify(root.name)` would yield `agent-reports` for every root in
    the fleet and collapse all their cycle keys onto one slug. `resolve()`
    happens first so a symlinked repo (`BC_ResNet_tf` -> `BC_ResNet`) slugs from
    its realpath, matching the roster entry (`bc-resnet`). A root that is not one
    of those two container names is not a repo-relative artifact root, so its own
    name stays the slug.
    """
    root = Path(root).resolve()
    name = root.parent.name if (root.name in ARTIFACT_ROOT_DIR_NAMES and root.parent.name) else root.name
    if not re.search(r"[a-z0-9]", name.lower()):
        # `_slugify` would return its `or "root"` fallback here, which is a wrong
        # answer dressed as a real one -- every such root would share one slug.
        raise ResplitError("root-slug-invalid", f"underivable:{name}")
    return _require_root_slug(_slugify(name), "derived")


def _canonical_digest(body: Dict[str, Any]) -> str:
    # `P._digest` already returns a `sha256:`-prefixed value; prefixing again here
    # produced `sha256:sha256:<hex>` in every sealed W7G inventory digest. The
    # comparison sites are all self-consistent, so the doubling was invisible until
    # a digest was read by anything outside this module. No root has an admitted
    # resplit run yet (PRD v12 §29 status), and R1 re-seals every inventory, so the
    # single-prefix form is the only one production will ever observe.
    stripped = {k: v for k, v in body.items() if k != "digest"}
    return P._digest(P._canonical(stripped))


def _find_campaign_by_key_local(root: Path, key: str) -> Optional[Dict[str, Any]]:
    campaigns_dir = Path(root) / "campaigns"
    if not campaigns_dir.is_dir():
        return None
    for entry in sorted(campaigns_dir.iterdir(), key=lambda p: p.name):
        record = P._read_json(entry / "campaign.json")
        if record and record.get("key") == key:
            return record
    return None


def _find_cycle_by_key(root: Path, cycle_key: str) -> Optional[Dict[str, Any]]:
    for record in P.list_cycle_records(root):
        if record.get("cycle_key") == cycle_key:
            return record
    return None


def _expected_tree_digest(files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    for f in files:
        digest = f.get("sha256") or ""
        hexd = digest.split(":", 1)[-1] if digest else ""
        rows.append((f["locator"], f.get("byte_size") or 0, hexd))
    rows.sort()
    payload = "\n".join(f"{r}\t{n}\t{d}" for r, n, d in rows).encode("utf-8")
    return {
        "file_count": len(rows), "byte_count": sum(n for _, n, _ in rows),
        "tree_sha256": hashlib.sha256(payload).hexdigest(),
    }


# ---------------------------------------------------------------------------
# D-76 scanner / read predicates (mutation 0)
# ---------------------------------------------------------------------------


def _loose_excluded_top_names() -> set:
    return (
        set(CYCLE_BUCKETS) | set(SHARED_SNAPSHOT.keys()) | set(P.CANONICAL_ROOTS)
        | set(C.PRESERVED_SUPPORT_CONTAINERS) | {"routes", "_routes"}
    )


def _lump_manifest_digest(cycle_dir: Path) -> Optional[str]:
    manifest_path = cycle_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    return "sha256:" + C._sha(manifest_path)


_WRITTEN_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
# D-5 route filename shape. A stage-session directory that is not a route id is not the
# `C-RT` record this relocation owns, so it is left where it is instead of moved.
_ROUTE_ID_RE = re.compile(r"^rt-[0-9a-f]{16}$")


def _entry_document_written_date(cdir: Path, bucket: str, depth1: str, files: Sequence[Dict[str, Any]]) -> Optional[str]:
    """D-79 step (2): a written date recorded inside the unit's entry document.

    The entry document is the first file by locator sort order (matches the
    manifest-row ordering `scan_lumps` already produces). Reads only its own
    bytes -- never mtime. Looks for a YAML frontmatter `created`/`date` key
    first, then falls back to the first bare `YYYY-MM-DD` token in the body.
    """
    if not files:
        return None
    entry = sorted(files, key=lambda f: f["locator"])[0]
    path = cdir / "artifacts" / entry["locator"]
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            frontmatter = text[4:end]
            for line in frontmatter.splitlines():
                key, _, value = line.partition(":")
                if key.strip() in ("created", "date"):
                    m = _WRITTEN_DATE_RE.search(value)
                    if m:
                        return m.group(1)
    m = _WRITTEN_DATE_RE.search(text)
    return m.group(1) if m else None


def _as_rfc3339(value: Optional[str]) -> Optional[str]:
    """D-6 requires `cycle.started_on` to be RFC3339 UTC, but D-79's first two
    priorities yield a bare `YYYY-MM-DD`. A date is widened to that day's UTC
    midnight -- the only reading that keeps the contract's date and the schema's
    timestamp the same instant. Anything else is not a date and returns `None` so
    the caller falls through to the next priority instead of sealing garbage."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _DATE_ONLY_RE.match(value):
        return value + "T00:00:00Z"
    if _RFC3339_RE.match(value):
        return value
    return None


def _lump_started_on(cdir: Path, bucket: str, depth1_name: str, files: Sequence[Dict[str, Any]],
                     record_started_on: Optional[str]) -> Tuple[str, str]:
    """D-79 `started_on` priority, with the source that decided it.

    (1) the directory name's `YYYY-MM-DD_` prefix, (2) the written date inside the
    unit's entry document, (3) the lump cycle's own `started_on`. mtime is never
    consulted -- every W7C copy carries its copy time. The run's wall clock is not
    a priority at all: it is the resplit's timestamp, not the work's, and it lives
    in the record's `resplit_run_at`.
    """
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})[_-]", depth1_name)
    if m:
        try:
            parsed = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            parsed = None
        if parsed is not None:
            return _as_rfc3339(parsed.isoformat()) or "", "directory-date-prefix"
    written = _as_rfc3339(_entry_document_written_date(cdir, bucket, depth1_name, files))
    if written:
        return written, "entry-document-date"
    return (_as_rfc3339(record_started_on) or ""), "lump-record"


def _original_legacy_sources(root: Path) -> Dict[str, str]:
    """D-82 lane (1): read-only inversion of the existing W7C compat map so a
    lump-relative target locator resolves back to its original pre-W7C
    source locator. Later map entries win on a repeated target (same
    latest-wins rule `resolve_legacy` already applies)."""
    inverted: Dict[str, str] = {}
    for _name, table in C._load_maps(root):
        for source_locator, target_locator in table.items():
            inverted[target_locator] = source_locator
    return inverted


def scan_lumps(root: Path, *, root_slug: Optional[str] = None) -> Dict[str, Any]:
    """D-76 deterministic scanner (mutation 0). Fresh read every call."""
    root = Path(root).resolve()
    slug = _require_root_slug(root_slug, "explicit") if root_slug else _default_root_slug(root)
    mdir = C.migrations_dir(root)
    groups: Dict[str, List[Path]] = {}
    if mdir.is_dir():
        for run in sorted(mdir.iterdir(), key=lambda p: p.name):
            report_path = run / "report.json"
            if not report_path.is_file():
                continue
            report = P._read_json(report_path)
            if not isinstance(report, dict) or report.get("kind") != LUMP_REPORT_KIND:
                continue
            if report.get("state") != "sealed":
                continue
            cycle_id = report.get("cycle_id")
            if isinstance(cycle_id, str):
                groups.setdefault(cycle_id, []).append(report_path)
    lumps: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    for cycle_id in sorted(groups):
        candidates = groups[cycle_id]
        if len(candidates) != 1:
            invalid.append({"lump_cycle_id": cycle_id, "code": "lump-report-invalid",
                            "detail": f"candidate-count:{len(candidates)}"})
            continue
        report_path = candidates[0]
        report = P._read_json(report_path)
        campaign_id = report.get("campaign_id")
        cycle_dir_str = report.get("cycle_dir")
        if not campaign_id or not cycle_dir_str or report.get("artifact_root") != str(root):
            invalid.append({"lump_cycle_id": cycle_id, "code": "lump-report-invalid", "detail": "fields-or-containment"})
            continue
        cdir = Path(cycle_dir_str)
        try:
            cdir.resolve().relative_to(root)
        except ValueError:
            invalid.append({"lump_cycle_id": cycle_id, "code": "lump-report-invalid", "detail": "cycle-dir-outside-root"})
            continue
        manifest_digest = _lump_manifest_digest(cdir)
        record = P.read_cycle_record(root, cycle_id)
        if manifest_digest is None or record is None or record.get("manifest_digest") != manifest_digest:
            invalid.append({"lump_cycle_id": cycle_id, "code": "lump-report-invalid", "detail": "manifest-digest-mismatch"})
            continue
        manifest = P._read_json(cdir / "manifest.json") or {}
        rows = manifest.get("artifact_revisions", []) if isinstance(manifest, dict) else []
        cycle_units: Dict[Tuple[str, str], Dict[str, Any]] = {}
        shared_input: List[Dict[str, Any]] = []
        stage_sessions: List[Dict[str, Any]] = []
        for row in rows:
            locator = row.get("locator") or {}
            path = locator.get("path", "")
            if not isinstance(path, str) or not path.startswith("artifacts/"):
                continue
            rel = path[len("artifacts/"):]
            parts = rel.split("/")
            top = parts[0] if parts else ""
            file_entry = {"locator": rel, "sha256": row.get("content_digest"), "byte_size": row.get("byte_size")}
            if top == "shared-input":
                kind = parts[1] if len(parts) > 1 else ""
                shared_input.append({"kind": kind, **file_entry})
                continue
            if top == "plans" and len(parts) > 1 and parts[1] == "stage-sessions":
                stage_sessions.append(file_entry)
                continue
            if top in CYCLE_BUCKETS and len(parts) > 1:
                depth1 = parts[1]
                unit = cycle_units.setdefault((top, depth1), {"bucket": top, "depth1_name": depth1, "files": []})
                unit["files"].append(file_entry)
        cycle_units_out = []
        for (bucket, depth1), unit in sorted(cycle_units.items()):
            files = sorted(unit["files"], key=lambda f: f["locator"])
            started_on, started_on_source = _lump_started_on(
                cdir, bucket, depth1, files, record.get("started_on"))
            cycle_units_out.append({
                "cycle_key": f"legacy:{slug}:{bucket}/{depth1}", "bucket": bucket, "depth1_name": depth1,
                "started_on": started_on, "started_on_source": started_on_source, "title": depth1,
                "file_count": len(files), "byte_count": sum(f["byte_size"] or 0 for f in files),
                "files": files,
            })
        lumps.append({
            "lump_cycle_id": cycle_id, "lump_campaign_id": campaign_id, "cycle_dir": str(cdir),
            "lump_manifest_digest": manifest_digest, "report_path": str(report_path),
            "report_sha256": "sha256:" + C._sha(report_path),
            "file_count": len(rows), "byte_count": sum(r.get("byte_size") or 0 for r in rows),
            "cycle_units": cycle_units_out, "shared_input": shared_input, "stage_sessions": stage_sessions,
        })
    body = {"schema_version": 1, "kind": "w7g-lump-inventory", "artifact_root_id": None,
            "sealed_at": None, "root_slug": slug, "lumps": lumps, "invalid": invalid}
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is not None:
        body["artifact_root_id"] = identity.artifact_root_id
    body["digest"] = _canonical_digest(body)
    return body


def _run_dirs(root: Path) -> List[Path]:
    mdir = C.migrations_dir(root)
    if not mdir.is_dir():
        return []
    return sorted((p for p in mdir.iterdir() if p.is_dir() and "-resplit-" in p.name), key=lambda p: p.name)


def _find_run_dir(root: Path, lump_cycle_id: str) -> Optional[Path]:
    suffix = f"-resplit-{lump_cycle_id}"
    matches = sorted((p for p in _run_dirs(root) if p.name.endswith(suffix)), key=lambda p: p.name)
    return matches[-1] if matches else None


def _admission_dir(run_dir: Path) -> Path:
    return run_dir / "admission"


def _marker_path(run_dir: Path) -> Path:
    return run_dir / "admitted.marker.json"


def _bundle_digest(admission_dir: Path) -> str:
    rows = []
    for name in ADMISSION_FILES:
        p = admission_dir / name
        if p.is_file():
            rows.append({"name": name, "sha256": "sha256:" + C._sha(p)})
    rows.sort(key=lambda r: r["name"])
    return P._digest(P._canonical(rows))  # `P._digest` already prefixes `sha256:`


def _valid_admitted_run(root: Path, run_dir: Path) -> Optional[Dict[str, Any]]:
    marker = P._read_json(_marker_path(run_dir))
    if not isinstance(marker, dict) or marker.get("kind") != "w7g-admission-marker":
        return None
    if _bundle_digest(_admission_dir(run_dir)) != marker.get("bundle_digest"):
        return None
    return marker


def lump_index(root: Path, *, root_slug: Optional[str] = None) -> Dict[str, Any]:
    """Sealed lump inventory from the latest verifiably-admitted run, else a fresh scan.

    `root_slug` only reaches the fresh scan -- a sealed inventory already carries
    the slug R1 admitted and is never re-slugged by a later reader.
    """
    root = Path(root).resolve()
    for run_dir in reversed(_run_dirs(root)):
        marker = _valid_admitted_run(root, run_dir)
        if marker is None:
            continue
        inv = P._read_json(_admission_dir(run_dir) / "lump-inventory.json")
        if isinstance(inv, dict) and inv.get("kind") == "w7g-lump-inventory":
            stripped = {k: v for k, v in inv.items() if k != "digest"}
            if _canonical_digest(stripped) == inv.get("digest"):
                return inv
    return scan_lumps(root, root_slug=root_slug)


def sealed_retire_inventory(root: Path) -> Optional[Dict[str, Any]]:
    root = Path(root).resolve()
    identity = P.artifact_lifecycle.read_root_identity(root)
    root_id = identity.artifact_root_id if identity else None
    best = None
    for run_dir in _run_dirs(root):
        if _valid_admitted_run(root, run_dir) is None:
            continue
        inv = P._read_json(_admission_dir(run_dir) / "retire-inventory.json")
        if not isinstance(inv, dict) or inv.get("kind") != "w7g-retire-inventory":
            continue
        if inv.get("artifact_root_id") != root_id:
            continue
        stripped = {k: v for k, v in inv.items() if k != "digest"}
        if _canonical_digest(stripped) != inv.get("digest"):
            continue
        best = inv
    return best


def sealed_loose_inventory(root: Path) -> Optional[Dict[str, Any]]:
    root = Path(root).resolve()
    best = None
    for run_dir in _run_dirs(root):
        if _valid_admitted_run(root, run_dir) is None:
            continue
        inv = P._read_json(_admission_dir(run_dir) / "loose-inventory.json")
        if not isinstance(inv, dict) or inv.get("kind") != "w7g-loose-inventory":
            continue
        stripped = {k: v for k, v in inv.items() if k != "digest"}
        if _canonical_digest(stripped) != inv.get("digest"):
            continue
        best = inv
    return best


def _is_contained(root: Path, candidate: Path) -> bool:
    """True when `candidate` is inside `root` both lexically and after symlinks.

    Two checks, because either alone is defeatable. `normpath` folds a `..` that a
    sealed locator should never contain but might; `realpath` on the deepest existing
    ancestor catches the other direction, where every component is innocent but a parent
    directory is a symlink pointing out of the artifact root. Only the existing prefix is
    resolved so a target that has not been created yet can still be judged.
    """
    root = Path(root).resolve()
    lexical = Path(os.path.normpath(str(candidate)))
    try:
        if os.path.commonpath([str(root), str(lexical)]) != str(root):
            return False
    except ValueError:
        return False
    probe = lexical
    tail = []
    while not probe.exists() and probe.parent != probe:
        tail.insert(0, probe.name)
        probe = probe.parent
    resolved = probe.resolve()
    for part in tail:
        resolved = resolved / part
    try:
        return os.path.commonpath([str(root), str(resolved)]) == str(root)
    except ValueError:
        return False


def stage_sessions_disposed_rows(root: Path) -> set:
    """D-85 extension: retire-inventory rows a C-RT relocation already accounted for.

    The nine roots that finished R1~R4 before this gate existed sealed their
    `plans/stage-sessions` files into the retire inventory, where R4 could never clear
    them: R3 had already deleted the lump copy those rows named as their target, so the
    digest comparison kept them forever and `legacy_top_level_retired` stayed `false`.
    A relocation is the honest disposition for them -- but only when it can still be
    proven, so every one of these holds before a row counts:

      (a) the run directory carries a valid admission marker,
      (b) `stage-sessions-disposition.json` exists, parses, and has the right `kind`,
      (c) its self-digest matches, so the record has not been edited after the fact,
      (d) its `artifact_root_id` is this root's,
      (e) the row's status is `relocated` or `already-relocated` (a
          `target-present-identical` row left the source in place and is deliberately
          *not* an excuse -- see `_stage_session_decide`),
      (f) the row cites the sealed inventory's own `ordinal`, and that entry's
          `source_locator` matches, and
      (g) on disk right now the target is a regular file with the row's digest and the
          source is gone.

    Anything else is dropped silently, which returns that entry to the ordinary R4 path
    rather than granting it a pass.
    """
    root = Path(root).resolve()
    inventory = sealed_retire_inventory(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    if inventory is None or identity is None:
        return set()
    by_ordinal = {e.get("ordinal"): e for e in inventory.get("entries") or []}
    out: set = set()
    for run_dir in _run_dirs(root):
        if _valid_admitted_run(root, run_dir) is None:
            continue
        body = P._read_json(run_dir / STAGE_SESSIONS_DISPOSITION)
        if not isinstance(body, dict) or body.get("kind") != "w7g-stage-sessions-disposition":
            continue
        if body.get("artifact_root_id") != identity.artifact_root_id:
            continue
        if _canonical_digest({k: v for k, v in body.items() if k != "digest"}) != body.get("digest"):
            continue
        for row in body.get("rows") or []:
            if row.get("status") not in {"relocated", "already-relocated"}:
                continue
            entry = by_ordinal.get(row.get("retire_inventory_ordinal"))
            if not entry or entry.get("source_locator") != row.get("source_locator"):
                continue
            # The row's own digest has to be the sealed one. Without this, a disposition
            # could cite ordinal N, carry a digest of its own, satisfy the target check
            # against that digest, and still contribute the *sealed* key to `disposed` --
            # retiring an entry whose bytes were never the ones that moved.
            if entry.get("sha256") != row.get("sha256"):
                continue
            source = root / str(row.get("source_locator") or "")
            target = root / str(row.get("target_locator") or "")
            if not _is_contained(root, target) or not _is_contained(root, source):
                continue
            if source.exists():
                continue
            if not target.is_file() or target.is_symlink():
                continue
            if "sha256:" + C._sha(target) != entry.get("sha256"):
                continue
            out.add((entry["source_locator"], entry.get("sha256")))
    return out


def resplit_hold(root: Path) -> Optional[Dict[str, Any]]:
    """D-77-a: hold is owned by the nonterminal journal on disk, never process liveness."""
    root = Path(root).resolve()
    holds = []
    for run_dir in _run_dirs(root):
        for gate, terminal in (("r2", R2_TERMINAL), ("r3", R3_TERMINAL),
                               ("campaign-supersede", {"complete", "no-op", "hold"}),
                               ("stage-sessions-relocate", {"complete", "no-op", "hold"})):
            journal = P._read_json(run_dir / f"journal-{gate}.json")
            if journal is None:
                continue
            phase = journal.get("phase")
            if phase not in terminal:
                holds.append({
                    "code": "resplit-in-progress", "journal": str(run_dir / f"journal-{gate}.json"),
                    "phase": phase, "gate": gate, "lump_cycle_id": journal.get("lump_cycle_id"),
                    "started_at": journal.get("started_at"),
                })
    if not holds:
        return None
    holds.sort(key=lambda h: h["journal"])
    return holds[0]


RESPLIT_LOCK_NAME = "resplit.lock"


def _resplit_lock_path(root: Path) -> Path:
    return P.producer_dir(root) / RESPLIT_LOCK_NAME


def _run_dir_fully_terminal(run_dir: Path) -> bool:
    """True only once this run_dir's R2 (and, if it got that far, R3) reached a
    terminal phase. A missing/unreadable journal is treated as *not* terminal
    (conservative: it cannot be proven safe to reclaim the lock)."""
    r2_journal = P._read_json(run_dir / "journal-r2.json")
    if not isinstance(r2_journal, dict) or r2_journal.get("phase") not in R2_TERMINAL:
        return False
    if r2_journal.get("phase") == "rolled-back":
        return True
    r3_journal = P._read_json(run_dir / "journal-r3.json")
    return isinstance(r3_journal, dict) and r3_journal.get("phase") in R3_TERMINAL


def _acquire_resplit_lock(root: Path, *, lump_cycle_id: str, run_dir: Path) -> None:
    """D-77-a: root-wide exclusive lock, claimed atomically (O_EXCL), owned from R2
    start through R3 terminal. The nonterminal-journal read in `resplit_hold` stays
    the hold predicate reader/gate consume; this is the actual mutual-exclusion
    primitive so two concurrent processes cannot both pass that read and then both
    mutate."""
    lock_path = _resplit_lock_path(root)
    run_dir_str = str(run_dir)
    body = {
        "schema_version": 1, "kind": "w7g-resplit-lock", "lump_cycle_id": lump_cycle_id,
        "run_dir": run_dir_str, "owner_pid": os.getpid(), "acquired_at": C._now(),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        P._write_exclusive(lock_path, P._json_bytes(body), 0o600)
        return
    except FileExistsError:
        pass
    existing = P._read_json(lock_path)
    if isinstance(existing, dict) and existing.get("run_dir") == run_dir_str:
        return  # this run already owns the lock -- idempotent re-entry (e.g. R2 -> R3)
    existing_run_dir = existing.get("run_dir") if isinstance(existing, dict) else None
    if existing_run_dir and _run_dir_fully_terminal(Path(existing_run_dir)):
        # Journal for the lock holder is terminal (complete or rolled-back) but the
        # lock file itself was left behind -- a stale lock, not a valid hold.
        try:
            lock_path.unlink()
        except OSError:
            pass
        P._write_exclusive(lock_path, P._json_bytes(body), 0o600)
        return
    raise ResplitError("resplit-in-progress", json.dumps(existing or {}))


def _release_resplit_lock_if_done(root: Path, run_dir: Path) -> None:
    lock_path = _resplit_lock_path(root)
    existing = P._read_json(lock_path)
    if not isinstance(existing, dict) or existing.get("run_dir") != str(run_dir):
        return
    if _run_dir_fully_terminal(run_dir):
        try:
            lock_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# D-83 fold consumer (read projection, mutation 0)
# ---------------------------------------------------------------------------


def fold_supersession_events(root: Path) -> Dict[str, Dict[str, Any]]:
    root = Path(root).resolve()
    rows: List[Dict[str, Any]] = []
    for run_dir in _run_dirs(root):
        journal = P._read_json(run_dir / "journal-r3.json")
        if not isinstance(journal, dict) or journal.get("phase") != "complete":
            continue
        for events_path in (run_dir / "events.jsonl", run_dir / "events-campaign.jsonl"):
            if events_path.is_file():
                rows.extend(C._read_jsonl(events_path))
    rows.sort(key=lambda r: (r.get("stream_id") or "", r.get("stream_sequence") or 0))
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row.get("event_type") not in {"cycle.superseded", "campaign.superseded"}:
            continue
        target_id = row.get("target_id")
        if not target_id:
            continue
        payload = row.get("payload") or {}
        out[target_id] = {
            "state": "superseded", "superseded_by": list(payload.get("superseded_by") or []),
            "superseded_event_id": row.get("event_id"), "event_id": row.get("event_id"),
        }
    return out


def lump_display_state(root: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    index = lump_index(root)
    fold = fold_supersession_events(root)
    lumps_out = []
    divergent = []
    remaining = 0
    ok = True
    for lump in index.get("lumps", []):
        cid = lump["lump_cycle_id"]
        record = P.read_cycle_record(root, cid) or {}
        record_state = record.get("state")
        fold_entry = fold.get(cid)
        fold_state = fold_entry["state"] if fold_entry else None
        agrees = (record_state == "superseded") == (fold_state == "superseded")
        # 🟡2: lane① (original pre-W7C path) omission accounting lives only in the
        # r3 journal's pre_image today -- surface it here so an operator does not
        # mistake an incomplete-lane resplit for a normal one without opening the
        # run_dir directly. Mutation stays 0; this is read-only projection.
        omitted_count = 0
        run_dir = _find_run_dir(root, cid)
        if run_dir is not None:
            r3_journal = P._read_json(run_dir / "journal-r3.json")
            if isinstance(r3_journal, dict):
                omitted = (r3_journal.get("pre_image") or {}).get("lane1_omitted_lump_locators") or []
                omitted_count = len(omitted)
        lumps_out.append({
            "lump_cycle_id": cid, "record_state": record_state, "fold_state": fold_state,
            "agrees": agrees, "compat_lane1_incomplete": omitted_count > 0,
            "compat_lane1_omitted_count": omitted_count,
        })
        if not agrees:
            ok = False
            divergent.append({"lump_cycle_id": cid, "record": record_state, "fold": fold_state})
        elif record_state != "superseded":
            remaining += 1
    return {
        "lumps": lumps_out,
        "lumped_cycles_remaining": remaining if ok else None,
        "lump_index_state": "ok" if ok else "supersession-record-event-divergent",
        "divergent": divergent,
    }


DEVIATION_CHECKS = ("started-on", "cycle-state", "capability", "compat-supersession", "backup-location",
                    "campaign-state", "stage-sessions")


def _cycle_key_bucket(cycle_key: Optional[str]) -> Optional[str]:
    if not isinstance(cycle_key, str):
        return None
    tail = cycle_key.split(":", 2)[-1]
    bucket = tail.split("/", 1)[0]
    return bucket if bucket in CYCLE_BUCKETS else None


def _admitted_started_on_overrides(run_dir: Optional[Path], cycle_keys: set) -> Tuple[Dict[str, str], Optional[str]]:
    """The admitted `display-quality` corrections, read back from the sealed proposal.

    The sealed lump inventory keeps the *derived* `started_on` (what the tree said);
    only the admitted proposal carries the correction R1 actually applied. Comparing a
    corrected record against the derived value reports every admitted correction as a
    deviation -- the audit would flag exactly the rows the contract asked for. So when a
    cycle record says `started_on_source == "display-quality-proposal"`, the expectation
    is the value that proposal admitted, not the derived one.

    Returns `(overrides, error_code)`. A missing or unreadable proposal is an error, not
    an empty override map: silently falling back to the derived value would turn absent
    evidence into a pass.
    """
    if run_dir is None:
        return {}, "override-evidence-missing"
    path = _admission_dir(run_dir) / "proposal.json"
    if not path.is_file():
        return {}, "override-evidence-missing"
    proposal = P._read_json(path)
    if not isinstance(proposal, dict) or not isinstance(proposal.get("proposals"), list):
        return {}, "override-evidence-invalid"
    try:
        overrides, err = _display_quality_started_on(proposal, cycle_keys)
    except (KeyError, TypeError):
        return {}, "override-evidence-invalid"
    if err:
        return {}, "override-evidence-invalid"
    return overrides, None


def _campaign_state_rows(root: Path) -> List[Dict[str, Any]]:
    """D-81 predicate, read straight off the side records so it is always evaluable.

    A campaign whose every cycle is `superseded` must itself be `superseded`; a campaign
    with one live cycle must not be. This reads `campaigns/*/campaign.json` and the cycle
    records directly rather than the run journal, because a root that never ran R2 still
    has a campaign state that can be wrong -- and a check that needs a journal reports
    `not-evaluated` there, which is the fail-open shape this audit exists to prevent.
    """
    rows: List[Dict[str, Any]] = []
    campaigns_dir = Path(root) / "campaigns"
    if not campaigns_dir.is_dir():
        return rows
    for entry in sorted(campaigns_dir.iterdir(), key=lambda q: q.name):
        campaign = P._read_json(entry / "campaign.json")
        if not isinstance(campaign, dict):
            continue
        cycle_ids = campaign.get("cycles") or []
        if not cycle_ids:
            continue
        states = []
        unresolved = []
        for cid in cycle_ids:
            record = P.read_cycle_record(root, cid)
            if record is None:
                unresolved.append(cid)
            else:
                states.append(record.get("state"))
        if unresolved:
            # A campaign naming a cycle with no side record cannot be judged either way.
            # Counting the missing one as "live" would quietly hold the campaign at
            # `active` and read as a pass, so the broken reference is the finding.
            rows.append({"campaign_id": campaign.get("campaign_id"), "key": campaign.get("key"),
                         "code": "campaign-cycle-record-missing", "cycle_ids": sorted(unresolved)})
            continue
        expected = "superseded" if all(s == "superseded" for s in states) else "active"
        observed = campaign.get("state")
        if observed != expected:
            rows.append({"campaign_id": campaign.get("campaign_id"), "key": campaign.get("key"),
                         "expected": expected, "observed": observed,
                         "cycle_states": sorted(set(s for s in states if s))})
    return rows


def _stage_session_residue(root: Path) -> List[str]:
    """Top-level `plans/stage-sessions/**` regular files still on disk (D-79 C-RT)."""
    stage_root = Path(root) / "plans" / "stage-sessions"
    if not stage_root.is_dir():
        return []
    return sorted(
        p.relative_to(root).as_posix()
        for p in stage_root.rglob("*") if p.is_file() and not p.is_symlink()
    )


def resplit_deviations(root: Path, *, lump_cycle_id: Optional[str] = None) -> Dict[str, Any]:
    """Read-only audit of what an already-executed resplit run actually produced.

    Seven things this module used to get wrong are invisible once a cycle is
    sealed: the manifest cannot be rewritten, so the only remaining question is
    whether a given run's output matches the contract or is a recorded deviation.
    This answers that question without touching anything -- it is the regression
    surface for the hearting canary, whose 16 cycles stay exactly as they were
    sealed (D-6/D-11) and are read here, never repaired.

    Every check reports `ok`, `deviation`, or `not-evaluated` (its evidence does
    not exist in this run yet); `not-evaluated` is never counted as a pass, which
    is why the CLI's exit code reads `not_evaluated` as well as `deviations`.
    `campaign-state` and `stage-sessions` read side records and the disk, so they
    stay evaluable even when no run directory exists -- a root that never ran R1
    must not look clean.
    """
    root = Path(root).resolve()
    run_dir = _find_run_dir(root, lump_cycle_id) if lump_cycle_id else None
    if run_dir is None:
        candidates = [d for d in _run_dirs(root) if (d / "journal-r2.json").is_file()]
        run_dir = candidates[-1] if candidates else None
    r2 = (P._read_json(run_dir / "journal-r2.json") or {}) if run_dir else {}
    r3 = (P._read_json(run_dir / "journal-r3.json") or {}) if run_dir else {}
    lump_id = r2.get("lump_cycle_id") or lump_cycle_id
    inventory = (P._read_json(_admission_dir(run_dir) / "lump-inventory.json") or {}) if run_dir else {}
    derived_started_on = {}
    for lump in inventory.get("lumps", []):
        for unit in lump.get("cycle_units", []):
            derived_started_on[unit["cycle_key"]] = _as_rfc3339(unit.get("started_on"))
    checks: Dict[str, Dict[str, Any]] = {
        cid: {"id": cid, "status": "not-evaluated", "rows": []} for cid in DEVIATION_CHECKS
    }
    overrides: Dict[str, str] = {}
    override_error: Optional[str] = None
    override_loaded = False
    for cyc in r2.get("cycles", []):
        record = P.read_cycle_record(root, cyc["cycle_id"])
        if record is None:
            continue
        manifest = P._read_json(Path(cyc["cycle_dir"]) / "manifest.json") or {}
        sealed_cycle = manifest.get("cycle") or {}
        key = cyc.get("cycle_key") or record.get("cycle_key")
        got = sealed_cycle.get("started_on") or record.get("started_on")
        if record.get("started_on_source") == "display-quality-proposal":
            if not override_loaded:
                overrides, override_error = _admitted_started_on_overrides(
                    run_dir, set(derived_started_on))
                override_loaded = True
            if override_error:
                checks["started-on"]["rows"].append(
                    {"cycle_id": cyc["cycle_id"], "cycle_key": key, "code": override_error,
                     "observed": got})
                want = None
            else:
                want = overrides.get(key)
                if want is None:
                    checks["started-on"]["rows"].append(
                        {"cycle_id": cyc["cycle_id"], "cycle_key": key,
                         "code": "override-evidence-missing", "observed": got})
        else:
            want = derived_started_on.get(key)
        if want and got and want != got:
            checks["started-on"]["rows"].append(
                {"cycle_id": cyc["cycle_id"], "cycle_key": key, "expected": want, "observed": got})
        if sealed_cycle:
            checks["cycle-state"]["status"] = "ok"
            if sealed_cycle.get("state") != "completed":
                checks["cycle-state"]["rows"].append(
                    {"cycle_id": cyc["cycle_id"], "cycle_key": key, "observed": sealed_cycle.get("state")})
        if want and got:
            checks["started-on"]["status"] = "ok"
        bucket = _cycle_key_bucket(key)
        if bucket:
            checks["capability"]["status"] = "ok"
            want_capability = BUCKET_CAPABILITY[bucket]
            if record.get("capability") != want_capability or record.get("route_capability") != want_capability:
                checks["capability"]["rows"].append({
                    "cycle_id": cyc["cycle_id"], "cycle_key": key, "expected": want_capability,
                    "observed": record.get("capability"),
                    "observed_route_capability": record.get("route_capability")})
    # D-81 / D-79 C-RT: evaluated from side records and disk, never from the journal, so
    # a root with no run directory is still judged instead of reported as clean.
    checks["campaign-state"]["status"] = "ok"
    checks["campaign-state"]["rows"].extend(_campaign_state_rows(root))
    checks["stage-sessions"]["status"] = "ok"
    disposed = {source for source, _sha in stage_sessions_disposed_rows(root)}
    for rel in _stage_session_residue(root):
        if rel in disposed:
            continue
        checks["stage-sessions"]["rows"].append({"code": "stage-session-residue", "source_locator": rel})
    for run in _run_dirs(root):
        disposition = P._read_json(run / "stage-sessions-disposition.json")
        if not isinstance(disposition, dict):
            continue
        for row in disposition.get("rows", []):
            if row.get("status") == "hold":
                checks["stage-sessions"]["rows"].append(
                    {"code": "stage-session-hold", "source_locator": row.get("source_locator"),
                     "target_locator": row.get("target_locator")})
    if run_dir is not None:
        map_path = run_dir / "compatibility-map.jsonl"
        state = C.load_map_state(root)
        recorded = {entry["path"] for entry in state["maps"]}
        if map_path.is_file() and str(map_path) in recorded:
            checks["compat-supersession"]["status"] = "ok"
            new_sources = {row["source_locator"] for row in C._read_jsonl(map_path)}
            for entry in state["maps"]:
                if entry["path"] == str(map_path) or not entry["present"]:
                    continue
                table = {row["source_locator"] for row in C._read_jsonl(Path(entry["path"]))}
                if new_sources.intersection(table) and not entry.get("superseded_by"):
                    checks["compat-supersession"]["rows"].append(
                        {"map": entry["path"], "shared_sources": len(new_sources & table)})
        if r3.get("phase") in {"backup-sealed", "artifacts-removed", "complete"}:
            checks["backup-location"]["status"] = "ok"
            if (run_dir / "legacy-artifacts.tar").is_file():
                checks["backup-location"]["rows"].append(
                    {"code": "backup-inside-artifact-root", "path": str(run_dir / "legacy-artifacts.tar")})
            location = P._read_json(run_dir / "backup-location.json")
            if not isinstance(location, dict) or not location.get("archive"):
                checks["backup-location"]["rows"].append({"code": "backup-location-missing"})
            elif _is_within(root, location["archive"]):
                checks["backup-location"]["rows"].append(
                    {"code": "backup-inside-artifact-root", "path": location["archive"]})
    rows = []
    for cid in DEVIATION_CHECKS:
        check = checks[cid]
        if check["rows"]:
            check["status"] = "deviation"
        rows.append(check)
    deviations = [c["id"] for c in rows if c["status"] == "deviation"]
    not_evaluated = [c["id"] for c in rows if c["status"] == "not-evaluated"]
    return {
        "status": ("no-run" if run_dir is None else ("deviation" if deviations else "ok")),
        "run_dir": str(run_dir) if run_dir else None,
        "lump_cycle_id": lump_id, "checks": rows, "deviations": deviations,
        "not_evaluated": not_evaluated,
    }


# ---------------------------------------------------------------------------
# D-80 proposal validator
# ---------------------------------------------------------------------------


def _rule_schema(proposal: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(proposal, dict):
        return {"code": "proposal-row-schema-invalid", "detail": "not-an-object"}
    for key in ("schema_version", "contract", "root_slug", "source_cutoff", "producer_version",
                "projection_version", "policy_version", "proposals", "campaigns", "loose_assignments"):
        if key not in proposal:
            return {"code": "proposal-row-schema-invalid", "detail": f"missing:{key}"}
    proposals = proposal["proposals"]
    if not isinstance(proposals, list):
        return {"code": "proposal-row-schema-invalid", "detail": "proposals-not-array"}
    ids = set()
    for i, row in enumerate(proposals):
        if not isinstance(row, dict) or set(row.keys()) != PROPOSAL_ROW_KEYS:
            return {"code": "proposal-row-schema-invalid", "detail": f"proposals[{i}]"}
        if row.get("lane") not in LANES:
            return {"code": "proposal-row-schema-invalid", "detail": f"proposals[{i}].lane"}
        ids.add(row.get("proposal_id"))
    for label in ("campaigns", "loose_assignments"):
        for i, row in enumerate(proposal.get(label, [])):
            if not isinstance(row, dict) or row.get("proposal_id") not in ids:
                return {"code": "proposal-backreference-missing", "detail": f"{label}[{i}]"}
    return None


def _rule_1_exactly_once(proposal: Dict[str, Any], lump_cycle_keys: set) -> Optional[Dict[str, Any]]:
    seen: Dict[str, int] = {}
    for row in proposal["proposals"]:
        if row.get("lane") != "semantic-boundary":
            continue
        for key in row.get("target_ids") or []:
            seen[key] = seen.get(key, 0) + 1
    missing = sorted(k for k in lump_cycle_keys if seen.get(k, 0) == 0)
    if missing:
        return {"code": "cycle-assignment-invalid", "detail": f"unassigned:{missing[0]}"}
    dup = sorted(k for k, c in seen.items() if c > 1)
    if dup:
        return {"code": "cycle-assignment-invalid", "detail": f"duplicate:{dup[0]}"}
    extraneous = sorted(k for k in seen if k not in lump_cycle_keys)
    if extraneous:
        return {"code": "cycle-assignment-invalid", "detail": f"unknown:{extraneous[0]}"}
    return None


def _rule_2_and_3(proposal: Dict[str, Any], root_slug: str) -> Optional[Dict[str, Any]]:
    for row in proposal.get("campaigns", []):
        slug = row.get("slug")
        if slug == UNASSIGNED_SLUG:
            if row.get("degraded") is not True:
                return {"code": "unassigned-campaign-invalid", "detail": "degraded-not-true"}
            continue
        if not isinstance(slug, str) or not CAMPAIGN_SLUG_RE.match(slug):
            return {"code": "campaign-slug-invalid", "detail": str(slug)}
    return None


def _normal_campaign(root: Path, root_slug: str, campaign_id: Optional[str] = None,
                     key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    found = P.read_campaign(root, campaign_id) if campaign_id else None
    if found is None and key:
        found = _find_campaign_by_key_local(root, key)
    if found is None:
        return None
    if str(found.get("key", "")).startswith(f"legacy:{root_slug}:"):
        return None
    return found


def _rule_4_no_absorption(proposal: Dict[str, Any], root: Path, root_slug: str) -> Optional[Dict[str, Any]]:
    for row in proposal.get("campaigns", []):
        for rel in row.get("related") or []:
            if not isinstance(rel, dict):
                continue
            if rel.get("kind") == "related":
                continue
            target = _normal_campaign(root, root_slug, rel.get("campaign_id"), rel.get("key"))
            if target is not None:
                return {"code": "campaign-absorption-refused",
                        "detail": str(rel.get("campaign_id") or rel.get("key"))}
    return None


def _rule_5_freshness(proposal: Dict[str, Any], lump: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    top = proposal.get("source_cutoff") or {}
    if top.get("lump_manifest_digest") != lump["lump_manifest_digest"]:
        return {"code": "proposal-stale", "detail": "source_cutoff-global"}
    for row in proposal["proposals"]:
        cutoff = row.get("source_cutoff") or {}
        if cutoff.get("lump_manifest_digest") != lump["lump_manifest_digest"]:
            return {"code": "proposal-stale", "detail": f"source_cutoff:{row.get('proposal_id')}"}
    return None


def _rule_6_evidence_in_cutoff(proposal: Dict[str, Any], evidence_universe: set) -> Optional[Dict[str, Any]]:
    """D-80 rule 6. `evidence_universe` is `RULE_6_EVIDENCE_UNIVERSE_KIND` -- see
    that constant's docstring for why this is a provisional, not contractual,
    definition. Do not change the comparison itself here without first
    updating the constant and its spec-impact note (§7 of the S2 plan)."""
    for row in proposal["proposals"]:
        for ev in row.get("cited_evidence_ids") or []:
            if ev not in evidence_universe:
                return {"code": "evidence-out-of-cutoff", "detail": str(ev)}
    return None


def _rule_7_loose(proposal: Dict[str, Any], loose_inventory: Dict[str, Any],
                  known_cycle_keys: set) -> Optional[Dict[str, Any]]:
    entries_by_locator = {e["source_locator"]: e for e in loose_inventory.get("entries", [])}
    seen: Dict[str, int] = {}
    for row in proposal.get("loose_assignments", []):
        src = row.get("source_locator")
        entry = entries_by_locator.get(src)
        if entry is None:
            return {"code": "loose-assignment-invalid", "detail": f"unknown-source:{src}"}
        cited_sha256 = row.get("sha256")
        if cited_sha256 is not None and cited_sha256 != entry.get("sha256"):
            return {"code": "loose-assignment-invalid", "detail": f"digest-drift:{src}"}
        seen[src] = seen.get(src, 0) + 1
        if row.get("target_cycle_key") not in known_cycle_keys:
            return {"code": "loose-assignment-invalid", "detail": f"unknown-target:{row.get('target_cycle_key')}"}
    dup = sorted(k for k, c in seen.items() if c > 1)
    if dup:
        return {"code": "loose-assignment-invalid", "detail": f"duplicate:{dup[0]}"}
    missing = sorted(k for k in entries_by_locator if seen.get(k, 0) == 0)
    if missing:
        return {"code": "loose-assignment-invalid", "detail": f"unassigned:{missing[0]}"}
    return None


def _rule_8_confirmed_constraints(proposal: Dict[str, Any], confirmed_constraints: Optional[Dict[str, Any]],
                                  campaign_of_cycle_key: Dict[str, str],
                                  loose_target_by_source: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not confirmed_constraints:
        return None
    for constraint in confirmed_constraints.get("constraints", []):
        kind = constraint.get("kind")
        detail = constraint.get("detail") or {}
        if kind == "keep-cycle":
            if campaign_of_cycle_key.get(detail.get("cycle_key")) != detail.get("campaign_slug"):
                return {"code": "confirmed-decision-conflict", "detail": constraint.get("id", kind)}
        elif kind == "no-split":
            slugs = {campaign_of_cycle_key.get(k) for k in detail.get("cycle_keys", [])}
            if len(slugs) > 1:
                return {"code": "confirmed-decision-conflict", "detail": constraint.get("id", kind)}
        elif kind == "no-absorb":
            pass
        elif kind == "loose-target":
            if loose_target_by_source.get(detail.get("source_locator")) != detail.get("target_cycle_key"):
                return {"code": "confirmed-decision-conflict", "detail": constraint.get("id", kind)}
        elif kind == "require-retire":
            pass
    return None


def _apply_started_on_override(unit: Dict[str, Any], overrides: Dict[str, str]) -> Dict[str, Any]:
    """Return the cycle unit R2 will build from. The sealed lump inventory keeps the
    derived value; only the verdict's copy carries the admitted correction, so the
    inventory stays a record of what the tree said and the verdict a record of what
    was admitted."""
    value = overrides.get(unit["cycle_key"])
    if value is None:
        return unit
    return {**unit, "started_on": value, "started_on_source": "display-quality-proposal"}


def _display_quality_started_on(proposal: Dict[str, Any], lump_cycle_keys: set,
                                ) -> Tuple[Dict[str, str], Optional[Dict[str, Any]]]:
    """D-17/D-79: the only lane allowed to correct a derived `started_on`.

    D-79 fixes the derivation (directory prefix -> entry-document date -> lump
    record) and then says display improvements move through a `display-quality`
    proposal. A unit whose directory carries no date and whose entry document
    records the wrong one is exactly that case, so the row is applied here -- at
    R1 admit, onto the sealed cycle unit -- rather than being noticed after R2 has
    already sealed a manifest that can never be rewritten.

    Everything about the row is checked by code (D-7): the target must be a lump
    cycle key, the value must be a date or an RFC3339 instant, and two rows may
    not disagree about one key. Agent output picks the value; it never picks which
    cycle it lands on or whether the value is well formed.
    """
    overrides: Dict[str, str] = {}
    for row in proposal["proposals"]:
        if row.get("lane") != "display-quality":
            continue
        proposed = row.get("proposed_value") or {}
        if not isinstance(proposed, dict) or "started_on" not in proposed:
            continue
        widened = _as_rfc3339(proposed.get("started_on"))
        if widened is None:
            return {}, {"code": "display-quality-invalid",
                        "detail": f"started_on:{row.get('proposal_id')}:{proposed.get('started_on')}"}
        targets = row.get("target_ids") or []
        if not targets:
            return {}, {"code": "display-quality-invalid", "detail": f"no-target:{row.get('proposal_id')}"}
        for key in targets:
            if key not in lump_cycle_keys:
                return {}, {"code": "display-quality-invalid", "detail": f"unknown-target:{key}"}
            if overrides.get(key, widened) != widened:
                return {}, {"code": "display-quality-invalid", "detail": f"conflict:{key}"}
            overrides[key] = widened
    return overrides, None


def validate_proposal(
    proposal: Any, *, root: Path, lump_inventory: Dict[str, Any], loose_inventory: Dict[str, Any],
    confirmed_constraints: Optional[Dict[str, Any]] = None, lump_cycle_id: Optional[str] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    rules_checked: List[str] = ["schema"]
    err = _rule_schema(proposal)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    lumps = lump_inventory.get("lumps", [])
    if lump_cycle_id is not None:
        lumps = [l for l in lumps if l["lump_cycle_id"] == lump_cycle_id]
    if len(lumps) != 1:
        return {"verdict": "hold", "code": "resplit-stale", "detail": "lump-not-uniquely-identified",
                "rules_checked": rules_checked}
    lump = lumps[0]
    root_slug = proposal.get("root_slug") or lump_inventory.get("root_slug") or _default_root_slug(root)
    lump_cycle_keys = {u["cycle_key"] for u in lump["cycle_units"]}
    rules_checked.append("1")
    err = _rule_1_exactly_once(proposal, lump_cycle_keys)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    rules_checked.append("2")
    err = _rule_2_and_3(proposal, root_slug)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    rules_checked.append("4")
    err = _rule_4_no_absorption(proposal, root, root_slug)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    rules_checked.append("5")
    err = _rule_5_freshness(proposal, lump)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    rules_checked.append("6")
    # universe kind = RULE_6_EVIDENCE_UNIVERSE_KIND -- see that constant's docstring
    evidence_universe = {f["locator"] for u in lump["cycle_units"] for f in u["files"]}
    err = _rule_6_evidence_in_cutoff(proposal, evidence_universe)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    rules_checked.append("7")
    err = _rule_7_loose(proposal, loose_inventory, lump_cycle_keys | {f"legacy:{root_slug}:{UNASSIGNED_SLUG}"})
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    # resolve campaign_of_cycle_key for rule 8 + downstream R2 consumption
    campaign_by_proposal_id: Dict[str, Dict[str, Any]] = {}
    slug_for_target: Dict[str, str] = {}
    for row in proposal["proposals"]:
        if row.get("lane") != "semantic-boundary":
            continue
        camp_row = next((c for c in proposal.get("campaigns", []) if c.get("proposal_id") == row.get("proposal_id")), None)
        slug = camp_row.get("slug") if camp_row else None
        if slug is None:
            continue
        for key in row.get("target_ids") or []:
            slug_for_target[key] = slug
    # D-80 rule 1 closure: "배정" means placed in a campaign, not merely named. A
    # `semantic-boundary` row with no backing `campaigns[]` entry passes the
    # target_ids count above while contributing no campaign, so its cycle units
    # would be dropped from `campaigns_out` -- an "ok" verdict under which the lump
    # is never fully resplit and D-78's conservation invariant cannot hold.
    unplaced = sorted(k for k in lump_cycle_keys if k not in slug_for_target)
    if unplaced:
        return {"verdict": "hold", "code": "cycle-assignment-invalid",
                "detail": f"unplaced:{unplaced[0]}", "rules_checked": rules_checked}
    loose_target_by_source = {
        row["source_locator"]: row["target_cycle_key"] for row in proposal.get("loose_assignments", [])
    }
    rules_checked.append("8")
    err = _rule_8_confirmed_constraints(proposal, confirmed_constraints, slug_for_target, loose_target_by_source)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    rules_checked.append("display-quality")
    started_on_overrides, err = _display_quality_started_on(proposal, lump_cycle_keys)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    campaigns_out = []
    for camp_row in proposal.get("campaigns", []):
        slug = camp_row["slug"]
        units = [_apply_started_on_override(u, started_on_overrides)
                 for u in lump["cycle_units"] if slug_for_target.get(u["cycle_key"]) == slug]
        campaigns_out.append({
            "slug": slug, "title": camp_row.get("title"), "goal": camp_row.get("goal"),
            "degraded": bool(camp_row.get("degraded")), "related": camp_row.get("related") or [],
            "cycle_units": units,
        })
    loose_out = [
        {"source_locator": row["source_locator"], "target_cycle_key": row["target_cycle_key"],
         "origin_bucket": row.get("origin_bucket")}
        for row in proposal.get("loose_assignments", [])
    ]
    return {
        "verdict": "ok", "code": None, "rules_checked": rules_checked,
        "campaigns": campaigns_out, "assignments": slug_for_target, "loose": loose_out,
        "started_on_overrides": started_on_overrides,
        "root_slug": root_slug, "lump": lump,
    }


# 🔴5 spec-impact (D-79 l.3027 vs D-80 rule 3 l.3076): the PRD says an
# unplaceable loose file `_unassigned 캠페인의 해당 사이클로 간다` -- it assumes
# that campaign has a cycle to receive it. D-80 rule 3 says `_unassigned` holds
# only cycles whose goal could not be determined, so when every lump cycle unit
# *was* placed (A-16.1's `_unassigned` 0, and the measured hearting proposal)
# that campaign has no cycle and the destination D-79 names does not exist. The
# two sentences cannot both be satisfied. This module resolves the tension by
# deferring: no synthetic loose-only cycle is invented, no empty campaign record
# is minted, and the file is left byte-identical in place with a typed
# `loose-deferred` result carried in the journal and every gate's return value.
# That choice is this module's, not the contract's. The alternative -- minting a
# loose-only cycle under `_unassigned` -- would put a non-D-23 shape in the cycle
# population, which D-79 forbids two paragraphs earlier. Resolving this needs a
# PRD amendment saying which reading wins; until then a deferred file stays a
# retire candidate (it is deliberately NOT added to the D-84 exclude list) so it
# can never be silently lost.
LOOSE_DEFERRAL_REASONS = (
    "unassigned-campaign-has-no-cycle",  # target is the reserved campaign key, which names no cycle
    "target-cycle-not-produced",         # target names no cycle this run will create
    "unmanifestable-locator",            # the post-move locator cannot be a D-6 locator
)


def loose_plan(verdict: Dict[str, Any], loose_inventory: Dict[str, Any]) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """D-79 "잔여 loose 파일": split an admitted proposal's `loose_assignments[]`
    into the rows R2 actually relocates and the rows it must leave alone.

    A row is relocatable only when (a) its `target_cycle_key` names a cycle this
    run will actually create, and (b) the post-move locator can be a D-6 locator.

    (a) is `verdict["assignments"]`, not the lump inventory's cycle units. A unit
    can pass rule 1 (its key appears in some `semantic-boundary` row's
    `target_ids`) and still never be produced, because the row that claimed it has
    no backing `campaigns[]` entry. Classifying against the lump would then mark
    the row relocatable, R1 would drop it from the D-84 retire inventory, and R2
    would silently never move it -- a top-level file that is neither relocated nor
    retirable, with `legacy_top_level_retired` reporting `true` over it.

    (b) matters only for loose files. Every lump-origin locator is already a row in
    the lump's validated manifest, so it is manifestable by construction; a loose
    file is an arbitrary path off the artifact root and may carry a hidden
    component or a non-locator character. Renaming one in and only discovering that
    at `finalize` is unrecoverable past the batch's first commit: the cycle raises
    `output-invalid`/`manifest-invalid` forever, `_r2_roll_forward` is the only
    remaining path and cannot roll back, and the root stays under
    `resplit-in-progress` with the file gone from its original path. So the check
    happens here, before R1 seals anything.

    R1 (retire-inventory excludes) and R2 (the renames) both call this, so the two
    gates can never disagree about which loose files are leaving the top level.
    """
    entries_by_locator = {e["source_locator"]: e for e in loose_inventory.get("entries", [])}
    produced_cycle_keys = set(verdict.get("assignments") or {})
    unassigned_key = f"legacy:{verdict['root_slug']}:{UNASSIGNED_SLUG}"
    by_cycle_key: Dict[str, List[Dict[str, Any]]] = {}
    deferred: List[Dict[str, Any]] = []

    def defer(src, target_key, entry, row, reason, detail=None):
        out = {
            "status": "loose-deferred", "source_locator": src, "target_cycle_key": target_key,
            "origin_bucket": entry.get("origin_bucket") or row.get("origin_bucket"),
            "reason": reason,
        }
        if detail is not None:
            out["detail"] = detail
        deferred.append(out)

    for row in verdict.get("loose", []):
        src = row["source_locator"]
        target_key = row["target_cycle_key"]
        entry = entries_by_locator.get(src, {})
        if target_key not in produced_cycle_keys:
            defer(src, target_key, entry, row,
                  "unassigned-campaign-has-no-cycle" if target_key == unassigned_key
                  else "target-cycle-not-produced")
            continue
        locator = f"{LOOSE_PREFIX}/{src}"
        unmanifestable = P._unmanifestable_reason("artifacts/" + locator)
        if unmanifestable is not None:
            defer(src, target_key, entry, row, "unmanifestable-locator", unmanifestable)
            continue
        by_cycle_key.setdefault(target_key, []).append({
            "source_locator": src,
            "locator": locator,
            "sha256": entry.get("sha256"),
            "byte_size": entry.get("byte_size"),
            "origin_bucket": entry.get("origin_bucket") or row.get("origin_bucket"),
        })
    for rows in by_cycle_key.values():
        rows.sort(key=lambda r: r["source_locator"])
    deferred.sort(key=lambda r: r["source_locator"])
    return by_cycle_key, deferred


def campaign_proposal_validate(
    root: Path, *, proposal_path: Path, lump_inventory_path: Optional[Path] = None,
    loose_inventory_path: Optional[Path] = None, confirmed_constraints_path: Optional[Path] = None,
    lump_cycle_id: Optional[str] = None,
) -> Dict[str, Any]:
    """CLI surface: zero side effects, including under `.runtime/`."""
    root = Path(root).resolve()
    proposal = json.loads(Path(proposal_path).read_text(encoding="utf-8"))
    lump_inventory = (
        P._read_json(Path(lump_inventory_path)) if lump_inventory_path else scan_lumps(root, root_slug=proposal.get("root_slug"))
    )
    loose_inventory = (
        P._read_json(Path(loose_inventory_path)) if loose_inventory_path else _build_loose_inventory(root)
    )
    confirmed = P._read_json(Path(confirmed_constraints_path)) if confirmed_constraints_path else None
    return validate_proposal(
        proposal, root=root, lump_inventory=lump_inventory, loose_inventory=loose_inventory,
        confirmed_constraints=confirmed, lump_cycle_id=lump_cycle_id,
    )


# ---------------------------------------------------------------------------
# R1 -- proposal admit + inventory sealing
# ---------------------------------------------------------------------------


def _build_loose_inventory(root: Path) -> Dict[str, Any]:
    excluded_top = _loose_excluded_top_names()
    entries: List[Tuple[str, Path]] = []
    for item in sorted(root.iterdir(), key=lambda p: p.name):
        name = item.name
        if name.startswith(".") or name == "_scratch":
            continue
        if item.is_symlink():
            continue
        if item.is_file():
            entries.append((name, item))
            continue
        if item.is_dir() and name not in excluded_top:
            for f in P._walk_files(item):
                if f.is_symlink() or not f.is_file():
                    continue
                rel = f.relative_to(root).as_posix()
                if any(rel == s or rel.startswith(s + "/") for s in C.SEALED_EVIDENCE_PATHS):
                    continue
                entries.append((rel, f))
    entries.sort(key=lambda t: t[0])
    rows = []
    for i, (rel, path) in enumerate(entries):
        rows.append({
            "ordinal": i, "source_locator": rel, "origin_bucket": rel.split("/", 1)[0],
            "sha256": "sha256:" + C._sha(path), "byte_size": path.stat().st_size, "kind": "file",
        })
    body = {"schema_version": 1, "kind": "w7g-loose-inventory", "sealed_at": C._now(),
            "entries": rows, "entry_count": len(rows)}
    body["digest"] = _canonical_digest(body)
    return body


def _is_within(root: Path, raw_path: str) -> bool:
    try:
        Path(raw_path).resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _build_retire_inventory(root: Path, extra_excludes: Sequence[str], identity) -> Dict[str, Any]:
    state = C.load_map_state(root)
    present_maps = [e for e in state["maps"] if e["present"]]
    excludes = list(C.RETIRE_DEFAULT_EXCLUDES) + list(extra_excludes) + [
        Path(e["path"]).resolve().relative_to(root).as_posix() for e in present_maps if _is_within(root, e["path"])
    ]
    sources: Dict[str, List[str]] = {}
    for e in present_maps:
        for row in C._read_jsonl(Path(e["path"])):
            if row.get("kind", "file") != "file":
                continue
            src = row["source_locator"]
            if C._excluded(src, excludes):
                continue
            sources.setdefault(src, []).append(row["target_locator"])
    entries = []
    for src in sorted(sources):
        path = root / src
        if path.is_symlink() or not path.is_file():
            continue
        digest = "sha256:" + C._sha(path)
        match = None
        for target in reversed(sources[src]):
            tpath = root / target
            if tpath.is_file() and not tpath.is_symlink() and "sha256:" + C._sha(tpath) == digest:
                match = target
                break
        if match is None:
            continue
        entries.append({"source_locator": src, "target_locator": match, "sha256": digest,
                        "byte_size": path.stat().st_size})
    entries.sort(key=lambda e: e["source_locator"])
    for i, e in enumerate(entries):
        e["ordinal"] = i
    entries = [{k: e[k] for k in ("ordinal", "source_locator", "target_locator", "sha256", "byte_size")}
              for e in entries]
    body = {
        "schema_version": 1, "kind": "w7g-retire-inventory",
        "artifact_root_id": identity.artifact_root_id if identity else None,
        "sealed_at": C._now(), "existence_filter": "applied-at-seal",
        "map_files": [{"path": e["path"], "sha256": e["sha256"]} for e in present_maps],
        "excludes": sorted(set(excludes)), "entries": entries, "entry_count": len(entries),
    }
    body["digest"] = _canonical_digest(body)
    return body


def _sealed_stage_session_sources(root: Path, inventory: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resolve the sealed lump stage-session rows to their original legacy paths."""
    inverted = _original_legacy_sources(root)
    rows: List[Dict[str, Any]] = []
    for lump in inventory.get("lumps", []):
        lump_dir = Path(lump.get("cycle_dir", ""))
        lump_rel = os.path.relpath(lump_dir, root)
        for ordinal, item in enumerate(lump.get("stage_sessions", [])):
            target = f"{lump_rel}/artifacts/{item.get('locator', '')}"
            source = inverted.get(target)
            if not source or not source.startswith("plans/stage-sessions/"):
                continue
            path = root / source
            if path.is_file() and not path.is_symlink():
                rows.append({"source_locator": source, "sha256": "sha256:" + C._sha(path),
                             "byte_size": path.stat().st_size, "lump_cycle_id": lump.get("lump_cycle_id"),
                             "ordinal": ordinal})
    return sorted(rows, key=lambda r: r["source_locator"])


def _hidden_residue(root: Path) -> List[Dict[str, Any]]:
    """Report-only: top-level bucket files whose locator can never be a D-6 locator.

    `artifact_manifest` rejects any path component starting with a dot, so a file like
    `experiments/<unit>/.visual-harness/report.png` cannot enter a cycle manifest and
    was never copied into the lump -- which also keeps it out of the compat map and out
    of the retire inventory. It is therefore invisible to every existing predicate. R1
    names it here so an operator sees it, and nothing moves or deletes it: which
    contract owns this residue is still open (PRD §29.3).
    """
    rows: List[Dict[str, Any]] = []
    for bucket in CYCLE_BUCKETS:
        base = Path(root) / bucket
        if not base.is_dir():
            continue
        for path in sorted(P._walk_files(base)):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if not any(part.startswith(".") for part in rel.split("/")):
                continue
            rows.append({"locator": rel, "byte_size": path.stat().st_size,
                         "reason": "hidden-component-unmanifestable"})
    return rows


def _write_self_digest(path: Path, body: Dict[str, Any]) -> None:
    payload = dict(body)
    payload["digest"] = _canonical_digest({k: v for k, v in payload.items() if k != "digest"})
    P._write_atomic(path, P._json_bytes(payload))


def _r1(root: Path, *, lump_cycle_id: str, proposal_path: Path,
       confirmed_constraints_path: Optional[Path], dry_run: bool,
       crash_after_phase: Optional[str], root_slug: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    C._require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    proposal_bytes = Path(proposal_path).read_bytes()
    plan_sha256 = "sha256:" + hashlib.sha256(proposal_bytes).hexdigest()
    proposal = json.loads(proposal_bytes.decode("utf-8"))
    # An explicit `--root-slug` is only the fallback: a proposal that declares its
    # own `root_slug` stays authoritative, because every campaign key in the package
    # was already sealed against that slug.
    effective_slug = proposal.get("root_slug") or root_slug
    if dry_run:
        lump_inventory = scan_lumps(root, root_slug=effective_slug)
        loose_inventory = _build_loose_inventory(root)
        confirmed = P._read_json(Path(confirmed_constraints_path)) if confirmed_constraints_path else None
        verdict = validate_proposal(proposal, root=root, lump_inventory=lump_inventory,
                                    loose_inventory=loose_inventory, confirmed_constraints=confirmed,
                                    lump_cycle_id=lump_cycle_id)
        result = {"status": "dry-run", "plan_sha256": plan_sha256, **verdict}
        if verdict["verdict"] == "ok":
            _, deferred = loose_plan(verdict, loose_inventory)
            result["loose_deferred"] = deferred
        return result
    run_dir = _find_run_dir(root, lump_cycle_id) or (C.migrations_dir(root) / f"{C._stamp()}-resplit-{lump_cycle_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    journal_path = run_dir / "journal-r1.json"
    journal = P._read_json(journal_path)
    if journal is not None and journal.get("plan_sha256") != plan_sha256:
        raise ResplitError("resplit-stale", "plan_sha256-mismatch")
    if journal is not None and journal.get("phase") == "admitted":
        return {"status": "already-applied", "run_dir": str(run_dir), **journal}
    if journal is None:
        journal = {
            "schema_version": 1, "kind": "w7g-resplit-journal", "gate": "r1", "plan_sha256": plan_sha256,
            "lump_cycle_id": lump_cycle_id, "artifact_root_id": identity.artifact_root_id if identity else None,
            "run_dir": str(run_dir), "phase": "prepared", "started_at": C._now(),
        }
        P._write_atomic(journal_path, P._json_bytes(journal))
    if crash_after_phase == "prepared":
        raise ResplitError("crash-fixture", "prepared")
    lump_inventory = scan_lumps(root, root_slug=effective_slug)
    loose_inventory = _build_loose_inventory(root)
    confirmed = P._read_json(Path(confirmed_constraints_path)) if confirmed_constraints_path else None
    verdict = validate_proposal(proposal, root=root, lump_inventory=lump_inventory,
                                loose_inventory=loose_inventory, confirmed_constraints=confirmed,
                                lump_cycle_id=lump_cycle_id)
    if verdict["verdict"] == "hold":
        journal.update({"hold": verdict})
        P._write_atomic(journal_path, P._json_bytes(journal))
        return {"status": "hold", "run_dir": str(run_dir), **verdict}
    journal["phase"] = "proposal-validated"
    P._write_atomic(journal_path, P._json_bytes(journal))
    if crash_after_phase == "proposal-validated":
        raise ResplitError("crash-fixture", "proposal-validated")
    admission_dir = _admission_dir(run_dir)
    admission_dir.mkdir(parents=True, exist_ok=True)
    # D-84 + D-79: a loose file R2 is about to relocate must not also be listed as a
    # retire target -- after the rename its top-level path is gone, and a retire
    # inventory that still names it would report a permanently unsatisfiable entry.
    # Deferred rows are *not* excluded: they stay in place and remain retirable.
    loose_assigned, loose_deferred = loose_plan(verdict, loose_inventory)
    relocating = sorted(row["source_locator"] for rows in loose_assigned.values() for row in rows)
    journal["loose_relocating"] = relocating
    journal["loose_deferred"] = loose_deferred
    stage_rows = _sealed_stage_session_sources(root, lump_inventory)
    stage_sources = [r["source_locator"] for r in stage_rows]
    retire_inventory = _build_retire_inventory(root, relocating + stage_sources, identity)
    P._write_atomic(admission_dir / "lump-inventory.json", P._json_bytes(lump_inventory))
    P._write_atomic(admission_dir / "loose-inventory.json", P._json_bytes(loose_inventory))
    P._write_atomic(admission_dir / "retire-inventory.json", P._json_bytes(retire_inventory))
    disposition = {"schema_version": 1, "kind": "w7g-r1-disposition",
                   "artifact_root_id": identity.artifact_root_id if identity else None,
                   "stage_sessions": stage_rows, "hidden_residue": _hidden_residue(root)}
    _write_self_digest(admission_dir / "r1-disposition.json", disposition)
    journal["phase"] = "inventories-sealed"
    P._write_atomic(journal_path, P._json_bytes(journal))
    if crash_after_phase == "inventories-sealed":
        raise ResplitError("crash-fixture", "inventories-sealed")
    P._write_atomic(admission_dir / "proposal.json", proposal_bytes)
    receipt = {
        "schema_version": 1, "kind": "w7g-admission-receipt", "recorded_at": C._now(),
        "artifact_root_id": identity.artifact_root_id if identity else None, "plan_sha256": plan_sha256,
        "lump_inventory_digest": lump_inventory["digest"], "loose_inventory_digest": loose_inventory["digest"],
        "retire_inventory_digest": retire_inventory["digest"],
    }
    P._write_atomic(admission_dir / "admission-receipt.json", P._json_bytes(receipt))
    events = [{"actor": {"kind": "curator-proposal-accepted"}, "recorded_at": C._now(), "plan_sha256": plan_sha256}]
    C._write_jsonl(admission_dir / "admission-events.jsonl", events)
    bundle_digest = _bundle_digest(admission_dir)
    journal["phase"] = "bundle-staged"
    P._write_atomic(journal_path, P._json_bytes(journal))
    if crash_after_phase == "bundle-staged":
        raise ResplitError("crash-fixture", "bundle-staged")
    marker_path = _marker_path(run_dir)
    if marker_path.is_file():
        existing_marker = P._read_json(marker_path)
        if not existing_marker or existing_marker.get("plan_sha256") != plan_sha256:
            raise ResplitError("resplit-stale", "marker-plan-mismatch")
    else:
        marker = {"schema_version": 1, "kind": "w7g-admission-marker", "plan_sha256": plan_sha256,
                  "bundle_digest": bundle_digest, "published_at": C._now()}
        P._write_exclusive(marker_path, P._json_bytes(marker), 0o600)
    journal["phase"] = "admitted"
    P._write_atomic(journal_path, P._json_bytes(journal))
    return {"status": "admitted", "run_dir": str(run_dir), "bundle_digest": bundle_digest,
            "lump_cycle_id": lump_cycle_id, "verdict": verdict,
            "loose_relocating": relocating, "loose_deferred": loose_deferred}


# ---------------------------------------------------------------------------
# R2 -- additive admit of new cycles
# ---------------------------------------------------------------------------


def _campaign_plan_from_verdict(verdict: Dict[str, Any], root_slug: str) -> List[Dict[str, Any]]:
    plan = []
    for camp in verdict["campaigns"]:
        plan.append({
            "slug": camp["slug"], "title": camp.get("title") or camp["slug"],
            "goal": camp.get("goal") or "W7G retrospective resplit campaign",
            "degraded": camp.get("degraded", False), "related": camp.get("related") or [],
            "cycle_units": camp["cycle_units"], "key": f"legacy:{root_slug}:{camp['slug']}",
        })
    plan.sort(key=lambda c: c["slug"])
    return plan


def _partition_campaign_plan(campaign_plan: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split the plan into campaigns that get a record and campaigns that do not.

    A campaign no cycle unit was assigned to has nothing to contain. Creating its
    `campaign.json` anyway would mint a permanently empty canonical record -- in
    practice the reserved `_unassigned` container, which D-80 rule 3 admits only as
    a degraded fallback for cycles that could not be placed. Any loose row that
    pointed at it is already `loose-deferred` (see `loose_plan`), so skipping the
    record leaves nothing unowned. The dry-run preview and the real prepare share
    this function so they can never report different campaigns.
    """
    deferred = [
        {"slug": c["slug"], "key": c["key"], "status": "campaign-not-created",
         "reason": "no-cycle-units", "degraded": bool(c.get("degraded"))}
        for c in campaign_plan if not c["cycle_units"]
    ]
    kept = [c for c in campaign_plan if c["cycle_units"]]
    return kept, deferred


RESPLIT_SEAL_NODE_ID = "resplit-seal"
RESPLIT_SEAL_GATE = "inline-complete"


def _resplit_seal_node() -> Dict[str, Any]:
    """The synthetic route's only node: one inline, dispatch-depth-0 terminal seal.

    The caller's own node graph is not copied. A `standard+` caller carries
    depth-2 nodes and sealed parallel groups, and reusing them would make the
    per-cycle route's completion gate depend on markers and arbitration records
    that belong to the resplit's own work, not to the cycle being sealed. One
    inline terminal node grants strictly less than the caller's route already
    granted (D-77-b `authority`) and is the whole reason the route exists: to give
    `finalize` a distinct closed route to bind the commit to.
    """
    return {
        "id": RESPLIT_SEAL_NODE_ID, "kind": "capability-owner", "role": "orchestrator",
        "dispatch_depth": 0, "execution_surface": "inline", "registered_worker": False,
        "resource_class": "normal", "terminal": True,
        "completion_gate": RESPLIT_SEAL_GATE, "terminal_gate": RESPLIT_SEAL_GATE,
        "write_scope": ["source-scoped"],
    }


def ledger_resplit_routes(root: Path) -> Dict[str, Dict[str, Any]]:
    """`resplit_cycle_key` -> the route the canonical ledger already holds for it.

    Read once per batch rather than once per cycle: a mature root's
    `.runtime/routes/` holds four figures of route records (hearting: 1,112), and
    a per-cycle rescan turns idempotency into an O(cycles x routes) file sweep.
    """
    routes_dir = Path(root) / ".runtime" / "routes"
    mapping: Dict[str, Dict[str, Any]] = {}
    if not routes_dir.is_dir():
        return mapping
    for path in sorted(routes_dir.glob("*.json")):
        if path.name.endswith(".outcome.json"):
            continue
        record = P._read_json(path)
        if isinstance(record, dict) and isinstance(record.get("resplit_cycle_key"), str):
            mapping.setdefault(record["resplit_cycle_key"], record)
    return mapping


def _find_ledger_route_by_cycle_key(root: Path, cycle_key: str) -> Optional[Dict[str, Any]]:
    """D-77-b: idempotency is carried by the `cycle_key` -> ledger mapping, not by a
    reproducible id. A rerun looks the key up in the canonical route ledger and
    reuses the route already admitted for it."""
    return ledger_resplit_routes(root).get(cycle_key)


def _write_run_dir_route_log(run_dir: Path, derived: Dict[str, Any]) -> Path:
    """The run_dir copy is an execution log, not route identity (D-77-b)."""
    path = run_dir / "routes" / f"{derived['route_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        P._write_atomic(path, P._json_bytes(derived))
    return path


def _per_cycle_route(root: Path, route: Dict[str, Any], cycle_key: str, capability: str,
                     run_dir: Path, ledger: Optional[Dict[str, Dict[str, Any]]] = None,
                     ) -> Tuple[Dict[str, Any], Path, bool]:
    """D-77-b per-cycle route identity: an opaque id, admitted to the canonical ledger.

    D-5 defines a route as `(artifact_root_id, route_id)` and D-71 makes `finalize`
    the only publication boundary, so a batch that seals several cycles under one
    caller route has no distinct identity per commit. The route issued here fixes
    that, and three properties are contractual rather than incidental:

    - **identity is entropy, not derivation.** The id comes from `os.urandom`. The
      previous `sha256(caller_route_id ‖ cycle_key)[:16]` seeded identity from a
      `cycle_key` that embeds `<bucket>/<depth1-name>`, which is a path -- D-4
      forbids that transitively, not just directly.
    - **the ledger owns it.** `admit_runtime_route` creates
      `.runtime/routes/<route_id>.json` with O_EXCL, so D-5's uniqueness is
      enforced by the same index every other route goes through. The `<run_dir>`
      copy stays, as an execution log.
    - **idempotency is the key's job.** A rerun finds the route by
      `resplit_cycle_key` instead of recomputing an id.

    Returns `(route, canonical route file, created_by_this_run)`.
    """
    ledger = ledger_resplit_routes(root) if ledger is None else ledger
    existing = ledger.get(cycle_key)
    if existing is not None:
        _write_run_dir_route_log(run_dir, existing)
        return existing, P.artifact_lifecycle.canonical_route_path(root, existing["route_id"]), False
    digest = hashlib.sha256(os.urandom(32)).hexdigest()
    derived = dict(route)
    derived["route_id"] = "rt-" + digest[:16]
    derived["route_hash"] = "sha256:" + digest
    derived["capability"] = capability
    derived["effective_intensity"] = RESPLIT_CYCLE_INTENSITY
    derived["requested_intensity"] = RESPLIT_CYCLE_INTENSITY
    derived["nodes"] = [_resplit_seal_node()]
    derived["completion_gates"] = [RESPLIT_SEAL_GATE]
    derived["parallel_groups"] = []
    derived["human_gates"] = []
    derived["human_gate_bindings"] = []
    derived["conditional_extensions"] = []
    derived["max_dispatch_depth"] = 0
    derived["execution_topology"] = "inline"
    derived["resplit_cycle_key"] = cycle_key
    binding = P.artifact_lifecycle.admit_runtime_route(root, derived)
    ledger[cycle_key] = derived
    _write_run_dir_route_log(run_dir, derived)
    return derived, Path(binding.route_file), True


def _cycle_seal_evidence(run_dir: Path, cyc: Dict[str, Any], route_id: str, lump_cycle_id: str) -> Path:
    """Completion evidence for the synthetic route's one gate. Deterministic bytes:
    the marker replay check compares the evidence digest, so a rerun must produce
    the same file rather than a new one that reads as a different attempt."""
    path = run_dir / "routes" / f"{route_id}.completion-evidence.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": 1, "kind": "w7g-resplit-cycle-seal", "cycle_key": cyc["cycle_key"],
            "cycle_id": cyc["cycle_id"], "campaign_id": cyc["campaign_id"],
            "lump_cycle_id": lump_cycle_id, "run_dir": str(run_dir)}
    if not path.is_file():
        P._write_atomic(path, P._json_bytes(body))
    return path


def _close_cycle_route(root: Path, run_dir: Path, cyc: Dict[str, Any], lump_cycle_id: str) -> None:
    """Close the cycle's own route before `finalize` so the sealed manifest says
    `completed` (D-10) instead of the provisional `active` `build_manifest` records
    for an open route.

    The work these cycles describe finished years before the resplit ran; a
    retrospective seal that reports it as still active is simply wrong, and the
    manifest is committed with O_EXCL and can never be corrected afterwards. So the
    closure happens on this side of the commit point: write the terminal node's
    completion marker, close the route through the ordinary outcome-sidecar
    contract, and refuse to continue if the gate did not actually prove.
    """
    record = P.read_cycle_record(root, cyc["cycle_id"])
    if record is None or record.get("state") != "open":
        return
    route_file = Path(record["route_file"])
    route = P.load_route(root, route_file)
    if P.route_is_closed(root, route):
        return
    route_module = P.artifact_lifecycle._load_capability_route()
    evidence = _cycle_seal_evidence(run_dir, cyc, route["route_id"], lump_cycle_id)
    for node in route.get("nodes", []):
        if node.get("terminal") is True:
            route_module.write_completion_marker(route, node, node["id"], evidence)
    outcome, _fresh = route_module.close_route(
        route, route_file, commit=None,
        summary=f"W7G retrospective resplit seal for {cyc['cycle_key']}")
    if outcome.get("terminal_gate_proven") is not True:
        raise ResplitError("resplit-stale", f"per-cycle-route-unproven:{cyc['cycle_id']}")


def _r2_prepare(root: Path, campaign_plan: List[Dict[str, Any]], alloc, lump_cycle_id: str,
                route: Dict[str, Any], route_file: Path, run_dir: Path,
                loose_by_cycle_key: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    lump_record = P.read_cycle_record(root, lump_cycle_id) or {}
    loose_by_cycle_key = loose_by_cycle_key or {}
    campaign_plan, deferred_campaigns = _partition_campaign_plan(campaign_plan)
    campaign_pre: List[Dict[str, Any]] = []
    cycle_pre: List[Dict[str, Any]] = []
    created_dirs: List[str] = []
    created_cycle_dirs: List[str] = []
    created_routes: List[Dict[str, str]] = []
    ledger = ledger_resplit_routes(root)
    cycles_out: List[Dict[str, Any]] = []
    for camp in campaign_plan:
        existing = _find_campaign_by_key_local(root, camp["key"])
        if existing is not None:
            campaign_id = existing["campaign_id"]
            path = P.campaign_dir(root, campaign_id) / "campaign.json"
            original = path.read_bytes()
            campaign_pre.append({"path": os.path.relpath(path, root), "created_by_this_run": False,
                                 "bytes_b64": _b64(original), "sha256": "sha256:" + hashlib.sha256(original).hexdigest(),
                                 "cycles_added": []})
            camp["campaign_created"] = False
        else:
            campaign_id = alloc.allocate("campaign")
            path = P.campaign_dir(root, campaign_id) / "campaign.json"
            campaign_pre.append({"path": os.path.relpath(path, root), "created_by_this_run": True,
                                 "bytes_b64": None, "sha256": None, "cycles_added": []})
            camp["campaign_created"] = True
        camp["campaign_id"] = campaign_id
    for camp in campaign_plan:
        for unit in camp["cycle_units"]:
            existing_cycle = _find_cycle_by_key(root, unit["cycle_key"])
            if existing_cycle is not None:
                cycle_id = existing_cycle["cycle_id"]
                cpath = P.cycle_record_path(root, cycle_id)
                original = cpath.read_bytes()
                cycle_pre.append({"path": os.path.relpath(cpath, root), "created_by_this_run": False,
                                  "bytes_b64": _b64(original), "sha256": "sha256:" + hashlib.sha256(original).hexdigest()})
                created = False
            else:
                cycle_id = alloc.allocate("cycle")
                cpath = P.cycle_record_path(root, cycle_id)
                cycle_pre.append({"path": os.path.relpath(cpath, root), "created_by_this_run": True,
                                  "bytes_b64": None, "sha256": None})
                created = True
            cdir = P.cycle_dir(root, camp["campaign_id"], cycle_id)
            cycles_out.append({
                "cycle_key": unit["cycle_key"], "cycle_id": cycle_id, "campaign_id": camp["campaign_id"],
                "cycle_dir": str(cdir), "created": created, "bucket": unit["bucket"],
                "depth1_name": unit["depth1_name"], "started_on": unit["started_on"],
                "started_on_source": unit.get("started_on_source"), "title": unit["title"],
                "capability": BUCKET_CAPABILITY[unit["bucket"]],
                "files": unit["files"],
                "loose_files": list(loose_by_cycle_key.get(unit["cycle_key"], [])),
            })
            if created:
                created_cycle_dirs.append(os.path.relpath(cdir, root))
    for camp in campaign_plan:
        if camp["campaign_created"]:
            record = {
                "schema_version": 1, "contract": P.CONTRACT, "campaign_id": camp["campaign_id"],
                "key": camp["key"], "title": camp["title"], "goal": camp["goal"],
                "completion_criterion": {"statement": "every cycle sealed with a manifest"},
                "state": "active", "created_on": C._now(), "cycles": [],
            }
            if camp["slug"] == UNASSIGNED_SLUG:
                record["degraded"] = True
            if camp["related"]:
                record["related"] = camp["related"]
            P._write_campaign(root, record, exclusive=True)
        camp_record = P.read_campaign(root, camp["campaign_id"])
        camp_cycles = list(camp_record.get("cycles", []))
        added = []
        for cyc in cycles_out:
            if cyc["campaign_id"] != camp["campaign_id"] or not cyc["created"]:
                continue
            cid = cyc["cycle_id"]
            cdir = Path(cyc["cycle_dir"])
            (cdir / "artifacts").mkdir(parents=True, exist_ok=True)
            created_dirs.append(os.path.relpath(cdir / "artifacts", root))
            capability = cyc["capability"]
            cyc_route, cyc_route_file, route_created = _per_cycle_route(
                root, route, cyc["cycle_key"], capability, run_dir, ledger)
            if route_created:
                created_routes.append({
                    "route_id": cyc_route["route_id"],
                    "route_file": os.path.relpath(cyc_route_file, root),
                })
            cyc["route_id"] = cyc_route["route_id"]
            cyc["route_file"] = str(cyc_route_file.resolve())
            record = {
                "schema_version": 1, "contract": P.CONTRACT, "cycle_id": cid, "campaign_id": camp["campaign_id"],
                "producer_id": alloc.allocate("producer"), "parent_cycle_id": None,
                # D-79/D-7: bucket-derived, never inherited from the calling route.
                "capability": capability, "route_capability": capability,
                "intensity": RESPLIT_CYCLE_INTENSITY,
                "route_id": cyc_route["route_id"], "route_hash": cyc_route["route_hash"],
                "route_file": str(cyc_route_file.resolve()), "node_id": None, "state": "open",
                # D-79: the work's own date, not the resplit's wall clock.
                "started_on": cyc["started_on"], "sealed_on": None, "manifest_digest": None,
                "title": cyc["title"], "cycle_key": cyc["cycle_key"], "derived_from_cycle_id": lump_cycle_id,
                "started_on_source": cyc.get("started_on_source"),
                "resplit_run_at": C._now(),
            }
            P._write_cycle_record(root, record, exclusive=True)
            camp_cycles.append(cid)
            added.append(cid)
        if added:
            camp_record = dict(camp_record)
            camp_record["cycles"] = camp_cycles
            P._write_campaign(root, camp_record, exclusive=False)
        for entry in campaign_pre:
            if entry["path"] == os.path.relpath(P.campaign_dir(root, camp["campaign_id"]) / "campaign.json", root):
                entry["cycles_added"] = added
    pre_image = {
        "schema_version": 1, "campaign_records": campaign_pre, "cycle_records": cycle_pre,
        "created_cycle_dirs": created_cycle_dirs, "created_dirs": created_dirs,
        "created_routes": created_routes,
    }
    return pre_image, cycles_out, deferred_campaigns


def _r2_disk_has_any_commit(root: Path, cycles: Sequence[Dict[str, Any]]) -> bool:
    return any((Path(c["cycle_dir"]) / "manifest.json").exists() for c in cycles)


def _prune_empty_tree(path: Path) -> None:
    """Bottom-up rmdir of every now-empty directory under (and including) `path`."""
    if not path.is_dir():
        return
    for current, dirs, files in os.walk(path, topdown=False):
        cur = Path(current)
        if files:
            raise ResplitError("rollback-residue", str(cur))
        try:
            cur.rmdir()
        except OSError:
            raise ResplitError("rollback-residue", str(cur))


def _release_created_routes(root: Path, created_routes: Sequence[Dict[str, str]]) -> None:
    route_module = P.artifact_lifecycle._load_capability_route()
    for entry in created_routes:
        route_file = root / entry["route_file"]
        for path in (route_file, route_file.with_name(route_file.stem + ".outcome.json")):
            if path.is_file():
                path.unlink()
        completion = route_module.completion_dir(entry["route_id"])
        if completion.is_dir():
            for child in sorted(completion.iterdir()):
                if child.is_file():
                    child.unlink()
            try:
                completion.rmdir()
            except OSError:
                pass


def _r2_rollback(root: Path, journal: Dict[str, Any], run_dir: Path) -> None:
    inverse_path = run_dir / "inverse.jsonl"
    if inverse_path.is_file():
        rows = sorted(C._read_jsonl(inverse_path), key=lambda r: -r["ordinal"])
        for row in rows:
            target = root / row["target_locator"]
            source = root / row["source_locator"]
            if target.is_file() and not target.is_symlink():
                if "sha256:" + C._sha(target) != row["sha256"]:
                    raise ResplitError("rollback-residue", row["target_locator"])
                source.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, source)
    pre_image = journal.get("pre_image") or {}
    for entry in pre_image.get("cycle_records", []):
        path = root / entry["path"]
        if entry["created_by_this_run"]:
            if path.is_file():
                path.unlink()
        else:
            data = base64.b64decode(entry["bytes_b64"])
            P._write_atomic(path, data)
    for rel in sorted(pre_image.get("created_cycle_dirs", []), key=lambda p: -p.count("/")):
        _prune_empty_tree(root / rel)
    for rel in sorted(pre_image.get("created_dirs", []), key=lambda p: -p.count("/")):
        path = root / rel
        if path.is_dir():
            _prune_empty_tree(path)
    for entry in pre_image.get("campaign_records", []):
        path = root / entry["path"]
        if entry["created_by_this_run"]:
            if path.is_file():
                path.unlink()
            _prune_empty_tree(path.parent)
        else:
            data = base64.b64decode(entry["bytes_b64"])
            P._write_atomic(path, data)
    # D-8 "실패 시 canonical 노출 0" reaches the route ledger too: a rolled-back batch
    # must not leave the routes it admitted (or their outcome sidecars and completion
    # markers) behind, or the re-run would meet its own leftovers as a duplicate.
    _release_created_routes(root, pre_image.get("created_routes", []))
    if inverse_path.is_file():
        inverse_path.unlink()
    journal["phase"] = "rolled-back"
    P._write_atomic(run_dir / "journal-r2.json", P._json_bytes(journal))


def _r2_preview(root: Path, verdict: Dict[str, Any], root_slug: str, loose_inventory: Dict[str, Any],
                run_dir: Path, journal: Optional[Dict[str, Any]], plan_sha256: str) -> Dict[str, Any]:
    """Read-only answer to "what would R2 do?" -- zero writes, zero ID allocation.

    When a real R2 journal already exists it is reported as-is (that run, not a
    fresh plan, is what would continue); otherwise the plan is derived from the
    admitted verdict exactly as `_r2_prepare` would derive it. A `rolled-back`
    journal is normalized away first, mirroring `_r2` -- a re-run after rollback
    restarts from `prepared`, so reporting it as a resumable run would be a lie in
    exactly the surface whose whole point is telling the truth.
    """
    if journal is not None and journal.get("phase") == "rolled-back":
        journal = None
    campaign_plan, deferred_campaigns = _partition_campaign_plan(
        _campaign_plan_from_verdict(verdict, root_slug))
    loose_by_cycle_key, loose_deferred = loose_plan(verdict, loose_inventory)
    planned_cycles = []
    for camp in campaign_plan:
        for unit in camp["cycle_units"]:
            existing = _find_cycle_by_key(root, unit["cycle_key"])
            loose_rows = loose_by_cycle_key.get(unit["cycle_key"], [])
            planned_cycles.append({
                "cycle_key": unit["cycle_key"], "campaign_slug": camp["slug"], "campaign_key": camp["key"],
                "bucket": unit["bucket"], "depth1_name": unit["depth1_name"], "title": unit["title"],
                "capability": BUCKET_CAPABILITY[unit["bucket"]], "intensity": RESPLIT_CYCLE_INTENSITY,
                "started_on": unit["started_on"], "started_on_source": unit.get("started_on_source"),
                "file_count": unit["file_count"],
                "byte_count": unit["byte_count"], "loose_file_count": len(loose_rows),
                "loose_locators": [r["locator"] for r in loose_rows],
                "existing_cycle_id": existing["cycle_id"] if existing else None,
            })
    return {
        "status": "dry-run", "run_dir": str(run_dir), "plan_sha256": plan_sha256,
        "journal": journal, "resumes_existing_journal": journal is not None,
        "campaigns": [{"slug": c["slug"], "key": c["key"], "cycle_count": len(c["cycle_units"])}
                     for c in campaign_plan],
        "cycles": planned_cycles, "loose_deferred": loose_deferred,
        "deferred_campaigns": deferred_campaigns,
    }


def _r2(root: Path, *, lump_cycle_id: str, route_file: Optional[Path] = None, dry_run: bool = False,
       crash_after_phase: Optional[str] = None, crash_at: Optional[str] = None,
       allocator=None) -> Dict[str, Any]:
    root = Path(root).resolve()
    C._require_active(root)
    if route_file is None:
        raise ResplitError("admission-marker-missing", "route-file-required")
    route = P.load_route(root, Path(route_file))
    run_dir = _find_run_dir(root, lump_cycle_id)
    if run_dir is None or _valid_admitted_run(root, run_dir) is None:
        raise ResplitError("admission-marker-missing", lump_cycle_id)
    marker = _valid_admitted_run(root, run_dir)
    admission_dir = _admission_dir(run_dir)
    lump_inventory = P._read_json(admission_dir / "lump-inventory.json")
    loose_inventory = P._read_json(admission_dir / "loose-inventory.json")
    proposal = P._read_json(admission_dir / "proposal.json")
    verdict = validate_proposal(proposal, root=root, lump_inventory=lump_inventory,
                                loose_inventory=loose_inventory, lump_cycle_id=lump_cycle_id)
    if verdict["verdict"] != "ok":
        raise ResplitError("resplit-stale", "admitted-proposal-no-longer-valid")
    root_slug = verdict["root_slug"]
    other_hold = resplit_hold(root)
    if other_hold and other_hold.get("lump_cycle_id") != lump_cycle_id:
        raise ResplitError("resplit-in-progress", json.dumps(other_hold))
    journal_path = run_dir / "journal-r2.json"
    journal = P._read_json(journal_path)
    plan_sha256 = marker["plan_sha256"]
    if journal is not None and journal.get("phase") == "complete" and journal.get("plan_sha256") == plan_sha256:
        return {"status": "already-applied", "run_dir": str(run_dir), "journal": journal,
                "loose_deferred": journal.get("loose_deferred", []),
                "deferred_campaigns": journal.get("deferred_campaigns", [])}
    if dry_run:
        # A dry run reports the plan and touches nothing -- not the canonical tree,
        # not `.runtime`, not the root-wide lock. It therefore returns before
        # `_acquire_resplit_lock`/`_r2_prepare`, both of which write: `_r2_prepare`
        # allocates IDs and commits campaign records, cycle records and cycle
        # directories, and the journal write that followed it published a
        # nonterminal R2 journal -- which `resplit_hold` then reports as
        # `resplit-in-progress`, putting the whole root on hold because someone
        # asked what *would* happen.
        return _r2_preview(root, verdict, root_slug, loose_inventory, run_dir, journal, plan_sha256)
    # D-77-a: root-wide mutual exclusion from here (R2 start) through R3 terminal.
    # `other_hold` above is the read-only journal-owned hold predicate (unchanged);
    # this is the atomic claim that actually prevents two processes from both
    # passing that read and mutating concurrently.
    _acquire_resplit_lock(root, lump_cycle_id=lump_cycle_id, run_dir=run_dir)
    try:
        alloc = allocator or artifact_identity.IdAllocator()
        if journal is not None and journal.get("phase") == "rolled-back":
            journal = None  # PRD 2959-2964: a re-run after rollback restarts from `prepared`
        resumed = journal is not None
        if journal is None:
            campaign_plan = _campaign_plan_from_verdict(verdict, root_slug)
            loose_by_cycle_key, loose_deferred = loose_plan(verdict, loose_inventory)
            pre_image, cycles, deferred_campaigns = _r2_prepare(
                root, campaign_plan, alloc, lump_cycle_id, route, route_file, run_dir,
                loose_by_cycle_key=loose_by_cycle_key)
            journal = {
                "schema_version": 1, "kind": "w7g-resplit-journal", "gate": "r2", "plan_sha256": plan_sha256,
                "lump_cycle_id": lump_cycle_id, "run_dir": str(run_dir), "phase": "prepared",
                "started_at": C._now(), "pre_image": pre_image, "cycles": cycles,
                "created_dirs": pre_image["created_dirs"], "created_cycle_dirs": pre_image["created_cycle_dirs"],
                "route_id": route["route_id"],
                "loose_deferred": loose_deferred, "deferred_campaigns": deferred_campaigns,
            }
            P._write_atomic(journal_path, P._json_bytes(journal))
        if crash_after_phase == "prepared":
            raise ResplitError("crash-fixture", "prepared")
        cycles = journal["cycles"]
        if _r2_disk_has_any_commit(root, cycles):
            # PRD 2962-2964: past the first `finalize` commit, recovery is roll-forward only.
            _r2_roll_forward(root, run_dir, journal, cycles, crash_at=crash_at, crash_after_phase=crash_after_phase)
        elif resumed:
            # A prior call already built this journal and did not reach a commit before
            # stopping (crash-fixture or process death) -- this retry rolls it back rather
            # than resuming forward (§S2-d-2: crash recovery, not crash tolerance).
            _r2_rollback(root, journal, run_dir)
        else:
            _r2_execute(root, run_dir, journal, cycles, crash_at=crash_at, crash_after_phase=crash_after_phase)
        return {"status": journal["phase"], "run_dir": str(run_dir), "journal": journal,
                "loose_deferred": journal.get("loose_deferred", []),
                "deferred_campaigns": journal.get("deferred_campaigns", [])}
    finally:
        # Released only once this run's R2 (and, if it got that far, R3) reached a
        # terminal phase -- a synthetic crash (an exception raised above) leaves the
        # journal nonterminal, so this is a no-op and the lock stays held, exactly as
        # a real process death would leave it.
        _release_resplit_lock_if_done(root, run_dir)


def _r2_move_cycle(root: Path, journal: Dict[str, Any], run_dir: Path, cyc: Dict[str, Any],
                   inverse_rows: List[Dict[str, Any]], *, crash_after_first_file: bool = False) -> None:
    lump_record = P.read_cycle_record(root, journal["lump_cycle_id"])
    lump = P.cycle_dir(root, lump_record["campaign_id"], journal["lump_cycle_id"])
    for idx, f in enumerate(cyc["files"]):
        source = lump / "artifacts" / f["locator"]
        target = Path(cyc["cycle_dir"]) / "artifacts" / f["locator"]
        if target.is_file():
            continue
        if not source.is_file():
            raise ResplitError("resplit-stale", f["locator"])
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
        inverse_rows.append({
            "ordinal": len(inverse_rows), "action": "rename_back",
            "source_locator": os.path.relpath(source, root), "target_locator": os.path.relpath(target, root),
            "sha256": f["sha256"],
        })
        C._write_jsonl(run_dir / "inverse.jsonl", inverse_rows)
        # 🟡1 (FP-4 granularity): a per-file fault point, distinct from the
        # whole-cycle boundary above -- a crash here leaves this cycle's file
        # rename genuinely mid-flight (some files moved, some not), matching
        # the plan's fault-point wording rather than only a whole-cycle
        # rename-complete-but-not-finalized boundary.
        if crash_after_first_file and idx == 0 and len(cyc["files"]) > 1:
            raise ResplitError("crash-fixture", "mid-cycle-file-rename")
    _r2_move_loose(root, run_dir, cyc, inverse_rows)


def _r2_move_loose(root: Path, run_dir: Path, cyc: Dict[str, Any],
                   inverse_rows: List[Dict[str, Any]]) -> None:
    """D-79 "잔여 loose 파일": relocate the top-level residue this cycle owns.

    Same rules as the lump renames -- same-filesystem `os.rename` only (never a
    copy), one `rename_back` inverse row per file written before the next move, and
    an existing target is a completed move, not a conflict. The only differences are
    the source (the artifact root's own top level, not the lump's `artifacts/`) and
    the target locator, which keeps the origin bucket name under `_internal/` so the
    file's provenance survives the move.
    """
    for f in cyc.get("loose_files", []):
        source = root / f["source_locator"]
        target = Path(cyc["cycle_dir"]) / "artifacts" / f["locator"]
        if target.is_file():
            continue
        if source.is_symlink() or not source.is_file():
            raise ResplitError("resplit-stale", f["source_locator"])
        if "sha256:" + C._sha(source) != f["sha256"]:
            raise ResplitError("resplit-stale", f"loose-digest-drift:{f['source_locator']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
        inverse_rows.append({
            "ordinal": len(inverse_rows), "action": "rename_back",
            "source_locator": f["source_locator"], "target_locator": os.path.relpath(target, root),
            "sha256": f["sha256"],
        })
        C._write_jsonl(run_dir / "inverse.jsonl", inverse_rows)


def _loose_locators(cyc: Dict[str, Any]) -> List[str]:
    """D-79: relocated loose files carry manifest role `support`, not `output` --
    they are provenance-preserved residue attached to the cycle, not something the
    cycle produced."""
    return [f["locator"] for f in cyc.get("loose_files", [])]


def _cycle_expected_files(cyc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every file the finished cycle must contain: the lump's own rows plus the
    D-79 loose rows at their post-move locators. Both witnesses (D-78: right after
    the renames, and again immediately before `finalize`) compare against this, so
    a loose file that failed to move is caught at the same point a lump file would
    be -- before the commit, while roll-back is still possible."""
    return list(cyc["files"]) + [
        {"locator": f["locator"], "sha256": f["sha256"], "byte_size": f["byte_size"]}
        for f in cyc.get("loose_files", [])
    ]


def _r2_witness(cyc: Dict[str, Any]) -> None:
    actual = C._tree_digest(Path(cyc["cycle_dir"]) / "artifacts")
    expected = _expected_tree_digest(_cycle_expected_files(cyc))
    if actual["tree_sha256"] != expected["tree_sha256"]:
        raise ResplitError("resplit-stale", f"witness-mismatch:{cyc['cycle_id']}")


def _r2_execute(root: Path, run_dir: Path, journal: Dict[str, Any], cycles: List[Dict[str, Any]], *,
                crash_at: Optional[str], crash_after_phase: Optional[str]) -> None:
    journal_path = run_dir / "journal-r2.json"
    inverse_rows = C._read_jsonl(run_dir / "inverse.jsonl") if (run_dir / "inverse.jsonl").is_file() else []
    first = cycles[0]
    _r2_move_cycle(root, journal, run_dir, first, inverse_rows)
    if crash_at == "r2:after-first-cycle-rename":
        raise ResplitError("crash-fixture", "after-first-cycle-rename")
    journal["phase"] = "renamed"
    P._write_atomic(journal_path, P._json_bytes(journal))
    _r2_witness(first)
    journal["phase"] = "witnessed"
    P._write_atomic(journal_path, P._json_bytes(journal))
    _r2_witness(first)  # witness (2): re-checked immediately before finalize
    if crash_at == "r2:before-finalize":
        raise ResplitError("crash-fixture", "before-finalize")
    _close_cycle_route(root, run_dir, first, journal["lump_cycle_id"])
    sealed = P.finalize(root, cycle_id=first["cycle_id"], state="completed",
                       support_locators=_loose_locators(first))
    if sealed.get("status") not in {"sealed", "already-sealed"}:
        raise ResplitError("resplit-stale", f"finalize-failed:{sealed.get('status')}")
    if crash_at == "r2:after-finalize-before-journal":
        raise ResplitError("crash-fixture", "after-finalize-before-journal")
    journal["phase"] = "committed"
    P._write_atomic(journal_path, P._json_bytes(journal))
    if crash_after_phase == "committed":
        raise ResplitError("crash-fixture", "committed")
    for i, cyc in enumerate(cycles[1:], start=1):
        crash_mid_file = crash_at == "r2:mid-cycle-file" and len(cyc["files"]) > 1
        _r2_move_cycle(root, journal, run_dir, cyc, inverse_rows, crash_after_first_file=crash_mid_file)
        if crash_at == "r2:mid-second-cycle" and i == 1:
            raise ResplitError("crash-fixture", "mid-second-cycle")
        _r2_witness(cyc)
        _close_cycle_route(root, run_dir, cyc, journal["lump_cycle_id"])
        sealed = P.finalize(root, cycle_id=cyc["cycle_id"], state="completed",
                           support_locators=_loose_locators(cyc))
        if sealed.get("status") not in {"sealed", "already-sealed"}:
            raise ResplitError("resplit-stale", f"finalize-failed:{sealed.get('status')}")
    journal["phase"] = "complete"
    P._write_atomic(journal_path, P._json_bytes(journal))


def _r2_roll_forward(root: Path, run_dir: Path, journal: Dict[str, Any], cycles: List[Dict[str, Any]], *,
                     crash_at: Optional[str], crash_after_phase: Optional[str]) -> None:
    journal_path = run_dir / "journal-r2.json"
    if journal.get("phase") not in {"committed", "complete"}:
        journal["phase"] = "committed"
    inverse_rows = C._read_jsonl(run_dir / "inverse.jsonl") if (run_dir / "inverse.jsonl").is_file() else []
    for i, cyc in enumerate(cycles):
        manifest_path = Path(cyc["cycle_dir"]) / "manifest.json"
        if manifest_path.exists():
            continue
        _r2_move_cycle(root, journal, run_dir, cyc, inverse_rows)
        if crash_at == "r2:mid-second-cycle" and i >= 1:
            P._write_atomic(journal_path, P._json_bytes(journal))
            raise ResplitError("crash-fixture", "mid-second-cycle")
        _r2_witness(cyc)
        _close_cycle_route(root, run_dir, cyc, journal["lump_cycle_id"])
        sealed = P.finalize(root, cycle_id=cyc["cycle_id"], state="completed",
                           support_locators=_loose_locators(cyc))
        if sealed.get("status") not in {"sealed", "already-sealed"}:
            raise ResplitError("resplit-stale", f"finalize-failed:{sealed.get('status')}")
    journal["phase"] = "complete"
    P._write_atomic(journal_path, P._json_bytes(journal))


# ---------------------------------------------------------------------------
# R3 -- lump supersession + compat append + backup + removal
# ---------------------------------------------------------------------------


def _r3(root: Path, *, lump_cycle_id: str, backup_root: Optional[Path], dry_run: bool = False,
       crash_after_phase: Optional[str] = None, crash_at: Optional[str] = None,
       allocator=None) -> Dict[str, Any]:
    root = Path(root).resolve()
    C._require_active(root)
    run_dir = _find_run_dir(root, lump_cycle_id)
    if run_dir is None:
        raise ResplitError("admission-marker-missing", lump_cycle_id)
    r2_journal = P._read_json(run_dir / "journal-r2.json")
    if not r2_journal or r2_journal.get("phase") != "complete":
        raise ResplitError("resplit-stale", "r2-not-complete")
    journal_path = run_dir / "journal-r3.json"
    journal = P._read_json(journal_path)
    plan_sha256 = r2_journal["plan_sha256"]
    if journal is not None and journal.get("phase") == "complete" and journal.get("plan_sha256") == plan_sha256:
        return {"status": "already-applied", "run_dir": str(run_dir), "journal": journal}
    if not dry_run:
        backup_root = _validated_backup_root(root, backup_root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    alloc = allocator or artifact_identity.IdAllocator()
    new_cycle_ids = [c["cycle_id"] for c in r2_journal["cycles"]]
    if dry_run:
        # Same rule as the R2 dry run: report, write nothing, and do not claim the
        # root-wide lock. The old ordering built and published `journal-r3.json`
        # first, which `resplit_hold` reads as `resplit-in-progress`.
        return {"status": "dry-run", "run_dir": str(run_dir), "plan_sha256": plan_sha256,
                "journal": journal, "resumes_existing_journal": journal is not None,
                "lump_cycle_id": lump_cycle_id, "new_cycle_ids": new_cycle_ids,
                "backup_verified": (run_dir / "backup-verified.json").is_file()}
    # D-77-a: the same root-wide lock claimed at R2 start is still (or, if this is a
    # fresh process, is re-)claimed here and held through R3 terminal.
    _acquire_resplit_lock(root, lump_cycle_id=lump_cycle_id, run_dir=run_dir)
    try:
        if journal is not None and journal.get("phase") == "rolled-back":
            journal = None  # PRD 2971-2974: a re-run after rollback restarts from `prepared`
        resumed = journal is not None
        if journal is None:
            lump_record = P.read_cycle_record(root, lump_cycle_id) or {}
            pre_image = {
                "schema_version": 1,
                "side_records": [{
                    "path": os.path.relpath(P.cycle_record_path(root, lump_cycle_id), root),
                    "bytes_b64": _b64(P.cycle_record_path(root, lump_cycle_id).read_bytes()),
                    "sha256": "sha256:" + hashlib.sha256(P.cycle_record_path(root, lump_cycle_id).read_bytes()).hexdigest(),
                }],
                "compat_json": None, "written_map_files": [],
            }
            campaign_id = lump_record.get("campaign_id")
            campaign_path = P._campaign_path(root, campaign_id) if campaign_id else None
            if campaign_path and campaign_path.is_file():
                data = campaign_path.read_bytes()
                pre_image["side_records"].append({
                    "path": os.path.relpath(campaign_path, root), "bytes_b64": _b64(data),
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                })
            compat_path = C.compat_path(root)
            if compat_path.is_file():
                data = compat_path.read_bytes()
                pre_image["compat_json"] = {"bytes_b64": _b64(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}
            journal = {
                "schema_version": 1, "kind": "w7g-resplit-journal", "gate": "r3", "plan_sha256": plan_sha256,
                "lump_cycle_id": lump_cycle_id, "run_dir": str(run_dir), "phase": "prepared",
                "started_at": C._now(), "pre_image": pre_image, "new_cycle_ids": new_cycle_ids,
            }
            P._write_atomic(journal_path, P._json_bytes(journal))
        if crash_after_phase == "prepared":
            raise ResplitError("crash-fixture", "prepared")
        backup_verified = (run_dir / "backup-verified.json").is_file()
        if backup_verified:
            # PRD 2971-2974/A-16.2: past the verified backup, recovery is roll-forward only.
            _r3_execute(root, run_dir, journal, identity, alloc, backup_root,
                       crash_at=crash_at, crash_after_phase=crash_after_phase)
        elif resumed:
            # A prior call already built this journal and did not reach a verified backup
            # before stopping -- this retry rolls it back rather than resuming forward.
            _r3_rollback(root, journal, run_dir)
        else:
            _r3_execute(root, run_dir, journal, identity, alloc, backup_root,
                       crash_at=crash_at, crash_after_phase=crash_after_phase)
        return {"status": journal["phase"], "run_dir": str(run_dir), "journal": journal}
    finally:
        _release_resplit_lock_if_done(root, run_dir)


def _r3_rollback(root: Path, journal: Dict[str, Any], run_dir: Path) -> None:
    pre_image = journal.get("pre_image") or {}
    for entry in pre_image.get("side_records", []):
        path = root / entry["path"]
        P._write_atomic(path, base64.b64decode(entry["bytes_b64"]))
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        events_path.unlink()
    campaign_events_path = run_dir / "events-campaign.jsonl"
    if campaign_events_path.is_file():
        campaign_events_path.unlink()
    compat_pre = pre_image.get("compat_json")
    compat_path = C.compat_path(root)
    if compat_pre is not None:
        P._write_atomic(compat_path, base64.b64decode(compat_pre["bytes_b64"]))
    elif compat_path.is_file() and not pre_image.get("written_map_files"):
        pass
    for rel in pre_image.get("written_map_files", []):
        p = root / rel
        if p.is_file():
            p.unlink()
    for name in ("legacy-artifacts.tar", "backup-seal.json", "backup-location.json"):
        p = run_dir / name
        if p.is_file():
            p.unlink()
    # The tar now lives outside the artifact root (D-84 backup rule), so the
    # roll-back has to reach it there; leaving it behind would let a later run
    # inherit a partial archive as if it had been sealed.
    backup_dir = journal.get("backup_dir")
    if backup_dir:
        bdir = Path(backup_dir)
        for name in ("legacy-artifacts.tar", "backup-seal.json"):
            p = bdir / name
            if p.is_file():
                p.unlink()
        try:
            bdir.rmdir()
        except OSError:
            pass
    journal["phase"] = "rolled-back"
    P._write_atomic(run_dir / "journal-r3.json", P._json_bytes(journal))


def _r3_execute(root: Path, run_dir: Path, journal: Dict[str, Any], identity, alloc, backup_root: Path,
                *, crash_at: Optional[str], crash_after_phase: Optional[str]) -> None:
    journal_path = run_dir / "journal-r3.json"
    lump_cycle_id = journal["lump_cycle_id"]
    new_cycle_ids = journal["new_cycle_ids"]
    phase = journal.get("phase")
    events_path = run_dir / "events.jsonl"
    if phase == "prepared":
        if not journal.get("event_id"):
            journal["event_id"] = alloc.allocate("event")
            journal["stream_id"] = alloc.allocate("stream")
            P._write_atomic(journal_path, P._json_bytes(journal))
        record = P.read_cycle_record(root, lump_cycle_id)
        if record.get("state") != "superseded":
            P.mark_cycle_superseded(root, lump_cycle_id, superseded_by=new_cycle_ids,
                                    superseded_event_id=journal["event_id"])
        journal["phase"] = "side-records-written"
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = journal["phase"]
        if crash_after_phase == phase:
            raise ResplitError("crash-fixture", phase)
    if phase == "side-records-written":
        existing = C._read_jsonl(events_path) if events_path.is_file() else []
        if not existing:
            r2_journal = P._read_json(run_dir / "journal-r2.json") or {}
            lump_record = P.read_cycle_record(root, lump_cycle_id) or {}
            lump_inventory = P._read_json(_admission_dir(run_dir) / "lump-inventory.json") or {}
            stream_id = journal["stream_id"]
            event_id = journal["event_id"]
            manifest_path = P.cycle_dir(root, lump_record["campaign_id"], lump_cycle_id) / "manifest.json"
            manifest = P._read_json(manifest_path) or {}
            lump_events = manifest.get("events") or []
            sealing_event_id = next(
                (e.get("event_id") for e in lump_events if e.get("event_type") == "cycle.completed"), event_id,
            )
            event = {
                "event_id": event_id, "stream_id": stream_id, "stream_sequence": 1,
                "event_type": "cycle.superseded", "target_id": lump_cycle_id,
                "actor": {"kind": "producer", "id": identity.repository_id if identity else "unknown"},
                "recorded_at": C._now(),
                "provenance": {
                    "source_manifest_id": manifest.get("manifest_id", "man_" + "0" * 32),
                    "source_revision_id": manifest.get("manifest_revision_id", "mrev_" + "0" * 32),
                    "producer_route_id": r2_journal.get("route_id", "rt-0000000000000000"),
                    "algorithm_version": RESPLIT_ALGORITHM_VERSION, "schema_version": 1,
                    "source_digest": lump_record.get("manifest_digest") or "sha256:" + "0" * 64,
                },
                "evidence_ids": [sealing_event_id, lump_inventory.get("digest") or "sha256:" + "0" * 64],
                "payload": {"superseded_by": new_cycle_ids, "resplit_run_dir": str(run_dir),
                           "new_cycle_count": len(new_cycle_ids)},
            }
            C._write_jsonl(events_path, [event])
        journal["phase"] = "events-appended"
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = journal["phase"]
        if crash_after_phase == phase:
            raise ResplitError("crash-fixture", phase)
    # D-81: the campaign can only be superseded once its cycles are, so this phase sits
    # after the cycle event and before the compat re-issue. Both of its actions are
    # read-then-act, so a crash anywhere inside it replays without a second producer
    # call and without a duplicate event row.
    if phase == "events-appended":
        result = _supersede_campaign(root, lump_cycle_id, dry_run=False)
        journal["campaign_supersede"] = result
        P._write_atomic(journal_path, P._json_bytes(journal))
        if result["phase"] == "complete":
            _campaign_supersession_event(root, run_dir, result["campaign_id"], new_cycle_ids,
                                         allocator=alloc)
        journal["phase"] = "campaign-superseded"
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = journal["phase"]
        if crash_after_phase == phase:
            raise ResplitError("crash-fixture", phase)
    if phase == "campaign-superseded":
        pre_image = journal.get("pre_image") or {}
        written_maps = pre_image.get("written_map_files") or []
        if not written_maps:
            record = P.read_cycle_record(root, lump_cycle_id)
            lump_dir = P.cycle_dir(root, record["campaign_id"], lump_cycle_id)
            map_path = run_dir / "compatibility-map.jsonl"
            rows = []
            omitted_lane1 = []
            original_sources = _original_legacy_sources(root)
            r2_journal = P._read_json(run_dir / "journal-r2.json") or {}
            for cyc in r2_journal.get("cycles", []):
                new_dir_rel = os.path.relpath(Path(cyc["cycle_dir"]), root)
                for f in cyc["files"]:
                    target = f"{new_dir_rel}/artifacts/{f['locator']}"
                    lump_locator = os.path.relpath(lump_dir, root) + "/artifacts/" + f["locator"]
                    original_locator = original_sources.get(lump_locator)
                    if original_locator is not None:
                        rows.append({"schema_version": C.MAP_SCHEMA, "kind": "file",
                                    "source_locator": original_locator,
                                    "target_locator": target, "sha256": f["sha256"], "identity_refs": []})
                    else:
                        omitted_lane1.append(lump_locator)
                    rows.append({"schema_version": C.MAP_SCHEMA, "kind": "file",
                                "source_locator": lump_locator,
                                "target_locator": target, "sha256": f["sha256"], "identity_refs": []})
                # D-79/D-82: a relocated loose file has only one source lane. It was
                # never copied into the lump, so its original legacy path *is* its
                # top-level `source_locator` -- there is no lump locator to invert and
                # nothing to count as a lane-1 omission.
                for lf in cyc.get("loose_files", []):
                    rows.append({"schema_version": C.MAP_SCHEMA, "kind": "file",
                                "source_locator": lf["source_locator"],
                                "target_locator": f"{new_dir_rel}/artifacts/{lf['locator']}",
                                "sha256": lf["sha256"], "identity_refs": []})
            C._write_jsonl(map_path, rows)
            # D-82: the chain is append-only, and an older map whose sources this map
            # now re-targets is superseded, not rewritten. Its row keeps its path,
            # `sha256` and `rows` byte-for-byte and gains only the pointer forward, so
            # `resolve_legacy`'s latest-wins answer and the chain's own account of why
            # it wins finally say the same thing.
            new_sources = {row["source_locator"] for row in rows}
            supersedes = [path for path, table in C._load_maps(root)
                          if new_sources.intersection(table)]
            C.compat_append(root, maps=[map_path], supersedes=supersedes)
            written_maps = [os.path.relpath(map_path, root)]
            pre_image["written_map_files"] = written_maps
            pre_image["superseded_map_files"] = supersedes
            pre_image["lane1_omitted_lump_locators"] = omitted_lane1
            journal["pre_image"] = pre_image
        journal["phase"] = "compat-reissued"
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = journal["phase"]
        if crash_after_phase == phase:
            raise ResplitError("crash-fixture", phase)
    if phase == "compat-reissued":
        record = P.read_cycle_record(root, lump_cycle_id)
        lump_dir = P.cycle_dir(root, record["campaign_id"], lump_cycle_id)
        backup_dir = _r3_backup_dir(root, run_dir, backup_root, identity)
        journal["backup_dir"] = str(backup_dir)
        P._write_atomic(journal_path, P._json_bytes(journal))
        _r3_backup(root, run_dir, lump_dir, backup_dir, crash_at=crash_at)
        if crash_at == "r3:after-reread-before-removal":
            raise ResplitError("crash-fixture", "after-reread-before-removal")
        journal["phase"] = "backup-sealed"
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = journal["phase"]
        if crash_after_phase == phase:
            raise ResplitError("crash-fixture", phase)
    if phase == "backup-sealed":
        record = P.read_cycle_record(root, lump_cycle_id)
        lump_dir = P.cycle_dir(root, record["campaign_id"], lump_cycle_id)
        artifacts = lump_dir / "artifacts"
        for f in P._walk_files(artifacts):
            if f.is_file() and not f.is_symlink():
                f.unlink()
        for current, dirs, files in os.walk(artifacts, topdown=False):
            cur = Path(current)
            if cur == artifacts:
                continue
            try:
                cur.rmdir()
            except OSError:
                pass
        journal["phase"] = "artifacts-removed"
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = journal["phase"]
        if crash_after_phase == phase:
            raise ResplitError("crash-fixture", phase)
    if phase == "artifacts-removed":
        # D-79 C-RT: `plans/stage-sessions` was never cycle population, so relocating it
        # is not part of the lump's byte conservation and must not be able to wedge a run
        # that is already past its verified backup. The step is journaled, idempotent and
        # advisory here; its own gate re-runs it when it holds.
        journal["stage_sessions"] = _relocate_stage_sessions(root, run_dir, dry_run=False)
        journal["phase"] = "complete"
        P._write_atomic(journal_path, P._json_bytes(journal))


CORRECTION_GATES = ("campaign-supersede", "stage-sessions-relocate")
CORRECTION_TERMINAL = {"complete", "no-op", "hold"}
STAGE_SESSIONS_DISPOSITION = "stage-sessions-disposition.json"


def _stage_session_plan(root: Path, run_dir: Path) -> List[Dict[str, Any]]:
    """The rows a stage-session relocation may touch, from the one sealed source.

    D-79 excludes `plans/stage-sessions` from the cycle population, so the sealed
    `lump-inventory.json` already lists it separately in `stage_sessions[]` -- and that
    file exists in every admitted bundle, including the nine roots that finished before
    this gate existed. `admission/r1-disposition.json` is a newer convenience record and
    is deliberately *not* an input here, because re-running R1 to create one would have
    to rewrite a sealed inventory.

    Only the original top-level legacy path is a relocation source. The copy inside the
    lump is removed by R3 with the rest of `artifacts/` and is already inside the R3
    backup tar, so moving it would take bytes out of a sealed conservation set.
    """
    inventory = P._read_json(_admission_dir(run_dir) / "lump-inventory.json") or {}
    retire = P._read_json(_admission_dir(run_dir) / "retire-inventory.json") or {}
    ordinal_by_source = {e.get("source_locator"): e for e in retire.get("entries", [])}
    inverted = _original_legacy_sources(root)
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for lump in inventory.get("lumps", []):
        lump_dir = lump.get("cycle_dir")
        if not lump_dir:
            continue
        lump_rel = os.path.relpath(Path(lump_dir), root)
        for item in lump.get("stage_sessions", []):
            locator = item.get("locator") or ""
            source_locator = inverted.get(f"{lump_rel}/artifacts/{locator}") or locator
            if not source_locator.startswith("plans/stage-sessions/") or source_locator in seen:
                continue
            parts = source_locator.split("/", 3)
            if len(parts) < 4 or not _ROUTE_ID_RE.match(parts[2]) or not parts[3]:
                continue
            # Both locators come from sealed records, but a `..` in either would let the
            # rename reach outside the artifact root, so containment is checked here --
            # before any path is handed to `os.rename` -- rather than trusted.
            target_locator = f".runtime/stage-sessions/{parts[2]}/{parts[3]}"
            if not _is_contained(root, root / source_locator) or not _is_contained(root, root / target_locator):
                continue
            seen.add(source_locator)
            entry = ordinal_by_source.get(source_locator) or {}
            rows.append({
                "source_locator": source_locator,
                "target_locator": target_locator,
                # The sealed digest, and the only value a rename is allowed to move.
                "sha256": entry.get("sha256") or item.get("sha256"),
                "retire_inventory_ordinal": entry.get("ordinal"),
            })
    return sorted(rows, key=lambda r: r["source_locator"])


def _stage_session_decide(root: Path, row: Dict[str, Any]) -> str:
    """Classify one row against the disk, without moving anything.

    `relocated` covers two shapes that have to be the same answer: the move this call is
    about to make, and a move a previous interrupted call already made. A run that
    renamed row 1 and then died leaves row 1 with its target in place and its source
    gone; if the replay called that `target-present-identical` the row would be excluded
    from the gate's evidence while its source was already deleted, and the entry could
    never be satisfied by anything again.
    """
    source = root / row["source_locator"]
    target = root / row["target_locator"]
    sealed = row.get("sha256")
    source_present = source.is_file() and not source.is_symlink()
    if target.exists():
        if not target.is_file() or target.is_symlink() or "sha256:" + C._sha(target) != sealed:
            return "hold"
        # Identical target, source gone -> the end state this relocation aims at already
        # holds. It is reported as `already-relocated` rather than `relocated` because
        # this call did not move anything and cannot claim to have: the row is evidence
        # about the tree, not about an actor. The gate accepts both, since D-85's own
        # predicate is a statement about what is on disk (`the sealed bytes are at the
        # canonical path and the legacy path is gone`), not about provenance.
        if not source.exists():
            return "already-relocated"
        # Identical target, source still here -> D-79's no-op. Nothing is copied, and
        # nothing is deleted either, because only D-84's approval package grants that.
        # `stage_sessions_disposed_rows` refuses to count this row, so the root stays
        # honestly short of `legacy_top_level_retired` until R4 runs.
        return "target-present-identical"
    if not source.exists():
        return "already-absent"
    if not source_present:
        return "hold"
    # Only the sealed bytes may move; anything else is a divergence to report, not carry.
    if "sha256:" + C._sha(source) != sealed:
        return "hold"
    return "relocated"


def _relocate_stage_sessions(root: Path, run_dir: Path, *, dry_run: bool) -> Dict[str, Any]:
    """Execute (or, in dry run, only decide) the D-79 C-RT relocation. Idempotent."""
    plan = _stage_session_plan(root, run_dir)
    rows: List[Dict[str, Any]] = []
    try:
        for row in plan:
            decision = _stage_session_decide(root, row)
            out = dict(row)
            out["status"] = decision
            out["moved_by_this_run"] = bool(decision == "relocated" and not dry_run)
            if decision == "relocated" and not dry_run:
                source = root / row["source_locator"]
                target = root / row["target_locator"]
                if source.is_file() and not source.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(source, target)
            rows.append(out)
    except OSError:
        # Seal what actually happened before the failure propagates. Without this a run
        # that moved row 1 and failed on row 2 would leave row 1's source deleted with no
        # record naming it, and the gate would have nothing to re-verify.
        if not dry_run and rows:
            _seal_stage_session_disposition(root, run_dir, rows)
        raise
    statuses = {r["status"] for r in rows}
    if "hold" in statuses:
        phase = "hold"
    elif "relocated" in statuses:
        phase = "complete"
    elif "already-relocated" in statuses:
        phase = "no-op"
    else:
        phase = "no-op"
    result = {"phase": phase, "rows": rows, "row_count": len(rows)}
    if dry_run:
        return result
    _seal_stage_session_disposition(root, run_dir, rows)
    return result


def _seal_stage_session_disposition(root: Path, run_dir: Path, rows: List[Dict[str, Any]]) -> None:
    identity = P.artifact_lifecycle.read_root_identity(root)
    _write_self_digest(run_dir / STAGE_SESSIONS_DISPOSITION, {
        "schema_version": 1, "kind": "w7g-stage-sessions-disposition",
        "artifact_root_id": identity.artifact_root_id if identity else None,
        "recorded_at": C._now(), "rows": rows,
    })


def _supersede_campaign(root: Path, lump_cycle_id: str, *, dry_run: bool) -> Dict[str, Any]:
    """D-81: mark the lump's campaign superseded once every cycle it owns is.

    `mark_campaign_superseded` enforces the "no live cycle" precondition itself, so this
    only classifies the outcome: a campaign that still holds a live cycle (hearting's
    does) is a typed skip, not a failure.
    """
    record = P.read_cycle_record(root, lump_cycle_id) or {}
    campaign_id = record.get("campaign_id")
    campaign = P.read_campaign(root, campaign_id) if campaign_id else None
    if campaign is None:
        return {"phase": "hold", "code": "campaign-unknown", "campaign_id": campaign_id}
    if campaign.get("state") == "superseded":
        return {"phase": "no-op", "code": "already-superseded", "campaign_id": campaign_id}
    states = [(P.read_cycle_record(root, cid) or {}).get("state") for cid in campaign.get("cycles") or []]
    if not states or any(state != "superseded" for state in states):
        return {"phase": "no-op", "code": "campaign-retained-live-cycles", "campaign_id": campaign_id,
                "cycle_states": sorted(set(s for s in states if s))}
    if dry_run:
        return {"phase": "complete", "code": "would-supersede", "campaign_id": campaign_id}
    try:
        P.mark_campaign_superseded(root, campaign_id)
    except P.ProducerError as exc:
        if getattr(exc, "code", "") != "campaign-has-live-cycles":
            raise
        return {"phase": "no-op", "code": "campaign-retained-live-cycles", "campaign_id": campaign_id}
    return {"phase": "complete", "code": "superseded", "campaign_id": campaign_id}


def _campaign_supersession_event(root: Path, run_dir: Path, campaign_id: str,
                                 superseded_by: Sequence[str], allocator=None) -> None:
    """Append the D-83 `campaign.superseded` row, once, to its own event file.

    It is a separate file from `events.jsonl` on purpose: R3 keeps the cycle event
    idempotent with an `if not existing:` guard over that file, so appending a second
    row there would make a roll-forward skip writing the cycle event at all. The reader
    folds both files and sorts by `(stream_id, stream_sequence)`, so the two stay one
    ordered stream.
    """
    path = run_dir / "events-campaign.jsonl"
    existing = C._read_jsonl(path) if path.is_file() else []
    if any(row.get("event_type") == "campaign.superseded" and row.get("target_id") == campaign_id
           for row in existing):
        return
    r3 = P._read_json(run_dir / "journal-r3.json") or {}
    identity = P.artifact_lifecycle.read_root_identity(root)
    alloc = allocator or artifact_identity.IdAllocator()
    event = {
        "event_id": alloc.allocate("event"),
        "stream_id": r3.get("stream_id") or alloc.allocate("stream"),
        "stream_sequence": 2, "event_type": "campaign.superseded", "target_id": campaign_id,
        "actor": {"kind": "producer", "id": identity.repository_id if identity else "unknown"},
        "recorded_at": C._now(),
        "payload": {"superseded_by": list(superseded_by), "resplit_run_dir": str(run_dir)},
    }
    C._write_jsonl(path, existing + [event])


def _pid_start(pid: Any) -> Optional[str]:
    """The process's start time from `/proc/<pid>/stat` field 22, or `None`.

    A bare pid is not an identity: pids are reused, and a lock reclaimed on pid liveness
    alone can be taken from a different, live process that happens to have inherited the
    number. Pairing the pid with its start time makes the check exact wherever procfs is
    available; where it is not, `None` is recorded and the pid alone is used, which is
    the pre-existing weaker guarantee rather than a new one.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(") ", 1)[-1].split()
        return fields[19]  # field 22 overall, 20th after the comm field
    except (OSError, IndexError):
        return None


def _owner_still_running(pid: Any, recorded_start: Any) -> bool:
    """True only when the recorded owner is provably the process still holding it."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return True
    current_start = _pid_start(pid)
    if recorded_start and current_start and recorded_start != current_start:
        return False  # same number, different process -- the owner is gone
    return True


def _claim_correction_lock(run_dir: Path, *, gate: str, lump_cycle_id: str) -> Path:
    """Exclusive per-gate lock that a dead owner cannot wedge forever.

    `resplit_hold` deliberately reads journals rather than process liveness, because a
    hold has to survive a reboot. A *lock* is the opposite case: if the process that
    took it is gone, refusing every later call would leave the correction surface
    permanently unusable with no operator command to clear it. So the lock is reclaimed
    only when its recorded owner is provably not running.
    """
    lock_path = run_dir / f"resplit-{gate}.lock"
    body = {"schema_version": 1, "kind": "w7g-correction-lock", "gate": gate,
            "lump_cycle_id": lump_cycle_id, "owner_pid": os.getpid(),
            "owner_pid_start": _pid_start(os.getpid()), "acquired_at": C._now()}
    try:
        P._write_exclusive(lock_path, P._json_bytes(body), 0o600)
        return lock_path
    except FileExistsError:
        pass
    existing = P._read_json(lock_path)
    if isinstance(existing, dict) and not _owner_still_running(
            existing.get("owner_pid"), existing.get("owner_pid_start")):
        try:
            lock_path.unlink()
        except OSError:
            pass
        try:
            P._write_exclusive(lock_path, P._json_bytes(body), 0o600)
            return lock_path
        except FileExistsError:
            pass
    raise ResplitError("resplit-in-progress", json.dumps(existing or {"gate": gate}))


def _correction_gate(root: Path, *, gate: str, lump_cycle_id: str, dry_run: bool) -> Dict[str, Any]:
    """The two post-hoc correction surfaces for roots that finished R1~R4 already.

    Neither touches a sealed manifest or a sealed admission bundle: `campaign-supersede`
    writes only the mutable campaign side record and its own event file, and
    `stage-sessions-relocate` renames paths D-79 excluded from the cycle population in
    the first place. Both are idempotent, journaled before they mutate, and refuse to
    start while an R2/R3 journal is nonterminal.
    """
    run_dir = _find_run_dir(root, lump_cycle_id)
    if run_dir is None or _valid_admitted_run(root, run_dir) is None:
        raise ResplitError("admission-marker-missing", lump_cycle_id)
    journal_path = run_dir / f"journal-{gate}.json"
    journal = P._read_json(journal_path)
    if isinstance(journal, dict) and journal.get("phase") in CORRECTION_TERMINAL:
        return {"status": "already-applied", "gate": gate, "run_dir": str(run_dir), "journal": journal}
    r3 = P._read_json(run_dir / "journal-r3.json") or {}
    if r3.get("phase") != "complete":
        raise ResplitError("resplit-stale", "r3-not-complete")
    if dry_run:
        # No journal, no lock, no directory: a dry run against a real root has to leave
        # the tree byte-identical, and the lock alone would create `.runtime/...`.
        if gate == "campaign-supersede":
            preview = _supersede_campaign(root, lump_cycle_id, dry_run=True)
        else:
            preview = _relocate_stage_sessions(root, run_dir, dry_run=True)
        return {"status": "dry-run", "gate": gate, "run_dir": str(run_dir),
                "would": preview, "resumes_existing_journal": journal is not None}
    hold = resplit_hold(root)
    if hold is not None and hold.get("gate") in {"r2", "r3"}:
        raise ResplitError("resplit-in-progress", json.dumps(hold))
    # The journal is written before the lock on purpose: a lock with no journal beside it
    # can only be an orphan, and that is the shape `_claim_correction_lock` reclaims.
    journal = {"schema_version": 1, "kind": "w7g-resplit-journal", "gate": gate,
               "lump_cycle_id": lump_cycle_id, "run_dir": str(run_dir),
               "phase": "prepared", "started_at": C._now(), "owner_pid": os.getpid()}
    P._write_atomic(journal_path, P._json_bytes(journal))
    lock_path = _claim_correction_lock(run_dir, gate=gate, lump_cycle_id=lump_cycle_id)
    try:
        if gate == "campaign-supersede":
            result = _supersede_campaign(root, lump_cycle_id, dry_run=False)
            if result["phase"] == "complete":
                _campaign_supersession_event(
                    root, run_dir, result["campaign_id"],
                    (P._read_json(run_dir / "journal-r3.json") or {}).get("new_cycle_ids") or [])
        else:
            result = _relocate_stage_sessions(root, run_dir, dry_run=False)
        journal["phase"] = result["phase"]
        journal["result"] = result
        P._write_atomic(journal_path, P._json_bytes(journal))
        return {"status": result["phase"], "gate": gate, "run_dir": str(run_dir),
                "result": result, "journal": journal}
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _validated_backup_root(root: Path, backup_root: Optional[Path]) -> Path:
    """D-84's backup rule, applied to R3's tar as well as R4's.

    R3 removes the lump's `artifacts/` once the backup verifies, so that tar is the
    only remaining copy of those bytes. Writing it under `<root>/.runtime/` put the
    sole copy inside the tree it is insurance against, and `--backup-root` was
    accepted and then ignored, which is worse than not offering it. There is no
    default: the caller names the location or R3 refuses.
    """
    if backup_root is None:
        raise ResplitError("backup-root-required", "r3")
    resolved = Path(backup_root).expanduser().resolve()
    if resolved == root or str(resolved).startswith(str(root) + os.sep):
        raise ResplitError("backup-root-inside-artifact-root", str(resolved))
    return resolved


def _r3_backup_dir(root: Path, run_dir: Path, backup_root: Path, identity) -> Path:
    """`<backup-root>/<artifact_root_id>/<stamp>/`, the same shape `retire` uses. The
    stamp is the run's own, not a fresh one, so a roll-forward retry lands in the
    directory the interrupted attempt was already filling."""
    root_id = identity.artifact_root_id if identity else "root-unissued"
    stamp = run_dir.name.split("-resplit-", 1)[0]
    return Path(backup_root) / root_id / stamp


def _r3_backup(root: Path, run_dir: Path, lump_dir: Path, backup_dir: Path, *,
               crash_at: Optional[str]) -> None:
    artifacts = lump_dir / "artifacts"
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / "legacy-artifacts.tar"
    files = [f for f in P._walk_files(artifacts) if f.is_file() and not f.is_symlink()]
    entries = []
    with tarfile.open(str(archive), "w") as tar:
        for f in files:
            rel = f.relative_to(artifacts).as_posix()
            tar.add(str(f), arcname=rel, recursive=False)
            entries.append({"path": rel, "byte_size": f.stat().st_size, "sha256": C._sha(f)})
    entries.sort(key=lambda e: e["path"])
    with archive.open("rb") as handle:
        os.fsync(handle.fileno())
    archive_sha = "sha256:" + C._sha(archive)
    if crash_at == "r3:after-backup-tar-before-seal":
        raise ResplitError("crash-fixture", "after-backup-tar-before-seal")
    seal = {"schema_version": 1, "kind": "w7g-backup-seal", "archive": str(archive),
            "archive_sha256": archive_sha, "entry_count": len(files), "entries": entries,
            "sealed_at": C._now()}
    seal_bytes = P._json_bytes(seal)
    P._write_atomic(backup_dir / "backup-seal.json", seal_bytes)
    # The run_dir keeps the seal copy and a pointer, never the payload.
    P._write_atomic(run_dir / "backup-seal.json", seal_bytes)
    P._write_atomic(run_dir / "backup-location.json", P._json_bytes({
        "schema_version": 1, "kind": "w7g-backup-location", "backup_dir": str(backup_dir),
        "archive": str(archive), "archive_sha256": archive_sha,
        "seal": str(backup_dir / "backup-seal.json"),
        "seal_sha256": "sha256:" + hashlib.sha256(seal_bytes).hexdigest(),
        "entry_count": len(files),
    }))
    if crash_at == "r3:after-seal-before-reread":
        raise ResplitError("crash-fixture", "after-seal-before-reread")
    # Re-read verification (PRD D-84 l.3184-3185 / A-16.6): recompute the archive's
    # digest from disk and compare it to the sealed digest, then compare every tar
    # member's path/size/content-digest against the sealed inventory -- not names
    # alone. Any mismatch is a typed failure before any unlink happens.
    reread_archive_sha = "sha256:" + C._sha(archive)
    if reread_archive_sha != seal["archive_sha256"]:
        raise ResplitError("backup-incomplete", "archive-digest-mismatch")
    entries_by_path = {e["path"]: e for e in entries}
    with tarfile.open(str(archive), "r") as tar:
        members_by_name = {m.name: m for m in tar.getmembers()}
        missing = [e["path"] for e in entries if e["path"] not in members_by_name]
        if missing:
            raise ResplitError("backup-incomplete", missing[0])
        for name, member in members_by_name.items():
            expected = entries_by_path.get(name)
            if expected is None:
                continue
            if member.size != expected["byte_size"]:
                raise ResplitError("backup-incomplete", f"size-mismatch:{name}")
            handle = tar.extractfile(member)
            content = handle.read() if handle is not None else b""
            content_sha = hashlib.sha256(content).hexdigest()
            if content_sha != expected["sha256"]:
                raise ResplitError("backup-incomplete", f"content-mismatch:{name}")
    verified = {"schema_version": 1, "kind": "w7g-backup-verified", "seal_sha256": archive_sha,
               "entry_count": len(files), "verified_at": C._now()}
    P._write_atomic(run_dir / "backup-verified.json", P._json_bytes(verified))


def resplit_legacy_cycle(
    root: Path, *, gate: str, lump_cycle_id: str, proposal: Optional[Path] = None,
    confirmed_constraints: Optional[Path] = None, route_file: Optional[Path] = None,
    backup_root: Optional[Path] = None, dry_run: bool = False,
    crash_after_phase: Optional[str] = None, crash_at: Optional[str] = None,
    allocator=None, root_slug: Optional[str] = None,
) -> Dict[str, Any]:
    """`root_slug` is the D-79 `<root-slug>` fallback for R1's fresh lump scan. R2
    and R3 read the inventory R1 already sealed, so it has no effect there."""
    root = Path(root).resolve()
    if gate == "r1":
        if proposal is None:
            raise ResplitError("admission-marker-missing", "proposal-required")
        return _r1(root, lump_cycle_id=lump_cycle_id, proposal_path=Path(proposal),
                  confirmed_constraints_path=Path(confirmed_constraints) if confirmed_constraints else None,
                  dry_run=dry_run, crash_after_phase=crash_after_phase, root_slug=root_slug)
    if gate == "r2":
        return _r2(root, lump_cycle_id=lump_cycle_id, route_file=route_file, dry_run=dry_run,
                  crash_after_phase=crash_after_phase, crash_at=crash_at, allocator=allocator)
    if gate == "r3":
        return _r3(root, lump_cycle_id=lump_cycle_id, backup_root=backup_root, dry_run=dry_run,
                   crash_after_phase=crash_after_phase, crash_at=crash_at, allocator=allocator)
    if gate in CORRECTION_GATES:
        return _correction_gate(root, gate=gate, lump_cycle_id=lump_cycle_id, dry_run=dry_run)
    raise ResplitError("resplit-stale", f"gate-unknown:{gate}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="artifact_resplit.py")
    parser.add_argument("--artifact-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    lump_index_p = sub.add_parser("lump-index")
    lump_index_p.add_argument("--root-slug", help="D-79 <root-slug> for a fresh scan "
                              "(default: the artifact root's repo directory name, slugified)")
    sub.add_parser("hold")
    sub.add_parser("retire-inventory")

    dev = sub.add_parser("deviations", help="read-only audit of an executed resplit run "
                         "against the D-79/D-10/D-82/D-84 contract points (mutation 0)")
    dev.add_argument("--lump-cycle-id")

    validate_p = sub.add_parser("campaign-proposal")
    validate_sub = validate_p.add_subparsers(dest="proposal_command", required=True)
    v = validate_sub.add_parser("validate")
    v.add_argument("--proposal", required=True)
    v.add_argument("--lump-inventory")
    v.add_argument("--loose-inventory")
    v.add_argument("--confirmed-constraints")
    v.add_argument("--lump-cycle-id")

    r = sub.add_parser("resplit-legacy-cycle")
    r.add_argument("--gate", required=True, choices=("r1", "r2", "r3") + CORRECTION_GATES)
    r.add_argument("--lump-cycle-id", required=True)
    r.add_argument("--proposal")
    r.add_argument("--confirmed-constraints")
    r.add_argument("--route-file")
    r.add_argument("--backup-root")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--root-slug", help="D-79 <root-slug> fallback for the r1 lump scan; "
                   "a proposal that declares its own root_slug still wins")
    r.add_argument("--crash-after-phase")
    r.add_argument("--crash-at")

    args = parser.parse_args(argv)
    root = Path(args.artifact_root)
    try:
        if args.command == "lump-index":
            _print(lump_index(root, root_slug=args.root_slug))
            return OK
        if args.command == "hold":
            hold = resplit_hold(root)
            _print(hold or {})
            return BLOCKED if hold else OK
        if args.command == "retire-inventory":
            inv = sealed_retire_inventory(root)
            _print(inv or {})
            return OK
        if args.command == "deviations":
            report = resplit_deviations(root, lump_cycle_id=args.lump_cycle_id)
            _print(report)
            return OK if not report["deviations"] and not report.get("not_evaluated") else BLOCKED
        if args.command == "campaign-proposal" and args.proposal_command == "validate":
            result = campaign_proposal_validate(
                root, proposal_path=Path(args.proposal),
                lump_inventory_path=Path(args.lump_inventory) if args.lump_inventory else None,
                loose_inventory_path=Path(args.loose_inventory) if args.loose_inventory else None,
                confirmed_constraints_path=Path(args.confirmed_constraints) if args.confirmed_constraints else None,
                lump_cycle_id=args.lump_cycle_id,
            )
            _print(result)
            return OK if result["verdict"] == "ok" else BLOCKED
        if args.command == "resplit-legacy-cycle":
            result = resplit_legacy_cycle(
                root, gate=args.gate, lump_cycle_id=args.lump_cycle_id,
                proposal=Path(args.proposal) if args.proposal else None,
                confirmed_constraints=Path(args.confirmed_constraints) if args.confirmed_constraints else None,
                route_file=Path(args.route_file) if args.route_file else None,
                backup_root=Path(args.backup_root) if args.backup_root else None,
                dry_run=args.dry_run, crash_after_phase=args.crash_after_phase, crash_at=args.crash_at,
                root_slug=args.root_slug,
            )
            _print(result)
            return OK if result.get("status") not in {"hold"} else BLOCKED
    except (ResplitError, C.CutoverError, P.ProducerError) as exc:
        _print({"error": getattr(exc, "code", "error"), "detail": getattr(exc, "detail", str(exc))})
        return BLOCKED
    return OK


if __name__ == "__main__":
    sys.exit(main())
