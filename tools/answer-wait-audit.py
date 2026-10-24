#!/usr/bin/env python3
"""Read-only before/after answer-wait audit for a dispatch registry."""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))

import route_authority
from dispatch_registry_cache import registry_read_scope, registry_snapshot
from fleet.collectors import dispatch
from fleet import route as route_module
import human_gate_observation
from dispatch_owner_input import blocked_owner_answers


def _stat(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _epoch(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return fallback


def _binding(meta):
    return (meta.get("owner_route_id") or meta.get("route_id"),
            meta.get("owner_route_hash") or meta.get("route_hash"),
            meta.get("owner_route_file") or meta.get("route_file"))


def audit(path: Path):
    as_of = datetime.now(timezone.utc).timestamp()
    before_stat, before_hash = _stat(path), _hash(path)
    with registry_read_scope():
        snap = registry_snapshot(path)
    if not snap.complete:
        print(f"registry_complete=false reason={snap.reason}")
        return 2

    latest, first, successor_sources = {}, {}, set()
    all_ends = {"BLOCKED": 0, "FAIL": 0}
    for row in snap.view.records:
        meta = row.completion_metadata()
        attempt = meta.get("attempt_id")
        if attempt:
            first.setdefault(attempt, row.order)
            latest[attempt] = (row.status, meta, row.fields, row.order)
        successor_sources.update(meta[key] for key in
            ("automatic_retry_of", "replacement_original_attempt_id", "prior_attempt_id")
            if meta.get(key))
    for status, meta, _fields, _order in latest.values():
        if meta.get("worker_type") != "owner":
            continue
        kind = route_authority.answerable_owner_end(status, meta)
        if kind in all_ends:
            all_ends[kind] += 1

    # Reproduce the previous reader's S3 display: BLOCKED and FAIL owner ends,
    # successor-link suppression, verified route, open outcome, and no retained answer.
    old_displayed, old_unbound = {}, {}
    for attempt, (status, meta, fields, order) in latest.items():
        if meta.get("worker_type") != "owner" or attempt in successor_sources:
            continue
        kind = route_authority.answerable_owner_end(status, meta)
        if not kind:
            continue
        route_id, digest, route_file = _binding(meta)
        if not route_id or not digest or not route_file:
            old_unbound[attempt] = (kind, meta, fields, order)
            continue
        route_record = route_module.load(route_file, digest, route_id)
        if route_record is None:
            continue
        outcome = route_module.load_outcome(route_file, route_id, digest)
        if outcome and outcome.get("closed_at") and outcome.get("match") is True:
            continue
        if blocked_owner_answers(path, attempt):
            continue
        old_displayed[attempt] = (kind, meta, fields, order)

    after = human_gate_observation.read_answer_waits(jobs_paths=[path], now=as_of)
    current = {item.get("attempt_id"): item for item in after.get("items", ())
               if item.get("source") == "S3"}
    reasons = {"FAIL": 0, "later-same-route-owner": 0, ">7d-drop": 0,
               "72h-7d-fold": 0, "current": 0, "closure-changed-or-unexplained": 0}
    old_details = []
    for attempt, (kind, meta, fields, order) in old_displayed.items():
        if kind != "BLOCKED":
            reasons["FAIL"] += 1
            continue
        route_id, digest, route_file = _binding(meta)
        later = any(other != attempt and first.get(other, info[3]) > first.get(attempt, order)
                    and info[1].get("worker_type") == "owner"
                    and _binding(info[1]) == (route_id, digest, route_file)
                    for other, info in latest.items())
        if later:
            reasons["later-same-route-owner"] += 1
            continue
        finished = meta.get("ended_at") or fields[0]
        age = as_of - _epoch(finished, as_of)
        if age > 7 * 86400:
            reasons[">7d-drop"] += 1
        elif attempt in current and current[attempt].get("stale"):
            reasons["72h-7d-fold"] += 1
        elif attempt in current:
            reasons["current"] += 1
            old_details.append((attempt, current[attempt]))
        else:
            reasons["closure-changed-or-unexplained"] += 1

    after_stat, after_hash = _stat(path), _hash(path)
    print("Answer-wait S3 audit (read-only)")
    print(f"as_of_utc={datetime.fromtimestamp(as_of, timezone.utc).isoformat()}")
    print(f"registry={path} dev={before_stat[0]} ino={before_stat[1]} bytes={before_stat[2]} "
          f"mtime_ns={before_stat[3]} sha256={before_hash} rows={snap.view.line_count}")
    print("stage\tcount")
    print(f"previous-reader-displayed-S3\t{len(old_displayed)}")
    print(f"previous-reader-route-unproven-unknown\t{len(old_unbound)}")
    print(f"before-including-route-unproven-unknown\t{len(old_displayed) + len(old_unbound)}")
    print("reconciliation-old-displayed-S3")
    for reason in ("FAIL", "later-same-route-owner", ">7d-drop", "72h-7d-fold",
                   "current", "closure-changed-or-unexplained"):
        print(f"{reason}\t{reasons[reason]}")
    print(f"reconciled-displayed-total\t{sum(reasons.values())}")
    print(f"route-unproven-unknown-to-unbound\t{len(old_unbound)}")
    print(f"secondary-all-owner-end-census\tBLOCKED={all_ends['BLOCKED']}\tFAIL={all_ends['FAIL']}\t"
          f"total={sum(all_ends.values())}")
    print("remaining_current\tattempt\troute\tage_hours\towner\tblocker")
    for attempt, item in old_details:
        owner = item.get("owner") or {}
        age = max(0, (as_of - float(item.get("since") or as_of)) / 3600)
        blocker = " ".join(str(item.get("question") or "BLOCKED owner").split())[:160]
        print(f"S3\t{attempt[:16]}\t{item.get('route_id')}\t{age:.2f}\t"
              f"{owner.get('harness')}:{owner.get('session_id')}\t{blocker}")
    print(f"registry_after dev={after_stat[0]} ino={after_stat[1]} bytes={after_stat[2]} "
          f"mtime_ns={after_stat[3]} sha256={after_hash} unchanged={before_stat == after_stat and before_hash == after_hash}")
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", nargs="?", help="optional registry path (normally the real jobs.log)")
    args = parser.parse_args(argv)
    paths = dispatch.human_gate_registry_paths(args.jobs) if args.jobs else dispatch.human_gate_registry_paths()
    if not paths:
        print("registry unavailable")
        return 2
    return audit(Path(paths[0]))


if __name__ == "__main__":
    raise SystemExit(main())
