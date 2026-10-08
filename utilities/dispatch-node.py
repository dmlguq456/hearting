#!/usr/bin/env python3
"""Materialize a registry route node onto existing adapter dispatch wrappers."""
import argparse, importlib.util, json, os, subprocess, sys
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (
    DispatchContractError,
    GOVERNOR_RESERVATION_ENV,
    parse_registry_metadata,
    resolve_global_registry,
)
from worker_bootstrap import assigned_contract, worker_type_for_kind
from dispatch_lifecycle import (
    FOREGROUND_NOTICE,
    FOREGROUND_SCOPED,
    run_forwarding_termination,
    select_launch_lifecycle,
)
import review_round_cap as REVIEW_ROUND_CAP
import route_authority as ROUTE_AUTHORITY
import dispatch_subsession_advance as SUBSESSION

_route_spec = importlib.util.spec_from_file_location(
    "capability_route", ROOT / "utilities" / "capability-route.py"
)
ROUTE = importlib.util.module_from_spec(_route_spec)
_route_spec.loader.exec_module(ROUTE)

# SD-66 fix-forward: deterministic dispatch_evidence -> wrapper-argument binding
# for dispatch-depth-2 route nodes (PRD §13.7.6, acceptance ③). Only same/cross-harness
# headless fallback hops carry a checked tuple; native-subagent/inline hops are
# not wrapper dispatch and are never consulted here.
FALLBACK_HOPS = {"same-harness-headless", "cross-harness-headless"}
EVIDENCE_TUPLE_FIELDS = (
    "parent_harness", "parent_transport", "parent_sandbox",
    "child_harness", "launch_authority", "status", "probe_source",
)
EVIDENCE_FLAG_MAP = {
    "launch_authority": "--launch-authority",
    "parent_harness": "--parent-harness",
    "parent_transport": "--parent-transport",
    "parent_sandbox": "--parent-sandbox",
    "status": "--nested-eligibility",
    "probe_source": "--eligibility-source",
}
FAILURE_CLASS_FLAG = "--eligibility-failure-class"
CURRENT_PARENT_ENV = {
    "parent_harness": "AGENT_DISPATCH_CURRENT_HARNESS",
    "parent_transport": "AGENT_DISPATCH_CURRENT_TRANSPORT",
    "parent_sandbox": "AGENT_DISPATCH_CURRENT_SANDBOX",
}
PROTECTED_ADAPTER_FLAGS = frozenset({
    "--worktree", "--slug", "--capability", "--capability-mode",
    "--worker-mode", "--mode", "--qa", "--intensity",
    "--dispatch-depth", "--worker-type", "--unit", "--assigned-contract",
    "--owner", "--route-file", "--route-id", "--route-hash", "--route-node",
    "--registry-digest", "--write-scope", "--completion-gate", "--prompt-text",
    "--harness-affinity", "--explicit-adapter", "--parent", "--start", "--register", "--dry-run",
    "--model-role", "--model-profile", "--model", "--reasoning", "--effort",
    "--variant", "--inherit-model-settings",
    "--subsession-id", "--subsession-index", "--subsession-count",
    "--subsession-mode", "--subsession-purpose", "--session-chain-id",
    "--phase-brief", "--stage-authority", "--fixed-file", "--narrow-verify",
    "--expected-round-trips", "--state-dir", "--attempt-id", "--reviewed-evidence",
    "--subsession-worktree",
})


class DispatchNodeError(Exception):
    """Structured fail-loud diagnostic for evidence binding/conflict."""

    def __init__(self, reason, **fields):
        super().__init__(reason)
        self.reason = reason
        self.fields = fields


def reject_generated_argument_overrides(adapter_args):
    """Keep trailing wrapper args from replacing route-generated authority."""

    for token in strip_leading_separator(adapter_args):
        flag = token.split("=", 1)[0]
        if flag in PROTECTED_ADAPTER_FLAGS:
            raise DispatchNodeError(
                "dispatch-generated-argument-override", flag=flag
            )


def _normalized_failure_class(row):
    return row.get("failure_class") or ""


CheckedSelection = namedtuple(
    "CheckedSelection", ("tuple_row", "candidate", "fallback_hop", "ordinal")
)


def candidate_matches_parent(row, parent_identity):
    """True when a sealed candidate row describes the actual launching parent.

    Mirrors validate_parent_identity's comparison without raising, so a caller
    can filter on parent identity *before* ordinal selection and still classify
    an empty result against the unfiltered set.

    `parent_identity is None` is a dispatch-depth-0/manual caller with no
    exported runtime identity: every row matches, and the foreign-parent shadow
    documented in resolve_checked_tuple stays reachable by hand on that path.
    That is deliberate — there is no parent to filter by — and it means this
    class of defect is contained, not eliminated.
    """
    if parent_identity is None:
        return True
    return all(row.get(field) == parent_identity.get(field) for field in CURRENT_PARENT_ENV)


