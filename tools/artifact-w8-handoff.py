#!/usr/bin/env python3
"""W7E: build the Cairn W8 handoff bundle from sealed artifact-root state.

Read-only over the artifact root except for the bundle directory, which must be
inside an OPEN producer cycle (`--bundle-dir` under `.../artifacts/`).  Every row
of the C-P0 REPORT §11 table becomes one file; `handoff.json` indexes them with
sha256 digests.  Only stable IDs, locators, digests, and counts are emitted —
never note bodies or secrets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_admission as adm  # noqa: E402
import artifact_lifecycle as L  # noqa: E402
import artifact_producer as P  # noqa: E402

SCHEMA = "hearting-w8-handoff-bundle/v1"
NOTES_SCHEMA = "cairn-w8-notes/v1"
# Cairn PRD §76.5 14항 body-free column allowlist. Anything else is refused before the bundle is written.
NOTE_ALLOWED_KEYS = frozenset({"id", "parent_id", "page_no", "repo", "source_dir", "source_capability", "trashed_at", "revision"})
NOTE_FORBIDDEN_KEYS = frozenset({"body", "title", "generated_title", "excerpt", "summary"})


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def sha_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


PRIMARY_NAMES = ("final_report.md", "report.md", "prd.md", "plan.md")


def pick_primary(rows):
    """Report landing page: by NAME PRIORITY first, then the shallowest path.

    Members are listed by locator, so a plain "first row whose name is in the set" picked
    `_internal/prompts/plan.md` over the report's own `final_report.md` (2026-08-28 Cairn W10b:
    10 of 12 residual hierarchy conflicts). The user-facing landing page is the highest-priority
    name at the shallowest depth; the locator order of members carries no meaning here.
    """
    for name in PRIMARY_NAMES:
        candidates = [r for r in rows if r["locator"].rsplit("/", 1)[1] == name]
        if candidates:
            return min(candidates, key=lambda r: (r["locator"].count("/"), r["locator"]))
    return rows[0]


class NotesInputError(ValueError):
    pass


def _forbidden_keys(obj, found=None):
    found = [] if found is None else found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in NOTE_FORBIDDEN_KEYS:
                found.append(str(k))
            _forbidden_keys(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _forbidden_keys(v, found)
    return found


def load_notes(path: Path):
    """Load the Cairn-exported body-free note population (`cairn-w8-notes/v1`).

    Cairn owns the note population (Hearting has no read access to `l2_notes`), so
    the export is produced on the Cairn side with the §76.5 SELECT allowlist and
    handed here only to be digest-sealed next to the other bundle rows.  This
    loader is a gate, not a transformer: exact key set per row, no body/title
    anywhere in the document, unique ids, deterministic order.
    """
    doc = read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("notes"), list):
        raise NotesInputError("notes input must be an object with a 'notes' list")
    if doc.get("schema", NOTES_SCHEMA) != NOTES_SCHEMA:
        raise NotesInputError(f"unsupported notes schema: {doc.get('schema')}")
    forbidden = _forbidden_keys(doc)
    if forbidden:
        raise NotesInputError("body-bearing keys are not allowed: " + ",".join(sorted(set(forbidden))))
    rows = []
    seen = set()
    for i, row in enumerate(doc["notes"]):
        if not isinstance(row, dict):
            raise NotesInputError(f"note #{i} is not an object")
        keys = set(row)
        if keys != NOTE_ALLOWED_KEYS:
            raise NotesInputError(f"note #{i} key set must be exactly {sorted(NOTE_ALLOWED_KEYS)}; got {sorted(keys)}")
        if not isinstance(row["id"], str) or not row["id"]:
            raise NotesInputError(f"note #{i} id must be a non-empty string")
        if row["id"] in seen:
            raise NotesInputError(f"duplicate note id: {row['id']}")
        seen.add(row["id"])
        for k in ("parent_id", "repo", "source_dir", "source_capability", "trashed_at"):
            if row[k] is not None and not isinstance(row[k], str):
                raise NotesInputError(f"note {row['id']} field {k} must be string or null")
        for k in ("page_no", "revision"):
            if row[k] is not None and not isinstance(row[k], int):
                raise NotesInputError(f"note {row['id']} field {k} must be int or null")
        rows.append({k: row[k] for k in sorted(NOTE_ALLOWED_KEYS)})
    rows.sort(key=lambda r: r["id"])
    meta = {k: doc[k] for k in ("exported_at", "source", "cairn_source_commit", "repository_hint") if k in doc}
    return rows, meta


class Bundle:
    def __init__(self, root: Path, bundle_dir: Path, cycles, w7_evidence: Path, w7c_run: Path,
                 retirement_run: Path, backup_tar: Path | None, census_runs, notes=None, notes_meta=None):
        self.notes = notes
        self.notes_meta = notes_meta or {}
        self.root = root
        self.dir = bundle_dir
        self.cycle_ids = cycles
        self.w7_evidence = w7_evidence
        self.w7c_run = w7c_run
        self.retirement_run = retirement_run
        self.backup_tar = backup_tar
        self.census_runs = census_runs
        self.identity = L.read_root_identity(root)
        self.index = adm.load_index(root)
        self.files = {}
        self.manifests = {}
        for cyc in cycles:
            record = P.read_cycle_record(root, cyc)
            if record is None or record.get("state") != "sealed":
                raise SystemExit(f"cycle not sealed: {cyc}")
            doc = read_json(P.cycle_dir(root, record["campaign_id"], cyc) / "manifest.json")
            self.manifests[cyc] = (record, doc)
        self.shared = self._shared_revisions()

    # -- helpers -------------------------------------------------------------
    def _shared_revisions(self):
        out = []
        base = self.root / "shared"
        for kind in sorted(p.name for p in base.iterdir() if p.is_dir()):
            for ref in sorted((base / kind).iterdir()):
                reference = read_json(ref / "reference.json") if (ref / "reference.json").is_file() else {}
                for rev in sorted((ref / "revisions").iterdir()):
                    meta = read_json(rev / "revision.json") if (rev / "revision.json").is_file() else {}
                    if not meta.get("files"):
                        # W7-adopted revision without revision.json: enumerate the immutable tree.
                        files = []
                        for cur, dirs, names in os.walk(rev):
                            dirs.sort()
                            for n in sorted(names):
                                fp = Path(cur) / n
                                if fp.is_symlink() or not fp.is_file() or fp.name == "revision.json":
                                    continue
                                files.append({"path": fp.relative_to(rev).as_posix(), "sha256": sha_file(fp), "byte_size": fp.stat().st_size})
                        meta = {**meta, "files": files, "file_count": len(files), "byte_size": sum(f["byte_size"] for f in files),
                                "content_digest": meta.get("content_digest") or sha_text("\n".join(f"{f['path']}\t{f['sha256']}" for f in files)),
                                "enumerated": True}
                    out.append({"kind": kind, "shared_reference_id": ref.name, "shared_reference_revision_id": rev.name,
                                "latest": reference.get("latest_revision_id") == rev.name,
                                "content_digest": meta.get("content_digest"), "file_count": meta.get("file_count"),
                                "byte_size": meta.get("byte_size"), "enumerated": meta.get("enumerated", False),
                                "path": os.path.relpath(rev, self.root),
                                "files": meta.get("files", [])})
        return out

    def write(self, name: str, payload, jsonl=False):
        path = self.dir / name
        if jsonl:
            text = "".join(canonical(row) + "\n" for row in payload)
        else:
            text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
        self.files[name] = {"sha256": sha_text(text), "bytes": len(text.encode("utf-8")),
                            "rows": (len(payload) if jsonl else None)}
        return path

    # -- rows ----------------------------------------------------------------
    def stable_population(self):
        rows = []
        root_id = self.identity.artifact_root_id
        for cyc, (record, doc) in self.manifests.items():
            by_artifact = {a["artifact_id"]: a for a in doc["artifacts"]}
            for rev in doc["artifact_revisions"]:
                art = by_artifact[rev["artifact_id"]]
                locator = rev["locator"]["path"]
                cycle_rel = os.path.relpath(P.cycle_dir(self.root, record["campaign_id"], cyc), self.root)
                rows.append({"artifact_root_id": root_id, "repository_id": self.identity.repository_id,
                             "campaign_id": record["campaign_id"], "cycle_id": cyc,
                             "artifact_id": rev["artifact_id"], "artifact_revision_id": rev["artifact_revision_id"],
                             "revision_sequence": rev.get("revision_sequence"), "content_digest": rev["content_digest"],
                             "byte_size": rev["byte_size"], "media_type": rev.get("media_type"),
                             "locator": f"{cycle_rel}/{locator}", "bucket": locator.split("/")[1] if locator.count("/") else None,
                             "type": art.get("type"), "capability": art.get("capability"), "role": art.get("role"),
                             "disposition": "C-INT" if "/_internal/" in locator or locator.split("/")[2:3] == ["_internal"] else "C-DUR",
                             "identity_class": "manifest"})
            for row in (record.get("adopted") or {}).get("hidden_excluded", []):
                rows.append({"artifact_root_id": root_id, "repository_id": self.identity.repository_id,
                             "campaign_id": record["campaign_id"], "cycle_id": cyc, "artifact_id": None,
                             "artifact_revision_id": None, "content_digest": "sha256:" + row["sha256"], "byte_size": row["byte_size"],
                             "locator": f"{os.path.relpath(P.cycle_dir(self.root, record['campaign_id'], cyc), self.root)}/{row['path']}",
                             "disposition": "C-LEG(runtime-residue)" if row["reason"] == "hidden-component" else "C-LEG(unmanifestable)",
                             "identity_class": "excluded", "reason": row["reason"]})
        for rev in self.shared:
            for f in rev["files"]:
                rows.append({"artifact_root_id": root_id, "repository_id": self.identity.repository_id,
                             "shared_reference_id": rev["shared_reference_id"], "shared_reference_revision_id": rev["shared_reference_revision_id"],
                             "kind": rev["kind"], "latest": rev["latest"], "content_digest": f["sha256"], "byte_size": f["byte_size"],
                             "locator": f"{rev['path']}/{f['path']}", "disposition": "C-INT" if "/_internal/" in "/" + f["path"] else "C-DUR",
                             "identity_class": "shared-revision"})
        rows.sort(key=lambda r: r["locator"])
        return rows

    def relocation_handoff(self, population):
        w7_map = self.w7_evidence / "compatibility-map.jsonl"
        w7c_map = self.w7c_run / "compatibility-map.jsonl"
        w7_rows = read_jsonl(w7_map)
        w7c_rows = read_jsonl(w7c_map)
        by_locator = {r["locator"]: r for r in population}
        unresolved = []
        for r in w7_rows + w7c_rows:
            t = r.get("target_locator")
            if r.get("kind") == "file" and t not in by_locator:
                unresolved.append(t)
        retire_journal = self.retirement_run / "journal.jsonl"
        retired = read_jsonl(retire_journal) if retire_journal.is_file() else []
        tombstones = [{"tombstone_kind": "source-removed", "source_locator": r.get("path") or r.get("source_locator"),
                       "sha256": r.get("sha256"), "target_locator": r.get("target") or r.get("target_locator")}
                      for r in retired if str(r.get("action", "retire")).startswith("retire")]
        excluded = [{"locator": r["locator"], "reason": r["reason"], "content_digest": r["content_digest"]}
                    for r in population if r["identity_class"] == "excluded"]
        backup_seal = read_json(self.w7_evidence / "backup-seal.json")
        return {
            "schema": SCHEMA, "row": "relocation handoff",
            "maps": [
                {"name": "w7-e2e3", "path": os.path.relpath(w7_map, self.root), "sha256": sha_file(w7_map), "rows": len(w7_rows)},
                {"name": "w7c-delta", "path": os.path.relpath(w7c_map, self.root), "sha256": sha_file(w7c_map), "rows": len(w7c_rows)},
            ],
            "applied_journal": {"path": os.path.relpath(self.w7_evidence / "applied-journal.jsonl", self.root),
                                "sha256": sha_file(self.w7_evidence / "applied-journal.jsonl")},
            "inverse_journal": {"path": os.path.relpath(self.w7_evidence / "applied-inverse.jsonl", self.root),
                                "sha256": sha_file(self.w7_evidence / "applied-inverse.jsonl")},
            "stable_id_invariance": {"claim": "W7 apply-receipt stable_id_bytes_unchanged", "receipt": read_json(self.w7_evidence / "apply-receipt.json")},
            "tombstones": {"count": len(tombstones), "source": os.path.relpath(retire_journal, self.root),
                           "sha256": sha_file(retire_journal) if retire_journal.is_file() else None, "sample": tombstones[:5]},
            "exceptions": {"unmanifestable_targets": excluded, "map_targets_without_manifest_identity": unresolved[:50],
                           "map_targets_without_manifest_identity_count": len(unresolved)},
            "rollback": {
                "w7_backup": {"kind": backup_seal.get("schema_version"), "object_set_sha256": backup_seal.get("object_set_sha256"),
                              "manifest_sha256": backup_seal.get("manifest_sha256"), "unique_objects": backup_seal.get("unique_object_count")},
                "w7c_retirement_backup": {"path": str(self.backup_tar) if self.backup_tar else None,
                                          "sha256": sha_file(self.backup_tar) if self.backup_tar and self.backup_tar.is_file() else None},
            },
        }

    def integrity_census(self):
        receipt = read_json(self.w7_evidence / "apply-receipt.json")
        cycles = {}
        for cyc, (record, doc) in self.manifests.items():
            total = sum(r["byte_size"] for r in doc["artifact_revisions"])
            cycles[cyc] = {"manifest_digest": record["manifest_digest"], "artifact_revisions": len(doc["artifact_revisions"]),
                           "manifest_bytes": total, "tree_after_seal": (record.get("adopted") or {}).get("tree_before"),
                           "hidden_excluded": len((record.get("adopted") or {}).get("hidden_excluded", []))}
        verify = adm.verify_index(self.root)
        return {"schema": SCHEMA, "row": "integrity census",
                "before": {"w7_file_bytes_before": receipt["file_bytes_before"], "w7_file_bytes_after": receipt["file_bytes_after"],
                           "byte_loss": receipt["byte_loss"], "applied_rows": receipt["applied_row_count"]},
                "after": {"cycles": cycles, "shared_revisions": [{k: v for k, v in r.items() if k != "files"} for r in self.shared],
                          "index": {"ok": verify.ok, "violations": [v.code for v in verify.violations], "warnings": [v.code for v in verify.warnings],
                                    "stable_ids": len(self.index.stable_ids), "manifests": len(self.index.manifests)},
                          "delta_census_runs": self.census_runs},
                "note": "digests verify bytes; identity is the artifact_id/artifact_revision_id in stable-population.jsonl"}

    def legacy_mapping(self, population):
        by_locator = {r["locator"]: r for r in population}
        rows = []
        conflicts = []
        seen = defaultdict(list)
        for name, path in (("w7-e2e3", self.w7_evidence / "compatibility-map.jsonl"), ("w7c-delta", self.w7c_run / "compatibility-map.jsonl")):
            for r in read_jsonl(path):
                if r.get("kind") != "file":
                    continue
                target = by_locator.get(r["target_locator"])
                row = {"legacy_locator": r["source_locator"], "target_locator": r["target_locator"], "map": name,
                       "artifact_id": (target or {}).get("artifact_id"), "artifact_revision_id": (target or {}).get("artifact_revision_id"),
                       "shared_reference_revision_id": (target or {}).get("shared_reference_revision_id"),
                       "state": "resolved" if target and target["identity_class"] != "excluded" else ("excluded" if target else "unresolved")}
                rows.append(row)
                seen[r["source_locator"]].append(name)
        for src, maps in seen.items():
            if len(maps) > 1:
                conflicts.append({"legacy_locator": src, "maps": maps})
        rows.sort(key=lambda r: (r["legacy_locator"], r["map"]))
        return rows, conflicts

    def publication_evidence(self):
        receipts = []
        for path in self.root.rglob("*receipt*.json"):
            rel = os.path.relpath(path, self.root)
            if rel.startswith(("_scratch/", ".runtime/", "campaigns/")):
                continue
            receipts.append({"path": rel, "sha256": sha_file(path), "kind": (read_json(path) or {}).get("kind") or (read_json(path) or {}).get("schema_version")})
        return {"schema": SCHEMA, "row": "publication evidence",
                "report_bundle_receipts": [], "decoder": "utilities/artifact_receipt.py (D-12 v1/v2/v3 exact decoders)",
                "approval_receipts_found": receipts,
                "existing_note_ids": None,
                "existing_notes": ("notes.json" if self.notes is not None else None),
                "note": "no report-bundle publication receipt exists under this root; a W8 exact receipt↔artifact_id join must be produced by note-publication (artifact-sink.sh) v3 lineage, and existing note IDs must come from Cairn (not read here)"}

    def report_granularity(self, population):
        groups = defaultdict(list)
        for r in population:
            if r["identity_class"] != "manifest":
                continue
            parts = r["locator"].split("/")
            # campaigns/<camp>/cycles/<cyc>/artifacts/<bucket>/<entry>/...
            key = "/".join(parts[:7]) if len(parts) > 7 else "/".join(parts[:6])
            groups[key].append(r)
        proposal = []
        for key, rows in sorted(groups.items()):
            rows.sort(key=lambda r: r["locator"])
            primary = pick_primary(rows)
            proposal.append({"report_locator": key, "cycle_id": rows[0]["cycle_id"], "member_count": len(rows),
                             "primary_artifact_id": primary["artifact_id"],
                             "ordered_member_set_digest": sha_text("\n".join(r["artifact_revision_id"] for r in rows)),
                             "members": [r["artifact_id"] for r in rows]})
        return {"schema": SCHEMA, "row": "report granularity",
                "proposal": "logical report = (cycle_id, report_locator) with a closed ordered set of member artifact_ids; identity of the set is ordered_member_set_digest over artifact_revision_ids; the primary artifact is the entry summary",
                "reports": proposal}

    def candidate_scope(self, population):
        facets = Counter((r.get("capability"), r.get("bucket")) for r in population if r["identity_class"] == "manifest")
        return {"schema": SCHEMA, "row": "candidate scope",
                "repository_id": self.identity.repository_id, "artifact_root_id": self.identity.artifact_root_id,
                "cycles": list(self.manifests), "shared_references": sorted({(r["kind"], r["shared_reference_id"]) for r in self.shared}),
                "facets": [{"capability": c, "bucket": b, "count": n} for (c, b), n in sorted(facets.items(), key=lambda x: (str(x[0][0]), str(x[0][1])))],
                "locator_prefix_map": {"campaigns/<camp>/cycles/<cyc>/artifacts/<bucket>/": "cycle output",
                                       "shared/<kind>/<ref>/revisions/<rrev>/": "shared reference revision"},
                "note": "facets narrow candidates; they are never a match key"}

    def expected_totals(self, population, mapping, conflicts):
        return {"schema": SCHEMA, "row": "expected totals",
                "artifact_population": Counter(r["identity_class"] for r in population),
                "dispositions": Counter(r["disposition"] for r in population),
                "legacy_mapping": Counter(r["state"] for r in mapping), "mapping_conflicts": len(conflicts),
                "existing_note_candidates": self.existing_note_counts(),
                "unresolved_ship_gate": ["dev_logs/ undeclared top-level container (PRD §16.5, D-38 ship_eligible=false)",
                                         "index-size-warning (sharding review) advisory"],
                "note": ("existing_note_candidates counts the Cairn body-free export sealed as notes.json; disposition is Cairn W9's"
                         if self.notes is not None else
                         "existing_note_candidates requires Cairn read access, which this bundle did not have")}

    def existing_note_counts(self):
        if self.notes is None:
            return None
        active = [n for n in self.notes if n["trashed_at"] is None]
        return {"total": len(self.notes), "active": len(active), "trashed": len(self.notes) - len(active),
                "active_by_repo": dict(sorted(Counter(n["repo"] for n in active).items(), key=lambda kv: str(kv[0])))}

    def existing_notes(self):
        return {"schema": NOTES_SCHEMA, "row": "existing notes", "columns": sorted(NOTE_ALLOWED_KEYS),
                "body_free": True, **self.notes_meta, "counts": self.existing_note_counts(), "notes": self.notes}

    def approval_boundary(self, bundle_digest_seed):
        def aid(stage):
            return "apr_" + hashlib.sha256(f"{SCHEMA}\0{bundle_digest_seed}\0{stage}".encode()).hexdigest()[:32]
        return {"schema": SCHEMA, "row": "approval boundary",
                "stages": [
                    {"stage": "W9-dry-run", "approval_id": aid("W9-dry-run"), "authorized": False, "mutates": "nothing (candidate digest only)"},
                    {"stage": "W10-D20-destructive-apply", "approval_id": aid("W10-D20"), "authorized": False,
                     "rollback_ids": {"w7_inverse_journal_sha256": sha_file(self.w7_evidence / "applied-inverse.jsonl"),
                                      "w7c_retirement_backup": str(self.backup_tar) if self.backup_tar else None}},
                    {"stage": "W10-note-link-apply", "approval_id": aid("W10-note-link"), "authorized": False,
                     "invariant": "l2_notes INSERT/UPDATE/DELETE = 0; link rows only"},
                ],
                "note": "three separate approvals; none is granted by this bundle"}

    # -- run -----------------------------------------------------------------
    def build(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        population = self.stable_population()
        self.write("stable-population.jsonl", population, jsonl=True)
        self.write("relocation-handoff.json", self.relocation_handoff(population))
        self.write("integrity-census.json", self.integrity_census())
        mapping, conflicts = self.legacy_mapping(population)
        self.write("legacy-mapping.jsonl", mapping, jsonl=True)
        self.write("legacy-mapping-conflicts.json", {"schema": SCHEMA, "conflicts": conflicts,
                                                     "resolution_rule": "a legacy locator present in both maps resolves through the latest map (W7C delta) first — artifact_cutover.resolve_legacy; both targets are listed in legacy-mapping.jsonl",
                                                     "mapping_digest": self.files["legacy-mapping.jsonl"]["sha256"],
                                                     "provenance": ["w7-e2e3 compatibility-map.jsonl", "w7c-delta compatibility-map.jsonl"]})
        self.write("publication-evidence.json", self.publication_evidence())
        self.write("report-granularity.json", self.report_granularity(population))
        self.write("candidate-scope.json", self.candidate_scope(population))
        if self.notes is not None:
            self.write("notes.json", self.existing_notes())
        self.write("expected-totals.json", self.expected_totals(population, mapping, conflicts))
        seed = "|".join(f"{n}:{m['sha256']}" for n, m in sorted(self.files.items()))
        self.write("approval-boundary.json", self.approval_boundary(seed))
        index = {"schema": SCHEMA, "artifact_root_id": self.identity.artifact_root_id, "repository_id": self.identity.repository_id,
                 "projection_version": "artifact-cycle-manifest/v2", "cycles": list(self.manifests),
                 "shared_revisions": [r["shared_reference_revision_id"] for r in self.shared],
                 "rows": {"stable population": "stable-population.jsonl", "relocation handoff": "relocation-handoff.json",
                          "integrity census": "integrity-census.json", "legacy mapping": ["legacy-mapping.jsonl", "legacy-mapping-conflicts.json"],
                          "publication evidence": "publication-evidence.json", "report granularity": "report-granularity.json",
                          "candidate scope": "candidate-scope.json", "expected totals": "expected-totals.json",
                          "approval boundary": "approval-boundary.json",
                          **({"existing notes": "notes.json"} if self.notes is not None else {})},
                 "files": self.files}
        index["bundle_digest"] = sha_text(canonical(self.files))
        self.write("handoff.json", index)
        return index


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact-root", required=True)
    ap.add_argument("--bundle-dir", required=True, help="inside an open producer cycle's artifacts/")
    ap.add_argument("--cycle", action="append", required=True)
    ap.add_argument("--w7-evidence", required=True)
    ap.add_argument("--w7c-run", required=True)
    ap.add_argument("--retirement-run", required=True)
    ap.add_argument("--backup-tar")
    ap.add_argument("--census", help="delta-census json to cite")
    ap.add_argument("--notes", help="Cairn body-free note export (cairn-w8-notes/v1) to seal as notes.json")
    args = ap.parse_args(argv)
    notes = notes_meta = None
    if args.notes:
        try:
            notes, notes_meta = load_notes(Path(args.notes))
        except NotesInputError as exc:
            raise SystemExit(f"notes input rejected: {exc}")
    root = Path(args.artifact_root).resolve()
    bundle_dir = Path(args.bundle_dir).resolve()
    verdict = P.check_write(root, bundle_dir / "handoff.json")
    if verdict["verdict"] != "allow":
        raise SystemExit(f"bundle dir not writable under the producer contract: {verdict}")
    census = None
    if args.census:
        c = read_json(Path(args.census))
        census = {"path": args.census, "sha256": sha_file(Path(args.census)), "runs": c.get("runs"), "stable": c.get("stable_across_runs"),
                  "unclassified_total": c.get("unclassified_total")}
    b = Bundle(root, bundle_dir, args.cycle, Path(args.w7_evidence), Path(args.w7c_run), Path(args.retirement_run),
               Path(args.backup_tar).expanduser() if args.backup_tar else None, census, notes, notes_meta)
    index = b.build()
    print(json.dumps({"bundle_dir": str(bundle_dir), "bundle_digest": index["bundle_digest"],
                      "files": {k: v["sha256"] for k, v in index["files"].items()}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
