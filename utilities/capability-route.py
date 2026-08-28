#!/usr/bin/env python3
"""Compile, verify, and complete immutable capability routes."""
from __future__ import annotations
import argparse, contextlib, fcntl, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capability_topology", ROOT/"tools/capability_topology.py")
TOPO = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(TOPO)
DEFAULTS_SPEC = importlib.util.spec_from_file_location("dispatch_defaults", ROOT/"utilities/dispatch-defaults.py")
DEFAULTS = importlib.util.module_from_spec(DEFAULTS_SPEC); DEFAULTS_SPEC.loader.exec_module(DEFAULTS)
VALID_AFFINITY = DEFAULTS.AFFINITY_VALUES | {"unspecified"}
sys.path.insert(0, str(ROOT/"utilities"))
from dispatch_contract import (
    CANONICAL_PARENT_TRANSPORTS,
    DispatchContractError,
    EXECUTION_SURFACES,
    FALLBACK_HOPS,
    PARENT_TRANSPORT_BY_DISPATCH_DEPTH,
    WRAPPER_PARENT_SANDBOXES,
    WRAPPER_TRANSPORTS,
    _atomic_registry_replace,
    agent_home_equivalent,
    attempt_process_quiescence,
    completion_marker_is_current,
    dispatch_state_roots,
    ensure_global_registry_writable,
    parse_registry_metadata,
    resolve_agent_home,
    resolve_completed_alias,
    resolve_dangling_registry,
    resolve_dispatch_state_root,
    stable_state_root,
    validate_attempt_metadata,
)
from stage_session_contract import load_manifest
from dispatch_degradation import record_degradation  # noqa: E402
from replica_batch_contract import verify_manifest as verify_batch_manifest  # noqa: E402
ORDER = {"direct":0,"quick":1,"standard":2,"strong":3,"thorough":4,"adversarial":5}
TRACKING = {"tracked", "untracked"}
GATE_FIELDS = {"spec_read", "drift_verdict", "workflow_mode", "artifact_guard"}
NESTED_STATUSES = {"supported", "unsupported", "unknown"}
NESTED_FIELDS = {
    "parent_harness", "parent_transport", "parent_sandbox", "child_harness",
    "launch_authority", "status", "probe_source", "probe_time", "failure_class",
}
NESTED_SCOPE_FIELDS = {
    "checked_worktree", "failure_scope", "codex_command",
    "retry_on_isolated_worktree",
}
NESTED_FAILURE_SCOPES = {
    "none", "exact-worktree", "runtime-global", "parent-runtime", "tuple-contract",
}
CODEX_COMMAND_STATES = {"ok", "unavailable", "unchecked", "not-applicable"}
DISPATCH_EVIDENCE_SCOPE_VERSION = 1
BROKER_FIELDS = {"broker_root", "broker_instance"}  # historical v1
BROKER_FIELDS_V2 = {"broker_root"}                   # historical v2
DISPATCH_CONTRACT_VERSION = 3
FALLBACK_ORDER = ["same-harness-headless", "cross-harness-headless", "native-subagent", "inline"]
ROUTE_SCHEMA_VERSION = 2
VALIDATION_BASIS_VERSION = 1
LAUNCH_COMPATIBILITY_TUPLE_VERSION = 1
CONTINUATION_CONTRACT_VERSION = 1
_LAUNCH_CODE_ANCHORS = (
    "core/CORE.md",
    "harness-manifest.json",
    "capabilities/topologies.json",
    "manifest.json",
)
_LAUNCH_ROOT_IDENTITY_CACHE = {}
_LAUNCH_CONTENT_DIGEST_CACHE = {}
_LAUNCH_SOURCE_REVISION_CACHE = {}
_RUNTIME_ACTIVATION = None
# Only dispatch-depth-2 nodes receive a checked `fallback_hops` chain, so they are
# the sole consumers of `dispatch_evidence.tuples`.
EVIDENCE_CONSUMER_DISPATCH_DEPTH = 2
REGISTERED_HEADLESS_EVIDENCE_FIELDS = {
    "harness", "transport", "surface", "status", "probe_source", "probe_time",
}
REGISTERED_HEADLESS_STATUSES = {"supported", "unsupported", "unknown"}
REGISTERED_HEADLESS_HARNESSES = {"claude", "codex", "opencode"}
NATIVE_SURFACES = {
    "codex": "codex-native-subagent",
    "claude": "claude-subagent",
}
NATIVE_EVIDENCE_FIELDS = {
    "harness",
    "transport",
    "execution_surface",
    "registered_worker",
    "status",
    "check_source",
}


def _validate_registered_headless_evidence(evidence):
    """Normalize quick eligibility; every invalid/empty case has one failure enum."""

    if not isinstance(evidence, dict):
        raise ValueError("quick-headless-unavailable")
    candidates = evidence.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("quick-headless-unavailable")
    normalized = []
    seen_harnesses = set()
    for row in candidates:
        if not isinstance(row, dict) or not REGISTERED_HEADLESS_EVIDENCE_FIELDS.issubset(row):
            raise ValueError("quick-headless-unavailable")
        if row["status"] not in REGISTERED_HEADLESS_STATUSES:
            raise ValueError("quick-headless-unavailable")
        if row["transport"] != "headless" or row["surface"] != "registered-headless":
            raise ValueError("quick-headless-unavailable")
        if row["harness"] not in REGISTERED_HEADLESS_HARNESSES:
            raise ValueError("quick-headless-unavailable")
        if row["harness"] in seen_harnesses:
            raise ValueError("quick-headless-unavailable")
        if not row["probe_source"] or not row["probe_time"]:
            raise ValueError("quick-headless-unavailable")
        seen_harnesses.add(row["harness"])
        normalized.append({key: row[key] for key in sorted(REGISTERED_HEADLESS_EVIDENCE_FIELDS)})
    if not any(row["status"] == "supported" for row in normalized):
        raise ValueError("quick-headless-unavailable")
    return sorted(normalized, key=lambda row: row["harness"])

def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode()

def route_hash(payload):
    bare={k:v for k,v in payload.items() if k not in ("route_hash","route_id")}
    return "sha256:"+hashlib.sha256(canonical(bare)).hexdigest()

def _runtime_activation_module():
    """Load the installer identity implementation without copying its convention."""
    global _RUNTIME_ACTIVATION
    if _RUNTIME_ACTIVATION is None:
        install_root=ROOT/"tools"/"install"
        if str(install_root) not in sys.path:
            sys.path.insert(0,str(install_root))
        spec=importlib.util.spec_from_file_location(
            "_capability_route_runtime_activation",
            install_root/"runtime_activation.py",
        )
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        _RUNTIME_ACTIVATION=module
    return _RUNTIME_ACTIVATION

def _launch_source_revision(path):
    resolved=Path(path).resolve(strict=False)
    key=str(resolved)
    if key not in _LAUNCH_SOURCE_REVISION_CACHE:
        _LAUNCH_SOURCE_REVISION_CACHE[key]=_runtime_activation_module().source_revision(resolved)
    return _LAUNCH_SOURCE_REVISION_CACHE[key]

def _launch_content_digest(path):
    """Digest only immutable code anchors, representing missing anchors explicitly."""
    resolved=Path(path).resolve(strict=False)
    key=str(resolved)
    if key not in _LAUNCH_CONTENT_DIGEST_CACHE:
        rows=[]
        for relative in _LAUNCH_CODE_ANCHORS:
            anchor=resolved/relative
            try:
                data=anchor.read_bytes() if anchor.is_file() else None
            except OSError:
                data=None
            rows.append({
                "anchor":relative,
                "state":"file" if data is not None else "missing",
                "sha256":hashlib.sha256(data).hexdigest() if data is not None else None,
            })
        _LAUNCH_CONTENT_DIGEST_CACHE[key]="sha256:"+hashlib.sha256(canonical(rows)).hexdigest()
    return _LAUNCH_CONTENT_DIGEST_CACHE[key]

def _launch_root_identity(kind, path, *, resolver_identity=None):
    """Return one memoized code-root identity or runtime-bound mutable path identity."""
    resolved=Path(path).expanduser().resolve(strict=False)
    resolver_key=None
    if resolver_identity is not None:
        resolver_key=(
            resolver_identity.get("path"), resolver_identity.get("release_id"),
            resolver_identity.get("content_digest"),
        )
    key=(kind,str(resolved),resolver_key)
    if key not in _LAUNCH_ROOT_IDENTITY_CACHE:
        if resolver_identity is None:
            release_id=_launch_source_revision(resolved)
            content_digest=_launch_content_digest(resolved)
        else:
            release_id=resolver_identity["release_id"]
            content_digest=resolver_identity["content_digest"]
        binding_digest="sha256:"+hashlib.sha256(canonical({
            "kind":kind,"path":str(resolved),"release_id":release_id,
            "content_digest":content_digest,
        })).hexdigest()
        _LAUNCH_ROOT_IDENTITY_CACHE[key]={
            "kind":kind,"path":str(resolved),"release_id":release_id,
            "content_digest":content_digest,"binding_digest":binding_digest,
        }
    return json.loads(json.dumps(_LAUNCH_ROOT_IDENTITY_CACHE[key]))

def launch_compatibility_tuple(*, artifact_root, jobs=None, cwd=None):
    """Compute v1 bounded launch identities without hashing mutable state contents."""
    runtime_root=Path(resolve_agent_home()).resolve(strict=False)
    runtime_identity=_launch_root_identity("runtime_root",runtime_root)
    grounding_cwd=Path(cwd if cwd is not None else Path.cwd()).resolve(strict=False)
    result={
        "tuple_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
        "registry_root":_launch_root_identity("registry_root",TOPO.ROOT),
        "launch_home":_launch_root_identity("launch_home",runtime_root),
        "runtime_root":runtime_identity,
        "grounding_roots":{
            "cwd":_launch_root_identity("grounding_cwd",grounding_cwd),
            "artifact_root":_launch_root_identity(
                "grounding_artifact_root",artifact_root,
                resolver_identity=runtime_identity,
            ),
        },
        "wrapper_root":_launch_root_identity("wrapper_root",runtime_root/"adapters"),
    }
    try:
        jobs_path=resolve_dispatch_state_root(resolve_agent_home(),jobs)/"jobs.log"
        result["jobs_path"]=_launch_root_identity(
            "jobs_path",jobs_path,resolver_identity=runtime_identity,
        )
    except (DispatchContractError,OSError,ValueError) as exc:
        reason=exc.reason if isinstance(exc,DispatchContractError) else type(exc).__name__
        unresolved={
            "kind":"jobs_path","path":None,
            "release_id":runtime_identity["release_id"],
            "content_digest":runtime_identity["content_digest"],
            "unresolved":reason,
        }
        unresolved["binding_digest"]="sha256:"+hashlib.sha256(canonical(unresolved)).hexdigest()
        result["jobs_path"]=unresolved
    return result

def _launch_tuple_roots(payload):
    roots={key:payload.get(key) for key in (
        "registry_root","launch_home","runtime_root","wrapper_root","jobs_path",
    )}
    grounding=payload.get("grounding_roots")
    roots["grounding_roots.cwd"]=grounding.get("cwd") if isinstance(grounding,dict) else None
    roots["grounding_roots.artifact_root"]=(
        grounding.get("artifact_root") if isinstance(grounding,dict) else None
    )
    return roots

_GIT_SHA=re.compile(r"[0-9a-f]{40}")

def _grounding_cwd_lineage_ok(path, sealed_release, actual_release):
    """SD-107 × SD-67/69: the route cwd is the mutation worktree, so its HEAD legitimately
    moves during the route (an execute stage dirties it; the owner commits after the gate).
    Accept that drift only along the sealed revision's first-parent line — same HEAD with a
    dirty suffix, or a HEAD whose first-parent history contains the sealed commit. Any other
    shape (rebase, reset, foreign checkout, non-git tree) stays a mismatch."""
    def base(value):
        if not isinstance(value,str):
            return None
        head=value.split("+",1)[0]
        return head if _GIT_SHA.fullmatch(head) else None
    sealed=base(sealed_release); actual=base(actual_release)
    if sealed is None or actual is None:
        return False
    if sealed == actual:
        return True
    try:
        probe=subprocess.run(
            ["git","-C",str(path),"rev-list","--first-parent",actual],
            text=True,capture_output=True,timeout=30,
        )
    except (OSError,subprocess.TimeoutExpired):
        return False
    if probe.returncode != 0:
        return False
    return sealed in probe.stdout.split()

def _jobs_path_alias_relieves_mismatch(route, expected, actual):
    """SD-112 §13.33.2-(3) decision 1: a `jobs_path`-only mismatch may be
    relieved by a `completed`, structurally-valid migration-alias record --
    and only that axis. The sealed tuple and open-row `jobs_path` are never
    rewritten; this only widens what `revalidate_launch_compatibility`
    accepts as equivalent. `expected` is the sealed (legacy) jobs_path
    identity, `actual` this process's fresh (current) one."""
    expected_path=expected.get("path")
    actual_path=actual.get("path")
    if not expected_path or not actual_path:
        return False
    try:
        stable_root=stable_state_root(os.environ)
    except DispatchContractError:
        return False
    record=resolve_completed_alias(stable_root,expected_path)
    if record is None:
        return False
    target=(record.get("stable_jobs_identity") or {}).get("path")
    if target != actual_path:
        return False
    record_route_hash=record.get("route_hash")
    if record_route_hash is not None and record_route_hash != route.get("route_hash"):
        return False
    return True

