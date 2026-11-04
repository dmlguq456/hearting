"""Artifact producer: review-output leases.

Acquiring, releasing and observing the review lease that governs a review
output, and the recovery and publication fences built on it.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import artifact_admission
from dispatch_contract import (
    _PROCESS_IDENTITY_METADATA_KEYS, REVIEW_GOVERNED_LEASE_KIND,
    REVIEW_GOVERNED_LEASE_NONCE_RE, encode_review_output_locator,
    review_governed_lease_is_held, review_lease_record_digest, process_start_ticks,
    review_output_binding_digest, validate_review_output_binding, review_holder_disposition,
)
from dispatch_lifecycle import (
    FiniteWatchdogBudget, begin_finite_watchdog, remaining_watchdog_seconds,
)

from artifact_producer_store import (
    _json_bytes, _read_json, _rfc3339, _rfc3339_precise, _rfc3339_to_epoch, _write_atomic,
    cycle_dir, journal_path, PRODUCER_REL, ProducerError, read_cycle_record, REVIEW_LEASE_REL,
)

from artifact_producer_routes import (
    load_route,
)


def _review_lease_dir(root: Path, cycle_id: str) -> Path:
    return Path(root) / PRODUCER_REL / REVIEW_LEASE_REL / cycle_id


def _review_lease_path(root: Path, cycle_id: str, attempt_id: str) -> Path:
    return _review_lease_dir(root, cycle_id) / f"{attempt_id}.json"


def _lease_record_state(
    record: Optional[Dict[str, Any]], *, root: Optional[Path] = None,
    now: Optional[float] = None,
) -> str:
    """SD-105/SD-90 evidence hierarchy, cited not redefined (plan.md §6.2), in three states.

    `live`: exact PID/start/PGID identity (or the v2 holder disposition) says the holder is running.
    `ended`: the holder was observed gone (released, dead exact holder, deadline passed for v1,
    process or start mismatch). `unobservable`: judgment-impossible inputs (corrupt record,
    unreadable /proc, clock anomaly, missing field, a holder that cannot be seen before its
    deadline). Writers treat `unobservable` like `live` (E47-4); only the report differs.
    """

    if record is None:
        return "ended"
    if record.get("released_at") is not None:
        return "ended"
    deadline = record.get("deadline")
    if not isinstance(deadline, str):
        return "unobservable"
    try:
        deadline_ts = _rfc3339_to_epoch(deadline)
    except (ValueError, OverflowError):
        return "unobservable"
    when = time.time() if now is None else now
    if _is_v2_review_lease(record):
        if record.get("expired") is not False:
            return "unobservable"
        acquired_at = record.get("acquired_at")
        try:
            acquired_ts = (
                _rfc3339_to_epoch(acquired_at)
                if isinstance(acquired_at, str) else None
            )
        except (ValueError, OverflowError):
            return "unobservable"
        if root is None or acquired_ts is None or acquired_ts > when or deadline_ts <= acquired_ts:
            return "unobservable"
        metadata = dict(record)
        metadata["review_cycle_id"] = record.get("cycle_id", "")
        # The record stores the sealed fields while the jobs row mirrors the
        # digest.  Reconstruct that mirror for the closed disposition so a
        # dead exact holder can unblock abandon instead of being treated as
        # malformed forever.
        metadata["review_lease_record_digest"] = review_lease_record_digest(record)
        disposition = review_holder_disposition(record, metadata, root, now=when)
        if disposition.state == "live":
            return "live"
        if disposition.state == "malformed":
            return "unobservable"
        if disposition.state == "dead":
            return "ended"
        # Valid but unobservable holders remain conservative until the finite
        # stale ceiling, after which time permits recovery but never grants a
        # write.
        return "unobservable" if when <= deadline_ts else "ended"
    if when > deadline_ts:
        return "ended"
    pid = record.get("pid")
    pid_start = record.get("pid_start")
    pgid = record.get("pgid")
    if not isinstance(pid, int) or not isinstance(pid_start, str) or not pid_start or not isinstance(pgid, int):
        return "unobservable"
    actual_start = process_start_ticks(pid)
    if actual_start is None:
        return "ended"
    if actual_start != pid_start:
        return "ended"
    try:
        actual_pgid = os.getpgid(pid)
    except OSError:
        return "ended"
    return "live" if actual_pgid == pgid else "ended"


def _lease_record_is_live(
    record: Optional[Dict[str, Any]], *, root: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """The write guard: anything not observed ended blocks (finish, abandon, seal)."""

    return _lease_record_state(record, root=root, now=now) != "ended"


def _is_v2_review_lease(record: object) -> bool:
    """Identify the exact-output lease without changing the v1 union seam."""

    return isinstance(record, Mapping) and record.get("schema_version") == 2


def _live_review_lease(
    root: Path, cycle_id: str, *, now: Optional[float] = None
) -> Optional[Path]:
    lease_dir = _review_lease_dir(root, cycle_id)
    if not lease_dir.is_dir():
        return None
    conservative: Optional[Path] = None
    for path in sorted(lease_dir.glob("*.json")):
        record = _read_json(path)
        if record is None:
            # The glob already proved this file exists, so an unparseable
            # read here is corruption, not absence -- conservative live
            # (E47-4), unlike `_lease_record_is_live(None)` below which
            # means "no lease file at this specific path".
            conservative = conservative or path
            continue
        if _lease_record_is_live(record, root=root, now=now):
            # Completed sealing cares about a live exact-report lease wherever
            # it appears in a mixed v1/v2 directory. Preserve the first
            # conservative v1/corrupt candidate for abandon semantics, but let
            # any live v2 report lease win this single union seam.
            if _is_v2_review_lease(record):
                return path
            conservative = conservative or path
    return conservative


def _review_lease_observation(
    root: Path, cycle_id: str, *, now: Optional[float] = None
) -> str:
    """What the cycle's review leases say, for reporting only: `live`, `unobservable` or `ended`.

    A lease whose holder is observed running is `live`; one that cannot be judged is
    `unobservable` and is not called a live writer. `_live_review_lease` is still the write guard.
    """
    lease_dir = _review_lease_dir(root, cycle_id)
    if not lease_dir.is_dir():
        return "ended"
    seen = "ended"
    for path in sorted(lease_dir.glob("*.json")):
        state = _lease_record_state(_read_json(path), root=root, now=now) if _read_json(path) is not None \
            else "unobservable"
        if state == "live":
            return "live"
        if state == "unobservable":
            seen = "unobservable"
    return seen


def _raise_if_recovery_fenced(root: Path, cycle_id: str, *, now: Optional[float] = None) -> None:
    """Keep recovery from sealing a cycle under a live v2 review lease.

    Recovery is itself a publication path: both a journal roll-forward and
    discovery of an already-published manifest call ``_commit_sealed``.  The
    normal finalize fence therefore has to be repeated immediately before
    that mutation.  Legacy v1 leases remain governed by their existing
    abandon-only policy; only the exact v2 report lease blocks recovery.
    """
    lease_path = _live_review_lease(root, cycle_id, now=now)
    if lease_path is None:
        return
    lease = _read_json(lease_path)
    if _is_v2_review_lease(lease):
        raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)


def _path_entry_present(path: Path) -> bool:
    """Return false only for an absent directory entry.

    Publication admission is fail-closed.  A dangling symlink, directory,
    special node, unreadable regular file, or lookup error is still an entry;
    none may be collapsed into the same state as ENOENT by ``exists`` or
    ``is_file``.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _raise_if_review_publication_started(
    root: Path, record: Mapping[str, Any]
) -> None:
    """Refuse a new v2 lease once either publication commit path has begun."""

    cycle_id = str(record.get("cycle_id", ""))
    directory = cycle_dir(
        root, str(record.get("campaign_id", "")), cycle_id, record,
    )
    for entry in (journal_path(root, cycle_id), directory / "manifest.json"):
        if _path_entry_present(entry):
            raise ProducerError(
                "review-lease-admission-after-publication",
                f"{cycle_id}: {entry}",
            )


