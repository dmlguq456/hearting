#!/usr/bin/env python3
"""W7G retrospective resplit of W7C lump cycles (PRD v12 §29). Owns R1~R3 and the
D-80 proposal validator; compat append and retire stay in artifact_cutover."""
from __future__ import annotations

import argparse
import base64
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
CAMPAIGN_SLUG_RE = re.compile(r"^[a-z0-9-]{3,48}$")
UNASSIGNED_SLUG = "_unassigned"
CYCLE_BUCKETS = C.CYCLE_BUCKETS
SHARED_SNAPSHOT = C.SHARED_SNAPSHOT
LANES = ("semantic-boundary", "relationship", "display-quality")
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


def _canonical_digest(body: Dict[str, Any]) -> str:
    stripped = {k: v for k, v in body.items() if k != "digest"}
    return "sha256:" + P._digest(P._canonical(stripped))


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


def _lump_started_on(cdir: Path, bucket: str, depth1_name: str, files: Sequence[Dict[str, Any]],
                     record_started_on: Optional[str]) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_", depth1_name)
    if m:
        return m.group(1)
    written = _entry_document_written_date(cdir, bucket, depth1_name, files)
    if written:
        return written
    return record_started_on or ""


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
    slug = root_slug or _slugify(root.name)
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
            cycle_units_out.append({
                "cycle_key": f"legacy:{slug}:{bucket}/{depth1}", "bucket": bucket, "depth1_name": depth1,
                "started_on": _lump_started_on(cdir, bucket, depth1, files, record.get("started_on")), "title": depth1,
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
    return "sha256:" + P._digest(P._canonical(rows))


def _valid_admitted_run(root: Path, run_dir: Path) -> Optional[Dict[str, Any]]:
    marker = P._read_json(_marker_path(run_dir))
    if not isinstance(marker, dict) or marker.get("kind") != "w7g-admission-marker":
        return None
    if _bundle_digest(_admission_dir(run_dir)) != marker.get("bundle_digest"):
        return None
    return marker


def lump_index(root: Path) -> Dict[str, Any]:
    """Sealed lump inventory from the latest verifiably-admitted run, else a fresh scan."""
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
    return scan_lumps(root)


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


def resplit_hold(root: Path) -> Optional[Dict[str, Any]]:
    """D-77-a: hold is owned by the nonterminal journal on disk, never process liveness."""
    root = Path(root).resolve()
    holds = []
    for run_dir in _run_dirs(root):
        for gate, terminal in (("r2", R2_TERMINAL), ("r3", R3_TERMINAL)):
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
        events_path = run_dir / "events.jsonl"
        if not events_path.is_file():
            continue
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
    root_slug = proposal.get("root_slug") or lump_inventory.get("root_slug") or _slugify(root.name)
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
    loose_target_by_source = {
        row["source_locator"]: row["target_cycle_key"] for row in proposal.get("loose_assignments", [])
    }
    rules_checked.append("8")
    err = _rule_8_confirmed_constraints(proposal, confirmed_constraints, slug_for_target, loose_target_by_source)
    if err:
        return {"verdict": "hold", **err, "rules_checked": rules_checked}
    campaigns_out = []
    for camp_row in proposal.get("campaigns", []):
        slug = camp_row["slug"]
        units = [u for u in lump["cycle_units"] if slug_for_target.get(u["cycle_key"]) == slug]
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
        "root_slug": root_slug, "lump": lump,
    }


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


def _r1(root: Path, *, lump_cycle_id: str, proposal_path: Path,
       confirmed_constraints_path: Optional[Path], dry_run: bool,
       crash_after_phase: Optional[str]) -> Dict[str, Any]:
    root = Path(root).resolve()
    C._require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    proposal_bytes = Path(proposal_path).read_bytes()
    plan_sha256 = "sha256:" + hashlib.sha256(proposal_bytes).hexdigest()
    proposal = json.loads(proposal_bytes.decode("utf-8"))
    if dry_run:
        lump_inventory = scan_lumps(root, root_slug=proposal.get("root_slug"))
        loose_inventory = _build_loose_inventory(root)
        confirmed = P._read_json(Path(confirmed_constraints_path)) if confirmed_constraints_path else None
        verdict = validate_proposal(proposal, root=root, lump_inventory=lump_inventory,
                                    loose_inventory=loose_inventory, confirmed_constraints=confirmed,
                                    lump_cycle_id=lump_cycle_id)
        return {"status": "dry-run", "plan_sha256": plan_sha256, **verdict}
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
    lump_inventory = scan_lumps(root, root_slug=proposal.get("root_slug"))
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
    retire_inventory = _build_retire_inventory(root, [], identity)
    P._write_atomic(admission_dir / "lump-inventory.json", P._json_bytes(lump_inventory))
    P._write_atomic(admission_dir / "loose-inventory.json", P._json_bytes(loose_inventory))
    P._write_atomic(admission_dir / "retire-inventory.json", P._json_bytes(retire_inventory))
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
            "lump_cycle_id": lump_cycle_id, "verdict": verdict}


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


