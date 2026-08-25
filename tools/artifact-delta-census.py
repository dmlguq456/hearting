#!/usr/bin/env python3
"""W7C read-only delta census of the live artifact root against the W7 journal.

Walks the canonical artifact root without following symlinks and classifies
every entry (file, directory, symlink) into exactly one disposition using the
sealed W7 evidence as read-only input:

  * `w6-relocation-baseline.jsonl` (pre-cutoff population, sha 93706553...),
  * W7 E2/E3 `applied-journal.jsonl` (5,631 additive relocation rows),
  * W7 `compatibility-map.jsonl` (stable ids), and
  * W7 `delta-d.json` (831 after-cutoff rows).

The census never writes under the artifact root except the report file the
caller names with `--output`; it never modifies, re-runs, or re-seals any W7
evidence.  `research/` is classified as a cycle candidate, never as shared.
Exit 0 only when `unclassified == 0`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

RUNTIME_TOPS = {".runtime", ".core-grounding", ".spec-grounding", ".route-grounding", ".pipeline-lock"}
RESIDUE_TOPS = {".git", ".agents", ".codex", ".claude"}
LEGACY_BUCKETS = {
    "analysis_project": "analysis", "research": "research", "spec": "spec", "plans": "plans",
    "documents": "documents", "experiments": "experiments", "designs": "designs",
}
LEGACY_INTERNAL = {"_internal", "reviews", "shards"}
LEGACY_ROUTES = {"routes", "_routes", ".routes"}
UNDECLARED_CONTAINERS = {"notes", "proposals", "spec-research-alternative", "research-alternative",
                         "release-config", "evidence", "dev_logs", "test_logs", "user_profile"}
SHARED_KINDS = {"spec", "analysis", "research"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path: Path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def walk(root: Path):
    """Yield (rel, kind) for every entry; never follows symlinks; `_scratch` excluded."""
    for current, dirs, files in os.walk(str(root), followlinks=False):
        rel_dir = os.path.relpath(current, str(root))
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir == "" and "_scratch" in dirs:
            dirs.remove("_scratch")
        dirs.sort()
        files.sort()
        for name in list(dirs):
            full = os.path.join(current, name)
            rel = os.path.join(rel_dir, name) if rel_dir else name
            if os.path.islink(full):
                yield rel, "symlink"
                dirs.remove(name)
            else:
                yield rel, "directory"
        for name in files:
            full = os.path.join(current, name)
            rel = os.path.join(rel_dir, name) if rel_dir else name
            yield rel, "symlink" if os.path.islink(full) else "file"


def under(rel: str, prefixes) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in prefixes)


def classify(rel: str, kind: str, ev: dict) -> tuple[str, str]:
    """Return (disposition, detail). Rule order is the authority."""
    top = rel.split("/", 1)[0]
    if top in RUNTIME_TOPS or top.startswith(".probe-") or top == "__pycache__":
        return "runtime-state", top
    if top in RESIDUE_TOPS:
        return "runtime-residue", top
    if kind == "symlink":
        return "symlink-row", top
    if top == "campaigns":
        if rel in ev["journal_target_ancestors"]:
            return "w7-relocated-target-ancestor", "campaigns"
        if rel in ev["journal_targets"]:
            return "w7-relocated-target", "campaigns"
        if under(rel, ev["journal_target_dirs"]):
            return "w7-relocated-target-descendant", "campaigns"
        return "post-w7-cycle-output", "campaigns"
    if top == "shared":
        parts = rel.split("/")
        kind_name = parts[1] if len(parts) > 1 else ""
        if rel in ev["journal_target_ancestors"]:
            return "w7-relocated-target-ancestor", f"shared/{kind_name}" if kind_name else "shared"
        if rel in ev["journal_targets"]:
            return "w7-relocated-target", f"shared/{kind_name}"
        if under(rel, ev["journal_target_dirs"]):
            return "w7-relocated-target-descendant", f"shared/{kind_name}"
        if kind_name in SHARED_KINDS:
            return "post-w7-shared-revision", f"shared/{kind_name}"
        return "unclassified", f"shared/{kind_name}"
    # legacy top-level population --------------------------------------------
    if rel in ev["delta_d"]:
        row = ev["delta_d"][rel]
        if top in LEGACY_BUCKETS:
            return f"after-cutoff-{row['classification']}", f"cycle-candidate:{LEGACY_BUCKETS[top]}"
        if top in LEGACY_INTERNAL or top in UNDECLARED_CONTAINERS or top in LEGACY_ROUTES:
            return f"after-cutoff-{row['classification']}", f"legacy-container:{top}"
        return f"after-cutoff-{row['classification']}", f"root-level:{top}"
    if rel in ev["journal_sources"]:
        return "w7-source-preserved", f"cycle-candidate:{LEGACY_BUCKETS.get(top, top)}"
    if under(rel, ev["self_write_scope"]):
        return "w7-self-write-transaction", "plans"
    if under(rel, ev["w7c_scope"]):
        return "w7c-self-write-transaction", "plans"
    if under(rel, ev["journal_source_dirs"]):
        return "w7-source-preserved-descendant", f"cycle-candidate:{LEGACY_BUCKETS.get(top, top)}"
    if rel in ev["baseline"]:
        if top in LEGACY_BUCKETS:
            return "w6-baseline-legacy", f"cycle-candidate:{LEGACY_BUCKETS[top]}"
        if top in LEGACY_INTERNAL:
            return "w6-baseline-legacy", f"legacy-internal:{top}"
        if top in LEGACY_ROUTES:
            return "w6-baseline-legacy", f"legacy-route-location:{top}"
        if top in UNDECLARED_CONTAINERS:
            return "w6-baseline-legacy", f"undeclared-container:{top}"
        return "w6-baseline-legacy", f"root-level:{top}"
    # arrivals after the W7 seal, not covered by any sealed evidence ---------
    if top in LEGACY_BUCKETS:
        return "post-w7-arrival", f"cycle-candidate:{LEGACY_BUCKETS[top]}"
    if top in LEGACY_INTERNAL:
        return "post-w7-arrival", f"legacy-internal:{top}"
    if top in LEGACY_ROUTES:
        return "post-w7-arrival", f"legacy-route-location:{top}"
    if top in UNDECLARED_CONTAINERS:
        return "post-w7-arrival", f"undeclared-container:{top}"
    if "/" not in rel:
        return "post-w7-arrival", f"root-level:{top}"
    return "unclassified", top


def load_evidence(root: Path, e2e3: Path, baseline: Path, manifest: Path, w7c_scope: str) -> dict:
    journal = load_jsonl(e2e3 / "applied-journal.jsonl")
    compat = load_jsonl(e2e3 / "compatibility-map.jsonl")
    delta = json.loads((e2e3 / "delta-d.json").read_text(encoding="utf-8"))
    base_rows = load_jsonl(baseline)
    baseline_paths = set()
    for row in base_rows:
        path = row.get("path")
        if isinstance(path, str):
            baseline_paths.add(path)
        elif isinstance(row.get("source_locator"), dict):
            baseline_paths.add(row["source_locator"].get("root_relative_path", ""))
    targets = {r["target_locator"] for r in journal}
    # W7 recorded rows from the revision/cycle directory downwards; the fixed
    # ancestors (`campaigns`, `campaigns/<camp>`, `.../cycles`, `shared`,
    # `shared/<kind>`, `.../<ref>`, `.../revisions`) are created-by-W7 too.
    ancestors = set()
    for target in targets:
        parts = target.split("/")
        for depth in range(1, len(parts)):
            ancestors.add("/".join(parts[:depth]))
    ancestors -= targets
    target_dirs = {r["target_locator"] for r in journal if r.get("kind") == "directory"}
    sources = {r["source_locator"] for r in journal}
    source_dirs = {r["source_locator"] for r in journal if r.get("kind") == "directory"}
    return {
        "journal_rows": len(journal), "journal_targets": targets, "journal_target_dirs": target_dirs,
        "journal_target_ancestors": ancestors,
        "journal_sources": sources, "journal_source_dirs": source_dirs,
        "compat_rows": len(compat),
        "delta_d": {r["path"]: r for r in delta["rows"]},
        "delta_meta": {k: delta[k] for k in ("baseline_sha256", "delta_sha256", "manifest_sha256", "row_count")},
        "baseline": baseline_paths,
        "self_write_scope": [delta.get("self_write_scope", "")] if delta.get("self_write_scope") else [],
        "w7c_scope": [w7c_scope] if w7c_scope else [],
        "evidence_sha256": {
            "applied-journal.jsonl": sha256_file(e2e3 / "applied-journal.jsonl"),
            "compatibility-map.jsonl": sha256_file(e2e3 / "compatibility-map.jsonl"),
            "delta-d.json": sha256_file(e2e3 / "delta-d.json"),
            "w6-relocation-baseline.jsonl": sha256_file(baseline),
            "w6-relocation-manifest.jsonl": sha256_file(manifest),
        },
    }


def run(root: Path, ev: dict, run_id: int) -> dict:
    started = time.time()
    rows = []
    dispositions: Counter = Counter()
    details: Counter = Counter()
    research_rows = 0
    for rel, kind in walk(root):
        disposition, detail = classify(rel, kind, ev)
        dispositions[disposition] += 1
        details[f"{disposition}|{detail}"] += 1
        if rel.split("/", 1)[0] == "research":
            research_rows += 1
            assert "shared" not in detail, (rel, detail)
        rows.append({"path": rel, "kind": kind, "disposition": disposition, "detail": detail})
    unclassified = [r for r in rows if r["disposition"] == "unclassified"]
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "run": run_id, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "elapsed_seconds": round(time.time() - started, 2), "entries": len(rows),
        "dispositions": dict(sorted(dispositions.items())), "details": dict(sorted(details.items())),
        "unclassified_count": len(unclassified), "unclassified": unclassified[:200],
        "research_rows": research_rows, "research_shared_rows": 0, "rows_sha256": digest,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--w7-e2e3-evidence", required=True, help="dir with applied-journal.jsonl, compatibility-map.jsonl, delta-d.json")
    parser.add_argument("--w6-baseline", required=True)
    parser.add_argument("--w6-manifest", required=True)
    parser.add_argument("--w7c-scope", default="", help="root-relative W7C transaction self-write scope")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    root = Path(args.artifact_root).resolve()
    ev = load_evidence(root, Path(args.w7_e2e3_evidence), Path(args.w6_baseline), Path(args.w6_manifest), args.w7c_scope)
    expected = {"applied-journal.jsonl": 5631}
    if ev["journal_rows"] != expected["applied-journal.jsonl"]:
        print(json.dumps({"status": "blocked", "reason": "w7-journal-row-count-mismatch", "rows": ev["journal_rows"]}))
        return 65
    if ev["evidence_sha256"]["w6-relocation-baseline.jsonl"] != ev["delta_meta"]["baseline_sha256"] \
            or ev["evidence_sha256"]["w6-relocation-manifest.jsonl"] != ev["delta_meta"]["manifest_sha256"]:
        print(json.dumps({"status": "blocked", "reason": "w6-evidence-digest-mismatch"}))
        return 65
    runs = [run(root, ev, i + 1) for i in range(max(1, args.runs))]
    stable = len({r["rows_sha256"] for r in runs}) == 1
    report = {
        "schema_version": 1, "kind": "w7c-delta-census", "mode": "read-only",
        "artifact_root": str(root), "evidence": {k: v for k, v in ev.items() if k in ("journal_rows", "compat_rows", "delta_meta", "evidence_sha256")},
        "runs": runs, "stable_across_runs": stable,
        "unclassified_total": sum(r["unclassified_count"] for r in runs),
        "research_policy": "research/ rows are cycle candidates; shared/research is reachable only by explicit promotion",
        "ok": stable and all(r["unclassified_count"] == 0 for r in runs),
    }
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "stable": stable, "runs": [(r["entries"], r["unclassified_count"]) for r in runs],
                      "dispositions": runs[-1]["dispositions"]}, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
