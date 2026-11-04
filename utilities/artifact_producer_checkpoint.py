"""Artifact producer: checkpoint runs.

`checkpoint` and the per-cycle checkpoint that writes an interim manifest and
then refreshes closed cycles.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import artifact_payload
import artifact_cycle_titles
import artifact_identity
import artifact_manifest

from artifact_producer_store import (
    _canonical, _digest, _ensure_dir, _json_bytes, _read_json, _rfc3339, _write_atomic,
    cycle_dir, journal_path, ProducerError, read_cycle_record,
)

from artifact_producer_routes import (
    load_route, route_cycle_for, route_is_closed,
)

from artifact_producer_history import (
    _observe_control_changes, deliver_pending_history,
)

from artifact_producer_interim import (
    _checkpoint_lock, _reservation_payload, _route_release_supports_interim,
    CHECKPOINT_EXCLUDED_SUFFIXES, checkpoint_interval_seconds, checkpoint_limits_from_config,
    CHECKPOINT_STALE_SECONDS, checkpoint_state_path, CHECKPOINT_TRIGGERS, CheckpointLimits,
    open_manifest_path, read_interim_reservation, remove_interim, reservation_path,
)

from artifact_producer_manifest import (
    build_manifest,
)

from artifact_producer_refresh import (
    refresh_cycle, refresh_sweep, RefreshBudget,
)


def _checkpoint_scan(directory: Path, previous: Mapping[str, Any], limits: CheckpointLimits,
                    *, excluded_paths: Sequence[str] = ()) -> Dict[str, Any]:
    """Bounded walk of `artifacts/`.  Returns `facts` (locator, digest, size),
    the refreshed digest cache, exclusion counts and the newest file mtime, or a
    `skip` reason as soon as a limit is exceeded (the walk stops there)."""
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        return {"skip": "artifacts-dir-missing"}
    excluded = {"hidden": 0, "binary": 0, "oversize": 0, "non-regular": 0,
                "invalid-locator": 0, "self-excluded": 0}
    excluded_roots: List[str] = []
    payload_roots = [("artifacts", artifacts)]
    internal = directory / "_internal"
    if internal.is_symlink():
        # A support-root link is reported without following its target.
        excluded["non-regular"] += 1
    elif internal.is_dir():
        payload_roots.append(("_internal", internal))
    candidates: List[Tuple[str, str, os.stat_result]] = []
    visited = total = 0
    newest = 0.0
    for payload_root, top in payload_roots:
        root_excluded, _root_snapshot = artifact_payload.observe_directory(top, payload_root=top)
        if root_excluded:
            excluded["self-excluded"] += 1
            excluded_roots.append(payload_root)
            continue
        for current, dirs, files in os.walk(str(top), followlinks=False):
            kept_dirs = []
            for name in sorted(dirs):
                if payload_root == "artifacts" and name == "raw-results":
                    if os.path.islink(os.path.join(current, name)):
                        excluded["non-regular"] += 1
                    continue  # §47 pruning applies only to artifacts raw-results
                if name.startswith("."):
                    excluded["hidden"] += 1
                elif os.path.islink(os.path.join(current, name)):
                    excluded["non-regular"] += 1
                else:
                    child = Path(current) / name
                    marker_excluded, _snapshot = artifact_payload.observe_directory(child, payload_root=top)
                    if marker_excluded:
                        excluded["self-excluded"] += 1
                        excluded_roots.append(child.relative_to(directory).as_posix())
                    else:
                        kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(files):
                path = os.path.join(current, name)
                rel = Path(path).relative_to(directory).as_posix()
                if rel in excluded_paths:
                    continue
                try:
                    info = os.lstat(path)
                except OSError:
                    excluded["non-regular"] += 1
                    continue
                if not stat.S_ISREG(info.st_mode):
                    excluded["non-regular"] += 1
                    continue
                newest = max(newest, info.st_mtime)
                if name.startswith("."):
                    excluded["hidden"] += 1
                    continue
                if os.path.splitext(name)[1].lower() in CHECKPOINT_EXCLUDED_SUFFIXES:
                    excluded["binary"] += 1
                    continue
                if info.st_size > limits.max_file_bytes:
                    excluded["oversize"] += 1
                    continue
                locator = artifact_manifest.validate_locator_path(rel)
                if not locator.ok:
                    excluded["invalid-locator"] += 1
                    reason = locator.violations[0].code
                    reasons = excluded.setdefault("invalid-locator-reasons", {})
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                visited += 1
                if visited > limits.max_walk_entries:
                    return {"skip": "walk-limit", "visited": visited}
                candidates.append((rel, path, info))
                total += info.st_size
                if len(candidates) > limits.max_files:
                    return {"skip": "file-count-limit", "files": len(candidates)}
                if total > limits.max_total_bytes:
                    return {"skip": "byte-size-limit", "bytes": total}
    facts: List[Tuple[str, str, int]] = []
    stats: Dict[str, List[Any]] = {}
    for rel, path, info in sorted(candidates):
        old = previous.get(rel)
        if (isinstance(old, list) and len(old) == 3 and old[0] == info.st_size
                and old[1] == info.st_mtime_ns and isinstance(old[2], str)):
            digest, size = old[2], info.st_size
        else:
            try:
                data = Path(path).read_bytes()
            except OSError:
                excluded["non-regular"] += 1
                continue
            digest, size = _digest(data), len(data)
        facts.append((rel, digest, size))
        stats[rel] = [size, info.st_mtime_ns, digest]
    return {"facts": facts, "stats": stats, "excluded": excluded,
            "excluded_roots": excluded_roots, "newest_mtime": newest,
            "total_bytes": sum(size for _rel, _digest_value, size in facts)}


def _checkpoint_target(root: Path, cycle_id: Optional[str], route_file: Optional[Path]) -> Optional[Dict[str, Any]]:
    if cycle_id:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        return record
    if route_file is None:
        raise ProducerError("checkpoint-target-required", "--cycle or --route")
    route = load_route(root, route_file)
    return route_cycle_for(root, route)


def checkpoint(
    root: Path,
    *,
    cycle_id: Optional[str] = None,
    route_file: Optional[Path] = None,
    trigger: str = "explicit",
    now: Optional[float] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    limits: Optional[CheckpointLimits] = None,
) -> Dict[str, Any]:
    """Look at the cycle a trigger names; an automatic trigger then looks at the rest of the root.

    An open cycle gets its interim manifest (`_checkpoint_cycle`); a closed one is observed
    for changed files (`refresh_cycle`, §45 D-124).  An automatic trigger (not `explicit`)
    then spends what is left of the same budget on the root's other closed cycles
    (`refresh_sweep`); that never changes the named cycle's result except for a `refresh` key
    naming what the sweep did."""
    if cycle_id:
        _observe_control_changes(Path(root).resolve(), cycle_id, now=now)
    budget = RefreshBudget.unlimited() if trigger == "explicit" else RefreshBudget()
    deliver_pending_history(root)  # §45 D-125: lines an earlier trigger could not hand over
    result = _checkpoint_cycle(root, cycle_id=cycle_id, route_file=route_file, trigger=trigger, now=now,
                               allocator=allocator, limits=limits, budget=budget)
    if trigger != "explicit":
        try:
            sweep = refresh_sweep(Path(root).resolve(), trigger=trigger, now=now, budget=budget,
                                  skip_cycle_id=result.get("cycle_id"), allocator=allocator)
        except Exception:  # noqa: BLE001 -- the sweep never changes what the trigger returns
            sweep = {}
        if sweep.get("visited"):
            result["refresh"] = {key: sweep[key] for key in ("visited", "refreshed", "skipped", "cursor")
                                 if key in sweep}
    return result


def _checkpoint_cycle(
    root: Path,
    *,
    cycle_id: Optional[str] = None,
    route_file: Optional[Path] = None,
    trigger: str = "explicit",
    now: Optional[float] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    limits: Optional[CheckpointLimits] = None,
    budget: Optional[RefreshBudget] = None,
) -> Dict[str, Any]:
    """Publish (or refresh) an open cycle's interim manifest.

    Every outcome that is not an error is a result: `emitted`, `unchanged`, or
    `skipped` with a `reason`.  Only a live cycle is published: the cycle is
    open with no sealed manifest, and its route is readable, not closed, and
    sealed to a release that keeps interim IDs at the seal.  Scans are
    rate-limited per cycle (`checkpoint_interval_seconds`) for every trigger,
    including an explicit one.  The scan runs unlocked; every write -- the
    reservation, the document and the bookkeeping -- happens under the cycle's
    checkpoint lock after the cycle is re-read.
    """
    root = Path(root).resolve()
    if trigger not in CHECKPOINT_TRIGGERS:
        raise ProducerError("checkpoint-trigger-invalid", trigger)
    clock = time.time() if now is None else float(now)
    try:
        record = _checkpoint_target(root, cycle_id, route_file)
    except ProducerError as exc:
        if exc.code == "route-hash-drift":
            return {"status": "skipped", "reason": "route-hash-drift", "trigger": trigger}
        raise
    if record is None:
        return {"status": "skipped", "reason": "no-open-cycle", "trigger": trigger}
    cid = record["cycle_id"]
    base = {"cycle_id": cid, "route_id": record.get("route_id"), "trigger": trigger}

    def skipped(reason: str, **extra: Any) -> Dict[str, Any]:
        return {"status": "skipped", "reason": reason, **base, **extra}

    if record.get("state") == "sealed":
        observed = refresh_cycle(root, cid, trigger=trigger, now=now, allocator=allocator, budget=budget)
        if observed["status"] == "skipped":
            return {"status": "skipped", "reason": observed["reason"], **base}
        return {"status": observed["status"], **base,
                **{key: observed[key] for key in ("changes", "complete", "cursor", "manifest_digest")
                   if key in observed}}
    if record.get("state") != "open":
        return skipped("cycle-not-open", cycle_state=record.get("state"))
    directory = cycle_dir(root, record["campaign_id"], cid, record)
    if (directory / "manifest.json").exists():
        return skipped("sealed-manifest-present")
    try:
        route = load_route(root, Path(record["route_file"]))
    except ProducerError as exc:
        return skipped("route-unreadable", detail=exc.code)
    if route["route_hash"] != record.get("route_hash"):
        return skipped("route-hash-drift")
    if route_is_closed(root, route):
        return skipped("route-closed")
    supported, release = _route_release_supports_interim(route)
    if not supported:
        return skipped("route-release-predates-interim", runtime_root=release)
    state_path = checkpoint_state_path(root, cid)
    state = _read_json(state_path) or {}
    observed_scan = state.get("last_scan_at")
    interval = checkpoint_interval_seconds()
    if (isinstance(observed_scan, (int, float)) and not isinstance(observed_scan, bool)
            and clock - observed_scan < interval):
        return skipped("min-interval", next_eligible_at=_rfc3339(observed_scan + interval))
    if limits is None:
        limits, _limits_source = checkpoint_limits_from_config()
    previous_stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    owned = _read_json(journal_path(root, cid)) or {}
    owned_paths = [row.get("locator") for row in owned.get("directory_bundles", [])
                   if isinstance(row, Mapping) and isinstance(row.get("locator"), str)]
    scan = _checkpoint_scan(directory, previous_stats, limits, excluded_paths=owned_paths)
    try:
        with _checkpoint_lock(root, cid, timeout=0.0):
            result = _checkpoint_commit(
                root, record, route, directory, scan, observed_scan=observed_scan, clock=clock,
                trigger=trigger, limits=limits, interval=interval, base=base,
                allocator=allocator or artifact_identity.IdAllocator(),
            )
    except ProducerError as exc:
        if exc.code == "checkpoint-lock-busy":
            return skipped("busy")
        raise
    if result["status"] in {"emitted", "unchanged"}:
        artifact_cycle_titles.emit_after_checkpoint(root, record)
    return result


def _checkpoint_commit(
    root: Path, record: Mapping[str, Any], route: Mapping[str, Any], directory: Path,
    scan: Mapping[str, Any], *, observed_scan: Any, clock: float, trigger: str,
    limits: CheckpointLimits, interval: float, base: Mapping[str, Any],
    allocator: artifact_identity.IdAllocator,
) -> Dict[str, Any]:
    cid = record["cycle_id"]
    fresh = read_cycle_record(root, cid)
    if fresh is None:
        return {"status": "skipped", "reason": "cycle-record-unreadable", **base}
    if fresh.get("state") != "open":
        # Sealed or dropped while this checkpoint scanned: whatever interim
        # files remain (the lock this call just re-created, at least) are garbage.
        remove_interim(root, cid)
        return {"status": "skipped", "reason": "cycle-not-open", **base}
    if (directory / "manifest.json").exists():
        # Still open with a manifest present: a seal in flight or a torn one.
        # The reservation stays -- a re-run finalize must find it.
        return {"status": "skipped", "reason": "sealed-manifest-present", **base}
    state_path = checkpoint_state_path(root, cid)
    state = _read_json(state_path) or {}
    if state.get("last_scan_at") != observed_scan:
        # Another checkpoint scanned and settled while this one scanned; its
        # result is newer than nothing and this scan may predate its files.
        return {"status": "skipped", "reason": "superseded", **base}
    interim_path = open_manifest_path(root, cid)
    had_interim = interim_path.exists()
    previous_stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    bookkeeping: Dict[str, Any] = {
        "schema_version": 1, "cycle_id": cid, "campaign_id": fresh["campaign_id"],
        "route_id": fresh.get("route_id"), "cycle_path": os.path.relpath(str(directory), str(root)),
        "open_manifest": os.path.relpath(str(interim_path), str(root)),
        "last_scan_at": state.get("last_scan_at"), "last_scan_on": state.get("last_scan_on"),
        "last_trigger": trigger, "last_emitted_at": state.get("last_emitted_at"),
        "output_digest": state.get("output_digest"), "limits": limits.to_payload(),
        "min_interval_seconds": interval, "stats": previous_stats,
    }

    def settle(status: str, reason: Optional[str], *, scanned: bool = True, **extra: Any) -> Dict[str, Any]:
        if scanned:
            bookkeeping.update({"last_scan_at": clock, "last_scan_on": _rfc3339(clock)})
        bookkeeping.update({"last_status": status, "last_reason": reason})
        bookkeeping.update({key: value for key, value in extra.items() if key in {
            "stats", "excluded", "artifact_count", "total_bytes", "output_digest",
            "last_emitted_at", "manifest_id", "manifest_revision_id", "excluded_roots"}})
        _ensure_dir(state_path.parent)
        _write_atomic(state_path, _json_bytes(bookkeeping), 0o644)
        result = {"status": status, **base, **{k: v for k, v in extra.items() if k != "stats"}}
        if reason:
            result["reason"] = reason
        return result

    if scan.get("skip"):
        detail = {key: value for key, value in scan.items() if key != "skip"}
        return settle("skipped", scan["skip"], limit_detail=detail)
    facts = scan["facts"]
    common = {"stats": scan["stats"], "excluded": scan["excluded"],
              "excluded_roots": scan.get("excluded_roots", []),
              "artifact_count": len(facts), "total_bytes": scan["total_bytes"]}
    if not had_interim and not facts:
        return settle("skipped", "no-output", scanned=False, **common)
    if (trigger != "explicit" and not had_interim and scan["newest_mtime"]
            and clock - scan["newest_mtime"] > CHECKPOINT_STALE_SECONDS):
        return settle("skipped", "stale-cycle", scanned=False,
                      newest_file_on=_rfc3339(scan["newest_mtime"]), **common)
    reserved, reservation = read_interim_reservation(root, fresh)
    if reservation in ("unreadable", "identity-mismatch"):
        # Never re-issue IDs over a reservation this code cannot read.
        return settle("skipped", f"reservation-{reservation}", **common)
    output_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    reserved_matches = reserved is not None and all(
        rel in reserved.artifacts and reserved.artifacts[rel].content_digest == digest
        for rel, digest, _size in facts)
    if (had_interim and reservation == "present" and reserved_matches
            and state.get("output_digest") == output_digest):
        return settle("unchanged", None, path=str(interim_path), **common)
    try:
        document = build_manifest(
            root, fresh, route, (), state="completed", primary=None, allow_open_route=True,
            allocator=allocator, now=clock, reserved=reserved, interim=True, facts=facts,
        )
    except ProducerError as exc:
        return settle("skipped", exc.code, detail=exc.detail, **common)
    report = artifact_manifest.validate_interim(document)
    if not report.ok:
        return settle("skipped", "interim-invalid",
                      detail=";".join(v.code for v in report.violations), **common)
    # The reservation is written before the document: a crash in between leaves
    # IDs reserved but unpublished, never published but unreserved.
    ledger = reservation_path(root, cid)
    _ensure_dir(ledger.parent)
    _write_atomic(ledger, _json_bytes(_reservation_payload(fresh, document, reserved)), 0o644)
    _ensure_dir(interim_path.parent)
    _write_atomic(interim_path, artifact_manifest.canonical_bytes(document), 0o644)
    return settle(
        "emitted", None, path=str(interim_path), output_digest=output_digest,
        last_emitted_at=clock, manifest_id=document["manifest_id"],
        manifest_revision_id=document["manifest_revision_id"],
        reservation=reservation, **common,
    )
