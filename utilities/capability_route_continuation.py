"""Capability route: continuation routes and source lineage.

Source lineage, the continuation contract hash, reused evidence and its
boundaries, the verified ancestor plan refresh, partial-group continuation,
and verifying, publishing and binding a continuation route.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from hearting_gates import note_same_work_drift
import route_identity as ROUTE_IDENTITY
import owner_write_advisory as OWNER_WRITE_ADVISORY
from dispatch_contract import (
    DispatchContractError, PARENT_TRANSPORT_BY_DISPATCH_DEPTH, WRAPPER_PARENT_SANDBOXES,
    attempt_process_quiescence, completion_marker_is_current, completion_attempt_readiness,
    completion_conflict_attempt, owner_closure_shape, evidence_currency, note_evidence_change,
    gate_currency, completion_superseded, parse_registry_metadata, resolve_agent_home,
    resolve_completed_alias, resolve_dangling_registry, resolve_dispatch_state_root,
    stable_state_root,
)
from codex_dispatch_terminal import inspect_terminal_attempt
from replica_batch_contract import verify_manifest as verify_batch_manifest

from capability_route_base import (
    _attempt_completion_path, _exclusive_lock, _validate_output_scopes, _verify_selection_pins,
    canonical, classify_route_location, completion_dir, CONTINUATION_CONTRACT_VERSION,
    CONTINUATION_SOURCE_COMMIT_REBIND_VERSION, EVIDENCE_CONSUMER_DISPATCH_DEPTH, GATE_FIELDS,
    LAUNCH_COMPATIBILITY_TUPLE_VERSION, route_path_is_exact, RouteLineageError, TOPO, TRACKING,
    verified_route_lineage, write_once,
)


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

def _inside_git_worktree(path):
    """Is this path inside a git worktree? Ask git, not the filesystem.

    `(cwd/".git").exists()` answers only for a repository *root*: a route whose
    cwd is any subdirectory has no `.git` entry, so the lineage recheck below
    silently skipped exactly the cases it was written for (review round 2, S3).
    A missing directory, a non-repository, or an unavailable git all read as
    "cannot answer here", and the probe is skipped rather than guessed at -- the
    launch guard still proves the same lineage against real HEAD before anything
    dispatches.
    """
    try:
        proc=subprocess.run(
            ["git","-C",str(path),"rev-parse","--is-inside-work-tree"],
            text=True,capture_output=True,timeout=30,
        )
    except (OSError,subprocess.SubprocessError):
        return False
    return proc.returncode==0 and proc.stdout.strip()=="true"

SOURCE_LINEAGE_GIT_TIMEOUT=30

class SourceLineage(NamedTuple):
    """One first-parent lineage verdict against a worktree's live HEAD.

    `kind` is `"exact"` (HEAD is the sealed commit), `"descendant"` (HEAD is a
    first-parent descendant of it -- the mutation-worktree, mid-cycle-progress
    shape SD-67/SD-107 named), `"diverged"` (HEAD is neither), or
    `"unverifiable"` (the question could not be asked at all; `reason` names
    why: `git-timeout`, `git-failed`, `unsafe-git-operation`,
    `unsafe-git-state`, `not-a-repo`). `commits[0]` is the observed HEAD
    whenever `kind` is `"descendant"` or `"diverged"`; `distance` counts the
    commits ahead of the sealed one along that line.
    """
    kind: str
    distance: "int | None"
    commits: tuple
    branch: "str | None"
    reason: "str | None"

def _source_lineage_unverifiable(reason):
    return SourceLineage("unverifiable",None,(),None,reason)

def source_lineage_verdict(cwd, sealed_commit, *, timeout=SOURCE_LINEAGE_GIT_TIMEOUT):
    """The one first-parent lineage probe every SD-156 consumer shares.

    `worker-route-guard.py`'s mutation gate, the session hook's staleness
    check, and `_grounding_cwd_lineage_ok`'s sealed-vs-fresh grounding compare
    each asked "is the sealed commit an ancestor of the live worktree HEAD"
    with their own copy of the same git calls -- three places to forget a
    timeout, and three places that could (and once did, defect C) disagree
    about what counts as an answer. This is the one probe: `rev-parse
    --git-dir` (repo existence), `rev-list --first-parent HEAD` (the lineage
    itself, whose first line is the observed commit), and -- only when HEAD is
    not the sealed commit itself -- an in-progress-operation check via the
    git-dir's sidecar files and `rev-parse --abbrev-ref HEAD` (branch;
    detached HEAD reports the literal string `"HEAD"`).

    The safety checks run only for a HEAD that differs from the sealed commit:
    accepting drift as legitimate forward progress is the risky question a
    mid-merge or detached checkout should block, not observing that nothing
    moved at all -- a linked worktree checked out detached at its own sealed
    commit (a real, supported shape; `git worktree add <dir> HEAD` produces
    exactly this) must still verify `exact`.
    """
    cwd=Path(cwd)
    def probe(*args):
        return subprocess.run(["git","-C",str(cwd),*args],text=True,capture_output=True,timeout=timeout)
    try:
        git_dir_probe=probe("rev-parse","--git-dir")
    except subprocess.TimeoutExpired:
        return _source_lineage_unverifiable("git-timeout")
    except (OSError,subprocess.SubprocessError):
        return _source_lineage_unverifiable("git-failed")
    if git_dir_probe.returncode != 0:
        return _source_lineage_unverifiable("not-a-repo")
    git_dir=Path(git_dir_probe.stdout.strip())
    if not git_dir.is_absolute():
        git_dir=cwd/git_dir
    try:
        list_probe=probe("rev-list","--first-parent","HEAD")
    except subprocess.TimeoutExpired:
        return _source_lineage_unverifiable("git-timeout")
    except (OSError,subprocess.SubprocessError):
        return _source_lineage_unverifiable("git-failed")
    if list_probe.returncode != 0:
        return _source_lineage_unverifiable("git-failed")
    commits=list_probe.stdout.split()
    if not commits:
        return _source_lineage_unverifiable("git-failed")
    if sealed_commit == commits[0]:
        return SourceLineage("exact",0,(),None,None)
    if (
        (git_dir/"MERGE_HEAD").exists()
        or (git_dir/"rebase-merge").exists() or (git_dir/"rebase-apply").exists()
        or (git_dir/"CHERRY_PICK_HEAD").exists()
    ):
        return _source_lineage_unverifiable("unsafe-git-operation")
    try:
        branch_probe=probe("rev-parse","--abbrev-ref","HEAD")
    except subprocess.TimeoutExpired:
        return _source_lineage_unverifiable("git-timeout")
    except (OSError,subprocess.SubprocessError):
        return _source_lineage_unverifiable("git-failed")
    if branch_probe.returncode != 0:
        return _source_lineage_unverifiable("git-failed")
    branch=branch_probe.stdout.strip()
    if branch == "HEAD":
        return _source_lineage_unverifiable("unsafe-git-state")
    if sealed_commit in commits:
        distance=commits.index(sealed_commit)
        return SourceLineage("descendant",distance,tuple(commits[:distance]),branch,None)
    return SourceLineage("diverged",None,tuple(commits),branch,None)

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
    return source_lineage_verdict(path,sealed).kind == "descendant"

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
    import review_input
    identified_input = False
    if review_input.is_plan_review_node(node):
        if review_input.has_plan_producer(route, node):
            identified_input = (directory / "plan.json").is_file()
        else:
            try:
                identified_input = review_input.sealed_plan_input(route, node) is not None
            except DispatchContractError:
                pass
        if identified_input:
            try:
                identified_input = review_input.resolve_input(route, node, jobs) is not None
            except DispatchContractError:
                identified_input = False
    if (identified_input
            and not isinstance(marker.get("source_subject"), dict)
            and marker.get("review_gate_closure") != "owner-closure"):
        # Historical marker validity does not prove a continuation's reviewed
        # input. Reuse needs the recorded subject; do not infer it from today.
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:completion-review-subject-unknown"
        )
    if not completion_marker_is_current(route,node,marker_path,marker,jobs=jobs):
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:attempt-link"
        )
    from dispatch_contract import resource_completion_reuse_proof
    try:
        resource_proof = resource_completion_reuse_proof(route, node, marker_path, marker, jobs)
    except DispatchContractError as exc:
        raise ValueError(f"continuation-source-node-unverified:{node_id}:{exc.reason}") from exc
    attempt_id=marker.get("attempt_id")
    link={}; link_bytes=b""
    if resource_proof is None:
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
    quiescence_digest=(_sha256_record({"resource":resource_proof,"marker_history_digest":history_digest}) if resource_proof is not None else (
        link.get("quiescence_proof_digest")
        or marker.get("quiescence_proof_digest")
        or _sha256_record({
            "attempt_id":attempt_id,
            "attempt_sidecar_digest":"sha256:"+hashlib.sha256(link_bytes).hexdigest(),
            "marker_history_digest":history_digest,
            "terminal_marker_current":True,
        })
    ))
    if not isinstance(quiescence_digest,str) or not quiescence_digest:
        raise ValueError(
            f"continuation-source-node-unverified:{node_id}:quiescence-proof"
        )
    evidence=marker.get("evidence") or {}
    currency = evidence_currency(route, node, marker_path, marker)
    evidence = dict(evidence, sha256=currency.evidence_digest)
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
        **({key: resource_proof[key] for key in ("resource_execution_identity", "resource_sha256")}
           if resource_proof is not None else {}),
    }
    last_turn_id=(
        link.get("last_turn_id") or link.get("lastTurnId")
        or marker.get("last_turn_id") or marker.get("lastTurnId")
    )
    return public,(str(last_turn_id) if last_turn_id else None)

def _source_evidence_prefix(route, node_ids):
    by_id={str(node.get("id")):node for node in route.get("nodes",[])}
    rows=[]; turns={}
    for node_id in node_ids:
        node=by_id.get(str(node_id))
        if node is None:
            return rows,_sha256_record(rows),turns,f"continuation-source-node-unknown:{node_id}",str(node_id)
        try:
            row,last_turn_id=_continuation_reused_evidence(route,node)
        except ValueError as exc:
            return rows,_sha256_record(rows),turns,str(exc),str(node_id)
        rows.append(row)
        if last_turn_id:
            turns[str(node_id)]=last_turn_id
    return rows,_sha256_record(rows),turns,None,None

def _source_evidence_snapshot(route, node_ids):
    rows,digest,turns,error,_failed_node=_source_evidence_prefix(route,node_ids)
    if error:
        raise ValueError(error)
    return rows,digest,turns

def _continuation_superseded_boundary(route, node_id, error):
    """The node whose superseded PASS the continuation reruns instead of reusing.

    A source- or input-bound PASS that is stale or unbound, or whose upstream completed
    again, is not reusable evidence (`completion_superseded`, the one rule); the prefix is
    cut at that node and its old marker stays history. A closure marker (an owner ruling
    over a FAIL), an integrity break and an unreadable marker are not that case.
    """
    prefix=f"continuation-source-node-unverified:{node_id}:"
    if not error.startswith(prefix):
        return None
    node=next((row for row in route.get("nodes",[]) if str(row.get("id"))==str(node_id)),None)
    if node is None:
        return None
    try:
        jobs=_continuation_source_jobs(route)
        path=completion_dir(route["route_id"],jobs=jobs)/f"{node_id}.json"
        marker=json.loads(path.read_text(encoding="utf-8"))
        if owner_closure_shape(marker) is not None:
            return None
        currency=gate_currency(route,node,path,marker,observe=True,jobs=jobs)
    except (OSError,ValueError,DispatchContractError):
        return None
    return str(node_id) if completion_superseded(currency) else None

def _terminal_owner_for_revised_ancestor(route, jobs):
    """The ended owner of the continuation being superseded, not a review verdict."""
    from owner_route_binding import resolve_owner_route_lifecycle
    matches=[]
    for line in Path(jobs).read_text(encoding="utf-8").splitlines():
        fields=line.split("\t")
        if len(fields)!=6:
            continue
        meta=parse_registry_metadata(fields[5])
        if (meta.get("worker_type")=="owner"
                and meta.get("owner_route_id")==route["route_id"]
                and meta.get("owner_route_hash")==route["route_hash"]):
            matches.append((fields,meta))
    if len(matches)!=1:
        raise ValueError("ancestor-revision-source-owner-not-exact")
    fields,meta=matches[0]
    if (fields[1]!="done" or meta.get("dispatch_depth")!="1"
            or meta.get("registered_worker")!="1"
            or meta.get("terminal_event")!="turn.completed"):
        raise ValueError("ancestor-revision-source-owner-not-terminal")
    binding,_=resolve_owner_route_lifecycle(jobs,owner_attempt_id=meta["attempt_id"])
    if (binding is None or (binding.route_id,binding.route_hash)
            != (route["route_id"],route["route_hash"])):
        raise ValueError("ancestor-revision-source-owner-binding-mismatch")
    if attempt_process_quiescence(meta,terminal_receipt=True).state!="quiescent":
        raise ValueError("ancestor-revision-source-owner-not-quiescent")
    terminal=inspect_terminal_attempt(
        meta.get("log_file"),worktree=route["cwd"],
        artifact_root_metadata=route["artifact_root"],worker_type="owner",
        artifact_binding=meta,
    )
    if (terminal.get("state")!="valid" or terminal.get("verdict") not in {"FAIL","BLOCKED"}
            or terminal.get("artifact_state")!="readable"
            or terminal.get("failure_class")!=meta.get("failure_class")
            or terminal.get("failure_note")!=meta.get("note")):
        raise ValueError("ancestor-revision-source-owner-terminal-unverified")
    return meta["attempt_id"]

def _verified_ancestor_plan_refresh(source_route, *, expected=None):
    """Derive one revised plan reuse from canonical history; never amend an old route."""
    if (source_route.get("continuation_contract_version")!=CONTINUATION_CONTRACT_VERSION
            or source_route.get("resume_from_node")!="plan-check"):
        return None
    old_rows=source_route.get("reused_nodes") or []
    plan_rows=[row for row in old_rows if isinstance(row,dict) and row.get("node_id")=="plan"]
    if len(plan_rows)!=1 or old_rows[-1].get("node_id")!="plan":
        return None
    if (not source_route.get("nodes")
            or source_route["nodes"][0].get("id")!="plan-check"):
        raise ValueError("ancestor-revision-review-boundary-invalid")
    if any(node.get("unit")=="qa/plan-review" and node.get("kind")=="review-worker"
           and node.get("id")!="plan-check" for node in source_route["nodes"]):
        raise ValueError("ancestor-revision-review-boundary-invalid")
    review=source_route["nodes"][0]
    dependencies=review.get("reused_dependencies") or []
    if ("plan" not in (review.get("source_depends_on") or [])
            or len(dependencies)!=1 or dependencies[0].get("node_id")!="plan"
            or dependencies[0].get("marker_digest")!=plan_rows[0].get("marker_digest")):
        raise ValueError("ancestor-revision-review-dependency-invalid")
    try:
        lineage=verified_route_lineage(source_route,artifact_root=source_route["artifact_root"])
    except RouteLineageError as exc:
        raise ValueError(exc.code) from exc
    if len(lineage)!=2:
        return None
    ancestor=lineage[1]
    plan=next((n for n in ancestor["nodes"] if n.get("id")=="plan"),None)
    if plan is None:
        raise ValueError("ancestor-revision-plan-absent")
    other_rows,_digest,_turns=_source_evidence_snapshot(
        ancestor,[row["node_id"] for row in old_rows[:-1]])
    if canonical(other_rows)!=canonical(old_rows[:-1]):
        raise ValueError("ancestor-revision-other-reuse-drift")
    current,_turn=_continuation_reused_evidence(ancestor,plan)
    old=plan_rows[0]
    if current["marker_digest"]==old.get("marker_digest"):
        return None
    jobs=_continuation_source_jobs(ancestor)
    directory=completion_dir(ancestor["route_id"],jobs=jobs)
    marker_path=directory/"plan.json"
    marker=json.loads(marker_path.read_text(encoding="utf-8"))
    revision=marker.get("revision") or {}
    old_sequence=revision.get("of_sequence")
    if not isinstance(old_sequence,int) or old_sequence<1 or marker.get("sequence")!=old_sequence+1:
        raise ValueError("ancestor-revision-history-gap")
    old_path=directory/f"plan.{old_sequence}.json"
    new_path=directory/f"plan.{marker['sequence']}.json"
    for path in (marker_path,old_path,new_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("ancestor-revision-history-invalid")
    old_bytes=old_path.read_bytes()
    new_bytes=new_path.read_bytes()
    old_marker=json.loads(old_bytes)
    old_digest="sha256:"+hashlib.sha256(old_bytes).hexdigest()
    new_digest="sha256:"+hashlib.sha256(new_bytes).hexdigest()
    link_path=_attempt_completion_path(
        ancestor,"plan",old.get("terminal_attempt_id"),jobs=jobs)
    link_bytes=link_path.read_bytes()
    link=json.loads(link_bytes)
    old_quiescence=(link.get("quiescence_proof_digest")
        or old_marker.get("quiescence_proof_digest")
        or _sha256_record({
            "attempt_id":old.get("terminal_attempt_id"),
            "attempt_sidecar_digest":"sha256:"+hashlib.sha256(link_bytes).hexdigest(),
            "marker_history_digest":old_digest,"terminal_marker_current":True,
        }))
    if (new_bytes!=marker_path.read_bytes() or old_digest!=old.get("marker_digest")
            or new_digest!=current["marker_digest"]
            or revision.get("of_marker_sha256")!=old_digest.split(":",1)[1]
            or revision.get("of_evidence_sha256")!=(old_marker.get("evidence") or {}).get("sha256")
            or revision.get("evidence_sha256")!=(marker.get("evidence") or {}).get("sha256")
            or marker.get("stage_authority")!="revision"
            or old_marker.get("sequence")!=old_sequence
            or old_marker.get("route_id")!=ancestor["route_id"]
            or marker.get("route_id")!=ancestor["route_id"]
            or old_marker.get("route_hash")!=ancestor["route_hash"]
            or marker.get("route_hash")!=ancestor["route_hash"]
            or old_marker.get("node_id")!="plan" or marker.get("node_id")!="plan"
            or old.get("marker_path")!=str(marker_path.resolve(strict=False))
            or old_marker.get("attempt_id")!=old.get("terminal_attempt_id")
            or marker.get("attempt_id")!=old.get("terminal_attempt_id")
            or (old_marker.get("evidence") or {}).get("sha256")!=old.get("output_evidence_digest")
            or old.get("contract_hash")!=current.get("contract_hash")
            or old.get("completion_gate")!=current.get("completion_gate")
            or old.get("quiescence_proof_digest")!=old_quiescence
            or old.get("verdict")!="PASS" or current.get("verdict")!="PASS"
            or source_route.get("source_evidence_digest")!=_sha256_record(old_rows)):
        raise ValueError("ancestor-revision-history-mismatch")
    author=_terminal_owner_for_revised_ancestor(source_route,jobs)
    if revision.get("author_attempt_id")!=author or revision.get("basis")!="owner-correction":
        raise ValueError("ancestor-revision-author-unverified")
    proof={
        "version":1,"source_route_id":source_route["route_id"],
        "ancestor_route_id":ancestor["route_id"],"ancestor_route_hash":ancestor["route_hash"],
        "node_id":"plan","old_marker_digest":old_digest,
        "current_marker_digest":new_digest,
        "current_evidence":dict(marker["evidence"]),
        "terminal_attempt_id":current["terminal_attempt_id"],
        "revision_author_attempt_id":author,
    }
    if expected is not None and proof!=expected:
        raise ValueError("ancestor-revision-refresh-drift")
    return proof

def verified_ancestor_plan_refresh(route):
    """Reprove a successor's sealed refresh against live marker and terminal history."""
    proof=route.get("ancestor_plan_refresh")
    if proof is None:
        return None
    try:
        lineage=verified_route_lineage(route,artifact_root=route["artifact_root"])
    except RouteLineageError as exc:
        raise ValueError(exc.code) from exc
    source=next((row for row in lineage[1:] if row["route_id"]==proof.get("source_route_id")),None)
    if source is None:
        raise ValueError("ancestor-revision-source-route-mismatch")
    if _verified_ancestor_plan_refresh(source,expected=proof) is None:
        raise ValueError("ancestor-revision-refresh-absent")
    return proof

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
        note_same_work_drift("continuation-last-turn-mismatch")
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
        note_same_work_drift("continuation-fork-source-mismatch")
    if not expected_turn or selected_turn != expected_turn:
        note_same_work_drift("continuation-last-turn-mismatch")
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