def _per_cycle_route(route: Dict[str, Any], cycle_key: str, run_dir: Path) -> Tuple[Dict[str, Any], Path]:
    """D-71 requires (artifact_root_id, route_id) uniqueness per finalized cycle -- a batch
    admitting several new cycles under one caller-supplied route needs a distinct route
    identity per cycle. Derives one deterministically from the caller's route + cycle_key so
    reruns are idempotent (same cycle_key -> same synthetic route_id).

    🟡3 spec-impact (S2 plan §7): this per-cycle synthetic route identity is a
    design choice made to satisfy D-71 uniqueness, not something the PRD/plan
    specifies. Its rules, as implemented here (not yet ratified elsewhere):
    - **identity**: `route_id = "rt-" + sha256(f"{caller_route_id}:{cycle_key}")[:16]`,
      deterministic and stable across reruns of the same admitted proposal.
    - **provenance**: the derived route is a full copy of the caller's route
      (same capability/intensity/gates) plus `resplit_cycle_key`, persisted
      once under `<run_dir>/routes/<route_id>.json` -- it is not registered
      in any route ledger outside this run_dir.
    - **authority**: it carries no capability beyond what the caller's route
      already granted; it exists solely so each new cycle's `finalize` call
      has a distinct `route_id` to bind to, not as an independently
      authorized route.
    """
    seed = f"{route['route_id']}:{cycle_key}".encode()
    digest = hashlib.sha256(seed).hexdigest()
    derived = dict(route)
    derived["route_id"] = "rt-" + digest[:16]
    derived["route_hash"] = "sha256:" + digest
    derived["resplit_cycle_key"] = cycle_key
    path = run_dir / "routes" / f"{derived['route_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        P._write_atomic(path, P._json_bytes(derived))
    return derived, path


