"""Dispatch contract: registry rows and sealed results.

Parsing and atomically replacing registry rows, sealing foreground, detached
review and cancellation-quiescence results, and stage/subsession advance
claims (`claim_stage_advance` itself stays in `dispatch_contract.py`).

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from typing import Mapping
from types import MappingProxyType

from dispatch_contract_base import (
    _cancellation_quiescence_receipt_record, _FOREGROUND_FIXED_FAILURES,
    _foreground_review_eligibility_reason, _parse_review_metadata,
    _PROCESS_IDENTITY_METADATA_KEYS, _updated_attempt_metadata,
    ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT, ATTEMPT_DESCENDANT_PROOF,
    ATTEMPT_MUTABLE_METADATA, ATTEMPT_TERMINAL_EVIDENCE_KEYS, DispatchContractError,
    FOREGROUND_OUTCOME_KEYS, FOREGROUND_OUTCOME_SOURCE, foreground_review_eligible,
    GROUP_REAP_PROOF, parse_registry_metadata, QuiescenceProof, ROUTE_IDENTITY_METADATA_KEYS,
    SealedForegroundOutcome,
)

from dispatch_contract_roots import (
    ensure_global_registry_writable,
)

from dispatch_contract_policy import (
    validate_attempt_metadata,
)

from dispatch_contract_process import (
    _cancellation_quiescence_binding_digest, _canonical_sha256, _detached_group_drain_receipt,
    _portable_teardown_receipt_digest, process_launch_identity,
)


def new_attempt_id(value: str | None = None) -> str:
    if value:
        if not value.startswith("att-") or len(value) < 12:
            raise DispatchContractError("invalid-attempt-id", value)
        return value
    return "att-" + uuid.uuid4().hex


class _RegistryParse:
    """One registration's parse of registry rows: each row text is split and parsed once.

    A registration looks at the same rows in several checks, and again in its fresh
    read under the jobs lock; this keeps one parse per row text across all of them.
    The fields and metadata it returns are shared, so callers only read them.
    """

    __slots__ = ("_rows",)

    def __init__(self):
        self._rows = {}

    def rows(self, lines):
        """`(fields, metadata)` for each line, or None for a line without six fields."""
        parsed, out = self._rows, []
        for line in lines:
            entry = parsed.get(line, False)
            if entry is False:
                fields = line.split("\t")
                entry = (fields, parse_registry_metadata(fields[5])) if len(fields) == 6 else None
                parsed[line] = entry
            out.append(entry)
        return out


def _immutable_attempt_identity(fields: list[str]) -> tuple[object, ...]:
    if len(fields) != 6:
        raise DispatchContractError("invalid-registry-row", "expected six tab-separated fields")
    metadata = parse_registry_metadata(fields[5])
    validate_attempt_metadata(metadata)
    immutable_metadata = tuple(
        sorted(
            (key, value)
            for key, value in metadata.items()
            if key not in ATTEMPT_MUTABLE_METADATA
            and key not in FOREGROUND_OUTCOME_KEYS
            and key != "review_process_outcome_b64"
        )
    )
    return fields[2], fields[3], fields[4], immutable_metadata



def launched_attempt_identity(fields: list[str]) -> tuple[object, ...]:
    """Compare an admitted attempt across its one terminal/delivery commit.

    Reuse registry mutation vocabularies; terminal evidence is added at close,
    whereas the admitted process identity and lifecycle must stay unchanged.
    """
    base = _immutable_attempt_identity(fields)
    metadata = parse_registry_metadata(fields[5])
    return (*base[:3],
            tuple((key, value) for key, value in base[3]
                  if key not in ATTEMPT_TERMINAL_EVIDENCE_KEYS),
            tuple((key, metadata.get(key, "")) for key in sorted(_PROCESS_IDENTITY_METADATA_KEYS)),
            metadata.get("launch_lifecycle", ""))

def _atomic_registry_replace(jobs: Path, lines: list[str]) -> None:
    """Replace the registry after fsync without exposing a truncated file.

    The write path is byte-for-byte what it was (same temp file, fsync, rename,
    directory fsync).  A derived ``jobs.log.lineage.json`` sidecar additionally
    records the line-aligned prefix this replacement preserved, so a reader can
    rescan only the changed suffix.  Recording never changes the result: an
    unreadable/racing parent, a missing temp fd, or a broken sidecar simply
    leaves no entry and the next reader does the full read it always did.
    """
    from dispatch_registry_cache import (common_prefix_line_aligned, record_lineage,
                                         registry_key)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    parent_key = child_key = anchor = None
    prefix = 0
    try:
        with jobs.open("rb") as existing:
            before = os.fstat(existing.fileno())
            if before.st_size:
                import mmap
                try:
                    with mmap.mmap(existing.fileno(), 0, access=mmap.ACCESS_READ) as view:
                        prefix = common_prefix_line_aligned(view, data)
                except (OSError, ValueError):
                    prefix = 0
            after = os.fstat(existing.fileno())
            if before.st_size == after.st_size and registry_key(before) == registry_key(after):
                parent_key = registry_key(after)
                anchor = hashlib.sha256(data[max(0, prefix - 4096):prefix]).hexdigest()
            else:
                prefix = 0
    except OSError:
        parent_key = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{jobs.name}.claim-", dir=str(jobs.parent))
    try:
        view = memoryview(data)
        while view:
            # os.write may write only part of the buffer; never rename a short image.
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.replace(tmp_name, jobs)
        # fstat after the rename: rename updates ctime, so a pre-rename key
        # would never match the path key a later reader observes.
        try:
            child_key = registry_key(fd)
        finally:
            os.close(fd)
            fd = -1
        dir_fd = os.open(str(jobs.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    if parent_key is not None and child_key is not None and anchor is not None:
        try:
            record_lineage(jobs, parent_key, child_key, prefix, anchor)
        except Exception:  # noqa: BLE001 - the derived record never changes the write
            pass


def _append_registry_rows(jobs: Path, rows: list[str]) -> None:
    """Append complete rows under the existing writer protocol and record lineage."""
    from dispatch_registry_cache import record_lineage, registry_key
    if not rows:
        return
    parent_key = child_key = anchor = None
    prefix = 0
    try:
        with jobs.open("rb") as existing:
            fd = existing.fileno()
            before = os.fstat(fd)
            if before.st_size and os.pread(fd, 1, before.st_size - 1) == b"\n":
                prefix = before.st_size
                start = max(0, prefix - 4096)
                anchor = hashlib.sha256(os.pread(fd, prefix - start, start)).hexdigest()
                parent_key = registry_key(os.fstat(fd))
    except OSError:
        parent_key = None
    with jobs.open("a", encoding="utf-8") as registry:
        # The fact is recorded only about the file this handle appends to: it must be
        # the same unchanged version the anchor was read from, and the child key comes
        # from the same descriptor, so a concurrent replace can never borrow the fact.
        try:
            if parent_key is not None and registry_key(os.fstat(registry.fileno())) != parent_key:
                parent_key = None
        except OSError:
            parent_key = None
        for row in rows:
            registry.write(row + "\n")
        registry.flush()
        os.fsync(registry.fileno())
        try:
            child_key = registry_key(os.fstat(registry.fileno()))
        except OSError:
            child_key = None
    if parent_key is not None and child_key is not None and anchor is not None:
        record_lineage(jobs, parent_key, child_key, prefix, anchor)



_FOREGROUND_SIGNED_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_FOREGROUND_FAILURE_SUFFIX = re.compile(r"(?:exit|signal)-([1-9][0-9]*)\Z")


def attempt_raw_row_sha256(raw_row: str) -> str:
    """Hash one exact raw registry row, without normalizing its bytes."""

    if not isinstance(raw_row, str):
        raise TypeError("raw registry row must be text")
    return hashlib.sha256(raw_row.encode("utf-8")).hexdigest()


def _foreground_outcome_values_from_pipe(pipe: str) -> dict[str, str] | None:
    """Validate the foreground namespace and return canonical values."""

    found: dict[str, str] = {}
    for part in pipe.split(","):
        key = part.split("=", 1)[0]
        if not key.startswith("foreground_"):
            continue
        if "=" not in part or key not in FOREGROUND_OUTCOME_KEYS:
            raise DispatchContractError("foreground-outcome-malformed", key)
        if key in found:
            raise DispatchContractError("foreground-outcome-malformed", key)
        found[key] = part.split("=", 1)[1]
    if not found:
        return None
    if set(found) != set(FOREGROUND_OUTCOME_KEYS):
        raise DispatchContractError(
            "foreground-outcome-partial", ",".join(sorted(found))
        )
    if found["foreground_outcome_schema"] != "1":
        raise DispatchContractError("foreground-outcome-malformed", "foreground_outcome_schema")
    if found["foreground_outcome_ready"] != "1":
        raise DispatchContractError("foreground-outcome-malformed", "foreground_outcome_ready")
    if found["foreground_outcome_source"] != FOREGROUND_OUTCOME_SOURCE:
        raise DispatchContractError("foreground-outcome-malformed", "foreground_outcome_source")
    exit_text = found["foreground_process_exit"]
    if not _FOREGROUND_SIGNED_INTEGER.fullmatch(exit_text):
        raise DispatchContractError("foreground-outcome-malformed", "foreground_process_exit")
    failure = found["foreground_process_failure"]
    if failure == "none":
        pass
    elif failure in _FOREGROUND_FIXED_FAILURES:
        pass
    else:
        match = _FOREGROUND_FAILURE_SUFFIX.fullmatch(failure)
        if match is None:
            raise DispatchContractError("foreground-outcome-malformed", "foreground_process_failure")
    try:
        exit_code = int(exit_text)
    except ValueError as exc:  # pragma: no cover - guarded by the regex
        raise DispatchContractError("foreground-outcome-malformed", "foreground_process_exit") from exc
    if (failure == "none") != (exit_code == 0):
        raise DispatchContractError("foreground-outcome-malformed", "none-exit-relation")
    if failure.startswith(("exit-", "signal-")):
        suffix = int(failure.split("-", 1)[1])
        if failure.startswith("exit-") and exit_code != suffix:
            raise DispatchContractError("foreground-outcome-malformed", "exit-relation")
    if found["foreground_group_empty"] not in {"0", "1"}:
        raise DispatchContractError("foreground-outcome-malformed", "foreground_group_empty")
    return {key: found[key] for key in FOREGROUND_OUTCOME_KEYS}


def _foreground_outcome_values(
    *, exit_code: object, failure: object, group_empty: object
) -> dict[str, str]:
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise DispatchContractError("foreground-outcome-malformed", "foreground_process_exit")
    if not isinstance(failure, str):
        raise DispatchContractError("foreground-outcome-malformed", "foreground_process_failure")
    storage_failure = "none" if failure == "" else failure
    if storage_failure == "none":
        pass
    elif storage_failure in _FOREGROUND_FIXED_FAILURES:
        pass
    elif _FOREGROUND_FAILURE_SUFFIX.fullmatch(storage_failure) is None:
        raise DispatchContractError("foreground-outcome-malformed", "foreground_process_failure")
    if (storage_failure == "none") != (exit_code == 0):
        raise DispatchContractError("foreground-outcome-malformed", "none-exit-relation")
    if storage_failure.startswith("exit-"):
        if exit_code != int(storage_failure.split("-", 1)[1]):
            raise DispatchContractError("foreground-outcome-malformed", "exit-relation")
    if not isinstance(group_empty, bool):
        raise DispatchContractError("foreground-outcome-malformed", "foreground_group_empty")
    return {
        "foreground_outcome_schema": "1",
        "foreground_outcome_ready": "1",
        "foreground_process_exit": str(exit_code),
        "foreground_process_failure": storage_failure,
        "foreground_group_empty": "1" if group_empty else "0",
        "foreground_outcome_source": FOREGROUND_OUTCOME_SOURCE,
    }


def _review_watchdog_outcome_identity(metadata: Mapping[str, str]) -> dict[str, str]:
    return {key: metadata.get(key, "") for key in (
        "attempt_id", "pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns",
        "review_watchdog_budget_digest", "review_readiness_digest",
        "review_governed_lease_nonce",
    )}


def detached_review_outcome_from_pipe(pipe: str) -> dict[str, str] | None:
    """Read the watchdog's outcome; never derive process success from prose."""
    metadata = _parse_review_metadata(pipe)
    encoded = metadata.get("review_process_outcome_b64")
    if encoded is None:
        return None
    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"source", "identity", "outcome"}:
            raise ValueError("shape")
        if body["source"] != "review-watchdog-v1":
            raise ValueError("source")
        expected = _review_watchdog_outcome_identity(metadata)
        if body["identity"] != expected or not all(expected.values()):
            raise ValueError("identity")
        outcome = body["outcome"]
        if not isinstance(outcome, dict) or set(outcome) != set(FOREGROUND_OUTCOME_KEYS):
            raise ValueError("outcome")
        return _foreground_outcome_values_from_pipe(",".join(f"{k}={v}" for k, v in outcome.items()))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DispatchContractError("review-process-outcome-invalid") from exc