def _continuation_release_authority(raise_entry, release_entry, binding):
    """Return authority while conservatively interpreting legacy interviews."""
    raise_evidence=raise_entry.get("evidence") if isinstance(raise_entry,dict) else {}
    raise_evidence=raise_evidence if isinstance(raise_evidence,dict) else {}
    release_evidence=release_entry.get("evidence") if isinstance(release_entry,dict) else {}
    release_evidence=release_evidence if isinstance(release_evidence,dict) else {}
    legacy_interview=bool(raise_evidence.get("interview") or
                          raise_evidence.get("questions"))
    authority=(release_evidence.get("release_authority") or
               binding.get("release_authority") or
               ("depth-0" if legacy_interview else "any"))
    if authority != "any" and (
            release_evidence.get("actor_kind") != "user"
            or not isinstance(release_evidence.get("released_by"), str)
            or not release_evidence.get("released_by", "").strip()):
        raise ValueError("continuation-human-gate-release-unauthorized:"+
                         str(raise_evidence.get("gate") or "unknown"))
    return authority

def _continuation_gate_release_proof(source_route,gate):
    """Seal the exact source raise/release pair before dropping a runtime gate."""
    import workflow_state as WS

    jobs=((source_route.get("launch_compatibility_tuple") or {})
          .get("jobs_path") or {}).get("path")
    if not isinstance(jobs,str) or not Path(jobs).is_absolute():
        raise ValueError("continuation-human-gate-release-unproven:"+str(gate))
    ledger=WS.WorkflowLedger(
        str(source_route.get("route_id") or ""),
        str(source_route.get("route_hash") or ""),jobs=jobs,
    )
    epoch=0; raised=None; released=None
    for entry in ledger.journal():
        evidence=entry.get("evidence") if isinstance(entry,dict) else None
        evidence=evidence if isinstance(evidence,dict) else {}
        if entry.get("workflow_state") == "BLOCKED_HUMAN_GATE" \
                and evidence.get("gate") == gate:
            epoch+=1; raised=entry; released=None
            continue
        if raised is None or evidence.get("released_gate") != gate:
            continue
        # Resolve the latest epoch through the shared workflow-state reader.
        # A CANCELLED entry intentionally has no decision and must never be
        # interpreted as the legacy proceed release.
        resolution = WS.human_gate_resolution(ledger.journal(), gate)
        if resolution.get("epoch") != epoch:
            continue
        if resolution.get("status") in {"proceed", "revise"}:
            released=entry
    if raised is None or released is None:
        raise ValueError("continuation-human-gate-release-unproven:"+str(gate))
    resolution = WS.human_gate_resolution(ledger.journal(), gate)
    if resolution.get("status") != "proceed" or resolution.get("epoch") != epoch:
        raise ValueError("continuation-human-gate-release-unproven:"+str(gate))
    evidence=released.get("evidence") or {}
    decision=evidence.get("decision") or "proceed"
    if decision != "proceed":
        raise ValueError("continuation-human-gate-release-unproven:"+str(gate))
    binding = next((item for item in source_route.get("human_gate_bindings", [])
                    if isinstance(item, dict) and item.get("gate") == gate), {})
    authority = _continuation_release_authority(raised, released, binding)
    return {
        "gate":str(gate),"source_route_id":str(source_route["route_id"]),
        "source_route_hash":str(source_route["route_hash"]),"epoch":epoch,
        "decision":"proceed","jobs_path":str(Path(jobs).resolve(strict=False)),
        "journal_path":str(ledger.journal_path.resolve(strict=False)),
        "raise_entry_digest":_sha256_record(raised),
        "release_entry_digest":_sha256_record(released),
    }

