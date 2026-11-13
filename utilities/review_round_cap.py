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
    from dispatch_contract import terminal_attempt_never_started
    rows = [(fields, meta) for fields, meta in rows
            if not terminal_attempt_never_started(fields[1], meta)]
    if jobs is None:
        return rows  # No registry binding grants no SD-157 projection authority.
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
        # Ephemeral projection only: the same canonical proof supplies raw
        # consumers with both physical members without counting two rounds.
        replaced[original] = (candidate_fields, {**candidate,
            "logical_round_attempt_ids": frozenset((original, target))})
        consumed.add(target)
    return [replaced.get(meta.get('attempt_id'), (fields, meta))
            for fields, meta in rows if meta.get('attempt_id') not in consumed]


def _base_recovery_fields(node_kind, state="exhausted"):
    from route_authority import decide_continuation
    choice = decide_continuation(action="round-recovery", facts={"state": state, "node_kind": node_kind})
    review = node_kind == "review-worker"
    fields = {"recovery_owner": "owner",
              "recovery_surface": "owner-report" if review else "capability-route complete --inline"}
    if state == "review-correction-required":
        fields.update(next_action=choice.next_action, required_action="resolve-review-findings",
            recovery_hint="Correct the blocking findings together, then repeat the same start for the closure check.")
    elif state == "verdictless-bound":
        fields.update(next_action=choice.next_action,
            recovery_hint="Continue the remaining work through the existing unregistered fallback; "
                "preserve prior FAILs and publish completion only after verification. No new registered round is admitted.")
    else:
        fields.update(required_action="report-unresolved-verification",
            recovery_hint="Preserve prior results and report any unresolved verification without inferring PASS.")
    return fields


# A gate whose verdict is a deterministic, re-runnable check rather than a
# model reviewer's judgment. The round cap limits reviewer re-invocations; it
# never removes the way to run the check itself.
DETERMINISTIC_CHECK_GATES = frozenset({"hash-bound-smoke"})


def is_deterministic_check(node):
    return isinstance(node, Mapping) and node.get("completion_gate") in DETERMINISTIC_CHECK_GATES