def seal_detached_review_result(
    jobs: str | Path, attempt_id: str, *, budget_digest: str, nonce: str,
    exit_code: int,
) -> None:
    """Only the exact watchdog seals its result; the reaper closes the row.

    The reader independently proves quiescence. A killed watchdog leaves no
    result, which cannot authorize PASS even when the log claims success.
    """
    current = process_launch_identity(os.getpid())
    failure = ("" if exit_code == 0 else "timeout" if exit_code == 124
               else f"signal-{-exit_code}" if exit_code < 0 else f"exit-{exit_code}")
    outcome = _foreground_outcome_values(exit_code=exit_code, failure=failure, group_empty=False)
    jobs = Path(jobs)
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8").splitlines()
        matches = _foreground_row(lines, attempt_id)
        if len(matches) != 1:
            raise DispatchContractError("review-process-outcome-row-not-unique")
        index, fields, metadata = matches[0]
        metadata = _parse_review_metadata(fields[5])
        if (metadata.get("worker_type") != "review"
                or metadata.get("dispatch_depth") != "1"
                or any(metadata.get(key) for key in ROUTE_IDENTITY_METADATA_KEYS)):
            return  # Route-bound reviews retain their marker completion path.
        if (fields[1] not in {"open", "running"}
                or metadata.get("launch_lifecycle") != "detached"
                or metadata.get("review_watchdog_budget_digest") != budget_digest
                or metadata.get("review_governed_lease_nonce") != nonce
                or metadata.get("review_admission") != "prepared"
                or metadata.get("launch_claimed") != "1"
                or any(metadata.get(key) != current.get(key) for key in
                       ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns"))
                or not foreground_review_eligible(
                    {**metadata, "launch_lifecycle": "foreground-scoped"},
                    expected_attempt_id=attempt_id, expected_pid=os.getpid(),
                    expected_pid_start=current.get("pid_start", ""), expected_pgid=os.getpid())):
            raise DispatchContractError("review-process-outcome-binding-mismatch")
        identity = _review_watchdog_outcome_identity(metadata)
        if not all(identity.values()):
            raise DispatchContractError("review-process-outcome-binding-mismatch")
        existing = detached_review_outcome_from_pipe(fields[5])
        if existing is not None:
            if existing != outcome:
                raise DispatchContractError("review-process-outcome-conflict")
            return
        body = {"source": "review-watchdog-v1", "identity": identity, "outcome": outcome}
        encoded = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).decode()
        fields[5] += ",review_process_outcome_b64=" + encoded
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)