def _verify_continuation_gate_release_proofs(route):
    import workflow_state as WS
    proofs=route.get("reused_human_gate_releases") or []
    if not isinstance(proofs,list):
        raise ValueError("continuation-human-gate-release-proofs-invalid")
    seen=set()
    for proof in proofs:
        if not isinstance(proof,dict) or set(proof)!={
            "gate","source_route_id","source_route_hash","epoch","decision",
            "jobs_path","journal_path","raise_entry_digest","release_entry_digest",
        }:
            raise ValueError("continuation-human-gate-release-proof-invalid")
        gate=proof.get("gate")
        if (
            not isinstance(gate,str) or not gate or gate in seen
            or proof.get("source_route_id")!=route.get("source_route_id")
            or proof.get("source_route_hash")!=route.get("source_route_hash")
            or proof.get("decision")!="proceed"
            or not isinstance(proof.get("epoch"),int) or proof["epoch"]<1
        ):
            raise ValueError("continuation-human-gate-release-proof-invalid")
        seen.add(gate)
        jobs=Path(str(proof.get("jobs_path") or ""))
        journal=Path(str(proof.get("journal_path") or ""))
        expected=jobs.resolve(strict=False).parent/"workflow"/proof["source_route_id"]/"journal.jsonl"
        if not jobs.is_absolute() or not journal.is_absolute() or journal.resolve(strict=False)!=expected:
            raise ValueError("continuation-human-gate-release-proof-authority-invalid")
        try:
            entries=[json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        except (OSError,ValueError,UnicodeDecodeError) as exc:
            raise ValueError("continuation-human-gate-release-proof-unreadable") from exc
        resolution = WS.human_gate_resolution(entries, gate)
        if resolution.get("status") != "proceed" or resolution.get("epoch") != proof["epoch"]:
            note_same_work_drift("continuation-human-gate-release-proof-drift")
        raise_count=0; matched_raise=False; matched_release=False
        for entry in entries:
            evidence=entry.get("evidence") if isinstance(entry,dict) else None
            evidence=evidence if isinstance(evidence,dict) else {}
            if entry.get("workflow_state")=="BLOCKED_HUMAN_GATE" and evidence.get("gate")==gate:
                raise_count+=1
                matched_raise=(raise_count==proof["epoch"] and
                               _sha256_record(entry)==proof["raise_entry_digest"])
                matched_release=False
                continue
            if matched_raise and evidence.get("released_gate")==gate \
                    and (evidence.get("decision") or "proceed")=="proceed" \
                    and _sha256_record(entry)==proof["release_entry_digest"]:
                binding = next((item for item in route.get("human_gate_bindings", [])
                                if isinstance(item, dict) and item.get("gate") == gate), {})
                _continuation_release_authority(
                    next(item for item in entries
                         if _sha256_record(item)==proof["raise_entry_digest"]),
                    entry, binding,
                )
                matched_release=True
        if not matched_raise or not matched_release:
            note_same_work_drift("continuation-human-gate-release-proof-drift")
    return proofs

def _verify_continuation_route(route):
    if route.get("continuation_contract_version") != CONTINUATION_CONTRACT_VERSION:
        raise ValueError("unsupported-continuation-contract-version")
    _verify_selection_pins(route)
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
    _verify_continuation_gate_release_proofs(route)
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
        **({"dispatch_evidence_override":True,"dispatch_evidence":route.get("dispatch_evidence")}
           if route.get("dispatch_evidence_override") is True else {}),
        **({"ancestor_revision_digest":route.get("ancestor_revision_digest")}
           if route.get("ancestor_plan_refresh") is not None else {}),
    })
    if route.get("ancestor_plan_refresh") is not None:
        if route.get("ancestor_revision_digest")!=_sha256_record(route["ancestor_plan_refresh"]):
            raise ValueError("ancestor-revision-digest-invalid")
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
    rebind=route.get("source_commit_rebind")
    if rebind is not None:
        inherited=rebind.get("inherited_source_commit") if isinstance(rebind,dict) else None
        rebound=rebind.get("rebound_source_commit") if isinstance(rebind,dict) else None
        if (
            not isinstance(rebind,dict)
            or rebind.get("contract_version") != CONTINUATION_SOURCE_COMMIT_REBIND_VERSION
            or rebind.get("basis") != "first-parent-descendant"
            or not isinstance(inherited,str) or not _COMMIT_SHA.fullmatch(inherited)
            or not isinstance(rebound,str) or not _COMMIT_SHA.fullmatch(rebound)
            or inherited == rebound
            or rebound != route.get("source_commit")
            or Path(str(rebind.get("cwd") or "")).resolve(strict=False)
               != Path(str(route.get("cwd") or "")).resolve(strict=False)
        ):
            raise ValueError("continuation-source-commit-rebind-invalid")
        # The claimed basis is re-proved against the tree when the tree is here.
        # `verify_route` also runs where the worktree is absent or not a repo (a
        # closure on another host, a fixture), and the launch guard proves the
        # same lineage against real HEAD before anything dispatches -- so an
        # unanswerable probe is skipped, never guessed at.
        cwd=Path(str(route.get("cwd") or ""))
        if (
            _inside_git_worktree(cwd)
            and source_lineage_verdict(cwd,inherited).kind not in ("exact","descendant")
        ):
            note_same_work_drift("continuation-source-commit-rebind-lineage-unproven")
    _validate_output_scopes(route.get("nodes",[]))
    return route

