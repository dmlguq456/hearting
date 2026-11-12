"""Dispatch contract: marker-bound delivery and launch start.

The marker-bound delivery transaction, marking an attempt's launch started,
and recovering an attempt that never started.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from dispatch_attempt_policy import (
    SUBSESSION_NOTE, deferred_completion, verdict_pass, success_note,
    terminal_conflict_pending,
)

from dispatch_contract_base import (
    _delivery_commit_proven, _delivery_intent_values, _parse_review_metadata,
    _PROCESS_IDENTITY_METADATA_KEYS, _updated_attempt_metadata, DispatchContractError,
    MarkerBoundCompletionProof, MarkerBoundDeliveryResult, parse_registry_metadata,
    ProcessQuiescence, REVIEW_GOVERNED_LEASE_KIND, REVIEW_GOVERNED_LEASE_NONCE_RE,
    row_is_subsession,
)

from dispatch_contract_roots import (
    ensure_global_registry_writable,
)

from dispatch_contract_policy import (
    validate_attempt_metadata,
)

from dispatch_contract_process import (
    _terminal_cleanup_values, attempt_process_quiescence, exact_process_group_signal_authority,
    LAUNCH_CGROUP_METADATA_KEYS, LAUNCH_LINEAGE_METADATA_KEYS, process_identity_is_live,
    process_launch_identity,
)

from dispatch_contract_registry import (
    _atomic_registry_replace,
)

from dispatch_contract_gates import (
    completion_marker_is_current, review_self_disposition_lineage,
    review_self_disposition_origin,
)


_MARKER_BOUND_PROCESS_KEYS = (
    "pid",
    "pid_start",
    "pid_scope",
    "pid_host",
    "pid_host_start",
    "pid_host_ns",
    "pid_host_proof",
    "pid_ns",
    "pid_observer_ns",
    "pgid",
    "pgid_host",
    "launch_lifecycle",
    "launch_outcome",
    "group_reap_proof",
    "group_reap_pgid",
    "attempt_descendant_proof",
    "attempt_descendant_observer_ns",
    "post_exit_receipt_substitute",
)


def marker_bound_process_identity(
    metadata: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Return the process-evidence generation observed before jobs locking."""

    return tuple((key, metadata.get(key, "")) for key in _MARKER_BOUND_PROCESS_KEYS)


# SD-96: a sub-session row (`stage_authority=0`) can never publish a stage marker
# -- `capability-route.py complete` refuses it before touching the row -- so it
# needs a terminal note of its own. Without one it had no closure path at all and
# every success was booked as `dead-route-completion-rejected` (defect F).
# Deliberately NOT `completed-supervisor`: several gates read that note to mean "a
# checked supervisor closed a depth-1 owner", and one of them (SD-94 marker
# eligibility, `capability-route.py`) grants a privilege a sub-session must never
# have.
SUBSESSION_TERMINAL_CLASSIFIER = "completion-join-subsession-terminal-v1"
SUBSESSION_CHAIN_REFUSAL_CLASSIFIER = "subsession-chain-refusal-v1"


def _marker_bound_row_verdict(metadata: dict[str, str]) -> str:
    from dispatch_attempt_policy import readable_result
    result = readable_result(metadata)
    if result:
        return result
    failure_class = metadata.get("failure_class", "").lower()
    if verdict_pass(metadata) or success_note(metadata):
        return "PASS"
    if failure_class == "fail":
        return "FAIL"
    if failure_class == "blocked":
        return "BLOCKED"
    return ""