def prepare_review_output_binding(
    root: Path, *, cycle_id: str, producer_id: str, attempt_id: str,
    review_output: str | Path, capability: str, unit: str,
    worktree: str | Path,
) -> Dict[str, Any]:
    """Validate the immutable cycle/route side before registry mutation."""

    root_input = Path(root)
    canonical_root = root_input.resolve(strict=False)
    worktree_input = Path(worktree)
    canonical_worktree = worktree_input.resolve(strict=False)
    if (
        not root_input.is_absolute() or str(root_input) != str(canonical_root)
        or not worktree_input.is_absolute()
        or str(worktree_input) != str(canonical_worktree)
    ):
        raise ProducerError("review-binding-root-not-canonical", str(root))
    record = read_cycle_record(canonical_root, cycle_id)
    if record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    if record.get("producer_id") != producer_id:
        raise ProducerError("review-binding-producer-mismatch", producer_id)
    # Fast preclaim refusal.  review_lease_acquire repeats this check while it
    # owns the canonical admission lock, closing publication after prepare.
    # Only a publication still in flight (cycle not yet closed) is refused: a
    # finished close does not stop a later review (§45 D-123).
    if record.get("state") == "open":
        _raise_if_review_publication_started(canonical_root, record)
    route = load_route(canonical_root, Path(str(record.get("route_file", ""))))
    expected_capability = str(record.get("capability", ""))
    if (
        capability != expected_capability
        or route.get("capability") != expected_capability
        or route.get("route_id") != record.get("route_id")
        or route.get("route_hash") != record.get("route_hash")
    ):
        raise ProducerError("review-binding-capability-mismatch", capability)
    if unit != "qa/code-review":
        raise ProducerError("review-binding-unit-mismatch", unit)
    if Path(str(route.get("cwd", ""))).resolve(strict=False) != canonical_worktree:
        raise ProducerError("review-binding-worktree-mismatch", str(worktree))
    if Path(str(route.get("artifact_root", ""))).resolve(strict=False) != canonical_root:
        raise ProducerError("review-binding-artifact-root-mismatch", str(root))
    output_input = Path(review_output)
    output = output_input.resolve(strict=False)
    if not output_input.is_absolute() or str(output_input) != str(output):
        raise ProducerError("review-output-path-not-canonical", str(review_output))
    artifacts = (
        cycle_dir(canonical_root, record["campaign_id"], cycle_id, record)
        / "artifacts"
    ).resolve(strict=False)
    try:
        locator = output.relative_to(canonical_root).as_posix()
        cycle_locator = output.relative_to(artifacts)
    except ValueError as exc:
        raise ProducerError("review-output-outside-cycle", str(output)) from exc
    if not cycle_locator.parts or cycle_locator.parts[0] != "plans":
        raise ProducerError("review-output-bucket-forbidden", str(output))
    if output.is_dir() or output.exists() and not output.is_file():
        raise ProducerError("review-output-target-invalid", str(output))
    current = output
    while current != artifacts:
        if current.is_symlink():
            raise ProducerError("review-output-symlink", str(current))
        current = current.parent
    binding: Dict[str, Any] = {
        "schema_version": 2,
        "attempt_id": attempt_id,
        "cycle_id": cycle_id,
        "producer_id": producer_id,
        "worktree": str(canonical_worktree),
        "artifact_root": str(canonical_root),
        "capability": capability,
        "unit": unit,
        "output_path": str(output),
    }
    binding["digest"] = review_output_binding_digest(binding)
    binding["locator_b64"] = encode_review_output_locator(locator)
    return binding


