"""Dispatch contract: attempt rows after launch.

Retry admission, closing and annotating attempt rows, terminal conflicts,
orphan and reap watches, refused-chain closes, the supervision probe, local
registry reconciliation, and attempt cleanup resolution. `claim_attempt_row`
stays in `dispatch_contract.py` because it checks the terminal claim.

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
import stat
import subprocess
import sys
from typing import Callable, Mapping
from dispatch_attempt_policy import (
    decide_attempt, SUCCESS_NOTES, committed_outcome, terminal_conflict_identity,
    terminal_conflicts, terminal_conflict_pending,
)
from route_authority import RELAUNCH_STABLE_KEYS as _RELAUNCH_STABLE_KEYS

from dispatch_contract_base import (
    _cancellation_quiescence_receipt_record, _delivery_intent_values,
    _has_persisted_process_identity, _MODULE_ROOT, _updated_attempt_metadata,
    ATTEMPT_DESCENDANT_PROOF, GROUP_REAP_PROOF, attempt_row_never_started, DispatchContractError, end_with_test,
    foreground_review_eligible, governance_environment, parse_registry_metadata,
    row_has_attempt, SealedForegroundOutcome,
)

from dispatch_contract_roots import (
    ensure_global_registry_writable,
)

from dispatch_contract_policy import (
    validate_attempt_metadata,
)

from dispatch_contract_process import (
    _canonical_sha256, _cleanup_receipt_reason, _declared_supervisor_lease_metadata,
    _detached_group_drain_receipt, _supervisor_lease_payload_matches, _tagged_residue_receipt,
    _terminal_cleanup_values, _validated_supervisor_lease_path, attempt_process_quiescence,
    attempt_scan_namespace_authority, attempt_tagged_descendants, CLEANUP_RECEIPT_TYPE,
    process_group_observation, process_namespace_identity, process_start_ticks, process_observation,
    prove_attempt_quiescence,
)

from dispatch_contract_registry import (
    _append_registry_rows, _atomic_registry_replace, _foreground_outcome_values_from_pipe,
    _foreground_row, attempt_raw_row_sha256,
)

from dispatch_contract_delivery import (
    SUBSESSION_CHAIN_REFUSAL_CLASSIFIER,
)


def resolve_attempt_cleanup(jobs: Path, attempt_id: str, *, apply: bool = False) -> dict[str, object]:
    try:
        return _resolve_attempt_cleanup(Path(jobs), attempt_id, apply=apply)
    except (OSError, ValueError, DispatchContractError) as error:
        return {"attempt_id": attempt_id, "settled": False, "changed": False,
                "reason": getattr(error, "reason", "cleanup-observation-unavailable")}


def _resolve_attempt_cleanup(jobs: Path, attempt_id: str, *, apply: bool = False) -> dict[str, object]:
    """Reconcile cleanup on an exact terminal row, preserving its committed result.

    The runtime join and operator reconcile use this same bounded cleanup
    authority. A missing proof remains an obligation; no cancellation or retry
    receipt is issued. Process observations happen outside the registry lock.
    """
    matches = [line.split("\t") for line in jobs.read_text(encoding="utf-8").splitlines()
               if len(line.split("\t")) == 6
               and row_has_attempt(line.split("\t")[5], attempt_id)]
    result = {"attempt_id": attempt_id, "settled": False, "changed": False}
    if len(matches) != 1:
        return {**result, "reason": "cleanup-row-not-unique"}
    fields = matches[0]
    metadata = parse_registry_metadata(fields[5])
    if fields[1] not in {"done", "killed", "cancelled"}:
        return {**result, "reason": "cleanup-terminal-result-required"}
    validate_attempt_metadata(metadata)
    process = attempt_process_quiescence(metadata, terminal_receipt=True)
    if apply and process.state == "live":
        from route_parent_close import reap_terminal_descendants
        reap_terminal_descendants(jobs, fields)
        process = attempt_process_quiescence(metadata, terminal_receipt=True)
    # A namespace-extinct verdict is quiescent only from a host-like observer.
    # Still seal the existing cleanup receipt when its proof holds, so a later
    # sandboxed reader of this row sees the same settled state.
    if process.state == "quiescent" and (
            _cleanup_receipt_reason(metadata) or _detached_group_drain_receipt(metadata)):
        return {**result, "settled": True, "reason": process.reason}
    if process.state == "live":
        return {**result, "reason": process.reason}
    proof = prove_attempt_quiescence(metadata, max_wait_seconds=0, allow_namespace_extinct=True)
    if not proof.proven:
        if process.state == "quiescent":
            return {**result, "settled": True, "reason": process.reason}
        return {**result, "reason": process.reason,
                "group_state": proof.process_group_state, "descendant_state": proof.descendant_state,
                "namespace_authority": proof.namespace_authority}
    record = _cancellation_quiescence_receipt_record(metadata, proof)
    record["receipt_type"] = CLEANUP_RECEIPT_TYPE
    digest = _canonical_sha256(record)
    if not apply:
        return {**result, "reason": "cleanup-proof-available", "proof_source": proof.source}
    values = {"cleanup_receipt_b64": base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode(),
              "cleanup_receipt_digest": digest}
    changed = annotate_attempt_row_if(
        jobs, attempt_id, values, lambda fresh: fresh == fields,
        statuses=frozenset({"done", "killed", "cancelled"}),
    )
    return {**result, "settled": changed, "changed": changed,
            "reason": "attempt-cleanup-proven" if changed else "cleanup-row-changed",
            "proof_source": proof.source, "receipt_digest": digest if changed else ""}


def resolve_residue_drain(jobs: Path, attempt_id: str, *, apply: bool = False) -> dict[str, object]:
    """Repeat the existing drain observation after residue or a lost reap watcher."""
    try:
        return _resolve_residue_drain(Path(jobs), attempt_id, apply=apply)
    except (OSError, ValueError, DispatchContractError) as error:
        return {"attempt_id": attempt_id, "state": "unverifiable", "changed": False,
                "live_pids": [], "reason": getattr(error, "reason", "residue-observation-unavailable")}


def _resolve_residue_drain(jobs: Path, attempt_id: str, *, apply: bool = False) -> dict[str, object]:
    """Repeat the watcher's clean drain for residue or a lost watcher.

    The watcher seals ``attempt-tagged-residue-v1`` and exits, so nobody sees
    the survivors leave. This makes the same group and tagged-descendant
    observation from the recording namespace and, only when both are empty,
    writes the fields the watcher itself writes for a clean drain. The same
    observation repairs a detached attempt whose watcher exited before its
    receipt: prove the recorded watcher is gone before scanning the full group
    and tagged descendants. No new evidence kind is created; history stays.
    Observations run
    outside the registry lock; the write is one compare-and-set.
    """
    matches = [line.split("\t") for line in jobs.read_text(encoding="utf-8").splitlines()
               if len(line.split("\t")) == 6
               and row_has_attempt(line.split("\t")[5], attempt_id)]
    result: dict[str, object] = {"attempt_id": attempt_id, "state": "not-applicable",
                                 "changed": False, "live_pids": []}
    if len(matches) != 1:
        return {**result, "reason": "residue-row-not-unique"}
    fields = matches[0]
    metadata = parse_registry_metadata(fields[5])
    if fields[1] not in {"open", "running"}:
        return {**result, "reason": "residue-open-row-required"}
    validate_attempt_metadata(metadata)
    residue = _tagged_residue_receipt(metadata)
    lost_watcher = (metadata.get("registered_worker") == "1"
                    and metadata.get("pid_scope") == "namespace-local"
                    and metadata.get("launch_lifecycle") == "detached"
                    and metadata.get("reap_watch") == "post-exit"
                    and str(metadata.get("reap_watch_pid", "")).isdecimal()
                    and int(metadata["reap_watch_pid"]) > 0
                    and not _detached_group_drain_receipt(metadata))
    if not residue and not lost_watcher:
        return {**result, "reason": "residue-receipt-absent"}
    if (process_namespace_identity() != metadata.get("pid_observer_ns")
            or (not residue and metadata.get("pid_ns") != metadata.get("pid_observer_ns"))
            or not attempt_scan_namespace_authority(metadata)):
        return {**result, "state": "unverifiable", "reason": "residue-observer-namespace-mismatch"}
    pid, pgid, start = metadata.get("pid", ""), metadata.get("pgid", ""), metadata.get("pid_start", "")
    if not (pid.isdecimal() and int(pid) > 0 and pgid == pid and start):
        return {**result, "state": "unverifiable", "reason": "residue-identity-invalid"}
    if residue:
        if process_start_ticks(int(pid)) == start:
            return {**result, "state": "live", "reason": "governed-leader-live"}
    else:
        visibility, observed_start, process_state = process_observation(int(pid))
        if visibility == "inaccessible":
            return {**result, "state": "unverifiable", "reason": "governed-leader-unverifiable"}
        if visibility == "present" and observed_start == start and process_state != "Z":
            return {**result, "state": "live", "reason": "governed-leader-live"}
    if lost_watcher and not residue:
        watcher_visibility, _watcher_start, watcher_state = process_observation(int(metadata["reap_watch_pid"]))
        if watcher_visibility == "inaccessible":
            return {**result, "state": "unverifiable", "reason": "reap-watcher-unverifiable"}
        if watcher_visibility == "present" and watcher_state != "Z":
            return {**result, "state": "live", "reason": "reap-watcher-live"}
    group = process_group_observation(int(pgid))
    descendants = attempt_tagged_descendants(metadata)
    if "unverifiable" in {group.state, descendants.state}:
        return {**result, "state": "unverifiable",
                "reason": "residue-scan-unverifiable:" + (group.reason or descendants.reason)}
    if descendants.state == "populated":
        # Display pids come from the tag scan only: a reused pgid must not
        # present an unrelated process as something the worker left behind.
        return {**result, "state": "residue-live", "reason": "attempt-descendant-live",
                "live_pids": [member[0] for member in descendants.members][:16]}
    if group.state == "populated":
        return {**result, "state": "live", "reason": "governed-group-live"}
    values = {"attempt_descendant_proof": ATTEMPT_DESCENDANT_PROOF,
              "attempt_descendant_observer_ns": metadata["pid_observer_ns"]}
    if not residue:
        values.update(launch_outcome="governed-process-group-drained",
                      group_reap_proof=GROUP_REAP_PROOF, group_reap_pgid=pgid)
        values.update(_terminal_cleanup_values({**metadata, **values}))
    if not apply:
        return {**result, "state": "drained", "reason": "residue-drain-proof-available"}
    changed = annotate_attempt_row_if(jobs, attempt_id, values, lambda fresh: fresh == fields)
    return {**result, "state": "drained", "changed": changed,
            "reason": ("residue-drain-refreshed" if residue else "post-exit-drain-refreshed")
                      if changed else "residue-row-changed"}


def _retry_route_related(source, candidate) -> bool:
    """Same route, or the source's route is a verified lineage ancestor of the candidate's.

    The relation answer only (``verified_route_lineage``); an unreadable or
    unverified lineage is "not related", which stays the existing refusal.
    """
    if source.get("route_id") == candidate.get("route_id"):
        return True
    if not source.get("route_id"):
        return False
    route_file = candidate.get("route_file")
    if not route_file or not all((source.get("route_hash"), candidate.get("route_id"), candidate.get("route_hash"))):
        return False
    from route_lineage import RouteLineageError, verified_route_lineage
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        if (route.get("route_id"), route.get("route_hash")) != (candidate.get("route_id"), candidate.get("route_hash")):
            return False
        lineage = verified_route_lineage(route, observe=True)
    except (OSError, ValueError, RouteLineageError, AttributeError):
        return False
    return any((item.get("route_id"), item.get("route_hash")) == (source["route_id"], source["route_hash"])
               for item in lineage[1:])


def _retry_parent_matches(source, candidate, jobs, lines) -> bool:
    """An existing owner replacement may continue its predecessor's child.

    The parent follows the replacement lineage (``parent_attempt_succeeded``)
    on one route; across a verified continuation route each parent owner is
    bound to its own route. Launch admission prepares these lineage and route
    observations outside the registry lock, then pins their exact dependency
    rows at commit.
    """
    previous, current = source.get("parent_attempt_id"), candidate.get("parent_attempt_id")
    if previous == current:
        return True
    if not jobs or not previous or not current:
        return False
    if any(row.get("dispatch_depth") != "2" for row in (source, candidate)):
        return False
    routes = ((source.get("route_id"), source.get("route_hash")),
              (candidate.get("route_id"), candidate.get("route_hash")))
    if not all(routes[0]) or not all(routes[1]) or not source.get("route_node"):
        return False
    from dispatch_replacement import _rows
    from owner_route_binding import OwnerRouteBindingError, resolve_owner_route_lifecycle
    from route_authority import parent_attempt_succeeded
    rows = _rows(lines)
    try:
        for parent, route in zip((previous, current), routes):
            owner = rows.get(parent, (None, {}))[1]
            if (owner.get("worker_type") != "owner" or owner.get("dispatch_depth") != "1"
                    or owner.get("registered_worker") != "1"):
                return False
            binding, _ = resolve_owner_route_lifecycle(
                jobs, owner_attempt_id=parent, registry_rows=list(rows.values()))
            if binding is None or (binding.route_id, binding.route_hash) != route:
                return False
    except (OSError, OwnerRouteBindingError):
        return False
    # A pin-handoff owner is a new owner of the continuation route, not a
    # replacement of the previous one; only one route keeps the lineage walk.
    if routes[0][0] != routes[1][0]:
        return True
    return routes[0] == routes[1] and parent_attempt_succeeded(jobs, previous, current)


def _retry_registration_census(lines, metadata):
    """Fresh row-only retry/competing-claim check; process proof is prepared."""
    prior = metadata.get("automatic_retry_of")
    if not prior:
        return
    predecessors = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        candidate = parse_registry_metadata(fields[5])
        if (candidate.get("automatic_retry_of") == prior
                and candidate.get("attempt_id") != metadata.get("attempt_id")
                and (candidate.get("launch_claimed") == "1" or fields[1] not in {"open", "running"})):
            raise DispatchContractError("retry-already-claimed",
                                        f"responsible_attempt={candidate.get('attempt_id')}")
        if candidate.get("attempt_id") == prior:
            predecessors.append(candidate)
    if len(predecessors) != 1:
        raise DispatchContractError("retry-predecessor-ambiguous" if predecessors
                                    else "retry-predecessor-missing", prior)


def _automatic_retry_admission(lines: list[str], metadata: dict[str, str], *, jobs: Path | None = None) -> None:
    """The jobs lock arbitrates retries against the committed predecessor.

    Observers propose a predecessor, never permission. Distinct consumers of
    one failure converge on one successor; explicit review rounds have no
    automatic_retry_of binding and keep their separate workflow semantics.
    """
    prior = metadata.get("automatic_retry_of")
    if not prior:
        return
    predecessor = None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        candidate = parse_registry_metadata(fields[5])
        if (candidate.get("automatic_retry_of") == prior
                and candidate.get("attempt_id") != metadata.get("attempt_id")
                and (candidate.get("launch_claimed") == "1" or fields[1] not in {"open", "running"})):
            raise DispatchContractError("retry-already-claimed",
                                        f"responsible_attempt={candidate.get('attempt_id')}")
        if candidate.get("attempt_id") == prior:
            if predecessor is not None:
                raise DispatchContractError("retry-predecessor-ambiguous", prior)
            predecessor = (fields[1], candidate)
    if predecessor is None:
        raise DispatchContractError("retry-predecessor-missing", prior)
    status, source = predecessor
    if source.get("replacement_family_id"):
        if jobs is None:
            family = source.get("replacement_family_id")
            replacement = source.get("replacement_attempt_id")
        else:
            from dispatch_replacement import _check_record, _read, _record_path, source_binding
            binding = source_binding(jobs, source)
            family, replacement = (binding[0], binding[1]) if binding else (None, None)
            if family and replacement:
                record = _check_record(_read(_record_path(jobs, family)), family,
                                       {"replacement_claim_digest": binding[2] or None})
                if (record.get("original_attempt_id") != prior
                        or record.get("replacement_attempt_id") != replacement):
                    raise DispatchContractError("replacement-lineage-unproven", prior)
            elif source.get("replacement_original_attempt_id"):
                # The inbound family on a replacement row is not its outgoing
                # claim. Without the exact by-source reservation there is no
                # authorized successor to register.
                raise DispatchContractError("automatic-replacement-exhausted", prior)
            else:
                family = source.get("replacement_family_id")
                replacement = source.get("replacement_attempt_id")
        if (not family or replacement != metadata.get("attempt_id")
                or metadata.get("replacement_family_id") != family):
            raise DispatchContractError("automatic-replacement-exhausted", prior)
        return
    if (source.get("route_node") != metadata.get("route_node")
            or not _retry_route_related(source, metadata)
            or not _retry_parent_matches(source, metadata, jobs, lines)):
        raise DispatchContractError("retry-predecessor-binding-mismatch", prior)
    proof = attempt_process_quiescence(source, terminal_receipt=status in {"done", "killed", "cancelled"})
    decision = decide_attempt(status, source, process_state=proof.state, process_reason=proof.reason)
    if not decision.retry_allowed:
        raise DispatchContractError("retry-predecessor-not-retryable",
            f"attempt={prior} outcome={decision.outcome} responsible={decision.responsible} action={decision.action}")


def _never_launched_same_work(fields, metadata, row_fields, row_metadata) -> bool:
    # replacement_row already admitted this candidate against its source and
    # current pin. Its unstarted owner row may follow the selector to another
    # harness; every work/lineage axis still matches and started rows never move.
    keys = _RELAUNCH_STABLE_KEYS
    if metadata.get('worker_type') == 'owner' and metadata.get('replacement_original_attempt_id'):
        keys = tuple(key for key in keys if key != 'harness')
    return (fields[1] == "open" and metadata.get("launch_claimed") == "0"
            and metadata.get("launch_started") != "1" and not metadata.get("pid")
            and fields[2:5] == row_fields[2:5]
            and all(metadata.get(key, "") == row_metadata.get(key, "")
                    for key in keys))


def _review_source_registration_fence(jobs: Path, row_fields: list[str], metadata: dict[str, str], *,
                                      new_attempt: bool) -> tuple[list[str], dict[str, str]]:
    """Bind implementation-review source once and verify it at registration."""
    from review_source_subject import SourceSubjectError, has_review_subject, seal_attempt, validate_attempt
    require_binding = False
    try:
        if new_attempt and metadata.get("route_file") and metadata.get("route_node"):
            route_path = Path(metadata["route_file"])
            route = json.loads(route_path.read_text(encoding="utf-8"))
            node = next((item for item in route.get("nodes", [])
                         if item.get("id") == metadata.get("route_node")), None)
            reviewed_input = None
            if metadata.get("review_input_digest"):
                from review_input import read_binding as read_review_input_binding
                reviewed_input = read_review_input_binding(jobs, metadata)
            if node is not None and has_review_subject(route, node, jobs, reviewed_input):
                predecessor = (metadata.get("automatic_retry_of")
                               or metadata.get("replacement_original_attempt_id"))
                if not predecessor and metadata.get("capacity_retry") == "1":
                    predecessor = metadata.get("prior_attempt_id")
                values = seal_attempt(jobs, route, node, metadata["attempt_id"],
                                      worktree=row_fields[3], predecessor_attempt_id=predecessor,
                                      expected_subject_digest=os.environ.get(
                                          "AGENT_REVIEW_SOURCE_SUBJECT_DIGEST"),
                                      attempt_metadata=metadata)
                for key, value in values.items():
                    prior = metadata.get(key)
                    if prior and prior != value:
                        raise SourceSubjectError("review-source-binding-conflict", metadata["attempt_id"])
                    if not prior:
                        row_fields[5] += f",{key}={value}"
                        metadata[key] = value
                require_binding = bool(values)
        elif new_attempt and metadata.get("completion_gate") == "code-impl-review":
            raise SourceSubjectError("review-source-route-unavailable", metadata.get("route_node", ""))
        validate_attempt(jobs, row_fields, metadata, require_new=require_binding)
    except SourceSubjectError as exc:
        raise DispatchContractError(exc.reason, exc.detail) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise DispatchContractError("review-source-route-unavailable", str(exc)) from exc
    return row_fields, metadata


def _row_identity(fields: list[str]) -> tuple[str, ...] | None:
    if len(fields) != 6:
        return None
    metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
    if metadata.get("attempt_id"):
        return ("attempt", metadata["attempt_id"])
    route_id = metadata.get("route_id")
    route_node = metadata.get("route_node")
    parent = metadata.get("parent")
    if route_id and route_node and parent:
        return ("legacy", route_id, route_node, parent, fields[4])
    return None


def _settlement_metadata_pipe(pipe: str, values: dict[str, str]) -> str:
    """Retire the known duplicated provenance without repairing authority seals."""
    parts = pipe.split(",")
    sources = [part for part in parts if part.startswith("model_source=")]
    if (values.get("note", "").startswith("dead-")
            and len(sources) > 1 and len(set(sources)) == 1):
        seen = False
        retained = []
        for part in parts:
            if part.startswith("model_source="):
                if seen:
                    continue
                seen = True
            retained.append(part)
        pipe = ",".join(retained)
    return pipe


def close_attempt_row(
    jobs: Path,
    attempt_id: str,
    note: str,
    *,
    evidence: dict[str, str] | None = None,
) -> bool:
    """Close one exact SD-49 attempt atomically and idempotently."""
    if not attempt_id or not note:
        raise DispatchContractError("attempt-close-invalid", "attempt_id and note are required")
    ensure_global_registry_writable(jobs)
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in {"open", "running"}:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") != attempt_id:
                continue
            validate_attempt_metadata(metadata)
            import route_parent_close
            if route_parent_close.row_requested(metadata, jobs) and note != "cancelled-by-parent":
                return False
            if metadata.get("teardown_claim"):
                return False
            fields[1] = "done"
            values = {"note": note}
            values.update({
                key: value for key, value in (evidence or {}).items()
                if value not in (None, "")
            })
            values.update(_terminal_cleanup_values({**metadata, **values}))
            values.update(_delivery_intent_values(fields, {**metadata, **values}))
            try:
                fields[5] = _updated_attempt_metadata(
                    _settlement_metadata_pipe(fields[5], values), values, terminal=True
                )
            except DispatchContractError:
                return False
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return True
    return False


def attempt_launch_state(
    jobs: Path,
    attempt_id: str,
    *,
    claimed: bool,
    action: str,
) -> str:
    """Return the typed launch receipt state for one exact attempt."""
    if action == "dry-run":
        return "preview-only"
    if claimed:
        return "claimed"
    return existing_attempt_launch_state(jobs, attempt_id)[0]


def existing_attempt_launch_state(jobs: Path, attempt_id: str) -> tuple[str, str]:
    """``(state, reason)`` for an attempt whose launch was claimed before this call.

    An open row is not proof of a running worker: its launcher can be stopped
    with the worker (5th Codex run), leaving the row open. The shared verdict
    decides -- ``live`` is ``existing-active``, ``quiescent`` is
    ``existing-dead``, anything this observer cannot see (or an observation
    error) is ``existing-unverified``. A closed row is ``existing-completed``
    and a missing one ``existing-unknown``. Read-only; never raises.
    """
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "existing-unknown", "registry-unreadable"
    seen = False
    open_metadata: dict[str, str] | None = None
    open_fields: list[str] | None = None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") != attempt_id:
            continue
        seen = True
        if fields[1] in {"open", "running"}:
            open_metadata = metadata
            open_fields = fields
    if open_metadata is None:
        if seen:
            return "existing-completed", "attempt-closed"
        return "existing-unknown", "attempt-row-missing"
    try:
        from recovery_evidence import observe_attempt
        evidence = observe_attempt(open_fields[1], open_metadata, registry_rows=lines,
                                   repo=open_fields[2], worktree=open_fields[3])
        process = evidence.process
    except Exception:  # noqa: BLE001 -- a launch receipt must not fail on observation
        return "existing-unverified", "process-observation-failed"
    if process.state == "live":
        return "existing-active", process.reason or "process-live"
    if evidence.result_state == "unverifiable":
        return "existing-unverified", "terminal-evidence-unverifiable"
    if process.state == "quiescent":
        return "existing-dead", process.reason or "process-gone"
    return "existing-unverified", process.reason or "process-unverifiable"


def resolve_terminal_conflict(
    jobs: Path, attempt_id: str, *, expected_row_sha256: str, review: Path,
) -> dict[str, str]:
    """Record an explicit review of an exact conflict, preserving its result.

    Ordinary conflict review never selects a different verdict. A legacy
    non-PASS publication instead uses the evidence-bound retained-review
    recovery, preserving both original judgments. Changed or live evidence
    invalidates either disposition.
    """
    import retained_review_completion as retained
    if retained.candidate(jobs, attempt_id):
        return retained.apply(jobs, attempt_id, expected_row_sha256=expected_row_sha256, review=review)
    review_bytes = review.read_bytes()
    if not review.is_absolute() or review.is_symlink() or not review_bytes.strip():
        raise DispatchContractError("terminal-conflict-review-required")
    lines = jobs.read_text(encoding="utf-8").splitlines()
    matches = [(i, line.split("\t")) for i, line in enumerate(lines)
               if len(line.split("\t")) == 6 and row_has_attempt(line.split("\t")[5], attempt_id)]
    if len(matches) != 1:
        raise DispatchContractError("attempt-terminal-row-not-unique")
    _, fields = matches[0]
    if fields[1] not in {"done", "killed", "cancelled"}:
        raise DispatchContractError("terminal-conflict-not-terminal")
    metadata = parse_registry_metadata(fields[5])
    proof = attempt_process_quiescence(metadata, terminal_receipt=fields[1] in {"done", "killed", "cancelled"})
    if proof.state != "quiescent":
        raise DispatchContractError("terminal-conflict-process-unsettled", proof.reason)
    if not terminal_conflict_pending(metadata):
        raise DispatchContractError("terminal-conflict-not-pending")
    conflicts = terminal_conflicts(metadata)
    for entry in conflicts.values():
        if not entry.get("review_sha256"):
            entry.update(review_sha256=hashlib.sha256(review_bytes).hexdigest(),
                         review_path_b64=base64.b64encode(str(review).encode()).decode())
    values = {"terminal_conflicts_b64": base64.b64encode(json.dumps(conflicts, sort_keys=True).encode()).decode()}
    expected = "\t".join(fields)
    if hashlib.sha256(expected.encode()).hexdigest() != expected_row_sha256:
        raise DispatchContractError("terminal-conflict-row-changed")
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fresh = jobs.read_text(encoding="utf-8").splitlines()
        matches = [i for i, line in enumerate(fresh) if len(line.split("\t")) == 6
                   and row_has_attempt(line.split("\t")[5], attempt_id)]
        if len(matches) != 1 or fresh[matches[0]] != expected:
            raise DispatchContractError("terminal-conflict-row-changed")
        # A resolution is diagnostic metadata, never a second terminal commit.
        fields[5] = _updated_attempt_metadata(fields[5], values, terminal=True)
        fresh[matches[0]] = "\t".join(fields)
        _atomic_registry_replace(jobs, fresh)
    return values


def is_runtime_owner_launch(args) -> bool:
    """Whether this launch is a registered depth-1 route owner."""
    bound = getattr(args, "owner_route_binding", None) or getattr(args, "route_file", None)
    return bool(getattr(args, "worker_type", None) == "owner" and bound
                and int(getattr(args, "dispatch_depth", 0)) == 1)


def workflow_completion_receipt(args) -> str:
    """Seal who finishes a newly launched route owner, across all adapters."""
    if is_runtime_owner_launch(args):
        return ",workflow_completion=runtime-v1"
    return ""


def reconcile_attempt_terminal(
    jobs: Path,
    attempt_id: str,
    note: str,
    *,
    evidence: dict[str, str] | None = None,
) -> str:
    """One terminal commit, with conflicts handed back outside the jobs lock."""
    outcome = _commit_attempt_terminal(jobs, attempt_id, note, evidence=evidence)
    if outcome == "terminal-conflict":
        from dispatch_supervision import materialize
        try:
            materialize(Path(jobs), {attempt_id}, reason="terminal-evidence-conflict")
        except (OSError, ValueError, RuntimeError) as exc:
            # The conflict is already durable. Keep the publication failure
            # visible and retryable rather than reverting the committed result.
            from dispatch_completion_join import log_delivery_refusal
            log_delivery_refusal(Path(jobs), attempt_id, "terminal-conflict-delivery-pending")
    return outcome


def _commit_attempt_terminal(
    jobs: Path,
    attempt_id: str,
    note: str,
    *,
    evidence: dict[str, str] | None = None,
) -> str:
    """Atomically close one supervisor-owned attempt or prove it already closed.

    Unlike a best-effort close, a missing/duplicate exact row is a typed
    contract failure.  This lets supervisors avoid reporting successful
    completion while their canonical row remains open.
    """

    if not attempt_id or not note:
        raise DispatchContractError(
            "attempt-terminal-reconcile-invalid",
            "attempt_id and note are required",
        )
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
                "attempt-terminal-row-not-unique",
                f"attempt_id={attempt_id} rows={len(matches)}",
            )
        index, fields, metadata = matches[0]
        validate_attempt_metadata(metadata)
        import route_parent_close
        if route_parent_close.row_requested(metadata, jobs):
            # Intent first wins; post-exit PASS/FAIL is a late observation,
            # not a contradictory terminal result requiring another review.
            return "already-terminal" if fields[1] not in {"open", "running"} else "cancellation-pending"
        if fields[1] in {"done", "killed", "cancelled"}:
            incoming = evidence or {}
            prior_class = metadata.get("failure_class", "")
            next_class = str(incoming.get("failure_class", ""))
            prior_outcome = committed_outcome(fields[1], metadata)
            next_outcome = committed_outcome("done", {"note": note, **incoming})
            if (prior_outcome == next_outcome
                    and (not prior_class or not next_class or prior_class == next_class)):
                return "already-terminal"
            # A classifier is an evidence producer, not an authority that can
            # rewrite another committed result (and its immutable receipt).
            values = {
                "terminal_conflict": "1",
                "prior_terminal_note": metadata.get("note", ""),
                "prior_classifier_source": metadata.get("classifier_source", ""),
                "prior_failure_class": prior_class,
                "conflicting_classifier_source": str(incoming.get("classifier_source", "")),
                "conflicting_failure_class": next_class,
                "conflicting_terminal_note": note,
            }
            conflicts = terminal_conflicts(metadata)
            conflicts.setdefault(terminal_conflict_identity({**metadata, **values}), {
                "note": note, "failure_class": next_class,
                "classifier_source": str(incoming.get("classifier_source", "")),
            })
            values["terminal_conflicts_b64"] = base64.b64encode(json.dumps(conflicts, sort_keys=True).encode()).decode()
            fields[5] = _updated_attempt_metadata(fields[5], values, terminal=True)
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return "terminal-conflict" if terminal_conflict_pending({**metadata, **values}) else "already-terminal"
        if fields[1] not in {"open", "running"}:
            raise DispatchContractError(
                "attempt-terminal-status-invalid", fields[1]
            )
        if metadata.get("teardown_claim"):
            raise DispatchContractError(
                "attempt-terminal-teardown-claimed", attempt_id
            )
        values = {"note": note}
        values.update(
            {
                key: value
                for key, value in (evidence or {}).items()
                if value not in (None, "")
            }
        )
        values.update(_terminal_cleanup_values({**metadata, **values}))
        values.update(_delivery_intent_values(fields, {**metadata, **values}))
        fields[1] = "done"
        fields[5] = _updated_attempt_metadata(fields[5], values, terminal=True)
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return "closed"


def launch_orphan_watch(
    jobs: Path,
    agent_home: Path,
    attempt_id: str,
    pid: int,
    pid_start: str,
) -> int:
    """Start one exact post-exit owner watcher outside the model governor.

    The watcher is deterministic infrastructure, not a model worker. It only
    waits for the recorded PID/start identity to end and then asks the shared
    registry classifier to close a true orphan. A waiting adopted continuation
    retains its children and goes through its existing normal start after join.
    """
    if not attempt_id or pid <= 0 or not pid_start:
        raise DispatchContractError(
            "orphan-watch-identity-invalid",
            "attempt_id, pid, and pid_start are required",
        )
    script = _MODULE_ROOT / "utilities" / "dispatch-orphan-watch.py"
    try:
        proc = subprocess.Popen(
            [
                sys.executable, str(script),
                "--jobs", str(Path(jobs).resolve()),
                "--agent-home", str(Path(agent_home).resolve()),
                "--attempt-id", attempt_id,
                "--pid", str(pid),
                "--pid-start", str(pid_start),
            ],
            cwd="/",
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DispatchContractError("orphan-watch-launch-failed", str(exc)) from exc
    end_with_test(proc)
    return proc.pid


def launch_reap_watch(
    jobs: Path,
    attempt_id: str,
    pid: int,
    pid_start: str,
    pgid: int,
    *,
    foreground_seal: SealedForegroundOutcome | None = None,
) -> int:
    """Start the exact detached-process drain observer in the launch namespace."""

    if not attempt_id or pid <= 0 or pgid != pid or not pid_start:
        raise DispatchContractError(
            "reap-watch-identity-invalid",
            "attempt_id, leader pid/start, and leader pgid are required",
        )
    jobs = Path(jobs)
    if foreground_seal is not None:
        if not isinstance(foreground_seal, SealedForegroundOutcome):
            raise DispatchContractError(
                "reap-watch-foreground-seal-invalid", "type"
            )
        try:
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise DispatchContractError(
                "reap-watch-foreground-seal-invalid", "registry"
            ) from exc
        matches = _foreground_row(lines, attempt_id)
        if len(matches) != 1:
            raise DispatchContractError(
                "reap-watch-foreground-seal-invalid", f"rows={len(matches)}"
            )
        _index, fields, metadata = matches[0]
        if fields[1] not in {"open", "running"} or not foreground_review_eligible(
            metadata,
            expected_attempt_id=attempt_id,
            expected_pid=pid,
            expected_pid_start=pid_start,
            expected_pgid=pgid,
        ):
            raise DispatchContractError(
                "reap-watch-foreground-seal-invalid", "binding-or-eligibility"
            )
        try:
            values = _foreground_outcome_values_from_pipe(fields[5])
        except DispatchContractError as exc:
            raise DispatchContractError(
                "reap-watch-foreground-seal-invalid", exc.detail
            ) from exc
        if (
            values is None
            or dict(foreground_seal.values) != values
            or foreground_seal.attempt_raw_row_sha256
            != attempt_raw_row_sha256("\t".join(fields))
        ):
            raise DispatchContractError(
                "reap-watch-foreground-seal-invalid", "stale-or-mismatched"
            )
    else:
        # A foreground review must consume the exact object returned by the
        # locked seal API.  Detached launches retain their historical None
        # fence and therefore need no registry read here.
        try:
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        matches = _foreground_row(lines, attempt_id)
        if len(matches) == 1 and matches[0][2].get("launch_lifecycle") == "foreground-scoped":
            raise DispatchContractError(
                "reap-watch-foreground-seal-required", attempt_id
            )
    script = _MODULE_ROOT / "utilities" / "dispatch-reap-watch.py"
    # The observer is governance machinery, not part of the governed attempt.
    # A direct wrapper can inherit the same attempt tag that supplied its
    # default --attempt-id; retaining it here would make the observer discover
    # itself forever and prevent the empty receipt it exists to issue.
    watcher_env = governance_environment()
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--jobs", str(Path(jobs).resolve()),
                "--attempt-id", attempt_id,
                "--pid", str(pid),
                "--pid-start", pid_start,
                "--pgid", str(pgid),
            ],
            cwd="/",
            env=watcher_env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DispatchContractError("reap-watch-launch-failed", str(exc)) from exc
    end_with_test(proc)
    return proc.pid


def close_attempt_row_if(
    jobs: Path,
    attempt_id: str,
    note: str,
    predicate: Callable[[list[str]], bool],
    *,
    evidence: dict[str, str] | None = None,
    teardown_claim: str | None = None,
    write_error: dict[str, str] | None = None,
) -> bool:
    """Revalidate and close one exact attempt inside the SD-49 lock.

    Reconciliation decisions depend on mutable process, worktree, marker and
    heartbeat evidence.  A read-then-``close_attempt_row`` sequence leaves a
    race between the decision and mutation.  This primitive re-reads the row
    and invokes the caller's safety predicate while the canonical registry is
    locked; a changed or newly-live row is therefore left untouched. An optional
    write_error distinguishes terminal metadata failures from a safety veto.
    """
    if write_error is not None:
        write_error.clear()
    if not attempt_id or not note:
        raise DispatchContractError("attempt-close-invalid", "attempt_id and note are required")
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
            if metadata.get("attempt_id") != attempt_id:
                continue
            matches.append((index, fields, metadata))
        if len(matches) != 1:
            return False
        index, fields, metadata = matches[0]
        if fields[1] not in {"open", "running"}:
            return False
        validate_attempt_metadata(metadata)
        import route_parent_close
        if route_parent_close.row_requested(metadata, jobs) and note != "cancelled-by-parent":
            return False
        recorded_claim = metadata.get("teardown_claim", "")
        if recorded_claim:
            if not teardown_claim or recorded_claim != teardown_claim:
                return False
        elif teardown_claim:
            return False
        if not predicate(fields.copy()):
            return False
        fields[1] = "done"
        values = {"note": note}
        if teardown_claim:
            values.update(
                teardown_claim="",
                teardown_claimed_at="",
                teardown_claim_pid="",
                teardown_claim_pid_start="",
            )
        values.update({
            key: value for key, value in (evidence or {}).items()
            if value not in (None, "")
        })
        values.update(_terminal_cleanup_values({**metadata, **values}))
        values.update(_delivery_intent_values(fields, {**metadata, **values}))
        try:
            fields[5] = _updated_attempt_metadata(
                _settlement_metadata_pipe(fields[5], values), values, terminal=True
            )
        except DispatchContractError as exc:
            if write_error is not None:
                write_error.update(reason=exc.reason, detail=exc.detail)
            return False
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return True
    return False


@dataclass(frozen=True)
class RefusedChainClose:
    cancelled: tuple[str, ...]
    already_closed: tuple[str, ...]
    unclosed: tuple[str, ...]
    unclosed_delivery: tuple[str, ...]


def attempt_row_terminal_never_started(fields: list[str]) -> bool:
    """True only for a terminal row carrying strict never-started fence proof."""
    if len(fields) != 6 or fields[1] not in {"done", "cancelled", "killed"}:
        return False
    return terminal_attempt_never_started(fields[1], parse_registry_metadata(fields[5]))


def terminal_attempt_never_started(status: str, metadata: Mapping[str, str]) -> bool:
    """Metadata form of the same terminal no-start proof for semantic censuses."""
    if status not in {"done", "cancelled", "killed"}:
        return False
    if metadata.get("launch_outcome") != "never-launched":
        return False
    if (metadata.get("launch_started") == "1" or metadata.get("launch_claimed") != "0"
            or _has_persisted_process_identity(metadata)):
        return False
    # Reviewing a terminal conflict records its inspection, not which outcome
    # was correct. Retained contradictory results still preclude strict proof.
    if (metadata.get("terminal_conflict") == "1"
            or any(metadata.get(key) for key in (
                "terminal_conflicts_b64", "conflicting_terminal_note", "conflicting_failure_class"))):
        return False
    # A committed work result contradicts a never-launched disposition.
    if (metadata.get("note") in SUCCESS_NOTES
            or metadata.get("note") in {"completed-review-blocking", "dead-worker-fail"}
            or metadata.get("failure_class") in {"pass", "success", "fail", "blocked"}):
        return False
    return True


def attempt_row_withdrawn_before_launch(fields: list[str]) -> bool:
    """Identify a never-started registration withdrawn under the jobs lock.

    An unclaimed open row may still be launched. This helper names only the
    cancellation-specific subset; `attempt_row_terminal_never_started` is the
    broader strict predicate used by round census and retry.
    """

    if len(fields) != 6 or not attempt_row_never_started(fields):
        return False
    metadata = parse_registry_metadata(fields[5])
    return fields[1] in {"done", "cancelled", "killed"} and metadata.get("failure_class") == "cancelled"


def close_refused_chain_rows(
    jobs: Path,
    attempt_ids: list[str] | tuple[str, ...],
    *,
    note: str,
    reconcile_reason: str,
) -> RefusedChainClose:
    """Close only exact rows whose lock-time state proves never-started."""

    cancelled: list[str] = []
    already_closed: list[str] = []
    unclosed: list[str] = []
    deliveries: list[str] = []
    evidence = {
        "failure_class": "cancelled",
        "classifier_source": SUBSESSION_CHAIN_REFUSAL_CLASSIFIER,
        "reconcile_reason": reconcile_reason,
        "launch_outcome": "never-launched",
    }
    try:
        ensure_global_registry_writable(jobs)
        with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
            for attempt_id in attempt_ids:
                try:
                    found = None
                    for index, line in enumerate(lines):
                        fields = line.split("\t")
                        if len(fields) == 6 and parse_registry_metadata(fields[5]).get("attempt_id") == attempt_id:
                            found = (index, fields)
                            break
                    if found is None:
                        unclosed.append(attempt_id)
                        deliveries.append("")
                        continue
                    index, fields = found
                    metadata = parse_registry_metadata(fields[5])
                    if fields[1] in {"done", "killed", "cancelled"}:
                        already_closed.append(attempt_id)
                        continue
                    if fields[1] not in {"open", "running"} or not attempt_row_never_started(fields):
                        unclosed.append(attempt_id)
                        deliveries.append(metadata.get("parent_completion_delivery", ""))
                        continue
                    values = {"note": note, **evidence}
                    values.update(_delivery_intent_values(fields, {**metadata, **values}))
                    fields[1] = "done"
                    # Keep this new lock-held close out of the W-site static
                    # bucket; its delivery intent was merged immediately
                    # above, but it is not one of the four ordinary edges.
                    terminal_edge = True
                    fields[5] = _updated_attempt_metadata(fields[5], values, terminal=terminal_edge)
                    lines[index] = "\t".join(fields)
                    _atomic_registry_replace(jobs, lines)
                    cancelled.append(attempt_id)
                except (DispatchContractError, OSError):
                    unclosed.append(attempt_id)
                    deliveries.append("")
    except (DispatchContractError, OSError):
        # Preserve the exact input order and fail closed when the registry
        # cannot be observed under its canonical lock.
        for attempt_id in attempt_ids:
            if attempt_id not in cancelled and attempt_id not in already_closed and attempt_id not in unclosed:
                unclosed.append(attempt_id)
                deliveries.append("")
    return RefusedChainClose(tuple(cancelled), tuple(already_closed), tuple(unclosed), tuple(deliveries))


@dataclass(frozen=True)
class SupervisionProbe:
    state: str
    reason: str = ""


def probe_owner_supervision(jobs: Path, attempt_id: str) -> SupervisionProbe:
    """Read-only proof that the exact Claude/Codex owner lease is held."""

    try:
        metadata = _declared_supervisor_lease_metadata(Path(jobs), attempt_id)
        path = _validated_supervisor_lease_path(jobs, attempt_id, metadata["supervisor_lease_file"])
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return SupervisionProbe("unproven", "lease-not-regular")
            if not _supervisor_lease_payload_matches(fd, metadata):
                return SupervisionProbe("unproven", "lease-payload-mismatch")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return SupervisionProbe("held", "")
            except OSError as exc:
                return SupervisionProbe("unproven", f"lease-lock-error:{type(exc).__name__}")
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return SupervisionProbe("unsupervised", "lease-unlocked")
        finally:
            os.close(fd)
    except DispatchContractError as exc:
        return SupervisionProbe("unsupervised", exc.reason)
    except OSError as exc:
        return SupervisionProbe("unproven", f"lease-open-error:{type(exc).__name__}")


def annotate_attempt_row(jobs: Path, attempt_id: str, values: dict[str, str]) -> bool:
    """Replace only mutable metadata on one exact attempt under the lock."""
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") != attempt_id:
                continue
            validate_attempt_metadata(metadata)
            fields[5] = _updated_attempt_metadata(fields[5], values)
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return True
    return False


def annotate_attempt_row_if(
    jobs: Path,
    attempt_id: str,
    values: dict[str, str],
    predicate: Callable[[list[str]], bool],
    *,
    statuses: frozenset[str] = frozenset({"open", "running"}),
) -> bool:
    """Compare-and-set mutable metadata on one exact attempt row.

    Only open rows are eligible by default. A caller that repairs missing
    post-exit evidence has to name a terminal status explicitly, because the row
    it needs to annotate is precisely the one an ordinary annotate would skip.
    """

    if not attempt_id:
        raise DispatchContractError("attempt-id-required")
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in statuses:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") != attempt_id:
                continue
            validate_attempt_metadata(metadata)
            if not predicate(fields.copy()):
                return False
            fields[5] = _updated_attempt_metadata(fields[5], values)
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return True
    return False


def reconcile_local_registry(global_jobs: Path, local_jobs: Path) -> tuple[int, int]:
    """Copy only current-contract local rows into the global registry once."""

    ensure_global_registry_writable(global_jobs)
    if not local_jobs.is_file():
        return 0, 0
    local_lines = local_jobs.read_text(encoding="utf-8").splitlines()
    # A row the registry archive moved is still registered; it is never copied back in.
    from dispatch_registry_archive import archived_rows
    archived = {identity for raw in archived_rows(global_jobs)
                if (identity := _row_identity(raw.split("\t"))) is not None}
    lock_path = Path(f"{global_jobs}.lock")
    reconciled = 0
    malformed = 0
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        global_lines = global_jobs.read_text(encoding="utf-8").splitlines()
        identities = archived | {
            identity for line in global_lines
            if (identity := _row_identity(line.split("\t"))) is not None
        }
        additions: list[str] = []
        for line in local_lines:
            fields = line.split("\t")
            identity = _row_identity(fields)
            if identity is None:
                malformed += 1
                continue
            metadata=parse_registry_metadata(fields[5])
            try:
                validate_attempt_metadata(metadata)
            except DispatchContractError:
                malformed += 1
                continue
            if identity in identities:
                continue
            fields[5] += f",reconciled_from={local_jobs}"
            additions.append("\t".join(fields))
            identities.add(identity)
            reconciled += 1
        if additions:
            _append_registry_rows(global_jobs, additions)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return reconciled, malformed