def resolve_checked_tuple(route, node, adapter, parent_identity=None):
    """Resolve the one checked tuple for (node, adapter, actual parent).

    Returns CheckedSelection(tuple_row, candidate, fallback_hop, ordinal) so no
    caller can re-derive the hop/ordinal from a second, divergent walk.

    capability-route.py:_fallback_chain partitions sealed evidence by
    `child_harness == parent_harness` *per row*, so ordinal 1
    (same-harness-headless) and ordinal 2 (cross-harness-headless) both hold
    rows for every sealed parent. Filtering on child_harness alone let a foreign
    parent's row at ordinal 1 shadow this parent's row at ordinal 2. Parent
    filtering is therefore a distinct step that runs BEFORE ordinal selection;
    after it, a given adapter can appear at exactly one ordinal.

    Reason precedence (asserted verbatim by dispatch_node.test.py):
      * parent-filtered set non-empty  -> walk its ordinals; ambiguous-candidate,
        candidate-unsupported, no-top-level-counterpart and
        conflicting-counterparts keep exactly today's meaning;
      * parent-filtered set empty AND adapter-only set non-empty ->
        dispatch-evidence-parent-runtime-mismatch, built from the row today's
        unfiltered walk would have selected, so `mismatch=field:record=…:actual=…`
        is byte-stable;
      * adapter has no row at any ordinal ->
        dispatch-evidence-no-eligible-fallback.
    """
    fallbacks = sorted(
        (f for f in node.get("fallback_hops", []) if f.get("fallback_hop") in FALLBACK_HOPS),
        key=lambda f: f.get("ordinal", 0),
    )
    adapter_hits = [
        (entry, [c for c in entry.get("candidates", []) if c.get("child_harness") == adapter])
        for entry in fallbacks
    ]
    adapter_hits = [(entry, rows) for entry, rows in adapter_hits if rows]
    if not adapter_hits:
        raise DispatchNodeError("dispatch-evidence-no-eligible-fallback", adapter=adapter)
    parent_hits = [
        (entry, [c for c in rows if candidate_matches_parent(c, parent_identity)])
        for entry, rows in adapter_hits
    ]
    parent_hits = [(entry, rows) for entry, rows in parent_hits if rows]
    if not parent_hits:
        # Only foreign-parent tuples were sealed for this adapter: the documented
        # dispatch-depth-0-sealing failure mode (core/OPERATIONS.md §5.10).
        # Classify against the unfiltered set so the typed reason and its field
        # format are unchanged. validate_parent_identity always raises here.
        validate_parent_identity(adapter_hits[0][1][0], parent_identity)
        raise DispatchNodeError(
            "dispatch-evidence-parent-runtime-mismatch", adapter=adapter
        )  # defensive: unreachable
    entry, matches = parent_hits[0]
    if len(matches) > 1:
        raise DispatchNodeError(
            "dispatch-evidence-ambiguous-candidate",
            ordinal=str(entry.get("ordinal")), adapter=adapter,
        )
    candidate = matches[0]
    if candidate.get("status") != "supported":
        raise DispatchNodeError(
            "dispatch-evidence-candidate-unsupported",
            ordinal=str(entry.get("ordinal")), adapter=adapter,
            status=str(candidate.get("status")),
        )
    top_tuples = route.get("dispatch_evidence", {}).get("tuples", [])
    counterparts = [
        t for t in top_tuples
        if all(t.get(f) == candidate.get(f) for f in EVIDENCE_TUPLE_FIELDS)
        and _normalized_failure_class(t) == _normalized_failure_class(candidate)
    ]
    if not counterparts:
        raise DispatchNodeError(
            "dispatch-evidence-no-top-level-counterpart",
            ordinal=str(entry.get("ordinal")), adapter=adapter,
        )
    if len(counterparts) > 1:
        raise DispatchNodeError(
            "dispatch-evidence-conflicting-counterparts",
            ordinal=str(entry.get("ordinal")), adapter=adapter,
            count=str(len(counterparts)),
        )
    return CheckedSelection(
        counterparts[0], candidate,
        str(entry.get("fallback_hop")), int(entry.get("ordinal", 0)),
    )


def select_checked_tuple(route, node, adapter, parent_identity=None):
    """Backward-compatible view of resolve_checked_tuple: the tuple row only."""
    return resolve_checked_tuple(route, node, adapter, parent_identity).tuple_row


def current_parent_identity(environ=None):
    """Return the actual launching runtime identity exported by its wrapper.

    No variables means a dispatch-depth-0/manual caller with no runtime identity to
    validate. A partial identity is never usable evidence and fails closed.
    """
    environ = os.environ if environ is None else environ
    values = {field: environ.get(name) for field, name in CURRENT_PARENT_ENV.items()}
    if not any(values.values()):
        return None
    missing = [CURRENT_PARENT_ENV[field] for field, value in values.items() if not value]
    if missing:
        raise DispatchNodeError(
            "dispatch-evidence-parent-runtime-incomplete",
            missing=",".join(missing),
        )
    return values


def validate_parent_identity(tuple_row, parent_identity):
    """Reject checked evidence compiled for a different actual parent runtime."""
    if parent_identity is None:
        return
    mismatches = {
        field: (str(tuple_row.get(field, "")), str(parent_identity.get(field, "")))
        for field in CURRENT_PARENT_ENV
        if tuple_row.get(field) != parent_identity.get(field)
    }
    if mismatches:
        raise DispatchNodeError(
            "dispatch-evidence-parent-runtime-mismatch",
            mismatch=";".join(
                f"{field}:record={record}:actual={actual}"
                for field, (record, actual) in mismatches.items()
            ),
        )


def strip_leading_separator(adapter_args):
    return adapter_args[1:] if adapter_args[:1] == ["--"] else adapter_args


def extract_adapter_jobs(adapter_args):
    """Accept the historical post-``--`` spelling, then normalize it away."""

    tokens = strip_leading_separator(adapter_args)
    filtered = []
    jobs = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--jobs":
            if index + 1 >= len(tokens):
                raise DispatchNodeError("dispatch-jobs-value-missing")
            jobs.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--jobs="):
            jobs.append(token.split("=", 1)[1])
            index += 1
            continue
        filtered.append(token)
        index += 1
    if any(not value for value in jobs):
        raise DispatchNodeError("dispatch-jobs-value-missing")
    if len(set(jobs)) > 1:
        raise DispatchNodeError("dispatch-jobs-conflict", explicit=",".join(jobs))
    return (jobs[0] if jobs else None), filtered


def has_model_selection(adapter_args):
    tokens = strip_leading_separator(adapter_args)
    return any(
        token in {"--model-role", "--model", "--inherit-model-settings"}
        or token.startswith(("--model-role=", "--model=", "--inherit-model-settings="))
        for token in tokens
    )



# Batched-correction rounds (core/CONVENTIONS.md §1.1, unit Round Protocol).
# A route node dispatched again after a failed gate is a correction round, not a
# fresh pass. The registry is the one durable count of prior attempts for the
# exact route/node, so the round number is derived here and stamped into the
# worker assignment; the owner's free-text prompt cannot silently turn round N
# into "a fresh independent audit" again (observed 2026-08-24 rt-08dd7ba8: twelve
# execute/impl-review rounds, each FAIL on a new finding).
def prior_round_attempts(jobs, route_id, node_id, *, exclude_slug=None, exclude_attempt=None, route=None):
 """Return the prior registry rows for one exact route/node as (cols, metadata) pairs.

 SD-153: callers used to get only (slug, note); `round_budget` needs the row
 status (`cols[1]`) and the full metadata to classify live/unsettled/verdict/
 verdict-less, so this now returns exactly what `review_round_records` does,
 filtered the same way.
 """
 prior=[]
 route_ids = {r["route_id"] for r in ROUTE.review_lineage_routes(route, node_id)} if route else {route_id}
 try:
  lines=Path(jobs).read_text(encoding="utf-8",errors="replace").splitlines()
 except OSError:
  return prior
 for cols, meta in ROUTE.review_round_records(lines, route_ids, node_id,jobs=jobs):
  if exclude_slug and cols[4]==exclude_slug and (meta.get("route_id") or meta.get("route"))==route_id: continue
  if exclude_attempt and meta.get("attempt_id")==exclude_attempt: continue
  prior.append((cols,meta))
 return prior