def _note_evidence_changes(route,node_ids,*,jobs=None):
    """Keep gates-off evidence edits of these completed nodes as history (`note_evidence_change`)."""
    directory=completion_dir(route["route_id"],jobs=jobs)
    by_id={str(node.get("id")):node for node in route.get("nodes",[])}
    for node_id in node_ids:
        node=by_id.get(str(node_id)); path=directory/f"{node_id}.json"
        if node is not None and path.is_file():
            note_evidence_change(route,node,path)

def publish_continuation_route(route,source_route,output_path):
    """Recheck source bytes immediately before the one immutable publication."""
    if route.get("requested_boundary_blocker") or route.get("first_runnable_blocker"):
        raise ValueError("continuation-boundary-blocked")
    def checked_write():
        if route.get("ancestor_plan_refresh") is not None:
            verified_ancestor_plan_refresh(route)
        node_ids=[row["node_id"] for row in route.get("reused_nodes",[])]
        try:
            current,current_digest,_turns=_source_evidence_snapshot(source_route,node_ids)
        except ValueError as exc:
            raise ValueError("continuation-source-evidence-drift") from exc
        if (
            current_digest != route.get("source_evidence_digest")
            or canonical(current) != canonical(route.get("reused_nodes"))
        ):
            note_same_work_drift("continuation-source-evidence-drift")
        path=Path(output_path)
        if classify_route_location(path,route["artifact_root"]) != "canonical":
            raise ValueError("route-output-outside-canonical")
        if not route_path_is_exact(path,route["artifact_root"],route["route_id"]):
            raise ValueError("route-output-alias-basename")
        write_once(path,route)
        # The continuation proceeds on these completions: keep any gates-off
        # edit of their evidence as history.
        _note_evidence_changes(source_route,node_ids,jobs=_continuation_source_jobs(source_route))
        return path
    proof=route.get("ancestor_plan_refresh")
    if proof is None:
        return checked_write()
    lock=completion_dir(proof["ancestor_route_id"],jobs=_continuation_source_jobs(source_route)) / ".plan.completion.lock"
    with _exclusive_lock(lock):
        return checked_write()

