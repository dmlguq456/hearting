#!/usr/bin/env python3
"""Tier-derived review-round cap leaf (CONVENTIONS §1.1 retry budget).

Moved out of `dispatch-node.py` so `capability-route.py` (SD-116 WP4) can
reuse the same derivation for its sealed `continuation_budget.review_round_cap`
without creating an import cycle: `dispatch-node.py` already loads
`capability-route.py`, so `capability-route.py` importing `dispatch-node.py`
back would cycle. Both now import this leaf instead (route_identity.py /
dispatch_launch_tuple.py precedent -- one definition, no duplication).

SD-153 adds the one shared admission decision (`round_budget`/`RoundBudget`)
that every registered launch surface (dispatch-node, dispatch-batch,
stage-dispatch-fallback) and `capability-route.py`'s owner-closure eligibility
now delegate to, instead of each keeping its own `len(prior)+1 > max_round`
comparison. A round with no verdict (a crashed/capacity-dead attempt, not a
PASS/FAIL/blocking review) does not spend the exhaustion budget by itself --
two of them in a row bind the node to the unregistered fallback chain instead
(`VERDICTLESS_BOUND`), which is a distinct outcome from "the correction budget
ran out on real verdicts"."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping, Sequence

from dispatch_attempt_policy import (
    OPEN_STATES,
    REVIEW_BLOCKING_NOTE,
    SUCCESS_NOTES,
    deferred_completion,
    readable_result,
    terminal_conflict_pending,
)

# SD-153 rule 3: two rounds in a row with no verdict at all bind the node to
# the sealed fallback chain's remaining unregistered hop rather than spending
# a third registered round on the same kind of silence.
VERDICTLESS_BOUND = 2

# C-14: only the plan-check/impl-review/test QA anchors carry a review/correction
# budget under CONVENTIONS §1.1. The cap applies to every recipe node with
# kind == "review-worker" -- enumerated exhaustively from
# capabilities/topologies.json (dispatch_node.test.py asserts set equality
# against that file). Lives here, not in dispatch-node.py, so
# capability-route.py's marker writers (SD-153 rule 5) can read the same set
# without dispatch-node.py's back-import of capability-route.py becoming a
# cycle; dispatch-node.py keeps `ROUND_CAPPED_NODE_IDS` as a re-export name.
ROUND_CAPPED_NODE_IDS = frozenset({
    "claim-verify", "critic-review", "fact-verify", "impl-review", "independent-verify",
    "inspect", "plan-check", "post-deploy-verify", "qa", "quality-review", "release-review",
    "review", "run-verify", "security-review", "smoke", "strategy-review", "verify",
    # Optional review-worker parts in the stage catalog are dispatchable too.
    "diagnose", "eval-smoke",
    "visual-verify",
    # Declared exception: kind is pipeline-stage, but `test` is the QA anchor
    # C-14 named. Every other exception must be explicit here too.
    "test",
})

def is_round_capped_node(node):
    """Keep historic capped anchors and include derived SD-161 review legs.

    Parallel realization changes the node id; the sealed review kind and unit
    retain its budget obligation. Each leg still counts only its own exact id.
    """
    return isinstance(node, Mapping) and (
        node.get("id") in ROUND_CAPPED_NODE_IDS
        or node.get("parallel_anchor") in ROUND_CAPPED_NODE_IDS
        or (node.get("kind") == "review-worker" and node.get("unit") == "qa/plan-review")
        # A stage a plan added is shaped on a catalog stage and keeps that stage's budget.
        or (isinstance(node.get("plan_stage"), Mapping)
            and node["plan_stage"].get("template") in ROUND_CAPPED_NODE_IDS)
    )


# SD-153 rule 5 (13.59.2): every marker of a `ROUND_CAPPED_NODE_IDS` node --
# registered, owner-closure, revision, and inline alike -- carries a round
# census naming one of these closure classes. Closed vocabulary so every
# writer names the same thing the same way; `marker_round_census` below is the
# only place that constructs one (`capability_route.test.py`'s source census
# enforces this).
CLOSURE_CLASSES = frozenset({
    "registered-verdict",     # a registered worker's own completion (incl. a closure-check round)
    "closure-check",          # a registered completion that landed the SD-154 rule 7 closure-check round
    "owner-closure",          # SD-124 owner-closure over an exhausted/bound review budget
    "revision",               # SD-154 `publish_revision_locked` over a capped node
    "review-verdictless-bound",  # review node inline/native completion after the SD-153 rule 3 bound
    "owner-run-verdictless",     # non-review capped node (e.g. `test`) owner-run after the bound
    "owner-override-unlinked",   # inline completion over an unresolved blocking FAIL (SD-134 A75-9)
    "inline-no-unresolved-fail", # plain inline completion, no unresolved blocking verdict in history
})


def logical_round_records(rows, *, jobs=None):
    """Project reciprocal, registry-sealed SD-157 rows onto one semantic round.

    These immutable fields are added only by the common registration fence
    after checking the canonical claim. A one-sided, malformed, or mismatched
    declaration leaves both rows visible. The registry and failure history are
    never changed; only the admission/marker census uses this projection.
    """
    if jobs is None:
        return rows  # No registry binding grants no census-reduction authority.
    from route_authority import replacement_parent_matches
    by_id = {}
    for fields, meta in rows:
        by_id.setdefault(meta.get('attempt_id'), []).append((fields, meta))
    replaced = {}
    consumed = set()
    for fields, source in rows:
        family = source.get('replacement_family_id', '')
        digest = source.get('replacement_claim_digest', '')
        original = source.get('attempt_id')
        target = source.get('replacement_attempt_id')
        if (not re.fullmatch(r'[0-9a-f]{64}', family)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or not original or len(by_id.get(original, [])) != 1
                or target != 'att-'+hashlib.sha256(('replacement:'+family).encode()).hexdigest()[:48]
                or len(by_id.get(target, [])) != 1
                or source.get('replacement_ordinal') != '1'
                or fields[1] not in {'done', 'cancelled', 'killed'}
                or classify_round_row(fields[1], source,
                                      worker_type=source.get('worker_type', 'review')) != 'verdict-less'):
            continue
        candidate_fields, candidate = by_id[target][0]
        if (candidate.get('replacement_original_attempt_id') != original
                or candidate.get('automatic_retry_of') != original
                or candidate.get('replacement_family_id') != family
                or candidate.get('replacement_claim_digest') != digest
                or candidate.get('replacement_ordinal') != '1'
                or candidate_fields[2:4] != fields[2:4]
                or not replacement_parent_matches(source, candidate, jobs, lineage=True)
                or any(candidate.get(key) != source.get(key) for key in
                       ('route_node', 'worker_type', 'dispatch_depth'))):
            continue
        # Metadata is reciprocal, but the immutable canonical claim must still
        # agree. Read-only and lock-free: callers may already hold the jobs lock.
        import dispatch_replacement as replacement
        try:
            record = replacement._check_record(
                replacement._read(replacement._record_path(jobs, family)), family, source)
            if (replacement._digest(record) != digest
                    or record['original_attempt_id'] != original
                    or record['replacement_attempt_id'] != target
                    or (replacement.source_reservation(jobs, original) or {}).get('family_id') != family):
                continue
        except (replacement.DC.DispatchContractError, KeyError):
            continue
        replaced[original] = (candidate_fields, candidate)
        consumed.add(target)
    return [replaced.get(meta.get('attempt_id'), (fields, meta))
            for fields, meta in rows if meta.get('attempt_id') not in consumed]


def _base_recovery_fields(node_kind, state="exhausted"):
    """A budget ends automatic review, not ownership of the findings.

    `state` selects between the two distinct outcomes `round_budget` can stop
    on: `"exhausted"` (the historical exhaustion guidance, unchanged) and
    `"verdictless-bound"` (SD-153 rule 3 -- two silent rounds in a row, so the
    next action is a hop already named in the sealed fallback chain, not a
    registered retry).
    """
    if state == "verdictless-bound":
        if node_kind == "review-worker":
            return {
                "next_action": "native-subagent",
                "recovery_owner": "owner",
                "recovery_surface": "capability-route complete",
                "recovery_hint": "Two rounds in a row ended with no verdict (a crashed or capacity-dead "
                    "attempt, not a PASS/FAIL/blocking review) -- a third registered dispatch would only "
                    "repeat the same silence. Route the review through the sealed fallback chain's remaining "
                    "unregistered hop (a native subagent, then owner-inline) and record the outcome as "
                    "evidence-bound owner-closure.",
            }
        return {
            "next_action": "owner-inline",
            "recovery_owner": "owner",
            "recovery_surface": "capability-route complete --inline",
            "recovery_hint": "Two rounds in a row ended with no verdict. Hand the remaining verification to "
                "the owner-inline fallback hop instead of spending a third registered round on the same "
                "silence; preserve the exact failing evidence for that hop. Run only the approved "
                "unfinished checks first; complete --inline records actual passing evidence afterwards, "
                "not permission to run checks or an independent PASS. Inherit canonical "
                "AGENT_DISPATCH_JOBS for the census; omit --jobs and --attempt-id for owner-inline completion.",
        }
    if node_kind == "review-worker":
        return {"required_action": "resolve-review-findings", "recovery_owner": "owner",
                "recovery_surface": "capability-route complete",
                "recovery_hint": "Read the blocking reviews and the corrected artifact. Record each finding's "
                    "resolution in an evidence-bound .owner-closure.md and use the existing owner-closure "
                    "completion path. This records owner judgment, not independent PASS. If findings remain "
                    "unresolved, hand them to the parent with the existing artifacts; a fresh route is not required."}
    return {"required_action": "report-unresolved-verification", "recovery_owner": "owner",
            "recovery_surface": "capability-route continuation",
            "recovery_hint": "Preserve the completed stages and exact failing verification evidence. "
                "Hand back the remaining verification for an explicit continuation; do not infer PASS from a corrected plan."}


def recovery_fields(node_kind, state="exhausted", *, route=None, node=None, jobs=None, route_file=None):
    fields = _base_recovery_fields(node_kind, state)
    if state != "exhausted" or node_kind != "review-worker" or not route or not node or jobs is None:
        return fields
    from pathlib import Path
    import shlex
    from route_lineage import verified_route_lineage, canonical_route_path
    from dispatch_contract import parse_registry_metadata, DispatchContractError
    import review_input
    try:
        lineage = verified_route_lineage(route)
        identities = {(r["route_id"], r["route_hash"]) for r in lineage}
        rows = []
        for line in Path(jobs).read_text().splitlines():
            cols = line.split("\t")
            if len(cols) != 6:
                continue
            meta = parse_registry_metadata(cols[5])
            if ((meta.get("route_id") or meta.get("route"), meta.get("route_hash")) in identities
                    and (meta.get("route_node") or meta.get("node")) == node["id"]):
                rows.append((cols, meta))
        rows = [(cols[1], meta) for cols, meta in logical_round_records(rows, jobs=jobs)]
        verdicts = [(status, meta) for status, meta in rows
                    if classify_round_row(status, meta, worker_type="review") == "verdict"]
        if not verdicts or verdicts[-1][1].get("note") != REVIEW_BLOCKING_NOTE:
            return fields
        latest = verdicts[-1][1]
        route_path = route_file or canonical_route_path(route["artifact_root"], route["route_id"])
        command = ["python3", str(Path(__file__).with_name("capability-route.py")), "complete",
                   "--route", str(route_path), "--node", node["id"], "--jobs", str(Path(jobs).resolve()),
                   "--attempt-id", latest["attempt_id"], "--evidence", "<current-cycle-file.owner-closure.md>"]
        fields["recovery_check_command"] = shlex.join(command + ["--check"])
        fields["recovery_complete_command"] = shlex.join(command)
        if review_input.is_review_node(node) and not review_input.has_plan_producer(route, node):
            try:
                review_input.read_binding(jobs, latest)
            except DispatchContractError as exc:
                fields["revision_unavailable"] = exc.reason
            else:
                budget = round_budget(route, node, rows)
                if budget.verdict_rounds < budget.cap + 1:
                    fields["recovery_revise_command"] = shlex.join([
                        "python3", str(Path(__file__).with_name("capability-route.py")), "revise",
                        "--route", str(route_path), "--node", node["id"], "--jobs", str(Path(jobs).resolve()),
                        "--basis", "review-findings", "--answers", latest["attempt_id"],
                        "--evidence", "<current-cycle-corrected-plan-file>",
                        "--author-attempt-id", "<current-owner-attempt-id>"])
    except (OSError, ValueError, KeyError, TypeError):
        # Recovery hints carry no authority and cannot weaken the original refusal.
        return fields
    return fields


def max_review_rounds(effective_intensity):
    """Tier-derived max round count for a capped anchor.

    `direct`/`quick` run no automatic correction round (max 1: the first pass
    only, matching the table's "One pass"/"None automatically"). `standard`/`strong`
    get one correction (max 2). `thorough`/`adversarial` get two, including the
    adversary pass that is not itself a correction (max 3). This is deliberately a
    per-tier derivation, not a hardcoded `cap=2` -- a tier change moves the cap with it.
    """
    if effective_intensity in ("direct", "quick"):
        return 1
    if effective_intensity in ("standard", "strong"):
        return 2
    if effective_intensity in ("thorough", "adversarial"):
        return 3
    raise ValueError(f"unknown effective_intensity for review round cap: {effective_intensity}")


def classify_round_row(status, metadata, *, worker_type):
    """One round's outcome, in positive-definition order.

    Order: live, then unsettled, then verdict, then everything else is
    verdict-less. A row is `"verdict"` only when it actually says something
    about the work: a recognized success note, a blocking review, or (for a
    non-review worker only) an explicit FAIL. A review worker that crashed,
    ran out of capacity, or died with an invalid envelope never produced a
    review verdict -- it is `"verdict-less"` even though the row is `done`,
    so it does not spend the exhaustion budget by itself (SD-153 rule 1);
    two of them in a row still bind the node via `VERDICTLESS_BOUND` (rule 3).
    """
    if status in OPEN_STATES:
        return "live"
    if deferred_completion(metadata) == "pending" or terminal_conflict_pending(metadata):
        return "unsettled"
    note = metadata.get("note", "")
    if status == "done" and (
        note in SUCCESS_NOTES
        or note == REVIEW_BLOCKING_NOTE
        or (worker_type != "review" and note == "dead-worker-fail" and metadata.get("failure_class") == "fail")
    ):
        return "verdict"
    return "verdict-less"


@dataclass(frozen=True)
class RoundBudget:
    """The one admission decision every registered launch surface reads.

    `state` is the caller's dispatch decision: `"admit"` launches, the two
    `"blocked-*"` states refuse for a reason that already exists elsewhere (a
    still-live sibling, or a row whose outcome is not yet settled) and carry
    no cap semantics of their own, `"exhausted"` is the historical
    correction-budget-spent refusal, and `"verdictless-bound"` is SD-153 rule
    3 -- a distinct outcome that hands the node to the sealed fallback
    chain's remaining unregistered hop instead of a third silent registered
    round.
    """
    verdict_rounds: int
    verdictless_streak: int
    verdictless_rounds: int
    cap: int
    state: str
    next_round: int
    correction_round: int
    round_kind: str
    next_action: str
    closure_check_used: bool
    progress_reset: bool = False


_NEXT_ACTION_BY_STATE = {
    "admit": "dispatch",
    "blocked-live": "wait",
    "blocked-unsettled": "wait",
    "exhausted": "owner-closure",
    "verdictless-bound": "native-subagent",
}


def round_budget(route, node, rows: Sequence[tuple[str, Mapping]], *, revisions=()):
    """The shared launch-admission decision for one route node's round history.

    `rows` is the exact registry census for the node as `(status, metadata)`
    pairs, oldest first -- the shape `capability-route.py`'s
    `_review_round_rows` already returns, and dispatch-node.py's
    `(cols, metadata)` census reduces to via `cols[1]`. `revisions` is the
    node's SD-154 revision history (empty until `capability-route.py` grows
    `publish_revision_locked` -- P3 always passes `()`, so `round_kind` never
    resolves to `"closure-check"` from a P3 call site); once a caller
    supplies it, a node whose last verdict is a blocking FAIL and whose
    revision names that exact row as its answer gets one `closure-check`
    round instead of an ordinary correction.
    """
    continuation_budget = route.get("continuation_budget") or {}
    cap = continuation_budget.get("review_round_cap")
    if cap is None:
        cap = max_review_rounds(route["effective_intensity"])
    worker_type = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
    kinds = [
        classify_round_row(status, meta, worker_type=meta.get("worker_type") or worker_type)
        for status, meta in rows
    ]
    verdict_rounds = kinds.count("verdict")
    verdictless_rounds = kinds.count("verdict-less")
    verdictless_streak = 0
    for kind in reversed(kinds):
        if kind != "verdict-less":
            break
        verdictless_streak += 1
    progress_reset = False
    if verdictless_streak >= VERDICTLESS_BOUND and route.get("route_plan") is not None:
        # RA-5: BLOCKED rounds that keep shrinking the leg's unmet items are progress (route_authority).
        import route_authority
        counted = route_authority.blocked_progress(route, rows, worker_type=worker_type)
        progress_reset = counted < verdictless_streak
        verdictless_streak = counted
    if "live" in kinds:
        state = "blocked-live"
    elif "unsettled" in kinds:
        state = "blocked-unsettled"
    elif verdictless_streak >= VERDICTLESS_BOUND:
        state = "verdictless-bound"
    elif verdict_rounds >= cap:
        state = "exhausted"
    else:
        state = "admit"
    next_round = len(rows) + 1
    correction_round = next_round if next_round >= 2 else 0
    round_kind = "first" if next_round < 2 else "correction"
    closure_check_used = False
    # SD-161: labels on within-cap corrections do not spend the extra
    # verdict. Only the lineage-wide verdict census does: at most cap + 1.
    # A verdictless replacement tail can still answer the last real FAIL;
    # live/unsettled rows and the verdictless bound always take precedence.
    verdict_indexes = [index for index, kind in enumerate(kinds) if kind == "verdict"]
    if state in ("admit", "exhausted") and verdict_rounds < cap + 1 and revisions and verdict_indexes:
        last_row = rows[verdict_indexes[-1]]
        _last_status, last_meta = last_row
        last_blocking = _last_round_blocking_verdict([last_row], worker_type)
        last_attempt = last_meta.get("attempt_id")
        if last_blocking and last_attempt and any(
            last_attempt in (revision.get("answers") or ()) for revision in revisions
        ):
            state = "admit"
            round_kind = "closure-check"
            closure_check_used = True
    return RoundBudget(
        verdict_rounds=verdict_rounds,
        verdictless_streak=verdictless_streak,
        verdictless_rounds=verdictless_rounds,
        cap=cap,
        state=state,
        next_round=next_round,
        correction_round=correction_round,
        round_kind=round_kind,
        next_action=_NEXT_ACTION_BY_STATE[state],
        closure_check_used=closure_check_used,
        progress_reset=progress_reset,
    )


def last_verdict_blocking(rows, worker_type):
    """True when the latest verdict round, past any verdict-less tail, is a
    blocking FAIL: the node's gate failed and no later verdict answered it.
    OPERATIONS §5.10 reads a sub-session opened now as a gap retry of the
    unfinished items, never as planned subdivision.
    """
    verdicts = [(status, metadata) for status, metadata in rows if classify_round_row(
        status, metadata, worker_type=metadata.get("worker_type") or worker_type) == "verdict"]
    return _last_round_blocking_verdict(verdicts[-1:], worker_type)


def gate_unmet(rows, worker_type):
    """True when the node's latest settled round left its gate unmet: a blocking
    verdict, or a worker's own BLOCKED (no judgment reached, items left), with no
    later verdict answering it. Deaths and live rows are passed over. OPERATIONS
    §5.10 reads a sub-session opened now as a gap retry of the unfinished items,
    never as planned subdivision.
    """
    for status, metadata in reversed(list(rows)):
        kind = classify_round_row(status, metadata, worker_type=metadata.get("worker_type") or worker_type)
        if kind == "verdict":
            return _last_round_blocking_verdict([(status, metadata)], worker_type)
        if kind == "verdict-less" and readable_result(metadata) == "BLOCKED":
            return True
    return False


def _last_round_blocking_verdict(rows, worker_type):
    """True when the most recent terminated round is a real blocking verdict
    (a review FAIL or a genuine test failure) -- SD-153 rule 5's "해소 안 된
    blocking FAIL": an inline completion landing over this row is an override,
    not an ordinary closure, no matter how much budget remains (SD-134 A75-9).
    """
    if not rows:
        return False
    status, metadata = rows[-1]
    kind = classify_round_row(status, metadata, worker_type=metadata.get("worker_type") or worker_type)
    if kind != "verdict":
        return False
    note = metadata.get("note", "")
    if (note == "completed-marker" and metadata.get("failure_class") == "fail"
            and (metadata.get("worker_type") or worker_type) == "review"):
        from dispatch_contract import owner_closure_shape
        return owner_closure_shape(metadata) == "registered-review"
    return note == REVIEW_BLOCKING_NOTE or (
        note == "dead-worker-fail" and metadata.get("failure_class") == "fail"
    )


def marker_round_census(route, node, rows, *, site, revisions=(), independently_reviewed=False):
    """The one derivation every capped-node marker writer calls for
    `round_census` (SD-153 rule 5, 13.59.2). Returns `None` only when the
    route's `effective_intensity` cannot derive a cap at all (`round_budget`'s
    own `ValueError`); every other case -- including an empty `rows` (no
    registry to read) -- returns a real census, so a marker is never silently
    missing the field.

    `site` names which writer is publishing, and fixes most of
    `closure_class`:

    * `"registered"` -- a registered worker's own completion. `rows` is the
      node's PRIOR round history (the completing attempt's own row excluded,
      exactly as `admit_round` saw it before this round ran); this landing
      round is added back as one verdict round. `closure_class` is
      `"closure-check"` when the prior budget already resolved this round to
      `round_kind == "closure-check"` (13.59.3 rule 7), else
      `"registered-verdict"`.
    * `"owner-closure"` -- SD-124 owner-closure. Always `"owner-closure"`.
    * `"revision"` -- SD-154 `publish_revision_locked` over a capped node.
      Always `"revision"`.
    * `"inline"` -- everything else (owner-inline, a claimed native-subagent,
      owner-chain aggregation). `closure_class` is `"owner-override-unlinked"`
      when the most recent terminated round is an unresolved blocking verdict
      and this completion is not itself an independently-reviewed claim
      (`independently_reviewed`, e.g. a verified native-subagent transcript --
      that IS the sealed fallback chain's legitimate hop, not an override);
      else the SD-153 rule 3 bound class (`"review-verdictless-bound"` for a
      `kind == "review-worker"` node, `"owner-run-verdictless"` for every
      other capped kind -- `test` included) when the budget is
      `verdictless-bound`; else plain `"inline-no-unresolved-fail"`.
    """
    try:
        budget = round_budget(route, node, rows, revisions=revisions)
    except ValueError:
        return None
    if site == "registered":
        closure_class = "closure-check" if budget.round_kind == "closure-check" else "registered-verdict"
        return {
            "verdict_rounds": budget.verdict_rounds + 1,
            "verdictless_rounds": budget.verdictless_rounds,
            "cap": budget.cap,
            "closure_class": closure_class,
        }
    if site == "owner-closure":
        closure_class = "owner-closure"
    elif site == "revision":
        closure_class = "revision"
    elif site == "inline":
        worker_type = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
        if not independently_reviewed and _last_round_blocking_verdict(rows, worker_type):
            closure_class = "owner-override-unlinked"
        elif budget.state == "verdictless-bound":
            closure_class = (
                "review-verdictless-bound" if node.get("kind") == "review-worker"
                else "owner-run-verdictless"
            )
        else:
            closure_class = "inline-no-unresolved-fail"
    else:
        raise ValueError(f"unknown round-census site: {site}")
    return {
        "verdict_rounds": budget.verdict_rounds,
        "verdictless_rounds": budget.verdictless_rounds,
        "cap": budget.cap,
        "closure_class": closure_class,
    }