def subsession_purpose(jobs, route, node, chain_id, declared):
 """OPERATIONS §5.10: after a gate failure or a BLOCKED (unfinished) round a
 sub-session is the gap retry of the unfinished items, never retroactive
 planned subdivision.

 Reads the full-stage census admission reads (sub-session rows are not in
 it), cut at this chain's first row, so register, start and the supervisor's
 later start of one chain all derive the same purpose. A declared gap retry
 stays one; the runtime never relabels a session planned.
 """
 if declared!="planned":
  return declared
 try:
  lines=Path(jobs).read_text(encoding="utf-8",errors="replace").splitlines()
 except OSError:
  return declared
 for index,line in enumerate(lines):
  fields=line.split("\t")
  if len(fields)==6 and parse_registry_metadata(fields[5]).get("session_chain_id")==chain_id:
   lines=lines[:index]
   break
 route_ids=({r["route_id"] for r in ROUTE.review_lineage_routes(route,node["id"])}
            if node.get("kind")=="review-worker" else {route["route_id"]})
 rows=[(cols[1],meta) for cols,meta in ROUTE.review_round_records(lines,route_ids,node["id"],jobs=jobs)]
 worker_type=node.get("worker_type") or ("review" if node.get("kind")=="review-worker" else "test")
 return "gap-retry" if ROUTE_AUTHORITY.gate_unmet(rows,worker_type) else declared

# C-14: only the plan-check/impl-review/test QA anchors carry a review/correction
# budget under CONVENTIONS §1.1. `execute`/`report` are outside that budget (P2-27
# review): execute's own retry mechanism is HEAD-lineage based, not round-counted,
# so excluding it here cannot open an execute-side bypass of the cap.
# The cap applies to every recipe node with kind == "review-worker" -- the PRD
# fixes the kind, not a hand-kept id list -- enumerated exhaustively from
# capabilities/topologies.json. dispatch_node.test.py asserts set equality
# against that file, so a new review node in any recipe breaks the test rather
# than silently escaping the cap. Defined in `review_round_cap.py` (SD-153
# rule 5) so `capability-route.py`'s marker writers share the exact same set
# without a back-import cycle; this is a re-export name, never a call.
ROUND_CAPPED_NODE_IDS = REVIEW_ROUND_CAP.ROUND_CAPPED_NODE_IDS

@dataclass(frozen=True)
class RoundAdmission:
 """The shared admission decision for a launch surface: budget AND SD-154
 auto-revision.

 `auto_revisions` contains every revision marker this call recorded (13.59.3
 rule 8). A read-only preview leaves that tuple empty and reports eligible
 nodes in `planned_revision_nodes`; the budget still accounts for them.
 """
 budget: object
 auto_revisions: tuple = ()
 planned_revision_nodes: frozenset[str] = frozenset()
 reviewed_input: object = None


def _auto_record_revisions(route, node, jobs, rows, *, owner_attempt_id, record=True):
 """SD-154 rule 8: before admitting D's next round, auto-record or preview a revision
 on each upstream N that is `revised-unrecorded` when D's last round was a
 blocking FAIL that basis-verifies against N. Never launches anything --
 only `publish_revision_locked` writes, under its own node lock.
 """
 if not REVIEW_ROUND_CAP.is_round_capped_node(node) or not rows or not owner_attempt_id:
  return ()
 last_status, last_meta = rows[-1]
 worker_type = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
 last_kind = REVIEW_ROUND_CAP.classify_round_row(
  last_status, last_meta, worker_type=last_meta.get("worker_type") or worker_type,
 )
 if last_kind != "verdict":
  return ()
 last_note = last_meta.get("note", "")
 last_blocking = (
  last_note == ROUTE.REVIEW_BLOCKING_NOTE
  or (last_note == "dead-worker-fail" and last_meta.get("failure_class") == "fail")
 )
 last_attempt = last_meta.get("attempt_id")
 if not last_blocking or not last_attempt:
  return ()
 recorded = []
 directory = ROUTE.completion_dir(route["route_id"], jobs=jobs)
 for dep in node.get("depends_on", []):
  dep_node = next((n for n in route.get("nodes", []) if n.get("id") == dep), None)
  if dep_node is None:
   continue
  marker_path = directory / f"{dep}.json"
  if not marker_path.is_file():
   continue
  currency = ROUTE.gate_currency(route, dep_node, marker_path)
  if currency.state != "revised-unrecorded":
   continue  # condition (1): only a revised-but-unrecorded upstream qualifies.
  try:
   marker = json.loads(marker_path.read_text(encoding="utf-8"))
   evidence_path = Path(str((marker.get("evidence") or {}).get("path", "")))
  except (OSError, ValueError):
   continue
  try:
   publish_or_preview = ROUTE.publish_revision_locked if record else ROUTE.preview_revision
   result = publish_or_preview(
    route, dep, evidence_path, basis="review-findings",
    answers=(last_attempt,), author_attempt_id=owner_attempt_id,
    recorded_by="runtime-auto", jobs=jobs,
   )
  except ValueError:
   continue  # Both paths apply the same complete revision proof.
  recorded.append(result["marker"])
 return tuple(recorded)


