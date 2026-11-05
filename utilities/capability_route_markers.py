"""Capability route: completion markers and review identity.

Marker axes, review identity, marker replay, review self-disposition,
`write_completion_marker`, review-input revisions, and review lineage.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import route_identity as ROUTE_IDENTITY
import review_round_cap as REVIEW_ROUND_CAP
import route_authority as ROUTE_AUTHORITY
from dispatch_contract import (
    row_is_subsession, DispatchContractError, agent_home_equivalent, terminal_conflict_pending,
    evidence_digest, evidence_change_history_path, _evidence_mtime_text, evidence_currency,
    gate_currency, original_review_self_claim, review_self_disposition_origin,
    review_self_disposition_subject, review_self_disposition_lineage, parse_registry_metadata,
    resolve_agent_home, validate_attempt_metadata,
)
from codex_dispatch_terminal import REVIEW_BLOCKING_NOTE, inspect_terminal_attempt

from capability_route_base import (
    _attempt_completion_path, _exclusive_lock, _realized_auxiliary_nodes, _realized_group_ids,
    _resolve_auxiliary_arbiter, _review_round_rows, atomic_write, AUXILIARY_ARBITER_NODE,
    canonical, completion_dir, DISPATCH_CONTRACT_VERSION, review_round_records,
    RouteLineageError, verified_route_lineage, write_once,
)

from capability_route_continuation import (
    _continuation_contract_hash, _continuation_source_jobs, _registered_marker_fence,
    _sha256_record, _source_evidence_snapshot, verified_ancestor_plan_refresh,
)

from capability_route_dispatch import (
    _fallback_chain,
)

from capability_route_compose import (
    _compose_default_jobs,
)


def _rewrite_migrated_attempt_links(directory, old, new):
    """Re-anchor the self-referential absolute paths inside migrated
    `<node>.<attempt>.attempt.json` sidecars to the directory they now live
    in. The sidecar records its own location (`completion_marker`,
    `completion_marker_history`) and readers verify that identity, so a
    byte-for-byte copy at a new root would evaluate as missing (review F-1).
    Only those two keys are rewritten; everything else stays byte-identical,
    and the origin directory is never touched (design constraint 7)."""

    old_prefix = str(old)
    new_prefix = str(new)
    for link_path in directory.glob("*.attempt.json"):
        try:
            link = json.loads(link_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for key in ("completion_marker", "completion_marker_history"):
            value = link.get(key)
            if isinstance(value, str) and value.startswith(old_prefix):
                link[key] = new_prefix + value[len(old_prefix):]
                changed = True
        if changed:
            # Match write_once's serialization exactly (review N-5): a
            # re-publish after migration compares this sidecar's bytes
            # against a fresh write_once() call, which would hard-fail on
            # any formatting drift even though the content is identical.
            link_path.write_text(
                json.dumps(link, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )


def _migrate_completion_dir_forward(route_id, *, jobs=None):
    """One-time, idempotent, origin-preserving copy of a legacy
    agent-home-relative completion dir into the canonical dispatch state
    root, so a route that started writing before this cycle's resolver
    unification keeps its marker/history reachable at the new root the
    writer now uses exclusively (design constraint 3 / 7). The copied
    attempt sidecars are re-anchored to the new root before the directory
    becomes visible; the origin stays byte-identical."""

    agent_home = resolve_agent_home()
    new = completion_dir(route_id, jobs=jobs)
    old = agent_home/".dispatch"/"completion"/route_id
    if new.is_dir() or not old.is_dir() or new == old:
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    tmp = new.parent/f".migrate-{route_id}.{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(old, tmp)
        _rewrite_migrated_attempt_links(tmp, old, new)
        os.rename(tmp, new)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)

def _marker_attempt_axes(node, attempt_id, attempt_metadata):
    if attempt_metadata is not None and attempt_metadata.get("stage_authority") == "owner-closure":
        # The attempt names the historical reviewer, not a newly launched
        # continuation worker. The common proof reader verifies this provenance.
        return {
            "attempt_id": attempt_id, "dispatch_depth": node.get("dispatch_depth"),
            "transport": "headless", "execution_surface": "inline",
            "registered_worker": False, "fallback_hop": "inline",
            "stage_authority": "owner-closure",
            "owner_closure_proof": attempt_metadata["owner_closure_proof"],
        }
    if node.get("kind") == "resource-runner":
        if attempt_id or attempt_metadata:
            raise ValueError("resource completion cannot carry agent attempt axes")
        return {
            "attempt_id":None,
            "dispatch_depth":None,
            "transport":None,
            "execution_surface":None,
            "registered_worker":False,
            "fallback_hop":None,
        }
    if attempt_metadata is not None and attempt_metadata.get("stage_authority") == "owner-chain":
        if not attempt_id or not attempt_metadata.get("subsession_manifest"):
            raise ValueError("owner-chain completion identity incomplete")
        return {
            "attempt_id":attempt_id,
            "dispatch_depth":node.get("dispatch_depth"),
            "transport":"headless",
            "execution_surface":"inline",
            "registered_worker":False,
            "fallback_hop":"inline",
            "stage_authority":"owner-chain",
            "subsession_manifest":attempt_metadata["subsession_manifest"],
            "subsession_manifest_sha256":attempt_metadata["subsession_manifest_sha256"],
            "session_chain_id":attempt_metadata["session_chain_id"],
        }
    if attempt_metadata is None:
        if node.get("dispatch_depth") != 0 or node.get("execution_surface") != "inline":
            raise ValueError("current dispatched completion requires exact attempt metadata")
        attempt_metadata={
            "attempt_schema_version":2,
            "dispatch_depth":0,
            "transport":"interactive",
            "execution_surface":"inline",
            "registered_worker":False,
            "fallback_hop":"",
        }
    validate_attempt_metadata(attempt_metadata)
    dispatch_depth=int(attempt_metadata["dispatch_depth"])
    if dispatch_depth != node.get("dispatch_depth"):
        raise ValueError("completion attempt dispatch_depth does not match route node")
    registered=str(attempt_metadata["registered_worker"]).lower() in {"1","true"}
    return {
        "attempt_id":attempt_id,
        "dispatch_depth":dispatch_depth,
        "transport":str(attempt_metadata["transport"]),
        "execution_surface":str(attempt_metadata["execution_surface"]),
        "registered_worker":registered,
        "fallback_hop":str(attempt_metadata.get("fallback_hop") or "") or None,
    }

# SD-OPEN-41(b): the three ways a review-class node's verdict can be produced.
# `registered-worker` and `native-subagent` are independent review; `owner-inline`
# is the owner ruling on its own work and is recorded as a degraded gate, never
# refused -- refusing would deadlock every route whose review node seals
# `native-subagent` and `inline` as its last two fallback hops (SD-132's mistake).
REVIEWER_KINDS = ("registered-worker", "native-subagent", "owner-inline")


def resolve_review_identity(
    node, axes, attempt_metadata, *,
    claim=None, jobs=None, route_id=None, node_id=None, route_hash=None,
    owner_override=False, owner_chain=False,
):
    """Name who actually reviewed, for a `review-worker` node's completion marker.

    Returns `{}` for every other node kind, so no non-review marker changes shape.

    Measured 2026-09-06 over canonical markers under the dispatch state root's
    `completion/` (excluding `*.attempt.json` and `<node>.<seq>.json` history
    siblings). The total depends entirely on the predicate -- 67 for markers
    whose route record still declares `kind=review-worker` (most route records
    were pruned), 2,911 for markers whose node id merely contains "review" --
    so the count is quoted with its predicate or not at all. What is stable
    across both scopes is the shape: the axes already separate registered from
    inline, and **12** are inline (not the same 12 -- 10 overlap; one predicate
    misses `plan-check`, the other misses pruned routes). Among those inline
    ones nothing distinguished "the owner reviewed its own work" from "a native
    subagent reviewed it", and the user's rule counts the second as independent.
    Nothing bound a *registered* completer to `worker_type=review` either, so
    `registered_worker=true` was not proof that a review worker produced the
    verdict.

    A claim is adjudicated against evidence, never taken on its word:

    * `registered-worker` names another attempt; the row must exist in `jobs`
      and carry `worker_type=review`.
    * `native-subagent` names a transcript; it must be a readable regular file,
      and its sha256 is recorded so the identity is checkable later.
    * no claim: the completing attempt is the reviewer, which is independent
      only when its own row says `worker_type=review`.

    Every failed claim **downgrades** to `owner-inline` carrying a typed
    `reviewer_downgrade_reason`. Downgrade and not refusal is the whole point:
    the next node still proceeds, and the route's own outcome carries the fact
    that this gate was not independently reviewed.

    `owner_override` is the SD-94 owner-closure path: the owner ruled over a
    review that returned FAIL. That row genuinely is a review worker's, so the
    plain rules below would call it `independent` -- which is precisely
    backwards, because it is the one case where the owner overrode a real
    reviewer. It gets its own verdict.
    """

    if node.get("kind") != "review-worker":
        return {}

    if owner_override:
        return {
            "reviewer_kind": "registered-worker",
            "review_independence": "owner-overridden",
            "reviewer_identity": axes.get("attempt_id") or "-",
            "review_gate_closure": "owner-closure",
        }

    def degraded(reason):
        return {
            "reviewer_kind": "owner-inline",
            "review_independence": "degraded",
            "reviewer_identity": axes.get("attempt_id") or "-",
            "reviewer_downgrade_reason": reason,
        }

    if claim and claim.get("kind") == "registered-worker":
        reviewer_attempt = claim.get("attempt_id") or ""
        if not jobs:
            return degraded("reviewer-claim-unverifiable-no-registry")
        try:
            matches = []
            for line in Path(jobs).read_text(encoding="utf-8").splitlines():
                fields = line.split("\t")
                if len(fields) == 6:
                    metadata = parse_registry_metadata(fields[5])
                    if metadata.get("attempt_id") == reviewer_attempt:
                        matches.append(dict(metadata, _status=fields[1]))
            if len(matches) > 1:
                return degraded("reviewer-attempt-row-ambiguous")
            row = matches[0] if matches else None
        except OSError:
            return degraded("reviewer-registry-unreadable")
        if row is None:
            return degraded("reviewer-attempt-row-absent")
        if row.get("worker_type") != "review":
            return degraded("reviewer-attempt-not-review-worker")
        # A sub-session slice "must not create, claim, or satisfy the route
        # stage's completion marker" (SD-96 / OPERATIONS §5.10), and
        # `_complete_node_locked` already refuses one as the *completer*. It
        # cannot be the evidence that the marker is independent either.
        if row_is_subsession(row):
            return degraded("reviewer-attempt-subsession")
        # The job title is not the assignment. A row bound to a route must be
        # bound to THIS route and node; otherwise any review worker anywhere in
        # the registry -- or a stale id pasted from a previous cycle -- would
        # certify this gate. An SD-OPEN-40 ad-hoc reviewer is deliberately
        # route-less, and stays admissible.
        claimed_route = row.get("route_id")
        if claimed_route and (
            claimed_route != route_id or row.get("route_node") != node_id
            or route_hash and row.get("route_hash") and row["route_hash"] != route_hash
        ):
            return degraded("reviewer-attempt-foreign-route-node")
        # A reviewer that was launched and died produced no verdict. `done` plus
        # a non-`dead-*` note is the contract's own definition of "this attempt
        # finished its work"; `completed-review-blocking` stays admissible
        # because a FAIL verdict is a produced verdict.
        note = str(row.get("note") or "")
        # The registered completer publishes its verdict before its own row
        # becomes done. Naming that same authoritative row is the implicit
        # self branch, not a claim that a different reviewer already finished.
        if (reviewer_attempt and reviewer_attempt == axes.get("attempt_id")
                and axes.get("registered_worker") is True
                and claimed_route == route_id and row.get("route_node") == node_id
                and route_hash and row.get("route_hash") == route_hash
                and row.get("_status") in {"open", "running"}
                and not note.startswith("dead-")
                and not terminal_conflict_pending(row)):
            try:
                canonical_axes = _marker_attempt_axes(node, reviewer_attempt, row)
                source = attempt_metadata or {}
                bound_keys = ("attempt_id", "worker_type", "route_id", "route_hash", "route_node")
                exact = canonical_axes == axes and all(row.get(k) == source.get(k) for k in bound_keys)
            except (DispatchContractError, KeyError, TypeError, ValueError):
                exact = False
            if exact:
                return {
                    "reviewer_kind": "registered-worker",
                    "review_independence": "independent",
                    "reviewer_identity": reviewer_attempt,
                }
        if row.get("_status") != "done" or note.startswith("dead-"):
            return degraded("reviewer-attempt-no-terminal-verdict")
        return {
            "reviewer_kind": "registered-worker",
            "review_independence": "independent",
            "reviewer_identity": reviewer_attempt,
        }

    if claim and claim.get("kind") == "native-subagent":
        transcript = Path(claim.get("transcript") or "")
        try:
            readable = transcript.is_file()
            digest = (
                hashlib.sha256(transcript.read_bytes()).hexdigest() if readable else None
            )
        except OSError:
            return degraded("reviewer-transcript-unreadable")
        if not readable or digest is None:
            return degraded("reviewer-transcript-unreadable")
        return {
            "reviewer_kind": "native-subagent",
            "review_independence": "independent",
            "reviewer_identity": str(transcript.resolve(strict=False)),
            "reviewer_identity_sha256": digest,
        }

    worker_type = (attempt_metadata or {}).get("worker_type")
    if axes.get("registered_worker") and worker_type == "review":
        return {
            "reviewer_kind": "registered-worker",
            "review_independence": "independent",
            "reviewer_identity": axes.get("attempt_id") or "-",
        }
    if axes.get("registered_worker"):
        return degraded("completer-not-review-worker")
    if owner_chain:
        # An owner-chain aggregation carries no per-slice `worker_type`, so the
        # slices' own review status cannot be inherited today. Conservative and
        # named: `review-completed-inline` would misdescribe the mechanism.
        return degraded("review-completed-owner-chain")
    return degraded("review-completed-inline")


def _notify_reviewer_claim_ignored(route, node_id, existing, resolved, review_claim):
    """Say so when a replay drops a reviewer claim the caller just supplied.

    The replay path has TWO entrances -- `write_completion_marker`'s own
    `_completion_marker_replay`, and `_publish_completion_locked`'s
    existing-attempt-link branch, which reads the marker off disk and never
    calls the writer at all. The first version of this notice lived at one of
    them, so an inline re-`complete` with a valid `--reviewer-subagent`
    silently dropped the claim and printed nothing. One definition, both doors.
    """

    if not review_claim:
        return
    if all(existing.get(key) == value for key, value in resolved.items()):
        return
    print(
        "capability-route: reviewer-claim-ignored-on-replay "
        f"route_id={route['route_id']} node={node_id} "
        f"recorded={existing.get('reviewer_kind','-')} "
        f"claimed={resolved.get('reviewer_kind','-')}",
        file=sys.stderr,
    )


def _next_marker_sequence(directory, node_id):
    maximum=0
    if directory.is_dir():
        prefix=f"{node_id}."
        for path in directory.glob(f"{node_id}.*.json"):
            middle=path.name[len(prefix):-5]
            if middle.isdigit():
                maximum=max(maximum,int(middle))
    return maximum+1

def _completion_marker_replay(route, node, node_id, evidence, axes, directory, *, repair=True,
                              evidence_sha256=None, resource_execution_identity=None):
    """The one answer to "is this call a replay of the marker already on disk?".

    N2: this used to live only inside `write_completion_marker`, and the
    owner-chain resume path carried a hand-copied version of it that reproduced
    the identity FIELDS but not the history-file check below. In the state where
    the immutable history sibling is missing or has drifted, the original
    refused and the copy reported the gate resumed -- so the copy's claim to
    recognize "exactly what `write_completion_marker` recognizes" was false.
    Both callers now take this same branch, which makes that claim structural
    instead of maintained by hand.

    Returns the existing marker for a replay, `None` when this is a new gate,
    and raises when the marker on disk contradicts itself.
    """
    canonical_path=directory/f"{node_id}.json"
    recovering = not canonical_path.is_file()
    if recovering:
        sequence = _next_marker_sequence(directory, node_id) - 1
        if sequence < 1:
            return None
        candidate = directory / f"{node_id}.{sequence}.json"
    else:
        candidate = canonical_path
    existing=json.loads(candidate.read_text(encoding="utf-8"))
    identity={
        "evidence_sha256":evidence_sha256 if evidence_sha256 is not None else evidence_digest(evidence),
        "resource_execution_identity":resource_execution_identity,
        **axes,
    }
    existing_identity={
        "evidence_sha256":existing.get("evidence",{}).get("sha256"),
        "resource_execution_identity":existing.get("resource_execution_identity"),
        **{key:existing.get(key) for key in axes},
    }
    if existing_identity!=identity:
        if recovering:
            raise ValueError("completion-recovery-history-conflict")
        return None
    static_identity={
        "schema_version":2,
        "route_id":route["route_id"],
        "route_hash":route["route_hash"],
        "registry_digest":route["registry_digest"],
        "node_id":node_id,
        "completion_gate":node["completion_gate"],
    }
    if any(existing.get(key)!=value for key,value in static_identity.items()):
        raise ValueError("canonical completion marker identity conflict")
    history_path=directory/f"{node_id}.{existing.get('sequence')}.json"
    if (
        not history_path.is_file()
        or json.loads(history_path.read_text(encoding="utf-8"))!=existing
    ):
        raise ValueError("canonical completion marker history conflict")
    disposition_candidate = (node.get("kind") == "review-worker" and
                            ("reviewer_disposition" in existing or
                             existing.get("reviewer_downgrade_reason") == "reviewer-attempt-no-terminal-verdict"))
    if recovering and repair and not disposition_candidate:
        atomic_write(canonical_path, existing)
    return existing

# SD-154 A-2: `evidence_digest` moved to `dispatch_contract.py` (imported
# above) so file/directory digest identity is the same definition on both
# sides of the module boundary -- this binding keeps every existing call
# site in this file unchanged.

def _review_self_disposition_actor(route, jobs, metadata):
    actor = review_self_disposition_lineage(route, jobs, metadata)[-1]
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if caller:
        if caller != actor:
            raise ValueError("review-self-disposition-actor-mismatch")
    else:
        owner = _find_attempt_row_metadata(Path(jobs), actor) if actor else None
        sid = ROUTE_AUTHORITY.caller_identity()[1]
        if not owner:
            raise ValueError("review-self-disposition-actor-unproven")
        ROUTE_AUTHORITY.require_owner(owner, sid, jobs, "review-self-disposition-actor-unproven")
        # The actual parent can settle this existing judgment while its current
        # owner waits for an answer. This is ordinary parent authority, not
        # permission to impersonate or revive that stopped owner.
        decision = ROUTE_AUTHORITY.decide_continuation(route, action="owner", jobs=Path(jobs),
                    owner_attempt_id=actor, facts={"actor": sid})
        if ((decision.state == "answer" and decision.next_action == "correct")
                or (decision.allowed and decision.next_action == "handback")):
            from route_parent_close import owner_continuation_processes
            quiescence, children = owner_continuation_processes(owner, Path(jobs))
            if quiescence.state != "quiescent" or children:
                raise ValueError("review-self-disposition-parent-owner-not-settled")
            return actor
    ROUTE_AUTHORITY.review_owner_authority(route, jobs, actor)
    return actor


def _review_self_disposition_locked(route, node, marker, evidence, *, review_claim, jobs, check_only=False):
    """One narrow normal replay transition, under the existing completion lock.

    This is a provenance disposition of an existing judgment, never a review
    launch or a second verdict. Missing original claim keeps the old judgment.
    """
    if node.get("kind") != "review-worker":
        return marker
    directory = completion_dir(route["route_id"])
    canonical = directory / f"{node['id']}.json"
    if "reviewer_disposition" in marker:
        if not check_only and review_claim and jobs:
            if review_claim != {"kind": "registered-worker", "attempt_id": marker["attempt_id"]}:
                raise ValueError("review-self-disposition-claim-mismatch")
            meta = _find_attempt_row_metadata(Path(jobs), marker["attempt_id"])
            if meta is None:
                raise ValueError("review-self-disposition-row-not-exact")
            _review_self_disposition_actor(route, jobs, meta)
        review_self_disposition_origin(route, node, marker, canonical)
        return marker
    if (marker.get("state") == "superseded-by-upstream-revision"
            and marker.get("reviewer_downgrade_reason") == "reviewer-attempt-no-terminal-verdict"
            and review_claim == {"kind": "registered-worker", "attempt_id": marker.get("attempt_id")}):
        raise ValueError("review-self-disposition-not-current")
    if (check_only or node.get("kind") != "review-worker" or not jobs
            or marker.get("reviewer_kind") != "owner-inline"
            or marker.get("review_independence") != "degraded"
            or marker.get("reviewer_downgrade_reason") != "reviewer-attempt-no-terminal-verdict"
            or not marker.get("attempt_id") or marker.get("reviewer_identity") != marker["attempt_id"]
            or marker.get("registered_worker") is not True
            or review_claim != {"kind": "registered-worker", "attempt_id": marker["attempt_id"]}
            or any(k in marker for k in ("state", "stage_authority", "review_gate_closure"))):
        return marker
    # A surviving old link must never resurrect a superseded canonical head.
    if not canonical.is_file() or json.loads(canonical.read_text()) != marker:
        raise ValueError("review-self-disposition-not-current")
    if gate_currency(route, node, canonical, marker, observe=True, jobs=jobs).state != "current":
        raise ValueError("review-self-disposition-integrity-invalid")
    origin_path = directory / f"{node['id']}.{marker['sequence']}.json"
    authority_basis = {}
    _, meta = review_self_disposition_subject(route, node, marker, origin_path, jobs=jobs,
                                            authority_basis=authority_basis)
    basis = original_review_self_claim(route, node, marker, jobs=jobs, metadata=meta)
    if basis is None:
        print("capability-route: review-self-disposition-original-claim-unverified", file=sys.stderr)
        return marker
    actor = _review_self_disposition_actor(route, jobs, meta)
    origin_path = directory / f"{node['id']}.{marker['sequence']}.json"
    link_path = _attempt_completion_path(route, node["id"], marker["attempt_id"])
    disposition = {"kind": "registered-named-self", "of_sequence": marker["sequence"],
                   "of_marker_sha256": hashlib.sha256(origin_path.read_bytes()).hexdigest(),
                   "origin_link_sha256": hashlib.sha256(link_path.read_bytes()).hexdigest(),
                   "original_claim": basis, "jobs": str(Path(jobs)), "actor": actor,
                   "settled_basis": "successful-quiescent-terminal"}
    if authority_basis:
        disposition.update(settled_basis="historical-provenance-blocked", authority_basis=authority_basis)
    # Reuse a valid uncommitted successor left by a crash before canonical
    # replace. Conflicting history is a collision, not another allocation.
    sequence = _next_marker_sequence(directory, node["id"])
    if sequence > marker["sequence"] + 1:
        pending_path = directory / f"{node['id']}.{sequence - 1}.json"
        pending = json.loads(pending_path.read_text())
        review_self_disposition_origin(route, node, pending, pending_path)
        recorded = dict(pending["reviewer_disposition"])
        recorded.pop("recorded_at", None)
        # A replacement/current parent finishes the recorded actor's interrupted
        # publication; it does not author a new historical judgment.
        recorded["actor"] = actor
        if agent_home_equivalent(recorded.get("jobs", ""), disposition["jobs"]):
            recorded["jobs"] = disposition["jobs"]
        recorded_claim = dict(recorded["original_claim"])
        if (recorded_claim.get("source") == basis.get("source") == "runtime-command"
                and agent_home_equivalent(recorded_claim.get("log_file", ""), basis.get("log_file", ""))):
            recorded_claim["log_file"] = basis["log_file"]
            recorded["original_claim"] = recorded_claim
        if recorded != disposition:
            raise ValueError("review-self-disposition-history-collision")
        successor = pending
    else:
        disposition["recorded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        successor = dict(marker, sequence=sequence, reviewer_kind="registered-worker",
                         review_independence="independent", reviewer_disposition=disposition)
        successor.pop("reviewer_downgrade_reason", None)
        write_once(directory / f"{node['id']}.{sequence}.json", successor)
    review_self_disposition_origin(route, node, successor, canonical)
    # Recheck evidence at the commit boundary. Before it, old canonical stays;
    # after it, append-only origin/successor history is never rolled back.
    head = json.loads(canonical.read_text(encoding="utf-8"))
    if head != marker or gate_currency(
        route, node, canonical, head, observe=True, jobs=jobs,
    ).state != "current":
        raise ValueError("review-self-disposition-origin-drift")
    atomic_write(canonical, successor)
    return successor


def write_completion_marker(
    route, node, node_id, evidence, *,
    attempt_id=None, attempt_metadata=None, review_claim=None, jobs=None,
    owner_override=False, owner_chain=False, expected_evidence_sha256=None,
    resource_execution_identity=None, _lock_held=False,
):
    if not _lock_held:
        with _exclusive_lock(completion_dir(route["route_id"]) / f".{node_id}.completion.lock"):
            lock = _exclusive_lock(Path(f"{jobs}.lock")) if jobs else contextlib.nullcontext()
            with lock:
                return write_completion_marker(
                    route, node, node_id, evidence, attempt_id=attempt_id, attempt_metadata=attempt_metadata,
                    review_claim=review_claim, jobs=jobs, owner_override=owner_override,
                    owner_chain=owner_chain, expected_evidence_sha256=expected_evidence_sha256,
                    resource_execution_identity=resource_execution_identity, _lock_held=True)
    directory=completion_dir(route["route_id"])
    canonical_path=directory/f"{node_id}.json"
    if expected_evidence_sha256 is None:
        _migrate_completion_dir_forward(route["route_id"])
    sha=evidence_digest(evidence)
    if expected_evidence_sha256 is not None and "sha256:"+sha != expected_evidence_sha256:
        raise ValueError("recorded-move-evidence-drift")
    axes=_marker_attempt_axes(node, attempt_id, attempt_metadata)
    review_identity=resolve_review_identity(
        node, axes, attempt_metadata,
        claim=review_claim, jobs=jobs,
        route_id=route["route_id"], node_id=node_id,
        route_hash=route["route_hash"],
        owner_override=owner_override, owner_chain=owner_chain,
    )
    from review_source_subject import SourceSubjectError, is_implementation_review, marker_subject
    inline_review_write = (
        is_implementation_review(route, node)
        and review_identity.get("reviewer_kind") == "owner-inline"
        and axes.get("registered_worker") is False
        and axes.get("execution_surface") == "inline"
    )
    # An exact attempt replay is historical even after source drift. Every
    # writer path checks its immutable evidence/axes/history before asking for
    # a fresh source binding, so callers share the same replay decision.
    replay_candidate = _completion_marker_replay(
        route, node, node_id, evidence, axes, directory, repair=False,
        evidence_sha256=sha if expected_evidence_sha256 is not None else None,
        resource_execution_identity=resource_execution_identity,
    )
    if replay_candidate is not None:
        source_subject = None
    else:
        try:
            source_subject = marker_subject(
                route, node, attempt_id=attempt_id, attempt_metadata=attempt_metadata,
                jobs=jobs, worktree=route.get("cwd"), completion_dir=directory,
                owner_inline=inline_review_write,
                evidence_sha256=sha if inline_review_write else None,
            )
        except SourceSubjectError as exc:
            # Only an implementation review consumes its source subject as evidence.
            # Another scoped node's subject is optional epoch history: when its tree
            # cannot be observed, the marker records none (legacy-unknown).
            if (is_implementation_review(route, node) or exc.reason not in {
                    "review-source-git-unavailable", "review-source-worktree-unavailable",
                    "review-source-scope-unavailable"}):
                raise ValueError(f"{exc.reason}:{exc.detail}" if exc.detail else exc.reason) from exc
            source_subject = None
    replayed=_completion_marker_replay(
        route,node,node_id,evidence,axes,directory,
        repair=expected_evidence_sha256 is None,
        evidence_sha256=sha if expected_evidence_sha256 is not None else None,
        resource_execution_identity=resource_execution_identity,
    )
    if replayed is None and canonical_path.is_file():
        prior = json.loads(canonical_path.read_text(encoding="utf-8"))
        if "reviewer_disposition" in prior and prior.get("attempt_id") == attempt_id:
            raise ValueError("review-self-disposition-evidence-conflict")
    if expected_evidence_sha256 is not None:
        # A correction's current-manifest digest is an input to the normal
        # writer. Recheck after its read-only census/replay work, before any
        # marker, history or link can be published.
        if "sha256:"+evidence_digest(evidence) != expected_evidence_sha256:
            raise ValueError("recorded-move-evidence-drift")
        _migrate_completion_dir_forward(route["route_id"])
        if replayed is not None:
            replayed=_completion_marker_replay(
                route,node,node_id,evidence,axes,directory,
                evidence_sha256=sha,
                resource_execution_identity=resource_execution_identity,
            )
    if replayed is not None:
        replayed = _review_self_disposition_locked(
            route, node, replayed, evidence, review_claim=review_claim, jobs=jobs)
        # A replay is the same completion, so provenance is deliberately not in
        # marker identity -- but a caller that named a reviewer this time and
        # gets the old marker back deserves to be told the claim was dropped,
        # rather than reading exit 0 as "recorded".
        _notify_reviewer_claim_ignored(
            route,node_id,replayed,review_identity,review_claim,
        )
        return replayed
    # SD-153 rule 5 (13.59.2): every marker of a `ROUND_CAPPED_NODE_IDS` node
    # -- registered, owner-closure, and inline alike (`test` included, not
    # only `kind=="review-worker"`) -- carries this census. The read is
    # read-only (never used to admit or refuse this completion) so it is safe
    # to compute even on the "attempt row absent" inline path. `jobs` absent
    # or unreadable is not an exemption: an empty row set still derives a
    # real census (round_budget's own fail-soft on an unknown intensity is
    # the only `None` case) rather than the field being silently omitted.
    round_census=None
    census_subject=(source_subject or {}).get("digest")
    if (census_subject is None and owner_override and node.get("kind") == "review-worker"
            and jobs is not None):
        reviewed_input = None
        import review_input
        if review_input.is_review_node(node):
            try:
                reviewed_input = review_input.resolve_input(route, node, jobs)
            except DispatchContractError:
                if (attempt_metadata or {}).get("review_input_digest"):
                    raise
        from review_source_subject import has_review_subject, observe
        if has_review_subject(route, node, jobs, reviewed_input):
            current_subject = observe(route, node, worktree=route.get("cwd"), jobs=jobs,
                                     reviewed_input=reviewed_input)
            census_subject = current_subject["digest"]
    if REVIEW_ROUND_CAP.is_round_capped_node(node):
        rows=()
        census_jobs=Path(jobs or _compose_default_jobs())
        if census_jobs.is_file():
            lines=census_jobs.read_text(encoding="utf-8",errors="replace").splitlines()
            generations = (review_lineage_routes(route, node_id)
                          if node.get("kind") == "review-worker"
                          or node.get("completion_gate") == "code-test" else [route])
            rows = [row for generation in reversed(generations)
                    for row in _review_round_rows(lines, generation["route_id"], node_id, jobs=census_jobs)]
        if owner_override:
            site="owner-closure"
        elif axes.get("registered_worker"):
            site="registered"
        else:
            site="inline"
        revisions=()
        if site=="registered":
            # This exact write IS the completing attempt's own verdict, still
            # `open`/`running` in the registry a moment before `_complete_
            # node_locked` marks it `done` -- exclude it rather than reading
            # it "live" a beat early; `marker_round_census` adds it back as
            # the one verdict round this write itself lands. Gather the same
            # upstream revisions `admit_round` used to grant this round a
            # closure-check (13.59.3 rule 7), so the marker's label matches
            # what admission already decided.
            if attempt_id:
                rows=tuple((status,meta) for status,meta in rows if meta.get("attempt_id")!=attempt_id)
            reviewed_input = {}
            if jobs is not None and attempt_id and Path(jobs).is_file():
                import review_input
                for line in lines:
                    fields = line.split("\t")
                    if len(fields) != 6:
                        continue
                    metadata = parse_registry_metadata(fields[5])
                    if metadata.get("attempt_id") == attempt_id and metadata.get("review_input_digest"):
                        reviewed_input = review_input.read_binding(jobs, metadata)
                        break
            if census_subject is None and node.get("kind") == "review-worker" and jobs is not None and attempt_metadata:
                # A pre-deploy review carries no marker subject; its launch binding
                # names the deploy-request subject admission counted.
                from review_source_subject import is_deploy_review, read_binding as read_review_subject
                if is_deploy_review(route, node):
                    binding = read_review_subject(jobs, attempt_metadata, required=False)
                    census_subject = binding["subject_digest"] if binding else None
            if census_subject is not None and (node.get("kind") == "review-worker"
                                               or node.get("completion_gate") == "code-test") and jobs is not None:
                from review_source_subject import read_binding as read_review_subject
                bound_rows = []
                for status, metadata in rows:
                    binding = read_review_subject(jobs, metadata, required=False)
                    bound_rows.append((status, metadata if binding is None else {
                        **metadata, "_review_subject_digest": binding["subject_digest"]
                    }))
                rows = bound_rows
            revisions=_dependency_revisions(route,node,jobs,reviewed_input=reviewed_input)
        round_census=REVIEW_ROUND_CAP.marker_round_census(
            route,node,rows,site=site,revisions=revisions,
            independently_reviewed=review_identity.get("review_independence")=="independent",
            source_subject_digest=census_subject,
        )
    if REVIEW_ROUND_CAP.is_round_capped_node(node) and site == "inline":
        ROUTE_AUTHORITY.require_continuation(ROUTE_AUTHORITY.decide_continuation(
            route, action="complete-inline", node=node, rows=rows, revisions=revisions,
            facts=({"source_subject_digest": census_subject}
                   if census_subject is not None else None)))
    sequence=_next_marker_sequence(directory,node_id)
    if expected_evidence_sha256 is not None and "sha256:"+evidence_digest(evidence) != expected_evidence_sha256:
        raise ValueError("recorded-move-evidence-drift")
    marker={
        "schema_version":2,
        "route_id":route["route_id"],"route_hash":route["route_hash"],
        "registry_digest":route["registry_digest"],"node_id":node_id,
        **axes,
        # SD-OPEN-41(b). Deliberately NOT part of marker identity
        # (`_completion_marker_replay` compares `axes` + evidence): who reviewed
        # is a fact recorded about a completion, not a second thing that has to
        # match for a replay to be the same completion. Empty for every node
        # kind but `review-worker`.
        **review_identity,
        **({"reviewer_claim": dict(review_claim)}
           if node.get("kind") == "review-worker" and review_claim else {}),
        **({"round_census":round_census} if round_census else {}),
        "completion_gate":node["completion_gate"],
        "evidence":{"path":str(evidence),"sha256":sha},
        "sequence":sequence,
        **({"resource_execution_identity":resource_execution_identity}
           if resource_execution_identity is not None else {}),
    }
    if source_subject is not None:
        marker["source_subject"] = source_subject
    from datetime import datetime, timezone
    marker["completed_at"]=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    while True:
        history_path=directory/f"{node_id}.{sequence}.json"
        try:
            write_once(history_path, marker)
        except ValueError:
            sequence+=1; marker["sequence"]=sequence; continue
        break
    atomic_write(canonical_path, marker)
    return marker

def _find_attempt_row_metadata(jobs, attempt_id):
    if not jobs.is_file(): return None
    for line in jobs.read_text(encoding="utf-8",errors="replace").splitlines():
        fields=line.split("\t")
        if len(fields)!=6: continue
        metadata=parse_registry_metadata(fields[5])
        if metadata.get("attempt_id")==attempt_id:
            metadata["_status"]=fields[1]
            return metadata
    return None

def _parse_auxiliary_findings(evidence: Path):
    """Extract `auxiliary_findings_considered` from JSON or markdown frontmatter.

    A review unit's sealed output is a markdown review file, not a JSON verdict;
    the anchor of an auxiliary-bearing group records which auxiliary findings it
    considered in that file's frontmatter (G1). Returns None when the field is
    absent from both surfaces.
    """
    try:
        text = evidence.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
        considered = payload.get("auxiliary_findings_considered")
        if isinstance(considered, list):
            return considered
    except (ValueError, TypeError):
        pass
    match = re.match(r"\A---\n(.*?\n)---\n", text, re.DOTALL)
    if not match:
        return None
    block = match.group(1)
    inline = re.search(
        r"^auxiliary_findings_considered:\s*\[([^\]]*)\]", block, re.MULTILINE
    )
    if inline:
        return [
            token.strip()
            for token in inline.group(1).split(",")
            if token.strip()
        ]
    found = re.search(
        r"^auxiliary_findings_considered:\s*(?:#.*)?$", block, re.MULTILINE
    )
    if found:
        items = []
        for line in block[found.end():].lstrip("\n").splitlines():
            item = re.match(r"^\s+-\s+(.*?)\s*$", line)
            if not item:
                break
            items.append(item.group(1))
        if items:
            return items
    return None
ARBITRATION_SCHEMA_VERSION = 1


def _auxiliary_groups_arbitrated_by(route, node_id):
    """(group ids, required considered-entry count) for one node arbiter.

    A single node can arbitrate more than one group, so the required length is
    the SUM of those groups' realized auxiliary legs, not any one group's count.

    A group whose arbiter cannot be resolved is skipped rather than raised
    through: its arbiter is unknown, so it is not arbitrated by THIS node or by
    any other, and letting the error out here made one group's declaration
    error refuse the completion of every unrelated node on the route. The
    read-only observer (`owner_merge_auxiliary_groups`) already degrades those
    groups to a failing row, and both gates surface them as
    `auxiliary-arbiter-unresolved`; the asymmetry between the reader and the
    writer was the defect.
    """
    groups = []
    required = 0
    for group_id in _realized_group_ids(route):
        auxiliary = _realized_auxiliary_nodes(route, group_id)
        if not auxiliary:
            continue
        try:
            kind, arbiter = _resolve_auxiliary_arbiter(route, group_id)
        except ValueError:
            continue
        if kind == AUXILIARY_ARBITER_NODE and arbiter == node_id:
            groups.append(group_id)
            required += len(auxiliary)
    return groups, required


def _validate_auxiliary_arbiter(route, node, evidence):
    """AC 5 (front half): the arbiter verdict of an auxiliary-bearing group must
    carry `auxiliary_findings_considered` with exactly one entry per realized
    auxiliary leg; otherwise the completion gate is not met.

    Only a *node* arbiter is gated here (see `_resolve_auxiliary_arbiter`). The
    anchor is never gated by being the anchor -- it is a concurrent sibling of
    the auxiliary leg. Owner-merge arbitration is a separate transaction
    (`arbitrate`) that can only run after the group has joined.

    The evidence surface is the sealed output -- a review unit's markdown file --
    so the list is read from JSON or from markdown frontmatter, not forced to JSON.
    """
    groups, required = _auxiliary_groups_arbitrated_by(route, node.get("id"))
    if not groups:
        return
    considered = _parse_auxiliary_findings(evidence)
    if considered is None:
        raise ValueError(
            f"auxiliary arbiter gate {node.get('id')} requires "
            "auxiliary_findings_considered in evidence or frontmatter"
        )
    if len(considered) != required:
        raise ValueError(
            f"auxiliary arbiter gate {node.get('id')} requires "
            f"auxiliary_findings_considered length {required}, got {len(considered)}"
        )


def _downstream_node_ids(route, node_id):
    """Every node reachable from `node_id` by `depends_on` edges, transitively.

    SD-154 rule 4: a revision reopens every downstream gate, not only the
    direct dependent -- a plan revision has to reopen `execute` even though
    `execute` depends on `plan-check`, not `plan`, directly.
    """
    result = set()
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        for candidate in route.get("nodes", []):
            candidate_id = candidate.get("id")
            if candidate_id in result:
                continue
            if current in (candidate.get("depends_on") or []):
                result.add(candidate_id)
                frontier.append(candidate_id)
    return result


def _revision_consumers(route, upstream_id, revision, *, directory, registry, restoring=None):
    """Prove registered downstream attempts started after the upstream observed N."""
    try:
        route_id = route["route_id"]
        upstream = next(n for n in route.get("nodes", []) if n.get("id") == upstream_id)
        p = revision["of_sequence"]
        old_sha, new_sha = revision["of_evidence_sha256"], revision["evidence_sha256"]
        history_path = evidence_change_history_path(Path(directory) / f"{upstream_id}.json", upstream_id)
        history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        if any(not isinstance(row, dict) for row in history):
            return {}
        changes = [row for row in history if row.get("marker_sequence") == p
                   and row.get("route_id") == route_id and row.get("node_id") == upstream_id]
        if not changes or changes[0].get("previous_sha256") != old_sha:
            return {}
        previous = old_sha
        for row in changes:
            if row.get("previous_sha256") != previous or row.get("evidence_path") != revision["evidence_path"]:
                return {}
            previous = row.get("sha256")
        if changes[-1].get("change_kind") != "edited" or previous != new_sha:
            return {}
        # The start is tied to the first observation in the final consecutive N run.
        start = len(changes) - 1
        while (start > 0 and changes[start - 1].get("sha256") == new_sha
               and changes[start - 1].get("change_kind") == "edited"):
            start -= 1
        observed = changes[start].get("observed_at")
        try:
            observed_dt = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            evidence_path = Path(str(revision["evidence_path"]))
            if not evidence_path.is_file() or _evidence_mtime_text(evidence_path) != changes[-1].get("evidence_changed_at"):
                return {}
        except (OSError, ValueError, TypeError):
            return {}
        if registry is None:
            registry = _continuation_source_jobs(route)
        lines = Path(registry).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration, DispatchContractError):
        return {}

    downstream = _downstream_node_ids(route, upstream_id)
    nodes = {str(n.get("id")): n for n in route.get("nodes", [])}
    parsed = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        try:
            meta = parse_registry_metadata(fields[5])
        except (ValueError, TypeError):
            continue
        parsed.append((fields, meta))
    memo = {}
    active = set()

    def prove(node_id):
        if node_id == upstream_id:
            return {"upstream": upstream_id, "upstream_sequence": revision.get("upstream_sequence", p),
                    "history_sha256": new_sha, "history_observed_at": observed,
                    "attempt_id": None, "launched_at": observed}
        if node_id in memo:
            return memo[node_id]
        if node_id in active or node_id not in downstream or node_id not in nodes:
            return None
        active.add(node_id)
        node = nodes[node_id]
        canonical_path = Path(directory) / f"{node_id}.json"
        try:
            marker = json.loads(canonical_path.read_text(encoding="utf-8"))
            restored_revision = marker.get("revision") if isinstance(marker.get("revision"), dict) else {}
            consumed = restored_revision.get("consumed") if isinstance(restored_revision.get("consumed"), dict) else {}
            if (restoring is not None and marker.get("stage_authority") == "revision"
                    and restored_revision.get("basis") == "upstream-revision-consumed"
                    and consumed.get("upstream") == restoring.get("node")
                    and consumed.get("upstream_sequence") == restoring.get("sequence")
                    and gate_currency(route, node, canonical_path, marker, observe=True,
                                      jobs=registry).state == "current"):
                proof = consumed
                memo[node_id] = proof
                return proof
            if (marker.get("registered_worker") is not True or marker.get("stage_authority") is not None):
                return None
            is_restore_tombstone = marker.get("state") == "superseded-by-upstream-revision"
            if is_restore_tombstone:
                if restoring is None or marker.get("superseded_by") != restoring:
                    return None
            elif marker.get("state") is not None:
                return None
            attempt_id = marker.get("attempt_id")
            matching = [(fields, meta) for fields, meta in parsed if meta.get("attempt_id") == attempt_id]
            if len(matching) != 1:
                return None
            fields, meta = matching[0]
            if ROUTE_IDENTITY.registered_node_identity(meta, node) != (route_id, route.get("route_hash"), node_id):
                return None
            launched = datetime.fromisoformat(fields[0].replace("Z", "+00:00"))
            if launched < observed_dt or _registered_marker_fence(route, node, marker, lines) is not None:
                return None
            if is_restore_tombstone:
                currency = gate_currency(route, node, canonical_path, marker, observe=True, jobs=registry)
                if currency.state != "superseded":
                    return None
                # A tombstone copies its predecessor. Read that original marker's
                # evidence and source half through the one shared reader rather
                # than recomputing its digest here (A-SD154-8).
                from dispatch_contract import _superseded_marker_predecessor
                prior_path, prior = _superseded_marker_predecessor(route, node, canonical_path, marker)
                if evidence_currency(route, node, prior_path, prior, observe=True,
                                     jobs=registry).state != "current":
                    return None
            elif gate_currency(route, node, canonical_path, marker, observe=True, jobs=registry).state != "current":
                return None
            for parent in node.get("depends_on") or ():
                if parent in downstream and parent != upstream_id and prove(str(parent)) is None:
                    return None
            proof = {"upstream": upstream_id, "upstream_sequence": revision.get("upstream_sequence", p),
                     "history_sha256": new_sha, "history_observed_at": observed,
                     "attempt_id": attempt_id, "launched_at": fields[0]}
            memo[node_id] = proof
            return proof
        except (OSError, ValueError, KeyError, TypeError, DispatchContractError, AttributeError):
            return None
        finally:
            active.discard(node_id)

    result = {}
    for candidate in route.get("nodes", []):
        candidate_id = str(candidate.get("id"))
        if candidate_id in downstream:
            proof = prove(candidate_id)
            if proof is not None:
                result[candidate_id] = proof
    return result


def revision_basis_verdict(route, node, basis, answers, *, jobs=None, direction=None, reason=None):
    """SD-154 rule 2: prove a revision's closed basis, or raise
    `ValueError("revision-basis-unverified")` (or an invalid-basis error).

    `review-findings` requires every attempt in `answers` to be a real verdict
    row (13.59.2 classification) of a node downstream (`depends_on` transitive
    closure) of `node`, in the route's own registry -- SD-134 "직함이 아니라
    배정을 본다" read onto revision provenance. `user-direction` requires a
    `owner-correction` requires a non-empty `reason` string.
    """
    if basis == "user-direction":
        if not direction:
            raise ValueError("revision-basis-unverified:user-direction needs --direction <gate-release-id|memo-path>")
        return {"basis": basis, "direction": direction}
    if basis == "owner-correction":
        if not reason:
            raise ValueError("revision-basis-unverified:owner-correction needs --reason <text>")
        return {"basis": basis, "reason": reason}
    if basis != "review-findings":
        raise ValueError(f"revision-basis-invalid:{basis}")
    answers = tuple(answers)
    if not jobs:
        raise ValueError("revision-basis-unverified:review-findings needs a readable registry (--jobs <jobs.log>)")
    if not answers:
        raise ValueError("revision-basis-unverified:review-findings needs --answers <downstream review/test verdict attempt ids>")
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        raise ValueError("revision-basis-unverified:review-findings needs a readable registry (--jobs <jobs.log>)")
    downstream = _downstream_node_ids(route, node["id"])
    verified = set()
    for candidate in route.get("nodes", []):
        candidate_id = candidate.get("id")
        if candidate_id not in downstream:
            continue
        worker_type = candidate.get("worker_type") or (
            "review" if candidate.get("kind") == "review-worker" else None
        )
        if worker_type not in ("review", "test"):
            continue
        for cols, meta in review_round_records(lines, {route["route_id"]}, candidate_id,jobs=jobs):
            attempt = meta.get("attempt_id")
            if not attempt or attempt not in answers:
                continue
            kind = REVIEW_ROUND_CAP.classify_round_row(
                cols[1], meta, worker_type=meta.get("worker_type") or worker_type,
            )
            if kind == "verdict":
                verified.add(attempt)
    if set(answers) - verified:
        raise ValueError("revision-basis-unverified:review-findings answers are not downstream verdicts: " +
                         ",".join(sorted(set(answers) - verified)))
    return {"basis": basis, "answers": list(answers)}


def _review_owner_authority(route, jobs, author_attempt_id):
    """Prove the current registered owner without taking or creating locks."""
    ROUTE_AUTHORITY.review_owner_authority(route, jobs, author_attempt_id)


def _review_input_revision_records(route, node_id, jobs):
    """Read append-only input history; it never acts as a completion marker."""
    if jobs is None:
        return []
    directory = Path(jobs).resolve().parent / "review-input-revisions" / route["route_id"] / node_id
    if directory.is_symlink() or any(parent.is_symlink() for parent in (directory.parent, directory.parent.parent)):
        raise ValueError("review-input-revision-history-invalid")
    import review_input
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("route_id") != route["route_id"]
                    or record.get("route_hash") != route["route_hash"] or record.get("node_id") != node_id
                    or record.get("jobs") != str(Path(jobs).resolve())
                    or path.name != f"{len(records) + 1:06d}.json"
                    or record.get("previous_digest") != (_sha256_record(records[-1]) if records else None)):
                raise ValueError("review-input-revision-history-invalid")
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("review-input-revision-history-invalid") from exc
        answers = record.get("answers")
        if not isinstance(answers, list) or len(answers) != 1 or record.get("sequence") != len(records) + 1 or path.is_symlink():
            raise ValueError("review-input-revision-history-invalid")
        candidates = []
        for line in Path(jobs).read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            if len(cols) == 6:
                meta = parse_registry_metadata(cols[5])
                if meta.get("attempt_id") == answers[0]:
                    candidates.append((cols, meta))
        if len(candidates) != 1:
            raise ValueError("review-input-revision-source-not-exact")
        cols, meta = candidates[0]
        if cols[1] != "done" or meta.get("note") != REVIEW_BLOCKING_NOTE or meta.get("review_input_digest") != record.get("input_binding_digest"):
            raise ValueError("review-input-revision-source-mismatch")
        original = review_input.read_binding(jobs, meta)
        lineage = review_lineage_routes(route, node_id)
        if ((original["route_id"], original["route_hash"]) not in {(r["route_id"], r["route_hash"]) for r in lineage}
                or original["route_node"] != node_id
                or record.get("of_evidence") != {"path": original["path"], "sha256": original["sha256"]}
                or not isinstance(record.get("evidence"), dict)
                or record["evidence"].get("sha256") == original["sha256"]):
            raise ValueError("review-input-revision-source-mismatch")
        records.append(record)
    return records


def _review_input_revision_plan(route, node_id, evidence, *, answers, author_attempt_id,
                                recorded_by="owner", jobs):
    """Pure SD-161 proof shared by preview and the serialized publisher."""
    from artifact_producer import require_cycle_output
    import review_input
    if jobs is None:
        raise ValueError("review-input-revision-jobs-required")
    jobs = Path(jobs).resolve()
    if jobs != _continuation_source_jobs(route).resolve():
        raise ValueError("review-input-revision-registry-mismatch")
    node = next((n for n in route["nodes"] if n["id"] == node_id), None)
    if node is None or not review_input.is_review_node(node) or review_input.has_plan_producer(route, node):
        raise ValueError("review-input-revision-node-ineligible")
    _review_owner_authority(route, jobs, author_attempt_id)
    evidence = Path(evidence).resolve()
    if not evidence.is_file():
        raise ValueError("review-input-revision-evidence-unreadable")
    if require_cycle_output(Path(route["artifact_root"]), evidence, route_id=route["route_id"]) is None:
        raise ValueError("review-input-revision-cycle-required")
    lineage = review_lineage_routes(route, node_id)
    lines = jobs.read_text(encoding="utf-8").splitlines()
    rows = [row for generation in reversed(lineage)
            for row in _review_round_rows(lines, generation["route_id"], node_id, jobs=jobs)]
    choice = ROUTE_AUTHORITY.require_continuation(ROUTE_AUTHORITY.decide_continuation(
        route, action="review-revision", node=node, rows=rows))
    budget = choice.budget
    verdicts = [(status, meta) for status, meta in rows
                if REVIEW_ROUND_CAP.classify_round_row(status, meta, worker_type="review") == "verdict"]
    if not verdicts or verdicts[-1][1].get("note") != REVIEW_BLOCKING_NOTE:
        raise ValueError("review-input-revision-blocking-verdict-required")
    selected = verdicts[-1][1]
    attempt = selected.get("attempt_id")
    if tuple(answers) != (attempt,):
        raise ValueError("review-input-revision-answer-not-current")
    source = next(r for r in lineage if r["route_id"] == (selected.get("route_id") or selected.get("route")))
    if ROUTE_IDENTITY.registered_node_identity(selected, node) != (source["route_id"], source["route_hash"], node_id):
        raise ValueError("review-input-revision-source-route-mismatch")
    terminal = inspect_terminal_attempt(selected.get("log_file"), worktree=route["cwd"],
        artifact_root_metadata=selected.get("artifact_root") or route["artifact_root"], worker_type="review",
        artifact_binding=selected)
    if terminal.get("state") != "valid" or terminal.get("verdict") != "FAIL" or terminal.get("artifact_state") != "readable":
        raise ValueError("review-input-revision-verdict-unproven")
    from dispatch_contract import attempt_process_quiescence
    process = attempt_process_quiescence(selected, terminal_receipt=True)
    if process.state != "quiescent":
        raise ValueError(f"review-input-revision-process-{process.state}:{process.reason}")
    original = review_input.read_binding(jobs, selected, verify_current=False)
    if not original:
        raise ValueError("review-input-revision-binding-required")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    if digest == original["sha256"]:
        raise ValueError("revision-evidence-unchanged")
    records = _review_input_revision_records(route, node_id, jobs)
    for record in records:
        if record["answers"] == [attempt] and record["evidence"] == {"path": str(evidence), "sha256": digest}:
            return {"input_revision": record, "tombstoned": []}, True
    from datetime import datetime, timezone
    record = {
        "schema_version": 1, "route_id": route["route_id"], "route_hash": route["route_hash"],
        "node_id": node_id, "jobs": str(jobs), "sequence": len(records) + 1,
        "previous_digest": _sha256_record(records[-1]) if records else None,
        "basis": "review-findings", "answers": [attempt],
        "input_binding_digest": selected.get("review_input_digest"),
        "of_evidence": {"path": original["path"], "sha256": original["sha256"]},
        "evidence": {"path": str(evidence), "sha256": digest},
        "author_attempt_id": author_attempt_id, "recorded_by": recorded_by,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return {"input_revision": record, "tombstoned": []}, False


def preview_review_input_revision(route, node_id, evidence, *, answers, author_attempt_id,
                                  recorded_by="owner", jobs):
    """Check the exact writer authority without creating a lock or history file."""
    result, _ = _review_input_revision_plan(
        route, node_id, evidence, answers=answers, author_attempt_id=author_attempt_id,
        recorded_by=recorded_by, jobs=jobs,
    )
    return result


def publish_review_input_revision(route, node_id, evidence, *, answers, author_attempt_id,
                                  recorded_by="owner", jobs):
    """SD-161's sole writer: rerun the common proof under the existing jobs lock."""
    options = dict(answers=answers, author_attempt_id=author_attempt_id,
                   recorded_by=recorded_by, jobs=jobs)
    # Refuse invalid authority before creating a lock, then prove it again in
    # the critical section. Preview never enters the mutating branch below.
    _review_input_revision_plan(route, node_id, evidence, **options)
    jobs = Path(jobs).resolve()
    with _exclusive_lock(Path(f"{jobs}.lock")):
        result, existing = _review_input_revision_plan(route, node_id, evidence, **options)
        if existing:
            return result
        record = result["input_revision"]
        directory = jobs.parent / "review-input-revisions" / route["route_id"] / node_id
        from artifact_receipt import _write_once
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{record['sequence']:06d}.json"
        encoded = (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if not _write_once(directory, path, encoded) and path.read_bytes() != encoded:
            raise ValueError("review-input-revision-history-conflict")
        return result

def _node_revision_records(route, node_id, jobs=None):
    """Every SD-154 `revision` record in one node's own completion-dir
    history. `dispatch-node.py`'s `admit_round` reads the same set through
    `_dependency_revisions`, so admission and the marker census agree on it."""
    directory=completion_dir(route["route_id"],jobs=jobs)
    if not directory.is_dir():
        return []
    prefix=f"{node_id}."
    revisions=[]
    for path in sorted(directory.glob(f"{node_id}.*.json")):
        middle=path.name[len(prefix):-5]
        if not middle.isdigit():
            continue
        try:
            marker=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            continue
        if marker.get("stage_authority")=="revision":
            revisions.append(marker.get("revision") or {})
    return revisions

def _supersession_records(route, node_id, jobs=None):
    """The node's canonical marker when an upstream revision tombstoned it,
    as a `supersedes` entry: its verdict no longer covers the revised input,
    so it gets one re-check inside the cap + 1 ceiling."""
    try:
        marker=json.loads((completion_dir(route["route_id"],jobs=jobs)/f"{node_id}.json").read_text(encoding="utf-8"))
    except (OSError,ValueError):
        return []
    if marker.get("state")!="superseded-by-upstream-revision" or not marker.get("attempt_id"):
        return []
    return [{"supersedes": [marker["attempt_id"]], "superseded_by": marker.get("superseded_by")}]

def _dependency_revisions(route, node, jobs=None, *, reviewed_input=None):
    """13.59.3 rule 7's closure-check eligibility set: every revision
    recorded on any node this one `depends_on`, plus the node's own marker
    such a revision tombstoned. `dispatch-node.py`'s
    `admit_round` calls this same function for its `round_budget`, so a
    marker published for a closure-check round is labeled
    `closure_class="closure-check"` the same way admission already saw it."""
    lineage = (review_lineage_routes(route, node["id"])
               if node.get("kind") == "review-worker"
               or node.get("completion_gate") == "code-test" else [route])
    dependencies = list(node.get("depends_on", []))
    if (node.get("id")=="plan-check" and route.get("ancestor_plan_refresh") is not None):
        verified_ancestor_plan_refresh(route)
        dependencies = list(node.get("source_depends_on") or [])
    return [
        revision
        for generation in reversed(lineage)
        for dep in dependencies
        for revision in _node_revision_records(generation, dep, jobs)
    ] + [
        revision
        for generation in reversed(lineage)
        for revision in _review_input_revision_records(generation, node["id"], jobs)
        if reviewed_input is None or revision.get("evidence") == {
            "path": reviewed_input.get("path"), "sha256": reviewed_input.get("sha256")}
    ] + [
        record
        for generation in reversed(lineage)
        for record in _supersession_records(generation, node["id"], jobs)
    ] + _answered_fix_revisions(route, jobs)


def _answered_fix_revisions(route, jobs):
    """A person's approved fix for this route's FAIL-ended owner answers the FAIL rows its
    claim pinned (`dispatch_replacement.answered_fix_revisions`): the same closure-check
    basis a revision naming that FAIL gives, within the verdict ceiling cap + 1."""
    if jobs is None:
        return []
    import dispatch_replacement
    return dispatch_replacement.answered_fix_revisions(jobs, route.get("route_id"))


def _continuation_node_projection(parent_node, reused_nodes):
    """Project a reviewed node through the continuation builder's exact rewrite.

    Dependency changes are authorized only by the source completion evidence,
    not by provenance fields copied from the child route.  The caller verifies
    those rows against the parent's canonical markers before using this view.
    """
    source_dependencies = list(parent_node.get("depends_on") or [])
    reused_by_id = {
        row.get("node_id"): row for row in reused_nodes
        if isinstance(row, dict) and isinstance(row.get("node_id"), str)
    }
    satisfied = [dependency for dependency in source_dependencies if dependency in reused_by_id]
    expected = json.loads(json.dumps(parent_node))
    expected["depends_on"] = [dependency for dependency in source_dependencies if dependency not in reused_by_id]
    if satisfied:
        expected["source_depends_on"] = source_dependencies
        expected["reused_dependencies"] = [
            {
                "node_id": dependency,
                "contract_hash": reused_by_id[dependency].get("contract_hash"),
                "marker_digest": reused_by_id[dependency].get("marker_digest"),
                "terminal_attempt_id": reused_by_id[dependency].get("terminal_attempt_id"),
                **{key: reused_by_id[dependency][key]
                   for key in ("resource_execution_identity", "resource_sha256")
                   if key in reused_by_id[dependency]},
            }
            for dependency in satisfied
        ]
    expected["source_contract_hash"] = _continuation_contract_hash(parent_node)
    return expected


def review_lineage_routes(route, node_id):
    """Exact node ancestry shared by review admission and disposition.

    A continuation's new route ID is not a fresh review budget. The hash-
    verified walk itself is `verified_route_lineage` (SD-155) -- shared with
    D-120 cycle admission and round census. This keeps only what stays
    review-specific on top: `effective_intensity` parity (not part of the
    common walk) and exact node-assignment identity between each generation.
    """
    try:
        lineage = verified_route_lineage(route, artifact_root=route.get("artifact_root"))
    except RouteLineageError as exc:
        raise ValueError(exc.code) from exc
    refresh=verified_ancestor_plan_refresh(route)
    for current, parent in zip(lineage, lineage[1:]):
        if parent.get("effective_intensity") != current.get("effective_intensity"):
            raise ValueError("owner-closure-lineage-context-mismatch:effective_intensity")
        child_node = next((n for n in current["nodes"] if n["id"] == node_id), None)
        parent_node = next((n for n in parent["nodes"] if n["id"] == node_id), None)
        edge = next((n for n in current.get("new_nodes", []) if n.get("node_id") == node_id), None)
        # Rebuild the reused-prefix evidence from the parent's canonical
        # markers. A self-consistent child hash cannot authorize invented
        # dependency provenance or a forged marker/attempt tuple.
        reused = current.get("reused_nodes")
        if not isinstance(reused, list):
            raise ValueError("owner-closure-lineage-node-mismatch:reuse-evidence")
        reused_ids = [row.get("node_id") for row in reused if isinstance(row, dict)]
        parent_node_ids = [str(node.get("id")) for node in parent.get("nodes", [])]
        resume_id = current.get("resume_from_node")
        if resume_id not in parent_node_ids:
            raise ValueError("owner-closure-lineage-node-mismatch:resume-boundary")
        resume_index = parent_node_ids.index(resume_id)
        if (reused_ids != parent_node_ids[:resume_index]
                or [str(node.get("id")) for node in current.get("nodes", [])]
                   != parent_node_ids[resume_index:]):
            raise ValueError("owner-closure-lineage-node-mismatch:reuse-prefix")
        try:
            expected_reused, expected_digest, _turns = _source_evidence_snapshot(parent, reused_ids)
        except (KeyError, ValueError) as exc:
            raise ValueError("owner-closure-lineage-node-mismatch:reuse-evidence") from exc
        if canonical(expected_reused) != canonical(reused) or current.get("source_evidence_digest") != expected_digest:
            # The immutable older continuation still names its historical
            # marker. Only a successor carrying a reverified official plan
            # revision may cross this one stale edge, and only after resuming
            # at the first affected review node.
            if not (refresh is not None
                    and current["route_id"]==refresh["source_route_id"]
                    and parent["route_id"]==refresh["ancestor_route_id"]
                    and reused_ids and reused_ids[-1]=="plan"
                    and canonical(expected_reused[:-1])==canonical(reused[:-1])
                    and expected_reused[-1].get("marker_digest")==refresh["current_marker_digest"]
                    and reused[-1].get("marker_digest")==refresh["old_marker_digest"]):
                raise ValueError("owner-closure-lineage-node-mismatch:reuse-evidence")
        if parent_node is None:
            raise ValueError("owner-closure-lineage-node-mismatch")
        if child_node is None:
            # A later continuation can carry this review in its completed
            # prefix rather than its runnable node list. Its exact marker row
            # is the assignment evidence; continue walking to the generation
            # that actually owns the review node so its attempt census remains
            # part of the same budget.
            reused_node = next((row for row in reused if row.get("node_id") == node_id), None)
            if (reused_node is None
                    or reused_node.get("contract_hash") != _continuation_contract_hash(parent_node)):
                raise ValueError("owner-closure-lineage-node-mismatch")
            continue
        if (not edge
                or edge.get("source_contract_hash") != _continuation_contract_hash(parent_node)
                or child_node.get("source_contract_hash") != _continuation_contract_hash(parent_node)
                or edge.get("realized_contract_hash") != _continuation_contract_hash(child_node)):
            raise ValueError("owner-closure-lineage-node-mismatch")
        # Topology rewriting may remove only dependencies proven by that exact
        # reused prefix; all other assignment, assurance, gate and scope fields
        # remain byte-for-byte equal to the source node.
        expected_node = _continuation_node_projection(parent_node, reused)
        if current.get("dispatch_evidence_override") is True and expected_node.get("dispatch_depth")==2:
            expected_node["fallback_hops"]=_fallback_chain(
                current.get("dispatch_evidence"),current.get("dispatch_contract_version") or DISPATCH_CONTRACT_VERSION,
                expected_worktree=current.get("cwd"),require_scope=True)
        if (refresh is not None
                and parent["route_id"]==refresh["source_route_id"]
                and current.get("resume_from_node")=="plan-check"
                and node_id=="plan-check"):
            dependencies=expected_node.get("reused_dependencies") or []
            if (len(dependencies)!=1 or dependencies[0].get("node_id")!="plan"
                    or dependencies[0].get("marker_digest")!=refresh["old_marker_digest"]):
                raise ValueError("owner-closure-lineage-node-mismatch:assignment")
            dependencies[0]["marker_digest"]=refresh["current_marker_digest"]
        if expected_node != child_node:
            raise ValueError("owner-closure-lineage-node-mismatch:assignment")
    return lineage