def revalidate_launch_compatibility(route):
    """Compare a route's sealed launch tuple with this process's current roots."""
    sealed=route.get("launch_compatibility_tuple")
    if sealed is None:
        return True,{"tuple":"absent-legacy"}
    fresh={"contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION}
    fresh.update(launch_compatibility_tuple(
        artifact_root=route.get("artifact_root","."),cwd=route.get("cwd","."),
    ))
    mismatches={}
    if not isinstance(sealed,dict):
        return False,{"tuple":{"expected":sealed,"actual":fresh}}
    for field in ("contract_version","tuple_version"):
        if sealed.get(field) != fresh.get(field):
            mismatches[field]={"expected":sealed.get(field),"actual":fresh.get(field)}
    expected_roots=_launch_tuple_roots(sealed)
    actual_roots=_launch_tuple_roots(fresh)
    identity_fields=("kind","path","release_id","content_digest","binding_digest")
    for name,expected in expected_roots.items():
        actual=actual_roots[name]
        if not isinstance(expected,dict) or not isinstance(actual,dict):
            mismatches[name]={"expected":expected,"actual":actual}
            continue
        changed={field:{"expected":expected.get(field),"actual":actual.get(field)}
                 for field in identity_fields if expected.get(field) != actual.get(field)}
        if expected.get("unresolved") != actual.get("unresolved"):
            changed["unresolved"]={
                "expected":expected.get("unresolved"),"actual":actual.get("unresolved"),
            }
        if (
            changed and name == "grounding_roots.cwd"
            and set(changed) <= {"release_id","content_digest","binding_digest"}
            and _grounding_cwd_lineage_ok(
                actual.get("path"),expected.get("release_id"),actual.get("release_id"),
            )
        ):
            changed={}
        if (
            changed and name == "jobs_path"
            and _jobs_path_alias_relieves_mismatch(route,expected,actual)
        ):
            changed={}
        if changed:
            mismatches[name]={
                "expected":expected,"actual":actual,"fields":sorted(changed),
            }
    return not mismatches,mismatches

def _sha256_record(value):
    return "sha256:"+hashlib.sha256(canonical(value)).hexdigest()

def _continuation_contract_hash(node):
    return _sha256_record(node)

def _continuation_source_jobs(source_route):
    jobs=(
        ((source_route.get("launch_compatibility_tuple") or {}).get("jobs_path") or {})
        .get("path")
    )
    if not isinstance(jobs,str) or not jobs or not Path(jobs).is_absolute():
        raise ValueError("continuation-source-jobs-binding-unresolved")
    sealed=Path(jobs).resolve(strict=False)
    resolution=resolve_dangling_registry(sealed)
    if resolution.status=="exact":
        return sealed
    if resolution.status=="aliased":
        # Decision 1/4: alias is evaluated before the compat shim below --
        # digest-verified equivalence must win over an unvalidated path swap.
        return resolution.jobs_path
    # Compat shim (SD-112 §13.33.2-(3)/(6)): kept intentionally, not removed
    # this cycle. A managed release upgrade prunes old release trees, and the
    # sealed `.dispatch` root lives inside one (observed 2026-08-27:
    # rt-eab5eba8's v2.80.1 root vanished while its migrated completion
    # markers live under the current release). The markers/attempt links that
    # continuation reads are migrated to the canonical live root, and every
    # marker is still verified against the route binding and exact attempt
    # link -- so when the sealed root itself is gone and no alias resolved it,
    # resolve the live canonical root instead of refusing with a dangling
    # path. Both branches above still apply first.
    return resolve_dispatch_state_root(resolve_agent_home(),None)/"jobs.log"

def _continuation_reused_evidence(route, node):
    """Read one reusable node from its canonical marker and exact attempt link."""
    node_id=str(node["id"])
    jobs=_continuation_source_jobs(route)
    directory=completion_dir(route["route_id"],jobs=jobs)
    marker_path=directory/f"{node_id}.json"
    gate=_marker_identity_row(
        route,node,node_id,node.get("completion_gate"),jobs=jobs
    )
    if not gate.get("passed"):
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:{gate.get('reason')}"
        )
    try:
        marker_bytes=marker_path.read_bytes()
        marker=json.loads(marker_bytes)
    except (OSError,ValueError) as exc:
        raise ValueError(f"continuation-source-node-unverified:{node_id}:marker") from exc
    if not completion_marker_is_current(route,node,marker_path,marker):
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:attempt-link"
        )
    attempt_id=marker.get("attempt_id")
    if not isinstance(attempt_id,str) or not attempt_id:
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:terminal-attempt"
        )
    link_path=_attempt_completion_path(route,node_id,attempt_id,jobs=jobs)
    try:
        link_bytes=link_path.read_bytes()
        link=json.loads(link_bytes)
    except (OSError,ValueError) as exc:
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:attempt-sidecar"
        ) from exc
    verdict=str(link.get("verdict") or marker.get("verdict") or "PASS").upper()
    if verdict != "PASS":
        raise ValueError(
            f"continuation-source-verdict-not-pass:{node_id}:{verdict}"
        )
    history_path=directory/f"{node_id}.{marker.get('sequence')}.json"
    try:
        history_digest="sha256:"+hashlib.sha256(history_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:marker-history"
        ) from exc
    quiescence_digest=(
        link.get("quiescence_proof_digest")
        or marker.get("quiescence_proof_digest")
        or _sha256_record({
            "attempt_id":attempt_id,
            "attempt_sidecar_digest":"sha256:"+hashlib.sha256(link_bytes).hexdigest(),
            "marker_history_digest":history_digest,
            "terminal_marker_current":True,
        })
    )
    if not isinstance(quiescence_digest,str) or not quiescence_digest:
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:quiescence-proof"
        )
    evidence=marker.get("evidence") or {}
    public={
        "node_id":node_id,
        "completion_gate":node.get("completion_gate"),
        "marker_path":str(marker_path.resolve(strict=False)),
        "marker_digest":"sha256:"+hashlib.sha256(marker_bytes).hexdigest(),
        "terminal_attempt_id":attempt_id,
        "verdict":verdict,
        "quiescence_proof_digest":quiescence_digest,
        "output_evidence_digest":str(evidence.get("sha256")),
        "contract_hash":_continuation_contract_hash(node),
        "new_attempt_count":0,
    }
    last_turn_id=(
        link.get("last_turn_id") or link.get("lastTurnId")
        or marker.get("last_turn_id") or marker.get("lastTurnId")
    )
    return public,(str(last_turn_id) if last_turn_id else None)

def _source_evidence_snapshot(route, node_ids):
    by_id={str(node.get("id")):node for node in route.get("nodes",[])}
    rows=[]; turns={}
    for node_id in node_ids:
        node=by_id.get(str(node_id))
        if node is None:
            raise ValueError(f"continuation-source-node-unknown:{node_id}")
        row,last_turn_id=_continuation_reused_evidence(route,node)
        rows.append(row)
        if last_turn_id:
            turns[str(node_id)]=last_turn_id
    return rows,_sha256_record(rows),turns

def source_evidence_digest(route, reused_node_ids=None):
    """Canonical digest of an exact reusable marker/attempt prefix."""
    if reused_node_ids is None:
        reusable=[]
        for node in route.get("nodes",[]):
            try:
                _continuation_reused_evidence(route,node)
            except ValueError:
                break
            reusable.append(str(node["id"]))
        reused_node_ids=reusable
    _rows,digest,_turns=_source_evidence_snapshot(route,list(reused_node_ids))
    return digest

def _continuation_lineage(
    source_route,reused_nodes,reused_turns,*,operation="resume",
    thread_id=None,new_thread_id=None,forked_from_id=None,last_turn_id=None,
    ephemeral=False,
):
    if ephemeral:
        raise ValueError("continuation-ephemeral-forbidden")
    if operation not in {"resume","fork"}:
        raise ValueError("continuation-lineage-operation-invalid")
    source_lineage=source_route.get("runtime_lineage") or {}
    source_thread=thread_id or source_lineage.get("thread_id")
    reused_end_node=reused_nodes[-1]["node_id"] if reused_nodes else None
    node_turns=source_lineage.get("node_turn_ids") or {}
    expected_turn=(
        reused_turns.get(reused_end_node)
        or (node_turns.get(reused_end_node) if reused_end_node else None)
    )
    selected_turn=last_turn_id or expected_turn
    if last_turn_id and expected_turn and last_turn_id != expected_turn:
        raise ValueError("continuation-last-turn-mismatch")
    if operation=="resume":
        if new_thread_id or forked_from_id:
            raise ValueError("continuation-resume-lineage-switch-forbidden")
        return {
            "operation":"resume","thread_id":source_thread,
            "lastTurnId":selected_turn,"ephemeral":False,
        }
    if not source_thread or not new_thread_id or new_thread_id==source_thread:
        raise ValueError("continuation-fork-lineage-incomplete")
    if forked_from_id != source_thread:
        raise ValueError("continuation-fork-source-mismatch")
    if not expected_turn or selected_turn != expected_turn:
        raise ValueError("continuation-last-turn-mismatch")
    return {
        "operation":"fork","thread_id":new_thread_id,
        "forkedFromId":source_thread,"lastTurnId":selected_turn,
        "ephemeral":False,
    }

def partial_group_continuation(
    source_route,*,source_group_id,source_batch_manifest,
    failed_source_attempt_id,gap_leg_id,
):
    """Seal the immutable successful-peer proof for one exact failed group leg."""
    manifest,manifest_digest,leg_digests=verify_batch_manifest(source_batch_manifest)
    if (
        manifest.get("route_id") != source_route.get("route_id")
        or manifest.get("parallel_group") != source_group_id
    ):
        raise ValueError("partial-continuation-batch-source-mismatch")
    group=next(
        (row for row in source_route.get("parallel_groups",[])
         if row.get("id")==source_group_id),None,
    )
    if group is None:
        raise ValueError("partial-continuation-group-unknown")
    members=manifest.get("members") or []
    if manifest.get("declared_size") != group.get("width") or len(members)!=group.get("width"):
        raise ValueError("partial-continuation-group-cardinality-mismatch")
    gap=next((row for row in members if row.get("route_node")==gap_leg_id),None)
    if gap is None or gap.get("attempt_id") != failed_source_attempt_id:
        raise ValueError("partial-continuation-gap-attempt-mismatch")
    nodes={str(node.get("id")):node for node in source_route.get("nodes",[])}
    realized=[]
    for member in members:
        node_id=str(member.get("route_node"))
        if node_id==gap_leg_id:
            continue
        node=nodes.get(node_id)
        if node is None:
            raise ValueError(f"partial-continuation-peer-unknown:{node_id}")
        evidence,_last_turn=_continuation_reused_evidence(source_route,node)
        if evidence["terminal_attempt_id"] != member.get("attempt_id"):
            raise ValueError(f"partial-continuation-peer-attempt-mismatch:{node_id}")
        realized.append({
            key:evidence[key] for key in (
                "node_id","terminal_attempt_id","marker_path","marker_digest",
                "verdict","quiescence_proof_digest","output_evidence_digest",
                "contract_hash",
            )
        })
    if len(realized) != int(group["width"])-1:
        raise ValueError("partial-continuation-peer-set-incomplete")
    peer_digest=_sha256_record(realized)
    replacement_identity=_sha256_record({
        "source_route_id":source_route["route_id"],
        "source_route_hash":source_route["route_hash"],
        "source_group_id":source_group_id,
        "failed_source_attempt_id":failed_source_attempt_id,
        "gap_leg_id":gap_leg_id,
        "reused_peer_set_proof_digest":peer_digest,
    })
    return {
        "contract_version":CONTINUATION_CONTRACT_VERSION,
        "source_group_id":source_group_id,
        "source_batch_manifest_digest":manifest_digest,
        "leg_manifest_digests":{
            str(member["route_node"]):leg_digests[str(member["attempt_id"])]
            for member in members
        },
        "original_group_cardinality":int(group["width"]),
        "join_policy":group.get("join_policy"),
        "failed_source_attempt_id":failed_source_attempt_id,
        "gap_leg_id":gap_leg_id,
        "realized_peer_set":realized,
        "reused_peer_set_proof_digest":peer_digest,
        "replacement_leg_identity":replacement_identity,
        "replacement_attempt_id":"att-"+replacement_identity.split(":",1)[1][:48],
    }

def _continuation_id(payload):
    return "cont-"+hashlib.sha256(canonical(payload)).hexdigest()[:32]

def build_continuation_route(
    source_route,*,resume_from_node,requested_boundary,reason,
    artifact_root,lineage_operation="resume",thread_id=None,new_thread_id=None,
    forked_from_id=None,last_turn_id=None,ephemeral=False,
    partial_group=None,
):
    """Generate one official route suffix without invoking the generic compiler."""
    source_nodes=source_route.get("nodes") or []
    node_ids=[str(node.get("id")) for node in source_nodes]
    requested_blocker=(
        None if requested_boundary in node_ids else "requested-boundary-unknown"
    )
    first_blocker=None
    if resume_from_node not in node_ids:
        first_blocker="first-runnable-node-unknown"
        resume_index=None
    else:
        resume_index=node_ids.index(resume_from_node)
    reused_ids=node_ids[:resume_index] if resume_index is not None else []
    reused=[]; reused_turns={}; evidence_digest=_sha256_record([])
    if first_blocker is None:
        try:
            reused,evidence_digest,reused_turns=_source_evidence_snapshot(
                source_route,reused_ids
            )
        except ValueError as exc:
            first_blocker=str(exc)
            reused=[]; reused_turns={}
            for node_id in reused_ids:
                try:
                    row,turn=_continuation_reused_evidence(
                        source_route,source_nodes[node_ids.index(node_id)]
                    )
                except ValueError:
                    break
                reused.append(row)
                if turn: reused_turns[node_id]=turn
            evidence_digest=_sha256_record(reused)
    lineage=_continuation_lineage(
        source_route,reused,reused_turns,operation=lineage_operation,
        thread_id=thread_id,new_thread_id=new_thread_id,
        forked_from_id=forked_from_id,last_turn_id=last_turn_id,
        ephemeral=ephemeral,
    )
    identity={
        "source_route_id":source_route.get("route_id"),
        "source_route_hash":source_route.get("route_hash"),
        "resume_from_node":resume_from_node,
        "requested_boundary":requested_boundary,
        "reason":reason,
        "source_evidence_digest":evidence_digest,
        "lineage":lineage,
    }
    continuation_id=_continuation_id(identity)
    edge={
        "edge_version":1,
        "edge_id":_sha256_record({
            "from_route_id":source_route.get("route_id"),
            "from_route_hash":source_route.get("route_hash"),
            "to_continuation_id":continuation_id,
            "reason":reason,
        }),
        "operation":"continuation",
        "from_route_id":source_route.get("route_id"),
        "from_route_hash":source_route.get("route_hash"),
        "to_continuation_id":continuation_id,
        "reason":reason,
        "source_verdict_preserved":True,
    }
    result={
        "continuation_contract_version":CONTINUATION_CONTRACT_VERSION,
        **identity,
        "continuation_id":continuation_id,
        "first_runnable_node":(
            resume_from_node if resume_index is not None else None
        ),
        "requested_boundary_blocker":requested_blocker,
        "first_runnable_blocker":first_blocker,
        "lineage_operation":lineage_operation,
        "runtime_lineage":lineage,
        "source_route_supersession":edge,
        "supersession_edges":[
            *json.loads(json.dumps(source_route.get("supersession_edges") or [])),
            edge,
        ],
        "reused_nodes":reused,
        "new_nodes":[],
        "partial_group_continuation":None,
    }
    if partial_group is not None and first_blocker is None:
        result["partial_group_continuation"]=partial_group_continuation(
            source_route,**partial_group
        )
    if requested_blocker or first_blocker:
        return result
    reused_by_id={row["node_id"]:row for row in reused}
    route_nodes=[]
    descriptors=[]
    for offset,source_node in enumerate(source_nodes[resume_index:]):
        node=json.loads(json.dumps(source_node))
        original_dependencies=list(node.get("depends_on") or [])
        satisfied=[dep for dep in original_dependencies if dep in reused_by_id]
        if satisfied:
            node["source_depends_on"]=original_dependencies
            node["depends_on"]=[dep for dep in original_dependencies if dep not in reused_by_id]
            node["reused_dependencies"]=[
                {
                    "node_id":dep,
                    "contract_hash":reused_by_id[dep]["contract_hash"],
                    "marker_digest":reused_by_id[dep]["marker_digest"],
                    "terminal_attempt_id":reused_by_id[dep]["terminal_attempt_id"],
                }
                for dep in satisfied
            ]
        source_contract_hash=_continuation_contract_hash(source_node)
        node["source_contract_hash"]=source_contract_hash
        route_nodes.append(node)
        descriptors.append({
            "node_id":str(source_node["id"]),
            "source_contract_hash":source_contract_hash,
            "realized_contract_hash":_continuation_contract_hash(node),
            "attempt_authority":"granted" if offset==0 else "pending-dependency",
            "new_attempt_count":0,
        })
    result["new_nodes"]=descriptors
    launch={
        "contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
        **launch_compatibility_tuple(
            artifact_root=artifact_root,cwd=source_route.get("cwd"),
        ),
    }
    inherited_keys=(
        "schema_version","capability","capability_mode","requested_intensity",
        "effective_intensity","owner_model_profile","execution_topology",
        "owner_dispatch_depth","max_dispatch_depth","tracking",
        "tracked_gate_evidence","spec_touch","cwd","source_commit",
        "registry_digest","dispatch_defaults_digest","dispatch_allocation",
        "owner_harness_policy","selection","human_gates","human_gate_bindings",
        "resume_retry_boundaries","dispatch_evidence","dispatch_contract_version",
        "dispatch_evidence_scope_version","registered_headless_candidates",
        "registered_headless_policy","unit_catalog_digest","validation_basis",
    )
    route={key:json.loads(json.dumps(source_route[key]))
           for key in inherited_keys if key in source_route}
    route.update(result)
    route["artifact_root"]=str(Path(artifact_root).resolve(strict=False))
    route["nodes"]=route_nodes
    route["parallel_groups"]=_realized_parallel_groups(route_nodes)
    route["conditional_extensions"]=[]
    route["completion_gates"]=sorted({
        str(node.get("terminal_gate") or node.get("completion_gate"))
        for node in route_nodes if node.get("terminal") is True
    })
    route["workflow_contract"]={
        "schema_version":WORKFLOW_CONTRACT_VERSION,
        "states":list((source_route.get("workflow_contract") or {}).get("states") or []),
        "failure_states":list((source_route.get("workflow_contract") or {}).get("failure_states") or []),
        "terminal_nodes":[
            str(node["id"]) for node in route_nodes if node.get("terminal") is True
        ],
        "continuations":{
            str(node["id"]):json.loads(json.dumps(node.get("continuation")))
            for node in route_nodes if node.get("terminal") is not True
        },
        "human_gate_bindings":json.loads(json.dumps(route.get("human_gate_bindings") or [])),
    }
    route["launch_compatibility_tuple"]=launch
    route["advance_generation"]=int(source_route.get("advance_generation") or 0)+1
    digest=route_hash(route)
    route["route_hash"]=digest
    route["route_id"]="rt-"+digest.split(":",1)[1][:16]
    return route

