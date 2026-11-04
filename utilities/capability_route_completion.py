"""Capability route: completing nodes.

`complete_node`, producer revision admission, owner closure plans, artifact
deltas, subdivision baselines, and `complete_subsession_stage`.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import route_identity as ROUTE_IDENTITY
import route_authority as ROUTE_AUTHORITY
from dispatch_contract import (
    DispatchContractError, owner_operation_fence, deferred_completion, verdict_pass,
    success_note, _atomic_registry_replace, _delivery_intent_values, _terminal_cleanup_values,
    _updated_attempt_metadata, ensure_terminal_claim_absent, agent_home_equivalent,
    attempt_process_quiescence, process_namespace_identity, terminal_conflict_pending,
    completion_marker_is_current, completion_attempt_readiness, owner_closure_shape,
    evidence_digest, gate_currency, review_self_disposition_origin, dispatch_state_roots,
    ensure_global_registry_writable, parse_registry_metadata, resolve_agent_home,
    validate_attempt_metadata,
)
from stage_session_contract import load_manifest, report_path_from_brief
from dispatch_degradation import record_degradation
from dispatch_completion_join import materialize_after_terminal_close
from codex_dispatch_terminal import REVIEW_BLOCKING_NOTE, inspect_terminal_attempt

from capability_route_base import (
    _attempt_completion_path, _exclusive_lock, _review_round_rows, atomic_write,
    canonical_route_path, completion_dir, ROOT, TOPO, write_once,
)

from capability_route_continuation import (
    _continuation_source_jobs, _note_evidence_changes, _registered_marker_fence,
)

from capability_route_compose import (
    _compose_default_jobs,
)

from capability_route_markers import (
    _completion_marker_replay, _marker_attempt_axes, _migrate_completion_dir_forward,
    _next_marker_sequence, _notify_reviewer_claim_ignored, _review_owner_authority,
    _review_self_disposition_locked, _validate_auxiliary_arbiter, resolve_review_identity,
    review_lineage_routes, revision_basis_verdict, write_completion_marker,
)

from capability_route_revision import (
    _require_entry_scope_review_preview,
)

from capability_route_close import (
    _head_commit, _promote_historical_false_outcome, outcome_path, owner_executed_terminal,
    owner_terminal_prerequisites, terminal_gate_observation,
)


def _publish_completion_locked(
    route,
    node,
    node_id,
    evidence,
    *,
    attempt_id,
    attempt_metadata,
    require_existing_link=False,
    review_claim=None,
    jobs=None,
    owner_override=False,
    owner_chain=False,
    check_only=False,
    expected_evidence_sha256=None,
):
    """Publish marker history, exact-attempt link, and canonical marker under one node lock."""

    from codex_dispatch_terminal import artifact_declared_verdict
    if not owner_override and artifact_declared_verdict(Path(evidence)) in {"FAIL", "BLOCKED"}:
        raise ValueError("completion-evidence-verdict-not-pass")

    # The same producer binding owns write admission and completion evidence.
    # An open neighbouring cycle cannot certify this route's completed work.
    from artifact_producer import ProducerError, require_cycle_output
    try:
        artifact_output = require_cycle_output(Path(route["artifact_root"]), Path(evidence), route_id=route["route_id"])
    except ProducerError as exc:
        raise ValueError(f"{exc.code}: {exc.detail}") from exc
    if artifact_output is not None:
        _require_entry_scope_review_preview(route, node, artifact_output)
    _validate_auxiliary_arbiter(route, node, evidence)
    axes=_marker_attempt_axes(node,attempt_id,attempt_metadata)
    evidence_sha=evidence_digest(evidence)
    if expected_evidence_sha256 is not None and "sha256:"+evidence_sha != expected_evidence_sha256:
        raise ValueError("recorded-move-evidence-drift")
    attempt_path=(
        _attempt_completion_path(route,node_id,attempt_id)
        if attempt_id else None
    )
    marker=None
    if attempt_path and attempt_path.is_file():
        existing_link=json.loads(attempt_path.read_text(encoding="utf-8"))
        expected_link_identity={
            "schema_version":2,
            "route_id":route["route_id"],
            "node_id":node_id,
            "attempt_id":attempt_id,
            **axes,
            "evidence_sha256":evidence_sha,
        }
        actual_link_identity={
            key:existing_link.get(key) for key in expected_link_identity
        }
        if actual_link_identity != expected_link_identity:
            raise ValueError("immutable attempt completion differs from existing link")
        history_path=Path(existing_link.get("completion_marker_history",""))
        if not history_path.is_file():
            # The recorded spelling may be a pointer-form path into a state
            # root that has since rotated away; look for the same basename
            # across every known state root before declaring it missing
            # (review N-1 -- identity, not verbatim spelling, is the contract).
            for root in dispatch_state_roots(resolve_agent_home()):
                candidate=root/"completion"/route["route_id"]/history_path.name
                if candidate.is_file():
                    history_path=candidate
                    break
        if not history_path.is_file():
            raise ValueError("immutable attempt completion history is missing")
        marker=json.loads(history_path.read_text(encoding="utf-8"))
        marker_identity={
            "schema_version":marker.get("schema_version"),
            "route_id":marker.get("route_id"),
            "node_id":marker.get("node_id"),
            "attempt_id":marker.get("attempt_id"),
            **{key:marker.get(key) for key in axes if key!="attempt_id"},
            "evidence_sha256":marker.get("evidence",{}).get("sha256"),
        }
        if marker_identity != expected_link_identity:
            raise ValueError("immutable attempt completion history differs from link")
        expected_history_path=completion_dir(route["route_id"])/f"{node_id}.{marker.get('sequence')}.json"
        if not agent_home_equivalent(history_path, expected_history_path):
            raise ValueError("immutable attempt completion history path differs from link")
        marker_static={
            "route_hash":route["route_hash"],
            "registry_digest":route["registry_digest"],
            "completion_gate":node["completion_gate"],
            "evidence_path":str(evidence),
        }
        actual_static={
            "route_hash":marker.get("route_hash"),
            "registry_digest":marker.get("registry_digest"),
            "completion_gate":marker.get("completion_gate"),
            "evidence_path":marker.get("evidence",{}).get("path"),
        }
        if actual_static!=marker_static:
            raise ValueError("immutable attempt completion route identity differs from link")
        current = None
        if (node.get("kind") == "review-worker"
                and marker.get("reviewer_downgrade_reason") == "reviewer-attempt-no-terminal-verdict"):
            current = _completion_marker_replay(
                route, node, node_id, evidence, axes, completion_dir(route["route_id"]), repair=not check_only)
        if current is not None and "reviewer_disposition" in current:
            origin, _ = review_self_disposition_origin(
                route, node, current, completion_dir(route["route_id"]) / f"{node_id}.json")
            if origin != marker:
                raise ValueError("review-self-disposition-origin-conflict")
            marker = current
        marker = _review_self_disposition_locked(
            route, node, marker, evidence, review_claim=review_claim, jobs=jobs, check_only=check_only)
        _notify_reviewer_claim_ignored(
            route,node_id,marker,
            resolve_review_identity(
                node,axes,attempt_metadata,
                claim=review_claim,jobs=jobs,
                route_id=route["route_id"],node_id=node_id,
                route_hash=route["route_hash"],
                owner_override=owner_override,owner_chain=owner_chain,
            ),
            review_claim,
        )
    elif require_existing_link:
        raise ValueError("completed attempt row lacks immutable completion link")

    if check_only:
        # The read-only branch uses the same immutable link/history checks
        # above and canonical replay proof below, without migration or repair.
        return marker if marker is not None else _completion_marker_replay(
            route, node, node_id, evidence, axes, completion_dir(route["route_id"]), repair=False,
        )

    if marker is None:
        marker=write_completion_marker(
            route,node,node_id,evidence,
            attempt_id=attempt_id,
            attempt_metadata=attempt_metadata,
            review_claim=review_claim,
            jobs=jobs,
            owner_override=owner_override,
            owner_chain=owner_chain,
            expected_evidence_sha256=expected_evidence_sha256,
            _lock_held=True,
        )
    if not attempt_id:
        return marker

    if "reviewer_disposition" in marker:
        # The attempt and current pointers still describe the original
        # publication. The shared reader follows the validated successor.
        review_self_disposition_origin(route, node, marker,
                                       completion_dir(route["route_id"]) / f"{node_id}.json")
        atomic_write(completion_dir(route["route_id"]) / f"{node_id}.attempt.json", existing_link)
        return marker

    canonical_marker_path=completion_dir(route["route_id"])/f"{node_id}.json"
    history_marker_path=completion_dir(route["route_id"])/f"{node_id}.{marker['sequence']}.json"
    attempt_link={
        "schema_version":2,
        "route_id":route["route_id"],"node_id":node_id,"attempt_id":attempt_id,
        "dispatch_depth":marker["dispatch_depth"],
        "transport":marker["transport"],
        "execution_surface":marker["execution_surface"],
        "registered_worker":marker["registered_worker"],
        "fallback_hop":marker["fallback_hop"],
        "evidence_sha256":marker["evidence"]["sha256"],
        "completion_marker":str(canonical_marker_path),
        "completion_marker_history":str(history_marker_path),
    }
    if marker.get("stage_authority") == "owner-closure":
        attempt_link.update(stage_authority="owner-closure", owner_closure_proof=marker["owner_closure_proof"])
    # Idempotent republish (review P-1): an existing sidecar whose only
    # difference from the link we would write is the SPELLING of its two
    # self-referential paths (pointer vs resolved form of one directory) is
    # the same publication, not a conflict. write_once compares whole byte
    # strings, so reaching it with such a sidecar re-raised on every retry;
    # skip the rewrite and keep the origin bytes exactly as first written.
    _SELF_REF_KEYS=("completion_marker","completion_marker_history")
    _skip_rewrite=False
    if attempt_path.is_file():
        try:
            _existing=json.loads(attempt_path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            _existing=None
        if isinstance(_existing,dict):
            _skip_rewrite=all(
                _existing.get(key)==value for key,value in attempt_link.items()
                if key not in _SELF_REF_KEYS
            ) and all(
                isinstance(_existing.get(key),str)
                and agent_home_equivalent(_existing[key],attempt_link[key])
                for key in _SELF_REF_KEYS
            )
    if not _skip_rewrite:
        write_once(attempt_path,attempt_link)
    if not canonical_marker_path.exists():
        # Recover publication after a crash using the verified immutable link
        # and latest history. A stale attempt cannot restore an older head.
        directory = completion_dir(route["route_id"])
        if marker["sequence"] != _next_marker_sequence(directory, node_id) - 1:
            raise ValueError("completion-recovery-history-not-latest")
        atomic_write(canonical_marker_path, marker)
    current_marker=json.loads(canonical_marker_path.read_text(encoding="utf-8"))
    if current_marker==marker:
        atomic_write(
            completion_dir(route["route_id"])/f"{node_id}.attempt.json",
            attempt_link,
        )
    return marker

def complete_node(
    route,
    node,
    node_id,
    evidence,
    jobs=None,
    attempt_id=None,
    explicit_attempt_metadata=None,
    review_claim=None,
):
    """Atomically publish one exact-attempt completion and close only its row.

    SD-111 trigger 1 for the `complete`-closed edge: when this call itself
    closes the registered row (`status=closed`), the delivery-intent stamp was
    appended inside the registry lock and the durable pending-delivery record
    is materialized here, after every lock is released (the materializer must
    never run under `<jobs>.lock`). Before 2026-08-29 this edge stamped no
    intent at all, so a quick one-shot owner's completion left no record and
    no carrier could ever deliver it.
    """
    if node_id=="route-decision" and isinstance(node,dict) and node.get("kind")==TOPO.ROUTE_DECISION_KIND:
        marker=_complete_framed_terminal(route,node,evidence,jobs)
        _launch_open_cycle_checkpoint(route)
        return marker,None
    if owner_executed_terminal(node):
        # The owner completing its own operation by hand passes the same entry gate its
        # settlement reads; no marker is written for an unreleased preview approval.
        try:
            owner_operation_fence(route,node,jobs=jobs)
        except DispatchContractError as exc:
            raise ValueError(f"{exc.reason}:{exc.detail}") from exc
    # This completion proceeds on its predecessors: keep any gates-off edit of their evidence as history.
    if node.get("completion_gate") == "ship-deploy":
        from ship_parent_deploy import validate_completion
        from dispatch_completion_join import JoinContractError
        try:
            validate_completion(route, jobs or _compose_default_jobs(), evidence)
        except (JoinContractError, KeyError, TypeError) as exc:
            raise ValueError("parent-deploy-invalid-evidence:" + str(exc)) from exc
    _note_evidence_changes(route,node.get("depends_on",[]) if isinstance(node,dict) else [],jobs=jobs)
    if node.get("completion_gate") == "compose-owner-close":
        missing = owner_terminal_prerequisites(route, node, jobs or _compose_default_jobs())
        if missing:
            raise ValueError("compose-owner-close-before-join:" + ",".join(sorted(missing)))
    artifact_root=route.get("artifact_root")
    route_id=route.get("route_id")
    pending=None
    # Legacy and subdivision routes may omit the inline-finish tuple entirely.
    # Only routes carrying both identifiers are in that fence's domain; once a
    # tuple is present, malformed or unreadable state remains a strict refusal.
    if artifact_root not in (None, "") and isinstance(route_id,str) and route_id:
        try:
            import inline_finish
            pending=inline_finish.pending_state(Path(artifact_root),route_id)
        except (OSError,ValueError) as exc:
            raise ValueError("finish-state-unreadable") from exc
    supplied_finish=os.environ.get("AGENT_INLINE_FINISH_ID")
    if pending and pending.get("state")!="finished" and pending.get("inline_finish_id")!=supplied_finish:
        raise ValueError("finish-in-progress")
    if pending and supplied_finish and pending.get("intent",{}).get("evidence_sha256"):
        if not inline_finish.evidence_matches(Path(evidence),pending["intent"]["evidence_sha256"]):
            raise ValueError("finish-evidence-drift")
    marker, row = _complete_node_locked(
        route, node, node_id, evidence,
        jobs=jobs, attempt_id=attempt_id,
        explicit_attempt_metadata=explicit_attempt_metadata,
        review_claim=review_claim,
    )
    if jobs and attempt_id and isinstance(row, dict) and row.get("status") == "closed":
        try:
            materialize_after_terminal_close(Path(jobs), attempt_id)
        except Exception:  # noqa: BLE001 -- a committed close is never unwound by delivery-layer failure
            pass
    if isinstance(artifact_root, str) and isinstance(route_id, str) and route_id:
        try:
            route_file = canonical_route_path(artifact_root, route_id)
            outcome_file = outcome_path(route_file)
            raw = outcome_file.read_bytes()
            prior = json.loads(raw.decode("utf-8"))
            if (prior.get("terminal_gate_proven") is False
                    and prior.get("inline_finish_id") is None and prior.get("summary_digest") is None):
                # A false record that carries a registered tuple is compared with the tuple the runtime
                # holds now for that owner (the promotion reads it); one that carries none keeps the
                # marker digest of this completion. An inline finish's record is that finish's to close.
                identity_bearing = any(prior.get(key) is not None for key in (
                    "terminal_commit_id", "terminal_owner_attempt_id", "producer_binding_digest",
                    "terminal_marker_digest"))
                marker_digest = None
                if not identity_bearing:
                    gates = terminal_gate_observation(route, jobs=jobs, exact_terminal=attempt_id is not None)
                    marker_digest = "sha256:" + hashlib.sha256(
                        json.dumps(gates, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                _promote_historical_false_outcome(
                    route, route_file, prior, raw, jobs=jobs,
                    terminal_marker_digest=marker_digest)
        except (OSError, ValueError, TypeError):
            # Completion is already committed; any unconsumed sidecar remains for
            # the established close/finalize retry path to inspect.
            pass
    _launch_open_cycle_checkpoint(route)
    return marker, row


def _complete_framed_terminal(route, node, evidence, jobs):
    """The framed route's runtime terminal completes without a model attempt, once its two frame
    legs are complete, the frame-review gate is released, and the evidence is this route's own
    `route_decision_v1` record. The marker is the ordinary inline-axes marker."""
    import route_plan
    from dispatch_contract import completion_marker_gate
    if not route_plan.is_framed_route(route):
        raise ValueError("route-frame-shape-mismatch")
    route_file=str(canonical_route_path(route["artifact_root"],route["route_id"]))
    registry=Path(jobs) if jobs else Path(_compose_default_jobs())
    try:
        # The one gate every entry node passes: both frame legs have a current completion marker
        # and the frame-review human gate bound at this node's entry is released.
        completion_marker_gate(route_file,node["id"],"start",ROOT,registry)
    except DispatchContractError as exc:
        raise ValueError(f"framed-terminal-not-ready:{exc.reason}") from exc
    frame_route=route_plan.read_record(evidence)["decision"]["frame_route"]
    if (frame_route["route_id"],frame_route["route_hash"])!=(route["route_id"],route["route_hash"]):
        raise ValueError("route-decision-invalid:frame_route")
    marker,_row=_complete_node_locked(route,node,node["id"],evidence)
    return marker


def _launch_open_cycle_checkpoint(route):
    """The route's open cycle republishes its interim manifest after a stage
    completes: detached, rate-limited, and never part of the completion."""
    try:
        import artifact_checkpoint_trigger
        artifact_checkpoint_trigger.launch_for_route(route, trigger="stage-complete")
    except Exception:  # noqa: BLE001
        pass


# OPERATIONS §5.10 "Review verdict is a result, not a worker death" -- the
# owner-closure completion path for a review row that ended
# `completed-review-blocking`. Evidence-bound on purpose: a bare flag, a memo
# that names no attempt, a `dead-*` row, or an unexhausted round budget all keep
# the SD-94 fail-closed refusal. Every refusal is typed `owner-closure-*`.
_OWNER_CLOSURE_SUFFIX=".owner-closure.md"
_OWNER_CLOSURE_VERDICT="closed-by-owner"
_REGISTRY_UNSAFE_CHARS=(",","=","\t","\n","\r")
_LIVE_ROW_STATUSES=ROUTE_AUTHORITY.LIVE_ROW_STATUSES

def _registry_unsafe(value):
    """True when a value cannot be sealed into the comma/=/tab/newline registry pipe."""
    text=str(value)
    return any(ch in text for ch in _REGISTRY_UNSAFE_CHARS) or any(ord(ch)<32 or ord(ch)==127 for ch in text)

def _owner_closure_frontmatter(text):
    """Parse the flat `key: value` frontmatter of an owner-closure record.

    Returns (fields, duplicate_keys); a duplicated key is a refusal upstream
    because last-value-wins would let a second `verdict:` line override the first."""
    match=re.match(r"\A---\n(.*?\n)---\n",text,re.DOTALL)
    if not match:
        return None, []
    fields={}
    duplicates=[]
    for line in match.group(1).splitlines():
        if ":" not in line or line.startswith((" ","\t","#")):
            continue
        key,_,value=line.partition(":")
        key=key.strip()
        if key in fields:
            duplicates.append(key)
        fields[key]=value.strip().strip("'\"")
    return fields, sorted(set(duplicates))

def _mentions(text, token):
    """Whole-token mention: `att-r1` must not be satisfied by `att-r10`."""
    return re.search(r"(?<![A-Za-z0-9_./-])"+re.escape(token)+r"(?![A-Za-z0-9_-])",text) is not None


def producer_revision_admission(route, nodes, jobs):
    """Resolve the approved generation for one atomic plan-author group.

    A generation changes only when the existing producer revision writer has
    recorded a new, basis-verified revision. The returned token is derived
    from immutable marker history; it is not a new approval or history store.
    """
    nodes = list(nodes or ())
    if (not nodes or any(node.get("unit") != "plan/plan-author" for node in nodes)
            or len({str(node.get("id")) for node in nodes}) != len(nodes)):
        return {"managed": False, "generation": None, "pending": False,
                "current_attempt_ids": {}}
    group_ids = {node.get("parallel_group") or node.get("replica_group") for node in nodes}
    if len(group_ids) != 1 or None in group_ids:
        raise ValueError("producer-revision-group-invalid")
    if jobs is None:
        raise ValueError("producer-revision-jobs-required")
    jobs = Path(jobs).resolve()
    directory = completion_dir(route["route_id"], jobs=jobs)
    if not jobs.is_file():
        if directory.exists() or directory.is_symlink():
            raise ValueError("producer-revision-registry-unreadable")
        return {"managed": True, "generation": "1", "pending": False,
                "current_attempt_ids": {},
                "basis": [{"node_id": str(node["id"]), "sequence": 0} for node in nodes]}
    generation_basis = []
    current_attempt_ids = {}
    pending = False
    any_revision = False
    for node in nodes:
        node_id = str(node["id"])
        prefix = f"{node_id}."
        revision_markers = []
        numeric_paths = []
        if directory.is_dir():
            for path in directory.glob(f"{node_id}.*.json"):
                middle = path.name[len(prefix):-5]
                if middle.isdigit():
                    numeric_paths.append((int(middle), path))
        numeric_paths.sort()
        for sequence, path in numeric_paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError("producer-revision-history-invalid")
            try:
                raw = path.read_bytes()
                marker = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("producer-revision-history-invalid") from exc
            if (not isinstance(marker, dict) or marker.get("sequence") != sequence
                    or marker.get("route_id") != route.get("route_id")
                    or marker.get("route_hash") != route.get("route_hash")
                    or marker.get("node_id") != node_id):
                raise ValueError("producer-revision-history-invalid")
            if marker.get("stage_authority") != "revision":
                continue
            revision = marker.get("revision")
            evidence = marker.get("evidence")
            if not isinstance(revision, dict) or not isinstance(evidence, dict):
                raise ValueError("producer-revision-history-invalid")
            prior_sequence = revision.get("of_sequence")
            if not isinstance(prior_sequence, int) or prior_sequence < 1 or prior_sequence >= sequence:
                raise ValueError("producer-revision-history-invalid")
            prior_path = directory / f"{node_id}.{prior_sequence}.json"
            try:
                prior_bytes = prior_path.read_bytes()
                prior_marker = json.loads(prior_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("producer-revision-history-invalid") from exc
            prior_sha = hashlib.sha256(prior_bytes).hexdigest()
            if (revision.get("of_marker_sha256") != prior_sha
                    or revision.get("of_evidence_sha256") != (prior_marker.get("evidence") or {}).get("sha256")
                    or revision.get("evidence_sha256") != evidence.get("sha256")):
                raise ValueError("producer-revision-history-invalid")
            try:
                revision_basis_verdict(
                    route, node, revision.get("basis"), revision.get("answers") or (),
                    jobs=jobs, direction=revision.get("direction"), reason=revision.get("reason"),
                )
            except ValueError as exc:
                raise ValueError(f"producer-revision-basis-unverified:{node_id}:{exc}") from exc
            revision_markers.append((sequence, raw, marker))

        canonical_path = directory / f"{node_id}.json"
        if canonical_path.is_symlink():
            raise ValueError("producer-revision-canonical-invalid")
        if canonical_path.is_file():
            try:
                canonical_raw = canonical_path.read_bytes()
                canonical = json.loads(canonical_raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("producer-revision-canonical-invalid") from exc
            if not isinstance(canonical, dict):
                raise ValueError("producer-revision-canonical-invalid")
            sequence = canonical.get("sequence")
            if not isinstance(sequence, int) or sequence < 1:
                raise ValueError("producer-revision-canonical-invalid")
            history_path = directory / f"{node_id}.{sequence}.json"
            if (canonical.get("route_id") != route.get("route_id")
                    or canonical.get("route_hash") != route.get("route_hash")
                    or canonical.get("node_id") != node_id
                    or history_path.is_symlink() or not history_path.is_file()
                    or history_path.read_bytes() != canonical_raw):
                raise ValueError("producer-revision-canonical-history-conflict")
            currency = gate_currency(route, node, canonical_path, canonical,
                                     observe=True, jobs=jobs)
            if currency.state != "current":
                raise ValueError(f"producer-revision-marker-{currency.state}:{node_id}:{currency.reason}")
            latest_revision = revision_markers[-1] if revision_markers else None
            if (latest_revision and canonical.get("stage_authority") == "revision"
                    and canonical.get("sequence") == latest_revision[0]):
                pending = True
            elif (canonical.get("stage_authority") != "revision"
                    and not canonical.get("state") and canonical.get("attempt_id")):
                current_attempt_ids[node_id] = str(canonical["attempt_id"])
        elif numeric_paths:
            raise ValueError("producer-revision-canonical-missing")

        if revision_markers:
            any_revision = True
            sequence, raw, marker = revision_markers[-1]
            revision = marker["revision"]
            generation_basis.append({
                "node_id": node_id,
                "sequence": sequence,
                "marker_sha256": hashlib.sha256(raw).hexdigest(),
                "basis": revision.get("basis"),
                "answers": revision.get("answers") or [],
                "direction": revision.get("direction"),
                "reason": revision.get("reason"),
                "author_attempt_id": revision.get("author_attempt_id"),
                "evidence_sha256": (marker.get("evidence") or {}).get("sha256"),
            })
        else:
            generation_basis.append({"node_id": node_id, "sequence": 0})

    generation = "1"
    if any_revision:
        encoded = json.dumps(generation_basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
        generation = "rev-" + hashlib.sha256(encoded).hexdigest()
    if pending:
        # A revision marker is the approval for a new generation. The prior
        # completed producer attempts remain history, not peers in this batch.
        current_attempt_ids = {}
    return {"managed": True, "generation": generation, "pending": pending,
            "current_attempt_ids": current_attempt_ids, "basis": generation_basis}


def _owner_closure_eligibility(route, node, node_id, evidence, row_metadata, lines,
                               *, rounds=None, check_canonical=True, jobs=None):
    """Admit `complete` on a `completed-review-blocking` row, or raise a typed refusal.

    Returns the closure facts the caller seals on the row. Checks, in order:
    the node is a review node and the row a review worker; no review round of
    the node is still open/running and the terminated rounds exhaust the
    budget; the node has no current canonical marker from another attempt; the exact
    attempt log still proves a FAIL handoff with a readable in-root review
    artifact; the evidence is a registry-safe, in-root `*.owner-closure.md`
    distinct from that artifact with the closure frontmatter; and its body
    names every blocking attempt of the node and the review artifact it rules
    on, as whole tokens.
    """
    def refuse(reason, detail=""):
        raise ValueError(f"owner-closure-{reason}"+(f":{detail}" if detail else ""))

    if node.get("kind")!="review-worker" or row_metadata.get("worker_type")!="review":
        refuse("node-not-review",
               f"kind={node.get('kind') or '-'};worker_type={row_metadata.get('worker_type') or '-'}")
    if rounds is None:
        rounds=_review_round_rows(lines,route["route_id"],node_id,jobs=jobs)
    source_subject_digest = None
    reviewed_input = None
    if jobs is not None:
        import review_input
        if review_input.is_review_node(node):
            try:
                reviewed_input = review_input.resolve_input(route, node, jobs)
            except DispatchContractError:
                if row_metadata.get("review_input_digest"):
                    raise
    from review_source_subject import (SourceSubjectError, annotate_rows, has_review_subject,
                                       observe, read_binding, source_epoch)
    if has_review_subject(route, node, jobs, reviewed_input):
        try:
            binding = read_binding(jobs, row_metadata, required=False) if jobs else None
            current = observe(route, node, worktree=route.get("cwd"), jobs=jobs,
                              reviewed_input=reviewed_input)
            epoch = source_epoch(route, node, current["digest"],
                                 completion_dir(route["route_id"], jobs=jobs),
                                 family=current.get("family", "source"))
        except (SourceSubjectError, OSError, ValueError) as exc:
            refuse("source-subject-unverifiable", getattr(exc, "reason", str(exc)))
        if (binding is not None and (binding["subject_digest"] != current["digest"]
                or binding.get("family", "source") != current.get("family", "source")
                or binding["source_epoch"] != epoch)):
            refuse("source-subject-stale", row_metadata.get("attempt_id", ""))
        try:
            rounds = annotate_rows(jobs, rounds, route=route, node=node,
                                   family=current.get("family", "source"))
        except SourceSubjectError as exc:
            refuse("source-subject-unverifiable", getattr(exc, "reason", str(exc)))
        if (binding is not None or current.get("family", "source") == "source"
                or any(meta.get("_review_subject_digest") for _status, meta in rounds)):
            source_subject_digest = current["digest"]
    choice = ROUTE_AUTHORITY.decide_continuation(route, action="owner-closure", node=node, rows=rounds,
        facts=({"source_subject_digest": source_subject_digest}
               if source_subject_digest is not None else None))
    ROUTE_AUTHORITY.require_continuation(choice)
    budget = choice.budget
    closure_rounds = rounds
    if source_subject_digest is not None:
        closure_rounds = [(status, metadata) for status, metadata in rounds
                          if metadata.get("_review_subject_digest") == source_subject_digest
                          or metadata.get("review_subject_digest") == source_subject_digest]
    terminated=[(status,metadata) for status,metadata in closure_rounds
                if status not in _LIVE_ROW_STATUSES]
    max_round=budget.cap
    own=row_metadata.get("attempt_id")
    canonical=completion_dir(route["route_id"])/f"{node_id}.json"
    if check_canonical and canonical.is_file():
        try:
            existing=json.loads(canonical.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            refuse("node-already-complete","canonical-marker-unreadable")
        # SD-70: one node, one exact attempt. A second closure would
        # overwrite the canonical marker and leave two rows claiming it. A
        # marker the reader fence already treats as not current (a later row
        # for the node was recorded, e.g. a correction added a review round)
        # claims nothing, so closing the latest round may replace it. Inline,
        # resource and continuation markers are never fenced by readers, so
        # they stay current here and still refuse.
        superseded=(_registered_marker_fence(route,node,existing,lines)=="completion-attempt-not-current"
                    and _registered_marker_fence(route,node,{"attempt_id":own},lines) is None)
        if existing.get("state")=="superseded-by-upstream-revision":
            from dispatch_contract import gate_currency
            currency=gate_currency(route,node,canonical,existing,observe=True,jobs=jobs)
            if currency.state!="superseded":
                refuse("canonical-marker-unproven",currency.reason)
            latest_blocking=next((metadata.get("attempt_id") for status,metadata in reversed(terminated)
                                  if status=="done" and metadata.get("note")==REVIEW_BLOCKING_NOTE),None)
            superseded=superseded or own==latest_blocking
        if existing.get("attempt_id")!=own and not superseded:
            refuse("node-already-complete",f"attempt={existing.get('attempt_id') or '-'}")
    terminal=inspect_terminal_attempt(
        row_metadata.get("log_file"),
        worktree=route["cwd"],
        artifact_root_metadata=row_metadata.get("artifact_root") or route.get("artifact_root"),
        worker_type="review",
        artifact_binding=row_metadata,
    )
    if (
        terminal.get("state")!="valid"
        or str(terminal.get("verdict"))!="FAIL"
        or terminal.get("artifact_state")!="readable"
    ):
        refuse("review-artifact-unverifiable",
               f"state={terminal.get('state')};verdict={terminal.get('verdict')};"
               f"artifact_state={terminal.get('artifact_state')};reason={terminal.get('reason')}")
    encoded=str(terminal.get("artifact_path_b64") or "")
    try:
        review_artifact=Path(
            base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)).decode("utf-8")
        ).resolve()
    except (ValueError,UnicodeDecodeError):
        refuse("review-artifact-unverifiable","artifact-undecodable")
    evidence_path=Path(evidence).resolve()
    if _registry_unsafe(evidence_path):
        # The path is sealed into the registry pipe; a ',' '=' tab or newline
        # in it would forge fields or whole rows.
        refuse("evidence-path-unsafe",evidence_path.name.encode("unicode_escape").decode("ascii")[:80])
    if not evidence_path.name.endswith(_OWNER_CLOSURE_SUFFIX):
        refuse("evidence-name-invalid",evidence_path.name)
    artifact_root=Path(route["artifact_root"]).resolve()
    try:
        evidence_path.relative_to(artifact_root)
    except ValueError:
        refuse("evidence-outside-root",str(evidence_path))
    if evidence_path==review_artifact:
        refuse("evidence-is-review-artifact",evidence_path.name)
    try:
        text=evidence_path.read_text(encoding="utf-8")
    except (OSError,UnicodeDecodeError):
        refuse("evidence-unreadable",str(evidence_path))
    frontmatter,duplicates=_owner_closure_frontmatter(text)
    if frontmatter is None:
        refuse("frontmatter-invalid","missing")
    if duplicates:
        refuse("frontmatter-invalid","duplicate="+"|".join(duplicates))
    if frontmatter.get("verdict")!=_OWNER_CLOSURE_VERDICT:
        refuse("frontmatter-invalid",f"verdict={frontmatter.get('verdict') or '-'}")
    if frontmatter.get("node")!=node_id:
        refuse("frontmatter-invalid",f"node={frontmatter.get('node') or '-'}")
    if "gate" in frontmatter and frontmatter["gate"]!=node.get("completion_gate"):
        refuse("frontmatter-invalid",f"gate={frontmatter['gate']}")
    blocking=[
        metadata.get("attempt_id") for status,metadata in terminated
        if status=="done" and metadata.get("note")==REVIEW_BLOCKING_NOTE and metadata.get("attempt_id")
    ]
    if own and own not in blocking:
        blocking.append(own)
    missing=[attempt for attempt in blocking if not _mentions(text,attempt)]
    if missing:
        refuse("evidence-unlinked","attempt="+"|".join(missing))
    if not _mentions(text,review_artifact.name):
        refuse("evidence-unlinked",f"artifact={review_artifact.name}")
    return {
        "evidence":str(evidence_path),
        "review_artifact_b64":encoded,
        "blocking_attempts":blocking,
        "rounds":len(terminated),
        "max_round":max_round,
    }


def _continuation_closure_marker_compatible(route, node, evidence, jobs, proof):
    path = completion_dir(route["route_id"], jobs=jobs) / f"{node['id']}.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (existing.get("stage_authority") != "owner-closure"
                or existing.get("owner_closure_proof") != proof
                or existing.get("evidence") != {"path": str(evidence), "sha256": evidence_digest(evidence)}):
            raise ValueError("owner-closure-node-already-complete")


def _owner_closure_sealed_pipe(pipe, facts):
    try:
        return _updated_attempt_metadata(pipe, {
            "gate_closure": "owner-closure", "owner_closure": facts["evidence"],
            "review_artifact_b64": facts["review_artifact_b64"],
        }, terminal=True)
    except DispatchContractError as exc:
        raise ValueError(f"owner-closure-seal-refused:{exc.reason}") from exc


def owner_closure_plan(route, node, evidence, jobs, attempt_id, *, lines=None):
    """Select authority by the exact row, never by continuation depth.

    Operator recovery remains available outside a registered worker context.
    A registered caller must be the current owner of this route.
    """
    jobs = Path(jobs).resolve()
    ensure_terminal_claim_absent(jobs, route["route_id"], attempt_id)
    if lines is None:
        lines = jobs.read_text(encoding="utf-8").splitlines()
    matches = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) == 6:
            meta = parse_registry_metadata(fields[5])
            if meta.get("attempt_id") == attempt_id:
                matches.append((fields, meta))
    if len(matches) != 1:
        raise ValueError("owner-closure-source-attempt-not-exact")
    fields, selected = matches[0]
    try:
        validate_attempt_metadata(selected)
    except DispatchContractError as exc:
        raise ValueError(f"row-contract-invalid:{exc.reason}") from exc
    if ROUTE_AUTHORITY.subsession_row(selected):
        raise ValueError("subsession-has-no-stage-gate-authority")
    current = ROUTE_IDENTITY.registered_node_identity(selected, node) == (
        route["route_id"], route["route_hash"], node["id"])
    if current and fields[1] == "done" and selected.get("note") == "completed-marker":
        marker = _publish_completion_locked(
            route, node, node["id"], evidence, jobs=jobs, attempt_id=attempt_id,
            attempt_metadata=selected, require_existing_link=True, check_only=True,
        )
        if owner_closure_shape(marker) != "registered-review":
            raise ValueError("owner-closure-replay-not-owner-closure")
        return {"schema_version": 1, "source_attempt_id": attempt_id, "jobs": str(jobs),
                "already_completed": True, "marker": marker}
    proof = continuation_owner_closure_plan(
        route, node, evidence, jobs, attempt_id, lines=lines,
        check_dependencies=not current, _same_route=current,
    )
    if current:
        _owner_closure_sealed_pipe(fields[5], proof["closure"])
        metadata = selected
    else:
        _continuation_closure_marker_compatible(route, node, evidence, jobs, proof)
        metadata = {"stage_authority": "owner-closure", "owner_closure_proof": proof}
    _publish_completion_locked(
        route, node, node["id"], evidence, jobs=jobs, attempt_id=attempt_id,
        attempt_metadata=metadata, owner_override=True, check_only=True,
    )
    return proof


def continuation_owner_closure_plan(route, node, evidence, jobs, attempt_id, *,
                                    lines=None, check_process=True, check_dependencies=True,
                                    _same_route=False):
    """Read-only authority shared by check, commit and downstream consumers."""
    from artifact_producer import require_cycle_output
    from dispatch_contract import attempt_process_quiescence, terminal_conflict_pending

    jobs = Path(jobs).resolve()
    if not _same_route and jobs != _continuation_source_jobs(route).resolve():
        raise ValueError("owner-closure-registry-mismatch")
    lineage = review_lineage_routes(route, node["id"])
    if not _same_route and len(lineage) < 2:
        raise ValueError("owner-closure-official-continuation-required")
    route_by_id = {r["route_id"]: r for r in lineage}
    if lines is None:
        lines = jobs.read_text(encoding="utf-8").splitlines()
    rounds = [row for r in reversed(lineage) for row in _review_round_rows(lines, r["route_id"], node["id"],jobs=jobs)]
    exact = [(status, meta) for status, meta in rounds if meta.get("attempt_id") == attempt_id]
    if len(exact) != 1:
        raise ValueError("owner-closure-source-attempt-not-exact")
    status, selected = exact[0]
    source_id = selected.get("route_id") or selected.get("route")
    if (source_id == route["route_id"]) != _same_route or status != "done" or selected.get("note") != REVIEW_BLOCKING_NOTE:
        raise ValueError("owner-closure-source-not-blocking-current" if _same_route else "owner-closure-source-not-blocking-ancestor")
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if caller and check_process:
        _review_owner_authority(route, jobs, caller)
    output = require_cycle_output(Path(route["artifact_root"]), Path(evidence), route_id=route["route_id"])
    if output is None and not _same_route:
        raise ValueError("owner-closure-destination-cycle-missing")
    facts = _owner_closure_eligibility(route, node, node["id"], evidence, selected, lines,
                                     rounds=rounds, check_canonical=_same_route, jobs=jobs)
    reviews = []
    seen = set()
    blocking_attempts = set(facts.get("blocking_attempts") or ())
    for row_status, meta in rounds:
        identity = meta.get("attempt_id")
        parent = route_by_id[meta.get("route_id") or meta.get("route")]
        if not identity or identity in seen:
            raise ValueError("owner-closure-round-identity-ambiguous")
        seen.add(identity)
        validate_attempt_metadata(meta)
        if ROUTE_IDENTITY.registered_node_identity(meta, node) != (parent["route_id"], parent["route_hash"], node["id"]):
            raise ValueError("owner-closure-round-route-mismatch")
        if terminal_conflict_pending(meta):
            raise ValueError(f"owner-closure-terminal-evidence-conflict:{identity}")
        if check_process:
            process = attempt_process_quiescence(meta, terminal_receipt=True)
            if process.state != "quiescent":
                raise ValueError(f"owner-closure-round-{process.state}:{identity}:{process.reason}")
        if (identity in blocking_attempts and row_status == "done"
                and meta.get("note") == REVIEW_BLOCKING_NOTE):
            reviewed = _owner_closure_eligibility(parent, node, node["id"], evidence, meta, lines,
                                                 rounds=rounds, check_canonical=False, jobs=jobs)
            encoded = reviewed["review_artifact_b64"]
            artifact = Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
            reviews.append({"attempt_id": identity, "route_id": parent["route_id"],
                            "route_hash": parent["route_hash"], "artifact": str(artifact),
                            "sha256": evidence_digest(artifact)})
    if check_dependencies:
        for dependency in node.get("depends_on", []):
            predecessor = next(n for n in route["nodes"] if n["id"] == dependency)
            path = completion_dir(route["route_id"], jobs=jobs) / f"{dependency}.json"
            if not completion_marker_is_current(route, predecessor, path, jobs=jobs):
                raise ValueError(f"owner-closure-dependency-unproven:{dependency}")
            marker = json.loads(path.read_text(encoding="utf-8"))
            ready = completion_attempt_readiness(route, predecessor, marker, jobs, registry_lines=lines)
            if ready.state != "ready":
                raise ValueError(f"owner-closure-dependency-{ready.state}:{dependency}:{ready.reason}")
    return {"schema_version": 1, "source_attempt_id": attempt_id, "jobs": str(jobs),
            "lineage": [{"route_id": r["route_id"], "route_hash": r["route_hash"]} for r in lineage],
            "output_dir": str(output) if output is not None else None, "reviews": reviews,
            "rounds": facts["rounds"], "max_round": facts["max_round"],
            **({"closure": facts} if _same_route else {})}


def validate_continuation_owner_closure(route, node, marker, *, jobs=None, lines=None, check_process=False):
    proof = marker.get("owner_closure_proof")
    if (not isinstance(proof, dict) or marker.get("registered_worker") is not False
            or marker.get("review_independence") != "owner-overridden"
            or marker.get("review_gate_closure") != "owner-closure"
            or marker.get("attempt_id") != proof.get("source_attempt_id")):
        raise ValueError("owner-closure-proof-invalid")
    actual = continuation_owner_closure_plan(
        route, node, Path(marker["evidence"]["path"]), jobs or proof.get("jobs", ""), marker["attempt_id"],
        lines=lines, check_process=check_process, check_dependencies=False,
    )
    if actual != proof:
        raise ValueError("owner-closure-proof-drift")


def _publish_continuation_owner_closure(route, node, evidence, jobs, attempt_id, lines):
    proof = owner_closure_plan(route, node, evidence, jobs, attempt_id, lines=lines)
    _continuation_closure_marker_compatible(route, node, evidence, jobs, proof)
    marker = _publish_completion_locked(
        route, node, node["id"], evidence, jobs=jobs, attempt_id=attempt_id,
        attempt_metadata={"stage_authority": "owner-closure", "owner_closure_proof": proof},
        owner_override=True,
    )
    return marker, {"status": "continuation-gate-completed", "gate_closure": "owner-closure",
                    "source_attempt_id": attempt_id, "source_rows_changed": 0,
                    "blocking_attempts": [r["attempt_id"] for r in proof["reviews"]]}


def _placed_artifact_false_negative(route, node, fields, metadata, evidence, lines):
    """Prove one legacy artifact-missing close was a recorded bucket move.

    This is deliberately narrower than terminal-conflict review: a FAIL or
    BLOCKED handoff, another failure class, a changed source path, or a missing
    current manifest cannot turn a committed terminal row into a PASS.
    """
    if (fields[1] != "done" or metadata.get("note") != "dead-invalid-envelope"
            or metadata.get("route_id") != route.get("route_id")
            or metadata.get("route_hash") != route.get("route_hash")
            or metadata.get("route_node") != node.get("id")
            or metadata.get("failure_class") != "invalid-envelope"
            or metadata.get("classifier_source") != "completion-join-invalid-envelope-v1"
            or metadata.get("reconcile_reason") != "terminal-invalid:artifact-missing"
            or metadata.get("worker_type") != "stage"
            or metadata.get("dispatch_depth") != "2"
            or metadata.get("launch_lifecycle") != "foreground-scoped"
            or metadata.get("pid_scope") != "namespace-local"
            or metadata.get("group_reap_proof") != "pgid-empty-v1"
            or metadata.get("attempt_descendant_proof") != "attempt-tagged-empty-v1"
            or metadata.get("pid_ns") != process_namespace_identity()
            or metadata.get("pid_observer_ns") != metadata.get("pid_ns")
            or terminal_conflict_pending(metadata)
            or metadata.get("terminal_correction_b64")
            or Path(fields[3]).resolve() != Path(route["cwd"]).resolve()):
        return None
    attempt_id = metadata.get("attempt_id", "")
    current_rows = [(index, parts, parse_registry_metadata(parts[5]))
                    for index, row in enumerate(lines)
                    if len(parts := row.split("\t")) == 6]
    exact = [index for index, _, candidate in current_rows
             if candidate.get("attempt_id") == attempt_id]
    if (not attempt_id or len(exact) != 1
            or not metadata.get("log_file", "").endswith(f".{attempt_id}.codex.jsonl")
            or attempt_process_quiescence(metadata, terminal_receipt=True).state != "quiescent"):
        return None
    if any(index > exact[0] and candidate.get("route_id") == route["route_id"]
           and candidate.get("route_node") == node["id"] and status[1] in {"open", "running", "done"}
           for index, status, candidate in current_rows):
        return None
    marker_path = completion_dir(route["route_id"]) / f"{node['id']}.json"
    if marker_path.is_file():
        try:
            if json.loads(marker_path.read_text(encoding="utf-8")).get("attempt_id") != attempt_id:
                return None
        except (OSError, ValueError):
            return None
    terminal = inspect_terminal_attempt(
        metadata["log_file"], worktree=fields[3],
        artifact_root_metadata=metadata.get("artifact_root"),
        artifact_binding=metadata,
    )
    if (terminal.get("state") != "valid" or terminal.get("source") != "exact-turn-completed"
            or terminal.get("terminal_event") != "turn.completed"
            or terminal.get("verdict") != "PASS"
            or terminal.get("failure_class") != "pass"
            or terminal.get("artifact_state") != "readable"
            or terminal.get("artifact_shape") != "file"
            or not terminal.get("artifact_origin_path_b64")):
        return None
    try:
        origin = Path(base64.urlsafe_b64decode(
            terminal["artifact_origin_path_b64"] + "=" * (-len(terminal["artifact_origin_path_b64"]) % 4)
        ).decode("utf-8"))
        observed = Path(base64.urlsafe_b64decode(
            terminal["artifact_path_b64"] + "=" * (-len(terminal["artifact_path_b64"]) % 4)
        ).decode("utf-8"))
        from artifact_producer import placed_output_proof
        proof = placed_output_proof(
            origin, route_id=route["route_id"], route_hash=route["route_hash"])
        if (proof is None or observed != Path(proof["destination"])
                or Path(evidence).resolve(strict=True) != observed):
            return None
        raw_log = Path(metadata["log_file"]).read_bytes()
    except (OSError, ValueError, KeyError, UnicodeDecodeError):
        return None
    terminal_time = ""
    for line in reversed(raw_log.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            terminal_time = str(event.get("timestamp") or "")
            break
    observed_at = datetime.now(timezone.utc).isoformat()
    proof.update(
        terminal_log_sha256=hashlib.sha256(raw_log).hexdigest(),
        terminal_time=terminal_time,
        prior_failure_observed_at=observed_at,
        prior_note=metadata["note"],
        prior_classifier_source=metadata["classifier_source"],
        prior_failure_class=metadata["failure_class"],
        prior_reconcile_reason=metadata["reconcile_reason"],
        corrected_at=observed_at,
        attempt_id=attempt_id, route_node=node["id"],
    )
    return proof

def _bound_env_attempt(route, node, jobs):
    """`AGENT_DISPATCH_ATTEMPT_ID` when its registry row is this route node's attempt.

    A registered worker completing its own node may omit `--attempt-id`; the
    environment already names it. Any other caller (an owner, a session that
    completes an inline node) gets nothing filled in and keeps today's forms.
    Fails open: an unreadable registry or an absent row fills nothing."""
    attempt_id=os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if not attempt_id: return None
    try:
        for line in Path(jobs).read_text(encoding="utf-8",errors="replace").splitlines():
            fields=line.split("\t")
            if len(fields)!=6: continue
            metadata=parse_registry_metadata(fields[5])
            if metadata.get("attempt_id")==attempt_id:
                bound=ROUTE_IDENTITY.registered_node_identity(metadata,node)==(
                    route["route_id"],route["route_hash"],node["id"])
                return attempt_id if bound else None
    except (OSError,ValueError,KeyError,TypeError):
        return None
    return None

def _unregistered_attempt(metadata):
    """Whether explicit attempt metadata declares an attempt the registry never owned.

    Mirrors `dispatch_contract._registered_worker` for the declared spellings; an
    absent or invalid value stays "registered" so the fail-closed row paths keep it.
    """
    if not metadata:
        return False
    value = metadata.get("registered_worker")
    if isinstance(value, bool):
        return not value
    return str(value).lower() in {"0", "false"}


def _sole_node_attempt(route,node,jobs_path):
    """The attempt id of the one registered attempt of `node` on this exact route, else None."""
    try:
        lines=jobs_path.read_text(encoding="utf-8",errors="replace").splitlines()
    except OSError:
        return None
    found=set()
    for line in lines:
        fields=line.split("\t")
        if len(fields)!=6:
            continue
        metadata=parse_registry_metadata(fields[5])
        try:
            same=(metadata.get("attempt_id") and not ROUTE_AUTHORITY.subsession_row(metadata)
                  and ROUTE_IDENTITY.registered_node_identity(metadata,node)
                      ==(route["route_id"],route["route_hash"],node["id"]))
        except (ValueError,KeyError,TypeError):
            same=False
        if same:
            found.add(metadata["attempt_id"])
    return next(iter(found)) if len(found)==1 else None

def _complete_node_locked(
    route,
    node,
    node_id,
    evidence,
    jobs=None,
    attempt_id=None,
    explicit_attempt_metadata=None,
    review_claim=None,
):
    if review_claim and node.get("kind")!="review-worker":
        raise ValueError("reviewer-claim-on-non-review-node")
    if jobs and not attempt_id:
        # The registry names the attempt when exactly one registered attempt belongs to this node;
        # none, or several, is a choice only the caller can make.
        attempt_id=_sole_node_attempt(route,node,Path(jobs))
        if not attempt_id:
            raise ValueError("registered completion requires --attempt-id")
    if not jobs and attempt_id and explicit_attempt_metadata is None:
        raise ValueError("unregistered completion requires explicit attempt metadata")
    if not jobs and explicit_attempt_metadata is not None and not attempt_id:
        raise ValueError(
            "explicit attempt metadata requires --attempt-id "
            "(--dispatch-depth/--transport/--execution-surface/--registered-worker/--fallback-hop "
            "describe an attempt; an inline node completed by the main session omits them all)"
        )

    jobs_path=Path(jobs) if jobs else None
    directory=completion_dir(route["route_id"])
    node_lock=directory/f".{node_id}.completion.lock"
    with _exclusive_lock(node_lock):
        if not jobs_path:
            marker=_publish_completion_locked(
                route,node,node_id,evidence,
                attempt_id=attempt_id,
                attempt_metadata=explicit_attempt_metadata,
                review_claim=review_claim,
            )
            status="unregistered-complete" if attempt_id else None
            return marker, ({"attempt_id":attempt_id,"status":status} if status else None)

        try:
            ensure_global_registry_writable(jobs_path)
        except DispatchContractError as exc:
            if explicit_attempt_metadata is not None and _unregistered_attempt(explicit_attempt_metadata):
                import route_parent_close
                try:
                    cancellation = route_parent_close.intent(route, jobs_path)
                except OSError:
                    # The cancellation ledger could not be read: keep the fail-closed row
                    # error instead of inferring "no cancellation" from a failed read.
                    _publish_completion_locked(
                        route,node,node_id,evidence,
                        attempt_id=attempt_id,
                        attempt_metadata=explicit_attempt_metadata,
                        review_claim=review_claim,
                        jobs=jobs_path,
                    )
                    raise ValueError(f"row-close-failed:{exc.reason}") from exc
                if cancellation:
                    raise ValueError("cancelled-by-parent")
                marker=_publish_completion_locked(
                    route,node,node_id,evidence,
                    attempt_id=attempt_id,
                    attempt_metadata=explicit_attempt_metadata,
                    review_claim=review_claim,
                    jobs=jobs_path,
                )
                return marker, {"attempt_id": attempt_id, "status": "unregistered-complete"}
            if explicit_attempt_metadata is not None:
                _publish_completion_locked(
                    route,node,node_id,evidence,
                    attempt_id=attempt_id,
                    attempt_metadata=explicit_attempt_metadata,
                    review_claim=review_claim,
                    jobs=jobs_path,
                )
            raise ValueError(f"row-close-failed:{exc.reason}") from exc
        with _exclusive_lock(Path(f"{jobs_path}.lock")) as jobs_lock:
            import route_parent_close
            if route_parent_close.intent(route, jobs_path):
                raise ValueError("cancelled-by-parent")
            # Existing jobs lock is the sole terminal-claim serialization point.
            ensure_terminal_claim_absent(jobs_path, route["route_id"], attempt_id)
            lines=jobs_path.read_text(encoding="utf-8",errors="replace").splitlines()
            row_index=None
            row_fields=None
            row_metadata=None
            for index,line in enumerate(lines):
                fields=line.split("\t")
                if len(fields)!=6:
                    continue
                metadata=parse_registry_metadata(fields[5])
                if metadata.get("attempt_id")==attempt_id:
                    row_index=index; row_fields=fields; row_metadata=metadata
                    break
            if row_fields is None or row_metadata is None:
                if explicit_attempt_metadata is not None:
                    marker=_publish_completion_locked(
                        route,node,node_id,evidence,
                        attempt_id=attempt_id,
                        attempt_metadata=explicit_attempt_metadata,
                        review_claim=review_claim,
                        jobs=jobs_path,
                    )
                    if _unregistered_attempt(explicit_attempt_metadata):
                        return marker, {"attempt_id": attempt_id, "status": "unregistered-complete"}
                raise ValueError(
                    f"attempt-row-absent:{attempt_id}; exact fallback attempt metadata required"
                )
            try:
                validate_attempt_metadata(row_metadata)
            except DispatchContractError as exc:
                raise ValueError(f"row-contract-invalid:{exc.reason}") from exc
            if ROUTE_AUTHORITY.subsession_row(row_metadata):
                raise ValueError("subsession-has-no-stage-gate-authority")
            if ROUTE_IDENTITY.registered_node_identity(row_metadata, node) != (
                route["route_id"], route["route_hash"], node_id
            ):
                if explicit_attempt_metadata is None and not review_claim and route.get("continuation_contract_version") == 1:
                    return _publish_continuation_owner_closure(route, node, evidence, jobs_path, attempt_id, lines)
                raise ValueError("attempt row route identity mismatch")
            if explicit_attempt_metadata is not None:
                axis_keys=(
                    "attempt_schema_version","dispatch_depth","transport",
                    "execution_surface","registered_worker","fallback_hop",
                )
                row_axes={key:str(row_metadata.get(key,"")).lower() for key in axis_keys}
                explicit_axes={key:str(explicit_attempt_metadata.get(key,"")).lower() for key in axis_keys}
                if row_axes != explicit_axes:
                    raise ValueError("explicit attempt metadata differs from canonical row")
            if row_fields[1] not in {"open","running","done"}:
                raise ValueError(f"attempt-row-terminal:{row_fields[1]}")
            already_closed=row_fields[1]=="done"
            row_note=row_metadata.get("note")
            # SD-94 — supervisor-delivered completion closes the exact row BEFORE `complete`
            # runs, so SD-70's "complete closes the row" order never happens on that path.
            # A checked supervisor terminal (note=completed-supervisor) carrying a success
            # verdict (failure_class=pass) is marker-eligible: publish the marker and append
            # its evidence to THIS row only, leaving the `done` status untouched. Every other
            # terminal note, and any non-pass verdict, keeps the fail-closed refusal.
            # 6번: a row the completion budget closed typed-deferred
            # (note=completion-deferred, no marker yet) is the same shape --
            # a checked writer already committed the row, only the marker
            # publication is outstanding -- so it is marker-eligible too.
            # The success judgment is the shared `verdict_pass`, not one note name. A row that already
            # carries its marker is a replay (handled below), and a `dead-*` death note is never a success.
            marker_eligible=(
                already_closed
                and row_note!="completed-marker"
                and (
                    (verdict_pass(row_metadata) and not terminal_conflict_pending(row_metadata)
                     and not str(row_note or "").startswith("dead-"))
                    or deferred_completion(row_metadata)=="pending"
                )
            )
            correction = None
            if already_closed and not marker_eligible and row_note == "dead-invalid-envelope":
                correction = _placed_artifact_false_negative(
                    route, node, row_fields, row_metadata, evidence, lines)
                marker_eligible = correction is not None
            # OPERATIONS §5.10 owner-closure extension: a review row that ended
            # `completed-review-blocking` is marker-eligible only through the
            # evidence-bound owner-closure gate; it raises its own typed refusal.
            owner_closure=None
            sealed_pipe=None
            if already_closed and row_note==REVIEW_BLOCKING_NOTE:
                owner_closure = owner_closure_plan(
                    route, node, evidence, jobs_path, attempt_id, lines=lines,
                )["closure"]
                # Seal the closure facts through the one sanitizing writer
                # every other terminal value uses (keys allowlisted in
                # ATTEMPT_TERMINAL_EVIDENCE_KEYS, ',' -> ';', immutability
                # checks) -- and compute it BEFORE the marker is published so a
                # refused seal publishes nothing.
                sealed_pipe = _owner_closure_sealed_pipe(row_fields[5], owner_closure)
                marker_eligible=True
            if already_closed and row_note!="completed-marker" and not marker_eligible:
                raise ValueError(
                    f"attempt-row-terminal-without-completion:{row_note or 'unknown'}"
                )
            correction_pipe = None
            if correction is not None:
                correction_pipe = _updated_attempt_metadata(row_fields[5], {
                    "prior_terminal_note": row_note,
                    "prior_classifier_source": row_metadata["classifier_source"],
                    "prior_failure_class": row_metadata["failure_class"],
                    "prior_reconcile_reason": row_metadata["reconcile_reason"],
                    "terminal_correction_b64": base64.urlsafe_b64encode(
                        json.dumps(correction, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).decode("ascii").rstrip("="),
                    "classifier_source": "recorded-bucket-move-correction-v1",
                    "failure_class": "pass",
                    "reconcile_reason": "recorded-bucket-move-corrected",
                }, terminal=True)
            attempt_metadata={
                key:value for key,value in parse_registry_metadata(correction_pipe or row_fields[5]).items()
                if not key.startswith("_")
            }
            marker=_publish_completion_locked(
                route,node,node_id,evidence,
                attempt_id=attempt_id,
                attempt_metadata=attempt_metadata,
                require_existing_link=already_closed and not marker_eligible,
                review_claim=review_claim,
                jobs=jobs_path,
                # SD-94 owner-closure: the row IS a review worker's, so the
                # ordinary rules would call this gate `independent` -- the exact
                # inversion of what happened, which is that the owner ruled over
                # a review that returned FAIL.
                owner_override=owner_closure is not None,
                expected_evidence_sha256=(
                    correction["current_content_digest"] if correction is not None else None
                ),
            )
            if already_closed and not marker_eligible:
                return marker, {"attempt_id":attempt_id,"status":"already-closed"}

            canonical_marker_path=directory/f"{node_id}.json"
            history_marker_path=directory/f"{node_id}.{marker['sequence']}.json"
            # SD-94: `done` for a marker-eligible row is a no-op re-assert, never a re-close —
            # the supervisor's own terminal stays the row's closing act, and the appended
            # note/marker evidence only records that the marker now exists. A duplicate
            # `complete` then reads note=completed-marker (last value wins) and returns the
            # idempotent already-closed path instead of appending twice.
            row_fields[1]="done"
            if correction_pipe is not None:
                row_fields[5] = correction_pipe
            if sealed_pipe is not None:
                # The gate closed on the owner's ruling, not on a passing review:
                # the closure facts were sealed above and the verdict axis stays
                # untouched (never `failure_class=pass` for a FAIL review).
                row_fields[5]=sealed_pipe
            row_fields[5] += (
                f",note=completed-marker,completion_marker={canonical_marker_path}"
                f",completion_marker_history={history_marker_path}"
            )
            # SD-OPEN-41(b): the row carries the same verdict-provenance axes the
            # marker does, so a consumer reading the registry alone (Fleet, the
            # reconcile carrier, a report) sees a degraded review without opening
            # the marker. Only the short enum tokens travel here -- the reviewer's
            # path-shaped identity stays in the marker, out of a comma-delimited
            # field. `note` is deliberately still `completed-marker`: two gates
            # read that exact literal to mean "this row terminated with a marker"
            # (`dispatch_contract.marker_attempt_readiness` and this function's
            # own already-closed branch), so spelling the degradation into `note`
            # would make an idempotent second `complete` refuse the very row it
            # had just closed.
            for key in ("reviewer_kind","review_independence",
                        "reviewer_downgrade_reason","review_gate_closure"):
                if marker.get(key):
                    row_fields[5] += f",{key}={marker[key]}"
            # DR-1: seal the pass verdict alongside the marker so partial-continuation
            # peer checks see immutable terminal success without re-deriving it.
            if owner_closure is None and row_metadata.get("failure_class") in (None,"","-"):
                row_fields[5] += ",failure_class=pass"
            if not already_closed:
                # SD-111 (§4.3.1): this is the row's one open|running -> done
                # edge, so the delivery-intent stamp belongs here, inside the
                # same registry lock, exactly like the supervisor-closed edges.
                stamped=parse_registry_metadata(row_fields[5])
                cleanup=_terminal_cleanup_values(stamped)
                if cleanup:
                    row_fields[5] = _updated_attempt_metadata(row_fields[5], cleanup, terminal=True)
                    stamped=parse_registry_metadata(row_fields[5])
                intent=_delivery_intent_values(row_fields,stamped)
                if intent:
                    row_fields[5] += "".join(f",{key}={value}" for key,value in intent.items())
            lines[row_index]="\t".join(row_fields)
            _atomic_registry_replace(jobs_path,lines)
            return marker, {
                "attempt_id":attempt_id,
                "status":"marker-appended" if marker_eligible else "closed",
                **({"gate_closure":"owner-closure",
                    "blocking_attempts":owner_closure["blocking_attempts"]}
                   if owner_closure is not None else {}),
            }

def _git_changed_files(worktree, *, route=None):
    """Return the worktree's git-visible changed file paths (AC 28 audit)."""
    if route is not None and route.get("source_commit") == "unversioned":
        raise ValueError("git-change-observation-not-applicable:unversioned-route")
    try:
        result = subprocess.run(
            # `-uall`: the default collapses an untracked directory to one
            # `dir/` entry, which can never match a `fixed_files` path and made
            # every slice that created a new directory look like an escape.
            # The audit compares files, so it has to be given files.
            ["git", "-C", str(worktree), "status", "--porcelain", "-uall"],
            text=True, capture_output=True, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, ValueError):
        return set()
    changed = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        # status code is the first two chars; a rename shows 'R  old -> new'
        rest = line[3:].strip()
        path = rest.split(" -> ")[-1].strip()
        if path:
            changed.add((Path(worktree) / path).resolve(strict=False))
    return changed