def bind_continuation_cycle(artifact_root,source_route,route):
    """D-120: extend the source route's open cycle binding to a fresh continuation.

    Runs immediately after `publish_continuation_route` lands the new route
    file. If the source route's verified lineage has no open producer cycle,
    there is nothing to extend -- an ordinary unbound continuation. If it does,
    the new route is judged by the same `cycle_route_admission` every write
    site uses; only an `allow` verdict appends to the audit record
    (`bind_cycle_route`). A denial (a fork, a material-input change) never
    blocks publication -- the route is already on disk -- it only rides back
    as an advisory (D-120 "분기의 정직 표기").
    """
    from artifact_producer import ProducerError, bind_cycle_route, route_cycle_for
    root=Path(artifact_root)
    try:
        record=route_cycle_for(root,source_route)
    except ProducerError as exc:
        return {"bound":False,"cycle_id":None,"advisory":exc.code}
    if record is None:
        return {"bound":False,"cycle_id":None,"advisory":None}
    try:
        result=bind_cycle_route(root,record["cycle_id"],route)
    except ProducerError as exc:
        # D-120: publication itself is never blocked by a denied bind -- a
        # sibling fork's *publish-time* advisory is the distinct token
        # `cycle-lineage-fork`; the *write-time* refusal later (an actual
        # attempt to write the cycle from either branch) stays
        # `cycle-route-binding-mismatch:lineage-fork`.
        advisory = "cycle-lineage-fork" if exc.code == "cycle-route-binding-mismatch:lineage-fork" else exc.code
        return {"bound":False,"cycle_id":record["cycle_id"],"advisory":advisory}
    return {"bound":True,"cycle_id":record["cycle_id"],"advisory":result.get("advisory")}