def _r2_prepare(root: Path, campaign_plan: List[Dict[str, Any]], alloc, lump_cycle_id: str,
                route: Dict[str, Any], route_file: Path, run_dir: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    lump_record = P.read_cycle_record(root, lump_cycle_id) or {}
    campaign_pre: List[Dict[str, Any]] = []
    cycle_pre: List[Dict[str, Any]] = []
    created_dirs: List[str] = []
    created_cycle_dirs: List[str] = []
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
                "depth1_name": unit["depth1_name"], "started_on": unit["started_on"], "title": unit["title"],
                "files": unit["files"],
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
            cyc_route, cyc_route_file = _per_cycle_route(route, cyc["cycle_key"], run_dir)
            record = {
                "schema_version": 1, "contract": P.CONTRACT, "cycle_id": cid, "campaign_id": camp["campaign_id"],
                "producer_id": alloc.allocate("producer"), "parent_cycle_id": None,
                "capability": cyc_route.get("capability", "autopilot-code"),
                "route_capability": cyc_route.get("capability", "autopilot-code"),
                "intensity": cyc_route.get("effective_intensity", "standard"),
                "route_id": cyc_route["route_id"], "route_hash": cyc_route["route_hash"],
                "route_file": str(cyc_route_file.resolve()), "node_id": None, "state": "open",
                "started_on": C._now(), "sealed_on": None, "manifest_digest": None,
                "title": cyc["title"], "cycle_key": cyc["cycle_key"], "derived_from_cycle_id": lump_cycle_id,
                "resplit_started_on": cyc["started_on"],
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
    }
    return pre_image, cycles_out


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
    if inverse_path.is_file():
        inverse_path.unlink()
    journal["phase"] = "rolled-back"
    P._write_atomic(run_dir / "journal-r2.json", P._json_bytes(journal))


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
        return {"status": "already-applied", "run_dir": str(run_dir), "journal": journal}
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
            pre_image, cycles = _r2_prepare(root, campaign_plan, alloc, lump_cycle_id, route, route_file, run_dir)
            journal = {
                "schema_version": 1, "kind": "w7g-resplit-journal", "gate": "r2", "plan_sha256": plan_sha256,
                "lump_cycle_id": lump_cycle_id, "run_dir": str(run_dir), "phase": "prepared",
                "started_at": C._now(), "pre_image": pre_image, "cycles": cycles,
                "created_dirs": pre_image["created_dirs"], "created_cycle_dirs": pre_image["created_cycle_dirs"],
                "route_id": route["route_id"],
            }
            P._write_atomic(journal_path, P._json_bytes(journal))
        if dry_run:
            return {"status": "dry-run", "journal": journal}
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
        return {"status": journal["phase"], "run_dir": str(run_dir), "journal": journal}
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


def _r2_witness(cyc: Dict[str, Any]) -> None:
    actual = C._tree_digest(Path(cyc["cycle_dir"]) / "artifacts")
    expected = _expected_tree_digest(cyc["files"])
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
    sealed = P.finalize(root, cycle_id=first["cycle_id"], state="completed", allow_open_route=True)
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
        sealed = P.finalize(root, cycle_id=cyc["cycle_id"], state="completed", allow_open_route=True)
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
        sealed = P.finalize(root, cycle_id=cyc["cycle_id"], state="completed", allow_open_route=True)
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
    identity = P.artifact_lifecycle.read_root_identity(root)
    alloc = allocator or artifact_identity.IdAllocator()
    new_cycle_ids = [c["cycle_id"] for c in r2_journal["cycles"]]
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
        if dry_run:
            return {"status": "dry-run", "journal": journal}
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
    for name in ("legacy-artifacts.tar", "backup-seal.json"):
        p = run_dir / name
        if p.is_file():
            p.unlink()
    journal["phase"] = "rolled-back"
    P._write_atomic(run_dir / "journal-r3.json", P._json_bytes(journal))


def _r3_execute(root: Path, run_dir: Path, journal: Dict[str, Any], identity, alloc, backup_root: Optional[Path],
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
    if phase == "events-appended":
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
            C._write_jsonl(map_path, rows)
            C.compat_append(root, maps=[map_path], supersedes=[])
            written_maps = [os.path.relpath(map_path, root)]
            pre_image["written_map_files"] = written_maps
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
        _r3_backup(root, run_dir, lump_dir, crash_at=crash_at)
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
        journal["phase"] = "complete"
        P._write_atomic(journal_path, P._json_bytes(journal))


def _r3_backup(root: Path, run_dir: Path, lump_dir: Path, *, crash_at: Optional[str]) -> None:
    artifacts = lump_dir / "artifacts"
    archive = run_dir / "legacy-artifacts.tar"
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
    P._write_atomic(run_dir / "backup-seal.json", P._json_bytes(seal))
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
    allocator=None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if gate == "r1":
        if proposal is None:
            raise ResplitError("admission-marker-missing", "proposal-required")
        return _r1(root, lump_cycle_id=lump_cycle_id, proposal_path=Path(proposal),
                  confirmed_constraints_path=Path(confirmed_constraints) if confirmed_constraints else None,
                  dry_run=dry_run, crash_after_phase=crash_after_phase)
    if gate == "r2":
        return _r2(root, lump_cycle_id=lump_cycle_id, route_file=route_file, dry_run=dry_run,
                  crash_after_phase=crash_after_phase, crash_at=crash_at, allocator=allocator)
    if gate == "r3":
        return _r3(root, lump_cycle_id=lump_cycle_id, backup_root=backup_root, dry_run=dry_run,
                  crash_after_phase=crash_after_phase, crash_at=crash_at, allocator=allocator)
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

    sub.add_parser("lump-index")
    sub.add_parser("hold")
    sub.add_parser("retire-inventory")

    validate_p = sub.add_parser("campaign-proposal")
    validate_sub = validate_p.add_subparsers(dest="proposal_command", required=True)
    v = validate_sub.add_parser("validate")
    v.add_argument("--proposal", required=True)
    v.add_argument("--lump-inventory")
    v.add_argument("--loose-inventory")
    v.add_argument("--confirmed-constraints")
    v.add_argument("--lump-cycle-id")

    r = sub.add_parser("resplit-legacy-cycle")
    r.add_argument("--gate", required=True, choices=("r1", "r2", "r3"))
    r.add_argument("--lump-cycle-id", required=True)
    r.add_argument("--proposal")
    r.add_argument("--confirmed-constraints")
    r.add_argument("--route-file")
    r.add_argument("--backup-root")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--crash-after-phase")
    r.add_argument("--crash-at")

    args = parser.parse_args(argv)
    root = Path(args.artifact_root)
    try:
        if args.command == "lump-index":
            _print(lump_index(root))
            return OK
        if args.command == "hold":
            hold = resplit_hold(root)
            _print(hold or {})
            return BLOCKED if hold else OK
        if args.command == "retire-inventory":
            inv = sealed_retire_inventory(root)
            _print(inv or {})
            return OK
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
            )
            _print(result)
            return OK if result.get("status") not in {"hold"} else BLOCKED
    except (ResplitError, C.CutoverError, P.ProducerError) as exc:
        _print({"error": getattr(exc, "code", "error"), "detail": getattr(exc, "detail", str(exc))})
        return BLOCKED
    return OK


if __name__ == "__main__":
    sys.exit(main())