def _marker_bound_prepare_marker_proof(
    metadata: dict[str, str], attempt_id: str
) -> MarkerBoundCompletionProof | None:
    """Prove the full immutable marker chain before taking the jobs lock."""

    raw_path = metadata.get("completion_marker", "")
    if not raw_path:
        return None
    marker_path = Path(raw_path)
    if not marker_path.is_absolute() or marker_path.is_symlink():
        return None
    try:
        marker_bytes = marker_path.read_bytes()
    except OSError:
        return None
    if not marker_bytes or len(marker_bytes) > 65_536:
        return None
    try:
        marker = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    expected = {
        "route_id": metadata.get("route_id", ""),
        "route_hash": metadata.get("route_hash", ""),
        "node_id": metadata.get("route_node", ""),
        "attempt_id": attempt_id,
    }
    if any(not value or marker.get(key) != value for key, value in expected.items()):
        return None
    route_path = Path(metadata.get("route_file", ""))
    if not route_path.is_absolute() or route_path.is_symlink():
        return None
    try:
        route_bytes = route_path.read_bytes()
        route = json.loads(route_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if (
        not isinstance(route, dict)
        or route.get("route_id") != metadata.get("route_id")
        or route.get("route_hash") != metadata.get("route_hash")
        or not isinstance(route.get("nodes"), list)
    ):
        return None
    nodes = [
        node
        for node in route["nodes"]
        if isinstance(node, dict) and node.get("id") == metadata.get("route_node")
    ]
    evidence_record = marker.get("evidence")
    if not isinstance(evidence_record, dict):
        return None
    evidence_path = Path(str(evidence_record.get("path", "")))
    history_path = marker_path.parent / f"{metadata.get('route_node', '')}.{marker.get('sequence', 0)}.json"
    dependency_paths = [route_path, evidence_path, history_path]
    if "retained_review" in marker:
        try:
            from retained_review_completion import verify_marker
            recovery = verify_marker(route, nodes[0], marker, marker_path)
            record = marker["retained_review"]
            dependency_paths.extend(Path(e["path"]) for e in recovery["dependencies"])
            dependency_paths.append(Path(record["jobs"]))
        except (IndexError, KeyError, OSError, TypeError, ValueError):
            return None
    if "reviewer_disposition" in marker:
        try:
            _, origin_path = review_self_disposition_origin(route, nodes[0], marker, marker_path)
            dependency_paths.append(origin_path)
            disposition = marker["reviewer_disposition"]
            if disposition.get("settled_basis") == "historical-provenance-blocked":
                authority_basis = disposition["authority_basis"]
                dependency_paths.extend(Path(authority_basis[key]) for key in
                                        ("report_path", "request_path", "cycle_record_path", "cycle_binding_path"))
                request = json.loads(Path(authority_basis["request_path"]).read_text())
                dependency_paths.extend(Path(path) for path in request["command"]["inputs"])
            jobs = Path(disposition["jobs"])
            dependency_paths.append(jobs)
            from dispatch_replacement import source_binding, _record_path, _directory
            bound_rows = []
            for line in jobs.read_text().splitlines():
                fields = line.split("\t")
                if len(fields) != 6:
                    continue
                owner = parse_registry_metadata(fields[5])
                bound_rows.append(owner)
            subject = next((row for row in bound_rows if row.get("attempt_id") == attempt_id), None)
            if subject is None:
                raise ValueError("review-self-disposition-row-not-exact")
            dependency_paths.append(Path(subject["log_file"]))
            lineage = review_self_disposition_lineage(route, jobs, subject)
            for owner in bound_rows:
                if owner.get("attempt_id") not in lineage:
                    continue
                dependency_paths.append(Path(owner["owner_route_file"]))
                binding = source_binding(jobs, owner)
                if binding:
                    dependency_paths.append(_record_path(jobs, binding[0]))
                    if owner.get("replacement_original_attempt_id"):
                        dependency_paths.append(_directory(jobs) / "by-source" / (owner["attempt_id"] + ".json"))
            claim = marker["reviewer_disposition"]["original_claim"]
            if claim.get("source") == "runtime-command":
                dependency_paths.append(Path(claim["log_file"]))
        except (IndexError, KeyError, OSError, TypeError, ValueError):
            return None
    if marker.get("stage_authority") == "owner-chain":
        dependency_paths.append(Path(str(marker.get("subsession_manifest", ""))))
    elif nodes and nodes[0].get("kind") != "resource-runner":
        safe_attempt = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in attempt_id
        )
        dependency_paths.append(
            marker_path.parent
            / f"{metadata.get('route_node', '')}.{safe_attempt}.attempt.json"
        )
    if "retained_review" in marker:
        dependency_paths = list(dict.fromkeys(dependency_paths))
    dependency_bytes: list[tuple[Path, bytes]] = []
    try:
        for dependency_path in dependency_paths:
            if not dependency_path.is_absolute():
                return None
            dependency_bytes.append((dependency_path, dependency_path.read_bytes()))
    except OSError:
        return None
    if dependency_bytes[0][1] != route_bytes:
        return None
    if len(nodes) != 1 or not completion_marker_is_current(
        route, nodes[0], marker_path, marker
    ):
        return None
    try:
        if marker_path.read_bytes() != marker_bytes:
            return None
        if any(path.read_bytes() != raw for path, raw in dependency_bytes):
            return None
    except OSError:
        return None
    return MarkerBoundCompletionProof(
        marker=marker,
        marker_path=marker_path,
        marker_digest=hashlib.sha256(marker_bytes).hexdigest(),
        route_id=metadata.get("route_id", ""),
        route_hash=metadata.get("route_hash", ""),
        node_id=metadata.get("route_node", ""),
        attempt_id=attempt_id,
        immutable_file_digests=tuple(
            (str(path), hashlib.sha256(raw).hexdigest())
            for path, raw in dependency_bytes
        ),
    )


