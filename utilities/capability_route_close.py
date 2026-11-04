"""Capability route: terminal observation, closing and status.

Legacy route diagnosis, terminal gate and owner terminal observation, stale
closure retirement, outcome replay, `close_route`, and `route_status`.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import route_identity as ROUTE_IDENTITY
import dispatch_terminal_commit
from dispatch_contract import (
    DispatchContractError, owner_operation_fence, verdict_pass, attempt_process_quiescence,
    completion_conflict_attempt, evidence_digest, parse_registry_metadata,
)
from codex_dispatch_terminal import inspect_terminal_attempt

from capability_route_base import (
    _exclusive_lock, arbitration_path, atomic_write, canonical_route_path,
    canonical_routes_dir, classify_route_location, completion_dir,
    owner_merge_auxiliary_groups, route_path_is_exact, ROUTE_SCHEMA_VERSION,
)

from capability_route_continuation import (
    _COMMIT_SHA, _marker_identity_row,
)

from capability_route_compose import (
    _compose_default_jobs,
)

from capability_route_revision import (
    _arbitration_observation, _route_revisions,
)


def legacy_route_diagnostic(route):
    """Return read-only classification for historical Fleet display."""
    version=route.get("schema_version",1)
    return {
        "route_id":route.get("route_id"),
        "schema_version":version,
        "legacy":version != ROUTE_SCHEMA_VERSION,
        "classification":"version-tagged-read-only-bootstrap" if version != ROUTE_SCHEMA_VERSION else "current",
    }

# Shared read-only terminal-gate seam: `close_route()` and `workflow-supervisor.py`'s
# `status`/`complete` all need the same four-field marker-identity truth (route id,
# route hash, node id, terminal-gate name, evidence readability, evidence hash), so it
# lives once here and `workflow-supervisor.py` dynamically loads this module rather than
# re-deriving it -- the dependency stays one-way (supervisor -> capability-route).
def terminal_gate_observation(route, *, jobs=None, exact_terminal=False, registry_rows=None,
                              registry_snapshot=None):
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
    if registry_snapshot is not None:
        try:
            snapshot_valid = (registry_snapshot.matches_jobs(jobs)
                              and registry_snapshot.complete)
        except (AttributeError, OSError, TypeError, ValueError):
            snapshot_valid = False
        if not snapshot_valid:
            reason = "registry-unreadable"
            for node_id in terminal_ids:
                rows[node_id] = {"passed": False, "reason": reason}
            for group_id, _error in sorted(owner_merge_auxiliary_groups(route).items()):
                rows[f"parallel_group:{group_id}"] = {"passed": False, "reason": reason}
            return rows
        registry_view = registry_snapshot.view
        registry_lines = registry_snapshot.lines
    else:
        registry_view = None
        registry_lines = (["\t".join(fields) for fields, _metadata in registry_rows]
                          if registry_rows is not None else None)
    for node_id in terminal_ids:
        node=nodes[node_id]
        marker=completion_dir(route["route_id"],jobs=jobs)/f"{node_id}.json"
        if owner_executed_terminal(node) and not (marker.exists() or marker.is_symlink()):
            rows[node_id]=_owner_terminal_observation(
                route,node,jobs=jobs,registry_rows=registry_rows,
                registry_lines=registry_lines,registry_view=registry_view)
        else:
            rows[node_id]=_marker_identity_row(route,node,node_id,node.get("terminal_gate"), jobs=jobs,
                                              exact_terminal=exact_terminal,
                                              registry_lines=registry_lines)
    for group_id,error in sorted(owner_merge_auxiliary_groups(route).items()):
        key=f"parallel_group:{group_id}"
        row=_arbitration_observation(route,group_id,error)
        if exact_terminal and row.get("passed"):
            try:
                raw=arbitration_path(route["route_id"],group_id).read_bytes()
                record=json.loads(raw); anchor=nodes[record["anchor_node"]]
                proof=_marker_identity_row(route,anchor,anchor["id"],anchor["completion_gate"],
                                           jobs=jobs,exact_terminal=True,
                                           registry_lines=registry_lines)
                if not proof.get("passed"):
                    row=proof
                else:
                    row=dict(row,node_id=key,attempt_id=proof["attempt_id"],completion_gate="owner-merge",
                             marker_digest=hashlib.sha256(raw).hexdigest(),
                             evidence_digest=evidence_digest(Path(record["evidence"]["path"])))
            except (OSError,ValueError,KeyError,TypeError):
                row={"passed":False,"reason":"auxiliary-arbitration-identity-unverified"}
        rows[key]=row
    return rows

def owner_executed_terminal(node):
    """A declared owner operation has the owner's executor, not an absent child."""
    return (node.get("terminal") is True and node.get("kind") == "capability-owner"
            and node.get("unit") == "_kernel/owner" and node.get("dispatch_depth") == 1)