def _foreground_row(
    lines: list[str], attempt_id: str
) -> list[tuple[int, list[str], dict[str, str]]]:
    matches: list[tuple[int, list[str], dict[str, str]]] = []
    for index, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            matches.append((index, fields, metadata))
    return matches


def seal_foreground_result(
    jobs: str | Path,
    attempt_id: str,
    pid: int,
    pid_start: str,
    pgid: int,
    *,
    exit_code: object,
    failure: object,
    group_empty: object,
) -> SealedForegroundOutcome:
    """Atomically persist and revalidate one foreground review outcome."""

    if not attempt_id:
        raise DispatchContractError("foreground-outcome-attempt-required", "attempt_id")
    desired = _foreground_outcome_values(
        exit_code=exit_code, failure=failure, group_empty=group_empty
    )
    jobs = Path(jobs)
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = _foreground_row(lines, attempt_id)
        if not matches:
            raise DispatchContractError("foreground-outcome-row-not-found", "count=0")
        if len(matches) != 1:
            raise DispatchContractError(
                "foreground-outcome-row-ambiguous", f"count={len(matches)}"
            )
        index, fields, metadata = matches[0]
        validate_attempt_metadata(metadata)
        if fields[1] not in {"open", "running"}:
            raise DispatchContractError("foreground-outcome-row-not-open", fields[1])
        if not foreground_review_eligible(
            metadata,
            expected_attempt_id=attempt_id,
            expected_pid=pid,
            expected_pid_start=pid_start,
            expected_pgid=pgid,
        ):
            eligibility_reason = _foreground_review_eligibility_reason(
                metadata,
                expected_attempt_id=attempt_id,
                expected_pid=pid,
                expected_pid_start=pid_start,
                expected_pgid=pgid,
            )
            if eligibility_reason == "binding":
                raise DispatchContractError("foreground-outcome-binding-mismatch", "binding")
            raise DispatchContractError("foreground-outcome-ineligible", eligibility_reason)
        existing = _foreground_outcome_values_from_pipe(fields[5])
        if existing is not None and existing != desired:
            raise DispatchContractError("foreground-outcome-conflict", "values")
        identity = _immutable_attempt_identity(fields)
        if existing is None:
            fields[5] = fields[5] + "," + ",".join(
                f"{key}={value}" for key, value in desired.items()
            )
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)

        readback_lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        readback = _foreground_row(readback_lines, attempt_id)
        if len(readback) == 0:
            raise DispatchContractError("foreground-outcome-readback-mismatch", "count=0")
        if len(readback) != 1:
            raise DispatchContractError(
                "foreground-outcome-readback-mismatch", f"count={len(readback)}"
            )
        _, readback_fields, readback_metadata = readback[0]
        try:
            if (
                _immutable_attempt_identity(readback_fields) != identity
                or readback_fields[1] not in {"open", "running"}
                or not foreground_review_eligible(
                    readback_metadata,
                    expected_attempt_id=attempt_id,
                    expected_pid=pid,
                    expected_pid_start=pid_start,
                    expected_pgid=pgid,
                )
                or _foreground_outcome_values_from_pipe(readback_fields[5]) != desired
            ):
                raise DispatchContractError("foreground-outcome-readback-mismatch", "row")
        except DispatchContractError as exc:
            if exc.reason == "foreground-outcome-readback-mismatch":
                raise
            raise DispatchContractError("foreground-outcome-readback-mismatch", exc.detail) from exc
        raw = readback_fields
        return SealedForegroundOutcome(
            values=MappingProxyType(dict(desired)),
            attempt_raw_row_sha256=attempt_raw_row_sha256("\t".join(raw)),
        )