def _recorded_smoke(route, node, jobs):
    """Read the exact completed smoke payload; stale inputs may be rerun, not reused."""
    import importlib.util
    import json
    from pathlib import Path
    import dispatch_contract as contract
    if not route or not node or jobs is None or not is_deterministic_check(node):
        return None
    try:
        marker_path = contract._route_module().completion_dir(route["route_id"], jobs=jobs) / (node["id"] + ".json")
        marker = json.loads(marker_path.read_text())
        if any(marker.get(key) != expected for key, expected in (
                ("route_id", route["route_id"]), ("route_hash", route["route_hash"]), ("node_id", node["id"]))):
            return None
        # The existing currency reader proves immutable history and attempt
        # linkage even when upstream edits superseded this smoke's inputs.
        currency = contract.gate_currency(route, node, marker_path, marker,
                                         jobs=jobs, observe=True, allow_source_transition=True)
        if currency.state not in {"current", "superseded"}:
            return None
        evidence = marker["evidence"]
        path = Path(evidence["path"])
        path.resolve(strict=True).relative_to(Path(route["artifact_root"]).resolve(strict=True))
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != evidence["sha256"]:
            return None
        data = json.loads(raw)
        bare = {key: value for key, value in data.items() if key != "attestation_hash"}
        if data.get("attestation_hash") != "sha256:" + hashlib.sha256(json.dumps(
                bare, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
            return None
        if (not isinstance(data.get("command"), list) or not data["command"]
                or not all(isinstance(arg, str) and arg for arg in data["command"])
                or Path(data["cwd"]).resolve(strict=True) != Path(route["cwd"]).resolve(strict=True)
                or not data.get("inputs")
                or not all(isinstance(item.get("path"), str) and Path(item["path"]).is_absolute()
                           for item in data["inputs"])):
            return None
        spec = importlib.util.spec_from_file_location("_round_smoke", Path(__file__).resolve().parents[1] / "tools/smoke-attestation.py")
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        return marker_path, marker, path, data, smoke
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def reuse_deterministic_pass(route, node, jobs, budget):
    """Return existing completion, before another model is invoked or round spent."""
    from pathlib import Path
    import dispatch_contract as contract
    if budget.state not in {"admit", "exhausted"}:
        return None  # A live, unsettled or blocking attempt retains precedence.
    recorded = _recorded_smoke(route, node, jobs)
    if recorded is None:
        return None
    marker_path, marker, evidence, data, smoke = recorded
    try:
        if not contract.completion_marker_is_current(route, node, marker_path, marker, jobs=jobs):
            return None
        if contract.completion_attempt_readiness(route, node, marker, Path(jobs)).state != "ready":
            return None
        smoke.verify(data)
        smoke.verify_execution_environment(data, data["command"], data["cwd"])
    except (contract.DispatchContractError, OSError, ValueError, KeyError, TypeError):
        return None
    return {"check": "ok", "launch_state": "existing", "reason": "existing-completed-attempt",
            "child_spawned": "0", "registered": "0", "started": "0",
            "attempt_id": marker["attempt_id"], "artifact": str(evidence),
            "next_action": "successor-start", "required_action": "use-current-pass",
            "review_independence": marker.get("review_independence", "degraded")}


def recovery_fields(node_kind, state="exhausted", *, route=None, node=None, jobs=None, route_file=None):
    fields = _review_recovery_fields(node_kind, state, route=route, node=node, jobs=jobs,
                                     route_file=route_file)
    if is_deterministic_check(node) and fields.get("required_action") == "use-current-pass":
        # Registry PASS text is not evidence that the current smoke still passes.
        budget = round_budget(route, node, [])
        if reuse_deterministic_pass(route, node, jobs, budget) is None:
            fields = _base_recovery_fields(node_kind, state)
    if (state in {"exhausted", "verdictless-bound"} and is_deterministic_check(node)
            and fields.get("required_action", "report-unresolved-verification")
            == "report-unresolved-verification" and "recovery_complete_command" not in fields):
        return _deterministic_check_fields(fields, route=route, node=node, jobs=jobs, route_file=route_file)
    return fields


def _deterministic_check_fields(fields, *, route, node, jobs, route_file):
    """Replay the existing smoke payload, with no new owner, gate or manual seal."""
    from pathlib import Path
    import json
    import shlex
    utilities = Path(__file__).resolve().parent
    smoke_tool = utilities.parent / "tools/smoke-attestation.py"
    result = {**fields, "next_action": "owner-inline", "required_action": "run-deterministic-check",
              "recovery_surface": "capability-route complete --inline",
              "review_independence_when_owner_run": "degraded",
              "recovery_hint": "Run the recorded deterministic check on current inputs, then complete inline with its fresh attestation. Independence is degraded; a failed or unavailable check stays unverified."}
    recorded = _recorded_smoke(route, node, jobs)
    if recorded is None:
        result["recovery_unavailable"] = "recorded-smoke-command-unavailable"
        return result
    _marker_path, _marker, evidence, data, smoke = recorded
    try:
        smoke.verify_execution_environment(data, data["command"], data["cwd"])
    except (OSError, ValueError, KeyError, TypeError):
        result["recovery_unavailable"] = "recorded-smoke-environment-unavailable"
        return result
    inputs = [item["path"] for item in data["inputs"]]
    config_args = []
    if any(key in data for key in smoke.CONFIG_KEYS):
        # Existing manifests already bind source and snapshot. Locate the current
        # one beside the old snapshot; never mint a new seal as recovery work.
        source = data.get("config_source_path")
        candidates = set()
        for item in inputs:
            candidates.update(Path(item).parent.glob("*.manifest.json"))
        manifests = []
        for candidate in sorted(candidates):
            try:
                manifest = json.loads(candidate.read_text())
                if (manifest.get("source_path") == source
                        and manifest.get("source_sha256") == smoke.digest(source)
                        and manifest.get("snapshot_sha256") == smoke.digest(manifest["snapshot_path"])):
                    manifests.append((candidate, manifest))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if not manifests:
            result["recovery_unavailable"] = "current-sealed-config-manifest-unavailable"
            return result
        config_args = ["--config-manifest", str(manifests[-1][0])]
        # The current manifest contributes its snapshot. Do not preserve an old
        # snapshot as an extra smoke input when config bytes have changed.
        old_snapshots = {item["path"] for item in data["inputs"]
                         if item["path"] != source and item.get("sha256") == data.get("config_sha256")}
        inputs = [item for item in inputs if item not in old_snapshots]
    command = ["python3", str(utilities / "verification-background-lease.py"), "--timeout", "600", "--",
               "python3", str(smoke_tool), "attest"]
    for item in inputs:
        command.extend(["--input", item])
    command.extend([*config_args, "--cwd", data["cwd"], "--output", str(evidence), "--", *data["command"]])
    if not route_file:
        from route_lineage import canonical_route_path
        route_file = canonical_route_path(route["artifact_root"], route["route_id"])
    result.update(recovery_run_command=shlex.join(command),
        recovery_check_command=shlex.join(["python3", str(smoke_tool), "verify", "--attestation", str(evidence)]),
        recovery_complete_command=shlex.join(["python3", str(utilities / "capability-route.py"), "complete", "--inline",
            "--route", str(route_file), "--node", node["id"], "--jobs", str(jobs), "--evidence", str(evidence)]))
    return result


def _review_recovery_fields(node_kind, state="exhausted", *, route=None, node=None, jobs=None, route_file=None):
    fields = _base_recovery_fields(node_kind, state)
    if state == "review-correction-required" and route and node and jobs and route_file:
        try:
            from worker_bootstrap import _lineage_rows, continuation_followup_fields, continuation_followup_text
            from review_source_subject import observe, annotate_rows
            from review_input import read_binding, resolve_input
            history = _lineage_rows(route, jobs).get(node["id"], ())
            previous = open_blocking_verdict(history, "review")
            binding = (read_binding(jobs, previous) if previous and previous.get("review_input_digest") else None)
            reviewed = (resolve_input(route, node, jobs, binding["path"] if binding and not binding.get("producer") else None)
                        if binding else None)
            subject = observe(route, node, worktree=route.get("cwd"), jobs=jobs, reviewed_input=reviewed)
            digest = subject["digest"]
            history = annotate_rows(jobs, history, route=route, node=node, family=subject.get("family"))
            matching = [(status, meta) for status, meta in history
                        if meta.get("_review_subject_digest") == digest]
            prior = open_blocking_verdict(matching, "review")
            guidance = continuation_followup_text(continuation_followup_fields(route, node, prior, jobs, route_file)) if prior else ""
            if guidance:
                fields.update(recovery_hint=guidance, review_attempt_id=prior["attempt_id"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return fields
    if state != "exhausted" or node_kind != "review-worker" or not route or not node or jobs is None:
        return fields
    import base64
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
        source_digest = None
        from review_source_subject import has_review_subject, observe, read_binding
        if has_review_subject(route, node, jobs):
            source_digest = observe(route, node, worktree=route.get("cwd"), jobs=jobs)["digest"]
            bound_rows = []
            for status, meta in rows:
                binding = read_binding(jobs, meta, required=False)
                bound_rows.append((status, meta if binding is None else {
                    **meta, "_review_subject_digest": binding["subject_digest"]
                }))
            rows = bound_rows
        verdicts = [(status, meta) for status, meta in rows
                    if source_digest is None or meta.get("_review_subject_digest") == source_digest
                    if classify_round_row(status, meta, worker_type="review") == "verdict"]
        if source_digest is not None and verdicts and verdicts[-1][1].get("note") in SUCCESS_NOTES:
            return _repeated_pass_fields(fields, rows, verdicts)
        if not verdicts or verdicts[-1][1].get("note") != REVIEW_BLOCKING_NOTE:
            return fields
        latest = verdicts[-1][1]
        encoded_review = latest.get("review_artifact_b64")
        if not encoded_review:
            return fields
        try:
            review_artifact = Path(base64.urlsafe_b64decode(
                encoded_review + "=" * (-len(encoded_review) % 4)).decode("utf-8")).resolve()
            review_artifact.relative_to(Path(route["artifact_root"]).resolve())
        except (ValueError, OSError, UnicodeDecodeError):
            return fields
        if not review_artifact.is_file():
            return fields
        try:
            from dispatch_contract import evidence_digest, observe_terminal_review_failure
            attempt_route = next((generation for generation in lineage
                if generation.get("route_id") == latest.get("route_id")
                and generation.get("route_hash") == latest.get("route_hash")), None)
            attempt_node = (next((item for item in attempt_route.get("nodes", [])
                                  if item.get("id") == node["id"]), None)
                            if attempt_route is not None else None)
            if attempt_node is None:
                return fields
            observe_terminal_review_failure(
                attempt_route, attempt_node, latest["attempt_id"], review_artifact,
                evidence_digest(review_artifact), Path(jobs).read_text(encoding="utf-8").splitlines(),
            )
        except (DispatchContractError, OSError, ValueError, KeyError, TypeError):
            return fields
        import route_authority
        closure = route_authority.decide_continuation(route, action="owner-closure", node=node,
            rows=rows, facts=({"source_subject_digest": source_digest}
                              if source_digest is not None else None))
        if not closure.allowed:
            return fields
        fields.update(recovery_surface="capability-route complete",
            next_action="owner-closure",
            required_action="resolve-review-findings",
            recovery_hint="Record dispositions for the exact blocking review in existing owner-closure evidence; this is owner judgment, not independent PASS.")
        route_path = route_file or canonical_route_path(route["artifact_root"], route["route_id"])
        command = ["python3", str(Path(__file__).with_name("capability-route.py")), "complete",
                   "--route", str(route_path), "--node", node["id"], "--jobs", str(Path(jobs).resolve()),
                   "--attempt-id", latest["attempt_id"], "--evidence", "<current-cycle-file.owner-closure.md>"]
        fields["recovery_check_command"] = shlex.join(command + ["--check"])
        fields["recovery_complete_command"] = shlex.join(command)
        if (review_input.is_review_node(node) and not review_input.has_plan_producer(route, node)
                and latest.get(review_input.KEY)):
            try:
                input_binding = review_input.read_binding(jobs, latest)
            except DispatchContractError as exc:
                fields["revision_unavailable"] = exc.reason
            else:
                if input_binding is None:
                    fields["revision_unavailable"] = "review-input-binding-missing"
                else:
                    answer = route_authority.decide_continuation(route, action="round", node=node,
                        rows=rows, revisions=[{"answers": [latest["attempt_id"]]}],
                        facts=({"source_subject_digest": source_digest}
                               if source_digest is not None else None))
                    if answer.allowed and answer.budget.round_kind == "closure-check":
                        fields["recovery_revise_command"] = shlex.join([
                            "python3", str(Path(__file__).with_name("capability-route.py")), "revise",
                            "--route", str(route_path), "--node", node["id"], "--jobs", str(Path(jobs).resolve()),
                            "--basis", "review-findings", "--answers", latest["attempt_id"],
                            "--evidence", "<current-cycle-corrected-plan-file>",
                            "--author-attempt-id", "<current-owner-attempt-id>"])
        elif review_input.is_review_node(node) and not review_input.has_plan_producer(route, node):
            fields["revision_unavailable"] = "review-input-binding-missing"
    except (OSError, ValueError, KeyError, TypeError):
        # Recovery hints carry no authority and cannot weaken the original refusal.
        return fields
    return fields


def _repeated_pass_fields(fields, rows, subject_verdicts):
    """Next step when the unchanged content already holds its PASS verdicts.

    Only repeating the same content with the same result is refused; the
    refusal names what to do instead of an unresolved-verification dead end.
    Changed content is a new subject with its own budget and is admitted.
    """
    latest = subject_verdicts[-1][1]
    passes = [meta.get("attempt_id") for _status, meta in subject_verdicts
              if meta.get("note") in SUCCESS_NOTES and meta.get("attempt_id")]
    lineage = [meta for status, meta in rows
               if classify_round_row(status, meta, worker_type="review") == "verdict"]
    current = bool(lineage) and lineage[-1].get("attempt_id") == latest.get("attempt_id")
    fields = {**fields, "subject_pass_attempt_ids": "|".join(passes)}
    if current:
        fields.update(next_action="successor-start", required_action="use-current-pass",
            recovery_hint="This exact content already passed (" + (latest.get("attempt_id") or "-")
                + "); continue with the next node's printed start. Changing the reviewed content "
                "admits a fresh verification automatically.")
    else:
        fields.update(required_action="report-unresolved-verification",
            recovery_hint="This content returned to an earlier revision that already passed ("
                + "|".join(passes) + "); change the content to verify it again, or report those "
                "PASS attempts as evidence without claiming a current PASS.")
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



def round_budget(route, node, rows, *, revisions=(), source_subject_digest=None):
    from route_authority import decide_continuation
    facts = ({"source_subject_digest": source_subject_digest}
             if source_subject_digest is not None else None)
    return decide_continuation(route, action="round", node=node, rows=rows,
                               revisions=revisions, facts=facts).budget


def last_verdict_blocking(rows, worker_type):
    """True when the latest verdict round, past any verdict-less tail, is a
    blocking FAIL: the node's gate failed and no later verdict answered it.
    OPERATIONS §5.10 reads a sub-session opened now as a gap retry of the
    unfinished items, never as planned subdivision.
    """
    verdicts = [(status, metadata) for status, metadata in rows if classify_round_row(
        status, metadata, worker_type=metadata.get("worker_type") or worker_type) == "verdict"]
    return _last_round_blocking_verdict(verdicts[-1:], worker_type)


def open_blocking_verdict(rows, worker_type):
    """The latest verdict row of a lineage when it still holds an open 🔴 list, else None.

    A blocking verdict the owner already closed in place (owner-closure,
    `owner_closure_shape == "registered-review"`) is disposed: its findings
    were ruled on, so a later round does not treat them as open. The closure
    check, a producing stage's next-round context and the repeat-failure
    count all read this one judgment. Rows are `(status, metadata)` pairs.
    """
    for status, metadata in reversed(list(rows)):
        if classify_round_row(status, metadata,
                              worker_type=metadata.get("worker_type") or worker_type) != "verdict":
            continue
        if not _last_round_blocking_verdict([(status, metadata)], worker_type):
            return None
        from dispatch_contract import owner_closure_shape
        return None if owner_closure_shape(metadata) else metadata
    return None


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


def marker_round_census(route, node, rows, *, site, revisions=(), independently_reviewed=False,
                        source_subject_digest=None):
    """Compatibility projection of the shared marker decision; no cap means no census."""
    from route_authority import decide_continuation
    try:
        return decide_continuation(route, action="marker", node=node, rows=rows, revisions=revisions,
            facts={"site": site, "independently_reviewed": independently_reviewed,
                   **({"source_subject_digest": source_subject_digest}
                      if source_subject_digest is not None else {})}).census
    except ValueError as error:
        if str(error).startswith("unknown round-census site:"):
            raise
        return None