def admit_round(route, node, jobs, *, owner_attempt_id=None, exclude_slug=None, exclude_attempt=None,
                reviewed_evidence=None, record_auto_revisions=True):
 """The one admission decision every registered launch surface reads.

 Replaces the three separate `len(prior)+1 > max_round` comparisons that
 used to live in dispatch-node.py, dispatch-batch.py and
 stage-dispatch-fallback.py with a single call over the same row census
 (`prior_round_attempts`) and the shared `RoundBudget` derivation. Also runs
 SD-154 rule 8's auto-revision (idempotent: a second call over the same
 state finds each upstream already `current`, not `revised-unrecorded`, and
 records nothing more) before deriving the budget, so a closure-check that
 rule 8 just unlocked is visible in the SAME call's `budget.round_kind`.
 """
 rows = prior_round_attempts(jobs, route["route_id"], node["id"], exclude_slug=exclude_slug,
                             exclude_attempt=exclude_attempt,
                             route=route if node.get("kind") == "review-worker" else None)
 classified_rows = [(cols[1], meta) for cols, meta in rows]
 auto_revisions = _auto_record_revisions(
  route, node, jobs, classified_rows, owner_attempt_id=owner_attempt_id,
  record=record_auto_revisions,
 )
 from review_input import is_review_node, is_spec_review_node, has_plan_producer, read_binding, resolve_input
 if (reviewed_evidence and is_review_node(node) and not has_plan_producer(route,node)
     and not is_spec_review_node(node)):
  verdicts=[(cols,meta) for cols,meta in rows if REVIEW_ROUND_CAP.classify_round_row(
      cols[1],meta,worker_type=meta.get("worker_type") or "review")=="verdict"]
  if verdicts and verdicts[-1][1].get("note")=="completed-review-blocking":
   previous=verdicts[-1][1]
   original=read_binding(jobs,previous)
   if original is None:
    raise DispatchContractError("review-input-revision-binding-required")
   current=resolve_input(route,node,jobs,reviewed_evidence)
   if current["sha256"]!=original["sha256"]:
    publish_or_preview = (ROUTE.publish_review_input_revision if record_auto_revisions
                          else ROUTE.preview_review_input_revision)
    result=publish_or_preview(route,node["id"],current["path"],
        answers=[previous["attempt_id"]],author_attempt_id=owner_attempt_id,
        recorded_by="dispatch-auto",jobs=jobs)
    auto_revisions=(*auto_revisions,result["input_revision"])
 producer_preview = next((item for item in auto_revisions
                          if item.get("node_id")=="plan" and item.get("stage_authority")=="revision"),None)
 input_options = {"producer_preview": producer_preview} if not record_auto_revisions and producer_preview else {}
 reviewed_input = (resolve_input(route,node,jobs,reviewed_evidence,**input_options)
                   if is_review_node(node) and (reviewed_evidence or producer_preview
                                                or route.get("ancestor_plan_refresh")) else None)
 dependency_revisions = ROUTE._dependency_revisions(route, node, jobs, reviewed_input=reviewed_input or {})
 dependency_ids = set(node.get("depends_on", ()))
 dependency_revisions.extend(
  revision.get("revision", {}) for revision in auto_revisions
  if revision.get("node_id") in dependency_ids
 )
 # A pure self-input preview has no on-disk history yet. Only its exact
 # candidate input, proved by the same writer checks, may unlock cap+1.
 if not record_auto_revisions and reviewed_input:
  dependency_revisions.extend(item for item in auto_revisions
      if item.get("node_id")==node["id"]
      and item.get("evidence")=={k:reviewed_input[k] for k in ("path","sha256")})
 budget = REVIEW_ROUND_CAP.round_budget(route, node, classified_rows, revisions=dependency_revisions)
 return RoundAdmission(
  budget=budget,
  auto_revisions=auto_revisions if record_auto_revisions else (),
  planned_revision_nodes=frozenset(
   str(item["node_id"]) for item in auto_revisions if item.get("node_id") in dependency_ids
  ),
  reviewed_input=reviewed_input,
 )


max_review_rounds = REVIEW_ROUND_CAP.max_review_rounds
review_budget_recovery_fields = REVIEW_ROUND_CAP.recovery_fields

def round_protocol_block(budget, rows, worker_type, node_id):
 """Render the assignment block that scopes a correction round.

 `budget` is the `RoundBudget` `admit_round` already computed for this
 launch; `rows` is the same `(cols, metadata)` census it was built from. A
 verdict-less row (crashed, capacity-dead, invalid envelope -- never an
 actual PASS/FAIL/blocking review) renders as "판정 없음(<note>)" in the
 history so a correction round can tell a real prior finding apart from a
 round that never reached one (SD-153 rule 5).
 """
 if budget.next_round<2: return ""
 history_parts=[]
 for cols,meta in rows:
  slug=cols[4]; status=cols[1]; note=meta.get("note","")
  kind=REVIEW_ROUND_CAP.classify_round_row(status,meta,worker_type=meta.get("worker_type") or worker_type)
  if kind=="verdict-less":
   history_parts.append(f"{slug} (판정 없음({note or status or 'open'}))")
  else:
   history_parts.append(f"{slug} ({note or 'open'})")
 history="; ".join(history_parts)
 head=(f"Round protocol (round {budget.next_round} of route node `{node_id}`; prior attempts: {history}):\n"
       "- This is a correction round, not a fresh pass. One correction consumes one unit of the intensity retry budget and must close the whole prior 🔴 list at once.\n")
 if worker_type=="review":
  body=("- Apply your unit's Round Protocol for round >= 2: read the prior round's review artifact first (the owner assignment names it; otherwise locate the latest `round_{N-1}.md` / `*_fix{M}.md` under the cycle's `_internal/`).\n"
        "- Decide the verdict by exactly two questions: is every prior 🔴 closed, and did the delta introduce a correctness defect? Report each prior 🔴 as closed/open/regressed with evidence.\n"
        "- A finding that is neither a prior 🔴 nor a delta-introduced defect is 🟡 `deferred` and cannot flip the verdict. Do not re-audit unchanged material, even if the assignment above asks for a fresh independent review.\n")
 else:
  body=("- Read the failing review artifact first and close every 🔴 it lists together, including the follow-on gaps it named; do not fix one finding and return for another audit.\n"
        "- Record, in the stage artifact, each prior 🔴 with the change that closes it so the closure re-review can verify it item by item.\n")
 return "\n\n"+head+body

def child_env(environ=None):
    """Return the node-wrapper environment without ancestor-only bindings.

    The wrapper receives this node's immutable route through explicit argv.
    An inherited depth-1 owner binding describes a different route identity
    and makes the wrapper reject the otherwise valid node tuple.
    """
    environ = os.environ if environ is None else environ
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("AGENT_OWNER_ROUTE_")
        and not key.startswith("AGENT_DISPATCH_BROKER_")
    }


def requested_launch_lifecycle(adapter_args, environ=None):
    """The lifecycle the wrapper this node starts will run under.

    An explicit ``--launch-lifecycle`` among the adapter arguments wins;
    otherwise the wrapper reselects in this same scope, so this call's own
    selection is the one it lands on.
    """
    extra = strip_leading_separator(adapter_args)
    for index, token in enumerate(extra):
        if token == "--launch-lifecycle" and index + 1 < len(extra):
            return extra[index + 1]
        if token.startswith("--launch-lifecycle="):
            return token.split("=", 1)[1]
    return select_launch_lifecycle(environ)