def seal_cancellation_quiescence_receipt(
    jobs: Path, attempt_id: str, proof: QuiescenceProof
) -> str:
    """Atomically seal one cancellation-only receipt and return its digest."""

    if (
        not proof.proven
        or proof.reason != "cancellation-quiescence-proven"
        or proof.attempt_id != attempt_id
        or proof.process_group_state != "empty"
        or proof.descendant_state != "empty"
        or not proof.namespace_authority
    ):
        raise DispatchContractError("cancellation-quiescence-unproven")
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches: list[tuple[int, list[str], dict[str, str]]] = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((index, fields, metadata))
        if len(matches) != 1:
            raise DispatchContractError(
                "attempt-row-not-unique", f"attempt_id={attempt_id} rows={len(matches)}"
            )
        index, fields, metadata = matches[0]
        validate_attempt_metadata(metadata)

        if proof.binding_digest != _cancellation_quiescence_binding_digest(metadata):
            raise DispatchContractError("cancellation-quiescence-proof-drift")
        current_pgid = metadata.get("pgid", "")
        if not current_pgid.isdigit() or proof.pgid != int(current_pgid):
            raise DispatchContractError("cancellation-quiescence-proof-drift")
        if proof.source == "authenticated-namespace-portable":
            if (
                not _detached_group_drain_receipt(metadata)
                or proof.portable_receipt_digest
                != _portable_teardown_receipt_digest(metadata)
            ):
                raise DispatchContractError("cancellation-portable-receipt-invalid")
        elif proof.source == "namespace-extinct":
            if proof.portable_receipt_digest:
                raise DispatchContractError("cancellation-quiescence-proof-source-invalid")
        elif proof.source != "exact-teardown" or proof.portable_receipt_digest:
            raise DispatchContractError("cancellation-quiescence-proof-source-invalid")

        receipt_digest = _canonical_sha256(
            _cancellation_quiescence_receipt_record(metadata, proof)
        )
        existing_type = metadata.get("cancellation_quiescence_receipt", "")
        existing_digest = metadata.get("cancellation_receipt_digest", "")
        if existing_type or existing_digest:
            if (
                existing_type == ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT
                and existing_digest == receipt_digest
                and metadata.get("quiescence_pgid_proof") == GROUP_REAP_PROOF
                and metadata.get("quiescence_descendant_proof")
                == ATTEMPT_DESCENDANT_PROOF
            ):
                return existing_digest
            raise DispatchContractError("cancellation-quiescence-receipt-conflict")
        fields[5] = _updated_attempt_metadata(
            fields[5],
            {
                "cancellation_quiescence_receipt":
                    ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
                "cancellation_receipt_digest": receipt_digest,
                "quiescence_pgid_proof": GROUP_REAP_PROOF,
                "quiescence_descendant_proof": ATTEMPT_DESCENDANT_PROOF,
            },
        )
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return receipt_digest


