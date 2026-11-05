"""Dispatch contract: review outputs and governed review leases.

Review output locators, governed review leases, the holder disposition, and
validating and authorizing a review output binding.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import base64
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Mapping
from hearting_gates import note_same_work_drift

from dispatch_contract_base import (
    _parse_review_metadata, _PROCESS_IDENTITY_METADATA_KEYS, DispatchContractError,
    REVIEW_GOVERNED_LEASE_KIND, REVIEW_GOVERNED_LEASE_NONCE_RE,
    REVIEW_OUTPUT_BINDING_SCHEMA_VERSION, ReviewHolderDisposition,
)

from dispatch_contract_process import (
    attempt_governed_process_quiescence,
)


def encode_review_output_locator(locator: str) -> str:
    """Encode an artifact-root relative path for comma-delimited metadata."""

    if not locator or "\x00" in locator:
        raise DispatchContractError("review-output-locator-invalid", locator)
    encoded = base64.urlsafe_b64encode(locator.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def decode_review_output_locator(encoded: str) -> str:
    """Decode only the canonical unpadded URL-safe locator representation."""

    if not encoded or re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
        raise DispatchContractError("review-output-locator-invalid", encoded)
    try:
        padding = "=" * (-len(encoded) % 4)
        locator = base64.b64decode(
            encoded + padding, altchars=b"-_", validate=True
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise DispatchContractError("review-output-locator-invalid", encoded) from exc
    if encode_review_output_locator(locator) != encoded:
        raise DispatchContractError("review-output-locator-invalid", encoded)
    return locator


def review_governed_lease_path(
    artifact_root: str | Path, cycle_id: str, attempt_id: str
) -> Path:
    """Return the derived namespace-independent worker-lifetime lease path."""

    root_input = Path(artifact_root)
    root = root_input.resolve(strict=False)
    if not root_input.is_absolute() or str(root_input) != str(root):
        raise DispatchContractError(
            "review-governed-lease-root-noncanonical", str(artifact_root)
        )
    safe = re.compile(r"[A-Za-z0-9._-]{4,240}")
    if safe.fullmatch(cycle_id or "") is None or safe.fullmatch(attempt_id or "") is None:
        raise DispatchContractError(
            "review-governed-lease-identity-invalid", f"{cycle_id}:{attempt_id}"
        )
    return (
        root / ".runtime" / "artifact-producer" / "v1" /
        "review-governed-leases" / cycle_id / f"{attempt_id}.lease"
    )


def review_governed_lease_payload(
    attempt_id: str, cycle_id: str, nonce: str
) -> bytes:
    return (
        f"kind={REVIEW_GOVERNED_LEASE_KIND}\n"
        f"attempt_id={attempt_id}\n"
        f"cycle_id={cycle_id}\n"
        f"nonce={nonce}\n"
    ).encode("ascii")


def review_governed_lease_is_held(
    artifact_root: str | Path, metadata: Mapping[str, object]
) -> bool:
    """Probe the exact summary-owned worker lease across PID namespaces."""

    attempt_id = str(metadata.get("attempt_id", ""))
    cycle_id = str(metadata.get("review_cycle_id", ""))
    nonce = str(metadata.get("review_governed_lease_nonce", ""))
    if (
        metadata.get("review_governed_lease") != REVIEW_GOVERNED_LEASE_KIND
        or REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(nonce) is None
    ):
        return False
    try:
        path = review_governed_lease_path(artifact_root, cycle_id, attempt_id)
        if path.is_symlink() or path.parent.is_symlink():
            return False
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except (DispatchContractError, OSError):
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        expected = review_governed_lease_payload(attempt_id, cycle_id, nonce)
        if os.pread(fd, len(expected) + 1, 0) != expected:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def review_output_binding_digest(binding: Mapping[str, object]) -> str:
    """Digest the closed, path-safe identity tuple of a reviewer report."""
    fields = (
        "schema_version", "attempt_id", "cycle_id", "producer_id",
        "worktree", "artifact_root", "capability", "unit", "output_path",
    )
    payload = {key: binding.get(key, "") for key in fields}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def review_lease_record_digest(record: Mapping[str, object]) -> str:
    """Seal every persisted lease field; the digest itself lives in jobs."""

    encoded = json.dumps(
        dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _review_epoch(value: str) -> float:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def review_holder_disposition(
    lease_record: Mapping[str, object] | None,
    registry_metadata: Mapping[str, object] | None,
    artifact_root: str | Path,
    *,
    now: float | None = None,
) -> ReviewHolderDisposition:
    """Classify one exact v2 holder without treating its timestamp as death.

    Process evidence wins over the flock fallback.  The fallback is admitted
    only for the namespace-unverifiable case required by SD-90, so a released,
    reused, zombie, or positively dead process can never be revived by a held
    file descriptor.
    """

    if not isinstance(lease_record, Mapping) or lease_record.get("schema_version") != 2:
        return ReviewHolderDisposition("malformed", "lease-record-schema")
    if not isinstance(registry_metadata, Mapping):
        return ReviewHolderDisposition("malformed", "registry-metadata-missing")
    if lease_record.get("released_at") is not None or lease_record.get("expired") is True:
        return ReviewHolderDisposition("dead", "released-or-expired")
    acquired_at = lease_record.get("acquired_at")
    deadline = lease_record.get("deadline")
    if not isinstance(acquired_at, str) or not isinstance(deadline, str):
        return ReviewHolderDisposition("malformed", "lease-time-missing")
    try:
        acquired_epoch = _review_epoch(acquired_at)
        deadline_epoch = _review_epoch(deadline)
    except (TypeError, ValueError, OverflowError):
        return ReviewHolderDisposition("malformed", "lease-time-invalid")
    observed_now = time.time() if now is None else now
    if acquired_epoch > observed_now:
        return ReviewHolderDisposition("malformed", "lease-acquired-in-future")
    if deadline_epoch <= acquired_epoch:
        return ReviewHolderDisposition("malformed", "lease-deadline-invalid")
    required = ("attempt_id", "pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")
    identity_complete = not any(not str(registry_metadata.get(key, "")) for key in required)
    if identity_complete and (
        not str(registry_metadata.get("pid", "")).isdigit()
        or not str(registry_metadata.get("pgid", "")).isdigit()
    ):
        return ReviewHolderDisposition("malformed", "holder-identity-invalid")
    nonce = str(registry_metadata.get("review_governed_lease_nonce", ""))
    witness_bound = (
        registry_metadata.get("review_governed_lease") == REVIEW_GOVERNED_LEASE_KIND
        and REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(nonce) is not None
    )
    if not identity_complete and not witness_bound:
        return ReviewHolderDisposition("malformed", "holder-identity-incomplete")
    budget_fields = (
        "watchdog_timeout_seconds", "watchdog_origin_monotonic_ns",
        "watchdog_deadline_monotonic_ns", "watchdog_origin_epoch",
        "watchdog_deadline_epoch", "watchdog_budget_digest",
    )
    budget_present = [key in lease_record for key in budget_fields]
    if any(budget_present) and not all(budget_present):
        return ReviewHolderDisposition("malformed", "watchdog-budget-incomplete")
    if all(budget_present):
        try:
            timeout = float(lease_record["watchdog_timeout_seconds"])
            origin_ns = int(lease_record["watchdog_origin_monotonic_ns"])
            deadline_ns = int(lease_record["watchdog_deadline_monotonic_ns"])
            origin_epoch = float(lease_record["watchdog_origin_epoch"])
            deadline_epoch_exact = float(lease_record["watchdog_deadline_epoch"])
            budget_payload = {
                "timeout_seconds": timeout,
                "origin_monotonic_ns": origin_ns,
                "deadline_monotonic_ns": deadline_ns,
                "origin_epoch": origin_epoch,
            }
            expected_budget_digest = "sha256:" + hashlib.sha256(
                json.dumps(budget_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                not math.isfinite(timeout) or timeout <= 0
                or origin_ns < 0
                or deadline_ns != origin_ns + int(timeout * 1_000_000_000)
                or not math.isfinite(origin_epoch)
                or not math.isfinite(deadline_epoch_exact)
                or deadline_epoch_exact != origin_epoch + timeout
                or expected_budget_digest != str(lease_record["watchdog_budget_digest"])
                or abs(deadline_epoch - deadline_epoch_exact) > 1.0
            ):
                return ReviewHolderDisposition("malformed", "watchdog-budget-contradictory")
        except (TypeError, ValueError, OverflowError):
            return ReviewHolderDisposition("malformed", "watchdog-budget-invalid")
    for key in _PROCESS_IDENTITY_METADATA_KEYS | {"review_governed_lease", "review_governed_lease_nonce"}:
        if key in lease_record and str(lease_record.get(key, "")) != str(registry_metadata.get(key, "")):
            return ReviewHolderDisposition("malformed", f"holder-binding-mismatch:{key}")
    record_digest = lease_record.get("review_lease_record_digest")
    metadata_digest = registry_metadata.get("review_lease_record_digest")
    if metadata_digest is None:
        return ReviewHolderDisposition("malformed", "lease-record-digest-missing")
    if record_digest is not None and metadata_digest is not None:
        unsigned = {key: value for key, value in lease_record.items() if key != "review_lease_record_digest"}
        if review_lease_record_digest(unsigned) != metadata_digest:
            return ReviewHolderDisposition("malformed", "lease-record-digest-mismatch")
    if identity_complete:
        process = attempt_governed_process_quiescence({str(k): str(v) for k, v in registry_metadata.items()})
        if process.state == "live":
            return ReviewHolderDisposition("live", process.reason)
        if process.state == "quiescent":
            return ReviewHolderDisposition("dead", process.reason)
        if process.reason in {"process-identity-invalid", "process-identity-missing"}:
            return ReviewHolderDisposition("malformed", process.reason)
        if process.reason != "process-namespace-unverifiable" or not witness_bound:
            return ReviewHolderDisposition("unobservable", process.reason)
    if witness_bound and review_governed_lease_is_held(artifact_root, registry_metadata):
        return ReviewHolderDisposition("live", "governed-witness-held")
    return ReviewHolderDisposition("unobservable", "process-namespace-unverifiable")


def validate_review_output_binding(
    jobs: str | Path,
    *,
    attempt_id: str,
    output_path: str | Path,
    cycle_id: str,
    producer_id: str,
    capability: str,
    unit: str,
    worktree: str | Path,
    artifact_root: str | Path,
) -> dict[str, object]:
    """Validate one route-free registered review and its exact report target.

    This is deliberately separate from ``validate_attempt_metadata``: the
    latter validates generic dispatch rows and must not grant artifact rights.
    A report binding is authorization evidence only when the canonical jobs
    file contains one exact, open registered review row with every axis bound.
    """
    registry_input = Path(jobs)
    registry = registry_input.resolve(strict=False)
    if (not registry_input.is_absolute() or str(registry_input) != str(registry)
            or registry.is_symlink() or not registry.is_file()):
        raise DispatchContractError("review-jobs-path-not-canonical", str(jobs))
    worktree_input = Path(worktree)
    root_input = Path(artifact_root)
    expected_worktree = str(worktree_input.resolve(strict=False))
    expected_root = str(root_input.resolve(strict=False))
    if (not worktree_input.is_absolute() or str(worktree_input) != expected_worktree
            or not root_input.is_absolute() or str(root_input) != expected_root):
        raise DispatchContractError("review-binding-root-not-canonical", str(artifact_root))
    if not attempt_id or not cycle_id or not producer_id or not capability or not unit:
        raise DispatchContractError("review-binding-tuple-incomplete", attempt_id)
    target = Path(output_path)
    if not target.is_absolute():
        raise DispatchContractError("review-output-not-absolute", str(target))
    target = target.resolve(strict=False)
    if str(target) != str(Path(output_path)):
        raise DispatchContractError("review-output-path-not-canonical", str(output_path))
    try:
        target.relative_to(Path(expected_root))
    except ValueError as exc:
        raise DispatchContractError("review-output-outside-artifact-root", str(target)) from exc
    if target.name in {"", ".", ".."} or target.is_dir() or target.exists() and not target.is_file():
        raise DispatchContractError("review-output-target-invalid", str(target))
    current = target
    while current != Path(expected_root):
        if current.is_symlink():
            raise DispatchContractError("review-output-symlink", str(current))
        current = current.parent
        if current == current.parent and current != Path(expected_root):
            raise DispatchContractError("review-output-outside-artifact-root", str(target))
    matches: list[tuple[list[str], dict[str, str]]] = []
    malformed_matches: list[str] = []
    try:
        lines = registry.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise DispatchContractError("review-jobs-unreadable", str(registry)) from exc
    for line in lines:
        fields = line.split("\t")
        attempt_token = f"attempt_id={attempt_id}"
        if len(fields) != 6:
            tokens = re.split(r"[,\t]", line)
            if attempt_token in tokens:
                malformed_matches.append("review-row-columns-invalid")
            continue
        attempt_hints = [
            part.split("=", 1)[1]
            for part in fields[5].split(",")
            if part.startswith("attempt_id=")
        ]
        try:
            metadata = _parse_review_metadata(fields[5])
        except DispatchContractError as exc:
            if attempt_id in attempt_hints:
                malformed_matches.append(exc.reason)
            continue
        if metadata.get("attempt_id") == attempt_id:
            matches.append((fields, metadata))
    if malformed_matches:
        raise DispatchContractError(
            "review-attempt-row-malformed",
            f"{attempt_id}:{','.join(malformed_matches)}",
        )
    if len(matches) != 1:
        raise DispatchContractError("review-attempt-not-unique", f"{attempt_id}:{len(matches)}")
    fields, metadata = matches[0]
    if fields[1] not in {"open", "running"}:
        raise DispatchContractError("review-attempt-not-open", attempt_id)
    if metadata.get("review_output_path") or metadata.get("review_output"):
        raise DispatchContractError("review-output-path-in-jobs-metadata", attempt_id)
    expected = {
        "attempt_schema_version": "2", "dispatch_depth": "1",
        "transport": "headless", "execution_surface": "registered-headless",
        "registered_worker": "1", "worker_type": "review",
        "unit": unit, "capability": capability,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise DispatchContractError("review-binding-axis-mismatch", key)
    route_keys = {
        "route_file", "route_id", "route_hash", "route_node", "registry_digest",
        "write_scope", "completion_gate", "owner_route_file", "owner_route_id",
        "owner_route_hash",
    }
    if any(metadata.get(key) for key in route_keys):
        raise DispatchContractError("review-attempt-route-bound", attempt_id)
    if (
        fields[3] != expected_worktree
        or metadata.get("worktree", expected_worktree) != expected_worktree
        or str(Path(fields[2]).resolve(strict=False)) != expected_worktree
    ):
        note_same_work_drift("review-binding-worktree-mismatch", attempt_id)
    if metadata.get("artifact_root") != expected_root:
        note_same_work_drift("review-binding-artifact-root-mismatch", attempt_id)
    if metadata.get("review_cycle_id") != cycle_id:
        note_same_work_drift("review-binding-cycle-mismatch", cycle_id)
    if metadata.get("review_producer_id") != producer_id:
        note_same_work_drift("review-binding-producer-mismatch", producer_id)
    try:
        locator = target.relative_to(Path(expected_root)).as_posix()
    except ValueError as exc:
        raise DispatchContractError("review-output-outside-artifact-root", str(target)) from exc
    encoded_locator = metadata.get("review_output_locator_b64", "")
    try:
        mirrored_locator = decode_review_output_locator(encoded_locator)
    except DispatchContractError as exc:
        raise DispatchContractError("review-binding-locator-mismatch", attempt_id) from exc
    if metadata.get("review_output_locator") or mirrored_locator != locator:
        raise DispatchContractError("review-binding-locator-mismatch", attempt_id)
    binding: dict[str, object] = {
        "schema_version": REVIEW_OUTPUT_BINDING_SCHEMA_VERSION,
        "attempt_id": attempt_id, "cycle_id": cycle_id,
        "producer_id": producer_id, "worktree": expected_worktree,
        "artifact_root": expected_root, "capability": capability,
        "unit": unit, "output_path": str(target),
    }
    binding["digest"] = review_output_binding_digest(binding)
    mirrored = metadata.get("review_output_digest")
    if mirrored != binding["digest"]:
        note_same_work_drift("review-binding-digest-mismatch", attempt_id)
    binding["_registry_metadata"] = metadata
    binding["_registry_status"] = fields[1]
    return binding


def review_output_write_authorized(
    jobs: str | Path, *, output_path: str | Path, attempt_id: str,
    cycle_id: str, producer_id: str, capability: str, unit: str,
    worktree: str | Path, artifact_root: str | Path,
    lease_record: Mapping[str, object] | None,
) -> bool:
    """Read-only hook predicate for one exact live v2 report lease."""
    try:
        binding = validate_review_output_binding(
            jobs, attempt_id=attempt_id, output_path=output_path,
            cycle_id=cycle_id, producer_id=producer_id, capability=capability,
            unit=unit, worktree=worktree, artifact_root=artifact_root,
        )
        if not isinstance(lease_record, Mapping) or lease_record.get("schema_version") != 2:
            return False
        for key, value in {
            "attempt_id": attempt_id, "cycle_id": cycle_id,
            "producer_id": producer_id, "worktree": binding["worktree"],
            "artifact_root": binding["artifact_root"], "capability": capability,
            "unit": unit, "review_output_path": binding["output_path"],
            "review_output_digest": binding["digest"],
        }.items():
            if lease_record.get(key) != value:
                return False
        if (
            lease_record.get("released_at") is not None
            or lease_record.get("expired") is not False
        ):
            return False
        acquired_at = lease_record.get("acquired_at")
        deadline = lease_record.get("deadline")
        if not isinstance(acquired_at, str) or not isinstance(deadline, str):
            return False
        try:
            acquired_epoch = _review_epoch(acquired_at)
            deadline_epoch = _review_epoch(deadline)
        except (TypeError, ValueError, OverflowError):
            return False
        if deadline_epoch <= acquired_epoch:
            return False
        metadata = binding.get("_registry_metadata")
        if not isinstance(metadata, Mapping):
            return False
        required_identity = ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")
        if not all(str(metadata.get(key, "")) for key in required_identity):
            return False
        for key in _PROCESS_IDENTITY_METADATA_KEYS:
            observed = str(metadata.get(key, ""))
            persisted = str(lease_record.get(key, ""))
            if observed != persisted:
                return False
        for key in ("review_governed_lease", "review_governed_lease_nonce"):
            if str(metadata.get(key, "")) != str(lease_record.get(key, "")):
                return False
        if (
            acquired_at != metadata.get("review_lease_acquired_at")
            or deadline != metadata.get("review_lease_deadline")
        ):
            return False
        record_for_digest = {
            key: value for key, value in lease_record.items()
            if key != "review_lease_record_digest"
        }
        record_digest = review_lease_record_digest(record_for_digest)
        if record_digest != metadata.get("review_lease_record_digest"):
            return False
        disposition = review_holder_disposition(
            lease_record, metadata, binding["artifact_root"]
        )
        return disposition.state == "live"
    except (DispatchContractError, OSError, TypeError, ValueError):
        return False