def _git_commit(cwd):
    p=subprocess.run(["git","-C",str(cwd),"rev-parse","HEAD"],text=True,capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else "unversioned"

def worktree_mutating_scope(scope):
    """Shared with the launch guard and the read-only owner advisory."""
    return OWNER_WRITE_ADVISORY.worktree_mutating_scope(scope)

def _node_mutates_worktree(node):
    return any(worktree_mutating_scope(scope) for scope in (node.get("write_scope") or []))

_COMMIT_SHA=re.compile(r"[0-9a-f]{40}")

def _assert_pin_matches_grounding(source_commit, launch):
    """The pin and the grounding cwd must name one commit, or say so loudly.

    The two are read by different functions at different moments -- `_git_commit`
    (`git rev-parse HEAD`) for the pin, `runtime_activation.source_revision` for
    the grounding -- which is exactly the shape defect C had. `source_revision`
    appends `+<dirty-digest>` for a dirty tree and returns `release:`/`tree:`
    forms for non-git roots; only the git shapes are comparable, and the dirty
    suffix is split off the same way `_grounding_cwd_lineage_ok` does.
    """
    sealed=((launch.get("grounding_roots") or {}).get("cwd") or {}).get("release_id")
    if not isinstance(sealed,str) or not isinstance(source_commit,str):
        return
    base=sealed.split("+",1)[0]
    if not _COMMIT_SHA.fullmatch(base) or not _COMMIT_SHA.fullmatch(source_commit):
        return
    if base != source_commit:
        note_same_work_drift(f"continuation-source-commit-grounding-mismatch: pin={source_commit} grounding={base}")

def _continuation_source_commit(source_route):
    """Reseal the resume-time HEAD as the continuation's `source_commit`.

    The source route pinned the HEAD it was compiled at, but a continuation seals
    the *current* worktree HEAD into `launch_compatibility_tuple.grounding_roots.cwd`.
    Inheriting the old pin made the route contradict itself: after depth-0
    fast-forwarded the worktree, worker-route-guard refused every pre-mutation node
    with `route-source-commit-mismatch` (defect C, route rt-d7541f1033ae677f).
    Two sources, one value: the pin and the grounding must name the same commit.

    SD-156 retires the SD-67/SD-128/SD-133 decline branch this function used to
    carry: a continuation now always re-pins to the observed HEAD when it is the
    sealed commit itself or a first-parent descendant of it
    (`source_lineage_verdict` kind `exact`/`descendant`). `worker-route-guard.py`
    is the one place that adjudicates a moved HEAD now, on the same lineage
    verdict, regardless of node position or prior registry attempts -- there is
    no longer a "mutation retry" this builder must protect by refusing to move
    the pin. A diverged HEAD (rewritten, reset, unrelated history) still refuses
    outright; an unverifiable tree (non-git, mid-merge, timeout) leaves the pin
    untouched, exactly as an unresolvable `git rev-parse HEAD` did before.

    Returns `(source_commit, rebind_record)`; `rebind_record` is `None` when the
    pin is unchanged.
    """
    inherited=source_route.get("source_commit")
    cwd=source_route.get("cwd")
    if not inherited or not cwd:
        return inherited,None
    verdict=source_lineage_verdict(cwd,inherited)
    if verdict.kind in ("exact","unverifiable"):
        return inherited,None
    if verdict.kind == "diverged":
        observed=verdict.commits[0] if verdict.commits else _git_commit(cwd)
        note_same_work_drift("continuation-source-commit-diverged", f"expected={inherited} observed={observed}")
        return observed, None
    head=verdict.commits[0]
    return head,{
        "contract_version":CONTINUATION_SOURCE_COMMIT_REBIND_VERSION,
        "inherited_source_commit":inherited,
        "rebound_source_commit":head,
        "basis":"first-parent-descendant",
        "cwd":str(Path(cwd).resolve(strict=False)),
    }

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
    if ([n.get("id") for n in nodes] in (list(TOPO.ROUTE_FRAME_NODE_IDS), list(TOPO.ROUTE_FRAME_ONE_LEG_NODE_IDS))
            and nodes[-1].get("kind") == TOPO.ROUTE_DECISION_KIND):
        # The framed route has no depth-2 consumer: its checked tuples describe the runtime that
        # launches its frame legs, and it names the same parent depth as every recipe.
        return owner_dispatch_depth
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


def _single_owner_nodes(nodes):
    """An owner-only recipe needs owner readiness, not unused child tuples."""
    return (len(nodes) == 1 and nodes[0].get("kind") == "capability-owner"
            and nodes[0].get("unit") == "_kernel/owner"
            and nodes[0].get("dispatch_depth") == 1)

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


def _pass_registry_lines(jobs: Path):
    """The registry's text lines (``read_text(errors="replace").splitlines()``).

    A runner pass proves hundreds of owner markers against the same jobs log;
    each read it whole (46 MB on the live store). The shared registry cache
    returns the same lines from its generation-checked view: the pinned one in
    an open read pass, otherwise the current stable generation.
    """
    import dispatch_registry_cache
    if dispatch_registry_cache.registry_scope_active():
        return dispatch_registry_cache.registry_text_lines(jobs)
    with dispatch_registry_cache.registry_read_scope():
        return dispatch_registry_cache.registry_text_lines(jobs)


def _marker_identity_row(route, node, node_id, gate, *, jobs=None, exact_terminal=False,
                         registry_lines=None):
    """One completion marker's on-disk truth, in the shared gate vocabulary."""
    path = completion_dir(route["route_id"],jobs=jobs)/f"{node_id}.json"
    try:
        marker_bytes = path.read_bytes()
        marker = json.loads(marker_bytes)
    except (OSError, ValueError):
        return {"passed": False, "reason": "completion-marker-absent"}
    if (marker.get("route_id") != route.get("route_id")
            or marker.get("route_hash") != route.get("route_hash")
            or marker.get("node_id") != node_id
            or marker.get("completion_gate") != gate):
        note_same_work_drift("completion-marker-identity-mismatch", node_id)
    evidence = marker.get("evidence") or {}
    # SD-154 A-SD154-8: `evidence_currency` is the only place non-writer code
    # recomputes a completion marker's evidence sha256 -- this used to keep
    # its own copy and its own "hash-mismatch" reason, which it now resolves
    # into `revised-unrecorded` (evidence changed, `revise` can record it) or
    # an `integrity-broken:*` reason (nothing short of a restore can). This
    # row's `passed` has always been WEAKER than `completion_marker_is_current`
    # (M7/`test_ac5_owner_merge_arbitration_transaction`: a join can pass here
    # before its attempt-link file exists) -- read only `evidence_currency`,
    # never the fuller `gate_currency` that also proves the link.
    currency = evidence_currency(route, node, path, marker, jobs=jobs)
    if currency.state == "superseded":
        return {"passed": False, "reason": currency.reason}
    if currency.state == "revised-unrecorded":
        result = {"passed": False, "reason": currency.reason}
        if currency.next_action:
            result["next_action"] = currency.next_action
        return result
    if currency.state != "current":
        return {"passed": False, "reason": currency.reason}
    digest = currency.evidence_digest
    # B-1 (SD-153 defect #1): the continuation owner-closure shape gets its own
    # early return (`validate_continuation_owner_closure` proves the whole
    # cross-generation lineage instead of a single registered row); the
    # registered-review shape -- the *same* review row closed in place by the
    # owner -- is a real registered attempt and correctly falls through to the
    # ordinary registered-worker path below, which `owner_closure_shape` being
    # non-None for it does not change.
    if owner_closure_shape(marker) == "continuation":
        if not completion_marker_is_current(route, node, path, marker, jobs=jobs):
            return {"passed": False, "reason": "owner-closure-proof-not-current"}
        if exact_terminal:
            registry = Path(jobs) if jobs is not None else _continuation_source_jobs(route)
            ready = completion_attempt_readiness(route, node, marker, registry)
            return {"passed": ready.state == "ready", "reason": ready.reason, "current": ready.state == "ready",
                    "node_id": node_id, "attempt_id": marker["attempt_id"], "completion_gate": gate,
                    "marker_digest": hashlib.sha256(marker_bytes).hexdigest(), "evidence_digest": digest,
                    "evidence": evidence["path"], "review_independence": "owner-overridden"}
    if marker.get("registered_worker") is True or marker.get("stage_authority") == "owner-chain":
        try:
            registry = Path(jobs) if jobs is not None else _continuation_source_jobs(route)
        except ValueError:
            registry = path.parents[2] / "jobs.log"
        if registry_lines is None:
            try:
                lines = _pass_registry_lines(registry)
                if lines is None:
                    lines = registry.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []  # Existing semantic-history contract permits archived process rows.
            except OSError:
                return {"passed": False, "reason": "registry-unreadable"}
        else:
            lines = registry_lines
        if completion_conflict_attempt(marker, lines):
            return {"passed": False, "reason": "terminal-evidence-conflict"}
    if exact_terminal:
        if jobs is None or not completion_marker_is_current(route, node, path, marker, jobs=jobs):
            return {"passed": False, "reason": "completion-marker-not-current"}
        if registry_lines is None:
            try:
                lines = _pass_registry_lines(Path(jobs))
                if lines is None:
                    lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return {"passed": False, "reason": "registry-unreadable", "current": False,
                        "attempt_id": marker.get("attempt_id")}
        else:
            lines = registry_lines
        # The readiness helper is the single exact-attempt proof.  In
        # particular, it resolves depth-1 owner rows through owner_route_* and
        # admits supported no-process markers without inventing a registry row.
        # A second route_id/route_node scan here would reject both cases and
        # could disagree with the shared conflict/quiescence policy.
        readiness = completion_attempt_readiness(route, node, marker, Path(jobs), registry_lines=lines)
        if readiness.state != "ready":
            return {"passed": False, "reason": readiness.reason, "current": False,
                    "attempt_id": marker.get("attempt_id"), "attempt_readiness": readiness.state}
        stale = _registered_marker_fence(route, node, marker, lines)
        if stale is not None:
            return {"passed": False, "reason": stale, "current": False,
                    "attempt_id": marker.get("attempt_id"), "attempt_readiness": readiness.state}
        return {"passed": True, "reason": "completion-marker-verified", "current": True,
                "node_id": node_id, "attempt_id": marker.get("attempt_id"), "completion_gate": gate,
                "marker_digest": hashlib.sha256(marker_bytes).hexdigest(), "evidence_digest": digest,
                "evidence": evidence["path"], "attempt_readiness": "quiescent"}
    # Gate currentness is the pre-A2a marker identity/evidence contract.  The
    # jobs lock is used by mutation-time claim checks, not to reclassify an
    # already valid marker or require a registry attempt row here.  This also
    # preserves legacy inline markers whose attempt is intentionally absent.
    if jobs is None:
        return {"passed": True, "reason": "completion-marker-verified",
                "marker_digest": hashlib.sha256(marker_bytes).hexdigest(),
                "evidence": evidence.get("path")}
    return {"passed": True, "reason": "completion-marker-verified",
            "marker_digest": hashlib.sha256(marker_bytes).hexdigest(),
            "evidence": evidence.get("path"), "current": True,
            "attempt_readiness": "unchecked", "attempt_id": marker.get("attempt_id")}


def _registered_marker_fence(route, node, marker, lines):
    """Retain the terminal-retry fence after canonical readiness succeeds.

    Readiness proves the marker's exact attempt, identity, terminal row, and
    process state. This small second axis preserves the older reader contract:
    a marker from an earlier attempt is stale once a later row for the same
    canonical node identity has been recorded. Inline/resource markers have no
    registry attempt and are already fully proved by readiness. Returns the
    stale reason, or ``None`` when the marker is not fenced out.
    """
    if node.get("kind") == "resource-runner" or marker.get("registered_worker") is False:
        return None
    attempt_id = marker.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return "marker-attempt-id-missing"
    expected = (route.get("route_id"), route.get("route_hash"), node.get("id"))
    latest = None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        try:
            identity = ROUTE_IDENTITY.registered_node_identity(metadata, node)
        except ValueError:
            continue
        if identity == expected:
            latest = metadata.get("attempt_id")
    if latest != attempt_id and "retained_review" in marker:
        try:
            from retained_review_completion import verify_marker
            verify_marker(route, node, marker, completion_dir(route["route_id"]) / f"{node['id']}.json")
            if latest == marker["retained_review"]["rejected_attempt_id"]:
                return None
        except (KeyError, OSError, TypeError, ValueError):
            pass
    if latest != attempt_id:
        return "completion-attempt-not-current"
    return None