def _verify_continuation_route(route):
    if route.get("continuation_contract_version") != CONTINUATION_CONTRACT_VERSION:
        raise ValueError("unsupported-continuation-contract-version")
    required=(
        "source_route_id","source_route_hash","resume_from_node",
        "requested_boundary","reason","source_evidence_digest",
        "continuation_id","first_runnable_node","requested_boundary_blocker",
        "first_runnable_blocker","lineage_operation","source_route_supersession",
        "reused_nodes","new_nodes",
    )
    if any(key not in route for key in required):
        raise ValueError("continuation-contract-incomplete")
    if route.get("requested_boundary_blocker") or route.get("first_runnable_blocker"):
        raise ValueError("blocked-continuation-route-published")
    reused=route.get("reused_nodes")
    new=route.get("new_nodes")
    if not isinstance(reused,list) or not isinstance(new,list):
        raise ValueError("continuation-node-sets-invalid")
    if route.get("source_evidence_digest") != _sha256_record(reused):
        raise ValueError("continuation-source-evidence-digest-invalid")
    reused_ids=[row.get("node_id") for row in reused if isinstance(row,dict)]
    new_ids=[row.get("node_id") for row in new if isinstance(row,dict)]
    route_ids=[node.get("id") for node in route.get("nodes",[]) if isinstance(node,dict)]
    if len(reused_ids)!=len(set(reused_ids)) or set(reused_ids)&set(new_ids):
        raise ValueError("continuation-node-sets-overlap")
    if new_ids != route_ids or not new_ids or new_ids[0]!=route.get("first_runnable_node"):
        raise ValueError("continuation-new-node-census-invalid")
    if any(row.get("new_attempt_count")!=0 for row in reused):
        raise ValueError("continuation-reused-node-attempt-authority")
    for descriptor,node in zip(new,route.get("nodes",[])):
        if descriptor.get("source_contract_hash") != node.get("source_contract_hash"):
            raise ValueError("continuation-new-node-contract-invalid")
        if descriptor.get("realized_contract_hash") != _continuation_contract_hash(node):
            raise ValueError("continuation-realized-node-contract-invalid")
    expected_continuation_id=_continuation_id({
        "source_route_id":route.get("source_route_id"),
        "source_route_hash":route.get("source_route_hash"),
        "resume_from_node":route.get("resume_from_node"),
        "requested_boundary":route.get("requested_boundary"),
        "reason":route.get("reason"),
        "source_evidence_digest":route.get("source_evidence_digest"),
        "lineage":route.get("runtime_lineage"),
    })
    if route.get("continuation_id") != expected_continuation_id:
        raise ValueError("continuation-id-invalid")
    edge=route.get("source_route_supersession") or {}
    if (
        edge.get("from_route_id") != route.get("source_route_id")
        or edge.get("from_route_hash") != route.get("source_route_hash")
        or edge.get("to_continuation_id") != route.get("continuation_id")
        or edge.get("source_verdict_preserved") is not True
    ):
        raise ValueError("continuation-supersession-edge-invalid")
    launch=route.get("launch_compatibility_tuple") or {}
    if launch.get("contract_version") != LAUNCH_COMPATIBILITY_TUPLE_VERSION:
        raise ValueError("launch-compatibility-tuple-required")
    _validate_output_scopes(route.get("nodes",[]))
    return route

def publish_continuation_route(route,source_route,output_path):
    """Recheck source bytes immediately before the one immutable publication."""
    if route.get("requested_boundary_blocker") or route.get("first_runnable_blocker"):
        raise ValueError("continuation-boundary-blocked")
    node_ids=[row["node_id"] for row in route.get("reused_nodes",[])]
    try:
        current,current_digest,_turns=_source_evidence_snapshot(source_route,node_ids)
    except ValueError as exc:
        raise ValueError("continuation-source-evidence-drift") from exc
    if (
        current_digest != route.get("source_evidence_digest")
        or canonical(current) != canonical(route.get("reused_nodes"))
    ):
        raise ValueError("continuation-source-evidence-drift")
    path=Path(output_path)
    if classify_route_location(path,route["artifact_root"]) != "canonical":
        raise ValueError("route-output-outside-canonical")
    if not route_path_is_exact(path,route["artifact_root"],route["route_id"]):
        raise ValueError("route-output-alias-basename")
    write_once(path,route)
    return path