def _marker_bound_current_marker(
    metadata: dict[str, str],
    attempt_id: str,
    proof: MarkerBoundCompletionProof | None,
) -> tuple[dict[str, object] | None, str]:
    """CAS only current marker bytes against a pre-lock immutable-chain proof."""

    if proof is None or (
        metadata.get("completion_marker") != str(proof.marker_path)
        or metadata.get("route_id") != proof.route_id
        or metadata.get("route_hash") != proof.route_hash
        or metadata.get("route_node") != proof.node_id
        or attempt_id != proof.attempt_id
    ):
        return None, ""
    try:
        marker_bytes = proof.marker_path.read_bytes()
    except OSError:
        return None, ""
    digest = hashlib.sha256(marker_bytes).hexdigest()
    if digest != proof.marker_digest:
        return None, ""
    try:
        marker = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, ""
    if marker != proof.marker:
        return None, ""
    if "reviewer_disposition" in marker or "retained_review" in marker:
        try:
            if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
                   for path, digest in proof.immutable_file_digests):
                return None, ""
        except OSError:
            return None, ""
    return marker, digest


def _marker_bound_latest_rows(lines: list[str]) -> dict[str, tuple[int, list[str], str]]:
    latest: dict[str, tuple[int, list[str], str]] = {}
    for index, raw in enumerate(lines):
        fields = raw.split("\t")
        if len(fields) != 6:
            continue
        attempt_id = parse_registry_metadata(fields[5]).get("attempt_id", "")
        if attempt_id:
            if attempt_id in latest:
                raise DispatchContractError(
                    "attempt-row-not-unique", f"attempt_id={attempt_id} rows=2+"
                )
            latest[attempt_id] = (index, fields, raw)
    return latest