def _owner_terminal_observation(route,node,*,jobs=None,registry_rows=None,
                                registry_lines=None,registry_view=None):
    """Consume the same exact native handoff as worker completion, without
    inventing a second attempt or publishing a synthetic worker marker.

    The claim binds this proof's digest just as it binds a worker marker. All
    subsequent readers recheck the owner, prerequisites, output and cleanup.
    """
    from owner_route_binding import resolve_owner_route_lifecycle
    def absent(reason):
        return {"passed":False,"reason":reason}
    try:
        jobs=Path(jobs) if jobs is not None else completion_dir(route["route_id"]).parents[1]/"jobs.log"
        if registry_view is not None:
            candidates=((record.fields,record.completion_metadata()) for record in
                        registry_view.completion_related("",route["route_id"],route["route_hash"]))
        elif registry_rows is not None:
            candidates=registry_rows
        else:
            from dispatch_registry_cache import registry_view as load_registry_view
            view=load_registry_view(jobs)
            registry_view=view
            if view.has_invalid_utf8 or view.has_partial_tail:
                # Keep the existing strict decoder and incomplete-line semantics.
                lines=jobs.read_text(encoding="utf-8").splitlines()
                candidates=((fields,parse_registry_metadata(fields[5]))
                            for line in lines for fields in (line.split("\t"),) if len(fields)==6)
            else:
                candidates=((record.fields,record.completion_metadata()) for record in
                            view.completion_related("",route["route_id"],route["route_hash"]))
        owners=[]
        for fields,meta in candidates:
            if meta.get("worker_type")!="owner" or meta.get("dispatch_depth")!="1": continue
            try:
                identity=ROUTE_IDENTITY.registered_node_identity(meta,node)
            except ValueError:
                continue
            if identity==(route["route_id"],route["route_hash"],node["id"]):
                owners.append((fields,meta))
        if not owners: return absent("owner-attempt-absent")
        fields,meta=owners[-1]
        binding,_=resolve_owner_route_lifecycle(
            jobs,owner_attempt_id=meta["attempt_id"],registry_rows=registry_rows)
        # A one-shot owner sealed by `route_*` fields alone (its supervisor closed the row
        # before the marker writer ran) has no lifecycle binding; the quick node-bound
        # contract that `dispatch_terminal_commit.validate_owner_route` enforces stands in.
        node_bound=(binding is None and route.get("effective_intensity")=="quick"
                    and not any(k in meta for k in ("owner_route_file","owner_route_id","owner_route_hash"))
                    and Path(meta.get("route_file","")).resolve()==canonical_route_path(route["artifact_root"],route["route_id"]))
        if not ((binding is not None and binding.route_id==route["route_id"] and binding.route_hash==route["route_hash"])
                or node_bound) or meta.get("registered_worker")!="1":
            return absent("owner-route-identity-mismatch")
        if fields[1]!="done" or not verdict_pass(meta):
            return absent("owner-terminal-not-pass")
        if completion_conflict_attempt({"attempt_id":meta["attempt_id"]},["\t".join(fields)]):
            return absent("terminal-evidence-conflict")
        process=attempt_process_quiescence(meta,terminal_receipt=True)
        if process.state!="quiescent": return absent("owner-not-quiescent")
        # A PASS proposal cannot erase a missing review or other prerequisite.
        prerequisites=owner_terminal_prerequisites(
            route,node,jobs,registry_rows=registry_rows,
            registry_lines=registry_lines)
        if prerequisites:
            return absent("owner-prerequisite-unproven:"+json.dumps(prerequisites,sort_keys=True))
        # The owner's own operation is a node entry like any other: a PASS does not stand in
        # for a person's release of the gate sealed at its entry (refine's preview approval).
        try:
            owner_operation_fence(route,node,jobs=jobs)
        except DispatchContractError as exc:
            return {**absent(exc.reason),"gate_detail":exc.detail}
        terminal=inspect_terminal_attempt(meta.get("log_file"),worktree=route["cwd"],
                                          artifact_root_metadata=route["artifact_root"],worker_type="owner",
                                          artifact_binding=meta)
        if terminal.get("state")!="valid" or terminal.get("verdict")!="PASS" or terminal.get("artifact_state")!="readable":
            return absent("owner-terminal-evidence-unverified")
        def decoded(value):
            return Path(base64.urlsafe_b64decode(value+"="*(-len(value)%4)).decode())
        # Bytes are read where the report lives now; the identity is the locator the owner named.
        # A move an earlier release made to a loose report is recorded by the producer and
        # surfaced as `artifact_origin_path_b64`, so placement never changes who the owner was.
        evidence=decoded(str(terminal["artifact_path_b64"]))
        origin=decoded(str(terminal["artifact_origin_path_b64"])) if terminal.get("artifact_origin_path_b64") else evidence
        digest=evidence_digest(evidence)
        identity={"route_id":route["route_id"],"route_hash":route["route_hash"],"node_id":node["id"],
                  "attempt_id":meta["attempt_id"],"completion_gate":node["terminal_gate"],
                  "evidence":str(origin),"evidence_digest":digest,"source":"owner-terminal"}
        return {**identity,"passed":True,"current":True,"reason":"owner-terminal-verified",
                "marker_digest":hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
                "attempt_readiness":"quiescent"}
    except (OSError,ValueError,KeyError,TypeError):
        return absent("owner-terminal-evidence-unverified")


def owner_terminal_prerequisites(route,node,jobs,*,registry_rows=None,registry_lines=None):
    nodes={n["id"]:n for n in route["nodes"]}
    pending=list(node.get("depends_on",[])); seen=set(); missing={}
    while pending:
        node_id=pending.pop()
        if node_id in seen: continue
        seen.add(node_id)
        predecessor=nodes[node_id]
        pending.extend(predecessor.get("depends_on",[]))
        if (predecessor.get("kind")=="capability-owner" and predecessor.get("unit")=="_kernel/owner"
                and predecessor.get("dispatch_depth")==1):
            continue  # This executor's final handoff includes its own preceding operations.
        proof=_marker_identity_row(route,predecessor,node_id,predecessor.get("completion_gate"),
                                   jobs=jobs,exact_terminal=predecessor.get("dispatch_depth") in (1,2),
                                   registry_lines=(registry_lines if registry_lines is not None else
                                                   (None if registry_rows is None else
                                                    ["\t".join(fields)
                                                     for fields, _metadata in registry_rows])))
        if not proof.get("passed"): missing[node_id]=proof["reason"]
    return missing

def terminal_gate_proven(gates):
    """Tri-state aggregate: True if every declared terminal gate passed, False if any
    declared terminal gate is unproven, None only when no terminal node is declared."""
    if not gates:
        return None
    return all(row["passed"] for row in gates.values())