def _stable_recovery_attempt_id(recovery_identity: str) -> str:
    """The retry attempt id an SD-106 recovery claim recorded (read for existing rows)."""
    encoded = f"{recovery_identity}\0retry_ordinal=1".encode("utf-8")
    return "att-retry-" + hashlib.sha256(encoded).hexdigest()[:32]


@dataclass(frozen=True)
class StageAdvanceRegistryClaim:
    """SD-110 claim result: no predecessor identity in `claim_key`
    (`§13.32.1-(3)C`). A stage-advance successor has no row yet, so this
    claim lives in its own CAS store under the same canonical registry lock."""

    stage_advance_id: str
    claim_key: tuple
    successor_attempt_id: str
    replayed: bool


def _stage_advance_claim_key_digest(claim_key: tuple) -> str:
    return hashlib.sha256(
        json.dumps(list(claim_key), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stable_stage_advance_attempt_id(stage_advance_id: str, successor_node: str) -> str:
    encoded = f"{stage_advance_id}\0{successor_node}".encode("utf-8")
    return "att-stage-advance-" + hashlib.sha256(encoded).hexdigest()[:32]


@dataclass(frozen=True)
class SubsessionAdvanceRegistryClaim:
    """SD-119 chain-advance claim result. `claim_key` is predecessor-free
    (`route_hash`, `route_node`, `chain_id`, `successor_subsession_index`,
    `advance_generation`) -- one generation, one chain, one index, one
    claimant, regardless of which predecessor index closed first."""

    subsession_advance_id: str
    claim_key: tuple
    successor_attempt_id: str
    replayed: bool


def claim_subsession_advance(
    jobs: Path,
    *,
    subsession_advance_id: str,
    route_hash: str,
    route_node: str,
    chain_id: str,
    successor_subsession_index: int,
    advance_generation: int,
    successor_attempt_id: str,
) -> SubsessionAdvanceRegistryClaim:
    """CAS one stable sub-session chain-advance claim inside `<jobs>.lock`, or
    replay the identical claim for a repeated `subsession_advance_id` (A-6/A-7).
    A distinct `claim_key` already claimed by a DIFFERENT `subsession_advance_id`
    is `subsession-advance-claim-conflict` (A-6), spawn 0."""

    if (
        not subsession_advance_id.startswith("ssadv-")
        or not route_hash
        or not route_node
        or not chain_id
        or not successor_attempt_id
        or type(advance_generation) is not int
        or type(successor_subsession_index) is not int
    ):
        raise DispatchContractError("subsession-advance-claim-identity-incomplete")
    ensure_global_registry_writable(jobs)
    claim_key = (route_hash, route_node, chain_id, successor_subsession_index, advance_generation)
    claims_dir = jobs.parent / "subsession_advance" / "claims"
    claim_path = claims_dir / f"{_stage_advance_claim_key_digest(claim_key)}.json"
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if claim_path.is_file():
            try:
                existing = json.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DispatchContractError("subsession-advance-claim-record-invalid") from exc
            if existing.get("subsession_advance_id") == subsession_advance_id:
                return SubsessionAdvanceRegistryClaim(
                    subsession_advance_id=subsession_advance_id,
                    claim_key=claim_key,
                    successor_attempt_id=existing["successor_attempt_id"],
                    replayed=True,
                )
            raise DispatchContractError("subsession-advance-claim-conflict")
        record = {
            "subsession_advance_id": subsession_advance_id,
            "claim_key": list(claim_key),
            "successor_attempt_id": successor_attempt_id,
            "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        claims_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".claim.", dir=str(claims_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, claim_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return SubsessionAdvanceRegistryClaim(
        subsession_advance_id=subsession_advance_id,
        claim_key=claim_key,
        successor_attempt_id=successor_attempt_id,
        replayed=False,
    )