def marker_bound_delivery_transaction(
    jobs: Path,
    attempt_id: str,
    *,
    parent_attempt_id: str,
    expected_row_revision: str,
    expected_process_identity: tuple[tuple[str, str], ...],
    process_observation: ProcessQuiescence,
    advance: bool = True,
) -> MarkerBoundDeliveryResult:
    """Classify and optionally advance one marker-bound attempt under one lock.

    Process inspection belongs to the caller.  This transaction compares that
    pre-lock observation with the exact row generation and process identity,
    reads only the canonical registry and its immutable marker while locked,
    and performs at most one in-process registry replacement.
    """

    if not attempt_id or not parent_attempt_id:
        raise DispatchContractError("delivery-identity-incomplete")
    try:
        prelock_lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        prelock_lines = []
    prelock_rows = _marker_bound_latest_rows(prelock_lines)
    prelock_current = prelock_rows.get(attempt_id)
    marker_proof = None
    if prelock_current is not None:
        marker_proof = _marker_bound_prepare_marker_proof(
            parse_registry_metadata(prelock_current[1][5]), attempt_id
        )
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        latest = _marker_bound_latest_rows(lines)
        current = latest.get(attempt_id)
        if current is None:
            return MarkerBoundDeliveryResult(None, "", "", "", "", "", False, 0, False)

        index, fields, raw = current
        metadata = parse_registry_metadata(fields[5])
        revision = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        observation_current = bool(
            expected_row_revision
            and revision == expected_row_revision
            and marker_bound_process_identity(metadata) == expected_process_identity
        )

        def owned_open_count(rows: dict[str, tuple[int, list[str], str]]) -> int:
            count = 0
            for _order, child_fields, _raw in rows.values():
                child_metadata = parse_registry_metadata(child_fields[5])
                if (
                    child_metadata.get("parent_attempt_id") == parent_attempt_id
                    and child_metadata.get("registered_worker") == "1"
                    and child_fields[1] in {"open", "running"}
                ):
                    count += 1
            return count

        owned_children = owned_open_count(latest)
        marker, marker_digest = _marker_bound_current_marker(
            metadata, attempt_id, marker_proof
        )
        verdict = _marker_bound_row_verdict(metadata)
        quiescent = observation_current and process_observation.state == "quiescent"
        advanced = False
        import route_parent_close
        if (
            advance
            and observation_current
            and fields[1] in {"open", "running"}
            and not route_parent_close.row_requested(metadata, jobs)
            and marker is not None
            and quiescent
            and owned_children == 0
            and verdict in {"", "PASS"}
        ):
            fields[1] = "done"
            values = {
                "note": "completed-marker",
                "failure_class": "pass",
                "classifier_source": "marker-bound-delivery-v1",
            }
            values.update(_terminal_cleanup_values({**metadata, **values}))
            values.update(_delivery_intent_values(fields, {**metadata, **values}))
            fields[5] = _updated_attempt_metadata(
                fields[5], values, terminal=True,
            )
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            advanced = True

        # Re-read exactly once before returning.  A concurrent writer cannot
        # enter until this lock is released, and our own replace is the sole
        # allowed mutation in this scope.
        refreshed_lines = jobs.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        refreshed = _marker_bound_latest_rows(refreshed_lines)
        current = refreshed.get(attempt_id)
        if current is None:
            return MarkerBoundDeliveryResult(None, "", "", "", "", "", False, 0, advanced)
        _index, refreshed_fields, refreshed_raw = current
        refreshed_metadata = parse_registry_metadata(refreshed_fields[5])
        refreshed_marker, refreshed_marker_digest = _marker_bound_current_marker(
            refreshed_metadata, attempt_id, marker_proof
        )
        refreshed_revision = hashlib.sha256(
            refreshed_raw.encode("utf-8")
        ).hexdigest()
        return MarkerBoundDeliveryResult(
            marker=refreshed_marker,
            marker_digest=refreshed_marker_digest,
            row_revision=refreshed_revision,
            row_digest=refreshed_revision,
            status=refreshed_fields[1],
            verdict=_marker_bound_row_verdict(refreshed_metadata),
            quiescent=quiescent,
            owned_children=owned_open_count(refreshed),
            advanced=advanced,
            completion_proven=(
                _delivery_commit_proven(refreshed_metadata)
                # A transient publication closes with an attention receipt.
                # Recovery retains that receipt, but the verified marker now
                # proves the completed publication independently of delivery.
                or (deferred_completion(refreshed_metadata) == "completed"
                    and bool(refreshed_marker is not None and refreshed_marker_digest))
                if refreshed_metadata.get("delivery_intent") else
                # Read compatibility for rows predating the one-time receipt.
                # New terminal kinds use the common committed receipt, not
                # another producer-specific boolean in every carrier.
                bool(refreshed_marker is not None and refreshed_marker_digest)
                or (refreshed_metadata.get("note") == "completed-supervisor"
                    and refreshed_metadata.get("worker_type") == "owner"
                    and refreshed_metadata.get("dispatch_depth") == "1"
                    and refreshed_metadata.get("failure_class") == "pass")
                or (refreshed_fields[1] == "done"
                    and refreshed_metadata.get("note") == SUBSESSION_NOTE
                    and refreshed_metadata.get("failure_class") == "pass"
                    and refreshed_metadata.get("classifier_source") == SUBSESSION_TERMINAL_CLASSIFIER
                    and row_is_subsession(refreshed_metadata))
            ),
            terminal_conflict=terminal_conflict_pending(refreshed_metadata),
        )