_LEGACY_LOCATIONS=("legacy-root","legacy-routes","legacy-_routes","legacy-.routes")
_LOCATION_SORT_PRIORITY={"canonical":0,"legacy-root":1,"legacy-routes":2,"legacy-_routes":3,"legacy-.routes":4,"outside":5}

# v2 adds `registry_current`: a closure recorded against a registry that has since
# changed is still a real closure, but it says so instead of implying currency.
# v3 adds `terminal_gate_proven`/`terminal_gates`: `close` previously validated only
# D-2 location and never consulted whether the workflow's terminal gate actually
# passed, so a route could be closed while `WORKFLOW §0.6`'s completion condition
# stayed false. Absence of these keys (v2 and earlier sidecars) has different
# semantics than an explicit `false` -- readers must not fold the two together.
OUTCOME_SCHEMA_VERSION=3
PUBLICATION_RESULTS=frozenset({"not-offered","skipped","succeeded","failed"})

def retire_stale_closure(route_file):
    """Move a previous cycle's closure sidecar aside when this identity reopens.

    `route_hash` is a pure function of the compile request, so recompiling an
    identical request at the same HEAD reproduces the same `route_id` and the
    same canonical path — and `write_once` returns quietly because the bytes
    match. The route is therefore "open" again while the old
    `<route_id>.outcome.json` still sits beside it. Anything that reads that
    sidecar as closure truth would otherwise mistake the new cycle for the
    previous completed cycle.

    The closure is retired, never deleted: it records a real completed cycle.

    Returns the retired path, or None when there was nothing to retire.
    """
    sidecar=outcome_path(route_file)
    if not sidecar.is_file():
        return None
    from datetime import datetime, timezone
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # The suffix stays `.outcome.json`. Five scanners filter on exactly that
    # (`fleet_cutover_gate.route_bookkeeping`, `route_status`'s diagnostics,
    # `artifact_cutover`'s route population, `artifact_lifecycle`,
    # `artifact_resplit`), so a `.superseded-<ts>.json` tail made a retired
    # closure read as an open route, as a malformed record, and as
    # `migrate-residue` — which would relocate the very history this keeps.
    # `<route_id>.superseded-<ts>.outcome.json` cannot collide with a real
    # `outcome_path()`: no route file `<route_id>.superseded-<ts>.json` exists.
    base=route_file.stem
    retired=sidecar.with_name(f"{base}.superseded-{stamp}.outcome.json")
    index=1
    while retired.exists():
        index+=1
        retired=sidecar.with_name(f"{base}.superseded-{stamp}-{index}.outcome.json")
    try:
        os.replace(sidecar,retired)
    except FileNotFoundError:
        # A concurrent identical compile retired it first. `write_once` has
        # already succeeded, so dying here would fail a compile over a race on
        # bookkeeping.
        return None
    return retired

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
def _outcome_replay_matches(existing, *, route_id, route_hash,
                            terminal_commit_id=None, owner_attempt_id=None,
                            producer_binding_digest=None,
                            terminal_marker_digest=None,
                            inline_finish_id=None, summary_digest=None,
                            inline_commit=None):
    if not isinstance(existing, dict):
        return False
    if existing.get("route_id") != route_id or existing.get("route_hash") != route_hash:
        return False
    if inline_finish_id is not None:
        return all(existing.get(key) == value for key, value in (
            ("inline_finish_id", inline_finish_id),
            ("terminal_marker_digest", terminal_marker_digest),
            ("producer_binding_digest", producer_binding_digest),
            ("summary_digest", summary_digest),
            ("head_commit", inline_commit),
        ))
    if existing.get("terminal_gate_proven") is False:
        # A recorded unproven close is a real record with a real identity: what it stored stays
        # authoritative even when a caller omits it, so a matching commit cannot hide a foreign
        # owner/binding/marker. (An already-proven outcome replays on the baseline rule below.)
        for key, supplied in (("terminal_commit_id", terminal_commit_id),
                              ("terminal_owner_attempt_id", owner_attempt_id),
                              ("producer_binding_digest", producer_binding_digest),
                              ("terminal_marker_digest", terminal_marker_digest)):
            stored = existing.get(key)
            if stored is not None and supplied != stored:
                return False
    if terminal_commit_id is None and owner_attempt_id is None and producer_binding_digest is None:
        # Legacy callers retain the historical route/hash/optional-marker rule.
        return (terminal_marker_digest is None
                or existing.get("terminal_marker_digest") == terminal_marker_digest)
    # A caller that names an identity axis is compared on the axes it names;
    # a matching commit cannot hide an explicitly named owner/binding/marker
    # that the recorded outcome contradicts.
    for key, supplied in (("terminal_owner_attempt_id", owner_attempt_id),
                          ("producer_binding_digest", producer_binding_digest),
                          ("terminal_marker_digest", terminal_marker_digest)):
        stored = existing.get(key)
        if supplied is not None and stored is not None and supplied != stored:
            return False
    if terminal_commit_id is not None and existing.get("terminal_commit_id") == terminal_commit_id:
        return True
    return (owner_attempt_id is not None and producer_binding_digest is not None
            and terminal_marker_digest is not None
            and existing.get("terminal_owner_attempt_id") == owner_attempt_id
            and existing.get("producer_binding_digest") == producer_binding_digest
            and existing.get("terminal_marker_digest") == terminal_marker_digest)


