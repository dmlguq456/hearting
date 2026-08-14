#!/usr/bin/env python3
"""Materialize a registry route node onto existing adapter dispatch wrappers."""
import argparse, importlib.util, json, os, subprocess, sys
from collections import namedtuple
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (
    DispatchContractError,
    GOVERNOR_RESERVATION_ENV,
    resolve_global_registry,
)
from worker_bootstrap import assigned_contract, worker_type_for_kind

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
    "--harness-affinity", "--parent", "--start", "--register", "--dry-run",
    "--model-role", "--model-profile", "--model", "--reasoning", "--effort",
    "--variant", "--inherit-model-settings",
    "--subsession-id", "--subsession-index", "--subsession-count",
    "--subsession-mode", "--subsession-purpose", "--session-chain-id",
    "--phase-brief", "--stage-authority", "--fixed-file", "--narrow-verify",
    "--expected-round-trips", "--state-dir", "--attempt-id",
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


def main():
 p=argparse.ArgumentParser(); p.add_argument("--route",required=True); p.add_argument("--node",required=True); p.add_argument("--adapter",choices=("claude","codex","opencode"),required=True); p.add_argument("--action",choices=("dry-run","register","start"),default="dry-run"); p.add_argument("--slug",required=True); p.add_argument("--qa",default="standard"); p.add_argument("--parent"); p.add_argument("--jobs"); p.add_argument("--prompt-text",default="Execute the selected immutable route node and emit its completion evidence."); p.add_argument("--subsession-id"); p.add_argument("--subsession-index",type=int); p.add_argument("--subsession-count",type=int); p.add_argument("--subsession-mode",choices=("serial","parallel")); p.add_argument("--subsession-purpose",choices=("planned","gap-retry"),default="planned"); p.add_argument("--session-chain-id"); p.add_argument("--phase-brief"); p.add_argument("--stage-authority",choices=(0,1),type=int,default=1); p.add_argument("--fixed-file",action="append",default=[]); p.add_argument("--narrow-verify"); p.add_argument("--expected-round-trips",type=int); p.add_argument("--state-dir"); p.add_argument("--attempt-id"); p.add_argument("adapter_args",nargs=argparse.REMAINDER)
 a=p.parse_args(); route=json.loads(Path(a.route).read_text()); subprocess.run([sys.executable,str(ROOT/"utilities/capability-route.py"),"verify","--route",a.route,"--cwd",route["cwd"]],check=True,stdout=subprocess.DEVNULL)
 node=next((x for x in route["nodes"] if x["id"]==a.node),None)
 if not node: raise SystemExit("unknown route node")
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
 if node["kind"]=="resource-runner": print("resource_runner="+str(ROOT/"utilities/resource-runner.py")+"\nroute_node="+a.node); return
 try:
  registry=resolve_global_registry(
      ROOT,requested_jobs,int(node.get("dispatch_depth",1)),a.action,child_env())
 except DispatchContractError as e:
  print("check=failed");print(f"reason={e.reason}");print(f"detail={e.detail}");print("child_spawned=0");raise SystemExit(65)
 print("completion_marker="+str(ROUTE.completion_dir(route["route_id"],jobs=registry.path)/(node["id"]+".json")))
 wrapper=ROOT/"adapters"/a.adapter/"bin"/"dispatch-headless.py"
 try:
  worker_type=worker_type_for_kind(node["kind"])
 except ValueError as e:
  raise SystemExit(str(e))
 contract=assigned_contract(capability=route["capability"],worker_type=worker_type,route_node=node["id"],completion_gate=node.get("completion_gate"),root=ROOT)
 argv=[sys.executable,str(wrapper),"--"+a.action,"--worktree",route["cwd"],"--slug",a.slug,"--capability",route["capability"],"--capability-mode",route["capability_mode"],"--qa",a.qa,"--intensity",route["effective_intensity"],"--dispatch-depth",str(node.get("dispatch_depth",1)),"--worker-type",worker_type,"--unit",node.get("unit",""),"--assigned-contract",contract,"--owner",route["capability"],"--route-file",str(Path(a.route).resolve()),"--route-id",route["route_id"],"--route-hash",route["route_hash"],"--route-node",node["id"],"--registry-digest",route["registry_digest"],"--write-scope",";".join(node["write_scope"]),"--completion-gate",node["completion_gate"],"--jobs",str(registry.path),"--prompt-text",a.prompt_text]
 unit=node.get("unit","")
 if unit and not unit.startswith("_kernel/"):
  argv += ["--worker-mode",unit]
 affinity=node.get("harness_affinity")
 if affinity: argv += ["--harness-affinity",affinity]
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
 raise SystemExit(subprocess.run(argv, env=child_env()).returncode)
if __name__=="__main__": main()
