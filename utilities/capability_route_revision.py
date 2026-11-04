"""Capability route: producer revisions, tombstones and group arbitration.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import review_round_cap as REVIEW_ROUND_CAP
from dispatch_contract import (
    DispatchContractError, completion_marker_is_current, _diff_attribution_execute_launch_head,
    evidence_digest, evidence_change_history, gate_currency,
)

from capability_route_base import (
    _exclusive_lock, _group_members, _realized_auxiliary_nodes, _resolve_auxiliary_arbiter,
    _review_round_rows, _safe_group_id, arbitration_path, atomic_write,
    AUXILIARY_ARBITER_OWNER_MERGE, completion_dir, write_once,
)

from capability_route_continuation import (
    _continuation_source_jobs, _marker_identity_row, source_lineage_verdict,
)

from capability_route_markers import (
    _downstream_node_ids, _migrate_completion_dir_forward, _next_marker_sequence,
    _parse_auxiliary_findings, _revision_consumers, ARBITRATION_SCHEMA_VERSION,
    publish_review_input_revision, revision_basis_verdict,
)


def _revision_target(route, node_id, basis):
    node = next((n for n in route.get("nodes", []) if n.get("id") == node_id), None)
    if node is None:
        raise ValueError(f"unknown route node: {node_id}")
    if basis not in ("review-findings", "user-direction", "owner-correction"):
        raise ValueError(f"revision-basis-invalid:{basis}")
    import review_input
    if review_input.is_review_node(node) and not review_input.has_plan_producer(route, node):
        if basis != "review-findings":
            raise ValueError("review-input-revision-review-findings-required")
        return node, True
    return node, False


def _producer_revision_plan(route, node_id, evidence, *, basis, answers=(), direction=None,
                            reason=None, author_attempt_id, recorded_by="owner", jobs=None):
    """Pure producer revision proof; publication reruns it inside its node lock."""
    node, input_only = _revision_target(route, node_id, basis)
    if input_only:
        raise ValueError("revision-producer-required")
    directory = completion_dir(route["route_id"], jobs=jobs)
    canonical_path = directory / f"{node_id}.json"
    try:
        marker = json.loads(canonical_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Not the gate's own missing-dependency reason (`dispatch_
        # completion_marker.test.py`'s static guardian keeps that literal
        # inside `dispatch_contract.py` and the adapters' relay) -- there
        # is nothing to revise, a distinct fact from a dependent gate
        # finding no marker for an unstarted node.
        raise ValueError("revision-target-marker-absent")
    currency = gate_currency(
        route, node, canonical_path, marker,
        allow_source_transition=True, jobs=jobs,
    )
    if currency.state not in ("current", "revised-unrecorded"):
        # `superseded`, `completion-evidence-unreadable`, or any
        # `integrity-broken:*` -- every one of these is a kept refusal
        # (13.59.3 "유지되는 거부"), not something `revise` can record over.
        raise ValueError(currency.reason)
    evidence_path = Path(evidence).resolve()
    if not (evidence_path.is_file() or evidence_path.is_dir()):
        raise ValueError("completion-evidence-unreadable")
    # "Unchanged" is the one comparison every reader makes: the evidence named
    # here against the digest the marker recorded. With gates off a changed
    # file still reads `current` (it is only history then), so the currency
    # state alone cannot say whether there is anything to record.
    evidence_sha = evidence_digest(evidence_path)
    if evidence_sha == (marker.get("evidence") or {}).get("sha256"):
        raise ValueError("revision-evidence-unchanged")
    # D-120: a revision's new evidence is bound by the same admitted cycle
    # write scope as an ordinary completion (`_publish_completion_locked`
    # already requires this) -- an open neighbouring cycle, or a
    # sealed/abandoned one, cannot certify a revision's evidence any more
    # than it can an original completion's.
    from artifact_producer import ProducerError, require_cycle_output
    try:
        require_cycle_output(Path(route["artifact_root"]), evidence_path, route_id=route["route_id"])
    except ProducerError as exc:
        raise ValueError(f"{exc.code}: {exc.detail}") from exc
    # Basis verification runs (and can raise `revision-basis-unverified`)
    # before any write -- a refused revision must publish nothing.
    registry = jobs
    if registry is None:
        try:
            registry = _continuation_source_jobs(route)
        except ValueError:
            registry = None
    revision_basis_verdict(route, node, basis, answers, jobs=registry, direction=direction, reason=reason)
    sequence = marker.get("sequence")
    history_path = directory / f"{node_id}.{sequence}.json"
    try:
        prior_bytes = history_path.read_bytes()
    except OSError:
        raise ValueError("canonical completion marker history conflict")
    prior_sha = hashlib.sha256(prior_bytes).hexdigest()
    from datetime import datetime, timezone
    new_sequence = _next_marker_sequence(directory, node_id)
    revision_record = {
        "of_sequence": sequence,
        "of_marker_sha256": prior_sha,
        "of_evidence_sha256": (marker.get("evidence") or {}).get("sha256"),
        "evidence_sha256": evidence_sha,
        "basis": basis,
        "answers": list(answers),
        "direction": direction,
        "reason": reason,
        "author_attempt_id": author_attempt_id,
        "recorded_by": recorded_by,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if node_id == "execute":
        # SD-154 A-2: an execute revision is a code change, not just new
        # gate evidence -- record the descendant commit range SD-156's
        # `source_lineage_verdict` proves, from execute's own most recent
        # terminal `launch_head` to the current HEAD.
        registry_jobs = Path(jobs) if jobs is not None else _continuation_source_jobs(route)
        prior_head = _diff_attribution_execute_launch_head(registry_jobs, route)
        cwd = route.get("cwd")
        if prior_head and isinstance(cwd, str):
            verdict = source_lineage_verdict(cwd, prior_head)
            if verdict.kind == "descendant":
                revision_record["commits"] = list(verdict.commits)
    new_marker = dict(marker)
    new_marker.pop("state", None)
    new_marker.pop("superseded_by", None)
    new_marker["stage_authority"] = "revision"
    new_marker["evidence"] = {"path": str(evidence_path), "sha256": evidence_sha}
    new_marker["sequence"] = new_sequence
    new_marker["revision"] = revision_record
    from review_source_subject import SourceSubjectError, marker_subject
    try:
        revised_subject = marker_subject(
            route, node, attempt_id=author_attempt_id, jobs=jobs,
            worktree=route.get("cwd"), completion_dir=directory,
        )
    except SourceSubjectError as exc:
        raise ValueError(f"{exc.reason}:{exc.detail}" if exc.detail else exc.reason) from exc
    if revised_subject is not None:
        new_marker["source_subject"] = revised_subject
    previous_subject = marker.get("source_subject")
    source_changed = bool(revised_subject is not None and (
        not isinstance(previous_subject, dict)
        or previous_subject.get("digest") != revised_subject.get("digest")
    ))
    # SD-153 rule 5: a revision over a capped node gets its OWN fresh
    # census (site="revision") -- the prior marker's census (if any)
    # described a different write and must not survive the copy above.
    new_marker.pop("round_census", None)
    if REVIEW_ROUND_CAP.is_round_capped_node(node):
        census_rows = ()
        if jobs is not None:
            census_jobs_path = Path(jobs)
            if census_jobs_path.is_file():
                census_lines = census_jobs_path.read_text(encoding="utf-8", errors="replace").splitlines()
                census_rows = _review_round_rows(census_lines, route["route_id"], node_id,jobs=jobs)
        census = REVIEW_ROUND_CAP.marker_round_census(route, node, census_rows, site="revision")
        if census:
            new_marker["round_census"] = census
    tombstones = {}
    kept = {}
    if not source_changed:
        prior_evidence_path = str((marker.get("evidence") or {}).get("path") or "")
        try:
            same_evidence_path = Path(evidence_path).resolve() == Path(prior_evidence_path).resolve()
        except (OSError, ValueError):
            same_evidence_path = False
        if same_evidence_path:
            consumers = _revision_consumers(route, node_id, {
                "of_sequence": sequence,
                "of_evidence_sha256": (marker.get("evidence") or {}).get("sha256"),
                "evidence_sha256": evidence_sha,
                "evidence_path": prior_evidence_path,
            }, directory=directory, registry=registry)
        else:
            consumers = {}
    else:
        consumers = {}
    for downstream_id in sorted(_downstream_node_ids(route, node_id)):
        downstream_canonical = directory / f"{downstream_id}.json"
        if not downstream_canonical.is_file():
            continue
        try:
            downstream_marker = json.loads(downstream_canonical.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if downstream_marker.get("state") == "superseded-by-upstream-revision":
            continue
        if downstream_id in consumers:
            kept[downstream_id] = consumers[downstream_id]
            continue
        if source_changed and revised_subject is not None:
            downstream_node = next((candidate for candidate in route.get("nodes", [])
                                    if candidate.get("id") == downstream_id), None)
            downstream_subject = downstream_marker.get("source_subject")
            if (downstream_node is not None and isinstance(downstream_subject, dict)
                    and downstream_subject.get("digest") == revised_subject.get("digest")
                    and downstream_subject.get("epoch") == revised_subject.get("epoch")):
                downstream_currency = gate_currency(
                    route, downstream_node, downstream_canonical, downstream_marker,
                    observe=True, jobs=jobs,
                )
                if downstream_currency.state == "current":
                    continue
        downstream_sequence = _next_marker_sequence(directory, downstream_id)
        tombstone_marker = dict(downstream_marker)
        tombstone_marker["sequence"] = downstream_sequence
        tombstone_marker["state"] = "superseded-by-upstream-revision"
        tombstone_marker["superseded_by"] = {"node": node_id, "sequence": new_sequence}
        tombstones[downstream_id] = tombstone_marker
    return new_marker, tombstones, kept


def restore_consumed_tombstones(route, *, jobs):
    """Restore legacy tombstones only when recorded evidence proves prior consumption."""
    directory = completion_dir(route["route_id"], jobs=jobs)
    candidates = []
    for node in route.get("nodes", []):
        node_id = str(node.get("id"))
        path = directory / f"{node_id}.json"
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if marker.get("state") == "superseded-by-upstream-revision":
            superseded = marker.get("superseded_by") or {}
            candidates.append((node, path, marker, str(superseded.get("node") or ""), superseded.get("sequence")))
    if not candidates:
        return {"restored": [], "errors": []}
    groups = {}
    for item in candidates:
        groups.setdefault((item[3], item[4]), []).append(item)
    restored, errors = [], []
    for (upstream_id, upstream_sequence), group in groups.items():
        try:
            upstream_node = next(n for n in route.get("nodes", []) if n.get("id") == upstream_id)
            upstream_path = directory / f"{upstream_id}.json"
            upstream_marker = json.loads(upstream_path.read_text(encoding="utf-8"))
            if (upstream_marker.get("sequence") != upstream_sequence
                    or upstream_marker.get("stage_authority") != "revision"):
                for _node, _path, tombstone, _u, _s in group:
                    node_id = str(_node.get("id"))
                    if _next_marker_sequence(directory, node_id) - 1 > int(tombstone.get("sequence", 0)):
                        errors.append(f"pending-restore-unproven:{node_id}")
                continue
            upstream_currency = gate_currency(route, upstream_node, upstream_path, upstream_marker,
                                              observe=True, jobs=jobs)
            if upstream_currency.state not in {"current", "revised-unrecorded"}:
                for _node, _path, tombstone, _u, _s in group:
                    node_id = str(_node.get("id"))
                    if _next_marker_sequence(directory, node_id) - 1 > int(tombstone.get("sequence", 0)):
                        errors.append(f"pending-restore-unproven:{node_id}")
                continue
            revision = dict(upstream_marker.get("revision") or {})
            revision["upstream_sequence"] = upstream_sequence
            revision["evidence_path"] = (upstream_marker.get("evidence") or {}).get("path")
            lock_nodes = sorted({upstream_id} | _downstream_node_ids(route, upstream_id))
            with contextlib.ExitStack() as locks:
                for affected in lock_nodes:
                    locks.enter_context(_exclusive_lock(directory / f".{affected}.completion.lock"))
                if jobs is not None:
                    locks.enter_context(_exclusive_lock(Path(f"{jobs}.lock")))
                upstream_marker = json.loads(upstream_path.read_text(encoding="utf-8"))
                if upstream_marker.get("sequence") != upstream_sequence:
                    continue
                upstream_currency = gate_currency(route, upstream_node, upstream_path, upstream_marker,
                                                  observe=True, jobs=jobs)
                if upstream_currency.state not in {"current", "revised-unrecorded"}:
                    continue
                consumers = _revision_consumers(
                    route, upstream_id, revision, directory=directory, registry=jobs,
                    restoring={"node": upstream_id, "sequence": upstream_sequence},
                )
                for node, canonical_path, tombstone, _u, _s in group:
                    node_id = str(node["id"])
                    latest_number = _next_marker_sequence(directory, node_id) - 1
                    if node_id not in consumers:
                        if latest_number > int(tombstone.get("sequence", 0)):
                            errors.append(f"pending-restore-unproven:{node_id}")
                        continue
                    history_path = directory / f"{node_id}.{tombstone.get('sequence')}.json"
                    orphan_path = directory / f"{node_id}.{latest_number}.json"
                    resumed = False
                    if latest_number > int(tombstone.get("sequence", 0)):
                        try:
                            orphan = json.loads(orphan_path.read_text(encoding="utf-8"))
                            prior_sha = hashlib.sha256(history_path.read_bytes()).hexdigest()
                            stripped = {k: v for k, v in orphan.items() if k not in {
                                "state", "superseded_by", "stage_authority", "sequence", "revision"}}
                            old_stripped = {k: v for k, v in tombstone.items() if k not in {
                                "state", "superseded_by", "stage_authority", "sequence", "revision"}}
                            identity_ok = all(orphan.get(k) == tombstone.get(k) for k in (
                                "route_id", "route_hash", "node_id", "attempt_id", "schema_version"))
                            import dispatch_contract as DC
                            schema_ok, _ = DC._marker_schema_identity_ok(route, node, orphan, orphan_path, observe=True)
                            currency = gate_currency(route, node, orphan_path, orphan, observe=True, jobs=jobs)
                            if (orphan_path.name != f"{node_id}.{latest_number}.json"
                                    or orphan.get("sequence") != latest_number or not identity_ok or not schema_ok
                                    or currency.state != "current" or not stripped == old_stripped
                                    or orphan.get("stage_authority") != "revision"
                                    or (orphan.get("revision") or {}).get("basis") != "upstream-revision-consumed"
                                    or (orphan.get("revision") or {}).get("of_sequence") != tombstone.get("sequence")
                                    or (orphan.get("revision") or {}).get("of_marker_sha256") != prior_sha
                                    or (orphan.get("revision") or {}).get("consumed", {}).get("upstream") != upstream_id
                                    or (orphan.get("revision") or {}).get("consumed", {}).get("upstream_sequence") != upstream_sequence):
                                errors.append(f"pending-restore-invalid:{node_id}:orphan-identity-or-provenance")
                                continue
                            atomic_write(canonical_path, orphan)
                            restored.append({"node": node_id, "upstream": upstream_id,
                                             "upstream_sequence": upstream_sequence, "resumed": True})
                            resumed = True
                        except (OSError, ValueError, KeyError, TypeError, DispatchContractError) as exc:
                            errors.append(f"pending-restore-invalid:{node_id}:{type(exc).__name__}:{exc}")
                            continue
                    if resumed:
                        continue
                    if latest_number > int(tombstone.get("sequence", 0)):
                        errors.append(f"pending-restore-unproven:{node_id}")
                        continue
                    ev = tombstone.get("evidence") or {}
                    new = dict(tombstone)
                    new.pop("state", None)
                    new.pop("superseded_by", None)
                    sequence = _next_marker_sequence(directory, node_id)
                    proof = consumers[node_id]
                    new["stage_authority"] = "revision"
                    new["sequence"] = sequence
                    new["revision"] = {
                        "of_sequence": tombstone.get("sequence"),
                        "of_marker_sha256": hashlib.sha256(history_path.read_bytes()).hexdigest(),
                        "of_evidence_sha256": ev.get("sha256"), "evidence_sha256": ev.get("sha256"),
                        "basis": "upstream-revision-consumed", "answers": [], "direction": None,
                        "reason": f"{upstream_id}@{upstream_sequence} evidence was observed before {proof.get('attempt_id')} started",
                        "author_attempt_id": os.environ.get("AGENT_DISPATCH_ATTEMPT_ID"),
                        "recorded_by": "runtime-auto",
                        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "consumed": proof,
                    }
                    write_once(directory / f"{node_id}.{sequence}.json", new)
                    atomic_write(canonical_path, new)
                    restored.append({"node": node_id, "upstream": upstream_id,
                                     "upstream_sequence": upstream_sequence, "resumed": False})
        except (OSError, ValueError, KeyError, TypeError, StopIteration, DispatchContractError) as exc:
            errors.append(f"{upstream_id}@{upstream_sequence}:{type(exc).__name__}:{exc}")
    return {"restored": restored, "errors": errors}


def publish_revision_locked(route, node_id, evidence, *, basis, answers=(), direction=None,
                            reason=None, author_attempt_id, recorded_by="owner", jobs=None):
    """Publish the shared producer proof and downstream tombstones under one lock."""
    _, input_only = _revision_target(route, node_id, basis)
    if input_only:
        result = publish_review_input_revision(
            route, node_id, evidence, answers=answers, author_attempt_id=author_attempt_id,
            recorded_by=recorded_by, jobs=jobs,
        )
        return {**result, "kept": []}
    directory = completion_dir(route["route_id"], jobs=jobs)
    # Every affected completion lock precedes jobs, in one sorted order.
    # Disposition holds its same node lock through the final head check/replace.
    with contextlib.ExitStack() as locks:
        for affected in sorted({node_id} | _downstream_node_ids(route, node_id)):
            locks.enter_context(_exclusive_lock(directory / f".{affected}.completion.lock"))
        if jobs:
            locks.enter_context(_exclusive_lock(Path(f"{jobs}.lock")))
        marker, tombstones, kept = _producer_revision_plan(
            route, node_id, evidence, basis=basis, answers=answers, direction=direction,
            reason=reason, author_attempt_id=author_attempt_id, recorded_by=recorded_by, jobs=jobs,
        )
        write_once(directory / f"{node_id}.{marker['sequence']}.json", marker)
        atomic_write(directory / f"{node_id}.json", marker)
        for downstream_id, tombstone in tombstones.items():
            write_once(directory / f"{downstream_id}.{tombstone['sequence']}.json", tombstone)
            atomic_write(directory / f"{downstream_id}.json", tombstone)
    return {"marker": marker, "tombstoned": list(tombstones), "kept": list(kept)}


def _route_revisions(route, *, jobs=None):
    """Every SD-154 revision recorded under this route's completion dir, for
    the close outcome (A-SD154-7)."""
    directory = completion_dir(route["route_id"], jobs=jobs)
    if not directory.is_dir():
        return []
    revisions = []
    for node in route.get("nodes", []):
        node_id = node["id"]
        prefix = f"{node_id}."
        for path in sorted(directory.glob(f"{node_id}.*.json")):
            middle = path.name[len(prefix):-5]
            if not middle.isdigit():
                continue
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if marker.get("stage_authority") != "revision":
                continue
            revision = marker.get("revision") or {}
            revisions.append({
                "node": node_id,
                "sequence": marker.get("sequence"),
                "author_attempt_id": revision.get("author_attempt_id"),
                "basis": revision.get("basis"),
                "of_evidence_sha256": revision.get("of_evidence_sha256"),
                "evidence_sha256": revision.get("evidence_sha256"),
                "recorded_by": revision.get("recorded_by"),
                "recorded_at": revision.get("recorded_at"),
            })
        # Gates off, an evidence edit after the marker is kept as history
        # (`record_evidence_change`), not as a revision marker; list it too.
        for change in evidence_change_history(directory / f"{node_id}.json", node_id):
            revisions.append({
                "node": node_id,
                "sequence": change.get("marker_sequence"),
                "author_attempt_id": change.get("observed_by"),
                "basis": "automatic",
                "of_evidence_sha256": change.get("previous_sha256"),
                "evidence_sha256": change.get("sha256"),
                "recorded_by": "runtime-auto",
                "recorded_at": change.get("observed_at"),
            })
    revisions.sort(key=lambda row: (row["node"], row["sequence"] or 0, row["recorded_at"] or ""))
    return revisions


def _arbitration_observation(route, group_id, error=None, *, path=None):
    """Read-only truth for one owner-merge group's arbitration record.

    `path` lets a caller that resolved its own dispatch state root (the wrapper
    start-gate, which is handed `agent_home`/`jobs` explicitly rather than
    re-reading the environment) name the exact record it found.
    """
    if error is not None:
        return {"passed": False, "reason": "auxiliary-arbiter-unresolved",
                "detail": error}
    path = Path(path) if path else arbitration_path(route["route_id"], group_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"passed": False, "reason": "completion-marker-absent"}
    expected_auxiliary = sorted(
        str(member.get("id"))
        for member in _realized_auxiliary_nodes(route, group_id)
    )
    if (record.get("route_id") != route.get("route_id")
            or record.get("route_hash") != route.get("route_hash")
            or record.get("group_id") != group_id
            or record.get("arbiter") != AUXILIARY_ARBITER_OWNER_MERGE
            or sorted(record.get("auxiliary_nodes") or []) != expected_auxiliary
            or len(record.get("auxiliary_findings_considered") or [])
            != len(expected_auxiliary)):
        return {"passed": False, "reason": "completion-marker-identity-mismatch"}
    evidence = record.get("evidence") or {}
    try:
        digest = evidence_digest(Path(evidence["path"]))
    except (OSError, KeyError, TypeError, ValueError):
        return {"passed": False, "reason": "completion-evidence-unreadable"}
    if digest != evidence.get("sha256"):
        return {"passed": False, "reason": "completion-evidence-hash-mismatch"}
    return {"passed": True, "reason": "completion-marker-verified",
            "evidence": evidence.get("path")}


def arbitrate_group(route, group_id, evidence):
    """Register the owner's merge record as one auxiliary-bearing group's arbitration.

    Fail-closed in declaration order; every refusal has its own typed reason.
    Step 4 is what makes G1 structurally impossible to reintroduce: the whole
    group must already hold canonical completion markers, so this transaction
    cannot be satisfied at the moment a concurrent sibling publishes its own.
    """
    members = _group_members(route, group_id)
    if not members:
        raise ValueError(f"auxiliary-group-unknown:{group_id}")
    auxiliary = _realized_auxiliary_nodes(route, group_id)
    if not auxiliary:
        raise ValueError(f"auxiliary-group-has-no-auxiliary-leg:{group_id}")
    kind, arbiter = _resolve_auxiliary_arbiter(route, group_id)
    if kind != AUXILIARY_ARBITER_OWNER_MERGE:
        raise ValueError(
            f"auxiliary-arbiter-is-node:{arbiter}; record "
            "auxiliary_findings_considered in that node's completion evidence"
        )
    _migrate_completion_dir_forward(route["route_id"])
    # M7: "joined" here has to mean the same thing it means downstream. The
    # identity row checks route/node/gate identity and the evidence digest;
    # `completion_marker_is_current` additionally requires schema v2, a real
    # sequence, the immutable history file, and the attempt linkage. Proving only
    # the weaker one let the arbitration record be written over a marker that a
    # dependent's start-gate then refuses as an absent canonical marker -- not
    # fail-open, since the dependent is blocked either way, but it makes the
    # arbitration record mean less than the join it claims to attest. (Spell
    # that refusal reason in prose, not as its literal token: the static
    # guardian in `dispatch_completion_marker.test.py` keeps the literal inside
    # `dispatch_contract.py` and the adapters' relay, and every allowlist entry
    # added to quiet a comment blunts it for the next real violation.)
    directory = completion_dir(route["route_id"])
    unjoined = sorted(
        str(member.get("id")) for member in members
        if not (
            _marker_identity_row(
                route, member, str(member.get("id")), member.get("completion_gate")
            )["passed"]
            and completion_marker_is_current(
                route, member, directory / f"{member.get('id')}.json"
            )
        )
    )
    if unjoined:
        raise ValueError("auxiliary-arbitration-before-join:" + ",".join(unjoined))
    considered = _parse_auxiliary_findings(evidence)
    if considered is None:
        raise ValueError(
            f"auxiliary arbiter gate {group_id} requires "
            "auxiliary_findings_considered in evidence or frontmatter"
        )
    if len(considered) != len(auxiliary):
        raise ValueError(
            f"auxiliary arbiter gate {group_id} requires "
            f"auxiliary_findings_considered length {len(auxiliary)}, "
            f"got {len(considered)}"
        )
    anchor = next(member for member in members if member.get("parallel_leg_index") == 0)
    record = {
        "schema_version": ARBITRATION_SCHEMA_VERSION,
        "route_id": route["route_id"],
        "route_hash": route["route_hash"],
        "registry_digest": route["registry_digest"],
        "group_id": group_id,
        "anchor_node": str(anchor.get("id")),
        "arbiter": AUXILIARY_ARBITER_OWNER_MERGE,
        "member_nodes": [str(member.get("id")) for member in members],
        "auxiliary_nodes": sorted(str(member.get("id")) for member in auxiliary),
        "auxiliary_findings_considered": list(considered),
        "evidence": {
            "path": str(evidence),
            "sha256": evidence_digest(evidence),
        },
    }
    path = arbitration_path(route["route_id"], group_id)
    directory = completion_dir(route["route_id"])
    # Same normalization as `arbitration_path`: the record path escapes an unsafe
    # group id and the lock used the raw one, so two spellings of one identity
    # could name different files. Today's group ids are safe either way.
    with _exclusive_lock(directory/f".{_safe_group_id(group_id)}.arbitration.lock"):
        if path.is_file():
            # Same immutability contract as `write_completion_marker`: an
            # identical re-registration is idempotent, a different one conflicts.
            # `arbitrated_at` is excluded from identity because a wall clock
            # reading is not part of what was decided.
            existing = json.loads(path.read_text(encoding="utf-8"))
            if {key: existing.get(key) for key in record} == record:
                return existing
            raise ValueError(f"auxiliary-arbitration-identity-conflict:{group_id}")
        from datetime import datetime, timezone
        record["arbitrated_at"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        write_once(path, record)
    return record


def _require_entry_scope_review_preview(route, node, artifact_output):
    """Scoped refine completion carries a real preview file before its existing node gate can close."""
    if (type(route.get("entry_scope_contract_version")) is not int
            or route.get("entry_scope_contract_version") != 1
            or route.get("capability") != "autopilot-refine"
            or node.get("id") not in {"review", "one-shot"}):
        return None
    preview = Path(artifact_output) / "reviews/refine/preview.md"
    if preview.is_symlink() or not preview.is_file():
        raise ValueError("completion-evidence-unreadable")
    try:
        preview.resolve(strict=True).relative_to(Path(artifact_output).resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("completion-evidence-unreadable") from exc
    return preview