def _current_terminal_identity(route, gates, owner_attempt_id):
    """The terminal tuple the runtime holds now for one candidate owner (read-only).

    Built only from state that already exists: the exact terminal markers just observed, the
    owner's terminal-commit slot, and its producer binding. An owner that neither holds a slot
    for this very marker set nor is the attempt that wrote a terminal marker is not provably
    current, and nothing is returned for it. Under the producer lifecycle the binding axes come from the
    binding held now, or from this owner's verified claimed slot when no binding file exists; with neither,
    no producer authority exists and nothing is returned, so a caller's or an old record's values never fill it in."""
    dtc = dispatch_terminal_commit
    try:
        rows = list(gates.values())
        marker_digest = dtc.terminal_marker_digest(rows)
        slot = dtc.terminal_slot(Path(route["artifact_root"]), route["route_id"], owner_attempt_id)
        state = None
        if (slot / "terminal-commit.json").is_file():
            state = json.loads((slot / "terminal-commit.json").read_text(encoding="utf-8"))
    except (dtc.TerminalCommitError, OSError, ValueError, KeyError, TypeError):
        return {}
    producer_digest = None
    if (isinstance(state, dict) and state.get("owner_attempt_id") == owner_attempt_id
            and state.get("route_id") == route["route_id"] and state.get("route_hash") == route["route_hash"]
            and state.get("terminal_marker_digest") == marker_digest
            and isinstance(state.get("producer_binding_digest"), str)
            and state.get("terminal_commit_id") == dtc.terminal_commit_id(
                route_id=route["route_id"], route_hash=route["route_hash"], owner_attempt_id=owner_attempt_id,
                marker_digest=marker_digest, producer_digest=state["producer_binding_digest"])):
        producer_digest = state["producer_binding_digest"]
    elif owner_attempt_id not in {row.get("attempt_id") for row in rows}:
        return {}
    current = {"terminal_owner_attempt_id": owner_attempt_id, "terminal_marker_digest": marker_digest}
    try:
        if dtc.producer_lifecycle_applies(route):
            try:
                binding = dtc.load_producer_binding(artifact_root=Path(route["artifact_root"]),
                                                    route_id=route["route_id"], owner_attempt_id=owner_attempt_id)
            except dtc.TerminalCommitError as exc:
                # No binding file: only this owner's verified claimed slot still knows its binding, and a
                # file that exists but is unusable proves nothing. Without either, no producer authority.
                if exc.code != "producer-binding-required" or producer_digest is None:
                    return {}
            else:
                if binding.digest is None or (binding.binding or {}).get("route_hash") != route["route_hash"]:
                    return {}
                producer_digest = binding.digest  # the binding held now, whatever an older claim recorded
        elif producer_digest is None:
            producer_digest = dtc._digest(dtc._canonical({"contract": "producer-binding-not-applicable/v1",
                                                         "reason": "sealed-topology-nonproducer"}))
    except (dtc.TerminalCommitError, OSError, ValueError, KeyError, TypeError):
        return {}
    current["producer_binding_digest"] = producer_digest
    current["terminal_commit_id"] = dtc.terminal_commit_id(
        route_id=route["route_id"], route_hash=route["route_hash"], owner_attempt_id=owner_attempt_id,
        marker_digest=marker_digest, producer_digest=producer_digest)
    return current