def dry_run_notice(action):
    """The default action launches nothing: its receipt says so first, before the wrapper's long one."""
    if action == "dry-run":
        print("started=0", flush=True)
        print("next_step=dry run: nothing was registered or started; rerun with --start to launch", flush=True)


def run_launcher(argv, action, lifecycle):
    """Run the wrapper (or owner selector) this node hands its launch to.

    A foreground-scoped ``start`` hosts the worker inside this call: a stop
    request is handed to the wrapper and this call waits for it to stop the
    worker and close the row, instead of killing it mid-cleanup. The receipt
    then says ``interrupted=1`` and the exit code is 128+signal.
    """
    if action != "start" or lifecycle != FOREGROUND_SCOPED:
        return subprocess.run(argv, env=child_env()).returncode
    run = run_forwarding_termination(argv, env=child_env(), capture=False, timeout=None)
    if run.received_signal is None:
        return run.returncode
    sys.stdout.flush()
    print("interrupted=1")
    if run.cleanup_incomplete:
        print("cleanup=incomplete")
    sys.stdout.flush()
    return 128 + int(run.received_signal)


def collect_explicit_evidence(tokens, flags):
    """Scan trailing adapter args for `--flag value` and `--flag=value` forms.

    Non-evidence tokens are opaque and simply walked past; only recognized
    evidence flags are captured (including repeats, to catch a caller
    supplying the same flag twice with disagreeing values).
    """
    values = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        matched = False
        for flag in flags:
            if tok == flag:
                if i + 1 >= len(tokens):
                    raise DispatchNodeError("dispatch-evidence-flag-missing-value", flag=flag)
                values.setdefault(flag, []).append(tokens[i + 1])
                i += 2
                matched = True
                break
            prefix = flag + "="
            if tok.startswith(prefix):
                values.setdefault(flag, []).append(tok[len(prefix):])
                i += 1
                matched = True
                break
        if not matched:
            i += 1
    return values


def bind_dispatch_evidence(route, node, adapter, adapter_args, parent_identity=None):
    """Return the wrapper flags to append for a dispatch-depth-2 route node's --start.

    Never silently overwrites a caller-supplied evidence flag: an explicit
    value equal to the record is accepted without duplication, and any
    explicit/record mismatch (or disagreeing duplicate explicit occurrences)
    stops before wrapper invocation via `DispatchNodeError`.
    """
    tuple_row = resolve_checked_tuple(route, node, adapter, parent_identity).tuple_row
    # Defence in depth: resolve_checked_tuple has already filtered on parent
    # identity before ordinal selection, so this is now a no-op assertion. Keep
    # it — it is the last guard if resolve_checked_tuple's filtering is ever
    # weakened.
    validate_parent_identity(tuple_row, parent_identity)
    record = {flag: str(tuple_row.get(field, "")) for field, flag in EVIDENCE_FLAG_MAP.items()}
    # The failure class is always part of the comparison set, even when the
    # record's value is empty — otherwise an explicit forged value would slip
    # past conflict detection. It is only omitted from the *output* when both
    # sides are empty.
    record[FAILURE_CLASS_FLAG] = _normalized_failure_class(tuple_row)
    trailing = strip_leading_separator(adapter_args)
    explicit = collect_explicit_evidence(trailing, list(record.keys()))
    extra = []
    for flag, value in record.items():
        seen = explicit.get(flag, [])
        if not seen:
            if value or flag != FAILURE_CLASS_FLAG:
                extra += [flag, value]
            continue
        if any(v != value for v in seen):
            raise DispatchNodeError(
                "dispatch-evidence-explicit-conflict",
                flag=flag, explicit=",".join(seen), record=value,
            )
    return extra


def pin_harness_available(route, node, harness, jobs):
    """Whether the route's sealed worker pin can run this node now.

    The same hard checks `dispatch-batch` applies before it places a leg: the node's own harness
    policy names the harness, no active usage limit holds it, and (for a dispatch-depth-2 node)
    the checked tuple for this parent supports it. The soft usage gate is not one of them.
    """
    policy = node.get("harness_policy")
    if isinstance(policy, dict):
        members = {name for band in ("primary", "relief", "last_resort") for name in (policy.get(band) or [])}
        if harness not in members:
            return False
    from dispatch_capacity_evidence import active_limits
    if harness in active_limits(jobs, profile=node.get("model_profile")):
        return False
    if node.get("dispatch_depth") == 2:
        try:
            resolve_checked_tuple(route, node, harness, current_parent_identity())
        except DispatchNodeError:
            return False
    return True


def replacement_task(args, route, node, jobs):
 """A verified death replacement replays its original semantic round verbatim."""
 import dispatch_replacement as replacement
 values=[]
 extra=strip_leading_separator(args.adapter_args)
 for index,token in enumerate(extra):
  if token=="--automatic-retry-of":
   if index+1>=len(extra): raise DispatchContractError("replacement-predecessor-invalid")
   values.append(extra[index+1])
  elif token.startswith("--automatic-retry-of="):
   values.append(token.split("=",1)[1])
 if not values: return None
 if len(values)!=1: raise DispatchContractError("replacement-predecessor-invalid")
 with replacement._locked(jobs) as lines:
  reservation=replacement.source_reservation(jobs,values[0])
  if not reservation: return None  # Existing non-SD157 retry semantics are unchanged.
  record=replacement._read(replacement._record_path(jobs,reservation['family_id']))
  _fields,source,replay=replacement.validate_claim_source(jobs,lines,record)
  if (record['replacement_attempt_id']!=args.attempt_id
      or record['original_attempt_id']!=values[0]
      or record['route_id']!=route['route_id']
      or record['route_hash']!=route['route_hash']
      or source.get('route_node')!=node['id']
      or source.get('harness')!=args.adapter
      or source.get('parent')!=args.parent):
   raise DispatchContractError('replacement-launch-binding-mismatch')
  return replay['task']