def _git_commit(cwd):
    p=subprocess.run(["git","-C",str(cwd),"rev-parse","HEAD"],text=True,capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else "unversioned"

def _validate_tracking_evidence(tracking, evidence):
    if tracking not in TRACKING: raise ValueError("invalid tracking value")
    if not isinstance(evidence, dict) or set(evidence) != GATE_FIELDS:
        raise ValueError("tracked gate evidence requires spec_read, drift_verdict, workflow_mode, artifact_guard")
    if evidence["workflow_mode"] != tracking:
        raise ValueError("tracked gate workflow_mode mismatch")
    if not isinstance(evidence["drift_verdict"], str) or not evidence["drift_verdict"]:
        raise ValueError("tracked gate drift_verdict required")
    for name in ("spec_read", "artifact_guard"):
        row=evidence[name]
        if not isinstance(row, dict) or not isinstance(row.get("satisfied"), bool) or not row.get("source"):
            raise ValueError(f"tracked gate {name} requires satisfied boolean and source")
        if tracking=="tracked" and not row["satisfied"]:
            raise ValueError(f"tracked gate {name} must be satisfied")
    return evidence

def _scope_touches_spec(scope):
    root=scope[:-3] if scope.endswith("/**") else scope
    return root=="spec" or root.startswith("spec/")

def _evidence_parent_dispatch_depth(nodes, owner_dispatch_depth):
    """Derive whose runtime the checked tuples must describe, from the route itself.

    `dispatch_evidence.tuples` are consumed by exactly the nodes that receive a
    `fallback_hops` chain, and only dispatch-depth-2 nodes do. The parent of a
    dispatch-depth-N node is the depth-(N-1) runtime, which for those nodes is
    the route's own registered-headless capability owner. Deriving the value
    here -- instead of hardcoding it at each call site -- keeps the check honest
    if a recipe ever seals evidence at another depth, and cross-checks the two
    structural facts the route already states about itself.
    """
    if not any(node.get("dispatch_depth") == EVIDENCE_CONSUMER_DISPATCH_DEPTH for node in nodes):
        raise ValueError(
            "dispatch-evidence-without-consumer-node: checked tuples were sealed but no "
            f"dispatch-depth-{EVIDENCE_CONSUMER_DISPATCH_DEPTH} node consumes them"
        )
    parent_dispatch_depth = EVIDENCE_CONSUMER_DISPATCH_DEPTH - 1
    if owner_dispatch_depth != parent_dispatch_depth:
        raise ValueError(
            "dispatch-evidence-parent-depth-mismatch: "
            f"owner_dispatch_depth={owner_dispatch_depth} cannot parent a "
            f"dispatch-depth-{EVIDENCE_CONSUMER_DISPATCH_DEPTH} node"
        )
    return parent_dispatch_depth

def _validate_tuple_parent_identity(row, parent_dispatch_depth):
    """Reject a checked tuple sealed for a parent runtime the route cannot have.

    The tuple's parent fields are compared field-for-field against the launching
    wrapper's `AGENT_DISPATCH_CURRENT_*` export at dispatch time
    (`dispatch-node.validate_parent_identity`), so any value that no wrapper can
    export is dead evidence. Two production incidents proved each unchecked
    field costs a whole owner cycle -- 2026-07-31 on `parent_sandbox`, 2026-08-04
    on `parent_transport` -- so all three fields fail closed at compile instead.
    """
    harness = row["parent_harness"]
    if harness not in WRAPPER_PARENT_SANDBOXES:
        raise ValueError(
            f"dispatch-evidence-parent-harness-unknown: {harness!r} is not a wrapper harness"
        )
    if row["child_harness"] not in WRAPPER_PARENT_SANDBOXES:
        raise ValueError(
            f"dispatch-evidence-child-harness-unknown: {row['child_harness']!r} is not a wrapper harness"
        )
    expected_transport = PARENT_TRANSPORT_BY_DISPATCH_DEPTH[parent_dispatch_depth]
    if row["parent_transport"] != expected_transport:
        raise ValueError(
            "dispatch-evidence-parent-transport-mismatch: a "
            f"dispatch-depth-{parent_dispatch_depth} parent is {expected_transport}, "
            f"tuple sealed {row['parent_transport']!r} "
            "(probe the dispatch-depth-2 node's parent, not the calling session)"
        )
    if row["parent_sandbox"] not in WRAPPER_PARENT_SANDBOXES[harness]:
        raise ValueError(
            "dispatch-evidence-parent-sandbox-unknown: the "
            f"{harness} wrapper exports {sorted(WRAPPER_PARENT_SANDBOXES[harness])}, "
            f"tuple sealed {row['parent_sandbox']!r}"
        )

def _validate_dispatch_evidence(
    evidence,
    contract_version=DISPATCH_CONTRACT_VERSION,
    parent_dispatch_depth=EVIDENCE_CONSUMER_DISPATCH_DEPTH - 1,
    *,
    expected_worktree=None,
    require_scope=False,
):
    contract_version = contract_version or 1
    if contract_version not in {1, 2, DISPATCH_CONTRACT_VERSION}:
        raise ValueError(f"unsupported dispatch contract version: {contract_version}")
    if not isinstance(evidence,dict): raise ValueError("checked dispatch evidence required")
    tuples=evidence.get("tuples")
    if not isinstance(tuples,list) or not tuples: raise ValueError("nested eligibility tuples required")
    normalized=[]
    for row in tuples:
        if not isinstance(row,dict) or not NESTED_FIELDS.issubset(row):
            raise ValueError("nested eligibility tuple fields missing")
        if row["status"] not in NESTED_STATUSES: raise ValueError("invalid nested eligibility status")
        if row["launch_authority"] not in ("conductor","ancestor-broker"):
            raise ValueError("invalid nested launch authority")
        if not row["probe_source"] or not row["probe_time"]:
            raise ValueError("nested eligibility checked source/time required")
        normalized_row={key:row[key] for key in sorted(NESTED_FIELDS)}
        present_scope=NESTED_SCOPE_FIELDS.intersection(row)
        has_scope=NESTED_SCOPE_FIELDS.issubset(row)
        if present_scope and not has_scope:
            raise ValueError("nested eligibility scope fields incomplete")
        if require_scope and not has_scope:
            raise ValueError("nested eligibility exact-worktree scope required")
        if has_scope:
            checked=Path(row["checked_worktree"])
            if not checked.is_absolute():
                raise ValueError("nested eligibility checked_worktree must be absolute")
            checked=checked.resolve()
            if expected_worktree is not None and checked != Path(expected_worktree).resolve():
                raise ValueError(
                    "dispatch-evidence-worktree-mismatch: "
                    f"route cwd {Path(expected_worktree).resolve()} != checked {checked}"
                )
            scope=row["failure_scope"]
            if scope not in NESTED_FAILURE_SCOPES:
                raise ValueError("invalid nested eligibility failure_scope")
            if row["codex_command"] not in CODEX_COMMAND_STATES:
                raise ValueError("invalid nested eligibility codex_command")
            retry=row["retry_on_isolated_worktree"]
            if type(retry) is not int or retry not in (0, 1):
                raise ValueError("retry_on_isolated_worktree must be 0 or 1")
            if row["status"] == "supported" and (scope != "none" or retry != 0):
                raise ValueError("supported nested eligibility cannot carry failure scope")
            if scope == "exact-worktree":
                if row["status"] == "supported" or retry != 1:
                    raise ValueError("exact-worktree failure requires unsupported retry evidence")
                if row["child_harness"] == "codex" and row["codex_command"] != "ok":
                    raise ValueError("Codex exact-worktree failure requires codex_command=ok")
            elif retry != 0:
                raise ValueError("isolated-worktree retry requires exact-worktree failure_scope")
            normalized_row.update({
                "checked_worktree": str(checked),
                "codex_command": row["codex_command"],
                "failure_scope": scope,
                "retry_on_isolated_worktree": retry,
            })
        if contract_version==DISPATCH_CONTRACT_VERSION:
            if row["launch_authority"] != "conductor":
                raise ValueError("v3 dispatch evidence requires conductor launch authority")
            if row["parent_transport"] not in CANONICAL_PARENT_TRANSPORTS:
                raise ValueError("v3 dispatch evidence requires canonical parent transport")
            _validate_tuple_parent_identity(row, parent_dispatch_depth)
            if any(row.get(key) for key in BROKER_FIELDS):
                raise ValueError("v3 dispatch evidence must not carry broker fields")
        elif contract_version==2:
            if row.get("launch_authority")=="ancestor-broker" and row.get("status")=="supported" and not row.get("broker_root"):
                raise ValueError("v2 dispatch evidence requires broker_root")
            if row.get("broker_root"):
                normalized_row["broker_root"]=row["broker_root"]
            # broker_instance is mutable rollover identity -- v2 strips it
            # even if the caller's probe output still carries one.
        else:
            for key in BROKER_FIELDS:
                if key in row:
                    normalized_row[key]=row[key]
        normalized.append(normalized_row)
    native=evidence.get("native_subagent",[])
    if not isinstance(native,list): raise ValueError("native_subagent evidence must be a list")
    normalized_native=[]
    for row in native:
        if not isinstance(row,dict) or not NATIVE_EVIDENCE_FIELDS.issubset(row):
            raise ValueError("invalid native subagent evidence")
        harness=row.get("harness")
        if (
            row.get("status") not in NESTED_STATUSES
            or harness not in NATIVE_SURFACES
            or row.get("execution_surface") != NATIVE_SURFACES[harness]
            or row.get("transport") != "headless"
            or row.get("registered_worker") is not False
            or not row.get("check_source")
        ):
            raise ValueError("invalid native subagent evidence")
        normalized_native.append({key:row[key] for key in sorted(NATIVE_EVIDENCE_FIELDS)})
    return {"tuples":normalized,"native_subagent":normalized_native}

def _fallback_chain(
    evidence,
    contract_version=DISPATCH_CONTRACT_VERSION,
    parent_dispatch_depth=EVIDENCE_CONSUMER_DISPATCH_DEPTH - 1,
    *,
    expected_worktree=None,
    require_scope=False,
):
    contract_version = contract_version or 1
    evidence=_validate_dispatch_evidence(
        evidence, contract_version, parent_dispatch_depth,
        expected_worktree=expected_worktree, require_scope=require_scope,
    )
    tuples=evidence["tuples"]
    if any(
        row.get("status") == "unsupported"
        and row.get("failure_scope") == "exact-worktree"
        and row.get("retry_on_isolated_worktree") == 1
        for row in tuples
    ):
        raise ValueError("dispatch-evidence-exact-worktree-reprobe-required")
    same=[row for row in tuples if row["child_harness"]==row["parent_harness"]]
    cross=[row for row in tuples if row["child_harness"]!=row["parent_harness"]]
    if contract_version==DISPATCH_CONTRACT_VERSION:
        same=[row for row in same if row["launch_authority"]=="conductor"]
        cross=[row for row in cross if row["launch_authority"]=="conductor"]
        has_direct=any(row["status"]=="supported" for row in same+cross)
        if not has_direct:
            raise ValueError("no supported direct headless tuple")
    elif contract_version==2:
        has_broker=any(
            row["status"]=="supported" and row["launch_authority"]=="ancestor-broker" and row.get("broker_root")
            for row in same+cross
        )
        if not has_broker:
            raise ValueError("no supported registered-headless launch tuple")
    else:
        has_broker=any(
            row["status"]=="supported" and row["launch_authority"]=="ancestor-broker"
            and row.get("broker_root") and row.get("broker_instance")
            for row in same+cross
        )
        if not has_broker:
            raise ValueError("no supported registered-headless launch tuple")
    return [
        {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":same},
        {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":cross},
        {"ordinal":3,"fallback_hop":"native-subagent","candidates":evidence["native_subagent"],"fleet_visibility":"degraded"},
        {"ordinal":4,"fallback_hop":"inline","status":"eligible-after-prior-hop-exhaustion","reason_enum":"runtime-unavailable","fleet_visibility":"none"},
    ]

def _verify_fallback_chain(node, contract_version=None):
    contract_version = contract_version or 1
    chain=node.get("fallback_hops")
    if not isinstance(chain,list) or [row.get("fallback_hop") for row in chain] != FALLBACK_ORDER:
        raise ValueError(f"dispatch-depth-2 node {node.get('id')} missing ordered dispatch fallback")
    candidates=[candidate for row in chain[:2] for candidate in row.get("candidates",[])]
    if contract_version==DISPATCH_CONTRACT_VERSION:
        supported=[c for c in candidates if c.get("status")=="supported" and c.get("launch_authority")=="conductor"]
        if not supported:
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported direct headless tuple")
        if any(c.get("broker_root") or c.get("broker_instance") for c in candidates):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} v3 candidate carries retired broker fields")
    elif contract_version==1:
        supported=[c for c in candidates if c.get("status")=="supported" and c.get("launch_authority")=="ancestor-broker"]
        if not any(c.get("broker_root") and c.get("broker_instance") for c in supported):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported dispatch-depth-0 broker tuple")
    elif contract_version==2:
        supported=[c for c in candidates if c.get("status")=="supported" and c.get("launch_authority")=="ancestor-broker"]
        if not any(c.get("broker_root") for c in supported):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported dispatch-depth-0 broker tuple")
        if any(c.get("broker_instance") for c in supported):
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} v2 candidate must not carry broker_instance")
    else:
        if not supported:
            raise ValueError(f"dispatch-depth-2 node {node.get('id')} lacks supported dispatch-depth-0 broker tuple")
    return chain

def _parallel_path(path, suffix):
    """Use the topology validator's single parallel artifact-path rule."""
    return TOPO._parallel_path(path, suffix)


def _validate_output_scopes(nodes):
    """Reject realized nodes whose path outputs escape their write authority."""
    for node in nodes:
        uncovered = TOPO._uncovered_path_outputs(
            node.get("outputs", []), node.get("write_scope", [])
        )
        if uncovered:
            raise ValueError(
                f"node {node.get('id')} outputs outside write_scope {sorted(uncovered)}"
            )

# G6 (AC 21 declaration-level gate): a parallel_group's non-anchor legs always
# have `terminal`/`terminal_gate` stripped during expansion (D3), which makes
# the downstream "2+ realized nodes share one terminal_gate" check in
# `_workflow_contract` structurally unreachable for any group declared on a
# terminal node -- it can never fire, so it silently permits peer expansion of
# ANY terminal node. The real gate has to run at declaration time, before that
# stripping happens. `autopilot-research claim-verify` already ships this
# pattern (D3′, `plan.md` D3/D3-a) and is preserved as a recorded, non-silent
# grandfather rather than a silent exception; no other recipe may add this
# pattern going forward.
_TERMINAL_PARALLEL_GROUP_GRANDFATHER = {("autopilot-research", "claim-verify")}


def _expand_parallel_groups(nodes, parallel_groups, effective_intensity,
                            capability, *, auxiliary_check_units=None):
    """Expand registry-v6 groups into ordered 2..4-way sibling nodes.

    `capability` is required (N2). It was an optional kwarg defaulting to
    `None`, and the grandfather lookup is keyed on `(capability, group id)` --
    so a caller that simply forgot the argument silently rejected the shipped
    `autopilot-research claim-verify` group instead of failing at the call.
    A required parameter turns that into a TypeError at the call site.

    The first leg keeps the anchor id for stable downstream references. Extra
    legs get suffix-specific ids, outputs, and write scopes. Direct consumers
    depend on every realized leg; non-review consumers also receive every leg's
    output. `replica_group`/`independence_axis` remain one-window read aliases,
    while `parallel_group` and the plural axes are canonical.

    A group declared on a node whose recipe declaration carries `terminal:
    true` is rejected (G6/AC 21) unless the (capability, group id) pair is
    named in `_TERMINAL_PARALLEL_GROUP_GRANDFATHER`.
    """
    if not parallel_groups:
        return nodes
    if effective_intensity not in ORDER:
        raise ValueError("invalid intensity")
    for group in parallel_groups:
        if ORDER[effective_intensity] < ORDER[group["min_intensity"]]:
            continue
        width = group["width_by_intensity"][effective_intensity]
        base = next(n for n in nodes if n["id"] == group["node"])
        if base.get("terminal") is True and (capability, group["id"]) not in _TERMINAL_PARALLEL_GROUP_GRANDFATHER:
            raise ValueError(
                f"parallel group {group['id']!r} is declared on terminal node "
                f"{base['id']!r}; peer expansion of a terminal node is rejected "
                "at declaration (G6/AC 21) unless explicitly grandfathered"
            )
        members = []
        for index, leg_spec in enumerate(group["legs"][:width]):
            leg = base if index == 0 else json.loads(json.dumps(base))
            suffix = leg_spec["suffix"]
            if index:
                leg["id"] = f"{base['id']}-{suffix}"
                leg["outputs"] = [_parallel_path(path, suffix) for path in base["outputs"]]
                leg["write_scope"] = [
                    _parallel_path(path, suffix) for path in base["write_scope"]
                ]
            leg["model_profile"] = leg_spec["model_profile"]
            leg["perspective"] = leg_spec["perspective"]
            leg["leg_class"] = leg_spec["leg_class"]
            if index:
                # D3: only the anchor holds the workflow terminal gate; a
                # realized sibling is a continuation leg, never a terminal.
                leg.pop("terminal", None)
                leg.pop("terminal_gate", None)
                if "continuation" not in leg:
                    leg["continuation"] = {"kind": "inline-next"}
            if leg_spec["leg_class"] == "auxiliary":
                leg["auxiliary_check"] = leg_spec["auxiliary_check"]
                unit = (auxiliary_check_units or {}).get(leg_spec["auxiliary_check"])
                if unit:
                    leg["unit"] = unit
                    leg["role"] = TOPO._unit_frontmatter(unit)["role"]
            leg["parallel_group"] = group["id"]
            leg["parallel_group_kind"] = group["kind"]
            leg["parallel_join_policy"] = group["join_policy"]
            leg["parallel_independence_axes"] = list(group["independence_axes"])
            leg["parallel_leg_index"] = index
            leg["parallel_leg_count"] = width
            leg["parallel_leg_suffix"] = suffix
            leg["parallel_anchor"] = base["id"]
            # One-window compatibility fields for jobs/Fleet and old receipts.
            leg["replica_group"] = group["id"]
            leg["independence_axis"] = "cross-harness"
            members.append(leg)
        for node in nodes:
            if node is not base and base["id"] in node.get("depends_on", []):
                node["depends_on"] = list(node["depends_on"]) + [
                    member["id"] for member in members[1:]
                ]
                if base.get("kind") != "review-worker":
                    node["inputs"] = list(node.get("inputs", [])) + [
                        output for member in members[1:] for output in member["outputs"]
                    ]
        expanded = []
        for node in nodes:
            expanded.append(node)
            if node is base:
                expanded.extend(members[1:])
        nodes = expanded
    return nodes


def _realized_parallel_groups(nodes):
    groups = {}
    for node in nodes:
        group_id = node.get("parallel_group")
        if not group_id:
            continue
        row = groups.setdefault(group_id, {
            "id": group_id,
            "kind": node["parallel_group_kind"],
            "join_policy": node["parallel_join_policy"],
            "independence_axes": list(node["parallel_independence_axes"]),
            "width": node["parallel_leg_count"],
            "members": [],
        })
        row["members"].append(node["id"])
    return list(groups.values())


WORKFLOW_CONTRACT_VERSION = 1


def _workflow_contract(registry, nodes, human_gate_bindings):
    """Seal the tracked-workflow shape the route commits to (`WORKFLOW §0.6`).

    Sealing terminal nodes and continuation kinds beside the graph is what lets a
    supervisor, a status surface, or a later session answer "is this finished?" from
    the route alone, instead of inferring completion from a process that exited.
    """
    ids = [node["id"] for node in nodes]
    terminal, continuations = [], {}
    terminal_gates: dict[str, str] = {}
    for node in nodes:
        node_id = node["id"]
        if node.get("terminal") is True:
            # D3-a: terminal classification is by the `terminal: true` flag, not
            # by "has no downstream dependents". A realized parallel sibling that
            # carries no flag is a continuation leg even when nothing depends on it.
            if not node.get("terminal_gate"):
                raise ValueError(f"terminal node {node_id} lacks a sealed terminal gate")
            if node.get("kind") == "resource-runner":
                raise ValueError(
                    f"terminal node {node_id} is a detached resource run; a workflow cannot "
                    "end on a process exit"
                )
            gate = node["terminal_gate"]
            # AC 21 (D3 retyping): one terminal_gate may be held by at most one
            # realized node — a second holder would duplicate the workflow end.
            if gate in terminal_gates:
                raise ValueError(
                    f"terminal gate {gate} held by both {terminal_gates[gate]} and {node_id}"
                )
            terminal_gates[gate] = node_id
            terminal.append(node_id)
            continue
        continuation = node.get("continuation")
        if not isinstance(continuation, dict) or continuation.get("kind") not in registry[
            "continuation_kinds"
        ]:
            raise ValueError(f"non-terminal node {node_id} declares no valid continuation")
        continuations[node_id] = continuation["kind"]
    if not terminal:
        raise ValueError("route declares no terminal node")
    return {
        "schema_version": WORKFLOW_CONTRACT_VERSION,
        "states": list(registry["workflow_states"]),
        "failure_states": list(registry["workflow_failure_states"]),
        "terminal_nodes": sorted(terminal),
        "continuations": continuations,
        "human_gate_bindings": json.loads(json.dumps(human_gate_bindings or [])),
    }


def _realize_conditional_extensions(recipe, effective_intensity):
    """Seal owner postconditions without turning them into dispatch nodes."""
    rows = json.loads(json.dumps(recipe.get("conditional_extensions", [])))
    terminal = "inline" if effective_intensity == "direct" else (
        "one-shot" if effective_intensity == "quick" else None
    )
    if terminal is not None:
        for row in rows:
            row["after"] = [terminal]
    return rows

def _seal_dispatch_defaults(nodes, capability, owner_profile=None):
    """Return defaults digest/allocation and stamp each dispatch-depth-2 node's
    harness_affinity, BEFORE route_hash is computed. Absent config -> all
    'unspecified' + digest None. Corrupt config -> fail-loud (reused loader
    validator), surfaced as ValueError so main() exits 64. registry_digest is
    a separate field and is never touched here."""
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        for node in nodes:
            if node.get("dispatch_depth") == 2:
                node["harness_affinity"] = "unspecified"
                node["harness_policy"] = None
        return None, None, None
    try:
        cfg = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
    except DEFAULTS.DefaultsConfigError as exc:
        raise ValueError(f"corrupt dispatch-defaults config: {exc}")
    for node in nodes:
        if node.get("dispatch_depth") == 2:
            node["harness_affinity"] = DEFAULTS.query_stage_affinity(
                cfg, capability, node.get("parallel_anchor", node["id"])
            )
            node["harness_policy"] = DEFAULTS.query_profile_policy(
                cfg, node["model_profile"]
            )
    return (
        "sha256:" + hashlib.sha256(canonical(cfg)).hexdigest(),
        DEFAULTS.query_allocation(cfg),
        DEFAULTS.query_profile_policy(cfg, owner_profile) if owner_profile else None,
    )

def _validation_basis():
    """Seal which install root produced `registry_digest`/`unit_catalog_digest`.

    `runtime_root` is normalized to an absolute, resolved path even though
    `resolve_agent_home()` returns its candidate unnormalized -- a relative
    `AGENT_HOME`/`CLAUDE_HOME` must not seal a relative `runtime_root`, since
    the close-time structural gate treats a non-absolute required field as a
    forged record with no legitimate producer.
    """
    runtime_root = Path(resolve_agent_home()).resolve(strict=False)
    registry_root = TOPO.ROOT
    unit_catalog_root = ROOT
    return {
        "basis_version": VALIDATION_BASIS_VERSION,
        "registry_root": str(registry_root),
        "unit_catalog_root": str(unit_catalog_root),
        "runtime_root": str(runtime_root),
        "runtime_root_validated": (runtime_root/"core"/"CORE.md").is_file(),
        "runtime_root_match": agent_home_equivalent(registry_root, runtime_root),
    }

def unit_catalog_digest(units_root=None):
    """Digest of unit frontmatter blocks (machine contracts); unit BODY prose stays un-hashed."""
    units_root=Path(units_root) if units_root else ROOT/"roles"/"units"
    blocks=[]
    for path in sorted(units_root.glob("*/*.md")):
        if path.name.startswith("_"): continue
        match=re.match(r"\A---\n.*?\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
        if match: blocks.append(f"{path.relative_to(units_root)}\n{match.group(0)}")
    return "sha256:"+hashlib.sha256("\n".join(blocks).encode()).hexdigest()

def compile_route(capability, capability_mode, requested_intensity, cwd, artifact_root,
                  predicates=(), signals=(), transport=None,
                  transport_evidence="caller-selected", inline_reason=None,
                  tracking="tracked", tracked_gate_evidence=None, dispatch_evidence=None,
                  registered_headless_evidence=None):
    registry=TOPO.load_registry(); TOPO.validate_registry(registry)
    recipe=TOPO.resolve_recipe(registry, capability, capability_mode)
    return _compile_from_recipe(
        registry, recipe, capability, capability_mode, requested_intensity, cwd, artifact_root,
        predicates=predicates, signals=signals, transport=transport,
        transport_evidence=transport_evidence, inline_reason=inline_reason,
        tracking=tracking, tracked_gate_evidence=tracked_gate_evidence,
        dispatch_evidence=dispatch_evidence,
        registered_headless_evidence=registered_headless_evidence)

def compile_composed_route(composed_recipe, capability_mode, requested_intensity, cwd, artifact_root,
                           **kwargs):
    """Compile a compose-on-demand recipe through the SAME validate/seal path (composed: true)."""
    registry=TOPO.load_registry(); TOPO.validate_registry(registry)
    if not isinstance(composed_recipe, dict): raise ValueError("composed recipe must be an object")
    TOPO._validate_recipe(
        composed_recipe, registry,
        registry["owner_profile_by_intensity"]["standard"],
    )
    # SAME validator means gates too: without this, a composed recipe could carry a
    # forged completion gate that no registry contract backs (2026-07-22 verify finding).
    TOPO._validate_gate_contracts(composed_recipe, registry)
    if capability_mode not in composed_recipe.get("modes", []):
        raise ValueError("composed recipe does not declare the requested capability mode")
    return _compile_from_recipe(
        registry, composed_recipe, composed_recipe["capability"], capability_mode,
        requested_intensity, cwd, artifact_root, composed=True, **kwargs)

def _compile_from_recipe(registry, recipe, capability, capability_mode, requested_intensity,
                         cwd, artifact_root, predicates=(), signals=(), transport=None,
                         transport_evidence="caller-selected", inline_reason=None,
                         tracking="tracked", tracked_gate_evidence=None, dispatch_evidence=None,
                         registered_headless_evidence=None, composed=False):
    cwd=Path(cwd).resolve(strict=True); artifact=Path(artifact_root).resolve()
    if not cwd.is_absolute() or not artifact.is_absolute(): raise ValueError("cwd and artifact root must be absolute")
    known_pred=set(recipe["direct_predicates"]); predicates=sorted(set(predicates))
    unknown=set(predicates)-known_pred
    if unknown: raise ValueError("unknown predicates: "+",".join(sorted(unknown)))
    signals=sorted(set(signals))
    if set(signals) & TRACKING: raise ValueError("tracking cannot be an escalation signal")
    unknown=set(signals)-set(recipe["promotion_signals"])
    if unknown: raise ValueError("unknown promotion signals: "+",".join(sorted(unknown)))
    requested="standard" if requested_intensity=="auto" else requested_intensity
    if requested not in ORDER: raise ValueError("invalid intensity")
    if transport is not None and transport not in WRAPPER_TRANSPORTS:
        raise ValueError(f"invalid transport: {transport!r}")
    inferred="standard" if signals else ("direct" if set(predicates)==known_pred else "quick")
    effective=max((requested,inferred),key=ORDER.get)
    if composed and effective in ("direct","quick"):
        raise ValueError("composed routes require a standard+ effective intensity")
    registered_headless_candidates=None
    if effective=="direct":
        transport="interactive"
        if inline_reason is None: inline_reason="atomic-direct"
        owner_model_profile=None
        nodes=[{"id":"inline","kind":"capability-owner","dispatch_depth":0,"role":"orchestrator",
                "write_scope":recipe["quick"]["write_scope"],"resource_class":"normal",
                "execution_surface":"inline","registered_worker":False,
                "completion_gate":"inline-complete",
                "terminal":True,"terminal_gate":"inline-complete"}]
        gates=["inline-complete"]
        selection_basis=[{"axis":"direct-predicate","signal":p,"source":"caller"} for p in predicates]
    elif effective=="quick":
        if transport not in (None, "headless"):
            raise ValueError(f"invalid quick transport: {transport!r}")
        registered_headless_candidates=_validate_registered_headless_evidence(
            registered_headless_evidence
        )
        transport="headless"
        owner_model_profile=registry["owner_profile_by_intensity"]["quick"]
        nodes=[{"id":"one-shot","kind":recipe["quick"]["worker_kind"],"dispatch_depth":1,"role":"orchestrator",
                "unit":"_kernel/owner",
                "model_profile":owner_model_profile,
                "write_scope":recipe["quick"]["write_scope"],"resource_class":"normal",
                "execution_surface":"registered-headless","registered_worker":True,
                "completion_gate":"quick-complete",
                "terminal":True,"terminal_gate":"quick-complete"}]
        gates=["quick-complete"]
        selection_basis=[{"axis":"direct-predicate-gap","signal":p,"source":"compiler"} for p in sorted(known_pred-set(predicates))]
    else:
        if transport not in (None, "headless"):
            raise ValueError(f"invalid standard+ transport: {transport!r}")
        transport="headless"
        owner_model_profile=registry["owner_profile_by_intensity"][effective]
        nodes=json.loads(json.dumps(recipe["standard_plus"]["nodes"])); gates=recipe["completion_gates"]
        nodes=_expand_parallel_groups(
            nodes, recipe["standard_plus"].get("parallel_groups"), effective,
            capability,
            auxiliary_check_units=registry.get("auxiliary_check_units"),
        )
        for node in nodes:
            node.pop("fallback_hops", None)
        selection_basis=[{"axis":"promotion","signal":s,"source":"caller"} for s in signals]
    _validate_output_scopes(nodes)
    if effective != "direct" and inline_reason is not None:
        raise ValueError("inline_reason only applies to direct")
    if effective=="direct" and inline_reason not in registry["inline_reasons"]:
        raise ValueError("structured inline_reason required")
    evidence=_validate_tracking_evidence(tracking, tracked_gate_evidence)
    checked_dispatch=None
    if effective not in ("direct","quick"):
        parent_dispatch_depth=_evidence_parent_dispatch_depth(
            nodes, recipe["standard_plus"]["owner_dispatch_depth"])
        checked_dispatch=_validate_dispatch_evidence(
            dispatch_evidence, DISPATCH_CONTRACT_VERSION, parent_dispatch_depth,
            expected_worktree=cwd, require_scope=True)
        chain=_fallback_chain(
            checked_dispatch, DISPATCH_CONTRACT_VERSION, parent_dispatch_depth,
            expected_worktree=cwd, require_scope=True)
        for node in nodes:
            if node.get("dispatch_depth")==2:
                node["fallback_hops"]=json.loads(json.dumps(chain))
    dispatch_defaults_digest,dispatch_allocation,owner_harness_policy=_seal_dispatch_defaults(
        nodes, capability, owner_model_profile
    )
    spec_touch=any(_scope_touches_spec(scope) for node in nodes for scope in node["write_scope"])
    payload={
      "schema_version":ROUTE_SCHEMA_VERSION,"capability":capability,"capability_mode":capability_mode,
      "requested_intensity":requested_intensity,"effective_intensity":effective,
      "owner_model_profile":owner_model_profile,
      "execution_topology":("inline" if effective=="direct" else recipe["quick"]["topology"] if effective=="quick" else recipe["topology_class"]),
      "owner_dispatch_depth":0 if effective=="direct" else (recipe["quick"]["owner_dispatch_depth"] if effective=="quick" else recipe["standard_plus"]["owner_dispatch_depth"]),
      "max_dispatch_depth":recipe["quick"]["max_dispatch_depth"] if effective=="quick" else (0 if effective=="direct" else recipe["standard_plus"]["max_dispatch_depth"]),
      "tracking":tracking,"tracked_gate_evidence":evidence,"spec_touch":spec_touch,
      "cwd":str(cwd),"artifact_root":str(artifact),"source_commit":_git_commit(cwd),
      "registry_digest":TOPO.registry_digest(registry),
      "dispatch_defaults_digest":dispatch_defaults_digest,
      "dispatch_allocation":dispatch_allocation,
      "owner_harness_policy":owner_harness_policy,
      "selection":{"direct_predicates":predicates,"promotion_signals":[{"signal":s,"source":"caller"} for s in signals],
                   "selection_basis":selection_basis,
                   "escalation_basis":[{"signal":s,"source":"caller"} for s in signals],
                   "transport":transport,"transport_evidence":transport_evidence,"inline_reason":inline_reason},
      "nodes":nodes,"parallel_groups":_realized_parallel_groups(nodes),
      "conditional_extensions":_realize_conditional_extensions(recipe, effective),
      "completion_gates":gates,"human_gates":recipe["human_gates"],
      "human_gate_bindings":json.loads(json.dumps(
          recipe["human_gate_bindings"] if effective not in ("direct","quick") else [])),
      "workflow_contract":_workflow_contract(
          registry, nodes,
          recipe["human_gate_bindings"] if effective not in ("direct","quick") else []),
      "resume_retry_boundaries":recipe["resume_retry_boundaries"],
      "dispatch_evidence":checked_dispatch,
      "dispatch_contract_version":DISPATCH_CONTRACT_VERSION,
      "registered_headless_candidates":registered_headless_candidates,
      "registered_headless_policy":"serial-attempt" if effective=="quick" else None,
      "unit_catalog_digest":unit_catalog_digest(),
      "validation_basis":_validation_basis(),
      "launch_compatibility_tuple":{
          "contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
          **launch_compatibility_tuple(artifact_root=artifact,cwd=cwd),
      },
      "advance_generation":0}
    if checked_dispatch is not None:
        payload["dispatch_evidence_scope_version"]=DISPATCH_EVIDENCE_SCOPE_VERSION
    if composed:
        payload["composed"]=True
        payload["composed_recipe"]=json.loads(json.dumps(recipe))
    digest=route_hash(payload); payload["route_hash"]=digest; payload["route_id"]="rt-"+digest.split(":",1)[1][:16]
    return payload

class _ValidationBasisDegrade:
    """Sentinel: an over-ceiling `basis_version` degrades rather than raising."""

_DEGRADE_VALIDATION_BASIS = _ValidationBasisDegrade()

def _check_validation_basis(route, *, allow_stale_registry):
    """Structural/version gate for `validation_basis` (task-brief B-2 §1.3).

    Returns the object when present and well-formed, `None` when the field is
    absent (legacy route), or `_DEGRADE_VALIDATION_BASIS` when the caller
    tolerates staleness and the object's `basis_version` exceeds this
    validator's ceiling. A structurally malformed object always raises on
    either `allow_stale_registry` setting -- no legitimate compiler can emit
    one (every required field is a non-empty absolute-path string by
    construction), so reaching this branch means a hand-edited and resealed
    record.
    """
    if "validation_basis" not in route:
        return None
    basis = route.get("validation_basis")
    if not isinstance(basis, dict):
        raise ValueError("invalid-validation-basis(field=validation_basis)")
    version = basis.get("basis_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("invalid-validation-basis(field=basis_version)")
    for field in ("registry_root", "unit_catalog_root", "runtime_root"):
        value = basis.get(field)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(f"invalid-validation-basis(field={field})")
    for field in ("runtime_root_validated", "runtime_root_match"):
        if field in basis and not isinstance(basis[field], bool):
            raise ValueError(f"invalid-validation-basis(field={field})")
    if version > VALIDATION_BASIS_VERSION:
        if allow_stale_registry:
            return _DEGRADE_VALIDATION_BASIS
        raise ValueError(f"unsupported-validation-basis-version(basis_version={version})")
    return basis

def classify_validation_basis(route, *, registry_digest_now, units_digest_now,
                              registry_root_now, unit_catalog_root_now):
    """Pure classifier for a route's registry/unit-catalog currentness
    (task-brief B-2 §1.4/§1.5). Never raises and never touches the filesystem.

    `route["validation_basis"]` must already have passed
    `_check_validation_basis`; its absence (legacy route) makes both axes
    classify same-root staleness on a digest mismatch, preserving today's
    exact wording.
    """
    basis = route.get("validation_basis")
    axes = {}
    for axis, own_digest, own_root, digest_key, root_key, stale_message, skew_reason in (
        ("registry", registry_digest_now, registry_root_now,
         "registry_digest", "registry_root", "stale registry digest", "registry-digest-skew"),
        ("unit_catalog", units_digest_now, unit_catalog_root_now,
         "unit_catalog_digest", "unit_catalog_root", "stale unit catalog digest", "unit-catalog-digest-skew"),
    ):
        sealed_digest = route.get(digest_key)
        if sealed_digest is None or sealed_digest == own_digest:
            axes[axis] = {"verdict": "current", "message": None}
            continue
        if basis is None or agent_home_equivalent(basis[root_key], own_root):
            axes[axis] = {"verdict": "stale", "message": stale_message}
            continue
        sealed_root = basis[root_key]
        axes[axis] = {
            "verdict": "skew",
            "message": (
                f"{skew_reason}(compiled={sealed_digest}@{sealed_root}, "
                f"validator={own_digest}@{own_root})"
            ),
        }
    verdict, message = "current", None
    for axis in ("registry", "unit_catalog"):
        if axes[axis]["verdict"] != "current":
            verdict, message = axes[axis]["verdict"], axes[axis]["message"]
            break
    return {
        "registry": axes["registry"], "unit_catalog": axes["unit_catalog"],
        "verdict": verdict, "message": message, "basis_present": basis is not None,
    }

def verify_route(route, expected_cwd=None, *, allow_stale_registry=False):
    """Verify a route for mutating/resume use.

    `allow_stale_registry` exists for one caller: closing a route. A registry or unit
    edit inside the same cycle changes the digest and permanently invalidates the route
    compiled before it, so a strict `close` could never record the outcome of exactly
    the work that changed the registry — leaving an open route that `WORKFLOW §0.5`
    calls indistinguishable from abandoned work. Closure writes a sidecar and grants no
    authority, so the route's own integrity (hash, id, cwd) is the right gate there;
    digest currentness stays required for anything that launches, dispatches, or
    mutates, and the closure records which case it was.
    """
    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        raise ValueError(
            f"legacy route schema_version={route.get('schema_version')!r} rejected for mutating/resume use"
        )
    if route.get("dispatch_contract_version") != DISPATCH_CONTRACT_VERSION:
        raise ValueError("legacy dispatch contract is read-only")
    scope_version=route.get("dispatch_evidence_scope_version")
    if scope_version not in (None, DISPATCH_EVIDENCE_SCOPE_VERSION):
        raise ValueError("unsupported dispatch evidence scope version")
    if route.get("route_hash") != route_hash(route): raise ValueError("stale or modified route hash")
    if route.get("route_id") != "rt-"+route["route_hash"].split(":",1)[1][:16]: raise ValueError("invalid route id")
    if expected_cwd and Path(expected_cwd).resolve()!=Path(route["cwd"]): raise ValueError("route cwd mismatch")
    basis=_check_validation_basis(route, allow_stale_registry=allow_stale_registry)
    if basis is _DEGRADE_VALIDATION_BASIS:
        # An unsupported basis_version is a legitimate newer harness's route;
        # closure records it honestly as unproven rather than stranding it.
        return dict(route, _registry_current=False)
    registry=TOPO.load_registry()
    classification=classify_validation_basis(
        route, registry_digest_now=TOPO.registry_digest(registry),
        units_digest_now=unit_catalog_digest(),
        registry_root_now=TOPO.ROOT, unit_catalog_root_now=ROOT,
    )
    if classification["verdict"] != "current":
        if not allow_stale_registry:
            raise ValueError(classification["message"])
        # A stale/skewed sealed graph cannot be re-derived from the current registry, so
        # every check that compares against it is skipped rather than guessed at.
        return dict(route, _registry_current=False)
    if "continuation_contract_version" in route:
        return _verify_continuation_route(route)
    _validate_output_scopes(route.get("nodes", []))
    def _node_identity(node):
        return {
            k: v for k, v in node.items()
            if k not in ("fallback_hops", "harness_affinity", "harness_policy")
        }
    if route.get("composed"):
        if route.get("effective_intensity") in ("direct","quick"):
            raise ValueError("composed routes require a standard+ effective intensity")
        composed_recipe=route.get("composed_recipe")
        if not isinstance(composed_recipe, dict):
            raise ValueError("composed route lacks embedded composed_recipe")
        TOPO._validate_recipe(
            composed_recipe, registry,
            registry["owner_profile_by_intensity"]["standard"],
        )
        expected_nodes=json.loads(json.dumps(composed_recipe["standard_plus"]["nodes"]))
        expected_nodes=_expand_parallel_groups(
            expected_nodes, composed_recipe["standard_plus"].get("parallel_groups"),
            route.get("effective_intensity"), route.get("capability"),
            auxiliary_check_units=registry.get("auxiliary_check_units"))
        if ([_node_identity(n) for n in route.get("nodes",[])]
                != [_node_identity(n) for n in expected_nodes]):
            raise ValueError("composed route nodes differ from embedded composed recipe")
        route_recipe=composed_recipe
    else:
        route_recipe=TOPO.resolve_recipe(
            registry, route.get("capability"), route.get("capability_mode")
        )
        if route.get("effective_intensity") not in ("direct", "quick"):
            expected_nodes=json.loads(json.dumps(route_recipe["standard_plus"]["nodes"]))
            expected_nodes=_expand_parallel_groups(
                expected_nodes, route_recipe["standard_plus"].get("parallel_groups"),
                route.get("effective_intensity"), route.get("capability"),
                auxiliary_check_units=registry.get("auxiliary_check_units"))
            # The remaining verifier owns field-level diagnostics.  This
            # census closes only the undeclared fanout hole: a rehashed route
            # may not add, remove, reorder, or rename recipe nodes.
            if ([n.get("id") for n in route.get("nodes", [])]
                    != [n.get("id") for n in expected_nodes]):
                raise ValueError("route nodes differ from the declared recipe")
    expected_extensions=_realize_conditional_extensions(
        route_recipe, route.get("effective_intensity")
    )
    if route.get("conditional_extensions") != expected_extensions:
        raise ValueError("route conditional extensions differ from the sealed recipe")
    route_node_ids={node.get("id") for node in route.get("nodes", [])}
    if any(not set(row["after"]) <= route_node_ids for row in expected_extensions):
        raise ValueError("route conditional extension anchor is not realized")
    expected_bindings=json.loads(json.dumps(
        route_recipe["human_gate_bindings"]
        if route.get("effective_intensity") not in ("direct","quick") else []))
    if route.get("human_gate_bindings") != expected_bindings:
        raise ValueError("route human gate bindings differ from the sealed recipe")
    if route.get("workflow_contract") != _workflow_contract(
            registry, route.get("nodes",[]), expected_bindings):
        raise ValueError("route workflow contract differs from the realized stage graph")
    if {row["gate"] for row in expected_bindings} - set(route.get("human_gates") or []):
        raise ValueError("route binds an undeclared human gate")
    if route.get("owner_dispatch_depth") not in {0, 1} or route.get("max_dispatch_depth") not in {0, 1, 2}:
        raise ValueError("invalid qualified dispatch depth")
    if any(key in route for key in ("depth", "owner_depth", "max_depth")):
        raise ValueError("bare route dispatch-depth fields are forbidden")
    allocation = route.get("dispatch_allocation")
    if allocation is not None:
        if not isinstance(allocation, dict) or set(allocation) not in ({
            "strategy", "window", "harness_order"
        }, {
            "strategy", "window", "usage_gate_used_percent", "harness_order"
        }):
            raise ValueError("invalid dispatch_allocation shape")
        if allocation.get("strategy") not in {
            "config-order", "least-recent-attempts", "capacity-aware", "balanced"
        }:
            raise ValueError("invalid dispatch_allocation strategy")
        window = allocation.get("window")
        if (
            not isinstance(window, int)
            or window < 0
            or (allocation["strategy"] in {"least-recent-attempts", "capacity-aware", "balanced"} and window < 3)
        ):
            raise ValueError("invalid dispatch_allocation window")
        gate = allocation.get("usage_gate_used_percent", 90)
        if "usage_gate_used_percent" in allocation and (
            not isinstance(gate, int) or not 0 <= gate <= 100
        ):
            raise ValueError("invalid dispatch_allocation usage gate")
        order = allocation.get("harness_order")
        if (
            not isinstance(order, list)
            or not order
            or len(order) != len(set(order))
            or any(item not in DEFAULTS.DISPATCHABLE_HARNESSES for item in order)
        ):
            raise ValueError("invalid dispatch_allocation harness order")
    observed_dispatch_depths = [route["owner_dispatch_depth"]]
    effective=route.get("effective_intensity")
    expected_owner_profile=(
        None if effective=="direct"
        else registry["owner_profile_by_intensity"].get(effective)
    )
    if route.get("owner_model_profile") != expected_owner_profile:
        raise ValueError("owner_model_profile differs from the portable intensity policy")
    def validate_harness_policy(policy):
        if policy is None:
            return
        if not isinstance(policy, dict):
            raise ValueError("harness_policy must be a mapping or null")
        flattened = []
        for band in DEFAULTS.QUALITY_BANDS:
            values = policy.get(band)
            if not isinstance(values, list) or any(
                value not in DEFAULTS.DISPATCHABLE_HARNESSES for value in values
            ):
                raise ValueError(f"invalid harness_policy band: {band}")
            flattened.extend(values)
        if len(flattened) != len(set(flattened)):
            raise ValueError("harness_policy repeats a harness across bands")
        threshold = policy.get("promote_relief_below")
        if not isinstance(threshold, int) or not 0 <= threshold <= 100:
            raise ValueError("invalid harness_policy promote_relief_below")
    owner_policy = route.get("owner_harness_policy")
    validate_harness_policy(owner_policy)
    if effective == "direct" and owner_policy is not None:
        raise ValueError("direct route cannot carry owner_harness_policy")
    if owner_policy is not None and allocation is not None:
        owner_set = {
            harness for band in DEFAULTS.QUALITY_BANDS for harness in owner_policy[band]
        }
        if owner_set != set(allocation["harness_order"]):
            raise ValueError("owner_harness_policy differs from dispatch allocation pool")
    realized_groups = {}
    for node in route.get("nodes", []):
        if node.get("kind") == "resource-runner":
            if any(
                key in node
                for key in (
                    "depth", "owner_depth", "max_depth", "dispatch_depth",
                    "transport", "fallback_hops",
                )
            ):
                raise ValueError(f"resource node {node.get('id')} has dispatch attempt fields")
            if node.get("resource_transport") != "detached-process":
                raise ValueError(f"resource node {node.get('id')} lacks detached lifecycle")
            continue
        if node.get("dispatch_depth") in {1, 2}:
            profile = node.get("model_profile")
            row = registry["model_profiles"].get(profile)
            if not isinstance(row, dict) or row.get("registered_topology") is not True:
                raise ValueError(f"node {node.get('id')} has invalid registered model_profile")
        if (
            effective not in ("direct", "quick")
            and node.get("kind") == "capability-owner"
            and node.get("unit") == "_kernel/owner"
            and (
                node.get("dispatch_depth") != 1
                or node.get("model_profile") != expected_owner_profile
            )
        ):
            raise ValueError(
                f"semantic capability owner {node.get('id')} differs from "
                "the portable standard+ owner policy"
            )
        if any(key in node for key in ("depth", "owner_depth", "max_depth")) or node.get("dispatch_depth") not in {0, 1, 2}:
            raise ValueError(f"node {node.get('id')} has invalid dispatch_depth")
        observed_dispatch_depths.append(node["dispatch_depth"])
        if "harness_affinity" in node and node["harness_affinity"] not in VALID_AFFINITY:
            raise ValueError(f"invalid harness_affinity vocabulary: {node['harness_affinity']!r}")
        policy = node.get("harness_policy")
        validate_harness_policy(policy)
        if "execution_surface" in node and node["execution_surface"] not in EXECUTION_SURFACES:
            raise ValueError(f"invalid execution_surface vocabulary: {node['execution_surface']!r}")
        if "registered_worker" in node and not isinstance(node["registered_worker"], bool):
            raise ValueError("registered_worker must be boolean")
        if "dispatch_fallback" in node:
            raise ValueError("legacy dispatch_fallback is read-only")
        for hop in node.get("fallback_hops", []):
            if not isinstance(hop, dict) or hop.get("fallback_hop") not in FALLBACK_HOPS:
                raise ValueError(f"invalid fallback_hop vocabulary: {hop!r}")
        group_id=node.get("parallel_group")
        if group_id:
            if node.get("replica_group") != group_id:
                raise ValueError(f"node {node.get('id')} has inconsistent parallel-group alias")
            realized_groups.setdefault(group_id,[]).append(node)
    expected_group_rows=[]
    for group_id, members in realized_groups.items():
        members.sort(key=lambda member: member.get("parallel_leg_index", -1))
        width=members[0].get("parallel_leg_count") if members else 0
        if width != len(members) or [member.get("parallel_leg_index") for member in members] != list(range(width)):
            raise ValueError(f"parallel group {group_id} has incomplete/duplicate leg indexes")
        invariant_fields=("parallel_group_kind","parallel_join_policy","parallel_independence_axes")
        if any(member.get("parallel_leg_count") != width for member in members):
            raise ValueError(f"parallel group {group_id} width metadata mismatch")
        if any(member.get(field) != members[0].get(field) for member in members for field in invariant_fields):
            raise ValueError(f"parallel group {group_id} invariant metadata mismatch")
        for index,left in enumerate(members):
            for right in members[index+1:]:
                if any(TOPO._overlap(a,b) for a in left.get("write_scope",[]) for b in right.get("write_scope",[])):
                    raise ValueError(f"parallel group {group_id} has overlapping write scopes")
        expected_group_rows.append({
            "id":group_id,
            "kind":members[0]["parallel_group_kind"],
            "join_policy":members[0]["parallel_join_policy"],
            "independence_axes":members[0]["parallel_independence_axes"],
            "width":width,
            "members":[member["id"] for member in members],
        })
    if route.get("parallel_groups") != expected_group_rows:
        raise ValueError("route parallel_groups summary differs from realized nodes")
    if route["max_dispatch_depth"] != max(observed_dispatch_depths):
        raise ValueError("max_dispatch_depth does not match the realized route")
    selection=route.get("selection",{})
    if effective=="direct":
        if (
            route.get("owner_dispatch_depth") != 0
            or route.get("max_dispatch_depth") != 0
            or selection.get("transport") != "interactive"
            or route.get("registered_headless_candidates") is not None
            or route.get("registered_headless_policy") is not None
            or len(route.get("nodes",[])) != 1
        ):
            raise ValueError("direct route shape mismatch")
        node=route["nodes"][0]
        if (
            node.get("id") != "inline"
            or node.get("dispatch_depth") != 0
            or node.get("execution_surface") != "inline"
            or node.get("registered_worker") is not False
            or node.get("fallback_hops")
        ):
            raise ValueError("direct node axes mismatch")
    elif effective=="quick":
        if (
            route.get("owner_dispatch_depth") != 1
            or route.get("max_dispatch_depth") != 1
            or selection.get("transport") != "headless"
            or selection.get("inline_reason") is not None
            or route.get("registered_headless_policy") != "serial-attempt"
            or len(route.get("nodes",[])) != 1
        ):
            raise ValueError("quick route shape mismatch")
        candidates=_validate_registered_headless_evidence({
            "candidates":route.get("registered_headless_candidates")
        })
        if candidates != route.get("registered_headless_candidates"):
            raise ValueError("quick registered-headless evidence is not canonical")
        node=route["nodes"][0]
        if (
            node.get("id") != "one-shot"
            or node.get("dispatch_depth") != 1
            or node.get("unit") != "_kernel/owner"
            or node.get("model_profile") != expected_owner_profile
            or node.get("execution_surface") != "registered-headless"
            or node.get("registered_worker") is not True
            or node.get("fallback_hops")
        ):
            raise ValueError("quick node axes mismatch")
    dd_digest=route.get("dispatch_defaults_digest")
    if dd_digest is not None and (not isinstance(dd_digest, str) or not dd_digest.startswith("sha256:")):
        raise ValueError("invalid dispatch_defaults_digest format")
    _validate_tracking_evidence(route.get("tracking"), route.get("tracked_gate_evidence"))
    escalation=route.get("selection",{}).get("escalation_basis")
    if not isinstance(escalation,list): raise ValueError("escalation_basis missing")
    if any(row.get("signal") in TRACKING for row in escalation if isinstance(row,dict)):
        raise ValueError("tracking cannot be an escalation basis")
    spec_touch=any(_scope_touches_spec(scope) for node in route.get("nodes",[]) for scope in node.get("write_scope",[]))
    if bool(route.get("spec_touch")) != spec_touch: raise ValueError("spec_touch declaration mismatch")
    if route.get("effective_intensity") not in ("direct","quick"):
        if route.get("selection",{}).get("transport") != "headless":
            raise ValueError("standard+ routes require checked headless transport")
        contract_version=route.get("dispatch_contract_version") or route.get("broker_contract_version") or 1
        parent_dispatch_depth=_evidence_parent_dispatch_depth(
            route.get("nodes",[]), route.get("owner_dispatch_depth"))
        checked_dispatch=_validate_dispatch_evidence(
            route.get("dispatch_evidence"), contract_version, parent_dispatch_depth,
            expected_worktree=route.get("cwd"),
            require_scope=scope_version == DISPATCH_EVIDENCE_SCOPE_VERSION)
        expected_chain=_fallback_chain(
            checked_dispatch, contract_version, parent_dispatch_depth,
            expected_worktree=route.get("cwd"),
            require_scope=scope_version == DISPATCH_EVIDENCE_SCOPE_VERSION)
        for node in route.get("nodes",[]):
            if node.get("dispatch_depth")==2:
                chain=_verify_fallback_chain(node, contract_version)
                if chain != expected_chain:
                    raise ValueError(f"dispatch-depth-2 node {node.get('id')} fallback differs from checked evidence")
    return route

def legacy_route_diagnostic(route):
    """Return read-only classification for historical Fleet display."""
    version=route.get("schema_version",1)
    return {
        "route_id":route.get("route_id"),
        "schema_version":version,
        "legacy":version != ROUTE_SCHEMA_VERSION,
        "classification":"version-tagged-read-only-bootstrap" if version != ROUTE_SCHEMA_VERSION else "current",
    }

def write_once(path, payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); data=json.dumps(payload,indent=2,ensure_ascii=False)+"\n"
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != data: raise ValueError("immutable route already exists with different content")
        return
    with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write(data); fh.flush(); os.fsync(fh.fileno())

def completion_dir(route_id, *, jobs=None):
    return resolve_dispatch_state_root(resolve_agent_home(), jobs)/"completion"/route_id


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

# Shared read-only terminal-gate seam: `close_route()` and `workflow-supervisor.py`'s
# `status`/`complete` all need the same four-field marker-identity truth (route id,
# route hash, node id, terminal-gate name, evidence readability, evidence hash), so it
# lives once here and `workflow-supervisor.py` dynamically loads this module rather than
# re-deriving it -- the dependency stays one-way (supervisor -> capability-route).
def terminal_gate_observation(route):
    """Per declared-terminal-node completion-gate truth, verified fresh from disk.

    An owner-merge auxiliary-bearing group contributes one extra row keyed
    `parallel_group:<group_id>` (G1/AC 5). Its downstream consumer is a
    `capability-owner` in two of the six realized groups, so nothing that node
    starts passes the wrapper start-gate -- without this row an unarbitrated
    group would leave no trace at all in the route's completion truth. Rows are
    judged in the same vocabulary as node rows, and no branch raises:
    `close_route` must stay able to close a failed route honestly.
    """
    nodes={node.get("id"):node for node in route.get("nodes",[])}
    terminal_ids=[node_id for node_id,node in nodes.items() if node.get("terminal") is True]
    rows={}
    for node_id in terminal_ids:
        node=nodes[node_id]
        rows[node_id]=_marker_identity_row(route,node,node_id,node.get("terminal_gate"))
    for group_id,error in sorted(owner_merge_auxiliary_groups(route).items()):
        rows[f"parallel_group:{group_id}"]=_arbitration_observation(route,group_id,error)
    return rows

def terminal_gate_proven(gates):
    """Tri-state aggregate: True if every declared terminal gate passed, False if any
    declared terminal gate is unproven, None only when no terminal node is declared."""
    if not gates:
        return None
    return all(row["passed"] for row in gates.values())

# D-2: route lifecycle records have exactly one canonical write location. `.resolve()`
# follows symlinks for every existing path segment, so a `--output` whose parent is a
# symlink pointing outside the canonical directory is classified by its real target, not
# its apparent one.
def canonical_routes_dir(artifact_root):
    return Path(artifact_root).resolve()/".runtime"/"routes"

def canonical_route_path(artifact_root, route_id):
    return canonical_routes_dir(artifact_root)/f"{route_id}.json"

def route_path_is_exact(path, artifact_root, route_id):
    return Path(path).resolve() == canonical_route_path(artifact_root, route_id)

def classify_route_location(path, artifact_root):
    """canonical | legacy-root | legacy-routes | legacy-_routes | legacy-.routes | outside"""
    resolved=Path(path).resolve()
    root=Path(artifact_root).resolve()
    if resolved.parent == canonical_routes_dir(artifact_root): return "canonical"
    if resolved.parent == root: return "legacy-root"
    if resolved.parent == root/"routes": return "legacy-routes"
    if resolved.parent == root/"_routes": return "legacy-_routes"
    if resolved.parent == root/".routes": return "legacy-.routes"
    return "outside"

_LEGACY_LOCATIONS=("legacy-root","legacy-routes","legacy-_routes","legacy-.routes")
_LOCATION_SORT_PRIORITY={"canonical":0,"legacy-root":1,"legacy-routes":2,"legacy-_routes":3,"legacy-.routes":4,"outside":5}

def atomic_write(path, payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    data=json.dumps(payload,indent=2,ensure_ascii=False)+"\n"
    temp=path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write(data); fh.flush(); os.fsync(fh.fileno())
    os.replace(temp,path)

# v2 adds `registry_current`: a closure recorded against a registry that has since
# changed is still a real closure, but it says so instead of implying currency.
# v3 adds `terminal_gate_proven`/`terminal_gates`: `close` previously validated only
# D-2 location and never consulted whether the workflow's terminal gate actually
# passed, so a route could be closed while `WORKFLOW §0.6`'s completion condition
# stayed false. Absence of these keys (v2 and earlier sidecars) has different
# semantics than an explicit `false` -- readers must not fold the two together.
OUTCOME_SCHEMA_VERSION=3
PUBLICATION_RESULTS=frozenset({"not-offered","skipped","succeeded","failed"})

def outcome_path(route_file):
    path=Path(route_file); return path.with_name(path.stem+".outcome.json")

def _head_commit(cwd):
    probe=subprocess.run(["git","-C",str(cwd),"rev-parse","HEAD"],text=True,capture_output=True)
    return probe.stdout.strip() if probe.returncode==0 else None

# A compiled route says work started; nothing said it finished. `complete` closes a
# registered attempt in the jobs registry, so an inline/direct route — which never
# reaches that registry — left no closure anywhere, and a leftover route file was
# indistinguishable from abandoned work. The route record cannot carry the closure
# itself: `route_hash` covers every field but the hash and id, so any added key makes
# `verify_route` reject it. The closure lives in a sidecar and binds `route_hash`, so a
# recompiled route leaves a detectably stale one rather than a silently wrong one.
def close_route(route, route_file, commit=None, summary=None, publication=None):
    from datetime import datetime, timezone
    # F7: D-2's single-storage-location contract has a compile-time entrance gate
    # (`route-output-outside-canonical`) but had no exit gate -- `close` would
    # happily write a sidecar next to a route file living anywhere at all. The
    # four legacy locations stay closeable read-only (that's how open records
    # left over from before D-2 get resolved); everywhere else is rejected.
    location=classify_route_location(route_file,route["artifact_root"])
    if location != "canonical" and location not in _LEGACY_LOCATIONS:
        raise ValueError("route-close-outside-canonical-or-legacy")
    alias_basename=(location=="canonical" and not route_path_is_exact(
        route_file,route["artifact_root"],route["route_id"]))
    if alias_basename or location in _LEGACY_LOCATIONS:
        print(
            "capability-route: route-location-drift "
            f"location={location} alias_basename={str(alias_basename).lower()} "
            f"route_file={Path(route_file).resolve()}",
            file=sys.stderr,
        )
    if publication is not None and publication not in PUBLICATION_RESULTS:
        raise ValueError("publication-unknown-result")
    target=outcome_path(route_file)
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8")), False
    # Live, not stored: every close computes gate truth fresh from the completion
    # markers on disk. A route closed once is never retroactively reopened to
    # recompute this, so the sidecar's `terminal_gate_proven` reflects gate state at
    # the moment of THIS close, not at any later inspection.
    gates=terminal_gate_observation(route)
    outcome={"schema_version":4 if publication is not None else OUTCOME_SCHEMA_VERSION,
             "route_id":route["route_id"],"route_hash":route["route_hash"],
             "route_file":str(Path(route_file).resolve()),"cwd":route["cwd"],
             "capability":route["capability"],"effective_intensity":route["effective_intensity"],
             "closed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
             "head_commit":commit or _head_commit(route["cwd"]),"summary":summary,
             "registry_current":route.get("_registry_current",True),
             "route_location":classify_route_location(route_file,route["artifact_root"]),
             "terminal_gate_proven":terminal_gate_proven(gates),"terminal_gates":gates}
    if publication is not None: outcome["publication"]=publication
    atomic_write(target,outcome)
    return outcome, True

def route_status(artifact_root, *, diagnostics=None):
    """Report every compiled route under one artifact root and whether it is closed.

    Scans the canonical `.runtime/routes/` directory plus four legacy locations
    (root-level `*-route.json`, `routes/`, `_routes/`, `.routes/`) read-only —
    D-2 blocks new writes to the legacy locations but `status` still surfaces
    them so open routes there remain discoverable and closeable.

    `diagnostics` is opt-in: when given a list, every candidate that fails to parse as a
    route (unreadable file, non-dict payload, missing `route_id`/`nodes`) is appended to
    it as `{"path", "location", "reason"}` instead of being silently skipped -- ordinary
    `status` callers that omit `diagnostics` keep today's fail-soft `continue` behavior
    unchanged; the scan itself never terminates on a malformed candidate either way.
    """
    root=Path(artifact_root)
    search_dirs=[canonical_routes_dir(artifact_root),root,root/"routes",root/"_routes",root/".routes"]
    by_route_id={}
    rows=[]
    for search_dir in search_dirs:
        if not search_dir.is_dir(): continue
        for path in sorted(search_dir.glob("*.json")):
            if path.name.endswith(".outcome.json"): continue
            try: raw=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError,UnicodeDecodeError) as exc:
                if diagnostics is not None:
                    diagnostics.append({"path":str(path),
                                         "location":classify_route_location(path,artifact_root),
                                         "reason":f"route-unreadable:{exc}"})
                continue
            if not isinstance(raw,dict) or "route_id" not in raw or "nodes" not in raw:
                if diagnostics is not None:
                    diagnostics.append({"path":str(path),
                                         "location":classify_route_location(path,artifact_root),
                                         "reason":"route-malformed-missing-required-keys"})
                continue
            location=classify_route_location(path,artifact_root)
            target=outcome_path(path)
            row={"route_file":str(path),"route_id":raw.get("route_id"),
                 "capability":raw.get("capability"),"effective_intensity":raw.get("effective_intensity"),
                 "source_commit":raw.get("source_commit"),"closed":target.is_file(),
                 "location":location,"drift":location != "canonical",
                 "read_only":location in _LEGACY_LOCATIONS}
            row["alias_basename"]=(location=="canonical" and not route_path_is_exact(
                path,artifact_root,row["route_id"]))
            row["drift"]=row["drift"] or row["alias_basename"]
            if row["closed"]:
                try: closure=json.loads(target.read_text(encoding="utf-8"))
                except (OSError,json.JSONDecodeError,UnicodeDecodeError): closure={}
                row["closed_at"]=closure.get("closed_at"); row["head_commit"]=closure.get("head_commit")
                row["stale_closure"]=closure.get("route_hash")!=raw.get("route_hash")
                row["registry_current"]=closure.get("registry_current",True)
            rows.append(row)
            by_route_id.setdefault(row["route_id"],[]).append(row["route_file"])
    for row in rows:
        locations=by_route_id.get(row["route_id"],[])
        if len(locations) > 1: row["duplicate_locations"]=sorted(locations)
    rows.sort(key=lambda row:(_LOCATION_SORT_PRIORITY.get(row["location"],9),row["route_file"]))
    return rows

def _marker_attempt_axes(node, attempt_id, attempt_metadata):
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

def _next_marker_sequence(directory, node_id):
    maximum=0
    if directory.is_dir():
        prefix=f"{node_id}."
        for path in directory.glob(f"{node_id}.*.json"):
            middle=path.name[len(prefix):-5]
            if middle.isdigit():
                maximum=max(maximum,int(middle))
    return maximum+1

def _completion_marker_replay(route, node, node_id, evidence, axes, directory):
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
    if not canonical_path.is_file():
        return None
    existing=json.loads(canonical_path.read_text(encoding="utf-8"))
    identity={
        "evidence_sha256":hashlib.sha256(evidence.read_bytes()).hexdigest(),
        **axes,
    }
    existing_identity={
        "evidence_sha256":existing.get("evidence",{}).get("sha256"),
        **{key:existing.get(key) for key in axes},
    }
    if existing_identity!=identity:
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
    return existing

def write_completion_marker(route, node, node_id, evidence, *, attempt_id=None, attempt_metadata=None):
    _migrate_completion_dir_forward(route["route_id"])
    directory=completion_dir(route["route_id"])
    canonical_path=directory/f"{node_id}.json"
    sha=hashlib.sha256(evidence.read_bytes()).hexdigest()
    axes=_marker_attempt_axes(node, attempt_id, attempt_metadata)
    replayed=_completion_marker_replay(route,node,node_id,evidence,axes,directory)
    if replayed is not None:
        return replayed
    sequence=_next_marker_sequence(directory,node_id)
    marker={
        "schema_version":2,
        "route_id":route["route_id"],"route_hash":route["route_hash"],
        "registry_digest":route["registry_digest"],"node_id":node_id,
        **axes,
        "completion_gate":node["completion_gate"],
        "evidence":{"path":str(evidence),"sha256":sha},
        "sequence":sequence,
    }
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

def _find_attempt_row_status(jobs, attempt_id):
    """Return the row status ('open'|'running'|'done') for attempt_id, or None if absent."""
    if not jobs.is_file(): return None
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields=line.split("\t")
        if len(fields)!=6: continue
        metadata=dict(part.split("=",1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("attempt_id")==attempt_id: return fields[1]
    return None

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

@contextlib.contextmanager
def _exclusive_lock(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a",encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def _attempt_completion_path(route, node_id, attempt_id, *, jobs=None):
    safe_attempt="".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in attempt_id
    )
    return completion_dir(route["route_id"],jobs=jobs)/f"{node_id}.{safe_attempt}.attempt.json"

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


AUXILIARY_ARBITER_OWNER_MERGE = "owner-merge"
AUXILIARY_ARBITER_NODE = "node"
ARBITRATION_SCHEMA_VERSION = 1


def _group_members(route, group_id):
    """Every realized leg of one parallel group, in route order."""
    return [
        candidate for candidate in route.get("nodes", [])
        if isinstance(candidate, dict)
        and candidate.get("parallel_group") == group_id
    ]


def _realized_auxiliary_nodes(route, group_id):
    return [
        member for member in _group_members(route, group_id)
        if member.get("leg_class") == "auxiliary"
    ]


def _realized_group_ids(route):
    return sorted({
        candidate["parallel_group"] for candidate in route.get("nodes", [])
        if isinstance(candidate, dict) and candidate.get("parallel_group")
    })


def _resolve_auxiliary_arbiter(route, group_id):
    """Who arbitrates one group's auxiliary findings, read off the compiled route.

    PRD 13.30.4 names an arbiter for each anchor kind that may declare an
    auxiliary leg, and in none of the three is it the anchor itself: a
    `review-worker` anchor's findings are merged by the conductor (the owner), a
    `map-worker` anchor's are read by its declared downstream consumer, and a
    `pipeline-stage` anchor's by its direct downstream `review-worker`. Gating
    the anchor (G1) demanded that a leg which runs *concurrently* with the
    auxiliary have already considered its output, which no anchor can satisfy.

    Returns `("owner-merge", None)` or `("node", <node_id>)`. Every undecidable
    case raises a typed error rather than defaulting to a pass -- an unresolvable
    arbiter is an integrity failure of the route, not an absent obligation.
    """
    members = _group_members(route, group_id)
    if not members:
        raise ValueError(f"auxiliary-group-unknown:{group_id}")
    anchor = next(
        (member for member in members if member.get("parallel_leg_index") == 0),
        None,
    )
    if anchor is None:
        raise ValueError(f"auxiliary-group-anchor-unknown:{group_id}")
    if anchor.get("terminal") is True:
        # Unreachable through a compiled route: `_expand_parallel_groups`
        # rejects a group declared on a terminal node (G6/AC 21) and
        # `capability_topology` rejects it again at declaration. Kept as a
        # typed error so a hand-built route cannot reach the gate silently.
        raise ValueError(f"auxiliary-arbiter-anchor-terminal:{anchor.get('id')}")
    if anchor.get("kind") == "review-worker":
        return AUXILIARY_ARBITER_OWNER_MERGE, None
    member_ids = {member.get("id") for member in members}
    consumers = [
        candidate for candidate in route.get("nodes", [])
        if isinstance(candidate, dict)
        and candidate.get("id") not in member_ids
        and anchor.get("id") in (candidate.get("depends_on") or [])
    ]
    if anchor.get("kind") == "pipeline-stage":
        # SD-82's pipeline-anchor arbiter requirement is unrevised: the arbiter
        # of a pipeline-stage anchor is its direct downstream review-worker.
        consumers = [
            candidate for candidate in consumers
            if candidate.get("kind") == "review-worker"
        ]
    # A consumer that is itself a realized parallel group appears here as every
    # one of its legs (D3 copies `depends_on` into each leg). They are one
    # arbiter, not three: collapse each leg onto its own anchor, which is the
    # node PRD 13.30.4 names ("autopilot-spec research" -> node `review`).
    arbiters = sorted({
        str(item.get("parallel_anchor") or item.get("id"))
        for item in consumers
    })
    if not arbiters:
        raise ValueError(f"auxiliary-arbiter-absent:{group_id}")
    if len(arbiters) > 1:
        raise ValueError(
            "auxiliary-arbiter-ambiguous:{}:{}".format(group_id, ",".join(arbiters))
        )
    return AUXILIARY_ARBITER_NODE, arbiters[0]


def owner_merge_auxiliary_groups(route):
    """Realized auxiliary-bearing groups whose arbiter is the owner's merge record.

    Returns `{group_id: error_or_None}` so a read-only observer can report an
    unresolvable arbiter as a failed row instead of raising -- `close_route`
    must be able to close a failed route.
    """
    rows = {}
    for group_id in _realized_group_ids(route):
        if not _realized_auxiliary_nodes(route, group_id):
            continue
        try:
            kind, _arbiter = _resolve_auxiliary_arbiter(route, group_id)
        except ValueError as exc:
            rows[group_id] = str(exc)
            continue
        if kind == AUXILIARY_ARBITER_OWNER_MERGE:
            rows[group_id] = None
    return rows


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


def _safe_group_id(group_id):
    return "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in str(group_id)
    )


def arbitration_path(route_id, group_id):
    return completion_dir(route_id)/f"{_safe_group_id(group_id)}.arbitration.json"


def _marker_identity_row(route, node, node_id, gate, *, jobs=None):
    """One completion marker's on-disk truth, in the shared gate vocabulary."""
    path = completion_dir(route["route_id"],jobs=jobs)/f"{node_id}.json"
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"passed": False, "reason": "completion-marker-absent"}
    if (marker.get("route_id") != route.get("route_id")
            or marker.get("route_hash") != route.get("route_hash")
            or marker.get("node_id") != node_id
            or marker.get("completion_gate") != gate):
        return {"passed": False, "reason": "completion-marker-identity-mismatch"}
    evidence = marker.get("evidence") or {}
    try:
        digest = hashlib.sha256(Path(evidence["path"]).read_bytes()).hexdigest()
    except (OSError, KeyError, TypeError):
        return {"passed": False, "reason": "completion-evidence-unreadable"}
    if digest != evidence.get("sha256"):
        return {"passed": False, "reason": "completion-evidence-hash-mismatch"}
    return {"passed": True, "reason": "completion-marker-verified",
            "evidence": evidence.get("path")}


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
        digest = hashlib.sha256(Path(evidence["path"]).read_bytes()).hexdigest()
    except (OSError, KeyError, TypeError):
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
            "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
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


def _publish_completion_locked(
    route,
    node,
    node_id,
    evidence,
    *,
    attempt_id,
    attempt_metadata,
    require_existing_link=False,
):
    """Publish marker history, exact-attempt link, and canonical marker under one node lock."""

    _validate_auxiliary_arbiter(route, node, evidence)
    axes=_marker_attempt_axes(node,attempt_id,attempt_metadata)
    evidence_sha=hashlib.sha256(evidence.read_bytes()).hexdigest()
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
    elif require_existing_link:
        raise ValueError("completed attempt row lacks immutable completion link")

    if marker is None:
        marker=write_completion_marker(
            route,node,node_id,evidence,
            attempt_id=attempt_id,
            attempt_metadata=attempt_metadata,
        )
    if not attempt_id:
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
):
    """Atomically publish one exact-attempt completion and close only its row."""
    if jobs and not attempt_id:
        raise ValueError("registered completion requires --attempt-id")
    if not jobs and attempt_id and explicit_attempt_metadata is None:
        raise ValueError("unregistered completion requires explicit attempt metadata")
    if not jobs and explicit_attempt_metadata is not None and not attempt_id:
        raise ValueError("explicit attempt metadata requires --attempt-id")

    jobs_path=Path(jobs) if jobs else None
    directory=completion_dir(route["route_id"])
    node_lock=directory/f".{node_id}.completion.lock"
    with _exclusive_lock(node_lock):
        if not jobs_path:
            marker=_publish_completion_locked(
                route,node,node_id,evidence,
                attempt_id=attempt_id,
                attempt_metadata=explicit_attempt_metadata,
            )
            status="unregistered-complete" if attempt_id else None
            return marker, ({"attempt_id":attempt_id,"status":status} if status else None)

        try:
            ensure_global_registry_writable(jobs_path)
        except DispatchContractError as exc:
            if explicit_attempt_metadata is not None:
                _publish_completion_locked(
                    route,node,node_id,evidence,
                    attempt_id=attempt_id,
                    attempt_metadata=explicit_attempt_metadata,
                )
            raise ValueError(f"row-close-failed:{exc.reason}") from exc
        with _exclusive_lock(Path(f"{jobs_path}.lock")):
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
                    _publish_completion_locked(
                        route,node,node_id,evidence,
                        attempt_id=attempt_id,
                        attempt_metadata=explicit_attempt_metadata,
                    )
                raise ValueError(
                    f"attempt-row-absent:{attempt_id}; exact fallback attempt metadata required"
                )
            try:
                validate_attempt_metadata(row_metadata)
            except DispatchContractError as exc:
                raise ValueError(f"row-contract-invalid:{exc.reason}") from exc
            if row_metadata.get("subsession_id") or str(row_metadata.get("stage_authority", "1")).lower() in {"0", "false"}:
                raise ValueError("subsession-has-no-stage-gate-authority")
            if (
                row_metadata.get("route_id") != route["route_id"]
                or row_metadata.get("route_hash") != route["route_hash"]
                or row_metadata.get("route_node") != node_id
            ):
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
            marker_eligible=(
                already_closed
                and row_note=="completed-supervisor"
                and row_metadata.get("failure_class")=="pass"
            )
            if already_closed and row_note!="completed-marker" and not marker_eligible:
                raise ValueError(
                    f"attempt-row-terminal-without-completion:{row_note or 'unknown'}"
                )
            attempt_metadata={
                key:value for key,value in row_metadata.items()
                if not key.startswith("_")
            }
            marker=_publish_completion_locked(
                route,node,node_id,evidence,
                attempt_id=attempt_id,
                attempt_metadata=attempt_metadata,
                require_existing_link=already_closed and not marker_eligible,
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
            row_fields[5] += (
                f",note=completed-marker,completion_marker={canonical_marker_path}"
                f",completion_marker_history={history_marker_path}"
            )
            # DR-1: seal the pass verdict alongside the marker so partial-continuation
            # peer checks see immutable terminal success without re-deriving it.
            if row_metadata.get("failure_class") in (None,"","-"):
                row_fields[5] += ",failure_class=pass"
            lines[row_index]="\t".join(row_fields)
            _atomic_registry_replace(jobs_path,lines)
            return marker, {
                "attempt_id":attempt_id,
                "status":"marker-appended" if marker_eligible else "closed",
            }

def _git_changed_files(worktree):
    """Return the worktree's git-visible changed file paths (AC 28 audit)."""
    try:
        result = subprocess.run(
            # `-uall`: the default collapses an untracked directory to one
            # `dir/` entry, which can never match a `fixed_files` path and made
            # every slice that created a new directory look like an escape.
            # The audit compares files, so it has to be given files.
            ["git", "-C", str(worktree), "status", "--porcelain", "-uall"],
            text=True, capture_output=True, check=False,
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
            text=True, capture_output=True, check=False,
        )
    except (OSError, ValueError):
        return set()
    if probe.returncode != 0:
        return set()
    return {
        (Path(worktree) / line.strip()).resolve(strict=False)
        for line in probe.stdout.splitlines()
        if line.strip()
    }


SUBDIVISION_BASELINE_SCHEMA_VERSION = 1


def subdivision_baseline_path(route_id, node_id, manifest_sha256):
    """Keyed by the manifest hash so a resumed admission finds its own baseline.

    Kept in its own subdirectory: the completion directory's own filenames are
    read back by `<node_id>.*.json` globs, and a sibling file matching that
    shape would be counted as marker history by any reader less careful than
    `_next_marker_sequence`.
    """
    return (
        completion_dir(route_id)
        / "subdivision"
        / f"{node_id}.{str(manifest_sha256)[:32]}.json"
    )


def record_subdivision_baseline(route, node_id, manifest):
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
    path = subdivision_baseline_path(route["route_id"], node_id, digest)
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
    record = {
        **identity,
        "head_commit": _head_commit(worktree),
        # anchor M3 / B5: path -> content digest, not a path list. A path list
        # is a permanent exemption: a file dirty at admission became invisible
        # to the audit for the whole subdivision, and those are exactly the
        # files the baseline exists to excuse (the stage's own dev log and
        # checklist), so in practice they are always dirty. Digests make the
        # subtraction a real delta -- unchanged since admission stays exempt,
        # changed again does not.
        "changed_files": {
            str(item): _content_digest(item)
            for item in sorted(_git_changed_files(worktree))
        },
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


def load_subdivision_baseline(route, node_id, manifest):
    """Resume the admission-time baseline by manifest hash; None when absent.

    Read across every dispatch state root, the same order completion markers use.
    Reading only the canonical root meant a state-root rotation kept the markers
    (which iterate the roots, and have `_migrate_completion_dir_forward`) while
    losing the baseline, and for a parallel subdivision a missing baseline is a
    permanent `subdivision-baseline-missing`. The writer still uses one root.
    """
    digest = manifest["_manifest_sha256"]
    canonical = subdivision_baseline_path(route["route_id"], node_id, digest)
    candidates = [canonical] + [
        root / "completion" / route["route_id"] / "subdivision" / canonical.name
        for root in dispatch_state_roots(resolve_agent_home())
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
    for session in manifest["sessions"]:
        matches=rows.get(session["attempt_id"],[])
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
            or metadata.get("note") not in {"completed-supervisor", "completed-marker"}
            or metadata.get("failure_class")!="pass"
        ):
            raise ValueError(f"subsession not semantic PASS:{session['attempt_id']}")
        process=attempt_process_quiescence(metadata)
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
    baseline = load_subdivision_baseline(route, node_id, manifest)
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
    if baseline is not None:
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
    changed = _git_changed_files(worktree) | committed
    outside = changed - declared_union - preexisting
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
        )
    return marker,{"status":"stage-gate-aggregated","sessions":len(manifest["sessions"])}

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
    c=sub.add_parser("compile"); c.add_argument("--capability",required=True); c.add_argument("--capability-mode",default="default")
    c.add_argument("--intensity",default="auto"); c.add_argument("--cwd",required=True); c.add_argument("--artifact-root",required=True)
    c.add_argument("--predicate",action="append",default=[]); c.add_argument("--signal",action="append",default=[])
    c.add_argument("--transport",default=None); c.add_argument("--transport-evidence",default="caller-selected")
    c.add_argument("--inline-reason"); c.add_argument("--tracking",choices=sorted(TRACKING),required=True)
    c.add_argument("--dispatch-evidence",help="JSON file with checked nested tuples/native evidence")
    c.add_argument("--registered-headless-evidence",help="JSON file with checked quick candidates")
    c.add_argument("--composed-recipe",help="JSON file with a compose-on-demand recipe (sealed composed: true)")
    c.add_argument("--spec-read",required=True); c.add_argument("--drift-verdict",required=True)
    c.add_argument("--workflow-mode",choices=sorted(TRACKING),required=True); c.add_argument("--artifact-guard",required=True)
    c.add_argument("--output")
    co=sub.add_parser("continuation")
    co.add_argument("--source-route",required=True)
    co.add_argument("--resume-from-node",required=True)
    co.add_argument("--requested-boundary",required=True)
    co.add_argument("--reason",required=True)
    co.add_argument("--artifact-root",required=True)
    co.add_argument("--output")
    co.add_argument("--lineage-operation",choices=("resume","fork"),default="resume")
    co.add_argument("--thread-id")
    co.add_argument("--new-thread-id")
    co.add_argument("--forked-from-id")
    co.add_argument("--last-turn-id")
    co.add_argument("--ephemeral",action="store_true")
    co.add_argument("--partial-group-manifest")
    co.add_argument("--source-group-id")
    co.add_argument("--failed-source-attempt-id")
    co.add_argument("--gap-leg-id")
    v=sub.add_parser("verify"); v.add_argument("--route",required=True); v.add_argument("--cwd")
    v.add_argument("--launch-phase",choices=("dry-run","register","start"))
    n=sub.add_parser("node"); n.add_argument("--route",required=True); n.add_argument("--node",required=True)
    d=sub.add_parser("complete"); d.add_argument("--route",required=True); d.add_argument("--node",required=True); d.add_argument("--evidence",required=True); d.add_argument("--output")
    d.add_argument("--jobs",help="canonical registry path for a registered attempt")
    d.add_argument("--attempt-id",help="exact current attempt id")
    d.add_argument("--dispatch-depth",type=int)
    d.add_argument("--transport")
    d.add_argument("--execution-surface")
    d.add_argument("--registered-worker",choices=("0","1","false","true"))
    d.add_argument("--fallback-hop")
    d.add_argument("--subsession-manifest",help="aggregate declared sub-sessions into this one stage gate")
    ar=sub.add_parser("arbitrate"); ar.add_argument("--route",required=True)
    ar.add_argument("--group",required=True,help="realized auxiliary-bearing parallel group id")
    ar.add_argument("--evidence",required=True,help="owner merge record carrying auxiliary_findings_considered")
    ar.add_argument("--output")
    cl=sub.add_parser("close"); cl.add_argument("--route",required=True)
    cl.add_argument("--commit",help="result commit; defaults to HEAD in the route cwd")
    cl.add_argument("--summary",help="one line naming what the route produced")
    st=sub.add_parser("status"); st.add_argument("--artifact-root",required=True)
    st.add_argument("--open-only",action="store_true",help="list only routes with no recorded outcome")
    a=p.parse_args()
    if a.command=="compile":
        gate={"spec_read":{"satisfied":a.spec_read.lower() not in ("0","false","no"),"source":a.spec_read},
              "drift_verdict":a.drift_verdict,"workflow_mode":a.workflow_mode,
              "artifact_guard":{"satisfied":a.artifact_guard.lower() not in ("0","false","no"),"source":a.artifact_guard}}
        dispatch_evidence=json.loads(Path(a.dispatch_evidence).read_text()) if a.dispatch_evidence else None
        registered_headless_evidence=(
            json.loads(Path(a.registered_headless_evidence).read_text())
            if a.registered_headless_evidence else None
        )
        if a.composed_recipe:
            composed_recipe=json.loads(Path(a.composed_recipe).read_text())
            if composed_recipe.get("capability") != a.capability:
                raise ValueError("composed recipe capability differs from --capability")
            route=compile_composed_route(
                composed_recipe,a.capability_mode,a.intensity,a.cwd,a.artifact_root,
                predicates=a.predicate,signals=a.signal,transport=a.transport,
                transport_evidence=a.transport_evidence,inline_reason=a.inline_reason,
                tracking=a.tracking,tracked_gate_evidence=gate,
                dispatch_evidence=dispatch_evidence,
                registered_headless_evidence=registered_headless_evidence,
            )
        else:
            route=compile_route(
                a.capability,a.capability_mode,a.intensity,a.cwd,a.artifact_root,
                a.predicate,a.signal,a.transport,a.transport_evidence,a.inline_reason,
                a.tracking,gate,dispatch_evidence,registered_headless_evidence,
            )
        vbasis=route.get("validation_basis") or {}
        if vbasis.get("runtime_root_match") is False:
            launch_tuple=route.get("launch_compatibility_tuple") or {}
            expected=launch_tuple.get("registry_root")
            observed=launch_tuple.get("runtime_root")
            print("route_file_written=0 registered=0 started=0 child_spawned=0",file=sys.stderr)
            raise ValueError(
                "launch-runtime-root-mismatch "
                f"expected={canonical(expected).decode()} observed={canonical(observed).decode()}"
            )
        expected_output=canonical_route_path(a.artifact_root,route["route_id"])
        if a.output:
            output_path=Path(a.output)
            if classify_route_location(output_path,a.artifact_root) != "canonical":
                raise ValueError("route-output-outside-canonical")
            if not route_path_is_exact(output_path,a.artifact_root,route["route_id"]):
                raise ValueError("route-output-alias-basename")
        else:
            output_path=expected_output
        write_once(output_path,route)
        print(f"route_file={output_path.resolve()}",file=sys.stderr)
        print(json.dumps(route,sort_keys=True))
    elif a.command=="continuation":
        source_path=Path(a.source_route).resolve(strict=True)
        source=verify_route(json.loads(source_path.read_text(encoding="utf-8")))
        artifact=Path(a.artifact_root).resolve(strict=False)
        if artifact != Path(source["artifact_root"]).resolve(strict=False):
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            raise ValueError("continuation-artifact-root-mismatch")
        partial_values=(
            a.partial_group_manifest,a.source_group_id,
            a.failed_source_attempt_id,a.gap_leg_id,
        )
        if any(partial_values) and not all(partial_values):
            raise ValueError("partial-continuation-input-incomplete")
        partial=None
        if a.partial_group_manifest:
            partial={
                "source_group_id":a.source_group_id,
                "source_batch_manifest":json.loads(
                    Path(a.partial_group_manifest).read_text(encoding="utf-8")
                ),
                "failed_source_attempt_id":a.failed_source_attempt_id,
                "gap_leg_id":a.gap_leg_id,
            }
        route=build_continuation_route(
            source,resume_from_node=a.resume_from_node,
            requested_boundary=a.requested_boundary,reason=a.reason,
            artifact_root=artifact,lineage_operation=a.lineage_operation,
            thread_id=a.thread_id,new_thread_id=a.new_thread_id,
            forked_from_id=a.forked_from_id,last_turn_id=a.last_turn_id,
            ephemeral=a.ephemeral,partial_group=partial,
        )
        if (
            route.get("requested_boundary_blocker")
            or route.get("first_runnable_blocker")
        ):
            print(json.dumps(route,sort_keys=True),file=sys.stderr)
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            raise ValueError("continuation-boundary-blocked")
        launch_tuple=route.get("launch_compatibility_tuple") or {}
        if not agent_home_equivalent(
            (launch_tuple.get("registry_root") or {}).get("path",""),
            (launch_tuple.get("runtime_root") or {}).get("path",""),
        ):
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            raise ValueError("launch-runtime-root-mismatch")
        output_path=Path(a.output) if a.output else canonical_route_path(
            artifact,route["route_id"]
        )
        try:
            publish_continuation_route(route,source,output_path)
        except ValueError as exc:
            if str(exc)=="continuation-source-evidence-drift":
                print(
                    "route_file_written=0 predecessor_attempts=0 registered=0 "
                    "started=0 child_spawned=0",file=sys.stderr,
                )
            raise
        print(f"route_file={output_path.resolve()}",file=sys.stderr)
        print(json.dumps(route,sort_keys=True))
    elif a.command=="status":
        rows=route_status(a.artifact_root)
        if a.open_only: rows=[row for row in rows if not row["closed"]]
        print(json.dumps(rows,sort_keys=True,indent=2))
    else:
        route=verify_route(
            json.loads(Path(a.route).read_text()), getattr(a,"cwd",None),
            allow_stale_registry=a.command=="close",
        )
        if a.command=="verify":
            if a.launch_phase:
                compatible,mismatches=revalidate_launch_compatibility(route)
                if mismatches.get("tuple") == "absent-legacy":
                    print("registered=0 started=0 child_spawned=0",file=sys.stderr)
                    raise ValueError("launch-compatibility-tuple-required")
                if not compatible:
                    name=sorted(mismatches)[0]
                    mismatch=mismatches[name]
                    print(
                        "launch-runtime-root-mismatch "
                        f"phase={a.launch_phase} mismatch={name}:"
                        f"expected={canonical(mismatch.get('expected',mismatch)).decode()}:"
                        f"actual={canonical(mismatch.get('actual',mismatch)).decode()}",
                        file=sys.stderr,
                    )
                    print("registered=0 started=0 child_spawned=0",file=sys.stderr)
                    raise ValueError("launch-runtime-root-mismatch")
            print(f"route_id={route['route_id']}\nroute_hash={route['route_hash']}")
        elif a.command=="arbitrate":
            evidence=Path(a.evidence).resolve()
            if not evidence.is_file(): raise SystemExit("arbitration evidence missing")
            record=arbitrate_group(route,a.group,evidence)
            if a.output: atomic_write(a.output, record)
            print(json.dumps(record,sort_keys=True))
        elif a.command=="close":
            outcome,created=close_route(route,a.route,a.commit,a.summary)
            print(json.dumps(outcome,sort_keys=True))
            if not created: print("capability-route: route already closed",file=sys.stderr)
            if outcome.get("terminal_gate_proven") is False:
                reasons={node_id:row.get("reason") for node_id,row in
                         (outcome.get("terminal_gates") or {}).items() if not row.get("passed")}
                print(f"capability-route: terminal-gate-unproven route_id={outcome['route_id']} "
                      f"reasons={json.dumps(reasons,sort_keys=True)}",file=sys.stderr)
        else:
            node=next((x for x in route["nodes"] if x["id"]==a.node),None)
            if not node: raise SystemExit("unknown route node")
            if a.command=="node": print(json.dumps(node,sort_keys=True))
            else:
                evidence=Path(a.evidence).resolve()
                if not evidence.is_file(): raise SystemExit("completion evidence missing")
                raw_axes=(a.dispatch_depth,a.transport,a.execution_surface,a.registered_worker,a.fallback_hop)
                explicit_attempt_metadata=None
                if any(value is not None for value in raw_axes):
                    explicit_attempt_metadata={
                        "attempt_schema_version":2,
                        "dispatch_depth":a.dispatch_depth,
                        "transport":a.transport,
                        "execution_surface":a.execution_surface,
                        "registered_worker":a.registered_worker,
                        "fallback_hop":a.fallback_hop,
                    }
                if a.subsession_manifest:
                    if not a.jobs or a.attempt_id or explicit_attempt_metadata is not None:
                        raise ValueError("subsession completion requires --jobs and forbids attempt axes")
                    route["_route_file"]=str(Path(a.route).resolve())
                    marker,row=complete_subsession_stage(
                        route,node,a.node,evidence,a.subsession_manifest,a.jobs,
                    )
                else:
                    marker,row=complete_node(
                        route,node,a.node,evidence,
                        jobs=a.jobs,
                        attempt_id=a.attempt_id,
                        explicit_attempt_metadata=explicit_attempt_metadata,
                    )
                if a.output: atomic_write(a.output, marker)
                print(json.dumps(marker,sort_keys=True))
                if row: print(json.dumps(row,sort_keys=True))

if __name__=="__main__":
    try: main()
    except (ValueError,TOPO.TopologyError) as exc: print(f"capability-route: {exc}",file=sys.stderr); raise SystemExit(64)