def review_lease_acquire(
    root: Path, *, cycle_id: str, attempt_id: str, deadline_seconds: float = 900.0,
    now: Optional[float] = None, review_output: Optional[str | Path] = None,
    binding: Optional[Mapping[str, Any]] = None,
    governed_identity: Optional[Mapping[str, Any]] = None,
    jobs: Optional[str | Path] = None,
    watchdog_budget: Optional[FiniteWatchdogBudget] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        try:
            path.lstat()
            existing_path = True
        except FileNotFoundError:
            existing_path = False
        existing = _read_json(path)
        when = time.time() if now is None else now
        v2_requested = review_output is not None or binding is not None
        if v2_requested and watchdog_budget is None:
            watchdog_budget = begin_finite_watchdog(
                deadline_seconds, origin_epoch=when
            )
        if v2_requested and not isinstance(watchdog_budget, FiniteWatchdogBudget):
            raise ProducerError("review-lease-budget-invalid", attempt_id)
        if v2_requested and watchdog_budget is not None:
            if remaining_watchdog_seconds(watchdog_budget) <= 0:
                raise ProducerError("review-lease-budget-exhausted", attempt_id)
            if when < watchdog_budget.origin_epoch:
                raise ProducerError("review-lease-budget-contradictory", attempt_id)
        # E47-9: the same (cycle, attempt) re-acquiring its own still-live
        # lease is an idempotent no-op -- zero state change.
        existing_live = existing is not None and _lease_record_is_live(
            existing, root=root, now=when
        )
        schema_version = 1
        exact_output = None
        registry_metadata: Mapping[str, object] = {}
        if v2_requested:
            if review_output is None or binding is None or jobs is None:
                raise ProducerError("review-lease-binding-incomplete", cycle_id)
            if not isinstance(binding, Mapping):
                raise ProducerError("review-lease-binding-invalid", attempt_id)
            admission_record = read_cycle_record(root, cycle_id)
            if admission_record is not None and admission_record.get("state") == "open":
                # This is the authoritative race-closing check: the admission
                # mutex above is still held and publication uses that same
                # mutex.  No recovery or nested lock acquisition occurs here.
                _raise_if_review_publication_started(root, admission_record)
            try:
                canonical_binding = prepare_review_output_binding(
                    root, cycle_id=cycle_id,
                    producer_id=str(binding.get("producer_id", "")),
                    attempt_id=attempt_id, review_output=review_output,
                    capability=str(binding.get("capability", "")),
                    unit=str(binding.get("unit", "")),
                    worktree=str(binding.get("worktree", "")),
                )
                checked_binding = validate_review_output_binding(
                    jobs, attempt_id=attempt_id, output_path=review_output,
                    cycle_id=cycle_id,
                    producer_id=str(canonical_binding["producer_id"]),
                    capability=str(canonical_binding["capability"]),
                    unit=str(canonical_binding["unit"]),
                    worktree=str(canonical_binding["worktree"]),
                    artifact_root=str(canonical_binding["artifact_root"]),
                )
            except Exception as exc:
                if isinstance(exc, (OSError, ValueError, TypeError)):
                    raise ProducerError("review-lease-binding-invalid", attempt_id) from exc
                raise ProducerError(getattr(exc, "reason", "review-lease-binding-invalid"), attempt_id) from exc
            exact_output = Path(checked_binding["output_path"])
            closed_fields = (
                "schema_version", "attempt_id", "cycle_id", "producer_id",
                "worktree", "artifact_root", "capability", "unit",
                "output_path", "digest", "locator_b64",
            )
            if any(binding.get(key) != canonical_binding.get(key) for key in closed_fields):
                raise ProducerError("review-lease-binding-mismatch", attempt_id)
            if checked_binding["digest"] != canonical_binding["digest"]:
                raise ProducerError("review-binding-digest-mismatch", attempt_id)
            identity = dict(governed_identity or {})
            required_identity = ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")
            if not all(identity.get(key) not in (None, "") for key in required_identity):
                raise ProducerError("review-governed-identity-incomplete", cycle_id)
            if not str(identity.get("pid")).isdigit() or not str(identity.get("pgid")).isdigit():
                raise ProducerError("review-governed-identity-invalid", cycle_id)
            registry_metadata = checked_binding.get("_registry_metadata", {})
            if not isinstance(registry_metadata, Mapping):
                raise ProducerError("review-lease-binding-invalid", attempt_id)
            if any(
                str(registry_metadata.get(key, "")) != str(identity.get(key, ""))
                for key in _PROCESS_IDENTITY_METADATA_KEYS
            ):
                raise ProducerError("review-lease-identity-mismatch", attempt_id)
            nonce = identity.get("review_governed_lease_nonce", registry_metadata.get("review_governed_lease_nonce"))
            if not isinstance(nonce, str) or REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(nonce) is None:
                raise ProducerError("review-governed-lease-nonce-invalid", attempt_id)
            if (
                registry_metadata.get("review_governed_lease") != REVIEW_GOVERNED_LEASE_KIND
                or registry_metadata.get("review_governed_lease_nonce") != nonce
            ):
                raise ProducerError("review-governed-lease-nonce-mismatch", attempt_id)
            if not review_governed_lease_is_held(root, registry_metadata):
                raise ProducerError("review-governed-lease-not-held", attempt_id)
            schema_version = 2
            binding_digest = str(canonical_binding["digest"])
        else:
            identity = {}
            binding_digest = None
        if existing_live:
            if schema_version == 2:
                expected_existing = {
                    "schema_version": 2, "cycle_id": cycle_id,
                    "attempt_id": attempt_id, "producer_id": canonical_binding["producer_id"],
                    "worktree": canonical_binding["worktree"],
                    "artifact_root": canonical_binding["artifact_root"],
                    "capability": canonical_binding["capability"],
                    "unit": canonical_binding["unit"],
                    "review_output_path": str(exact_output),
                    "review_output_digest": binding_digest,
                    "review_governed_lease": registry_metadata.get("review_governed_lease"),
                    "review_governed_lease_nonce": registry_metadata.get("review_governed_lease_nonce"),
                }
                if any(existing.get(key) != value for key, value in expected_existing.items()):
                    raise ProducerError("review-lease-binding-mismatch", attempt_id)
                for key in _PROCESS_IDENTITY_METADATA_KEYS:
                    if str(existing.get(key, "")) != str(identity.get(key, "")):
                        raise ProducerError("review-lease-identity-mismatch", attempt_id)
                digest = review_lease_record_digest(existing)
                return {
                    "status": "already-held", "cycle_id": cycle_id,
                    "attempt_id": attempt_id,
                    "registry_metadata": {
                        "review_lease_acquired_at": existing.get("acquired_at", ""),
                        "review_lease_deadline": existing.get("deadline", ""),
                        "review_lease_record_digest": digest,
                    },
                }
            return {"status": "already-held", "cycle_id": cycle_id, "attempt_id": attempt_id}
        if schema_version == 2 and existing_path:
            raise ProducerError("review-lease-existing-invalid", attempt_id)
        pid = int(identity.get("pid", os.getpid()))
        pid_start = str(identity.get("pid_start", process_start_ticks(pid) or ""))
        pgid = int(identity.get("pgid", os.getpgid(pid)))
        lease_seconds = (
            watchdog_budget.timeout_seconds
            if schema_version == 2 and watchdog_budget is not None
            else max(1.0, deadline_seconds)
        )
        record = {
            "schema_version": schema_version, "cycle_id": cycle_id, "attempt_id": attempt_id,
            "pid": pid, "pid_start": pid_start,
            "pgid": pgid, "acquired_at": _rfc3339(when),
            # The audit wall deadline belongs to the one launch-origin clock,
            # not to the later lease-acquisition moment.  ``acquired_at``
            # remains the real acquisition observation for future-timestamp
            # rejection and audit coherence.
            "deadline": (_rfc3339_precise(
                watchdog_budget.origin_epoch + watchdog_budget.timeout_seconds
                if schema_version == 2 and watchdog_budget is not None
                else when + lease_seconds
            ) if schema_version == 2 else _rfc3339(when + lease_seconds)),
            "released_at": None, "expired": False,
        }
        if schema_version == 2:
            record.update({
                "review_output_path": str(exact_output),
                "review_output_digest": binding_digest,
                "worktree": canonical_binding["worktree"],
                "artifact_root": canonical_binding["artifact_root"],
                "capability": canonical_binding["capability"],
                "unit": canonical_binding["unit"],
                "producer_id": canonical_binding["producer_id"],
                "review_governed_lease": registry_metadata.get("review_governed_lease"),
                "review_governed_lease_nonce": registry_metadata.get("review_governed_lease_nonce"),
                "watchdog_timeout_seconds": watchdog_budget.timeout_seconds,
                "watchdog_origin_monotonic_ns": watchdog_budget.origin_monotonic_ns,
                "watchdog_deadline_monotonic_ns": watchdog_budget.deadline_monotonic_ns,
                "watchdog_origin_epoch": watchdog_budget.origin_epoch,
                "watchdog_deadline_epoch": watchdog_budget.origin_epoch + watchdog_budget.timeout_seconds,
                "watchdog_budget_digest": watchdog_budget.digest,
            })
            for key in _PROCESS_IDENTITY_METADATA_KEYS:
                if key in identity:
                    record[key] = identity[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, _json_bytes(record), 0o600)
        result = {"status": "acquired", "cycle_id": cycle_id, "attempt_id": attempt_id}
        if schema_version == 2:
            result["registry_metadata"] = {
                "review_lease_acquired_at": record["acquired_at"],
                "review_lease_deadline": record["deadline"],
                "review_lease_record_digest": review_lease_record_digest(record),
            }
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_release(
    root: Path, *, cycle_id: str, attempt_id: str, now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        existing = _read_json(path)
        if existing is None or existing.get("released_at") is not None:
            # E47-9: releasing an already-released (or never-acquired) lease
            # is an idempotent no-op.
            return {"status": "already-released", "cycle_id": cycle_id, "attempt_id": attempt_id}
        existing["released_at"] = _rfc3339(time.time() if now is None else now)
        _write_atomic(path, _json_bytes(existing), 0o600)
        return {"status": "released", "cycle_id": cycle_id, "attempt_id": attempt_id}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_status(root: Path, *, cycle_id: str, attempt_id: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    if attempt_id is not None:
        record = _read_json(_review_lease_path(root, cycle_id, attempt_id))
        return {
            "cycle_id": cycle_id, "attempt_id": attempt_id,
            "live": _lease_record_is_live(record, root=root),
        }
    live_path = _live_review_lease(root, cycle_id)
    return {"cycle_id": cycle_id, "live": live_path is not None}