def _artifact_output_snapshot(directory):
    """Digest regular files in one cycle output tree; never follow symlinks."""
    lexical_root = Path(directory).expanduser()
    if lexical_root.is_symlink():
        raise ValueError("artifact-snapshot-root-is-symlink")
    root = lexical_root.resolve(strict=False)
    snapshot = {}
    if not root.is_dir():
        return snapshot
    def walk_error(exc):
        raise ValueError(f"artifact-snapshot-unreadable:{exc}") from exc
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(name for name in dirs if not (Path(base) / name).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve(strict=True)
            if root not in resolved.parents:
                continue
            try:
                snapshot[str(resolved)] = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError as exc:
                raise ValueError(f"artifact-snapshot-unreadable:{resolved}:{exc}") from exc
    return snapshot


def _artifact_delta(baseline, directory):
    before = _baseline_content_map(baseline)
    after = {Path(path).resolve(strict=False): digest
             for path, digest in _artifact_output_snapshot(directory).items()}
    return {path for path in (set(before) | set(after))
            if before.get(path) != after.get(path)}


def _content_digest(path):
    """sha256 of a worktree path's bytes, or None when it is not a readable file.

    None is a real state, not an error: a path that `git status` reported as
    deleted has no content, and "still deleted" has to compare equal to itself.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _baseline_content_map(baseline):
    """The admission snapshot as {resolved path: content digest at admission}.

    Records written before this became a content snapshot carry a bare list of
    paths. Those are read back as "unknown digest" so they keep the behaviour
    they were written under instead of being silently re-judged.
    """
    changed = (baseline or {}).get("changed_files") or {}
    if isinstance(changed, dict):
        return {
            Path(path).resolve(strict=False): digest
            for path, digest in changed.items()
        }
    return {Path(path).resolve(strict=False): _LEGACY_BASELINE_DIGEST for path in changed}


_LEGACY_BASELINE_DIGEST = "legacy-path-only-baseline"


def _published_owner_chain_marker(route, node, node_id, evidence, *, attempt_id, attempt_metadata):
    """Return the canonical marker when this exact aggregation already published one.

    "Exact" is `write_completion_marker`'s own replay branch, called here --
    the evidence digest plus every attempt axis (which for an owner-chain gate
    includes the manifest sha256), the static route/node identity, AND the
    immutable history sibling. A different manifest, different evidence, or a
    marker written by any other authority is not a replay and falls through to
    the full audit; a marker that contradicts its own history raises here
    exactly as it does there. N2: the second half of that used to be a
    hand-copied subset, so this path resumed a gate the writer refused.

    A missing attempt axis is not a replay decision this path can make, so it
    falls through to the audit, which ends at `write_completion_marker` and the
    same refusal.
    """
    _migrate_completion_dir_forward(route["route_id"])
    try:
        axes = _marker_attempt_axes(node, attempt_id, attempt_metadata)
    except ValueError:
        return None
    return _completion_marker_replay(
        route, node, node_id, evidence, axes, completion_dir(route["route_id"])
    )


def _first_parent_descends_from(worktree, ancestor, head):
    """True when `head` reaches `ancestor` along first parents only.

    `core/OPERATIONS.md` §5.10 states the lineage proof for a declared
    sub-session chain in exactly these terms, so the stage gate asks the same
    question rather than a stricter one of its own. `merge-base --is-ancestor`
    would also accept a side branch merged in; the contract says first-parent.
    """
    if not ancestor or not head:
        return False
    try:
        probe = subprocess.run(
            ["git", "-C", str(worktree), "rev-list", "--first-parent", str(head)],
            text=True, capture_output=True, check=False,
        )
    except (OSError, ValueError):
        return False
    if probe.returncode != 0:
        return False
    return str(ancestor) in probe.stdout.split()


def _git_committed_files(worktree, ancestor, head):
    """Paths whose content differs between two commits (AC 28/30 audit).

    A commit takes its files out of `git status`, so a gate that accepts the
    commit has to read them back out of history or it stops measuring them.
    """
    if not ancestor or not head:
        return set()
    try:
        probe = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", str(ancestor), str(head)],
            text=True, capture_output=True, check=False, timeout=60,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError(f"git-commit-observation-failed:{exc}") from exc
    if probe.returncode != 0:
        raise ValueError("git-commit-observation-failed:" + (probe.stderr or f"exit={probe.returncode}"))
    return {
        (Path(worktree) / line.strip()).resolve(strict=False)
        for line in probe.stdout.splitlines()
        if line.strip()
    }


SUBDIVISION_BASELINE_SCHEMA_VERSION = 1


def subdivision_baseline_path(route_id, node_id, manifest_sha256, *, jobs=None):
    """Keyed by the manifest hash so a resumed admission finds its own baseline.

    Kept in its own subdirectory: the completion directory's own filenames are
    read back by `<node_id>.*.json` globs, and a sibling file matching that
    shape would be counted as marker history by any reader less careful than
    `_next_marker_sequence`.

    `jobs` pins the state root to the registry the caller already holds
    (SD-OPEN-49 / H8): without it the path fell back to the inherited
    `AGENT_DISPATCH_JOBS` or the per-user default, so a `dispatch-batch --jobs
    <fixture>` run wrote 220 `rt-fixture` baselines into the live
    `~/.local/state/hearting/dispatch/completion/`.
    """
    return (
        completion_dir(route_id, jobs=jobs)
        / "subdivision"
        / f"{node_id}.{str(manifest_sha256)[:32]}.json"
    )


def record_subdivision_baseline(route, node_id, manifest, *, jobs=None):
    """Snapshot the worktree at subdivision admission (anchor M3 / AC 30).

    The post-hoc diff-scope audit is a statement about what the SLICES changed,
    but `git status` reports the whole worktree. Without a start-of-subdivision
    baseline, work the stage legitimately did outside the slices' `fixed_files`
    -- its own dev log, its checklist, anything inside `write_scope` but outside
    the slice union -- is indistinguishable from a slice escaping its fence, and
    the marker is refused for changes no slice made.

    `head_commit` rides along because SD-103 makes parallel slices no-commit
    workers: index and HEAD are shared state that `fixed_files` disjointness
    cannot protect, so a moved HEAD is the evidence that some slice committed.

    Write-once and idempotent by identity, so a resumed admission with the same
    manifest recovers the original baseline instead of snapshotting the
    half-finished worktree as if it were the start state.
    """
    digest = manifest["_manifest_sha256"]
    worktree = Path(manifest["worktree"])
    path = subdivision_baseline_path(route["route_id"], node_id, digest, jobs=jobs)
    identity = {
        "schema_version": SUBDIVISION_BASELINE_SCHEMA_VERSION,
        "route_id": route["route_id"],
        "route_hash": route["route_hash"],
        "node_id": node_id,
        "manifest_sha256": digest,
        "worktree": str(worktree),
    }
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if {key: existing.get(key) for key in identity} != identity:
            raise ValueError(f"subdivision-baseline-identity-conflict:{node_id}")
        return existing
    from datetime import datetime, timezone
    unversioned = route.get("source_commit") == "unversioned"
    artifact_output = (os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR")
                       or str(route.get("artifact_root") or ""))
    record = {
        **identity,
        "head_commit": None if unversioned else _head_commit(worktree),
        # anchor M3 / B5: path -> content digest, not a path list. A path list
        # is a permanent exemption: a file dirty at admission became invisible
        # to the audit for the whole subdivision, and those are exactly the
        # files the baseline exists to excuse (the stage's own dev log and
        # checklist), so in practice they are always dirty. Digests make the
        # subtraction a real delta -- unchanged since admission stays exempt,
        # changed again does not.
        "artifact_output_dir": artifact_output if unversioned else None,
        "changed_files": (_artifact_output_snapshot(artifact_output) if unversioned else {
            str(item): _content_digest(item)
            for item in sorted(_git_changed_files(worktree, route=route))
        }),
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        write_once(path, record)
    except ValueError:
        # A concurrent admission won the race with byte-different content
        # (differing `recorded_at`); its record is equally valid as the start
        # state, so adopt it rather than failing the admission.
        return json.loads(path.read_text(encoding="utf-8"))
    return record


def load_subdivision_baseline(route, node_id, manifest, *, jobs=None):
    """Resume the admission-time baseline by manifest hash; None when absent.

    Read across every dispatch state root, the same order completion markers use.
    Reading only the canonical root meant a state-root rotation kept the markers
    (which iterate the roots, and have `_migrate_completion_dir_forward`) while
    losing the baseline, and for a parallel subdivision a missing baseline is a
    permanent `subdivision-baseline-missing`. The writer still uses one root.
    """
    digest = manifest["_manifest_sha256"]
    canonical = subdivision_baseline_path(route["route_id"], node_id, digest, jobs=jobs)
    candidates = [canonical] + [
        root / "completion" / route["route_id"] / "subdivision" / canonical.name
        for root in dispatch_state_roots(resolve_agent_home(), jobs)
    ]
    path = next((item for item in candidates if item.is_file()), canonical)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        record.get("route_id") != route.get("route_id")
        or record.get("route_hash") != route.get("route_hash")
        or record.get("node_id") != node_id
        or record.get("manifest_sha256") != manifest["_manifest_sha256"]
    ):
        return None
    return record


def complete_subsession_stage(route, node, node_id, evidence, manifest_path, jobs):
    """Aggregate exact PASS sub-sessions into the route node's one stage marker."""

    # The manifest itself binds the actual route file; compare id/hash/node/gate
    # here, then use its immutable digest as marker identity.
    manifest=load_manifest(manifest_path,node=node)
    if (
        manifest.get("route_id") != route.get("route_id")
        or manifest.get("route_hash") != route.get("route_hash")
        or Path(manifest["route_file"]).resolve() != Path(route.get("_route_file", manifest["route_file"])).resolve()
    ):
        raise ValueError("subsession manifest route identity mismatch")
    jobs_path=Path(jobs)
    if not jobs_path.is_file():
        raise ValueError("subsession registry missing")
    rows={}
    for line in jobs_path.read_text(encoding="utf-8",errors="replace").splitlines():
        fields=line.split("\t")
        if len(fields)!=6:
            continue
        metadata=parse_registry_metadata(fields[5])
        attempt_id=metadata.get("attempt_id")
        if attempt_id:
            rows.setdefault(attempt_id,[]).append((fields,metadata))
    from dispatch_replacement_subsession import project
    projected, effective_attempts = project(jobs_path, manifest, [
        {"fields": fields, "metadata": metadata, "status": fields[1]}
        for matches in rows.values() for fields, metadata in matches
        if metadata.get("session_chain_id") == manifest["chain_id"]
    ])
    rows = {}
    for item in projected:
        rows.setdefault(item["metadata"]["attempt_id"], []).append((item["fields"], item["metadata"]))
    for session in manifest["sessions"]:
        matches=rows.get(effective_attempts[session["attempt_id"]],[])
        if len(matches)!=1:
            raise ValueError(f"subsession attempt row count invalid:{session['attempt_id']}:{len(matches)}")
        fields,metadata=matches[0]
        validate_attempt_metadata(metadata)
        expected={
            "route_id":route["route_id"], "route_node":node_id,
            "subsession_id":session["subsession_id"],
            "session_chain_id":manifest["chain_id"], "stage_authority":"0",
        }
        if any(str(metadata.get(key,""))!=str(value) for key,value in expected.items()):
            raise ValueError(f"subsession attempt identity mismatch:{session['attempt_id']}")
        if (
            fields[1]!="done"
            or not success_note(metadata)
            or not verdict_pass(metadata)
        ):
            raise ValueError(f"subsession not semantic PASS:{session['attempt_id']}")
        # An exact terminal gate like the other completion gates: the row was just
        # proven a done PASS, so its complete post-exit receipt also settles a slice
        # that ran in a PID namespace this observer can no longer scan (a codex
        # owner's command sandbox), the case the receipt exists for.
        process=attempt_process_quiescence(metadata,terminal_receipt=True)
        if process.state!="quiescent":
            raise ValueError(f"subsession process not quiescent:{session['attempt_id']}:{process.reason}")
    # AC 28 post-hoc diff-scope audit, measured against the subdivision's own
    # start state (anchor M3). `git status` sees the whole worktree, so the
    # audit subtracts the admission-time baseline first; what remains is what
    # the slices actually did, which is the only thing their `fixed_files`
    # fence can be held to. Without a baseline the measurement is not slice
    # attribution at all, so its absence fails closed rather than silently
    # widening the audit back to the whole worktree.
    worktree = Path(manifest["worktree"])
    baseline = load_subdivision_baseline(route, node_id, manifest, jobs=jobs)
    digest = manifest["_manifest_sha256"]
    attempt_id = "att-stage-" + digest[:32]
    metadata = {
        "stage_authority": "owner-chain",
        "subsession_manifest": str(Path(manifest_path).resolve()),
        "subsession_manifest_sha256": digest,
        "session_chain_id": manifest["chain_id"],
    }
    # AC 30 resume. The audit below measures a mutation window that CLOSED when
    # this exact aggregation published its marker, and SD-103 has the owner
    # commit after that gate -- so re-measuring a worktree that has legitimately
    # moved on since would refuse a gate that is already closed, permanently,
    # against a write-once baseline and an unrewindable HEAD. An idempotent
    # replay of an already-published marker therefore returns it. This weakens
    # nothing: no audit run after publication can un-publish the marker, and the
    # identity below is the same exact one `write_completion_marker` would
    # require to treat the call as a replay rather than a new gate.
    published = _published_owner_chain_marker(
        route, node, node_id, evidence, attempt_id=attempt_id, attempt_metadata=metadata
    )
    if published is not None:
        return published, {
            "status": "stage-gate-aggregated",
            "sessions": len(manifest["sessions"]),
            "resumed": True,
        }

    def _refuse(reason, detail):
        record_degradation(
            route_id=route.get("route_id"), route_node=node_id,
            route_hash=route.get("route_hash"), dispatch_depth=2,
            fallback_hop=None, execution_surface="registered-headless",
            writer="capability-route.py", kind="degradation",
            reason=reason, detail=detail[:512],
            slice_manifest_sha256=manifest["_manifest_sha256"],
        )
        raise ValueError(reason)

    # The baseline is required for a PARALLEL subdivision -- SD-103's admission
    # path records one, and its absence there means the audit would not be slice
    # attribution at all. A `serial` SD-96 chain is admitted through a different
    # path that records no baseline, so demanding one would make every serial
    # chain uncompletable; it keeps the pre-existing whole-worktree measurement
    # instead. That residual is real and is recorded as such: the serial path's
    # audit still cannot attribute a change to a session.
    parallel = manifest.get("mode") == "parallel"
    if baseline is None and parallel:
        _refuse(
            "subdivision-baseline-missing",
            f"no admission baseline for manifest {manifest['_manifest_sha256'][:16]}",
        )
    declared_union = {
        Path(path).resolve(strict=False)
        for session in manifest["sessions"]
        for path in session["fixed_files"]
    }
    # AC 30: parallel slices are no-commit workers (SD-103). index and HEAD are
    # shared state that `fixed_files` disjointness cannot protect, so a slice
    # that commits is a real integrity break.
    #
    # "HEAD moved at all" is a stricter proposition than the one this repo's own
    # contract states, and it is the wrong one. `core/OPERATIONS.md` §5.10
    # already accepts first-parent descendant HEAD movement during a declared
    # sub-session chain under the same lineage proof as an in-place retry, and
    # SD-103 makes the owner commit once after quiescence. Judging by movement
    # alone therefore refused the owner's OWN commit -- and with a write-once
    # baseline and an unrewindable HEAD that refusal had no recovery path.
    #
    # So the judgement is lineage first, then content: history that is not a
    # first-parent descendant of the baseline commit was rewound or diverged and
    # is refused outright, and a lineage-clean descent is a slice commit only
    # when it actually carries a slice's `fixed_files`.
    committed = set()
    unversioned = route.get("source_commit") == "unversioned"
    if baseline is not None and not unversioned:
        head = _head_commit(worktree)
        baseline_head = baseline.get("head_commit")
        if head != baseline_head:
            if not _first_parent_descends_from(worktree, baseline_head, head):
                _refuse(
                    "subdivision-commit-attempted",
                    f"head {baseline_head} -> {head} is not a first-parent descendant",
                )
            committed = _git_committed_files(worktree, baseline_head, head)
            slice_commits = sorted(committed & declared_union)
            if slice_commits:
                _refuse(
                    "subdivision-commit-attempted",
                    f"head {baseline_head} -> {head} carries "
                    + ";".join(str(path) for path in slice_commits),
                )
    # Exempt a baseline-dirty path only while its CONTENT still matches the
    # admission snapshot. Subtracting the path itself would excuse every later
    # change to that file too, which is why AC 28 did not hold for the stage's
    # own artifacts. A record written before the baseline carried digests keeps
    # its original path-set meaning rather than being re-judged retroactively.
    preexisting = {
        path
        for path, digest in _baseline_content_map(baseline).items()
        if digest == _LEGACY_BASELINE_DIGEST or _content_digest(path) == digest
    }
    # A lineage-clean commit moves its files out of `git status` and into
    # history, so the audit has to add them back or accepting the commit would
    # silently blind the very measurement it just passed.
    declared_artifact_outputs = set()
    artifact_output_dir = None
    if unversioned:
        artifact_output_dir = (
            (baseline or {}).get("artifact_output_dir")
            or os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR")
        )
        if not artifact_output_dir:
            _refuse("subdivision-baseline-missing", "unversioned artifact output directory unavailable")
        output_root = Path(artifact_output_dir).resolve(strict=False)
        for session in manifest["sessions"]:
            report_identity = (
                session.get("gap_retry_of") or session.get("continues")
                or session["subsession_id"]
            )
            report_path = report_path_from_brief(
                Path(session["phase_brief"]).read_text(encoding="utf-8"),
                session_id=report_identity, route_node=node_id, node=node,
            )
            if not report_path:
                continue
            report = output_root / report_path
            resolved_report = report.resolve(strict=False)
            if resolved_report != report:
                _refuse("subdivision-scope-violation", f"report path uses a symlink: {report_path}")
            try:
                resolved_report.relative_to(output_root)
            except ValueError:
                _refuse("subdivision-scope-violation", f"report path escaped cycle output: {report_path}")
            declared_artifact_outputs.add(resolved_report)
        changed = _artifact_delta(baseline, artifact_output_dir)
    else:
        changed = _git_changed_files(worktree, route=route) | committed
    outside = changed - declared_union - declared_artifact_outputs - preexisting
    if outside:
        _refuse(
            "subdivision-scope-violation",
            ";".join(str(path) for path in sorted(outside)),
        )
    # The arbiter gate has two marker writers, and this is the second one:
    # `_publish_completion_locked` calls this, and until now this path reached
    # `write_completion_marker` directly. No SD-103 node currently arbitrates any
    # group, so nothing escapes today -- but a gate that lives at one of two
    # entrances is not a gate. One defensive call closes it.
    _validate_auxiliary_arbiter(route, node, evidence)
    directory=completion_dir(route["route_id"])
    with _exclusive_lock(directory/f".{node_id}.completion.lock"):
        marker=write_completion_marker(
            route,node,node_id,evidence,
            attempt_id=attempt_id,attempt_metadata=metadata,
            owner_chain=True,
            _lock_held=True,
        )
    _launch_open_cycle_checkpoint(route)
    return marker,{"status":"stage-gate-aggregated","sessions":len(manifest["sessions"])}
