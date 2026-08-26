#!/usr/bin/env python3
"""W7C approval-gate executors: delta migration, compatibility switch, source retirement.

All three run only against an artifact root whose producer cutover is active
(`artifact_producer.py activate`, gate G1) and every mutation is journaled:

  migrate-delta   G2  copy the census-classified cycle candidates into one open
                      producer cycle (`campaigns/<camp>/cycles/<cyc>/artifacts/`),
                      snapshot the current `spec/` and `analysis_project/` trees
                      as staged shared-kind input, and write a W7-shaped journal,
                      inverse journal and compatibility map.  Sources are never
                      touched.
  migrate-seal    G2  after the route is closed: finalize the cycle, admit the
                      staged `spec`/`analysis` trees as new immutable shared
                      revisions (adopting the W7 references), rewrite the map.
  compat-close    G3  record the closed compatibility window and the map set that
                      legacy readers must consult (`resolve-legacy`).
  resolve-legacy  G3  map a legacy root-relative path to its live target
                      (latest map wins) or list the canonical prd candidates.
  retire          G4  verify every mapped file source against its target digest,
                      back the sources up outside the artifact root, then delete
                      them and prune the emptied legacy directories.  Excluded
                      prefixes and unverified files are always kept.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_identity  # noqa: E402
import artifact_producer as P  # noqa: E402

JOURNAL_SCHEMA = "artifact-relocation-live-journal-row/v1"
MAP_SCHEMA = "artifact-relocation-compatibility-map-row/v1"
CYCLE_BUCKETS = ("plans", "documents", "designs", "research", "experiments")
SHARED_SNAPSHOT = {"spec": "spec", "analysis_project": "analysis"}
CANDIDATE_DISPOSITIONS = (
    "after-cutoff-after_cutoff_arrival", "after-cutoff-after_cutoff_drift", "after-cutoff-after_cutoff_unstable",
    "w6-baseline-legacy", "w7-source-preserved-descendant", "post-w7-arrival",
)
OK, BLOCKED = 0, 65


class CutoverError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()
    P._write_atomic(path, data)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _require_active(root: Path) -> Dict[str, Any]:
    cut = P.read_cutover(root)
    if cut.get("state") != "active":
        raise CutoverError("cutover-inactive", "run artifact_producer.py activate (gate G1) first")
    return cut


def _excluded(rel: str, excludes: Sequence[str]) -> bool:
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in excludes)


def _has_hidden_component(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.split("/"))


def _prune_hidden_copies(root: Path, run_dir: Path, report: Dict[str, Any]) -> List[str]:
    """Remove copied targets whose locator has a hidden component (an earlier
    migrate-delta copied them before the D-6 rule was applied) and rewrite the
    journal, inverse and map without them."""
    pruned: List[str] = []
    for name in ("journal.jsonl", "inverse.jsonl", "compatibility-map.jsonl"):
        path = run_dir / name
        if not path.is_file():
            continue
        kept = []
        for row in _read_jsonl(path):
            target = row.get("target_locator", "")
            if row.get("kind", "file") == "file" and _has_hidden_component(target):
                if name == "journal.jsonl":
                    victim = root / target
                    if victim.is_file() and not os.path.islink(str(victim)):
                        victim.unlink()
                    pruned.append(row.get("source_locator", target))
                continue
            kept.append(row)
        report["digests"][name.split(".")[0].replace("compatibility-map", "compatibility_map")] = _write_jsonl(path, kept)
    if pruned:
        report["skipped_hidden_components"] = sorted(set(report.get("skipped_hidden_components", [])) | set(pruned))
        report["journal_rows"] = len(_read_jsonl(run_dir / "journal.jsonl"))
    return pruned


def migrations_dir(root: Path) -> Path:
    return P.producer_dir(root) / "migrations"


# ---------------------------------------------------------------------------
# G2 migrate-delta
# ---------------------------------------------------------------------------


def _identity_refs(identity, campaign_id: Optional[str], cycle_id: Optional[str],
                   shared: Optional[Tuple[str, str, str]] = None) -> List[Dict[str, str]]:
    rows = [
        {"binding_key": "repository", "id_kind": "repository", "required_id": "repository_id", "stable_id": identity.repository_id},
        {"binding_key": "artifact_root", "id_kind": "artifact_root", "required_id": "artifact_root_id", "stable_id": identity.artifact_root_id},
    ]
    if campaign_id:
        rows.append({"binding_key": "campaign", "id_kind": "campaign", "required_id": "campaign_id", "stable_id": campaign_id})
    if cycle_id:
        rows.append({"binding_key": "cycle", "id_kind": "cycle", "required_id": "cycle_id", "stable_id": cycle_id})
    if shared:
        kind, ref, rrev = shared
        rows.append({"binding_key": kind, "id_kind": "shared_reference", "required_id": "shared_reference_id", "stable_id": ref})
        rows.append({"binding_key": kind, "id_kind": "shared_reference_revision", "required_id": "shared_reference_revision_id", "stable_id": rrev})
    return rows


def migrate_delta(root: Path, *, census_rows: Path, route_file: Path, capability: str, intensity: str,
                  excludes: Sequence[str], approval_receipt_sha256: Optional[str], campaign_id: Optional[str],
                  campaign_key: Optional[str] = "w7c-delta-migration") -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    rows = _read_jsonl(census_rows)
    candidates = [r for r in rows if r["disposition"] in CANDIDATE_DISPOSITIONS and r["kind"] == "file"
                  and r["detail"].startswith("cycle-candidate:") and not _excluded(r["path"], excludes)]
    skipped_excluded = [r["path"] for r in rows if _excluded(r["path"], excludes) and r["kind"] == "file"]
    begun = P.begin(root, route_file=route_file, capability=capability, intensity=intensity,
                    campaign_id=campaign_id, campaign_key=campaign_key,
                    title="W7C delta migration", goal="relocate the post-W7 legacy delta into cycle output")
    if begun["status"] not in ("begun", "resumed"):
        raise CutoverError("begin-failed", begun.get("status", "?"))
    cycle_dir = Path(begun["cycle_dir"])
    run_dir = migrations_dir(root) / f"{_stamp()}-{begun['cycle_id']}"
    run_dir.mkdir(parents=True, exist_ok=False)
    journal: List[Dict[str, Any]] = []
    inverse: List[Dict[str, Any]] = []
    mapping: List[Dict[str, Any]] = []
    made_dirs: set = set()
    ordinal = 0

    def copy_one(rel: str, target_rel: str, refs) -> None:
        nonlocal ordinal
        src = root / rel
        dst = root / target_rel
        if os.path.islink(str(src)) or not src.is_file():
            raise CutoverError("source-not-regular", rel)
        parent_rel = os.path.dirname(target_rel)
        chain = []
        p = parent_rel
        while p and p not in made_dirs and not (root / p).exists():
            chain.append(p)
            p = os.path.dirname(p)
        for d in reversed(chain):
            (root / d).mkdir()
            made_dirs.add(d)
            journal.append({"schema_version": JOURNAL_SCHEMA, "row_ordinal": ordinal, "action": "create_destination",
                            "kind": "directory", "source_locator": os.path.dirname(rel), "target_locator": d,
                            "sha256": None, "size": None, "mode": 0o755, "commit_state": "committed",
                            "source_preserved": True, "link_inverse": {"action": "none"},
                            "mapping_inverse": {"action": "none"}})
            inverse.append({"ordinal": ordinal, "action": "remove_directory_if_empty", "target_locator": d})
            ordinal += 1
        verdict = P.check_write(root, dst)
        if verdict["verdict"] != "allow":
            raise CutoverError(verdict["reason"], target_rel)
        data = src.read_bytes()
        P._write_exclusive(dst, data, stat.S_IMODE(src.stat().st_mode) & 0o644 | 0o644)
        digest = hashlib.sha256(data).hexdigest()
        journal.append({"schema_version": JOURNAL_SCHEMA, "row_ordinal": ordinal, "action": "create_destination",
                        "kind": "file", "source_locator": rel, "target_locator": target_rel, "sha256": digest,
                        "size": len(data), "mode": src.stat().st_mode & 0o777, "commit_state": "committed",
                        "source_preserved": True, "link_inverse": {"action": "none"},
                        "mapping_inverse": {"action": "remove_mapping_row", "source_locator": rel}})
        inverse.append({"ordinal": ordinal, "action": "remove_file", "target_locator": target_rel, "sha256": digest})
        mapping.append({"schema_version": MAP_SCHEMA, "kind": "file", "source_locator": rel,
                        "target_locator": target_rel, "sha256": digest, "identity_refs": refs})
        ordinal += 1

    cycle_refs = _identity_refs(identity, begun["campaign_id"], begun["cycle_id"])
    per_bucket: Dict[str, int] = {}
    skipped_hidden: List[str] = []
    for row in candidates:
        rel = row["path"]
        bucket = rel.split("/", 1)[0]
        if _has_hidden_component(rel):
            skipped_hidden.append(rel)  # D-6 locators cannot name dot-files; stays legacy
            continue
        if bucket in SHARED_SNAPSHOT:
            continue  # shared kinds are snapshotted whole below
        if bucket not in CYCLE_BUCKETS:
            continue
        target_rel = os.path.relpath(str(cycle_dir / "artifacts" / rel), str(root))
        copy_one(rel, target_rel, cycle_refs)
        per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
    # Full snapshots of the current shared-kind trees (a revision is a whole copy).
    snapshot_counts: Dict[str, int] = {}
    for bucket, kind in SHARED_SNAPSHOT.items():
        base = root / bucket
        if not base.is_dir() or os.path.islink(str(base)):
            continue
        staged = "shared-input/" + kind
        n = 0
        for entry in P._walk_files(base):
            if os.path.islink(str(entry)) or not entry.is_file():
                continue
            rel = entry.relative_to(root).as_posix()
            if _excluded(rel, excludes):
                continue
            if _has_hidden_component(rel):
                skipped_hidden.append(rel)
                continue
            target_rel = os.path.relpath(str(cycle_dir / "artifacts" / staged / entry.relative_to(base).as_posix()), str(root))
            copy_one(rel, target_rel, cycle_refs)
            n += 1
        snapshot_counts[kind] = n
    digests = {
        "journal": _write_jsonl(run_dir / "journal.jsonl", journal),
        "inverse": _write_jsonl(run_dir / "inverse.jsonl", inverse),
        "compatibility_map": _write_jsonl(run_dir / "compatibility-map.jsonl", mapping),
    }
    report = {
        "schema_version": 1, "kind": "w7c-delta-migration", "state": "copied-awaiting-seal", "created_at": _now(),
        "artifact_root": str(root), "run_dir": str(run_dir), "campaign_id": begun["campaign_id"],
        "cycle_id": begun["cycle_id"], "producer_id": begun["producer_id"], "cycle_dir": str(cycle_dir),
        "route_file": str(Path(route_file).resolve()), "approval_receipt_sha256": approval_receipt_sha256,
        "census_rows": str(Path(census_rows).resolve()), "census_rows_sha256": _sha(Path(census_rows)),
        "candidates_total": len(candidates), "copied_by_bucket": per_bucket, "shared_snapshots": snapshot_counts,
        "journal_rows": len(journal), "excluded_prefixes": list(excludes), "excluded_files": len(skipped_excluded),
        "skipped_hidden_components": skipped_hidden,
        "digests": digests, "sources_touched": False,
    }
    P._write_atomic(run_dir / "report.json", P._json_bytes(report))
    return report


def migrate_seal(root: Path, *, run_dir: Path, primary: Optional[str] = None,
                 spec_reference: Optional[str] = None, analysis_reference: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    run_dir = Path(run_dir)
    report = P._read_json(run_dir / "report.json")
    if report is None or report.get("kind") != "w7c-delta-migration":
        raise CutoverError("run-report-invalid", str(run_dir))
    if report.get("state") == "sealed":
        return report
    identity = P.artifact_lifecycle.read_root_identity(root)
    cycle_id = report["cycle_id"]
    report["pruned_hidden_copies"] = _prune_hidden_copies(root, run_dir, report)
    sealed = P.finalize(root, cycle_id=cycle_id, primary=primary)
    if sealed["status"] not in ("sealed", "already-sealed"):
        raise CutoverError("finalize-failed", sealed.get("status", "?"))
    report["finalize"] = sealed
    admitted: Dict[str, Any] = {}
    references = {"spec": spec_reference, "analysis": analysis_reference}
    cycle_dir = Path(report["cycle_dir"])
    for kind in ("spec", "analysis"):
        staged = cycle_dir / "artifacts" / "shared-input" / kind
        if not staged.is_dir():
            continue
        ref = references.get(kind)
        if ref:
            _adopt_reference(root, kind, ref, title=f"{kind} (W7 reference)")
        admitted[kind] = P.admit_shared(root, cycle_id=cycle_id, kind=kind, source=f"shared-input/{kind}",
                                        reference_id=ref, key=None if ref else kind, title=f"{kind} snapshot (W7C delta)")
    report["shared_admissions"] = admitted
    # Rewrite the map: shared snapshot rows now point at the immutable revision.
    mapping = _read_jsonl(run_dir / "compatibility-map.jsonl")
    rewritten = []
    for row in mapping:
        target = row["target_locator"]
        marker = "/artifacts/shared-input/"
        if marker in target:
            kind, _, rest = target.split(marker, 1)[1].partition("/")
            adm = admitted.get(kind)
            if adm:
                row = dict(row)
                row["target_locator"] = os.path.relpath(adm["revision_dir"], str(root)) + "/" + rest
                row["identity_refs"] = _identity_refs(identity, report["campaign_id"], cycle_id,
                                                      (kind, adm["shared_reference_id"], adm["shared_reference_revision_id"]))
        rewritten.append(row)
    report["digests"]["compatibility_map"] = _write_jsonl(run_dir / "compatibility-map.jsonl", rewritten)
    report["state"] = "sealed"
    report["sealed_at"] = _now()
    P._write_atomic(run_dir / "report.json", P._json_bytes(report))
    return report


def _tree_digest(directory: Path) -> Dict[str, Any]:
    """Byte-conservation witness: sorted (rel, size, sha256) over every regular file."""
    rows = []
    for entry in P._walk_files(directory):
        if os.path.islink(str(entry)) or not entry.is_file():
            continue
        rows.append((entry.relative_to(directory).as_posix(), entry.stat().st_size, _sha(entry)))
    rows.sort()
    payload = "\n".join(f"{r}\t{n}\t{d}" for r, n, d in rows).encode("utf-8")
    return {"file_count": len(rows), "byte_count": sum(n for _, n, _ in rows),
            "tree_sha256": hashlib.sha256(payload).hexdigest()}


def seal_legacy_cycle(root: Path, *, cycle_dir: Path, route_file: Path, capability: str = "autopilot-code",
                      title: Optional[str] = None, started_on: Optional[str] = None,
                      primary: Optional[str] = None, exclude_hidden: bool = False,
                      allocator=None) -> Dict[str, Any]:
    """W7E: adopt a producer record for an existing `campaigns/<camp>/cycles/<cyc>` directory
    that was created outside the producer (the W7 relocation), then run the ordinary
    finalize (manifest build, validation, index apply).  Bytes under `artifacts/` are
    never touched; the route must already be closed (this is a retrospective seal)."""
    root = Path(root).resolve()
    _require_active(root)
    directory = Path(cycle_dir).resolve()
    try:
        rel = directory.relative_to(root)
    except ValueError as exc:
        raise CutoverError("cycle-dir-outside-root", str(directory)) from exc
    parts = rel.parts
    if len(parts) != 4 or parts[0] != "campaigns" or parts[2] != "cycles":
        raise CutoverError("cycle-dir-shape-invalid", str(rel))
    campaign_id, cycle_id = parts[1], parts[3]
    if not (directory / "artifacts").is_dir():
        raise CutoverError("artifacts-dir-missing", str(rel))
    if (directory / "manifest.json").exists():
        raise CutoverError("manifest-already-present", str(rel))
    existing = P.read_cycle_record(root, cycle_id)
    if existing is not None and existing.get("state") != "no-lineage":
        raise CutoverError("cycle-record-exists", existing.get("state", "?"))
    campaign = P.read_campaign(root, campaign_id)
    if campaign is None:
        raise CutoverError("campaign-unknown", campaign_id)
    if capability not in P.ENTRY_CAPABILITIES:
        raise CutoverError("capability-unknown", capability)
    route = P.load_route(root, Path(route_file))
    if not P.route_is_closed(root, route):
        raise CutoverError("route-not-closed", route["route_id"])
    if route["capability"] != capability:
        raise CutoverError("route-capability-mismatch", f"{route['capability']}!={capability}")
    before = _tree_digest(directory / "artifacts")
    alloc = allocator or P.artifact_identity.IdAllocator()
    record = {
        "schema_version": 1, "contract": P.CONTRACT, "cycle_id": cycle_id, "campaign_id": campaign_id,
        "producer_id": alloc.allocate("producer"), "parent_cycle_id": None,
        "capability": capability, "route_capability": route["capability"], "intensity": route["effective_intensity"],
        "route_id": route["route_id"], "route_hash": route["route_hash"],
        "route_file": str(Path(route_file).resolve()), "node_id": None, "state": "open",
        "started_on": started_on or _now(), "sealed_on": None, "manifest_digest": None,
        "title": title or f"{capability} legacy cycle (retrospective seal)",
        "adopted": {"kind": "seal-legacy-cycle", "adopted_on": _now(), "tree_before": before},
    }
    P._write_cycle_record(root, record, exclusive=existing is None)
    if cycle_id not in campaign.get("cycles", []):
        campaign["cycles"] = list(campaign.get("cycles", [])) + [cycle_id]
        P._write_campaign(root, campaign, exclusive=False)
    try:
        sealed = P.finalize(root, cycle_id=cycle_id, primary=primary, allocator=alloc, exclude_hidden=exclude_hidden)
    except P.ProducerError as exc:
        # Leave no half-adopted record behind; the directory itself is untouched.
        P.cycle_record_path(root, cycle_id).unlink(missing_ok=True)
        raise CutoverError("finalize-failed", f"{exc.code}: {exc.detail}") from exc
    if sealed.get("status") != "sealed":
        P.cycle_record_path(root, cycle_id).unlink(missing_ok=True)
        raise CutoverError("finalize-failed", sealed.get("status", "?"))
    after = _tree_digest(directory / "artifacts")
    if after != before:
        raise CutoverError("bytes-changed", json.dumps({"before": before, "after": after}))
    excluded = list(sealed.get("excluded_hidden") or [])
    if excluded:
        # Durable trace of what the manifest deliberately does not list.
        sealed_record = P.read_cycle_record(root, cycle_id) or {}
        sealed_record.setdefault("adopted", {})["hidden_excluded"] = [
            {"path": rel, "reason": P._unmanifestable_reason(rel), "sha256": _sha(directory / rel),
             "byte_size": (directory / rel).stat().st_size} for rel in excluded]
        P._write_cycle_record(root, sealed_record, exclusive=False)
    return {"status": "sealed", "cycle_id": cycle_id, "campaign_id": campaign_id, "route_id": route["route_id"],
            "producer_id": record["producer_id"], "manifest_digest": sealed["manifest_digest"],
            "artifact_count": sealed["artifact_count"], "hidden_excluded": len(excluded),
            "tree": after, "bytes_unchanged": True}


def adopt_campaign(root: Path, campaign_id: str, *, title: str, goal: str) -> Dict[str, Any]:
    """Create `campaign.json` for a W7-relocated campaign directory that has none.

    The W7 E2/E3 relocation created `campaigns/<camp>/cycles/<cyc>/artifacts/`
    additively without producer records; adopting the campaign lists its
    existing cycle directories so `begin --campaign` can join it.
    """
    root = Path(root).resolve()
    _require_active(root)
    if not artifact_identity.is_well_formed(campaign_id, "campaign"):
        raise CutoverError("campaign-id-malformed", campaign_id)
    existing = P.read_campaign(root, campaign_id)
    if existing is not None:
        return existing
    directory = P.campaign_dir(root, campaign_id)
    if not directory.is_dir() or os.path.islink(str(directory)):
        raise CutoverError("campaign-dir-missing", str(directory))
    cycles_dir = directory / "cycles"
    cycles = sorted(p.name for p in cycles_dir.iterdir()
                    if p.is_dir() and artifact_identity.is_well_formed(p.name, "cycle")) if cycles_dir.is_dir() else []
    record = {
        "schema_version": 1, "contract": P.CONTRACT, "campaign_id": campaign_id, "key": f"adopted:{campaign_id}",
        "title": title, "goal": goal, "completion_criterion": {"statement": "every cycle sealed with a manifest"},
        "state": "active", "created_on": _now(), "adopted_from": "w7-e2-e3-relocation", "cycles": cycles,
    }
    P._write_campaign(root, record, exclusive=True)
    return record


def _adopt_reference(root: Path, kind: str, ref_id: str, *, title: str) -> Dict[str, Any]:
    """Create `reference.json` for a W7-relocated shared reference that has none."""
    if not artifact_identity.is_well_formed(ref_id, "shared_reference"):
        raise CutoverError("reference-id-malformed", ref_id)
    path = P._reference_path(root, kind, ref_id)
    existing = P._read_json(path)
    if existing is not None:
        return existing
    revisions_dir = path.parent / "revisions"
    if not revisions_dir.is_dir():
        raise CutoverError("reference-unknown", ref_id)
    revisions = sorted(p.name for p in revisions_dir.iterdir()
                       if p.is_dir() and artifact_identity.is_well_formed(p.name, "shared_reference_revision"))
    record = {
        "schema_version": 1, "contract": P.CONTRACT, "shared_reference_id": ref_id, "kind": P.SHARED_KINDS[kind],
        "key": kind, "title": title, "created_on": _now(), "adopted_from": "w7-e2-e3-relocation",
        "latest_revision_id": revisions[-1] if revisions else None, "revisions": revisions,
    }
    P._write_exclusive(path, P._json_bytes(record))
    return record


# ---------------------------------------------------------------------------
# G3 compat-close / resolve-legacy
# ---------------------------------------------------------------------------


def compat_path(root: Path) -> Path:
    return P.producer_dir(root) / "compat.json"


def compat_close(root: Path, *, maps: Sequence[Path], approval_receipt_sha256: Optional[str]) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    rows = []
    for m in maps:
        m = Path(m).resolve()
        if not m.is_file():
            raise CutoverError("map-missing", str(m))
        rows.append({"path": str(m), "sha256": _sha(m), "rows": sum(1 for _ in open(m, encoding="utf-8") if _.strip())})
    body = {"schema_version": 1, "contract": P.CONTRACT, "compatibility_window": "closed", "closed_at": _now(),
            "maps": rows, "approval_receipt_sha256": approval_receipt_sha256,
            "legacy_readers": "resolve through artifact_cutover.py resolve-legacy; latest map wins"}
    P._ensure_dir(compat_path(root).parent)
    P._write_atomic(compat_path(root), P._json_bytes(body), 0o600)
    return body


def _load_maps(root: Path) -> List[Tuple[str, Dict[str, str]]]:
    compat = P._read_json(compat_path(root)) or {}
    out = []
    for entry in compat.get("maps", []):
        path = Path(entry["path"])
        if not path.is_file():
            continue
        table = {}
        for row in _read_jsonl(path):
            table[row["source_locator"]] = row["target_locator"]
        out.append((str(path), table))
    return out


def resolve_legacy(root: Path, rel: str) -> Dict[str, Any]:
    root = Path(root).resolve()
    rel = rel.strip("/")
    direct = root / rel
    if direct.exists() and not os.path.islink(str(direct)):
        return {"path": rel, "resolution": "present", "target": rel, "absolute": str(direct)}
    maps = _load_maps(root)
    for name, table in reversed(maps):  # latest map wins
        if rel in table and (root / table[rel]).exists():
            return {"path": rel, "resolution": "mapped", "target": table[rel], "absolute": str(root / table[rel]), "map": name}
    # longest mapped ancestor directory
    parts = rel.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        ancestor = "/".join(parts[:depth])
        tail = "/".join(parts[depth:])
        for name, table in reversed(maps):
            if ancestor in table and (root / table[ancestor] / tail).exists():
                return {"path": rel, "resolution": "mapped-ancestor", "target": table[ancestor] + "/" + tail,
                        "absolute": str(root / table[ancestor] / tail), "map": name}
    return {"path": rel, "resolution": "unresolved", "target": None, "absolute": None}


def latest_shared_revision(root: Path, kind: str) -> Optional[Path]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return None
    best: Optional[Tuple[str, Path]] = None
    for ref in sorted(base.iterdir()):
        record = P._read_json(ref / "reference.json")
        if record and record.get("latest_revision_id"):
            candidate = ref / "revisions" / record["latest_revision_id"]
            stamp = str(record.get("updated_on") or record.get("created_on") or "")
        else:
            revs = sorted(p for p in (ref / "revisions").iterdir() if p.is_dir()) if (ref / "revisions").is_dir() else []
            if not revs:
                continue
            candidate = revs[-1]
            stamp = ""
        if candidate.is_dir() and (best is None or stamp >= best[0]):
            best = (stamp, candidate)
    return best[1] if best else None


def prd_candidates(root: Path) -> List[str]:
    """Canonical prd.md candidates: legacy `spec/` first, else the latest shared/spec revision."""
    root = Path(root).resolve()
    out: List[str] = []
    legacy = root / "spec"
    if legacy.is_dir():
        if (legacy / "prd.md").is_file():
            out.append(str(legacy / "prd.md"))
        for d in sorted(legacy.iterdir()):
            if d.is_dir() and d.name != "_internal" and (d / "prd.md").is_file():
                out.append(str(d / "prd.md"))
    if out:
        return out
    revision = latest_shared_revision(root, "spec")
    if revision is None:
        return out
    if (revision / "prd.md").is_file():
        out.append(str(revision / "prd.md"))
    for d in sorted(revision.iterdir()):
        if d.is_dir() and d.name != "_internal" and (d / "prd.md").is_file():
            out.append(str(d / "prd.md"))
    return out


# ---------------------------------------------------------------------------
# G4 retire
# ---------------------------------------------------------------------------


def retire(root: Path, *, maps: Sequence[Path], backup_root: Path, excludes: Sequence[str],
           approval_receipt_sha256: Optional[str], dry_run: bool = False) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    rows: List[Dict[str, Any]] = []
    for m in maps:
        rows.extend(_read_jsonl(Path(m)))
    # latest map wins for a source seen more than once: later rows overwrite
    files: Dict[str, List[str]] = {}
    dirs: set = set()
    for row in rows:
        src = row["source_locator"]
        if _excluded(src, excludes):
            continue
        if row.get("kind") == "directory":
            dirs.add(src)
        else:
            files.setdefault(src, []).append(row["target_locator"])
    verified: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    absent = 0
    for src, targets in sorted(files.items()):
        path = root / src
        if os.path.islink(str(path)):
            kept.append({"source": src, "reason": "symlink"})
            continue
        if not path.exists():
            absent += 1
            continue
        if not path.is_file():
            kept.append({"source": src, "reason": "not-regular"})
            continue
        digest = _sha(path)
        match = None
        for target in targets:
            tpath = root / target
            if tpath.is_file() and not os.path.islink(str(tpath)) and _sha(tpath) == digest:
                match = target
                break
        if match is None:
            kept.append({"source": src, "reason": "no-target-with-identical-digest", "targets": targets})
            continue
        verified.append({"source": src, "target": match, "sha256": digest, "size": path.stat().st_size})
    stamp = _stamp()
    run_dir = migrations_dir(root) / f"{stamp}-retirement"
    backup_dir = Path(backup_root).resolve() / identity.artifact_root_id / stamp
    report: Dict[str, Any] = {
        "schema_version": 1, "kind": "w7c-source-retirement", "created_at": _now(), "dry_run": dry_run,
        "artifact_root": str(root), "run_dir": str(run_dir), "backup_dir": str(backup_dir),
        "approval_receipt_sha256": approval_receipt_sha256, "map_files": [str(Path(m).resolve()) for m in maps],
        "excluded_prefixes": list(excludes), "verified_files": len(verified), "kept_files": len(kept),
        "already_absent": absent, "kept": kept[:500],
    }
    if dry_run:
        report["verified_sample"] = verified[:20]
        return report
    if Path(backup_root).resolve() == root or str(Path(backup_root).resolve()).startswith(str(root) + "/"):
        raise CutoverError("backup-root-inside-artifact-root", str(backup_root))
    run_dir.mkdir(parents=True, exist_ok=False)
    backup_dir.mkdir(parents=True, exist_ok=False)
    archive = backup_dir / "retired-sources.tar.gz"
    with tarfile.open(str(archive), "w:gz") as tar:
        for row in verified:
            tar.add(str(root / row["source"]), arcname=row["source"], recursive=False)
    with open(archive, "rb") as fh:
        os.fsync(fh.fileno())
    archive_sha = _sha(archive)
    manifest_sha = _write_jsonl(backup_dir / "retired-manifest.jsonl", verified)
    seal = {"schema_version": 1, "archive": str(archive), "archive_sha256": archive_sha, "manifest_sha256": manifest_sha,
            "file_count": len(verified), "byte_size": sum(r["size"] for r in verified), "artifact_root_id": identity.artifact_root_id,
            "created_at": _now()}
    P._write_atomic(backup_dir / "backup-seal.json", P._json_bytes(seal))
    # verify the archive before deleting anything
    with tarfile.open(str(archive), "r:gz") as tar:
        names = set(tar.getnames())
    missing = [r["source"] for r in verified if r["source"] not in names]
    if missing:
        raise CutoverError("backup-incomplete", f"{len(missing)} sources missing from archive")
    journal: List[Dict[str, Any]] = []
    for ordinal, row in enumerate(verified):
        (root / row["source"]).unlink()
        journal.append({"schema_version": "artifact-retirement-journal-row/v1", "row_ordinal": ordinal, "action": "retire_source",
                        "source_locator": row["source"], "target_locator": row["target"], "sha256": row["sha256"],
                        "backup_archive": str(archive), "commit_state": "committed"})
    # prune emptied directories: mapped source dirs plus their ancestors inside the legacy buckets
    pruned = []
    candidates = set(dirs)
    for row in verified:
        p = os.path.dirname(row["source"])
        while p:
            candidates.add(p)
            p = os.path.dirname(p)
    for d in sorted(candidates, key=lambda s: -s.count("/")):
        if _excluded(d, excludes) or d.split("/", 1)[0] in ("campaigns", "shared") or d.startswith("."):
            continue
        path = root / d
        if path.is_dir() and not os.path.islink(str(path)):
            try:
                path.rmdir()
                pruned.append(d)
            except OSError:
                pass
    report.update({"backup_seal": seal, "retired_files": len(verified), "pruned_directories": len(pruned),
                   "pruned_top_level": sorted(d for d in pruned if "/" not in d),
                   "journal_sha256": _write_jsonl(run_dir / "journal.jsonl", journal)})
    P._write_atomic(run_dir / "report.json", P._json_bytes(report))
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("migrate-delta")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--census-rows", required=True, help="jsonl rows from artifact-delta-census.py --rows-output")
    p.add_argument("--route", required=True)
    p.add_argument("--capability", default="autopilot-code")
    p.add_argument("--intensity", default="direct")
    p.add_argument("--campaign")
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--approval-receipt-sha256")
    p = sub.add_parser("adopt-campaign")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--campaign", required=True)
    p.add_argument("--title", default="W7 relocation campaign")
    p.add_argument("--goal", default="artifact knowledge index relocation (W7) and its W7C delta")
    p = sub.add_parser("migrate-seal")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--primary")
    p.add_argument("--spec-reference")
    p.add_argument("--analysis-reference")
    p = sub.add_parser("seal-legacy-cycle")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle-dir", required=True)
    p.add_argument("--route", required=True, help="a closed route owned by the sealing session")
    p.add_argument("--capability", default="autopilot-code")
    p.add_argument("--title")
    p.add_argument("--started-on", help="RFC3339 start instant of the original transaction")
    p.add_argument("--primary")
    p.add_argument("--exclude-hidden", action="store_true",
                   help="leave files that cannot carry a D-6 locator (dot components, over-long components) out of the manifest; recorded in the cycle record")
    p = sub.add_parser("compat-close")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--map", action="append", required=True)
    p.add_argument("--approval-receipt-sha256")
    p = sub.add_parser("resolve-legacy")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--path")
    p.add_argument("--prd-candidates", action="store_true")
    p = sub.add_parser("retire")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--map", action="append", required=True)
    p.add_argument("--backup-root", required=True)
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--approval-receipt-sha256")
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.artifact_root)
    try:
        if args.command == "migrate-delta":
            result = migrate_delta(root, census_rows=Path(args.census_rows), route_file=Path(args.route),
                                   capability=args.capability, intensity=args.intensity, excludes=args.exclude,
                                   approval_receipt_sha256=args.approval_receipt_sha256, campaign_id=args.campaign)
        elif args.command == "adopt-campaign":
            result = adopt_campaign(root, args.campaign, title=args.title, goal=args.goal)
        elif args.command == "seal-legacy-cycle":
            result = seal_legacy_cycle(root, cycle_dir=Path(args.cycle_dir), route_file=Path(args.route),
                                       capability=args.capability, title=args.title, started_on=args.started_on,
                                       primary=args.primary, exclude_hidden=args.exclude_hidden)
        elif args.command == "migrate-seal":
            result = migrate_seal(root, run_dir=Path(args.run_dir), primary=args.primary,
                                  spec_reference=args.spec_reference, analysis_reference=args.analysis_reference)
        elif args.command == "compat-close":
            result = compat_close(root, maps=[Path(m) for m in args.map], approval_receipt_sha256=args.approval_receipt_sha256)
        elif args.command == "resolve-legacy":
            if args.prd_candidates:
                for line in prd_candidates(root):
                    print(line)
                return OK
            if not args.path:
                parser.error("--path or --prd-candidates required")
            result = resolve_legacy(root, args.path)
            print(json.dumps(result, sort_keys=True))
            return OK if result["resolution"] != "unresolved" else BLOCKED
        elif args.command == "retire":
            result = retire(root, maps=[Path(m) for m in args.map], backup_root=Path(args.backup_root),
                            excludes=args.exclude, approval_receipt_sha256=args.approval_receipt_sha256, dry_run=args.dry_run)
        else:  # pragma: no cover
            parser.error("unknown command")
            return 64
    except (CutoverError, P.ProducerError) as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code, "detail": exc.detail}, sort_keys=True))
        return BLOCKED
    except P.artifact_admission.AdmissionRecoveryRequired as exc:
        print(json.dumps({"status": "blocked", "reason": "recovery-required", "detail": str(exc)}, sort_keys=True))
        return BLOCKED
    print(json.dumps({k: v for k, v in result.items() if k not in ("kept",)}, sort_keys=True, default=str))
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