def attempt_launch_is_available(jobs: Path, attempt_id: str) -> bool:
    """Return true only for one exact current open registered-only row."""

    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        matches = []
        for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((fields, metadata))
        if len(matches) != 1:
            return False
        fields, metadata = matches[0]
        try:
            validate_attempt_metadata(metadata)
        except DispatchContractError:
            return False
        return fields[1] == "open" and metadata.get("launch_claimed") == "0"


def mark_attempt_launch_started(jobs: Path, attempt_id: str, pid: int) -> None:
    """Let the exact launch fence durably attest before it executes payload."""

    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = []
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
        # A detached governed review stores the watchdog as the attempt PID.
        # Its payload fence is the separately sealed child, whose exact parent
        # must still be that live watchdog. Other launches retain the old tuple.
        fence = metadata
        if metadata.get("review_admission") == "prepared" and metadata.get("launch_lifecycle") == "detached":
            metadata = _parse_review_metadata(fields[5])
            if (metadata.get("worker_type") != "review"
                    or metadata.get("review_governed_lease") != REVIEW_GOVERNED_LEASE_KIND
                    or not REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(metadata.get("review_governed_lease_nonce", ""))
                    or not metadata.get("review_readiness_digest")
                    or not metadata.get("review_watchdog_budget_digest")
                    or pid != os.getpid()
                    or metadata.get("pid") != str(os.getppid())
                    or not process_identity_is_live(os.getppid(), metadata.get("pid_start", ""))
                    or exact_process_group_signal_authority(os.getppid(), metadata.get("pid_start", "")) != "authoritative"):
                raise DispatchContractError("attempt-launch-fence-identity-mismatch", attempt_id)
            parent = process_launch_identity(os.getppid())
            if any(metadata.get(key) != parent.get(key) for key in
                   ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")):
                raise DispatchContractError("attempt-launch-fence-identity-mismatch", attempt_id)
            current = process_launch_identity(pid)
            fence = {key: metadata.get("review_fence_" + key, "") for key in
                     ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")}
            if any(not value or current.get(key) != value for key, value in fence.items()):
                raise DispatchContractError("attempt-launch-fence-identity-mismatch", attempt_id)
        expected_start = fence.get("pid_start", "")
        if (
            fields[1] not in {"open", "running"}
            or metadata.get("launch_claimed") != "1"
            or metadata.get("launch_fence") != "registry-v1"
            or fence.get("pid") != str(pid)
            or fence.get("pgid") != str(pid)
            or not expected_start
            or not process_identity_is_live(pid, expected_start)
            or exact_process_group_signal_authority(pid, expected_start)
            != "authoritative"
        ):
            raise DispatchContractError(
                "attempt-launch-fence-identity-mismatch", attempt_id
            )
        fields[5] = _updated_attempt_metadata(
            fields[5], {"launch_started": "1"}
        )
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)


def recover_unstarted_attempt(jobs: Path, attempt_id: str) -> bool:
    """Reset only a dead registry-v1 fence that never authorized payload exec."""

    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((index, fields, metadata))
        if len(matches) != 1:
            return False
        index, fields, metadata = matches[0]
        try:
            validate_attempt_metadata(metadata)
        except DispatchContractError:
            return False
        if (
            fields[1] != "open"
            or metadata.get("launch_claimed") != "1"
            or metadata.get("launch_fence") != "registry-v1"
            or metadata.get("launch_started") == "1"
            or metadata.get("launch_outcome")
        ):
            return False
        process = attempt_process_quiescence(metadata)
        if process.state != "quiescent":
            return False
        remove = {
            *_PROCESS_IDENTITY_METADATA_KEYS,
            *LAUNCH_CGROUP_METADATA_KEYS,
            *LAUNCH_LINEAGE_METADATA_KEYS,
            "launch_claimed",
            "launch_lifecycle",
            "launch_started",
        }
        parts = [
            part for part in fields[5].split(",")
            if part.split("=", 1)[0] not in remove
        ]
        parts.append("launch_claimed=0")
        fields[5] = ",".join(parts)
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return True