def _promote_historical_false_outcome(route, route_file, existing, raw, *, jobs=None,
                                     terminal_commit_id=None, owner_attempt_id=None,
                                     producer_binding_digest=None, terminal_marker_digest=None,
                                     inline_finish_id=None, summary_digest=None, inline_commit=None):
    """Consume a later exact terminal proof while retaining the original false bytes."""
    if (not isinstance(existing, dict) or existing.get("terminal_gate_proven") is not False
            or existing.get("autoclose") is not None
            or existing.get("disposition") in ("abandoned", "operator-decision", "cancelled")
            or route.get("stop_reason") is not None or route.get("workflow_state") == "CANCELLED"
            or existing.get("route_id") != route.get("route_id")
            or existing.get("route_hash") != route.get("route_hash")
            or existing.get("route_file") != str(Path(route_file).resolve())
            or existing.get("cwd") != route.get("cwd")):
        return existing, False
    try:
        import artifact_producer
        route_on_disk = json.loads(Path(route_file).read_text(encoding="utf-8"))
        if (not isinstance(route_on_disk, dict)
                or route_on_disk.get("route_id") != route.get("route_id")
                or route_on_disk.get("route_hash") != route.get("route_hash")):
            return existing, False
        cycle_record = artifact_producer.route_cycle_for(Path(route["artifact_root"]), route_on_disk)
        if cycle_record is not None:
            if cycle_record.get("abandon_reason") or cycle_record.get("deleted_at"):
                return existing, False
            if cycle_record.get("state") == "sealed":
                manifest_path = artifact_producer._record_cycle_manifest_path(Path(route["artifact_root"]), cycle_record)
                manifest = artifact_producer._read_json(manifest_path) if manifest_path.is_file() else None
                if not isinstance(manifest, dict) or (manifest.get("cycle") or {}).get("state") != "active":
                    return existing, False
    except ImportError:
        return existing, False
    except artifact_producer.ProducerError:
        return existing, False
    except (OSError, ValueError, KeyError, TypeError):
        return existing, False
    try:
        import inline_finish
        pending = inline_finish.pending_state(Path(route["artifact_root"]), route["route_id"])
    except (OSError, ValueError):
        return existing, False
    if pending and pending.get("state") != "finished" and not (
            inline_finish_id is not None
            and pending.get("inline_finish_id") == inline_finish_id
            and pending.get("state") in ("node-completed", "route-closed")):
        return existing, False
    cleanup_scope = dispatch_terminal_commit.require_current_cleanup(
        "close-forward-recovery", target=Path(route_file), jobs=jobs)
    if cleanup_scope is not None and (
            not terminal_commit_id or terminal_commit_id != cleanup_scope.terminal_commit_id
            or owner_attempt_id != cleanup_scope.owner_attempt_id
            or not producer_binding_digest):
        return existing, False
    # What the false record names decides how strictly "now" must be read: a stored owner/commit is a
    # registered tuple, so the markers are observed per exact attempt, as the owner's own close does.
    exact = any(value is not None for value in (
        terminal_commit_id, owner_attempt_id, existing.get("terminal_commit_id"),
        existing.get("terminal_owner_attempt_id")))
    if exact and jobs is None:
        jobs = _compose_default_jobs()  # a close that names no registry reads the one this execution runs under
    gates = terminal_gate_observation(route, jobs=jobs, exact_terminal=exact)
    if terminal_gate_proven(gates) is not True:
        return existing, False
    if existing.get("inline_finish_id") is not None:
        # An inline finish's tuple is consumed by that same intent's finish, never by a caller naming none.
        if inline_finish_id is None:
            raise ValueError("route-close-outcome-conflict")
        if terminal_marker_digest is None:
            terminal_marker_digest = "sha256:" + hashlib.sha256(
                json.dumps(gates, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        identity_matches = _outcome_replay_matches(
            existing, route_id=route["route_id"], route_hash=route["route_hash"],
            terminal_commit_id=terminal_commit_id, owner_attempt_id=owner_attempt_id,
            producer_binding_digest=producer_binding_digest,
            terminal_marker_digest=terminal_marker_digest, inline_finish_id=inline_finish_id,
            summary_digest=summary_digest, inline_commit=inline_commit)
    else:
        registered = ("terminal_commit_id", "terminal_owner_attempt_id", "producer_binding_digest")
        if inline_finish_id is not None and any(existing.get(key) is not None for key in registered):
            raise ValueError("route-close-outcome-conflict")  # a registered owner's record is not an inline finish's
        # The caller names the tuple it holds, or none: either way it is compared with the tuple the
        # runtime holds now for the recorded (or named) owner, never filled in from the history.
        owner = owner_attempt_id if owner_attempt_id is not None else existing.get("terminal_owner_attempt_id")
        current = _current_terminal_identity(route, gates, owner) if owner is not None else {}
        if owner is not None and "terminal_owner_attempt_id" not in current:
            raise ValueError("route-close-outcome-conflict")  # not the owner of the marker observed now
        supplied = {
            "terminal_commit_id": terminal_commit_id,
            "terminal_owner_attempt_id": owner_attempt_id,
            "producer_binding_digest": producer_binding_digest,
            "terminal_marker_digest": terminal_marker_digest,
        }
        effective = {}
        for key, value in supplied.items():
            if value is not None and current.get(key) is not None and value != current[key]:
                raise ValueError("route-close-outcome-conflict")
            effective[key] = value if value is not None else current.get(key)
        identity_matches = all(existing.get(key) is None or existing.get(key) == effective[key]
                               for key in supplied)
        if existing.get("summary_digest") is not None:
            identity_matches = identity_matches and existing.get("summary_digest") == summary_digest
        terminal_marker_digest = effective["terminal_marker_digest"] or "sha256:" + hashlib.sha256(
            json.dumps(gates, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if not identity_matches:
        raise ValueError("route-close-outcome-conflict")
    from datetime import datetime, timezone
    promoted = dict(existing)
    promoted.update(terminal_gate_proven=True, terminal_gates=gates,
                    terminal_gate_promoted_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    historical_false_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                    terminal_marker_digest=terminal_marker_digest)
    if terminal_commit_id is not None:
        promoted["terminal_commit_id"] = terminal_commit_id
    if owner_attempt_id is not None:
        promoted["terminal_owner_attempt_id"] = owner_attempt_id
    if producer_binding_digest is not None:
        promoted["producer_binding_digest"] = producer_binding_digest
    if inline_finish_id is not None:
        promoted.update(inline_finish_id=inline_finish_id, summary_digest=summary_digest, head_commit=inline_commit)
    target = outcome_path(route_file)
    retained = target.with_name(f"{Path(route_file).stem}.historical-false-{hashlib.sha256(raw).hexdigest()[:16]}.outcome.json")
    with _exclusive_lock(target.with_name(f".{Path(route_file).stem}.outcome.lock")):
        current_raw = target.read_bytes()
        if current_raw != raw:
            current = json.loads(current_raw.decode("utf-8"))
            if (current.get("historical_false_sha256") == "sha256:" + hashlib.sha256(raw).hexdigest()
                    and current.get("terminal_gate_proven") is True
                    and _outcome_replay_matches(
                        current, route_id=route["route_id"], route_hash=route["route_hash"],
                        terminal_commit_id=terminal_commit_id, owner_attempt_id=owner_attempt_id,
                        producer_binding_digest=producer_binding_digest,
                        terminal_marker_digest=terminal_marker_digest, inline_finish_id=inline_finish_id,
                        summary_digest=summary_digest, inline_commit=inline_commit)):
                return current, False
            raise ValueError("route-close-outcome-conflict")
        from workflow_state import WorkflowLedger
        try:
            workflow = WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).read_only_state()
        except (OSError, ValueError):
            return existing, False
        if workflow["workflow_state"] == "CANCELLED" or workflow["journal_unreadable"]:
            return existing, False
        # The retained copy is created complete or not at all (temp + link), so an
        # interrupted promotion never leaves a partial file that would turn the
        # normal retry into a permanent conflict. A short prefix of the same
        # bytes left by an older interrupted write is completed in place.
        kept = retained.read_bytes() if retained.exists() else None
        if kept is not None and kept != raw and not (len(kept) < len(raw) and raw.startswith(kept)):
            raise ValueError("route-close-history-conflict")
        if kept != raw:
            fd, temporary = tempfile.mkstemp(prefix=".historical-false-", dir=retained.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                if kept is None:
                    os.link(temporary, retained)
                else:
                    os.replace(temporary, retained)
                dfd = os.open(str(retained.parent), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        atomic_write(target, promoted)
    return promoted, True

def close_route(route, route_file, commit=None, summary=None, publication=None,
                allow_unproven=True, jobs=None, expected_terminal_marker_digest=None,
                terminal_commit_id=None, expected_owner_attempt_id=None,
                expected_producer_binding_digest=None, inline_finish_id=None,
                expected_summary_digest=None, inline_commit=None, autoclose=None):
    import route_parent_close
    if route_parent_close.intent(route, jobs):
        raise ValueError("cancelled-by-parent")
    try:
        import inline_finish
        pending=inline_finish.pending_state(Path(route["artifact_root"]),route["route_id"])
    except (OSError,ValueError) as exc:
        raise ValueError("finish-state-unreadable") from exc
    if pending and pending.get("state")!="finished" and (
            pending.get("inline_finish_id")!=inline_finish_id or not inline_finish_id):
        raise ValueError("finish-in-progress")
    from datetime import datetime, timezone
    cleanup_scope = dispatch_terminal_commit.require_current_cleanup(
        "close-forward-recovery", target=Path(route_file), jobs=jobs)
    if cleanup_scope is not None and (
            not terminal_commit_id or terminal_commit_id != cleanup_scope.terminal_commit_id
            or expected_owner_attempt_id != cleanup_scope.owner_attempt_id
            or not expected_producer_binding_digest or allow_unproven):
        raise ValueError("cleanup-scope-exact-terminal-close-required")
    # F7: D-2's single-storage-location contract has a compile-time entrance gate
    # (`route-output-outside-canonical`) but had no exit gate -- `close` would
    # happily write a sidecar next to a route file living anywhere at all. The
    # four legacy locations stay closeable read-only (that's how open records
    # left over from before D-2 get resolved); everywhere else is rejected.
    location=classify_route_location(route_file,route["artifact_root"])
    if location != "canonical" and location not in _LEGACY_LOCATIONS:
        # A copy of the route kept elsewhere closes the one record of the same route (same id and
        # hash) under this artifact root; a route with no such record would write into another root.
        canonical=canonical_route_path(route["artifact_root"],route["route_id"])
        try:
            same=(canonical.is_file() and not canonical.is_symlink()
                  and json.loads(canonical.read_text(encoding="utf-8")).get("route_hash")==route["route_hash"])
        except (OSError,ValueError):
            same=False
        if not same:
            raise ValueError("route-close-outside-canonical-or-legacy")
        route_file,location=canonical,"canonical"
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
        raw = target.read_bytes()
        existing=json.loads(raw.decode("utf-8"))
        if existing.get("terminal_gate_proven") is False:
            promoted, changed = _promote_historical_false_outcome(
                route, route_file, existing, raw, jobs=jobs,
                terminal_commit_id=terminal_commit_id, owner_attempt_id=expected_owner_attempt_id,
                producer_binding_digest=expected_producer_binding_digest,
                terminal_marker_digest=expected_terminal_marker_digest,
                inline_finish_id=inline_finish_id, summary_digest=expected_summary_digest,
                inline_commit=inline_commit)
            if changed:
                return promoted, True
            if promoted is not existing and promoted.get("terminal_gate_proven") is True:
                return promoted, False  # a concurrent close already consumed it; report the current record
            if all(value is None for value in (terminal_commit_id, expected_owner_attempt_id,
                    expected_producer_binding_digest, expected_terminal_marker_digest, inline_finish_id)):
                # A caller that names no identity claims none: the record is not consumed (the terminal gate
                # is not proven yet, or the tuple it names is not for this caller) and nothing foreign is taken.
                return existing, False
        if not _outcome_replay_matches(existing, route_id=route["route_id"], route_hash=route["route_hash"],
                terminal_commit_id=terminal_commit_id, owner_attempt_id=expected_owner_attempt_id,
                producer_binding_digest=expected_producer_binding_digest,
                terminal_marker_digest=expected_terminal_marker_digest,
                inline_finish_id=inline_finish_id, summary_digest=expected_summary_digest,
                inline_commit=inline_commit):
            raise ValueError("route-close-outcome-conflict")
        return existing, False
    # Live, not stored: every close computes gate truth fresh from the completion
    # markers on disk. A route closed once is never retroactively reopened to
    # recompute this, so the sidecar's `terminal_gate_proven` reflects gate state at
    # the moment of THIS close, not at any later inspection.
    gates=terminal_gate_observation(route, jobs=jobs, exact_terminal=terminal_commit_id is not None)
    # C-25c: a `close` that lands before the terminal node's `complete` used to
    # seal `terminal_gate_proven=false` permanently -- finalize could never
    # prove the gate afterward even once `complete` actually ran. Refuse the
    # close instead of writing that sidecar, unless the caller explicitly opts
    # into recording the false proof (e.g. an intentionally abandoned route).
    if terminal_gate_proven(gates) is False and not allow_unproven:
        raise ValueError("route-close-before-complete")
    outcome={"schema_version":4 if publication is not None else OUTCOME_SCHEMA_VERSION,
             "route_id":route["route_id"],"route_hash":route["route_hash"],
             "route_file":str(Path(route_file).resolve()),"cwd":route["cwd"],
             "capability":route["capability"],"effective_intensity":route["effective_intensity"],
             "closed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
             "head_commit":(None if inline_finish_id is not None and
                            not _COMMIT_SHA.fullmatch(str(route.get("source_commit") or ""))
                            else commit or _head_commit(route["cwd"])),"summary":summary,
             "registry_current":route.get("_registry_current",True),
             "route_location":classify_route_location(route_file,route["artifact_root"]),
             "terminal_gate_proven":terminal_gate_proven(gates),"terminal_gates":gates}
    if expected_terminal_marker_digest is not None:
        outcome["terminal_marker_digest"]=expected_terminal_marker_digest
    if terminal_commit_id is not None:
        outcome["terminal_commit_id"] = terminal_commit_id
    if expected_owner_attempt_id is not None:
        outcome["terminal_owner_attempt_id"] = expected_owner_attempt_id
    if expected_producer_binding_digest is not None:
        outcome["producer_binding_digest"] = expected_producer_binding_digest
    if inline_finish_id is not None:
        outcome["inline_finish_id"] = inline_finish_id
        outcome["summary_digest"] = expected_summary_digest
    # Closed by the runtime because nobody works on the route any more
    # (utilities/route_autoclose.py), not by the session that composed it.
    if autoclose is not None:
        outcome["autoclose"] = dict(autoclose)
    # A-SD154-7: a route's closed outcome names every SD-154 revision recorded
    # under it, so a reader never has to walk completion-dir history by hand
    # to learn a gate's evidence was corrected mid-route.
    revisions = _route_revisions(route, jobs=jobs)
    if revisions:
        outcome["revisions"] = revisions
    if not allow_unproven and outcome["terminal_gate_proven"] is not True:
        raise ValueError("route-close-before-complete")
    if publication is not None: outcome["publication"]=publication
    releases=_gate_releases(route_file)
    if releases: outcome["gate_releases"]=releases
    reviews=review_independence_observation(route)
    if reviews:
        outcome["review_independence"]=reviews
        # `owner-overridden` belongs here with `degraded`: both mean "this gate
        # was not closed by an independent reviewer's PASS", and the §0.5 card
        # rule reads this one list.
        degraded=sorted(
            node_id for node_id,row in reviews.items()
            if row.get("review_independence") in ("degraded","owner-overridden")
        )
        if degraded: outcome["review_independence_degraded"]=degraded
    # Outcome creation is write-once.  A concurrent creator is replayed only
    # after the same identity/gate checks above; it is never overwritten.
    data=json.dumps(outcome,indent=2,ensure_ascii=False)+"\n"
    target.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".outcome-", dir=target.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as fh:
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing=json.loads(target.read_text(encoding="utf-8"))
            if not _outcome_replay_matches(existing, route_id=route["route_id"], route_hash=route["route_hash"],
                    terminal_commit_id=terminal_commit_id, owner_attempt_id=expected_owner_attempt_id,
                    producer_binding_digest=expected_producer_binding_digest,
                    terminal_marker_digest=expected_terminal_marker_digest,
                    inline_finish_id=inline_finish_id, summary_digest=expected_summary_digest,
                    inline_commit=inline_commit):
                raise ValueError("route-close-outcome-conflict")
            return existing, False
        dfd=os.open(str(target.parent),os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.unlink(temporary)
    try:
        # C-W4: the campaign's status page, when it keeps one, records this close (ARTIFACT_META).
        import status_board
        status_board.note_route_closed(route, outcome)
    except Exception:  # noqa: BLE001 -- the page is optional; a close never waits on it
        pass
    return outcome, True

def review_independence_observation(route):
    """Per review-class node: who produced the verdict, read live from the markers.

    SD-OPEN-41(b) requirement (2) -- an owner-inline review does not block the
    route, but the route's own closed outcome has to say the gate was not
    independently reviewed, so a later reader is never left inferring
    independence from the fact that the route closed.

    Computed at close time from the markers on disk, exactly like
    `terminal_gate_observation`: a route closed once is never reopened to
    recompute it. A review node completed before this field existed has no
    provenance in its marker and is reported `unrecorded` rather than being
    guessed at.
    """

    rows={}
    for node in route.get("nodes",[]):
        if node.get("kind")!="review-worker":
            continue
        node_id=node.get("id")
        path=completion_dir(route["route_id"])/f"{node_id}.json"
        try:
            marker=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            rows[node_id]={"review_independence":"unrecorded","reason":"marker-unreadable"}
            continue
        if not isinstance(marker,dict) or not marker.get("review_independence"):
            rows[node_id]={"review_independence":"unrecorded","reason":"marker-predates-provenance"}
            continue
        row={
            "review_independence":marker["review_independence"],
            "reviewer_kind":marker.get("reviewer_kind"),
            "reviewer_identity":marker.get("reviewer_identity"),
        }
        if marker.get("reviewer_downgrade_reason"):
            row["reviewer_downgrade_reason"]=marker["reviewer_downgrade_reason"]
        if marker.get("review_gate_closure"):
            row["review_gate_closure"]=marker["review_gate_closure"]
        rows[node_id]=row
    return rows

def _gate_releases(route_file):
    """SD-123 (8)(d): fold the gate-release sidecar into the closed outcome.

    `workflow-supervisor.py` appends one row per release beside the route file.
    Without this fold nothing ever read that sidecar, so a headless owner's
    self-release was recorded in a file no consumer opened -- which is the same
    silence (d) exists to end. Additive and fail-soft: a missing or malformed
    sidecar leaves the key absent, exactly as before, and the workflow ledger
    stays the authoritative record of the transition itself.
    """
    path=Path(route_file); path=path.with_name(path.stem+".gate-release.json")
    try: data=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError): return []
    rows=data.get("gate_releases") if isinstance(data,dict) else None
    if not isinstance(rows,list): return []
    return [row for row in rows if isinstance(row,dict) and row.get("gate")]

# Canonical route record basename (`compile`/`compose` write `rt-<16 hex>.json`).
ROUTE_RECORD_BASENAME=re.compile(r"rt-[0-9a-f]{16}\.json")
# Typed sidecars that live beside a route record and are never route candidates
# (SD-OPEN-54, #15): `.outcome.json` (closure), `.gate-release.json` (the
# workflow-supervisor gate ledger), `.superseded-<stamp>.outcome.json`.
_ROUTE_SIDECAR_SUFFIXES=((".gate-release.json","gate-release"),(".outcome.json","outcome"))


def route_sidecar_kind(path):
    """`outcome` / `gate-release` when `path` is a typed route sidecar, else None."""
    name=Path(path).name
    for suffix,kind in _ROUTE_SIDECAR_SUFFIXES:
        if name.endswith(suffix): return kind
    return None


def route_status(artifact_root, *, diagnostics=None, open_only=False, only=None):
    """Report every compiled route under one artifact root and whether it is closed.

    `only` (a route file) narrows the report to that one record without reading the others.

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
    canonical=canonical_routes_dir(artifact_root)
    search_dirs=[canonical,root,root/"routes",root/"_routes",root/".routes"]
    by_route_id={}
    rows=[]
    only_path=os.path.realpath(only) if only is not None else None
    for search_dir in search_dirs:
        if not search_dir.is_dir(): continue
        with os.scandir(search_dir) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
        if only_path is not None:
            entries = [entry for entry in entries if entry.name == os.path.basename(only_path)
                       and os.path.realpath(entry.path) == only_path]
        closed_names = ({entry.name.removesuffix(".outcome.json") for entry in entries
                         if entry.name.endswith(".outcome.json") and entry.is_file()}
                        if open_only and diagnostics is None and search_dir == canonical else set())
        for entry in entries:
            if not entry.name.endswith(".json") or entry.name == ".route-children-index.json": continue
            path = search_dir / entry.name
            # SD-OPEN-54 (#15): typed sidecars beside a route record (`.outcome.json`,
            # `.gate-release.json` -- the workflow-supervisor gate ledger) are never
            # route candidates; the ledger used to be read as a route, fail
            # `route-malformed-missing-required-keys`, and turn every quiescence
            # observation of the root fail-closed (hearting rt-5d862a3d..., cairn W15d).
            if route_sidecar_kind(path) is not None: continue
            # Closed canonical records need no payload read for --open-only.
            # Retain their filename identity for duplicate-location reporting.
            if (ROUTE_RECORD_BASENAME.fullmatch(path.name) and path.stem in closed_names):
                by_route_id.setdefault(path.stem, []).append(str(path))
                continue
            # A canonical file that is not a route record by name (`rt-<16 hex>.json`)
            # may still be an alias route (drift, reported below); when it does not
            # parse as a route it is foreign evidence, not a malformed route, so its
            # diagnostic is non-blocking.
            record_named=(search_dir!=canonical) or bool(ROUTE_RECORD_BASENAME.fullmatch(path.name))
            try: raw=json.loads(path.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError,UnicodeDecodeError) as exc:
                if diagnostics is not None:
                    diagnostics.append({"path":str(path),
                                         "location":classify_route_location(path,artifact_root),
                                         "reason":f"route-unreadable:{exc}","blocking":record_named})
                continue
            if not isinstance(raw,dict) or "route_id" not in raw or "nodes" not in raw:
                if diagnostics is not None:
                    diagnostics.append({"path":str(path),
                                         "location":classify_route_location(path,artifact_root),
                                         "reason":("route-malformed-missing-required-keys" if record_named
                                                   else "route-candidate-foreign-basename"),
                                         "blocking":record_named})
                continue
            location=classify_route_location(path,artifact_root)
            target=outcome_path(path)
            row={"route_file":str(path),"route_id":raw.get("route_id"),
                 "capability":raw.get("capability"),"effective_intensity":raw.get("effective_intensity"),
                 "source_commit":raw.get("source_commit"),"closed":target.is_file(),
                 "location":location,"drift":location != "canonical",
                 "read_only":location in _LEGACY_LOCATIONS}
            finish_state=None
            try:
                import inline_finish
                finish_state=inline_finish.pending_state(Path(raw.get("artifact_root", "")), raw.get("route_id", ""))
                if finish_state and finish_state.get("state") != "finished":
                    row["finish_pending"]=True
                    row["finish_state"]=finish_state.get("state")
                    row["state"]="finish-pending"
            except (OSError, ValueError, TypeError):
                row["finish_pending"]=True
                row["state"]="finish-pending-unreadable"
            row["alias_basename"]=(location=="canonical" and not route_path_is_exact(
                path,artifact_root,row["route_id"]))
            row["drift"]=row["drift"] or row["alias_basename"]
            if row["closed"]:
                try: closure=json.loads(target.read_text(encoding="utf-8"))
                except (OSError,json.JSONDecodeError,UnicodeDecodeError): closure={}
                row["closed_at"]=closure.get("closed_at"); row["head_commit"]=closure.get("head_commit")
                if (finish_state and finish_state.get("updates")
                        and finish_state.get("inline_finish_id")==closure.get("inline_finish_id")):
                    # Review fixes recorded after the finish: the close keeps the finish's commit.
                    row["head_commit"]=inline_finish.recorded_head(finish_state)[0]
                    row["finish_updates"]=finish_state["updates"]
                row["stale_closure"]=closure.get("route_hash")!=raw.get("route_hash")
                row["registry_current"]=closure.get("registry_current",True)
                if (raw.get("route_plan") is not None and closure.get("terminal_gate_proven") is True
                        and not closure.get("autoclose") and not row["stale_closure"]):
                    import route_plan as RP
                    next_leg=RP.next_leg_for_route(raw)
                    if next_leg is not None:
                        row["next_leg"]=next_leg
            rows.append(row)
            by_route_id.setdefault(row["route_id"],[]).append(row["route_file"])
    for row in rows:
        locations=by_route_id.get(row["route_id"],[])
        if len(locations) > 1: row["duplicate_locations"]=sorted(locations)
    rows.sort(key=lambda row:(_LOCATION_SORT_PRIORITY.get(row["location"],9),row["route_file"]))
    return [row for row in rows if not row["closed"]] if open_only else rows