def main():
 p=argparse.ArgumentParser(); p.add_argument("--route",help="default: this owner's current route"); p.add_argument("--node",required=True); p.add_argument("--adapter",choices=("claude","codex","opencode"),required=True); p.add_argument("--action",choices=("dry-run","register","start"),default="dry-run"); p.add_argument("--start",dest="action",action="store_const",const="start",help="same as --action start"); p.add_argument("--slug",help="default: <parent>-<node>"); p.add_argument("--qa",default=None); p.add_argument("--parent"); p.add_argument("--jobs"); p.add_argument("--prompt-text",default="Execute the selected immutable route node and emit its completion evidence."); p.add_argument("--subsession-id"); p.add_argument("--subsession-index",type=int); p.add_argument("--subsession-count",type=int); p.add_argument("--subsession-mode",choices=("serial","parallel")); p.add_argument("--subsession-purpose",choices=("planned","gap-retry"),default="planned"); p.add_argument("--session-chain-id"); p.add_argument("--phase-brief"); p.add_argument("--stage-authority",choices=(0,1),type=int,default=1); p.add_argument("--fixed-file",action="append",default=[]); p.add_argument("--narrow-verify"); p.add_argument("--expected-round-trips",type=int); p.add_argument("--state-dir"); p.add_argument("--subsession-worktree"); p.add_argument("--attempt-id"); p.add_argument("adapter_args",nargs=argparse.REMAINDER)
 from review_input import add_arguments, drop_inapplicable, resolve_input
 add_arguments(p)
 a=p.parse_args()
 if a.route is None:
  from owner_route_binding import OwnerRouteBindingError, default_owner_route_file
  try:
   a.route=default_owner_route_file(a.jobs)
  except OwnerRouteBindingError as exc:
   print(f"check=failed\nreason={exc}\ndetail=name the route with --route\nchild_spawned=0"); raise SystemExit(64)
 a.slug=a.slug or f"{a.parent or os.environ.get('AGENT_DISPATCH_SELF_SLUG') or 'stage'}-{a.node}"
 lifecycle=requested_launch_lifecycle(a.adapter_args)
 if a.action=="start" and lifecycle==FOREGROUND_SCOPED:
  print(FOREGROUND_NOTICE,file=sys.stderr,flush=True)
 route=json.loads(Path(a.route).read_text())
 verify=subprocess.run(
  [sys.executable,str(ROOT/"utilities/capability-route.py"),"verify","--route",a.route,
   "--cwd",route["cwd"],"--launch-phase",a.action],
  text=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=False,
 )
 if verify.returncode:
  detail=(verify.stderr or "route launch verification failed").strip()[:1000]
  reason="launch-runtime-root-mismatch" if "launch-runtime-root-mismatch" in detail else "route-record-invalid"
  if "launch-compatibility-tuple-required" in detail: reason="launch-compatibility-tuple-required"
  print("check=failed"); print(f"reason={reason}"); print(f"detail={detail}")
  print("registered=0"); print("started=0"); print("child_spawned=0")
  raise SystemExit(65)
 route=ROUTE_AUTHORITY.route_in_force(route)  # the verified sealed route, with its parent's pin changes
 node=next((x for x in route["nodes"] if x["id"]==a.node),None)
 if not node: raise SystemExit("unknown route node")
 a.reviewed_evidence=drop_inapplicable(node,a.reviewed_evidence)
 # A subsession declaration stays all-or-nothing and still requires the exact
 # attempt identity; a standalone --attempt-id is a batch-reserved leg identity
 # and is valid without any subsession axis (2026-08-06 eiren-m4-r2 frame batch).
 sub_values=(a.subsession_id,a.subsession_index,a.subsession_count,a.subsession_mode,a.session_chain_id,a.phase_brief,a.narrow_verify,a.expected_round_trips)
 if any(value is not None for value in sub_values) and not (all(value is not None for value in sub_values) and a.attempt_id is not None):
  print("check=failed\nreason=subsession-arguments-incomplete\nchild_spawned=0"); raise SystemExit(64)
 if a.subsession_id and a.stage_authority != 0:
  print("check=failed\nreason=subsession-stage-authority-forbidden\nchild_spawned=0"); raise SystemExit(64)
 if not a.subsession_id and a.stage_authority != 1:
  print("check=failed\nreason=stage-authority-zero-without-subsession\nchild_spawned=0"); raise SystemExit(64)
 try:
  trailing_jobs,a.adapter_args=extract_adapter_jobs(a.adapter_args)
  if a.jobs and trailing_jobs and Path(a.jobs).expanduser().resolve(strict=False) != Path(trailing_jobs).expanduser().resolve(strict=False):
   raise DispatchNodeError("dispatch-jobs-conflict",explicit=f"{a.jobs},{trailing_jobs}")
  requested_jobs=a.jobs or trailing_jobs
  reject_generated_argument_overrides(a.adapter_args)
 except DispatchNodeError as e:
  print("check=failed"); print(f"reason={e.reason}")
  for k,v in e.fields.items(): print(f"{k}={v}")
  raise SystemExit(65)
 group=node.get("parallel_group") or node.get("replica_group")
 if group and a.action in {"register", "start"}:
  if a.action == "register" or not os.environ.get(GOVERNOR_RESERVATION_ENV):
   print("check=failed")
   print("reason=parallel-group-batch-required")
   print(f"parallel_group={group}")
   print("child_spawned=0")
   raise SystemExit(65)
 # This route-process check is advisory only: the adapter is a subprocess and
 # does not inherit a jobs.log.lock held here.  The registration fence lives in
 # the adapter's claim_attempt_row critical section.
 if node["kind"]=="resource-runner": print("resource_runner="+str(ROOT/"utilities/resource-runner.py")+"\nroute_node="+a.node); return
 # Frame nodes share the depth-1 selector, including cycle preparation and
 # parent delivery. A map-worker kind alone used to turn them into support
 # workers here and silently discard their bounded frame assignment.
 if node.get("unit") == "plan/frame" and node.get("dispatch_depth") == 1:
  argv=[sys.executable,str(ROOT/"utilities/dispatch-owner.py"),"--"+a.action,
        "--adapter",a.adapter,"--route-evidence",str(Path(a.route).resolve()),
        "--route-node",node["id"],"--slug",a.slug,
        "--prompt-text",a.prompt_text]
  # Omitted when unset: the selector derives it from the route's
  # effective_intensity (dispatch_mode_contract.resolve_qa).
  if a.qa: argv += ["--qa",a.qa]
  if requested_jobs: argv += ["--jobs",requested_jobs]
  if a.attempt_id: argv += ["--attempt-id",a.attempt_id]
  argv += strip_leading_separator(a.adapter_args)
  dry_run_notice(a.action)
  raise SystemExit(run_launcher(argv,a.action,lifecycle))
 try:
  registry=resolve_global_registry(
      ROOT,requested_jobs,int(node.get("dispatch_depth",1)),a.action,child_env())
 except DispatchContractError as e:
  print("check=failed");print(f"reason={e.reason}");print(f"detail={e.detail}");print("child_spawned=0");raise SystemExit(65)
 # Every worker launches in the route cwd. A slice launches in the worktree its
 # sealed manifest names (the route cwd, or a linked worktree of the same
 # repository); the route itself is still verified at the route cwd above.
 launch_worktree=route["cwd"]
 if a.subsession_id and a.action=="register" and a.subsession_worktree: launch_worktree=a.subsession_worktree
 if a.subsession_id and a.action=="start":
  # Defect F3. A slice may only start once the chain it belongs to has been
  # sealed. It refuses exactly the orphan: a row carrying a chain identity that
  # no manifest names, which no surface can ever aggregate
  # (`complete_subsession_stage` reads the manifest, finds nothing, and the
  # chain stalls with the slice's work already done). Observed in W7G.
  #
  # Safe for both live start surfaces: `stage-session-chain.py` persists the
  # manifest AFTER its register loop and BEFORE it starts index 1, and the
  # supervisor advance of indexes 2..N reads that same sealed pointer much
  # later.
  #
  # PARALLEL SUBDIVISION (SD-103, routing-flex): `subdivision_batch_admission.
  # start_admitted_batch` seals the manifest at the same pointer after every
  # slice registered and before the first start, so a parallel slice passes
  # this gate for the same reason a serial one does. `SubsessionChainSealTest.
  # test_parallel_admission_persists_before_start` pins that.
  manifest=SUBSESSION.load_chain_manifest(registry.path,a.session_chain_id)
  pointer=SUBSESSION.chain_manifest_pointer_path(registry.path,a.session_chain_id)
  sealed=None
  if isinstance(manifest,dict):
   for session in (manifest.get("sessions") or []):
    if not isinstance(session,dict): continue
    if str(session.get("subsession_id"))==a.subsession_id:
     sealed=session
     break
  # The manifest is on-disk JSON: coerce, never trust. `"index": null` used to
  # raise TypeError and print a traceback instead of the typed refusal envelope
  # every other refusal here emits (review round 1, item 8).
  try:
   sealed_index=int(sealed.get("index")) if sealed is not None else None
  except (TypeError,ValueError):
   sealed_index=None
  if sealed is None or str(sealed.get("attempt_id"))!=str(a.attempt_id) or sealed_index!=int(a.subsession_index):
   print("check=failed")
   print("reason=subsession-chain-manifest-unsealed")
   print(f"session_chain_id={a.session_chain_id}")
   print(f"subsession_id={a.subsession_id}")
   print(f"manifest_pointer={pointer}")
   print(f"manifest_present={int(isinstance(manifest,dict))}")
   print(f"subsession_declared={int(sealed is not None)}")
   print("child_spawned=0")
   raise SystemExit(64)
  sealed_worktree=str(manifest.get("worktree") or route["cwd"])
  if a.subsession_worktree and Path(a.subsession_worktree).resolve(strict=False)!=Path(sealed_worktree).resolve(strict=False):
   print("check=failed")
   print("reason=subsession-worktree-mismatch")
   print(f"subsession_worktree={a.subsession_worktree}")
   print(f"sealed_worktree={sealed_worktree}")
   print("child_spawned=0")
   raise SystemExit(64)
  launch_worktree=sealed_worktree
 dry_run_notice(a.action)
 print("completion_marker="+str(ROUTE.completion_dir(route["route_id"],jobs=registry.path)/(node["id"]+".json")))
 try:
  worker_type=worker_type_for_kind(node["kind"])
 except ValueError as e:
  raise SystemExit(str(e))
 # A sealed `--pin worker=<harness>` beats the requested adapter while that harness can run this node
 # (CONVENTIONS §2.1: an explicit route pin leads the selection order). The request is kept as `explicit_adapter`.
 # A replay keeps its predecessor's harness and a subsession keeps the one its sealed manifest names.
 overridden_adapter=None
 replaying=any(t=="--automatic-retry-of" or t.startswith("--automatic-retry-of=") for t in strip_leading_separator(a.adapter_args))
 if worker_type not in {"owner","frame"} and not a.subsession_id and not replaying:
  a.adapter,overridden_adapter=ROUTE_AUTHORITY.pinned_launch_harness(
   route,worker_type=worker_type,requested=a.adapter,
   available=lambda harness:pin_harness_available(route,node,harness,registry.path))
 wrapper=ROOT/"adapters"/a.adapter/"bin"/"dispatch-headless.py"
 # SD-165: a borrowed part reads its origin capability's contract; the route binding stays the host's.
 contract=assigned_contract(capability=(node.get("part") or "").partition(":")[0] or route["capability"],worker_type=worker_type,route_node=node["id"],completion_gate=node.get("completion_gate"),root=ROOT)
 try:
  original_task=replacement_task(a,route,node,registry.path)
 except DispatchContractError as exc:
  print("check=failed"); print(f"reason={exc.reason}"); print("child_spawned=0")
  raise SystemExit(65)
 if a.subsession_id:
  a.subsession_purpose=subsession_purpose(registry.path,route,node,a.session_chain_id,a.subsession_purpose)
 prior_rounds=prior_round_attempts(registry.path,route["route_id"],node["id"],exclude_slug=a.slug,exclude_attempt=a.attempt_id,
                                  route=route if node.get("kind")=="review-worker" else None) if not a.subsession_id and original_task is None else []
 # SD-153/SD-154: `admit_round` (budget + rule-8 auto-revision) is the one
 # admission decision every registered launch surface (this main(),
 # dispatch-batch.py, stage-dispatch-fallback.py) reads -- no surface keeps
 # its own `len(prior)+1 > max_round` comparison, or its own auto-revision
 # copy, any more. A subsession carries no stage-gate authority (common.md),
 # so it gets the same no-op budget the manual empty-`prior_rounds` call
 # always produced, without touching auto-revision at all.
 try:
  admission=(
   admit_round(route,node,registry.path,owner_attempt_id=os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or a.parent,exclude_slug=a.slug,exclude_attempt=a.attempt_id,reviewed_evidence=a.reviewed_evidence,record_auto_revisions=a.action!="dry-run")
   if not a.subsession_id and original_task is None
   else RoundAdmission(budget=REVIEW_ROUND_CAP.round_budget(route,node,[],revisions=()))
  )
 except (DispatchContractError, ValueError) as exc:
  print("check=failed"); print("reason="+getattr(exc,"reason",str(exc))); print("child_spawned=0")
  raise SystemExit(65)
 round_budget=admission.budget
 if REVIEW_ROUND_CAP.is_round_capped_node(node) and not a.subsession_id and original_task is None:
  if round_budget.state=="blocked-live":
   print("check=failed")
   print("reason=prior-attempt-still-live")
   print(f"route_id={route['route_id']}")
   print(f"route_node={a.node}")
   print("child_spawned=0")
   raise SystemExit(78)
  if round_budget.state=="blocked-unsettled":
   print("check=failed")
   print("reason=round-unsettled")
   print(f"route_id={route['route_id']}")
   print(f"route_node={a.node}")
   print("child_spawned=0")
   raise SystemExit(65)
  if round_budget.state=="exhausted":
   print("check=failed")
   print("reason=review-round-budget-exhausted")
   print(f"route_id={route['route_id']}")
   print(f"route_node={a.node}")
   print(f"effective_intensity={route['effective_intensity']}")
   print(f"round={round_budget.next_round}")
   print(f"max_round={round_budget.cap}")
   for key,value in review_budget_recovery_fields(node.get("kind"), route=route, node=node, jobs=registry.path, route_file=a.route).items():
    print(f"{key}={value}")
   print("child_spawned=0")
   raise SystemExit(65)
  if round_budget.state=="verdictless-bound":
   print("check=failed")
   print("reason=review-verdictless-bound")
   print(f"route_id={route['route_id']}")
   print(f"route_node={a.node}")
   print(f"effective_intensity={route['effective_intensity']}")
   print(f"round={round_budget.next_round}")
   print(f"max_round={round_budget.cap}")
   for key,value in review_budget_recovery_fields(node.get("kind"),"verdictless-bound", route=route, node=node, jobs=registry.path, route_file=a.route).items():
    print(f"{key}={value}")
   print("child_spawned=0")
   raise SystemExit(65)
 try:
  retry_values=[]
  extra=strip_leading_separator(a.adapter_args)
  for index,token in enumerate(extra):
   if token=="--automatic-retry-of" and index+1<len(extra): retry_values.append(extra[index+1])
   elif token.startswith("--automatic-retry-of="): retry_values.append(token.split("=",1)[1])
  if len(retry_values)>1: raise DispatchContractError("replacement-predecessor-invalid")
  review_candidate=(admission.reviewed_input
                    if a.action=="dry-run" and admission.reviewed_input is not None and not retry_values
                    else resolve_input(route,node,registry.path,a.reviewed_evidence,
                                       retry_of=retry_values[0] if retry_values else None))
  if review_candidate is not None: a.reviewed_evidence=review_candidate["path"]
 except DispatchContractError as e:
  print("check=failed");print(f"reason={e.reason}");print(f"detail={e.detail}");print("child_spawned=0");raise SystemExit(65)
 prompt_text=(original_task if original_task is not None else
              a.prompt_text+round_protocol_block(round_budget,prior_rounds,worker_type,node["id"]))
 if round_budget.correction_round: print(f"correction_round={round_budget.correction_round}")
 argv=[sys.executable,str(wrapper),"--"+a.action,"--worktree",launch_worktree,"--slug",a.slug,"--capability",route["capability"],"--capability-mode",route["capability_mode"],"--intensity",route["effective_intensity"],"--dispatch-depth",str(node.get("dispatch_depth",1)),"--worker-type",worker_type,"--unit",node.get("unit",""),"--assigned-contract",contract,"--owner",route["capability"],"--route-file",str(Path(a.route).resolve()),"--route-id",route["route_id"],"--route-hash",route["route_hash"],"--route-node",node["id"],"--registry-digest",route["registry_digest"],"--write-scope",";".join(node["write_scope"]),"--completion-gate",node["completion_gate"],"--jobs",str(registry.path),"--prompt-text",prompt_text]
 if a.reviewed_evidence: argv += ["--reviewed-evidence",a.reviewed_evidence]
 # Reuse existing stage specialization declarations when they match this
 # harness/type; every other launch still gets the default typed worker home.
 if a.adapter=="claude" and worker_type=="stage" and (ROOT/"profiles"/(contract+".yaml")).is_file():
  argv += ["--profile",contract]
 unit=node.get("unit","")
 if unit and not unit.startswith("_kernel/"):
  argv += ["--worker-mode",unit]
 # Omitted when unset: the wrapper derives it from --intensity
 # (dispatch_mode_contract.resolve_qa, CONVENTIONS §1.1, single SoT).
 if a.qa: argv += ["--qa",a.qa]
 affinity=node.get("harness_affinity")
 if affinity: argv += ["--harness-affinity",affinity]
 if overridden_adapter: argv += ["--explicit-adapter",overridden_adapter]
 if node.get("dispatch_depth")==2:
  if not a.parent: raise SystemExit("dispatch-depth-2 route node requires --parent")
  argv += ["--parent",a.parent]
  try:
   argv += bind_dispatch_evidence(
       route, node, a.adapter, a.adapter_args,
       parent_identity=current_parent_identity(),
   )
  except DispatchNodeError as e:
   print("check=failed"); print(f"reason={e.reason}")
   for k,v in e.fields.items(): print(f"{k}={v}")
   raise SystemExit(65)
 if a.subsession_id:
  argv += ["--subsession-id",a.subsession_id,"--subsession-index",str(a.subsession_index),"--subsession-count",str(a.subsession_count),"--subsession-mode",a.subsession_mode,"--subsession-purpose",a.subsession_purpose,"--session-chain-id",a.session_chain_id,"--phase-brief",a.phase_brief,"--stage-authority",str(a.stage_authority),"--narrow-verify",a.narrow_verify,"--expected-round-trips",str(a.expected_round_trips)]
  for fixed_file in a.fixed_file: argv += ["--fixed-file",fixed_file]
  if a.state_dir: argv += ["--state-dir",a.state_dir]
 # Batch-reserved attempt identity travels through this named argument for every
 # leg shape; adapter_args rejects it as a protected override (the 2026-08-06
 # eiren-m4 frame batch died there when only the subsession path forwarded it).
 if a.attempt_id: argv += ["--attempt-id",a.attempt_id]
 argv += ["--model-role",node.get("role","fast implementer")]
 if node.get("model_profile"):
  argv += ["--model-profile",node["model_profile"]]
 argv += strip_leading_separator(a.adapter_args)
 raise SystemExit(run_launcher(argv,a.action,lifecycle))
if __name__=="__main__": main()
