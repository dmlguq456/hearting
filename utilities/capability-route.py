#!/usr/bin/env python3
"""Compile, verify, and complete immutable capability routes."""
from __future__ import annotations

import argparse, base64, contextlib, fcntl, functools, hashlib, importlib.util, json, os, re, shlex, shutil, subprocess, sys, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capability_topology", ROOT/"tools/capability_topology.py")
TOPO = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(TOPO)
DEFAULTS_SPEC = importlib.util.spec_from_file_location("dispatch_defaults", ROOT/"utilities/dispatch-defaults.py")
DEFAULTS = importlib.util.module_from_spec(DEFAULTS_SPEC); DEFAULTS_SPEC.loader.exec_module(DEFAULTS)
VALID_AFFINITY = DEFAULTS.AFFINITY_VALUES | {"unspecified"}
sys.path.insert(0, str(ROOT/"utilities"))
from hearting_gates import gates_on, same_work_or_refuse
import artifact_locator as ARTIFACT_LOCATOR
import route_identity as ROUTE_IDENTITY
import route_lineage as ROUTE_LINEAGE
import dispatch_runtime_support as RUNTIME_SUPPORT
import dispatch_terminal_commit
import model_profile as PROFILE
import review_round_cap as REVIEW_ROUND_CAP
import route_authority as ROUTE_AUTHORITY
import owner_write_advisory as OWNER_WRITE_ADVISORY
import gpu_execution_sandbox as GPU_SANDBOX
import resource_resume as RESOURCE_RESUME
from dispatch_continuation_budget import (COMPATIBILITY_FLOOR, TERMINAL_RESERVE_DEFAULT,
                                          derive_workload_ordinary)
from dispatch_contract import (
    row_is_subsession,
    CANONICAL_PARENT_TRANSPORTS,
    DispatchContractError,
    owner_operation_fence,
    EXECUTION_SURFACES,
    FALLBACK_HOPS,
    PARENT_TRANSPORT_BY_DISPATCH_DEPTH,
    SUCCESS_NOTES,
    deferred_completion,
    verdict_pass,
    success_note,
    WRAPPER_PARENT_SANDBOXES,
    WRAPPER_TRANSPORTS,
    _atomic_registry_replace,
    _delivery_intent_values,
    _updated_attempt_metadata,
    claim_terminal_route_locked,
    ensure_terminal_claim_absent,
    terminal_claim_observation,
    agent_home_equivalent,
    attempt_process_quiescence,
    process_namespace_identity,
    terminal_conflict_pending,
    completion_marker_is_current,
    completion_attempt_readiness,
    completion_conflict_attempt,
    owner_closure_shape,
    _diff_attribution_execute_launch_head,
    evidence_digest,
    evidence_change_history,
    evidence_currency,
    note_evidence_change,
    gate_currency,
    ROUTE_STATE_REFUSAL_REASONS,
    route_state_next_action,
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
from dispatch_completion_join import materialize_after_terminal_close  # noqa: E402
from codex_dispatch_terminal import (  # noqa: E402
    REVIEW_BLOCKING_NOTE,
    inspect_terminal_attempt,
)
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
# Strict equality on purpose: a future v2 changes what the record *means*, so an
# older verifier must refuse it rather than read it under v1 rules (fail closed).
CONTINUATION_SOURCE_COMMIT_REBIND_VERSION = 1
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
def runtime_root_hint(route=None):
    # A refused tuple is untrusted input even when its route hash is valid.
    # Recovery formatting must not replace the typed refusal with an exception.
    sealed = route.get("launch_compatibility_tuple") if isinstance(route, dict) else None
    runtime = sealed.get("runtime_root") if isinstance(sealed, dict) else None
    path = runtime.get("path") if isinstance(runtime, dict) else None
    expected = path if isinstance(path, str) and Path(path).is_absolute() else str(
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "hearting/current"
    )
    return (
        f"hint: use the sealed runtime root: AGENT_HOME={shlex.quote(expected)} "
        f"python3 {shlex.quote(str(Path(expected) / 'utilities/<tool>.py'))}; "
        "a runtime projection such as ~/.claude is not the managed release root"
    )
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
    """Normalize headless owner eligibility using the existing quick contract."""

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

canonical = ROUTE_IDENTITY.canonical
route_hash = ROUTE_IDENTITY.route_hash
# SD-155: one lineage walk, one canonical-path derivation -- re-exported, not
# redefined, so `ROUTE.verified_route_lineage is route_lineage.verified_route_lineage`.
verified_route_lineage = ROUTE_LINEAGE.verified_route_lineage
RouteLineageError = ROUTE_LINEAGE.RouteLineageError

def route_family_key(capability,cwd,capability_mode,owner_attempt_id):
    payload=[capability,str(cwd),capability_mode,owner_attempt_id]
    digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    return "sha256:"+digest

def _resolve_owner_attempt_id():
    """`AGENT_DISPATCH_ATTEMPT_ID` inside an owner, else `-` (not-missing sentinel, F47-1)."""
    return os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or "-"

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
        _LAUNCH_SOURCE_REVISION_CACHE[key]=_runtime_activation_module().source_revision(resolved,runtime_launch=True)
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

def _contained_regular_release_file(root, relative):
    """Return one release-owned regular file, rejecting every symlink component."""
    candidate=root
    for part in Path(relative).parts:
        if part in ("", ".", ".."):
            return None
        candidate=candidate/part
        try:
            if candidate.is_symlink():
                return None
        except OSError:
            return None
    try:
        resolved=candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not candidate.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate

def _verified_immutable_release_identity(path):
    """Return the bounded identity of one complete managed-release code root.

    A marker is necessary but never sufficient: the installed release version,
    whole immutable release revision, and closed launch-anchor digest must all
    agree.  `published_at` proves marker completeness but is intentionally not
    a comparison axis; the revision digest still covers its exact bytes.
    """
    try:
        root=Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not root.is_dir():
        return None
    marker_path=_contained_regular_release_file(root,".hearting-release.json")
    version_path=_contained_regular_release_file(root,"RELEASE_VERSION")
    if marker_path is None or version_path is None:
        return None
    try:
        if marker_path.stat().st_size > 16_384 or version_path.stat().st_size > 128:
            return None
        marker=json.loads(marker_path.read_text(encoding="utf-8"))
        release_version=version_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(marker,dict) or type(marker.get("schema")) is not int:
        return None
    version=marker.get("version")
    archive_sha256=marker.get("archive_sha256")
    published_at=marker.get("published_at")
    if (
        marker["schema"] != 1
        or not isinstance(version,str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",version) is None
        or release_version != version
        or not isinstance(archive_sha256,str)
        or re.fullmatch(r"[0-9a-f]{64}",archive_sha256) is None
        or not isinstance(published_at,str)
        or not published_at
        or len(published_at) > 256
    ):
        return None
    if any(_contained_regular_release_file(root,relative) is None
           for relative in _LAUNCH_CODE_ANCHORS):
        return None
    try:
        release_id=_launch_source_revision(root)
        content_digest=_launch_content_digest(root)
    except (OSError, RuntimeError):
        return None
    prefix=f"release:{version}:"
    if (
        not release_id.startswith(prefix)
        or re.fullmatch(r"[0-9a-f]{12}",release_id[len(prefix):]) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}",content_digest) is None
    ):
        return None
    return {
        "schema":marker["schema"],"version":version,
        "archive_sha256":archive_sha256,"release_id":release_id,
        "content_digest":content_digest,
    }

def immutable_code_root_equivalent(a,b):
    """Compare route code roots without weakening general path equivalence.

    Symlink aliases retain the existing resolved-path behavior.  Distinct
    physical paths compare by content only when both are complete, verified
    immutable managed-release copies.  Mutable/state callers must continue to
    use `agent_home_equivalent()` directly.
    """
    if agent_home_equivalent(a,b):
        return True
    left=_verified_immutable_release_identity(a)
    right=_verified_immutable_release_identity(b)
    return left is not None and left == right

def _forget_launch_path(path):
    """Drop every memoized identity naming this path, before any of them is read.

    The caches exist for immutable code roots, but a route cwd is the mutation
    worktree and moves under them: a process that already sealed that cwd (an
    earlier compile, a test) would otherwise hand a continuation the HEAD as it
    was then.

    Every identity for the path goes, not just the one being re-read. Under dev
    activation `runtime_root`, `launch_home` and `registry_root` can *be* that
    same path, and re-reading one of them while the others answer from cache
    puts two release ids for one path in a single tuple -- the same
    two-sources-one-value shape defect C was (review round 2, S2). Eviction is
    keyed by resolved path, so identities for genuinely different paths are
    untouched.
    """
    resolved=str(Path(path).expanduser().resolve(strict=False))
    for key in [key for key in _LAUNCH_ROOT_IDENTITY_CACHE if key[1]==resolved]:
        _LAUNCH_ROOT_IDENTITY_CACHE.pop(key,None)
    _LAUNCH_SOURCE_REVISION_CACHE.pop(resolved,None)
    _LAUNCH_CONTENT_DIGEST_CACHE.pop(resolved,None)

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
            # A mutable project cwd is compared by first-parent HEAD lineage,
            # never by installer dirty-content hashes. On 2026-09-27 home-os
            # had >100k untracked runtime files under a tracked directory;
            # hashing them here exhausted the pre-claim fence's 60s budget.
            # A cwd without HEAD has no lineage to compare and reads
            # "unversioned", like its source pin: hashing it instead read a
            # whole home folder on every start and resume. Code roots retain
            # their complete release identity, including when a development
            # checkout or a release tree (RELEASE_VERSION) is the grounding cwd.
            if kind == "grounding_cwd" and str(resolved) not in _LAUNCH_SOURCE_REVISION_CACHE:
                try:
                    head=subprocess.run(
                        ["git","-C",str(resolved),"rev-parse","--verify","HEAD"],
                        capture_output=True,text=True,timeout=10,
                        env={**os.environ,"GIT_OPTIONAL_LOCKS":"0"},
                    )
                except (OSError,subprocess.TimeoutExpired) as exc:
                    raise ValueError("grounding-cwd-revision-unverifiable") from exc
                if head.returncode == 0 and _GIT_SHA.fullmatch(head.stdout.strip()):
                    release_id=head.stdout.strip()
                elif (resolved/"RELEASE_VERSION").is_file():
                    release_id=_launch_source_revision(resolved)
                else:
                    release_id="unversioned"
            else:
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

def launch_compatibility_tuple(*, artifact_root, jobs=None, cwd=None, refresh_cwd=False):
    """Compute v1 bounded launch identities without hashing mutable state contents."""
    runtime_root=Path(resolve_agent_home()).resolve(strict=False)
    grounding_cwd=Path(cwd if cwd is not None else Path.cwd()).resolve(strict=False)
    if refresh_cwd:
        # Up front, before the first identity is read, so every identity in this
        # tuple comes from one reading of the tree.
        _forget_launch_path(grounding_cwd)
    runtime_identity=_launch_root_identity("runtime_root",runtime_root)
    # Compute every executable root before the mutable cwd: a route may run
    # inside adapters/, which is itself the wrapper code root.
    wrapper_identity=_launch_root_identity("wrapper_root",runtime_root/"adapters")
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
        "wrapper_root":wrapper_identity,
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
    malformed_roots=False
    if not isinstance(sealed,dict):
        return False,{"tuple":{"expected":sealed,"actual":fresh}}
    for field in ("contract_version","tuple_version"):
        if sealed.get(field) != fresh.get(field):
            mismatches[field]={"expected":sealed.get(field),"actual":fresh.get(field)}
    expected_roots=_launch_tuple_roots(sealed)
    actual_roots=_launch_tuple_roots(fresh)
    release_moved=ROUTE_AUTHORITY.release_moved(
        expected_roots,actual_roots,
        managed_release=lambda path:_verified_immutable_release_identity(path) is not None,
    )
    identity_fields=("kind","path","release_id","content_digest","binding_digest")
    for name,expected in expected_roots.items():
        actual=actual_roots[name]
        if not isinstance(expected,dict) or not isinstance(actual,dict):
            malformed_roots=True
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
        if changed and name in release_moved:
            changed={}  # the installed release moved: where the launch runs, not the work
        if changed:
            mismatches[name]={
                "expected":expected,"actual":actual,"fields":sorted(changed),
            }
    if mismatches and not malformed_roots and not gates_on():
        same_work_or_refuse("launch-runtime-root-mismatch", ",".join(sorted(mismatches)))
        return True, mismatches
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
    if not gates_on():
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
        if gates_on():
            raise ValueError("continuation-last-turn-mismatch")
        same_work_or_refuse("continuation-last-turn-mismatch")
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
        if gates_on():
            raise ValueError("continuation-fork-source-mismatch")
        same_work_or_refuse("continuation-fork-source-mismatch")
    if not expected_turn or selected_turn != expected_turn:
        if gates_on():
            raise ValueError("continuation-last-turn-mismatch")
        same_work_or_refuse("continuation-last-turn-mismatch")
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
            if gates_on():
                raise ValueError("continuation-human-gate-release-proof-drift")
            same_work_or_refuse("continuation-human-gate-release-proof-drift")
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
            if gates_on():
                raise ValueError("continuation-human-gate-release-proof-drift")
            same_work_or_refuse("continuation-human-gate-release-proof-drift")
    return proofs

def build_continuation_route(
    source_route,*,resume_from_node,requested_boundary,reason,
    artifact_root,lineage_operation="resume",thread_id=None,new_thread_id=None,
    forked_from_id=None,last_turn_id=None,ephemeral=False,
    partial_group=None,dispatch_evidence=None,
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
    inherited_refresh=source_route.get("ancestor_plan_refresh")
    if inherited_refresh is not None:
        verified_ancestor_plan_refresh(source_route)
    else:
        inherited_refresh=_verified_ancestor_plan_refresh(source_route)
        if inherited_refresh is not None and resume_from_node!="plan-check":
            first_blocker="ancestor-revision-affected-node-skipped"
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
    if dispatch_evidence is not None:
        dispatch_evidence=_validate_dispatch_evidence(
            dispatch_evidence,source_route.get("dispatch_contract_version") or DISPATCH_CONTRACT_VERSION,
            _evidence_parent_dispatch_depth(source_nodes,source_route.get("owner_dispatch_depth",1)),
            expected_worktree=source_route.get("cwd"),require_scope=True)
        identity.update({"dispatch_evidence_override":True,"dispatch_evidence":dispatch_evidence})
    if inherited_refresh is not None:
        identity["ancestor_revision_digest"]=_sha256_record(inherited_refresh)
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
    if inherited_refresh is not None:
        result["ancestor_plan_refresh"]=json.loads(json.dumps(inherited_refresh))
    if partial_group is not None and first_blocker is None:
        result["partial_group_continuation"]=partial_group_continuation(
            source_route,**partial_group
        )
    if requested_blocker or first_blocker:
        return result
    route_nodes=[]
    descriptors=[]
    for offset,source_node in enumerate(source_nodes[resume_index:]):
        source_contract_hash=_continuation_contract_hash(source_node)
        node=_continuation_node_projection(source_node,reused)
        if dispatch_evidence is not None and node.get("dispatch_depth")==2:
            node["fallback_hops"]=_fallback_chain(
                dispatch_evidence,source_route.get("dispatch_contract_version") or DISPATCH_CONTRACT_VERSION,
                expected_worktree=source_route.get("cwd"),require_scope=True)
        if (inherited_refresh is not None
                and source_route["route_id"]==inherited_refresh["source_route_id"]
                and node["id"]=="plan-check"):
            dependencies=node.get("reused_dependencies") or []
            if (len(dependencies)!=1 or dependencies[0].get("node_id")!="plan"
                    or dependencies[0].get("marker_digest")!=inherited_refresh["old_marker_digest"]):
                raise ValueError("ancestor-revision-review-dependency-invalid")
            dependencies[0]["marker_digest"]=inherited_refresh["current_marker_digest"]
        route_nodes.append(node)
        descriptors.append({
            "node_id":str(source_node["id"]),
            "source_contract_hash":source_contract_hash,
            "realized_contract_hash":_continuation_contract_hash(node),
            "attempt_authority":"granted" if offset==0 else "pending-dependency",
            "new_attempt_count":0,
        })
    result["new_nodes"]=descriptors
    # A continuation is sealed at resume time: ground it in the cwd as it is now,
    # not as a memoized earlier seal of the same process saw it (defect C pairing).
    launch={
        "contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
        **launch_compatibility_tuple(
            artifact_root=artifact_root,cwd=source_route.get("cwd"),refresh_cwd=True,
        ),
    }
    inherited_keys=(
        "schema_version","capability","capability_mode","slug","slug_truncated",
        "campaign_key","parent_cycle_id","campaign_unassigned","work_request",
        "selection_pins","requested_intensity",
        "effective_intensity","owner_model_profile","execution_topology",
        "owner_dispatch_depth","max_dispatch_depth","tracking",
        "tracked_gate_evidence","spec_touch","cwd","source_commit",
        "registry_digest","capability_registry_digest","dispatch_defaults_digest","dispatch_allocation",
        "owner_harness_policy","selection","codex_execution_sandbox","human_gates","human_gate_bindings",
        "confirmation_mode","small_work_confirmation",
        "resume_retry_boundaries","dispatch_evidence","dispatch_contract_version",
        "dispatch_evidence_scope_version","registered_headless_candidates",
        "registered_headless_policy","unit_catalog_digest","validation_basis",
        # Profile-demand sealing is part of the route identity. Continuation
        # suffixes must carry it forward so verification can replay the same
        # resolver contract instead of treating an annotated source as legacy.
        "profile_selection_contract_version","profile_demands","explicit_profiles",
        "owner_profile_demand","owner_profile_selection",
        # SD-OPEN-46: a composed source's composition fields must ride the
        # suffix, or the continuation presents itself as a preset route and the
        # embedded composed_recipe loses its hash seal. (`route_origin`/`shape`
        # live inside `selection`, inherited above.)
        "composed","composed_recipe",
        # An approved route plan's leg keeps pointing at its plan when it resumes.
        "route_plan",
    )
    inheritance=ROUTE_AUTHORITY.route_in_force(source_route)
    route={key:json.loads(json.dumps(inheritance[key]))
           for key in inherited_keys if key in inheritance}
    pins=ROUTE_AUTHORITY.selection_pin_rows(source_route)
    if pins:
        route["selection_pins"]={"contract_version":SELECTION_PIN_CONTRACT_VERSION,**pins}
    route.update(result)
    if route.get("profile_selection_contract_version") == 1:
        retained = {node["id"] for node in route_nodes} | {"__owner__"}
        for key in ("profile_demands", "explicit_profiles"):
            route[key] = {k: v for k, v in route.get(key, {}).items() if k in retained}
    # Defect C: the pin must name the same commit the grounding tuple above sealed.
    source_commit,source_commit_rebind=_continuation_source_commit(source_route)
    if source_commit is not None:
        route["source_commit"]=source_commit
    if source_commit_rebind is not None:
        route["source_commit_rebind"]=source_commit_rebind
    _assert_pin_matches_grounding(route.get("source_commit"),launch)
    route["artifact_root"]=str(Path(artifact_root).resolve(strict=False))
    route["nodes"]=route_nodes
    # A continuation is a suffix, not a copy of the source graph. An entry gate
    # whose target was cut is gone. A raised gate whose raiser was cut but target
    # remains may disappear only with the exact source proceed evidence sealed.
    suffix_ids={str(node["id"]) for node in route_nodes}
    source_raisers={
        str((node.get("continuation") or {}).get("gate")):str(node.get("id"))
        for node in source_nodes
        if (node.get("continuation") or {}).get("kind")=="human-gate"
    }
    projected_bindings=[]; release_proofs=[]
    for row in (source_route.get("human_gate_bindings") or []):
        if str(row.get("node")) not in suffix_ids:
            continue
        gate=str(row.get("gate"))
        raiser=source_raisers.get(gate)
        if raiser is not None and raiser not in suffix_ids:
            release_proofs.append(_continuation_gate_release_proof(source_route,gate))
            continue
        projected_bindings.append(json.loads(json.dumps(row)))
    route["human_gate_bindings"]=projected_bindings
    route["human_gates"]=sorted({str(row["gate"]) for row in projected_bindings})
    route["reused_human_gate_releases"]=release_proofs
    registry=TOPO.load_registry()
    try:
        TOPO._validate_continuations(
            route,registry,route_nodes,{str(node["id"]):node for node in route_nodes},
        )
    except TOPO.TopologyError as exc:
        raise ValueError("continuation-human-gate-unrepresentable:"+str(exc)) from exc
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
    owner_attempt_id=_resolve_owner_attempt_id()
    route["owner_attempt_id"]=owner_attempt_id
    route["route_family_key"]=route_family_key(
        route.get("capability"),route.get("cwd"),route.get("capability_mode"),owner_attempt_id,
    )
    return route

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
            if gates_on():
                raise ValueError("continuation-source-commit-rebind-lineage-unproven")
            same_work_or_refuse("continuation-source-commit-rebind-lineage-unproven")
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
            if gates_on():
                raise ValueError("continuation-source-evidence-drift")
            same_work_or_refuse("continuation-source-evidence-drift")
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

_STAGE_FALLBACK=None

def _stage_fallback():
    """Load the shared registry reader the launch guard uses, once."""
    global _STAGE_FALLBACK
    if _STAGE_FALLBACK is None:
        spec=importlib.util.spec_from_file_location(
            "_capability_route_stage_fallback",ROOT/"utilities"/"stage-dispatch-fallback.py",
        )
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _STAGE_FALLBACK=module
    return _STAGE_FALLBACK

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
        if gates_on():
            raise ValueError(
                f"continuation-source-commit-grounding-mismatch: pin={source_commit} grounding={base}"
            )
        same_work_or_refuse(f"continuation-source-commit-grounding-mismatch: pin={source_commit} grounding={base}")

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
        if gates_on():
            raise ValueError(
                f"continuation-source-commit-diverged: expected={inherited} observed={observed}"
            )
        same_work_or_refuse("continuation-source-commit-diverged", f"expected={inherited} observed={observed}")
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
                            capability, *, auxiliary_check_units=None, persona_policy=True):
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
                if "part_io" in leg:  # SD-165: a borrowed leg's own outputs moved with it
                    leg["part_io"] = {
                        name: _parallel_path(path, suffix) if path in base["outputs"] else path
                        for name, path in base["part_io"].items()}
            leg["model_profile"] = leg_spec["model_profile"]
            leg.pop("profile_demand", None)
            if "profile_demand" in leg_spec:
                leg["profile_demand"] = json.loads(json.dumps(leg_spec["profile_demand"]))
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
            leg["independence_axis"] = "perspective" if persona_policy else "cross-harness"
            members.append(leg)
        for node in nodes:
            if node is not base and base["id"] in node.get("depends_on", []):
                node["depends_on"] = list(node["depends_on"]) + [
                    member["id"] for member in members[1:]
                ]
                if (base.get("kind") != "review-worker"
                        or node.get("completion_gate") == "compose-owner-close"):
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

# Recommendation findings of the dispatch-defaults policy (`load_validated`)
# collected while a route is sealed; `compose` prints them as `warning=` lines.
# Warnings never change what is sealed beyond the normalization the loader
# already applied.
DISPATCH_DEFAULTS_WARNINGS = []


def _note_warning(line):
    if line not in DISPATCH_DEFAULTS_WARNINGS:
        DISPATCH_DEFAULTS_WARNINGS.append(line)


def _seal_dispatch_defaults(nodes, capability, owner_profile=None):
    """Return defaults digest/allocation and stamp each dispatch-depth-2 node's
    harness_affinity, BEFORE route_hash is computed. Absent config -> all
    'unspecified' + digest None. Structurally corrupt config -> fail-loud
    (reused loader validator), surfaced as ValueError so main() exits 64; a
    recommendation the config breaks is a `warning=` line, not a failure.
    Each depth-2 node and each depth-1 frame leg carries its profile's sealed
    harness_policy, so a frame launched later reads the same bands the route
    was sealed with instead of re-reading the live file. registry_digest is a
    separate field and is never touched here."""
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        for node in nodes:
            if node.get("dispatch_depth") == 2:
                node["harness_affinity"] = "unspecified"
                node["harness_policy"] = None
            elif _frame_node(node):
                node["harness_policy"] = None
        return None, None, None
    try:
        cfg, findings = DEFAULTS.load_validated(config_path, DEFAULTS.default_topology_path())
    except DEFAULTS.DefaultsConfigError as exc:
        raise ValueError(f"corrupt dispatch-defaults config: {exc}")
    for finding in findings:
        _note_warning(f"warning=dispatch-defaults:{finding}")
    for node in nodes:
        if node.get("dispatch_depth") == 2:
            node["harness_affinity"] = DEFAULTS.query_stage_affinity(
                cfg, capability, node.get("parallel_anchor", node["id"])
            )
            node["harness_policy"] = DEFAULTS.query_profile_policy(
                cfg, node["model_profile"]
            )
        elif _frame_node(node):
            node["harness_policy"] = DEFAULTS.query_profile_policy(
                cfg, node["model_profile"]
            )
    return (
        "sha256:" + hashlib.sha256(canonical(cfg)).hexdigest(),
        DEFAULTS.query_allocation(cfg),
        DEFAULTS.query_profile_policy(cfg, owner_profile) if owner_profile else None,
    )


def _resolve_owner_profile(effective, registry, demand=None, explicit_profile=None):
    """One selection for compile and verify: demand, otherwise legacy default.

    Intensity describes the workflow shape; its compatibility default must not
    veto the independently validated judgment/execution demand.
    """
    default = registry["owner_profile_by_intensity"].get(effective) or "light"
    selection = PROFILE.resolve_profile_demand(
        demand, explicit_profile=explicit_profile if explicit_profile is not None else default if demand is None else None,
        legacy=explicit_profile is None, existing_versioned_stage=True)
    profile = selection["resolved_profile"]
    if effective == "direct":
        if profile == PROFILE.TOP_PROFILE:
            raise ValueError("owner-profile-top-requires-owner")
        return None, selection
    if profile != PROFILE.TOP_PROFILE and registry["model_profiles"].get(
            profile, {}).get("registered_topology") is not True:
        raise ValueError("owner-profile-ineligible-for-registered-dispatch")
    return profile, selection


def _owner_node(node, effective):
    """Whether `node` IS the route's owner: only the quick shape's `one-shot`
    node (the owner and the node are one process there). A standard+ recipe's
    depth-1 `_kernel/owner` *stage* nodes (prd-transaction, handback, deploy,
    ...) are not the owner and never inherit its profile (review R2 B1: sealing
    `top` onto them made compile and verify disagree again, and would have
    spent the main-session model on stages nobody asked for). One predicate
    for the compiler and every verify check."""

    return (effective == "quick" and node.get("id") == "one-shot"
            and node.get("dispatch_depth") == 1 and node.get("unit") == "_kernel/owner")


def _no_model_node(node):
    """Whether `node` runs no model: a detached resource run, or the framed route's runtime terminal."""
    return node.get("kind") in ("resource-runner", TOPO.ROUTE_DECISION_KIND)


def _frame_node(node):
    """Whether `node` is a frame bootstrap leg: the depth-1 direction-setting
    pair that the depth-0 session launches itself, ahead of any owner. Mirrors
    `_owner_node`'s style so both the compiler and every verify check read one
    predicate. This is the SECOND node class allowed the `top` exception
    profile -- a frame leg is the one place where spending the main-session
    model buys the whole route its framing, and it is bounded to one leg by
    `replica_batch_contract`'s per-group top cap."""

    return (node.get("unit") == "plan/frame"
            and node.get("dispatch_depth") == 1
            and node.get("worker_type") == "frame")


def _quick_frame_diversity(candidates):
    """ONE definition of a quick frame pair's harness diversity, shared by the
    compiler and `verify_route` (they used to count supported harnesses
    separately, and a policy change on one side alone would have made every
    sealed single-harness route unverifiable).

    Zero supported harnesses cannot frame at all. The supported harness set
    is descriptive provenance, not an independence requirement (SD-160).
    One harness with separate executions and two personas is normal."""

    harnesses = sorted({
        row.get("harness") for row in candidates or []
        if row.get("status") == "supported" and row.get("harness")
    })
    if not harnesses:
        raise ValueError("quick-frame-harness-unavailable")
    return ("cross-harness" if len(harnesses) >= 2
            else "single-harness:" + harnesses[0])


def _stamp_frame_profiles(nodes, owner_profile, owner_demand, *, seal_persona=True, prior=False):
    """Stamp every frame leg's `model_profile` from the one tier ladder.

    ONE function called by BOTH the compiler and `verify_route`'s expected-node
    recomputation. It has to be shared: the verifier rebuilds the node list
    from the recipe and compares field by field, so a ladder applied on only
    one side reports every standard+ route as
    `node-profile-declaration-mismatch:frame` -- which is exactly what happened
    the first time this was written inline in the compiler.

    Runs BEFORE `_seal_profile_demands` on both sides, because that is what
    turns the stamped profile into the node's sealed selection.

    `prior` is for the verifier alone (`_expected_nodes_under_frame_policy`): it rebuilds the
    declaration an already sealed route made under the one prior frame policy. Compilation never
    passes it."""

    rungs = PROFILE.frame_profile_for_owner(owner_profile, prior=prior)
    if sum(1 for node in nodes if _frame_node(node)) == 1 and not prior:
        # A framed route's one leg (only a framed route has a single frame leg).
        rungs = {"anchor": PROFILE.FRAME_SINGLE_LEG_PROFILE, "others": PROFILE.FRAME_SINGLE_LEG_PROFILE}
    for node in nodes:
        if not _frame_node(node):
            continue
        # SD-160: seal the distinct personas; old routes retain their original
        # bytes and admission resolves the same canonical frame roles.
        if seal_persona:
            node.setdefault("perspective", "primary-frame" if node.get("id") == "frame"
                            else "alternative-frame")
        # Both frame perspectives get the same top-tier default. Explicit
        # route selections are applied later and retain precedence.
        profile = rungs["anchor" if node.get("id") == "frame" else "others"]
        node["model_profile"] = profile
        if profile == PROFILE.TOP_PROFILE:
            # `top` is not a portable profile, so it cannot be sealed through
            # the legacy "explicit profile, no demand" path -- the resolver
            # refuses that with `profile-demand-required`. Give the anchor a
            # real explicit selection instead: the owner's own demand when the
            # caller supplied one, otherwise the frame shape's intrinsic
            # demand. Each leg seals its own selection and demand.
            node["profile_explicit"] = True
            node["profile_demand"] = json.loads(json.dumps(
                owner_demand or PROFILE.frame_anchor_shape_demand(prior=prior)))
    return nodes


def _expected_nodes_under_frame_policy(base_nodes, route, *, persona_version, legacy, accepts):
    """The route's expected nodes, stamped and sealed under ONE frame policy.

    A route sealed before the top/top default keeps the frame declaration it was made with. The
    current policy is tried first; if the route does not hold it, the one prior policy is tried, and
    only as a whole -- every frame node of the route is rebuilt under the same policy, so a route
    that mixes the two is refused. Everything else (graph, scope, gates, explicit caller profiles,
    the demand digests) is rebuilt and compared exactly as before by the caller. When neither policy
    is held, the current policy's nodes (or its error) are returned so the caller reports the
    same diagnostic as before."""

    attempts = (False, True) if any(_frame_node(n) for n in base_nodes) else (False,)
    first_error = first_nodes = None
    for prior in attempts:
        nodes = json.loads(json.dumps(base_nodes))
        try:
            _stamp_frame_profiles(nodes, route.get("owner_model_profile"),
                                  route.get("owner_profile_demand"),
                                  seal_persona=persona_version == 1, prior=prior)
            _seal_profile_demands(nodes, route.get("profile_demands"),
                                  route.get("explicit_profiles"), legacy=legacy)
        except ValueError as exc:
            if not prior:
                first_error = exc
            continue
        if accepts(nodes):
            return nodes
        if not prior:
            first_nodes = nodes
    if first_error is not None:
        raise first_error
    return first_nodes


def _recipe_has_frame(recipe):
    return any(_frame_node(node) for node in recipe["standard_plus"]["nodes"])


def _quick_gate_bindings(recipe):
    """Quick's single human gate binding: the frame pair fences `one-shot`."""

    bindings = ([{"gate": "frame-review", "node": "one-shot", "position": "entry"}]
                if _recipe_has_frame(recipe) else [])
    bindings.extend({"gate": gate, "node": "one-shot", "position": "terminal"}
                    for gate in recipe["quick"].get("inline_human_gates", []))
    return bindings


def _one_leg_frame_recipe(recipe):
    """The framed recipe that runs one frame leg: the alternative leg is gone and the decision
    terminal depends on `frame` alone. ONE projection, used by the compiler and `verify_route`."""
    view=json.loads(json.dumps(recipe))
    nodes=[n for n in view["standard_plus"]["nodes"] if n.get("id")!="frame-alternative"]
    for node in nodes:
        if node.get("id")=="route-decision":
            node["depends_on"]=["frame"]
    view["standard_plus"]["nodes"]=nodes
    return view


def _frameless_recipe(recipe):
    """The recipe as a leg of an approved route plan sees it: the frame pair, its gate and its
    binding are gone, exactly as for a capability that never had a frame layer."""
    view=json.loads(json.dumps(recipe))
    view["standard_plus"]["nodes"]=[n for n in view["standard_plus"]["nodes"] if not _frame_node(n)]
    view["human_gates"]=[g for g in view["human_gates"] if g!="frame-review"]
    view["human_gate_bindings"]=[b for b in view["human_gate_bindings"] if b.get("gate")!="frame-review"]
    return view


def _seal_profile_demands(nodes, profile_demands=None, explicit_profiles=None, *, legacy=False):
    demands = profile_demands or {}
    explicit_profiles = explicit_profiles or {}
    for node in nodes:
        if _no_model_node(node):
            continue
        node_id = node["id"]
        demand = demands.get(node_id, node.get("profile_demand"))
        supplied = demand is not None
        if supplied:
            demand = PROFILE.normalize_profile_demand(demand)
        explicit = explicit_profiles.get(node_id)
        if node.get("profile_explicit") and node_id not in explicit_profiles:
            explicit = node.get("model_profile")
        if not supplied:
            if explicit is None and legacy:
                explicit = node.get("model_profile", "light")
        selection = PROFILE.resolve_profile_demand(
            demand, explicit_profile=explicit, legacy=legacy and node_id not in explicit_profiles and not node.get("profile_explicit"),
            existing_versioned_stage=legacy,
        )
        node["profile_demand"] = demand
        node["profile_selection"] = selection
        node["model_profile"] = selection["resolved_profile"]
    return nodes


def _profile_input_maps(nodes, demands, explicit):
    valid = {n["id"] for n in nodes if not _no_model_node(n)} | {"__owner__"}
    normalized = {}
    for label, values in (("profile_demands", demands), ("explicit_profiles", explicit)):
        if values is not None and (not isinstance(values, dict) or set(values) - valid):
            raise ValueError("profile-input-unknown-node:" + label)
    for key, value in (demands or {}).items():
        normalized[key] = PROFILE.normalize_profile_demand(value)
    frame_ids = {n["id"] for n in nodes if _frame_node(n)}
    for key, value in (explicit or {}).items():
        if value not in PROFILE.KNOWN_PROFILES:
            raise ValueError("profile-explicit-input-invalid:" + key)
        if value == PROFILE.TOP_PROFILE and key != "__owner__" and key not in frame_ids:
            # The top exception profile is a dispatch-depth-1 decision: a
            # depth-2 stage node or parallel leg never spends the main-session
            # model. A depth-1 frame leg is the one added exception -- it is an
            # anchor for the whole route's direction, launched by the depth-0
            # session itself.
            raise ValueError("profile-explicit-top-owner-only:" + key)
    return normalized, dict(explicit or {})


def _verify_profile_contract(route):
    version = route.get("profile_selection_contract_version")
    if version is None:
        # Exact sealed legacy routes contain no new semantic fields.
        if route.get("owner_profile_selection") is not None or any(
            "profile_selection" in n or "profile_demand" in n for n in route.get("nodes", [])):
            raise ValueError("profile-selection-contract-missing")
        return
    if type(version) is not int or version != 1:
        raise ValueError("profile-selection-version-unsupported")
    demands, explicit = _profile_input_maps(route.get("nodes", []), route.get("profile_demands"), route.get("explicit_profiles"))
    if route.get("owner_profile_demand") != demands.get("__owner__"):
        raise ValueError("owner-profile-demand-map-mismatch")
    owner_profile = route.get("owner_model_profile") or "light"
    owner_expected = PROFILE.resolve_profile_demand(
        demands.get("__owner__"), explicit_profile=(explicit.get("__owner__")
            if "__owner__" in explicit or "__owner__" in demands else owner_profile),
        legacy="__owner__" not in explicit, existing_versioned_stage=True)
    if owner_expected != route.get("owner_profile_selection"):
        raise ValueError("owner-profile-selection-map-mismatch")
    for node in route.get("nodes", []):
        node_id = node["id"]
        if node_id in demands and node.get("profile_demand") != demands[node_id]:
            raise ValueError("node-profile-demand-map-mismatch:" + node_id)
        if node_id in explicit and (node.get("profile_selection", {}).get("source") != "explicit"
                                   or node.get("model_profile") != explicit[node_id]):
            raise ValueError("node-profile-explicit-map-mismatch:" + node_id)
    PROFILE.validate_profile_selection(
        route.get("owner_profile_selection"), route.get("owner_profile_demand"),
        profile=route.get("owner_model_profile"), existing_versioned_stage=True,
    )
    for node in route.get("nodes", []):
        if _no_model_node(node):
            continue
        PROFILE.validate_profile_selection(node.get("profile_selection"), node.get("profile_demand"),
                                           profile=node.get("model_profile"), existing_versioned_stage=True)


def _seal_confirmation_mode():
    """SD-123: resolve the declared `confirmation.mode` to seal into the
    route, independent of `_seal_dispatch_defaults`'s return tuple.

    T-3: `_seal_dispatch_defaults` returns `(None, None, None)` early when
    the user config file is absent, and threading `confirmation_mode`
    through that tuple would seal `None` for exactly that user instead of
    the `hybrid` default -- the "configured but not applied" drift class.
    This helper is deliberately separate and always returns a real mode.
    """
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        return DEFAULTS.DEFAULT_CONFIRMATION_MODE
    try:
        cfg = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
    except (DEFAULTS.DefaultsConfigError, OSError, json.JSONDecodeError):
        return DEFAULTS.DEFAULT_CONFIRMATION_MODE
    return DEFAULTS.query_confirmation_mode(cfg)

def _seal_small_work_confirmation():
    """SD-136: seal `confirmation.small_work` (notice|card) the same way as
    `_seal_confirmation_mode`: absent/corrupt config -> the shipped default,
    never None."""
    config_path = DEFAULTS.default_config_path()
    if not os.path.exists(config_path):
        return DEFAULTS.DEFAULT_SMALL_WORK_CONFIRMATION
    try:
        cfg = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
    except (DEFAULTS.DefaultsConfigError, OSError, json.JSONDecodeError):
        return DEFAULTS.DEFAULT_SMALL_WORK_CONFIRMATION
    return DEFAULTS.query_small_work_confirmation(cfg)


def _seal_terminal_commit_support(validation_basis):
    """PRD §13.53.2: seal SD-120/121 activation as *checked support*.

    Activation is never an owner-remembered switch. The route declares support
    only when the runtime root it is bound to actually publishes the whole
    contract -- producer binding, terminal transaction, claim fence, exact
    finalize, the supervisor gate, and the registered lock-order table -- and
    the operator has not disabled it. Hash agreement alone can never open it,
    which is exactly the D-2 regression §13.36.6 rejected.

    Fail-closed: any unreadable root, missing surface, unparsable config or
    unexpected error yields `False`. The Claude adapter remains the only
    consumer of the flag, so declaring support claims nothing about Codex or
    OpenCode parity (§13.36.4).
    """
    try:
        config_path = DEFAULTS.default_config_path()
        if os.path.exists(config_path):
            try:
                config = DEFAULTS.load_and_validate(config_path, DEFAULTS.default_topology_path())
            except (DEFAULTS.DefaultsConfigError, OSError, json.JSONDecodeError):
                # A config that exists but does not validate is *not* the same
                # as no config. Falling back to `None` here would discard an
                # operator's `off` whenever some unrelated key in the same file
                # was stale or misspelled -- the switch would be ignored exactly
                # when the file is damaged, which is when it is most likely to
                # have been reached for. Refuse instead.
                return False
        else:
            config = None
        verdict = RUNTIME_SUPPORT.terminal_commit_support(
            (validation_basis or {}).get("runtime_root"), config)
        return bool(verdict.get("supported"))
    except Exception:
        return False

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
        "runtime_root_match": immutable_code_root_equivalent(registry_root, runtime_root),
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
                  registered_headless_evidence=None, slug=None,
                         route_origin="preset", shape=None, profile_demands=None,
                         explicit_profiles=None, campaign_key=None, parent_cycle_id=None, profile=None,
                         route_plan=None, frameless=False, frame_legs=2):
    registry=TOPO.load_registry(); TOPO.validate_registry(registry)
    recipe=TOPO.resolve_recipe(registry, capability, capability_mode)
    return _compile_from_recipe(
        registry, recipe, capability, capability_mode, requested_intensity, cwd, artifact_root,
        predicates=predicates, signals=signals, transport=transport,
        transport_evidence=transport_evidence, inline_reason=inline_reason,
        tracking=tracking, tracked_gate_evidence=tracked_gate_evidence,
        dispatch_evidence=dispatch_evidence,
        registered_headless_evidence=registered_headless_evidence, slug=slug,
        campaign_key=campaign_key, parent_cycle_id=parent_cycle_id,
        route_origin=route_origin, shape=shape, profile_demands=profile_demands,
        explicit_profiles=explicit_profiles, profile=profile,
        route_plan=route_plan, frameless=frameless, frame_legs=frame_legs)

# ---------------------------------------------------------------------------
# compose: the preset-free work route (SD-135).
#
# `compile` asks the caller to pick a registry recipe by entry-router trigger
# and to restate ~15 flags plus evidence files by hand. `compose` inverts that:
# the caller names the *shape* of the work (direct / solo / staged) and, for a
# staged shape, the stage subgraph it actually wants; every other flag and both
# evidence probes default from the checkout and the registry. The result goes
# through the SAME validator, sealer, verifier and guards as a preset route --
# composition changes route shape only (WORKFLOW §7 compose-on-demand).
# ---------------------------------------------------------------------------
COMPOSE_SHAPES = ("direct", "solo", "staged", "framed")
SHAPE_INTENSITY = {"direct": "direct", "solo": "quick", "staged": "standard", "framed": "standard"}
# The compiler-internal capability behind `--shape framed`. No person and no model names it; a
# route of any other shape can never seal it (`compose-shape-invalid`).
ROUTE_FRAME_CAPABILITY = TOPO.ROUTE_FRAME_CAPABILITY
INTENSITY_SHAPE = {"direct": "direct", "quick": "solo"}
ROUTE_ORIGINS = ("preset", "compose")
COMPOSE_DEFAULT_CAPABILITY = "autopilot-code"
# Fallback when the user's policy names no enabled harnesses (no file, unreadable):
# the shipped default enables all three.
COMPOSE_DEFAULT_CHILDREN = ("claude", "codex", "opencode")
SELECTION_PIN_TARGETS = ROUTE_AUTHORITY.PIN_TARGETS
SELECTION_PIN_CONTRACT_VERSION = 1
# The harness is split at the first ":" and the effort at the last "@", so a
# model id may itself contain ":" (a provider tag) but never "@", whitespace,
# "|" or ",".  Deliberately narrower than model_profile.SAFE_VALUE.
SELECTION_PIN_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$")
SELECTION_PIN_EFFORT = re.compile(r"^[a-z]+$")
COMPOSE_SPEC_CANDIDATES = ("spec/prd.md",)


def shape_for_intensity(effective):
    return INTENSITY_SHAPE.get(effective, "staged")


def parse_graph_spec(text, capabilities=None):
    """`execute,test,report` or `execute:dev/refactor,test` -> [(id, unit|None)].

    SD-165: when the first segment names a registered capability the token is a
    part id, `autopilot-lab:diagnose` or `autopilot-lab:metrics:qa/ml-debug`,
    and the returned key is `capability:stage`. Every other token parses as
    before.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("compose-graph-empty")
    rows = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            raise ValueError("compose-graph-empty-node")
        node_id, _, unit = item.partition(":")
        node_id = node_id.strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", node_id):
            raise ValueError(f"compose-graph-node-invalid:{node_id}")
        if unit and capabilities and node_id in capabilities:
            stage, _, unit = unit.partition(":")
            stage = stage.strip()
            if not re.fullmatch(r"[a-z][a-z0-9-]*", stage):
                raise ValueError(f"compose-graph-node-invalid:{node_id}:{stage}")
            node_id = f"{node_id}:{stage}"
        rows.append((node_id, unit.strip() or None))
    ids = [row[0] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("compose-graph-duplicate-node")
    return rows


def _compose_inputs(base_nodes, base_node, kept, find_input=None, alternates=None):
    """Keep the base node's declared inputs wherever they can still exist.

    An input stays when it is not produced by any recipe node (an external
    literal such as `task`/`spec`/`source`), when its producer is a kept
    node, or when it is a semantic token of the tree (`source-diff`, …) that
    no node has to author. An input whose only producer was dropped is
    removed -- the stage brief (`dispatch_stage_advance.render_stage_brief`)
    prints `inputs` verbatim, so a dropped node's file must not be promised.
    Nothing is added: the chain edge lives in `depends_on`, and a full-graph
    compose must yield exactly the preset's inputs. (Canary review round 1
    B1, round 2 M2.) SD-163: when `find_input(name)` finds a same-named prior
    output, the input stays and its source is returned in the second value.
    SD-165: `alternates(name, dropped_producers | None)` names further prior
    outputs to look for -- the relocated name of a borrowed part, or the part
    output the catalog maps an external input to. The input's own name is
    always tried first, so a sealed source replays through `sealed.get`.
    """
    producers = {}
    for candidate in base_nodes.values():
        for output in candidate.get("outputs") or []:
            producers.setdefault(output, set()).add(candidate["id"])
    inputs, sources = [], {}
    for item in base_node.get("inputs") or []:
        owners = producers.get(item)
        if owners is None or owners & kept or TOPO._is_semantic_output(item):
            if item not in inputs:
                inputs.append(item)
            names = alternates(item, None) if alternates is not None and owners is None else ()
        else:
            names = (item, *(alternates(item, owners) if alternates is not None else ()))
        if find_input is None:
            continue
        source = next((found for name in names if (found := find_input(name)) is not None), None)
        if source is not None:
            if item not in inputs:
                inputs.append(item)
            sources[item] = source
    return inputs or ["task"], sources


def _compose_graph_order_violation(base_nodes, ids):
    """First caller-order violation in a `--graph` selection (D9): a consumer
    named before a producer it transitively `depends_on` in the base recipe.
    Two nodes with no ancestor relationship either way (parallel/alternative
    siblings, e.g. `frame`/`frame-alternative`) may appear in either order --
    only `compose_subgraph_recipe` re-links edges in the CALLER's order, so
    nothing before this check ever refused a graph that silently reordered a
    real dependency (e.g. `test,execute` sealed with `test` running before the
    `execute` output it reads).

    Returns `(consumer, producer)` for the first violation found scanning
    `ids` left to right, or `None` when the order is consistent.
    """
    base_order = list(base_nodes)
    ancestor_cache: dict[str, set[str]] = {}

    def ancestors(node_id):
        cached = ancestor_cache.get(node_id)
        if cached is not None:
            return cached
        ancestor_cache[node_id] = set()  # defend against a cyclic recipe
        result: set[str] = set()
        for parent in base_nodes[node_id].get("depends_on") or []:
            result.add(parent)
            result |= ancestors(parent)
        ancestor_cache[node_id] = result
        return result

    position = {node_id: index for index, node_id in enumerate(ids)}
    for index, node_id in enumerate(ids):
        selected_ancestors = ancestors(node_id) & position.keys()
        for ancestor in sorted(selected_ancestors, key=base_order.index):
            if position[ancestor] > index:
                return node_id, ancestor
    return None


def _unarbitrated_auxiliary_legs(registry, group, anchor, consumers):
    """Suffixes of a borrowed group's auxiliary legs that no selected node arbitrates.

    Same rule `_validate_gate_contracts` applies to a recipe: a review anchor is
    merged by the conductor; a map or pipeline anchor needs exactly one direct
    consumer whose gate declares `auxiliary_arbiter`.
    """
    auxiliary = [leg["suffix"] for leg in group["legs"] if leg.get("leg_class") == "auxiliary"]
    if not auxiliary or anchor["kind"] == "review-worker":
        return []
    if anchor["kind"] == "pipeline-stage":
        consumers = [node for node in consumers if node["kind"] == "review-worker"]
    contracts = registry.get("completion_gate_contracts") or {}
    if len(consumers) == 1 and (contracts.get(consumers[0]["completion_gate"]) or {}).get(
            "auxiliary_arbiter") is True:
        return []
    return auxiliary


def _compose_unarbitrated_groups(registry, recipe):
    nodes = recipe["standard_plus"]["nodes"]
    for group in recipe["standard_plus"].get("parallel_groups", []):
        anchor = next(node for node in nodes if node["id"] == group["node"])
        consumers = [node for node in nodes if anchor["id"] in node.get("depends_on", [])]
        missing = _unarbitrated_auxiliary_legs(registry, group, anchor, consumers)
        if missing:
            yield {"id": group["id"], "reason": "auxiliary-arbiter-not-selected", "legs": missing}


def _complete_compose_group_consumers(registry, base, graph, find_input, **kwargs):
    """Restore missing safe registry consumers through the ordinary part assembler.

    Replaying the original selection remains possible: historical recipes do not
    ask for this completion, and new recipes record the inserted nodes rather
    than treating them as caller-selected stages.
    """
    recipe = compose_subgraph_recipe(registry, base, graph, find_input, **kwargs)
    original = json.loads(json.dumps(recipe["compose"]))
    additions = []
    contracts = registry.get("completion_gate_contracts") or {}
    while True:
        nodes = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
        completed = False
        pending = recipe["compose"].get("omitted_parallel_presets", []) + list(
            _compose_unarbitrated_groups(registry, recipe))
        for omission in pending:
            anchor = nodes.get(omission["id"])
            if anchor is None:
                declared = next((row for row in base["standard_plus"].get("parallel_groups", [])
                                 if row["id"] == omission["id"]), {})
                anchor = nodes.get(declared.get("node"))
            if anchor is None:
                continue
            part_id = anchor.get("part")
            found = TOPO.part_recipe(registry, part_id) if part_id else (base, anchor)
            if found is None:
                continue
            origin, source = found
            group = next((row for row in origin["standard_plus"].get("parallel_groups", [])
                          if row["node"] == source["id"]), None)
            if group is None or ORDER[kwargs["group_intensity"]] < ORDER[group["min_intensity"]]:
                continue
            reason = omission["reason"]
            if reason == "terminal-anchor" and anchor["kind"] == "pipeline-stage":
                reason = "review-consumer-not-selected"
            if reason not in ("review-consumer-not-selected", "auxiliary-arbiter-not-selected"):
                continue
            consumers = [node for node in origin["standard_plus"]["nodes"]
                         if source["id"] in node.get("depends_on", [])]
            if anchor["kind"] == "pipeline-stage":
                consumers = [node for node in consumers if node["kind"] == "review-worker"]
            if any(leg.get("leg_class") == "auxiliary" for leg in group["legs"]):
                consumers = [node for node in consumers
                             if contracts[node["completion_gate"]].get("auxiliary_arbiter") is True]
            if len(consumers) != 1:
                continue
            consumer = consumers[0]
            if (consumer["kind"] not in ("review-worker", "pipeline-stage")
                    or consumer.get("resource_class") != "normal"
                    or consumer.get("dispatch_depth") != 2 or consumer.get("commit_expected")
                    or any(worktree_mutating_scope(scope) for scope in consumer["write_scope"])
                    or (consumer.get("continuation") or {}).get("kind") != "inline-next"):
                continue
            key = f"{origin['capability']}:{consumer['id']}" if part_id else consumer["id"]
            consumer_id = key.replace(":", "-") if part_id else key
            if consumer_id in nodes or part_id and TOPO.resolve_shared_part(registry, base, key) is None:
                continue  # Never duplicate or move a caller-selected stage.
            selected = recipe["compose"]["graph"]
            anchor_key = part_id or anchor["id"]
            if anchor_key not in selected:
                continue
            augmented = list(selected)
            augmented.insert(augmented.index(anchor_key) + 1, key)
            overrides = recipe["compose"]["unit_overrides"]
            try:
                candidate = compose_subgraph_recipe(registry, base,
                    [(stage, overrides.get(stage)) for stage in augmented], find_input, **kwargs)
            except ValueError:
                continue  # A declared consumer may conflict with a selected dependency.
            if any(row["id"] == omission["id"]
                   for row in candidate["compose"].get("omitted_parallel_presets", []) + list(
                       _compose_unarbitrated_groups(registry, candidate))):
                continue  # Declared ordering/extra stages may make completion unsafe.
            additions.append({"id": consumer_id, "reason": reason, "anchors": [anchor["id"]],
                              "intensity": kwargs["group_intensity"]})
            recipe, completed = candidate, True
            break
        if not completed:
            break
    # Host groups historically kept auxiliary declarations even without their
    # arbiter and then failed topology validation. Strong+ uses the same safe
    # omission as borrowed groups when the declared consumer cannot be restored.
    missing = list(_compose_unarbitrated_groups(registry, recipe))
    for omission in missing:
        group = next(row for row in recipe["standard_plus"]["parallel_groups"] if row["id"] == omission["id"])
        group["legs"] = [leg for leg in group["legs"] if leg["suffix"] not in omission["legs"]]
        group["width_by_intensity"] = {
            tier: min(width, len(group["legs"])) for tier, width in group["width_by_intensity"].items()}
        recipe["compose"].setdefault("omitted_parallel_presets", []).append(omission)
    if missing:
        # Assembly metadata only, so this new omission can replay without
        # reinterpreting historical graphs that never requested completion.
        recipe["compose"]["group_completion_intensity"] = kwargs["group_intensity"]
    if additions:
        recipe["compose"]["graph"] = original["graph"]
        recipe["compose"]["unit_overrides"] = original["unit_overrides"]
        recipe["compose"]["auto_completed_nodes"] = additions + recipe["compose"].get("auto_completed_nodes", [])
    if (additions or missing) and kwargs.get("preserve_base_dependencies"):
        recipe["compose"]["preserve_base_dependencies"] = True
    return recipe


def compose_subgraph_recipe(registry, base_recipe, graph_spec, find_input=None, *, preserve_base_dependencies=False,
                            extra_stages=None, group_intensity=None, complete_group_consumers=False):
    """Cut the caller's stage subgraph out of the capability's own recipe.

    The nodes keep their unit, kind, gate, write scope, profile and permissions;
    only the edges change: the subgraph is re-linked in the caller's order, the
    last node becomes the terminal, a human gate a kept node raised is rebound
    to the entry of the node that now follows it (dropped when nothing
    follows), and a parallel group survives only when its anchor is kept and is
    not the new terminal (G6). Strong+ may append a side-effect-free owner close
    to keep eligible terminal review/map groups. An absent group_intensity
    replays the historical assembly. Validation stays with `_validate_recipe` --
    this function never re-implements a rule, it only assembles.

    SD-165: a graph key is a host stage id, an optional catalog part of the host
    recipe, or a registered shareable `capability:stage`. A borrowed part keeps
    its unit, kind and gate and is relocated under `parts/<capability>/<stage>/`
    inside the host's own artifact scope; the name mapping is sealed as
    `part_io`. A graph that names none of these assembles exactly as before.
    """
    if complete_group_consumers and group_intensity is not None and ORDER[group_intensity] >= ORDER["strong"]:
        return _complete_compose_group_consumers(registry, base_recipe, graph_spec, find_input,
            preserve_base_dependencies=preserve_base_dependencies,
            extra_stages=extra_stages, group_intensity=group_intensity)
    host = base_recipe["capability"]
    base_nodes = {node["id"]: node for node in base_recipe["standard_plus"]["nodes"]}
    optional = dict(TOPO.recipe_optional_parts(registry, base_recipe))
    # `view` is what a key may resolve to locally; `order` carries the
    # precedence edges used only by the caller-order check (sealed `depends_on`
    # is always rewritten in the caller's order below).
    view = dict(base_nodes)
    view.update({stage: row["optional"]["node"] for stage, row in optional.items()})
    order = {node_id: {"depends_on": list(node.get("depends_on") or [])} for node_id, node in view.items()}
    for stage, row in optional.items():
        order[stage]["depends_on"] = list(row["optional"]["after"])
        for follower in row["optional"]["before"]:
            order[follower]["depends_on"].append(stage)
    rows, unknown = [], []
    for key, unit in graph_spec:
        capability, separator, stage = key.partition(":")
        if separator and capability == host and stage in view:
            key, separator = stage, ""
        if not separator:
            if key not in view:
                unknown.append(key)
                continue
            rows.append((key, key, unit, None))
            continue
        part = TOPO.resolve_shared_part(registry, base_recipe, key)
        if part is None:
            unknown.append(key)
            continue
        rows.append((key, f"{capability}-{stage}", unit, part))
    # A plan's extra stages (`extra_stages`): each is shaped on a catalog stage of the same unit and
    # runs right after the stage it names; one that cannot be shaped or placed is left to the owner.
    placed = []
    for stage in extra_stages or ():
        part = TOPO.plan_stage_part(registry, base_recipe, stage)
        node_id = f"plan-{stage.get('id')}"
        after = [index for index, row in enumerate(rows) if row[1] == stage.get("after")]
        if part is None or not after or after[0] == len(rows) - 1 or node_id in {row[1] for row in rows}:
            continue
        rows.insert(after[0] + 1, (part["part"], node_id, None, part))
        placed.append({key: stage[key] for key in ("id", "unit", "after", "verify") if key in stage})
        view[node_id] = part["node"]
        order[node_id] = {"depends_on": [stage["after"]]}
        follower = rows[after[0] + 2][1]
        order[follower] = {"depends_on": list(order.get(follower, {}).get("depends_on") or []) + [node_id]}
    if unknown:
        raise ValueError(
            "compose-graph-unknown-node:" + ",".join(unknown)
            + " (available: " + ",".join(view) + ")"
            + " (run `capability-route.py stages --capability "
            + base_recipe["capability"] + "` to list valid stage ids)"
        )
    ids = [row[1] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("compose-graph-duplicate-node")
    source_of = {node_id: (part["node"] if part else view[node_id]) for _, node_id, _, part in rows}
    for _, node_id, _, part in rows:
        if part and not part.get("plan_stage"):
            borrowed_ids = {p["stage"]: other for _, other, _, p in rows
                            if p and p["recipe"] is part["recipe"]}
            order[node_id] = {"depends_on": [borrowed_ids[dep] for dep in part["node"].get("depends_on") or []
                                             if dep in borrowed_ids]}
    violation = _compose_graph_order_violation(order, ids)
    if violation:
        consumer, producer = violation
        raise ValueError(
            f"compose-graph-order:{consumer}-before-{producer} "
            "(recipe order: " + ",".join(view) + ")"
        )
    def gate_group(node_id):
        base = source_of[node_id]
        continuation = base.get("continuation") or {}
        if continuation.get("kind") == "human-gate":
            return continuation["gate"], tuple(base.get("depends_on", []))
        return None

    host_kept = {node_id for _, node_id, _, part in rows if part is None}
    # The catalog's input-name lookup fills a SUBGRAPH; a full recipe graph and a
    # preset never ask (stage-dispatch 13.64.3: "in a partial graph").
    strict = len(host_kept & set(base_nodes)) < len(base_nodes)
    selected_outputs = {out for node_id in ids for out in source_of[node_id].get("outputs") or []}

    def producers_in(capability, name):
        stages = [node["id"] for recipe in registry["recipes"] if recipe["capability"] == capability
                  for node in recipe["standard_plus"]["nodes"] if name in (node.get("outputs") or [])]
        stages += [part_id.partition(":")[2] for part_id, row in (TOPO.part_catalog(registry).get("parts") or {}).items()
                   if part_id.partition(":")[0] == capability and isinstance(row.get("optional"), dict)
                   and name in (row["optional"]["node"].get("outputs") or [])]
        return sorted(set(stages))

    scope = json.loads(json.dumps(base_recipe["artifact_scope"]))
    nodes = []
    overrides = {}
    used_parts = set()
    meta = {}
    for index, (key, node_id, unit, part) in enumerate(rows):
        node = json.loads(json.dumps(source_of[node_id]))
        part_id = key if part else f"{host}:{node_id}"
        catalog_row = TOPO.part_row(registry, part_id)
        capability = part["capability"] if part else host
        if unit:
            choices = node.get("unit_choices")
            widened = catalog_row.get("unit_choices")
            if choices is None and widened and unit != node.get("unit") and unit in widened:
                # A widened choice is merged only when the graph actually picks it.
                choices = node["unit_choices"] = list(widened)
                used_parts.add(part_id)
            if choices is not None and unit not in choices:
                raise ValueError(
                    f"compose-unit-not-in-choices:{node_id}:{unit} (choices: {','.join(choices)})"
                )
            if node.get("kind") in ("capability-owner", "resource-runner"):
                raise ValueError(f"compose-unit-override-reserved:{node_id}")
            node["unit"] = unit
            node["role"] = TOPO._unit_frontmatter(unit)["role"]
            overrides[key] = unit
        originals = list(node.get("outputs") or [])
        moved = {}
        if part:
            kind = node["kind"]
            def relocate(path, _kind=kind, _part=part):
                return TOPO.relocate_part_path(_part["scope"], _kind, _part["capability"], _part["stage"], path)
            node["id"] = node_id
            node["write_scope"] = [target for path in node["write_scope"] for target in relocate(path)]
            for out in originals:
                if not TOPO._is_semantic_output(out):
                    moved[out] = relocate(out)[0]
            node["outputs"] = [moved.get(out, out) for out in originals]
            node["part"] = key
            if part.get("plan_stage"):
                node["plan_stage"] = dict(part["plan_stage"])
            if catalog_row.get("start_approval"):
                node["start_approval"] = catalog_row["start_approval"]
            if part["merged_anchor"]:
                scope[part["merged_anchor"]] = part["scope"][part["merged_anchor"]]
            used_parts.add(key)
        elif node_id in optional:
            used_parts.add(part_id)
        previous = nodes[-1] if nodes else None
        if preserve_base_dependencies:
            if part:
                borrowed_ids = {p["stage"]: other for _, other, _, p in rows
                                if p and p["recipe"] is part["recipe"]}
                declared_dependencies = (part["node"].get("depends_on") or [])
                node["depends_on"] = [borrowed_ids[dep] for dep in declared_dependencies if dep in borrowed_ids]
            else:
                declared_dependencies = order.get(node_id, {}).get("depends_on") or []
                node["depends_on"] = [dep for dep in declared_dependencies if dep in ids]
        else:
            node["depends_on"] = [previous["id"]] if previous else []
        if not preserve_base_dependencies and previous and gate_group(previous["id"]):
            group = gate_group(previous["id"])
            if gate_group(node_id) == group:
                # Independent raisers share their predecessor. Serializing
                # them would make the first gate block its own second raiser.
                node["depends_on"] = list(previous["depends_on"])
            else:
                raisers = []
                for prior in reversed(nodes):
                    if gate_group(prior["id"]) != group:
                        break
                    raisers.append(prior["id"])
                node["depends_on"] = list(reversed(raisers))
        input_names = catalog_row.get("input_names") or {}
        if part:
            origin_nodes = {n["id"]: n for n in part["recipe"]["standard_plus"]["nodes"]}
            kept = {p["stage"] for _, _, _, p in rows if p and p["recipe"] is part["recipe"]}
        else:
            origin_nodes, kept = base_nodes, set(ids)

        def alternates(name, owners, _capability=capability, _names=input_names):
            if owners is not None:  # every recipe producer was dropped: also look under the borrowed name
                return tuple(f"parts/{_capability}/{stage}/{name}" for stage in sorted(owners))
            target = _names.get(name)
            if not target or not strict or target in selected_outputs:
                return ()
            return (name, target, *(f"parts/{_capability}/{stage}/{target}"
                                    for stage in producers_in(_capability, target)))

        if part and part.get("plan_stage"):
            # A plan's stage reads what the stage before it wrote.
            node["inputs"], sources = [out for out in (previous or {}).get("outputs") or []
                                       if not TOPO._is_semantic_output(out)], {}
        else:
            node["inputs"], sources = _compose_inputs(origin_nodes, source_of[node_id], kept, find_input, alternates)
        if sources:
            node["input_sources"] = sources
            if set(sources) & set(input_names):
                used_parts.add(part_id)
        node.pop("terminal", None)
        node.pop("terminal_gate", None)
        node.pop("continuation", None)
        node.pop("parallel_group", None)
        nodes.append(node)
        meta[node_id] = {"part_id": part_id, "originals": originals, "moved": moved,
                         "input_names": {} if part and part.get("plan_stage") else input_names,
                         "catalog": bool(part) or node_id in optional}
    # SD-165 name mapping, sealed as `part_io` (absent on a route that uses no catalog part).
    produced = {}
    for index, node in enumerate(nodes):
        info = meta[node["id"]]
        io = {}
        inputs = []
        for name in node["inputs"]:
            target = produced.get(name, name)
            if target != name:  # the nearest producer is a borrowed part: read its relocated path
                io[name] = target
            if target not in inputs:
                inputs.append(target)
        for name, target in info["input_names"].items():
            if name in inputs and target in produced:  # catalog name mapping to a selected producer
                io[name] = produced[target]
                used_parts.add(info["part_id"])
        if index and meta[nodes[index - 1]["id"]]["catalog"]:
            # A catalog part's result reaches the next stage brief even when that
            # stage's recipe inputs never named it.
            handed = [out for out in nodes[index - 1]["outputs"] if not TOPO._is_semantic_output(out)]
            if handed and not set(handed) & (set(inputs) | set(io.values())):
                inputs.extend(handed)
        node["inputs"] = inputs
        io.update(info["moved"])
        if io:
            node["part_io"] = io
        for original, sealed in zip(info["originals"], node["outputs"]):
            if not TOPO._is_semantic_output(original):
                produced[original] = sealed
    declared_groups = [(group, False) for group in base_recipe["standard_plus"].get("parallel_groups") or []]
    for _, node_id, _, part in rows:
        if part:  # a borrowed anchor brings its own group, re-keyed to the borrowed node id
            declared_groups.extend(
                (dict(json.loads(json.dumps(group)), id=node_id, node=node_id), True)
                for group in part["recipe"]["standard_plus"].get("parallel_groups") or []
                if group["node"] == part["stage"])
    if preserve_base_dependencies:
        terminal_ids = {node["id"] for node in nodes
                        if not any(node["id"] in (other.get("depends_on") or []) for other in nodes)}
    else:
        terminal_ids = {nodes[-1]["id"]}
    auto_completed = []
    closing_boundaries = set()
    if group_intensity is not None and ORDER[group_intensity] >= ORDER["strong"]:
        eligible = {group["node"] for group, _ in declared_groups
                    if ORDER[group_intensity] >= ORDER[group["min_intensity"]]}
        anchors = [node for node in nodes if node["id"] in terminal_ids & eligible
                   and node["kind"] in ("review-worker", "map-worker")]
        if anchors:
            closing_boundaries = set(terminal_ids)
            owner_id, suffix = "owner-close", 1
            occupied = {node["id"] for node in nodes} | {out for node in nodes for out in node["outputs"]}
            while owner_id in occupied or owner_id + ".md" in occupied:
                suffix += 1
                owner_id = f"owner-close-{suffix}"
            terminals = [node for node in nodes if node["id"] in terminal_ids]
            owner = {
                "id": owner_id, "kind": "capability-owner", "unit": "_kernel/owner",
                "depends_on": [node["id"] for node in terminals], "role": "deep orchestrator",
                "inputs": list(dict.fromkeys(item for node in terminals
                                             for item in node["inputs"] + node["outputs"])),
                "outputs": [owner_id + ".md"], "write_scope": [owner_id + ".md"],
                "resource_class": "normal", "completion_gate": "compose-owner-close",
                "dispatch_depth": 1, "model_profile": registry["owner_profile_by_intensity"]["standard"],
                "fallback_hops": ["same-harness-headless", "cross-harness-headless", "inline"],
                "advance_class": "model-required", "model_required_reason": "terminal-report",
                "commit_expected": False,
            }
            nodes.append(owner)
            source_of[owner_id] = owner
            terminal_ids = {owner_id}
            auto_completed.append({"id": owner_id, "reason": "terminal-anchor",
                                   "anchors": [node["id"] for node in anchors],
                                   "intensity": group_intensity})
    for terminal in nodes:
        if terminal["id"] not in terminal_ids:
            continue
        terminal["terminal"] = True
        terminal["terminal_gate"] = terminal["completion_gate"]
        if terminal.get("advance_class") != "model-required":
            terminal["advance_class"] = "model-required"
            terminal["model_required_reason"] = "terminal-report"
    # Human gates: a base node whose continuation was `human-gate G` keeps G
    # only when a graph node follows it; G is rebound to that node's entry.
    # One binding per DISTINCT gate, not one per raiser. Sibling nodes can raise
    # the same gate -- the frame pair both continue on `frame-review` -- and a
    # second binding for the same gate is refused by
    # `capability_topology.py:734-737` ("every declared human gate must bind to
    # exactly one node"), which made a compose graph naming both frame legs
    # impossible at any capability. The anchor is the node following the LAST
    # raiser, so the gate opens only after every raiser has run.
    bindings, gates = [], []
    gate_anchor: dict[str, str] = {}
    for index, node in enumerate(nodes):
        if node["id"] in terminal_ids:
            continue
        base = source_of[node["id"]]
        continuation = base.get("continuation") or {}
        # A closing summary is not the omitted transaction: the old terminal
        # boundary still drops its approval rather than rebinding it to the close.
        if continuation.get("kind") == "human-gate" and node["id"] not in closing_boundaries:
            gate = continuation["gate"]
            if gate not in gate_anchor:
                gates.append(gate)
            gate_anchor[gate] = nodes[index + 1]["id"]
            node["continuation"] = {"kind": "human-gate", "gate": gate}
        elif node.get("kind") == "resource-runner":
            node["continuation"] = {"kind": "supervised"}
        else:
            node["continuation"] = {"kind": "inline-next"}
    bindings.extend(
        {"gate": gate, "node": gate_anchor[gate], "position": "entry"} for gate in gates
    )
    # A gate declared on the entry of a base SOURCE node (no predecessor raises
    # it, e.g. autopilot-spec `intent-confirmation` on `research`) is kept
    # verbatim when that node is kept: it is parent-owned, not a continuation.
    kept_ids = set(ids)
    for row in base_recipe.get("human_gate_bindings") or []:
        node_id = row.get("node")
        if (row.get("position") == "entry" and node_id in kept_ids and node_id in base_nodes
                and not (base_nodes[node_id].get("depends_on") or [])
                and row.get("gate") not in gates):
            bindings.append({"gate": row["gate"], "node": node_id, "position": "entry"})
            gates.append(row["gate"])
    groups, omitted_groups = [], []
    for group, borrowed in declared_groups:
        anchor = next((n for n in nodes if n["id"] == group["node"]), None)
        if anchor is None:
            continue
        if anchor.get("terminal") is True:
            omitted_groups.append({"id": group["id"], "reason": "terminal-anchor"})
            continue
        consumers = [n for n in nodes if anchor["id"] in n.get("depends_on", [])]
        if anchor["kind"] == "pipeline-stage" and not any(
                n["kind"] == "review-worker" for n in consumers):
            # The preset's fan-out requires a review consumer. A caller who
            # selected a smaller graph did not select that fan-out obligation.
            omitted_groups.append({"id": group["id"], "reason": "review-consumer-not-selected"})
            continue
        group = json.loads(json.dumps(group))
        dropped = _unarbitrated_auxiliary_legs(registry, group, anchor, consumers) if borrowed else []
        if dropped:
            # Peer legs stay; an auxiliary leg nobody arbitrates is left out.
            group["legs"] = [leg for leg in group["legs"] if leg["suffix"] not in dropped]
            group["width_by_intensity"] = {
                tier: min(width, len(group["legs"])) for tier, width in group["width_by_intensity"].items()}
            omitted_groups.append({"id": group["id"], "reason": "auxiliary-arbiter-not-selected",
                                   "legs": dropped})
        groups.append(group)
    extensions = [
        json.loads(json.dumps(row))
        for row in base_recipe.get("conditional_extensions") or []
        if set(row.get("after") or []) <= kept_ids
        and {ref.get("node") for ref in row.get("source_outputs") or []} <= kept_ids
    ]
    recipe = {
        "capability": base_recipe["capability"],
        "modes": list(base_recipe["modes"]),
        "topology_class": base_recipe["topology_class"],
        "direct_predicates": list(base_recipe["direct_predicates"]),
        "promotion_signals": list(base_recipe["promotion_signals"]),
        "artifact_scope": scope,
        "quick": json.loads(json.dumps(base_recipe["quick"])),
        "standard_plus": {
            "topology": base_recipe["standard_plus"].get("topology", base_recipe["topology_class"]),
            "owner_dispatch_depth": base_recipe["standard_plus"]["owner_dispatch_depth"],
            "max_dispatch_depth": max(
                (node.get("dispatch_depth", 0) for node in nodes if node.get("kind") != "resource-runner"),
                default=0,
            ),
            "nodes": nodes,
        },
        "conditional_extensions": extensions,
        "completion_gates": sorted({node["completion_gate"] for node in nodes}),
        "human_gates": sorted(set(gates)),
        "human_gate_bindings": bindings,
        "resume_retry_boundaries": list(ids) + [row["id"] for row in auto_completed],
        "compose": {"origin": "compose", "shape": "staged",
                    "graph": [row[0] for row in rows if not (row[3] and row[3].get("plan_stage"))],
                    "unit_overrides": overrides, "base_capability": base_recipe["capability"]},
    }
    if placed:
        recipe["compose"]["extra_stages"] = placed
    if used_parts:
        # Catalog rows this recipe derives from; `capability_registry_digest` reads it.
        recipe["compose"]["parts"] = sorted(used_parts)
    if groups:
        recipe["standard_plus"]["parallel_groups"] = groups
    if omitted_groups:
        recipe["compose"]["omitted_parallel_presets"] = omitted_groups
    if auto_completed:
        recipe["compose"]["auto_completed_nodes"] = auto_completed
        if preserve_base_dependencies:
            recipe["compose"]["preserve_base_dependencies"] = True
    return recipe


def _versioned_subgraph(registry, recipe):
    """Only an exact registry-derived subgraph may inherit legacy demands."""
    meta = recipe.get("compose") or {}
    if not isinstance(meta, dict) or not meta.get("base_capability"):
        return False
    try:
        base = TOPO.resolve_recipe(registry, meta["base_capability"], recipe["modes"][0])
        overrides = meta.get("unit_overrides", {})
        graph = [(node_id, overrides.get(node_id)) for node_id in meta["graph"]]
        sealed = {}
        for node in recipe["standard_plus"]["nodes"]:
            if isinstance(node.get("input_sources"), dict):
                for name, source in node["input_sources"].items():
                    sealed.setdefault(name, source)
        auto = meta.get("auto_completed_nodes") or []
        expected = compose_subgraph_recipe(registry, base, graph, sealed.get if sealed else None,
                                           extra_stages=meta.get("extra_stages"),
                                           group_intensity=meta.get("group_completion_intensity") or (auto[0]["intensity"] if auto else None),
                                           complete_group_consumers=bool(meta.get("group_completion_intensity")) or any(
                                               row["reason"] != "terminal-anchor" for row in auto),
                                           preserve_base_dependencies=meta.get("preserve_base_dependencies", False))
        if expected == recipe:
            return True
        # Pre-notice routes already had this identical graph, but did not
        # record terminal omissions. Preserve their original bytes/profiles.
        omissions = expected["compose"].get("omitted_parallel_presets", [])
        remaining = [row for row in omissions if row["reason"] != "terminal-anchor"]
        if remaining:
            expected["compose"]["omitted_parallel_presets"] = remaining
        else:
            expected["compose"].pop("omitted_parallel_presets", None)
        return expected == recipe
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return False


def _validate_compose_owner_close(registry, recipe, intensity=None):
    """Automatic closing/consumer nodes must be the exact registry-derived assembly."""
    carries = any(node.get("completion_gate") == "compose-owner-close"
                  for node in recipe["standard_plus"]["nodes"])
    meta = recipe.get("compose") or {}
    auto = meta.get("auto_completed_nodes")
    selected = {key.replace(":", "-") for key in meta.get("graph", [])}
    # Consumer completion is strong+ only; lower intensities retain the
    # existing profile-demand validation of an inexact subgraph marker.
    extra = intensity not in ("direct", "quick", "standard") and bool(meta.get("graph")) and any(
        node["id"] not in selected and not node.get("plan_stage")
        for node in recipe["standard_plus"]["nodes"])
    completion_intensity = meta.get("group_completion_intensity")
    nodes = {node["id"]: node for node in recipe["standard_plus"]["nodes"]}
    host_omission = any(row["reason"] == "auxiliary-arbiter-not-selected"
                        and row["id"] in nodes and not nodes[row["id"]].get("part")
                        for row in meta.get("omitted_parallel_presets", []))
    if carries or auto or extra or completion_intensity or host_omission:
        if ((carries or auto or extra) and not auto
                or carries != any(row["reason"] == "terminal-anchor" for row in auto or [])
                or not _versioned_subgraph(registry, recipe)
                or intensity is not None and (any(row["intensity"] != intensity for row in auto or [])
                                             or completion_intensity and completion_intensity != intensity)):
            raise ValueError("compose-owner-close-differs-from-assembly")


def _compose_default_jobs():
    inherited = os.environ.get("AGENT_DISPATCH_JOBS")
    if inherited:
        return Path(inherited)
    return stable_state_root(os.environ) / "jobs.log"


def _compose_readiness(cwd, jobs, parent_harness, children, *, gpu_route=None):
    spec = importlib.util.spec_from_file_location(
        "hearting_dispatch_readiness", ROOT / "utilities" / "dispatch-readiness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.generate(
            worktree=Path(cwd), jobs=Path(jobs),
            owner_harnesses=[parent_harness], child_harnesses=list(children),
            **({"codex_execution_selection": GPU_SANDBOX.select(gpu_route)}
               if gpu_route and GPU_SANDBOX.gpu_resource_nodes(gpu_route) else {}),
        )
    except module.ReadinessError as exc:
        raise ValueError(f"compose-readiness-unavailable:{exc}") from exc


SPEC_READ_SHARED_PREFIX = "compose-auto: shared spec present, read "
# compose and compile record the same tracked-gate defaults when the caller states none.
DEFAULT_DRIFT_VERDICT = "no-spec-impact: compose default (caller asserted no spec-significant change)"
DEFAULT_ARTIFACT_GUARD = "compose-prechecked"


def compose_spec_read(cwd, artifact_root, explicit):
    """`auto` is honest, not permissive: with no spec candidate it records the
    absence; with a `spec/prd.md` present it is satisfied when this session's read
    hook recorded reading that file unchanged, and otherwise refuses and names the
    file the caller must read (or assert with `--spec-read <source>`). The spec-read gate is
    a real invariant (WORKFLOW §7.0); compose only removes the boilerplate case.
    A shared-spec `prd.md` never blocks compose: the record says which file is
    there to read and `compose_card` passes that path on as one line."""
    if explicit not in (None, "", "auto"):
        return {"satisfied": explicit.lower() not in ("0", "false", "no"), "source": explicit}
    present, shared = [], []
    for root in (Path(cwd), Path(artifact_root)):
        for rel in COMPOSE_SPEC_CANDIDATES:
            candidate = root / rel
            if candidate.is_file():
                present.append(str(candidate))
        shared.extend(_compose_shared_spec_prds(root))
    unread = sorted(set(present) - set(_spec_reads_recorded(present, artifact_root) if present else ()))
    if unread:
        raise ValueError("compose-spec-read-required:" + ",".join(unread))
    if present:
        return {"satisfied": True, "source": SPEC_READ_MARKER_PREFIX + ",".join(sorted(set(present)))}
    if shared:
        return {"satisfied": True, "source": SPEC_READ_SHARED_PREFIX + ",".join(sorted(set(shared)))}
    return {"satisfied": True, "source": "compose-auto: no spec/prd.md under cwd or artifact root"}


SPEC_READ_MARKER_PREFIX = "spec-read-marker: "


def _spec_reads_recorded(paths, artifact_root):
    """The `paths` this session has read unchanged, as the read hook recorded them
    (`hooks/spec-read-marker.sh`: `<agent home>/.spec-grounding/<session>__<root key>`
    holding the file's mtime at the read). Only a root-level `spec/prd.md` of the
    artifact root has such a key; anything unreadable counts as not read."""
    try:
        session = ROUTE_AUTHORITY.default_parent_session_id()
    except ValueError:                      # an ambiguous caller has no reads of its own
        return []
    if not session:
        return []
    root = Path(artifact_root)
    key = str(root.parent).replace("/", "_").replace(" ", "_")
    marker = Path(resolve_agent_home()) / ".spec-grounding" / f"{session}__{key}"
    try:
        recorded = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return []
    read = []
    for path in paths:
        try:
            if Path(path) == root / "spec" / "prd.md" and int(Path(path).stat().st_mtime) == recorded:
                read.append(path)
        except OSError:
            continue
    return read


def _compose_spec_read_notice(route):
    """One `[경로]` card line when compose found a shared spec it did not ask the caller to read."""
    source = ((route.get("tracked_gate_evidence") or {}).get("spec_read") or {}).get("source")
    if isinstance(source, str) and source.startswith(SPEC_READ_SHARED_PREFIX):
        return "  공유 spec 있음 — 읽을 경로: " + source[len(SPEC_READ_SHARED_PREFIX):]
    return None


def _compose_shared_spec_prds(root):
    """Latest `prd.md` of each admitted shared spec (`shared/spec/<ref>/revisions/<rrev>/`,
    CORE artifact layout); older revisions are history, not what the caller must read."""
    found = []
    for reference in sorted((root / "shared" / "spec").glob("*/reference.json")):
        try:
            latest = json.loads(reference.read_text(encoding="utf-8")).get("latest_revision_id")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(latest, str) or not re.fullmatch(r"rrev_[0-9a-f]+", latest):
            continue
        candidate = reference.parent / "revisions" / latest / "prd.md"
        if candidate.is_file():
            found.append(str(candidate))
    return found


def _parse_selection_pins(values, owner=None):
    """Parse repeated `--pin <target>=<harness>[:<model>[@<effort>]]` values.

    Returns `{target: {"harness", "model", "effort"}}` for the targets that were
    given. `--owner H` is the same statement as `--pin owner=H`; naming two
    different harnesses for the owner is a contradiction in the input.  Only a
    value that cannot be read at all is refused -- a route without pins never
    reaches this function with anything to refuse.
    """
    pins = {}
    for raw in values or ():
        target, sep, rest = str(raw).partition("=")
        if not sep or target not in SELECTION_PIN_TARGETS:
            raise ValueError(
                f"compose-pin-invalid:{raw!r} (use <target>=<harness>[:<model>[@<effort>]], "
                f"target one of {','.join(SELECTION_PIN_TARGETS)})")
        if target in pins:
            raise ValueError(f"compose-pin-invalid:{target} given twice")
        harness, colon, remainder = rest.partition(":")
        if harness not in DEFAULTS.DISPATCHABLE_HARNESSES:
            raise ValueError(
                f"compose-pin-invalid:{raw!r} unknown harness {harness!r} "
                f"(one of {','.join(sorted(DEFAULTS.DISPATCHABLE_HARNESSES))})")
        model = effort = None
        if colon:
            if not remainder:
                raise ValueError(f"compose-pin-invalid:{raw!r} empty model")
            model, at, tail = remainder.rpartition("@")
            if not at:
                model, effort = remainder, None
            elif not model or not tail:
                raise ValueError(f"compose-pin-invalid:{raw!r} empty model or effort around '@'")
            else:
                effort = tail
            if not SELECTION_PIN_MODEL.fullmatch(model):
                raise ValueError(
                    f"compose-pin-invalid:{raw!r} model must start with a letter or digit and contain only letters, digits and ._/:-")
            if effort is not None and not SELECTION_PIN_EFFORT.fullmatch(effort):
                raise ValueError(f"compose-pin-invalid:{raw!r} effort must be lowercase letters")
        pins[target] = {"harness": harness, "model": model, "effort": effort}
    if owner:
        if "owner" in pins and pins["owner"]["harness"] != owner:
            raise ValueError(
                f"compose-pin-owner-conflict:--owner {owner} vs --pin owner={pins['owner']['harness']}")
        pins.setdefault("owner", {"harness": owner, "model": None, "effort": None})
    return pins


def _turn_peer_source(harness, session, cwd):
    """Where the current turn's instruction came from, as far as the runtime knows.

    A peer message received at this session's latest prompt is `peer:<sender>·<ref>`.
    Anything else is `unattributed`: a typed prompt is not recorded anywhere a launcher
    can read, so it is never guessed to be the user's."""
    try:
        import calendar
        import time
        import session_tidy
        turn = session_tidy.read_json(session_tidy._prompt_seq_path(
            session_tidy.resolve_seat(harness, cwd or None, os.environ, session, "")))
        if not isinstance(turn, dict) or turn.get("sid") != session:
            return "unattributed"
        started = float(turn["at"]) - 5.0
        spec = importlib.util.spec_from_file_location("pin_change_peer_message", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        latest = None
        for row in peer._iter_records(since_hours=24):
            to = row.get("to") or {}
            if (row.get("kind") != "notice" or (row.get("delivery") or {}).get("status") != "received"
                    or (to.get("harness"), to.get("session_id")) != (harness, session)):
                continue
            stamp = calendar.timegm(time.strptime(row.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"))
            if stamp >= started and (latest is None or stamp >= latest[0]):
                latest = (stamp, row)
        if latest is None:
            return "unattributed"
        sender = latest[1].get("from") or {}
        name = sender.get("name") or sender.get("session_id") or sender.get("harness") or "?"
        return f"peer:{name}·{latest[1].get('transfer_ref') or latest[1].get('message_id') or '-'}"
    except Exception:  # noqa: BLE001 -- an unknown source is recorded as unknown
        return "unattributed"


def _pin_change_probe(route, jobs, target, harness, caller):
    """`(tuples, candidates)` a pin moved to `harness` launches with, probed by the runtime now.

    owner: from the new owner harness to the route's children. worker: from the owner
    harness in force to the new worker harness. frame: from this depth-0 session to the new
    frame harness. A quick route (one owner, its registered-headless candidates) is probed for
    candidates instead; it starts no stage, so a worker change there needs nothing probed.
    Nothing supported for the new pin is refused before anything is recorded."""
    import route_authority as RA
    view = RA.route_in_force(route)
    quick = route.get("effective_intensity") == "quick" or route.get("registered_headless_candidates") is not None
    rows = [row for row in (view.get("dispatch_evidence") or {}).get("tuples") or [] if isinstance(row, dict)]
    if quick and target == "worker":
        return [], []
    if target == "owner" or quick:
        children = sorted({row.get("child_harness") for row in rows if row.get("child_harness")}) \
            or list(_compose_default_children())
        probe = _compose_readiness(route["cwd"], jobs, harness, children, gpu_route=route)
        tuples = [] if quick else [row for row in probe.get("tuples") or [] if row.get("parent_harness") == harness]
        candidates = [row for row in probe.get("candidates") or [] if row.get("harness") == harness] if quick else []
        ready = (any(row.get("status") == "supported" for row in candidates) if quick else
                 any(row.get("status") == "supported" and row.get("launch_authority") == "conductor" for row in tuples))
    else:
        if target == "worker":
            owner = ((view.get("selection_pins") or {}).get("owner") or {}).get("harness")
            parents = [owner] if owner else sorted({row.get("parent_harness") for row in rows
                                                    if row.get("launch_authority") == "conductor"
                                                    and row.get("parent_harness")})
        else:
            parents = [caller] if caller else sorted({row.get("parent_harness") for row in rows
                                                      if row.get("parent_harness")})
        tuples, candidates = [], []
        for parent in parents:
            probe = _compose_readiness(route["cwd"], jobs, parent, [harness], gpu_route=route)
            tuples += [row for row in probe.get("tuples") or []
                       if row.get("parent_harness") == parent and row.get("child_harness") == harness]
        ready = any(row.get("status") == "supported"
                    and (target == "frame" or row.get("launch_authority") == "conductor") for row in tuples)
    if not ready:
        raise ValueError(f"start-pin-{target}-unready:{harness} (the runtime probed no supported launch "
                         f"for that {target} harness in this worktree; the {target} pin is unchanged)")
    return tuples, candidates


def _change_pins(route, jobs, values):
    """`start --pin owner|frame|worker=<harness>[:<model>[@<effort>]]`: the route's parent moves
    pins of a sealed route.

    Each pin's checked launch tuples are probed now (`_pin_change_probe`), all of them before
    anything is recorded, and each change is one row beside the route
    (`route_authority.record_pin_change`); every launch decision reads the route through
    `route_authority.route_in_force`, so the next launch of that target uses the new pin and an
    attempt that already launched keeps what it launched with."""
    import route_authority as RA
    from work_start import _rows
    pins, warnings = _filter_top_pins(_parse_selection_pins(values))
    harness, session = RA.caller_identity()
    owners = [meta for _status, meta in _rows(jobs).values()
              if meta.get("worker_type") == "owner"
              and route["route_id"] in {meta.get("owner_route_id"), meta.get("route_id")}]
    if owners and not RA.owns(owners[-1], session, jobs):
        raise ValueError("start-pin-parent-only: only this route's parent session moves its pins")
    targets = [target for target in RA.PIN_CHANGE_TARGETS if target in pins]
    probed = {target: _pin_change_probe(route, jobs, target, pins[target]["harness"], harness)
              for target in targets}
    source = _turn_peer_source(harness, session, route.get("cwd"))
    result = {"changed": False, "warnings": warnings, "changes": {}}
    for target in targets:
        tuples, candidates = probed[target]
        row = RA.record_pin_change(route, target=target, pin=pins[target],
                                   by={"harness": harness, "session_id": session},
                                   source=source, tuples=tuples, candidates=candidates)
        current = (RA.route_in_force(route).get("selection_pins") or {}).get(target)
        result["changes"][target] = {"changed": row is not None, "pin": current,
                                     "previous": (row or {}).get("previous")}
        result.update({target: current, "previous": (row or {}).get("previous"),
                       "source": (row or {}).get("source"),
                       "changed": result["changed"] or row is not None})
    return result


def _record_access_change(route, jobs):
    """The route's parent hands its next owner the execution access request in its environment
    (`AGENT_DISPATCH_EXECUTION_ACCESS_FILE`): one `access` row beside the route
    (`route_authority.record_access_change`), which the next owner and a replacement owner
    launch with. Only the route's parent records it; a request that does not validate is not
    recorded, and the launch meets it as it does today."""
    import route_authority as RA
    from execution_access import (AccessContext, ExecutionAccessError, load_request,
                                  normalized_request, request_path)
    from work_start import _rows
    given = request_path(None)
    if given is None:
        return None
    harness, session = RA.caller_identity()
    owners = [meta for _status, meta in _rows(jobs).values()
              if meta.get("worker_type") == "owner"
              and route["route_id"] in {meta.get("owner_route_id"), meta.get("route_id")}]
    if owners and not RA.owns(owners[-1], session, jobs):
        return {"changed": False, "reason": "access-change-parent-only"}
    try:
        context = AccessContext.build(worktree=str(route.get("cwd") or ""),
                                      artifact_root=str(route.get("artifact_root") or ""),
                                      dispatch_state_root=Path(jobs).expanduser().resolve(strict=False).parent,
                                      agent_home=ROOT)
        request = load_request(given, context=context)
        row = RA.record_access_change(route, request=normalized_request(request),
                                      request_sha256=request.request_sha256,
                                      by={"harness": harness, "session_id": session},
                                      source=_turn_peer_source(harness, session, route.get("cwd")))
    except (ExecutionAccessError, OSError, ValueError) as exc:
        return {"changed": False, "reason": getattr(exc, "reason", None) or str(exc)[:200]}
    current = RA.access_in_force(route) or {}
    return {"changed": row is not None, "request_sha256": current.get("request_sha256"),
            "source": current.get("source")}


def _main_session_only_models(harness):
    """The harness's `CFG_MAIN_SESSION_ONLY_MODELS`, or "" when it declares none
    or its model config cannot be read (compose then leaves the pin alone; the
    wrapper still refuses a restricted model at launch)."""
    try:
        import model_config
        values, _receipt = model_config.resolve_config(harness)
    except Exception:
        return ""
    return values.get("CFG_MAIN_SESSION_ONLY_MODELS", "")


def _filter_top_pins(pins):
    """`top-model-pin`: a main-session-only (top) model runs only in the main
    session and in frame.  For the owner and worker targets compose drops just
    the model part of the pin (the harness pin stays) and says so; frame keeps
    its model.  Returns `(pins, warnings)`."""
    kept, warnings = {}, []
    for target, pin in pins.items():
        pin = dict(pin)
        if target != "frame" and pin["model"]:
            import model_config
            restricted = _main_session_only_models(pin["harness"])
            if restricted and model_config.restricted_model(pin["model"], restricted):
                warnings.append(
                    f"warning=pin-dropped:{target}:{pin['model']}:main-session-only "
                    "(top models run only in the main session and frame)")
                pin["model"] = pin["effort"] = None
        kept[target] = pin
    return kept, warnings


def _compose_default_children(pins=None):
    """Child harnesses compose probes when `--children` is not given: every
    harness the user's policy enables (`all-enabled-candidates`) plus any
    harness a pin names.  An unreadable or absent policy falls back to the
    shipped default set; a harness that is not installed shows up as an
    `unsupported` probe tuple, never as a refusal."""
    children = list(COMPOSE_DEFAULT_CHILDREN)
    try:
        path = DEFAULTS.default_config_path()
        if os.path.exists(path):
            cfg = DEFAULTS.load_and_validate(path, DEFAULTS.default_topology_path())
            enabled = [h for h in DEFAULTS.query_owners(cfg) if isinstance(h, str)]
            if enabled:
                children = enabled
    except (DEFAULTS.DefaultsConfigError, OSError, ValueError):
        pass
    for pin in (pins or {}).values():
        if pin["harness"] not in children:
            children.append(pin["harness"])
    return tuple(children)


def _apply_selection_pins(route, pins):
    """Seal `pins` into `route` and stamp the worker pin on every depth-2 node.

    A worker pin H becomes each depth-2 node's `harness_affinity` and moves H to
    the front of that node's sealed `primary` band (removing it from the other
    bands), so the ordinary affinity consumers pick H first.  The pin leads the
    selection order (CONVENTIONS §2.1): it is ahead of the usage gate, and only
    hard eligibility (policy, checked tuple) and an active usage limit keep H from
    running.  A route with no pin gets no key.
    """
    if not pins:
        return
    route["selection_pins"] = {
        "contract_version": SELECTION_PIN_CONTRACT_VERSION,
        **{target: dict(pins[target]) for target in SELECTION_PIN_TARGETS if target in pins},
    }
    worker = pins.get("worker")
    if worker is None:
        return
    harness = worker["harness"]
    for node in route.get("nodes", []):
        if node.get("dispatch_depth") != 2:
            continue
        node["harness_affinity"] = harness
        policy = node.get("harness_policy")
        if isinstance(policy, dict):
            for band in ("relief", "last_resort"):
                policy[band] = [h for h in policy.get(band, []) if h != harness]
            policy["primary"] = [harness] + [h for h in policy.get("primary", []) if h != harness]


def _verify_selection_pins(route):
    """Structure-only check of a sealed `selection_pins`; absent means nothing to check."""
    if "selection_pins" not in route:
        return
    pins = route["selection_pins"]
    if not isinstance(pins, dict) or pins.get("contract_version") != SELECTION_PIN_CONTRACT_VERSION:
        raise ValueError("invalid selection_pins contract")
    if not set(pins) <= {"contract_version", *SELECTION_PIN_TARGETS}:
        raise ValueError("invalid selection_pins key")
    for target in SELECTION_PIN_TARGETS:
        if target not in pins:
            continue
        pin = pins[target]
        if not isinstance(pin, dict) or set(pin) != {"harness", "model", "effort"}:
            raise ValueError(f"invalid selection_pins.{target} shape")
        if pin["harness"] not in DEFAULTS.DISPATCHABLE_HARNESSES:
            raise ValueError(f"invalid selection_pins.{target}.harness")
        for key, pattern in (("model", SELECTION_PIN_MODEL), ("effort", SELECTION_PIN_EFFORT)):
            value = pin[key]
            if value is not None and not (isinstance(value, str) and pattern.fullmatch(value)):
                raise ValueError(f"invalid selection_pins.{target}.{key}")


def _inherited_selection_pins(binding, artifact_root):
    """The structurally valid pins of the frame a `--route-plan` binding names, else `{}`."""
    import route_plan as RP
    pins = RP.frame_selection_pins(binding["record"]["decision"], artifact_root)
    try:
        _verify_selection_pins({"selection_pins": {"contract_version": SELECTION_PIN_CONTRACT_VERSION, **pins}})
    except ValueError:
        return {}
    return pins


# Shapes a session picks for work that is already decided (the person named the capability and
# its stages, a stopped route continues, or an approved proposal's next leg): they compile without
# the recipe's frame pair. Routes sealed before this rule keep the frame nodes they were sealed with.
DECIDED_SHAPES = ("solo", "staged")


def compose_route(*, capability, capability_mode, shape, graph, slug, cwd, artifact_root,
                  intensity=None, signals=(), spec_read=None, drift_verdict=None,
                  tracking=None, artifact_guard=None, children=None, parent_harness="claude",
                  dispatch_evidence=None, registered_headless_evidence=None,
                  transport_evidence="compose-default", jobs=None, profile_demands=None, explicit_profiles=None,
                  campaign_key=None, parent_cycle_id=None, profile=None, work_request=None, unassigned=False,
                  selection_pins=None, route_plan=None, frameless=False, execution_scope=None,
                  extra_stages=None):
    """Resolve every default, then compile through the ordinary sealer.

    `route_plan` (a binding read by `route_plan.read_route_plan`) seals `{decision, digest, index}`
    and compiles without frame nodes; `frameless` alone is the same compile without the seal, the form
    a proposal leg is validated in. A `solo` or `staged` shape is chosen for work that is already
    decided, so it compiles without the recipe's frame pair too; `framed` is where a frame runs.
    """
    import route_plan as RP
    given_intensity, given_capability = intensity, capability
    if capability is None and shape != "framed":
        capability = COMPOSE_DEFAULT_CAPABILITY
    sealed_plan = RP.sealed_form(route_plan) if route_plan is not None else None
    frameless = frameless or route_plan is not None or shape in DECIDED_SHAPES
    if execution_scope is None and route_plan is not None:
        execution_scope = route_plan_execution_scope(route_plan)
    if execution_scope is not None and execution_scope not in ("complete", "report"):
        raise ValueError("compose-shape-invalid:execution-scope")
    if shape not in COMPOSE_SHAPES:
        raise ValueError(f"compose-shape-invalid:{shape}")
    routing_hints = None
    if shape == "framed":
        # The shape itself picks the compiler-internal capability. Whatever was typed for the
        # ordinary shapes is only a hint for the work request: recorded, never sealed as the
        # route's capability, never refused as an argument conflict, never applied to a model.
        routing_hints = {key: value for key, value in (
            ("capability", capability), ("capability_mode", capability_mode),
            ("graph", graph), ("profile", profile)) if value}
        capability, capability_mode, graph, profile = ROUTE_FRAME_CAPABILITY, "default", None, None
        if intensity is not None and intensity not in ORDER:
            raise ValueError("invalid intensity")
        # One frame leg for ordinary work; both top legs when the session marks the work uncertain
        # or hard to reverse with a higher intensity (user decision 2026-10-07). The framed route
        # itself is always sealed at `standard`.
        frame_legs = 2 if intensity is not None and ORDER[intensity] > ORDER["standard"] else 1
        intensity = None
    resume_graph = (shape in ("direct", "solo") and RESOURCE_RESUME.selected(
        capability, capability_mode or "setup", graph.split(",") if graph else None))
    if shape != "staged" and graph and not resume_graph:
        raise ValueError(f"compose-graph-only-staged:{shape}")
    registry = TOPO.load_registry()
    # A capability may own several recipes (autopilot-lab: setup, eval); the
    # requested mode picks the recipe that declares it. The internal framed
    # capability exists only for the framed shape.
    candidates = [r for r in registry["recipes"] if r["capability"] == capability
                  and (capability != ROUTE_FRAME_CAPABILITY or shape == "framed")]
    if not candidates:
        raise ValueError(f"compose-capability-unknown:{capability}")
    if capability_mode is None:
        base = next((r for r in candidates if "dev" in r["modes"]), candidates[0])
        capability_mode = "dev" if "dev" in base["modes"] else sorted(base["modes"])[0]
    base = next((r for r in candidates if capability_mode in r["modes"]), None)
    if base is None:
        modes = sorted({mode for r in candidates for mode in r["modes"]})
        raise ValueError(f"compose-mode-unknown:{capability_mode} (modes: {','.join(modes)})")
    requested = intensity or SHAPE_INTENSITY[shape]
    if requested not in ORDER:
        raise ValueError("invalid intensity")
    if shape == "direct" and requested != "direct":
        raise ValueError("compose-shape-intensity-mismatch:direct")
    if shape == "solo" and requested != "quick":
        raise ValueError("compose-shape-intensity-mismatch:solo")
    if shape == "staged" and ORDER[requested] < ORDER["standard"]:
        raise ValueError("compose-shape-intensity-mismatch:staged")
    cwd = str(Path(cwd).resolve(strict=True))
    artifact_root = str(Path(artifact_root).resolve())
    # The campaign is the agent's proposal for the work stream (CONVENTIONS
    # "Campaign or cycle").  A route sealed without one silently landed in the
    # root's degraded `_unassigned` container (TF-Rehancer 2026-09-15: nine
    # routes, zero keys), so compose demands the proposal, shows the streams
    # that already exist, and accepts only an explicit opt-out.
    if unassigned and (campaign_key is not None or parent_cycle_id is not None):
        raise ValueError("compose-campaign-selection-conflict:--unassigned excludes --campaign-key and --parent-cycle")
    if campaign_key is None and parent_cycle_id is None and not unassigned:
        raise ValueError("compose-campaign-key-required:" + compose_campaign_hint(artifact_root, cwd))
    if tracking is None:
        tracking = "tracked" if shape in ("staged", "framed") or resume_graph else "untracked"
    gate = {
        "spec_read": compose_spec_read(cwd, artifact_root, spec_read),
        "drift_verdict": drift_verdict or DEFAULT_DRIFT_VERDICT,
        "workflow_mode": tracking,
        "artifact_guard": {"satisfied": True, "source": artifact_guard or DEFAULT_ARTIFACT_GUARD},
    }
    predicates = list(base["direct_predicates"]) if shape == "direct" else []
    if resume_graph:
        predicates = [p for p in predicates if p not in ("no-resource-run", "no-independent-verifier")]
    signals = sorted(set(signals or ()))
    if shape == "direct" and signals:
        raise ValueError("compose-direct-signals-conflict")
    readiness = None
    find_input = None
    frame_ids = [n["id"] for n in base["standard_plus"]["nodes"] if _frame_node(n)]
    planned_full = shape == "staged" and not graph and frameless and bool(frame_ids)
    if shape == "staged" and (graph or planned_full or (execution_scope == "report" and frameless)) and not unassigned:
        try:
            import artifact_producer
            find_input = artifact_producer.input_source_finder(
                artifact_root, parent_cycle_id=parent_cycle_id, campaign_key=campaign_key,
                capability=capability, route_chain_identity=_route_chain_identity("compose", None))
        except ImportError:
            find_input = None
    capabilities = {recipe["capability"] for recipe in registry["recipes"]
                    if recipe["capability"] != ROUTE_FRAME_CAPABILITY}
    if find_input is not None and frameless:
        if parent_cycle_id is None and route_plan is None:
            find_input = _without_implicit_frame_briefs(registry, find_input)
        find_input = _frame_brief_finder(registry, capability, find_input)
    if planned_full:
        # No graph: the recipe's own order, minus the frame nodes the plan already ran.
        graph_spec = [(n["id"], None) for n in base["standard_plus"]["nodes"] if not _frame_node(n)]
    else:
        graph_spec = parse_graph_spec(graph, capabilities) if (shape == "staged" or resume_graph) and graph else None
    if shape == "staged" and execution_scope == "report":
        source_graph = ([f"{node}" + (f":{unit}" if unit else "") for node, unit in graph_spec]
                        if graph_spec is not None else None)
        projected = project_entry_execution_scope({"capability": capability, "mode": capability_mode,
                                                   "shape": shape, "graph": source_graph}, "report", registry)
        graph_spec = parse_graph_spec(",".join(projected.get("graph") or []), capabilities)
    if extra_stages is None and isinstance(route_plan, dict) and isinstance(route_plan.get("leg"), dict):
        extra_stages = route_plan["leg"].get("extra_stages")   # the plan's own stages, from the sealed leg
    selected_recipe = (compose_subgraph_recipe(registry, base, graph_spec, find_input,
                                                preserve_base_dependencies=execution_scope == "report",
                                                extra_stages=extra_stages, group_intensity=requested,
                                                complete_group_consumers=True)
                       if graph_spec is not None else base)
    if execution_scope == "report" and shape == "staged" and graph_spec is not None and profile is None:
        # The narrowed graph is made only from existing recipe/catalog parts. Keep each
        # stage's declared profile so a report boundary adds no new profile-demand input.
        explicit_profiles = dict(explicit_profiles or {})
        for node in selected_recipe["standard_plus"]["nodes"]:
            if node.get("model_profile") and node["id"] not in explicit_profiles:
                explicit_profiles[node["id"]] = node["model_profile"]
    owner_only = shape == "staged" and _single_owner_nodes(selected_recipe["standard_plus"]["nodes"])
    gpu_route = {"capability": capability, "nodes": selected_recipe["standard_plus"]["nodes"],
                 "selection": {"promotion_signals": [{"signal": s} for s in signals]}}
    if (shape == "staged" and not owner_only and dispatch_evidence is not None
            and GPU_SANDBOX.gpu_resource_nodes(gpu_route)):
        dispatch_evidence = _validate_dispatch_evidence(
            dispatch_evidence, DISPATCH_CONTRACT_VERSION,
            expected_worktree=cwd, require_scope=True)
        choice = GPU_SANDBOX.select(gpu_route)
        codex_rows = [row for row in dispatch_evidence.get("tuples", [])
                      if row.get("parent_harness") == "codex"]
        if any(row.get("parent_sandbox") != choice["sandbox"] for row in codex_rows):
            _fallback_chain(dispatch_evidence, expected_worktree=cwd, require_scope=True)
            # A planning frame checked its own normal owner. The selected GPU
            # leg needs a real prospective check of its chosen Codex owner,
            # before its tuple is sealed; never relabel the frame's evidence.
            readiness = _compose_readiness(cwd, jobs or _compose_default_jobs(), "codex",
                sorted({row["child_harness"] for row in codex_rows}), gpu_route=gpu_route)
            dispatch_evidence = {**dispatch_evidence, "tuples": [
                row for row in dispatch_evidence["tuples"] if row.get("parent_harness") != "codex"
            ] + readiness["tuples"]}
    owner_pin = (selection_pins or {}).get("owner", {}).get("harness")
    probe_parent = owner_pin if shape == "staged" and owner_pin else parent_harness
    if shape in ("staged", "framed") and not owner_only and dispatch_evidence is None:
        readiness = _compose_readiness(cwd, jobs or _compose_default_jobs(), probe_parent,
                                       children or _compose_default_children(selection_pins),
                                       gpu_route=gpu_route if shape == "staged" else None)
        dispatch_evidence = {"tuples": readiness["tuples"], "native_subagent": []}
    elif shape == "staged" and not owner_only and owner_pin and not any(
            row.get("parent_harness") == owner_pin and row.get("status") == "supported"
            and row.get("launch_authority") == "conductor"
            for row in (dispatch_evidence or {}).get("tuples", [])):
        # A frame's evidence describes its conductor. Check the selected owner
        # with its workers rather than relabelling that evidence.
        readiness = _compose_readiness(cwd, jobs or _compose_default_jobs(), owner_pin,
                                       children or _compose_default_children(selection_pins),
                                       gpu_route=gpu_route)
        dispatch_evidence = {**dispatch_evidence,
                             "tuples": dispatch_evidence["tuples"] + readiness["tuples"]}
    if (shape == "solo" or owner_only or resume_graph) and registered_headless_evidence is None:
        readiness = readiness or _compose_readiness(cwd, jobs or _compose_default_jobs(),
                                                    parent_harness, children or _compose_default_children(selection_pins))
        registered_headless_evidence = {"candidates": readiness["candidates"]}
    common = dict(
        signals=signals, transport=None, transport_evidence=transport_evidence,
        tracking=tracking, tracked_gate_evidence=gate, slug=slug,
        campaign_key=campaign_key, parent_cycle_id=parent_cycle_id,
        dispatch_evidence=dispatch_evidence,
        registered_headless_evidence=registered_headless_evidence,
        route_origin="compose", shape=shape,
        profile_demands=profile_demands, explicit_profiles=explicit_profiles, profile=profile,
        route_plan=sealed_plan, frameless=frameless,
        **({"frame_legs": frame_legs} if shape == "framed" else {}),
    )
    if graph_spec is not None:
        recipe = selected_recipe
        route = compile_composed_route(
            recipe, capability_mode, requested, cwd, artifact_root,
            predicates=predicates, inline_reason=None, **common)
    else:
        route = compile_route(
            capability, capability_mode, requested, cwd, artifact_root,
            predicates=predicates, inline_reason="atomic-direct" if shape == "direct" else None,
            **common)
    if unassigned:
        route["campaign_unassigned"] = True
    # Observational provenance in the existing basis list, shared by compose
    # and later start; it never changes stage/profile selection or approvals.
    route["selection"]["selection_basis"].extend([
        {"axis": "compose-intensity", "signal": given_intensity or SHAPE_INTENSITY[shape],
         "source": "explicit" if given_intensity is not None else "shape-default"},
        {"axis": "compose-capability", "signal": given_capability or route["capability"],
         "source": "frame-shape" if shape == "framed" else
                   "explicit" if given_capability is not None else "compose-default"},
    ])
    if work_request is not None:
        from work_start import capture_request_context
        if routing_hints:
            work_request = {**work_request, "routing_hints": routing_hints}
        route["work_request"] = capture_request_context(work_request, route)
    if selection_pins:
        _apply_selection_pins(route, selection_pins)
    if execution_scope is not None:
        route = _bind_entry_execution_scope(route, execution_scope)
        if work_request is not None:
            request = dict(route.get("work_request") or {})
            request["text"] = request.get("text", "").rstrip() + f"\n\nExecution scope: {execution_scope}\n"
            route["work_request"] = request
    # The compose input provenance also participates in the route seal.
    route["route_hash"] = route_hash(route)
    route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
    return route


def _without_implicit_frame_briefs(registry, finder):
    """Keep campaign lookup for ordinary results, but do not infer frame lineage from it.

    The blocked names come from the frame contract, including capability-specific consumer names
    and the borrowed-part relocation form used by `_compose_inputs`. An explicit parent or route
    proposal bypasses this wrapper at the compose call site and keeps the existing mapping.
    """
    catalog = TOPO.part_catalog(registry)
    frame = catalog.get("frame") or {}
    names = set(frame.get("brief_outputs") or [])
    names.update(name for brief in (frame.get("briefs") or {}).values()
                 for name in brief.get("inputs", []))
    blocked = set(names)
    aliases = frame.get("aliases") or []
    for part_id in (catalog.get("parts") or {}):
        part_capability = part_id.partition(":")[0]
        blocked.update(f"parts/{part_capability}/{producer}/{name}"
                       for producer in aliases for name in names)

    def find(name):
        return None if name in blocked else finder(name)
    return find


def _frame_brief_finder(registry, capability, finder):
    """Let a leg of an approved plan find the frame cycle's two briefs under the names its recipe reads.

    A recipe that keeps its frame briefs under its own names (design, spec) maps them, through the
    part catalogue's brief-name table, to the canonical `shards/<frame>/direction-brief.md` outputs
    the framed route's cycle holds. Names that already match (code, draft, refine) need no mapping.
    """
    names = TOPO.frame_brief_inputs(registry, capability)
    outputs = (TOPO.part_catalog(registry).get("frame") or {}).get("brief_outputs") or []
    table = dict(zip(names, outputs)) if len(names) == len(outputs) else {}

    def find(name):
        return finder(name) or (finder(table[name]) if name in table else None)
    return find


def _frame_pin_rows(frame_route):
    """Compatibility name; all descendants read the same pins in force."""
    return ROUTE_AUTHORITY.selection_pin_rows(frame_route)


def proposal_readiness(frame_route, jobs):
    """One read-only readiness probe shared by every proposal leg's memory compile."""
    from dispatch_parent_completion import default_parent_harness
    return _compose_readiness(frame_route["cwd"], jobs, default_parent_harness("claude"),
                              _compose_default_children(_frame_pin_rows(frame_route) or None))


def _leg_compose_kwargs(leg_args, *, frame_route, frame_cycle_id, slug):
    import route_plan as RP
    spec = ((frame_route.get("tracked_gate_evidence") or {}).get("spec_read") or {}).get("source") or "auto"
    return dict(
        capability=leg_args["capability"], capability_mode=leg_args["capability_mode"], shape=leg_args["shape"],
        graph=leg_args["graph"], intensity=leg_args["intensity"], slug=slug,
        cwd=RP.leg_cwd(leg_args, frame_route["cwd"]),
        artifact_root=frame_route["artifact_root"],
        spec_read="auto" if str(spec).startswith("compose-auto:") else spec,
        campaign_key=frame_route.get("campaign_key"), parent_cycle_id=frame_cycle_id,
        selection_pins=_frame_pin_rows(frame_route) or None)


def compile_proposal_leg(leg, index, *, frame_route, frame_cycle_id, readiness):
    """Memory compile of one proposal leg in the frame-less form a `--route-plan` compile uses.

    `readiness` is a zero-argument probe, called only for a leg that needs checked dispatch evidence.
    Nothing is written, started or recorded: no route file, no route-chain line, no producer call.
    The registry, graph, order, unit, scope and gate rules are the ones compose applies when it seals.
    """
    import route_plan as RP
    kwargs = _leg_compose_kwargs(RP.leg_arguments(leg), frame_route=frame_route, frame_cycle_id=frame_cycle_id,
                                 slug=f"{frame_route.get('slug') or 'framed'}-leg{index}")
    probe = (lambda: readiness(kwargs["cwd"])) if leg.get("cwd") else readiness
    return compose_route(**kwargs, **_leg_evidence(leg, probe), frameless=True,
                         extra_stages=leg.get("extra_stages"))


def _leg_evidence(leg, readiness):
    if leg["shape"] == "direct":
        return {}
    probe = readiness()
    return {"dispatch_evidence": {"tuples": probe["tuples"], "native_subagent": []},
            "registered_headless_evidence": {"candidates": probe["candidates"]}}


def compile_first_leg(leg, *, frame_route, frame_cycle_id, context, binding, work_request, readiness, index=0):
    """An approved leg, compiled in memory with its route-plan reference sealed.

    Same arguments the printed compose command carries: the leg's shape, graph, capability, mode and
    intensity, `--route-plan <record>#<index>`, the previous cycle as parent and the same campaign.
    The first leg compiles from the frame route; a later leg from the leg before it.
    """
    import route_plan as RP
    kwargs = _leg_compose_kwargs(RP.leg_arguments(leg), frame_route=frame_route, frame_cycle_id=frame_cycle_id,
                                 slug=f"{context['slug']}-leg{index}")
    kwargs["cwd"] = RP.leg_cwd(leg, frame_route["cwd"], base_cwd=context["cwd"])
    owner = ((kwargs["selection_pins"] or {}).get("owner") or {}).get("harness") or context.get("owner")
    if work_request is not None:
        work_request = {**work_request, "owner_harness": owner or work_request.get("owner_harness")}
    probe = (lambda: readiness(kwargs["cwd"])) if leg.get("cwd") else readiness
    route = compose_route(**kwargs, **_leg_evidence(leg, probe), work_request=work_request, route_plan=binding,
                            parent_harness=owner or "claude")
    if not leg.get("cwd") and leg["shape"] != "direct" and _isolates_worktree(route):
        # Every leg of one decision shares the frame's worktree, prepared by the first leg that changes source.
        worktree = prepare_isolated_worktree(frame_route["cwd"], frame_route.get("slug"))
        if worktree.get("cwd"):
            route = compose_route(**{**kwargs, "cwd": worktree["cwd"]},
                                  **_leg_evidence(leg, lambda: readiness(worktree["cwd"])),
                                  work_request=work_request, route_plan=binding, parent_harness=owner or "claude")
    scope = route_plan_execution_scope(binding)
    if scope in ("complete", "report"):
        route = _bind_entry_execution_scope(route, scope)
    return route


def route_plan_execution_scope(binding):
    """The start choice every leg of one frame decision carries.

    A leg keeps `complete` only when each start approval it declares was given for that leg in the
    same interview; a part not approved there (one held for the person included) keeps its gate.
    `report` carries as it is, except to a later staged leg that would have no step left before its
    first approval (that leg keeps its gate).
    """
    if not isinstance(binding, dict) or type(binding.get("index")) is not int:
        return None
    approvals = (((binding.get("record") or {}).get("decision") or {}).get("approvals") or {})
    scope = approvals.get("execution_scope")
    if scope not in ("complete", "report") or (binding["index"] == 0 and scope == "report"):
        return scope
    legs = binding.get("legs")
    if not isinstance(legs, list) or not 0 <= binding["index"] < len(legs) or not isinstance(legs[binding["index"]], dict):
        return None
    leg = legs[binding["index"]]
    if scope == "report":
        return scope if leg.get("shape") != "staged" or project_entry_execution_scope(leg, "report").get("graph") else None
    given = {(row.get("key"), row.get("leg")) for row in approvals.get("given") or []
             if isinstance(row, dict) and row.get("accepted") is True}
    return scope if all((key, binding["index"]) in given for key, _part in declared_start_approvals(leg)) else None


def _bind_entry_execution_scope(route, scope):
    """Seal the already-selected entry scope and remove only the matching new-route preview wait."""
    if scope not in ("complete", "report"):
        return route
    route = json.loads(json.dumps(route))
    route["entry_execution_scope"] = scope
    route["entry_scope_contract_version"] = 1
    _project_entry_scope_nodes(route.get("nodes") or [], scope)
    route["human_gate_bindings"] = [row for row in route.get("human_gate_bindings") or []
                                    if row.get("gate") != "preview-disposition"]
    route["human_gates"] = sorted({row["gate"] for row in route["human_gate_bindings"]})
    route["workflow_contract"] = _workflow_contract(
        TOPO.load_registry(), route.get("nodes") or [], route["human_gate_bindings"])
    route["route_hash"] = route_hash(route)
    route["route_id"] = ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
    return route


def valid_entry_execution_scope_marker(version, scope):
    """Whether optional scope metadata uses the one supported, typed contract."""
    return (version is None and scope is None) or (type(version) is int and version == 1
                                                   and scope in ("complete", "report"))


def _project_entry_scope_nodes(nodes, scope):
    if scope not in ("complete", "report"):
        return nodes
    for node in nodes:
        if "inline_human_gates" in node:
            remaining = [gate for gate in node.get("inline_human_gates", []) if gate != "preview-disposition"]
            if remaining:
                node["inline_human_gates"] = remaining
            else:
                node.pop("inline_human_gates", None)
        if node.get("continuation") == {"kind": "human-gate", "gate": "preview-disposition"}:
            node["continuation"] = {"kind": "inline-next"}
    return nodes


def project_entry_execution_scope(leg, scope, registry=None):
    """Copy a proposed leg and, for report, stop a staged graph before its first catalog approval.

    Catalog part ids are resolved before cutoff, so borrowed namespaced tokens share exactly the
    same behavior as host stage ids. An explicit graph is only narrowed; an omitted graph expands
    from its base recipe before the same cutoff is applied.
    """
    result = json.loads(json.dumps(leg))
    if scope != "report" or result.get("shape") != "staged":
        return result
    registry = registry or TOPO.load_registry()
    capability = result.get("capability")
    recipes = [r for r in registry.get("recipes", []) if r.get("capability") == capability]
    mode = result.get("mode")
    base = next((r for r in recipes if mode in r.get("modes", [])), None) if mode else next(
        (r for r in recipes if "dev" in r.get("modes", [])), recipes[0] if recipes else None)
    if base is None:
        return result
    graph = list(result.get("graph") or [n["id"] for n in base.get("standard_plus", {}).get("nodes", [])
                                          if not _frame_node(n)])
    known = {row.get("capability") for row in registry.get("recipes", [])}
    catalog = TOPO.part_catalog(registry).get("parts", {})
    end = len(graph)
    for index, token in enumerate(graph):
        head, sep, tail = str(token).partition(":")
        if sep and head in known:
            part_id = f"{head}:{tail.partition(':')[0]}"
        else:
            part_id = f"{capability}:{head}"
        if (catalog.get(part_id) or {}).get("start_approval"):
            end = index
            break
    result["graph"] = graph[:end]
    return result


def declared_start_approvals(leg, registry=None):
    """`(start_approval, part id)` pairs a proposal leg declares in the part catalogue.

    Read from the catalogue alone (no compile, no probe), so anything rendered from it is the same on
    every replay. Only a staged leg carries stage parts.
    """
    registry = registry or TOPO.load_registry()
    capability = leg["capability"]
    recipes = [r for r in registry["recipes"] if r["capability"] == capability and capability != ROUTE_FRAME_CAPABILITY]
    if not recipes:
        return []
    mode = leg.get("mode")
    base = (next((r for r in recipes if "dev" in r["modes"]), recipes[0]) if mode is None
            else next((r for r in recipes if mode in r["modes"]), None))
    if base is None:
        return []
    if RESOURCE_RESUME.selected(capability, mode or "setup", leg.get("graph")):
        return []
    if leg.get("shape") != "staged":
        # A one-shot recipe still contains these internal approval-scoped parts.
        # Keep the same catalogue keys so the entry choice can carry its scope.
        internal = {
            ("autopilot-lab", "setup"): ("full-run", "autopilot-lab:full-run"),
            ("autopilot-ship", "default"): ("deploy", "autopilot-ship:deploy"),
            ("autopilot-refine", None): ("preview", "autopilot-refine:transaction"),
            ("autopilot-apply", None): ("handback", "autopilot-apply:handback"),
        }
        selected = internal.get((capability, mode)) or internal.get((capability, None))
        return [selected] if selected and TOPO.part_row(registry, selected[1]).get("start_approval") else []
    known = {r["capability"] for r in registry["recipes"]} - {ROUTE_FRAME_CAPABILITY}
    tokens = leg.get("graph") or [n["id"] for n in base["standard_plus"]["nodes"] if not _frame_node(n)]
    rows = []
    for token in tokens:
        head, _, rest = token.partition(":")
        part = f"{head}:{rest.partition(':')[0]}" if rest and head in known else f"{base['capability']}:{head}"
        approval = TOPO.part_row(registry, part).get("start_approval")
        if approval:
            rows.append((approval, part))
    return rows


def publish_composed_route(route, artifact_root, *, plan=None):
    """The compose CLI's write tail for an already compiled route: canonical write-once,
    owner binding and one route-chain line. Returns the canonical route path."""
    shim = argparse.Namespace(command="compose", start=True, output=None, owner=None, full_record=False)
    shim._route_chain_plan = (plan, "explicit" if plan else None)
    return _emit_compiled_route(shim, route, artifact_root)


COMPOSE_CAMPAIGN_LIST_CAP = 12


def compose_campaign_summaries(artifact_root):
    """Active campaigns of the root, newest first.  A fresh root has none
    (empty list); a root that cannot be read returns None so the caller says
    "unavailable" rather than mislabelling a join as a creation."""
    try:
        import artifact_producer
        return artifact_producer.list_campaign_summaries(Path(artifact_root), active_only=False)
    except (OSError, ImportError, ValueError):
        return None


CWD_CAMPAIGN_SCAN_ROUTES = 100
CWD_CAMPAIGN_KEYS_SHOWN = 3


def cwd_campaign_keys(artifact_root, cwd, rows=None):
    """The active work streams of this folder, newest first: the campaign keys of the newest
    routes under the artifact root that were sealed for `cwd` (at most `CWD_CAMPAIGN_SCAN_ROUTES`
    route files are read, and the scan stops at `CWD_CAMPAIGN_KEYS_SHOWN` keys)."""
    rows = compose_campaign_summaries(artifact_root) if rows is None else rows
    active = {r.get("key") for r in rows or [] if r.get("state") == "active"} - {None, "_unassigned"}
    here, keys = os.path.realpath(str(cwd)), []
    try:
        with os.scandir(canonical_routes_dir(artifact_root)) as entries:
            files = sorted((entry for entry in entries if entry.name.startswith("rt-") and entry.name.endswith(".json")),
                           key=lambda entry: entry.stat().st_mtime, reverse=True)[:CWD_CAMPAIGN_SCAN_ROUTES]
    except OSError:
        return []
    for path in files:
        if len(keys) >= CWD_CAMPAIGN_KEYS_SHOWN:
            break
        try:
            route = json.loads(Path(path.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = route.get("campaign_key") if isinstance(route, dict) else None
        if (key in active and key not in keys and isinstance(route.get("cwd"), str)
                and os.path.realpath(route["cwd"]) == here):
            keys.append(key)
    return keys


def compose_campaign_hint(artifact_root, cwd=None):
    rows = compose_campaign_summaries(artifact_root)
    if rows is None:
        shown = "unavailable (campaign scan failed)"
    else:
        keyed = [r for r in rows if r.get("key") not in (None, "_unassigned")]
        shown = ", ".join(f"{r['key']}({r['cycle_count']})" for r in keyed[:COMPOSE_CAMPAIGN_LIST_CAP])
        if len(keyed) > COMPOSE_CAMPAIGN_LIST_CAP:
            shown += f", … +{len(keyed) - COMPOSE_CAMPAIGN_LIST_CAP}"
    here = cwd_campaign_keys(artifact_root, cwd, rows) if cwd is not None and rows is not None else []
    return ((f"this folder's streams: {', '.join(here)}; " if here else "")
            + f"name the work stream with --campaign-key <existing|new> (active: {shown or 'none'}); "
            "--unassigned keeps the work in the root's degraded _unassigned container")


def compose_campaign_selection(route):
    """What the sealed campaign choice means in this artifact root (read-only)."""
    key = route.get("campaign_key")
    parent = route.get("parent_cycle_id")
    rows = compose_campaign_summaries(route["artifact_root"])
    unavailable = rows is None
    rows = rows or []
    active = [r for r in rows if r.get("state") == "active" and r.get("key") not in (None, "_unassigned")]
    selection = {"key": key, "active_count": len(active),
                 "active_keys": [r["key"] for r in active[:COMPOSE_CAMPAIGN_LIST_CAP]],
                 "active_keys_unavailable": unavailable}
    if key is not None:
        import artifact_producer
        choice = artifact_producer.classify_campaign_key(rows, key)
        match = next((r for r in rows if r.get("campaign_id") == choice.get("campaign_id")), None)
        mode = "unresolved" if unavailable else choice["mode"]
        selection.update(mode=mode, campaign_id=match["campaign_id"] if match else None,
                         title=match["title"] if match else None,
                         **({"blocked_reason": choice["code"]} if choice["mode"] == "blocked" else {}))
    elif parent is not None:
        selection.update(mode="parent", parent_cycle_id=parent)
    else:
        selection.update(mode="unassigned", explicit=route.get("campaign_unassigned") is True)
    return selection


def compose_resolve_campaign_key(artifact_root, key):
    """`(key, given)`: a key no campaign carries, but that names exactly one active
    campaign by its folder, id or spelling, joins that campaign (`given` keeps what
    was typed); any other key is returned unchanged with `given` None.

    Only an exact key used to join, so a folder name (`2026-09-04_prd-v25`) or a
    near spelling quietly opened a duplicate stream (BC 2026-09-04)."""
    rows = compose_campaign_summaries(artifact_root)
    if not rows or any(r.get("key") == key for r in rows):
        return key, None

    def norm(value):
        try:
            return ARTIFACT_LOCATOR.slugify(ARTIFACT_LOCATOR.strip_leading_date(str(value or "")))[0]
        except ARTIFACT_LOCATOR.LocatorError:
            return ""

    wanted = norm(key)
    matches = {r["key"] for r in rows
               if r.get("state") == "active" and r.get("key") not in (None, "_unassigned")
               and (key in (r.get("locator"), r.get("campaign_id"))
                    or (wanted and wanted in (norm(r.get("key")), norm(r.get("locator")))))}
    return (matches.pop(), key) if len(matches) == 1 else (key, None)


COMPOSE_SLUG_WORDS = 6


def compose_default_slug(task_text, *, capability=None, shape=None):
    """The slug a compose names from its own task, so `--slug` is optional.

    The first line with ASCII words names it (up to six words). A task with
    none, such as a request written only in Korean, is named by its capability
    and a short digest of the text, so two such tasks still get different names."""
    text = task_text or ""
    for line in text.splitlines()[:40]:
        words = re.findall(r"[A-Za-z0-9]+", line)
        if any(re.search(r"[A-Za-z]", word) for word in words):
            return ARTIFACT_LOCATOR.slugify("-".join(words[:COMPOSE_SLUG_WORDS]))[0]
    parts = [capability or shape or "work"]
    if text.strip():
        parts.append(hashlib.sha256(text.encode("utf-8")).hexdigest()[:6])
    return ARTIFACT_LOCATOR.slugify("-".join(parts))[0]


def _compose_campaign_line(selection):
    mode = selection["mode"]
    if mode == "join":
        text = f"캠페인 {selection['key']} (기존 합류)"
    elif mode == "create":
        text = f"캠페인 {selection['key']} (신규 생성)"
    elif mode == "reopen":
        text = f"캠페인 {selection['key']} (닫힌 캠페인 재개)"
    elif mode == "blocked":
        text = f"캠페인 {selection['key']} (begin 거부 예정: {selection['blocked_reason']})"
    elif mode == "parent":
        text = f"캠페인 parent {selection['parent_cycle_id']} 상속"
    elif mode == "unresolved":
        text = f"캠페인 {selection['key']} (기존 여부 확인 불가: 캠페인 목록 읽기 실패)"
    else:
        text = "캠페인 미배정 (_unassigned, degraded)"
    shown = ", ".join(selection["active_keys"][:6])
    if selection["active_count"] > 6:
        shown += " …"
    return f"  {text} · 활성 캠페인 {selection['active_count']}개" + (f": {shown}" if shown else "")


def route_start_approvals(route, registry=None):
    """Start-approval marks of the parts a route carries (SD-165; stage-dispatch 13.63.7).

    A declaration for the card, the frame catalog and the interview -- never a
    gate, fence or wait. A borrowed node seals its mark; a host's own stage is
    looked up in the part catalog as `<capability>:<stage>`.
    """
    registry = registry or TOPO.load_registry()
    rows, seen = [], set()
    for node in route.get("nodes") or []:
        borrowed = node.get("part")
        part = borrowed or f"{route.get('capability')}:{node.get('parallel_anchor') or node.get('id')}"
        approval = node.get("start_approval") if borrowed else TOPO.part_row(registry, part).get("start_approval")
        if approval and part not in seen:
            seen.add(part)
            rows.append({"node": node.get("parallel_anchor") or node.get("id"), "part": part,
                         "start_approval": approval, "borrowed": bool(borrowed)})
    if (route.get("selection") or {}).get("shape") in ("direct", "solo"):
        internal = {
            ("autopilot-lab", "setup"): ("full-run", "autopilot-lab:full-run"),
            ("autopilot-ship", "default"): ("deploy", "autopilot-ship:deploy"),
            ("autopilot-refine", None): ("preview", "autopilot-refine:transaction"),
            ("autopilot-apply", None): ("handback", "autopilot-apply:handback"),
        }
        key, part = internal.get((route.get("capability"), route.get("capability_mode"))) or internal.get(
            (route.get("capability"), None), (None, None))
        if key and part not in seen and TOPO.part_row(registry, part).get("start_approval"):
            rows.append({"node": "one-shot", "part": part, "start_approval": key, "borrowed": False})
    return rows


def compose_observations(route):
    """Display facts already known by compose; never changes routing or approval."""
    from session_identity import identity
    who = identity()
    origin = _turn_peer_source(who.harness, who.session_id, route.get("cwd")) if who.known and who.session_id else "unattributed"
    shape = route.get("selection", {}).get("shape") or shape_for_intensity(route["effective_intensity"])
    peer = origin.startswith("peer:") and shape in {"direct", "solo"}
    external = route.get("capability") == "autopilot-ship" and route.get("entry_execution_scope") != "report"
    confirmation = {"method": "peer-notice" if peer and not external else "user-card", "origin": origin,
                    "meaning": "display only; existing approval and destructive/external/user-requested card exceptions remain"}
    nodes = [n for n in route.get("nodes", []) if not _frame_node(n)]
    lab_run = any(n.get("kind") == "resource-runner" and
                  (route.get("capability") == "autopilot-lab" or
                   str(n.get("part", "")).startswith("autopilot-lab:")) for n in nodes)
    selection = route.get("selection") or {}
    evidence = route.get("dispatch_evidence") or {}
    native = evidence.get("native_subagent")
    native_label = "관측됨" if native else "미확인"
    headless = bool(evidence.get("tuples") or route.get("registered_headless_candidates"))
    answers = [
        ("주 capability", route["capability"]),
        ("새 실측", "예 (lab 실행 선언됨)" if lab_run else "미확인 (작업 의미에 따름)"),
        ("standard+", "예" if route["effective_intensity"] in {"standard", "strong", "thorough", "adversarial"} else "아니오"),
        ("분리 단계", f"{len(nodes)}개 선언됨" if shape == "staged" else "frame 결정 대기" if shape == "framed" else "없음 (단일 실행)"),
        ("inline 예외", str(selection.get("inline_reason") or selection.get("selection_basis") or "미확인") if shape == "direct" else "해당 없음"),
        ("위임 표면", f"native {native_label} / headless {'관측됨' if headless else '미확인'}"),
        ("lineage·RUNLOG", "미확인 (기존 lineage·append-only RUNLOG 유지 계약)"),
    ]
    return {"confirmation": confirmation,
            "pre_execution_answers": [{"question": i, "name": name, "answer": value} for i, (name, value) in enumerate(answers, 1)]}


def compose_omission_lines(route):
    """Existing compose diagnostics only; no stage, profile or gate changes."""
    recipe = route.get("composed_recipe") or {}
    omissions = (recipe.get("compose") or {}).get("omitted_parallel_presets", [])
    if not omissions:
        return []
    lines = []
    for row in omissions:
        group = None
        try:
            registry = TOPO.load_registry()
            base = TOPO.resolve_recipe(registry, route["capability"], route["capability_mode"])
            anchor = next((n for n in route["nodes"] if n["id"] == row["id"]), {"id": row["id"]})
            source, node = TOPO.part_recipe(registry, anchor["part"]) if anchor.get("part") else (base, anchor)
            group = next((g for g in source["standard_plus"].get("parallel_groups", [])
                          if g["node"] == node["id"]), None)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # A historical registry gap cannot block compose/start.
        if group is None:
            continue  # Older diagnostic rows have no intensity hint; do not guess.
        if ORDER[route["effective_intensity"]] < ORDER[group["min_intensity"]]:
            continue  # This intensity did not select the declared preset.
        legs = " legs " + ",".join(row["legs"]) if row.get("legs") else ""
        lines.append(f"  {route['effective_intensity']} group {row['id']}{legs} dropped by --graph: {row['reason']}")
    return lines


def compose_decision_lines(route):
    """Read only the sealed selection/nodes; same display for every harness."""
    basis = {row.get("axis"): row for row in
             (route.get("selection") or {}).get("selection_basis", [])}
    intensity = basis.get("compose-intensity", {})
    source = intensity.get("source", "sealed-route")
    requested = intensity.get("signal", route.get("requested_intensity"))
    detail = ", 지정 안 됨" if source == "shape-default" else ""
    if requested and requested != route["effective_intensity"]:
        detail += f", requested={requested}"
    lines = [f"  intensity={route['effective_intensity']} (source={source}{detail})"]
    if source == "shape-default":
        lines.append("  강도 안내: 판정·원인 분석이면 --intensity strong 검토")
    shape = (route.get("selection") or {}).get("shape") or shape_for_intensity(route["effective_intensity"])
    capability = basis.get("compose-capability", {})
    if capability.get("source") == "compose-default":
        lines.append(f"  capability={route['capability']} (source=compose-default, --capability 지정 안 됨; "
                     "고정 기본값, 과제 문장 분류 없음; 결과 분석·평가·비교는 autopilot-lab/eval 검토)")
    elif capability.get("source") == "frame-shape":
        lines.append("  capability=frame 결정 대기 (source=frame-shape)")
    elif not capability:
        label = "frame 결정 대기" if shape == "framed" else route["capability"]
        lines.append(f"  capability={label} (source=sealed-route)")
    profiles = {}
    nodes = route.get("nodes", [])
    for node in nodes:
        if _no_model_node(node):
            continue
        anchor = node.get("parallel_anchor") or node["id"]
        profile = ("current-session(등급 미기록)" if node.get("execution_surface") == "inline"
                   else node.get("model_profile") or "unknown")
        profiles.setdefault(anchor, []).append(profile)
    lines.append("  tiers: " + (" ".join(f"{node}={'+'.join(values)}" for node, values in profiles.items()) or "없음"))
    workers = sum(n.get("dispatch_depth") == 2 or _frame_node(n) for n in nodes)
    owners = int(shape != "framed" and route["effective_intensity"] != "direct")
    resources = [n["id"] for n in nodes if n.get("kind") == "resource-runner"]
    lines.append(f"  규모: nodes={len(nodes)} worker_dispatches={workers} owner_dispatches={owners} "
                 f"resource(측정)={','.join(resources) or '없음'} (초기 실행 예상, 재시도·후속 route 제외)")
    for row in ((route.get("composed_recipe") or {}).get("compose") or {}).get("auto_completed_nodes", []):
        purpose = "owner closing summary" if row["reason"] == "terminal-anchor" else "registry consumer"
        lines.append(f"  {row['intensity']} group {','.join(row['anchors'])}: "
                     f"auto-added {row['id']} ({row['reason']}; {purpose})")
    return lines + compose_omission_lines(route)


def compose_card(route, plan=None, plan_source=None, *, owner_harness=None, route_plan_unreadable=False,
                 campaign_selection=None, include_decisions=True):
    """One-line `[경로]` notice the acting session pastes instead of a card."""
    shape = route.get("selection", {}).get("shape") or shape_for_intensity(route["effective_intensity"])
    ids = [node["id"] for node in route["nodes"]]
    framed = shape == "framed"
    graph = "→".join(ids) if route.get("composed") or framed else (ids[0] if ids else "-")
    gates = ",".join(sorted({row["gate"] for row in route.get("human_gate_bindings") or []})) or "없음"
    card = (
        f"[경로] {route['capability']} · {shape}({route['effective_intensity']}) {graph}"
        f" · route {route['route_id']} · origin compose · 사람 게이트 {gates}\n"
        f"  cwd {route['cwd']} · slug {route.get('slug', '-')}\n"
        + _compose_campaign_line(campaign_selection if campaign_selection is not None
                                 else compose_campaign_selection(route))
    )
    observed = compose_observations(route)
    if include_decisions:
        card += "\n" + "\n".join(compose_decision_lines(route))
    card += f"\n  확인 방식 {observed['confirmation']['method']} · 출처 {observed['confirmation']['origin']}"
    for answer in observed["pre_execution_answers"]:
        card += f"\n  {answer['question']}. {answer['name']}: {answer['answer']}"
    if framed:
        pair = len([n for n in route.get("nodes", []) if _frame_node(n)]) == 2
        card += ("\n  frame이 방향과 경로를 조립해 제안합니다"
                 + ("\n  비용: 최상위 모델 두 갈래 · 방향 확인 질문 1회" if pair
                    else "\n  비용: frame 한 갈래 · 방향 확인 질문 1회"))
    sourced = {}
    for node in route["nodes"]:
        for name, source in (node.get("input_sources") or {}).items():
            sourced.setdefault(name, source)
    if sourced:  # SD-163: inputs filled from a prior cycle
        source_dir = os.path.commonpath([str(Path(source["path"]).parent) for source in sourced.values()])
        card += (f"\n  입력 {'·'.join(sourced)} ← {','.join(sorted({s['cycle_id'] for s in sourced.values()}))}"
                 f" {Path(route['artifact_root']) / source_dir}")
    borrowed = list(dict.fromkeys(node["part"] for node in route["nodes"] if node.get("part")))
    if borrowed:  # SD-165: parts taken from another recipe, written under parts/<capability>/<stage>/
        card += f"\n  빌린 부품 {'·'.join(borrowed)}"
    if valid_entry_execution_scope_marker(route.get("entry_scope_contract_version"),
                                         route.get("entry_execution_scope")) and route.get("entry_scope_contract_version") == 1:
        card += f"\n  실행 범위 {route['entry_execution_scope']}"
    else:
        for row in route_start_approvals(route):
            card += f"\n  시작 승인 {row['start_approval']} ({row['part']})"
    from artifact_producer import route_cycle_for, cycle_dir, default_bucket, ProducerError
    try:
        record = route_cycle_for(Path(route["artifact_root"]), route)
    except (ProducerError, OSError, ValueError):
        record = None
    if record is not None:
        folder = cycle_dir(Path(route["artifact_root"]), record["campaign_id"], record["cycle_id"], record)
        card += f"\n  산출물 {folder.resolve() / 'artifacts' / default_bucket(route['capability'])}"
    if plan:
        suffix = " (상속)" if plan_source == "inherited" else ""
        card += f"\n  계획 {' › '.join(plan)}{suffix}"
    if route_plan_unreadable:
        card += "\n  경로 계획을 읽지 못함"
    notice = _compose_spec_read_notice(route)
    if notice:
        card += "\n" + notice
    for advisory in OWNER_WRITE_ADVISORY.advisories(route, owner_harness=owner_harness):
        card += "\n  " + advisory["message"]
    return card


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
    _validate_compose_owner_close(registry, composed_recipe,
                                 "standard" if requested_intensity == "auto" else requested_intensity)
    if capability_mode not in composed_recipe.get("modes", []):
        raise ValueError("composed recipe does not declare the requested capability mode")
    return _compile_from_recipe(
        registry, composed_recipe, composed_recipe["capability"], capability_mode,
        requested_intensity, cwd, artifact_root, composed=True, **kwargs)

def _compile_from_recipe(registry, recipe, capability, capability_mode, requested_intensity,
                         cwd, artifact_root, predicates=(), signals=(), transport=None,
                         transport_evidence="caller-selected", inline_reason=None,
                         tracking="tracked", tracked_gate_evidence=None, dispatch_evidence=None,
                         registered_headless_evidence=None, slug=None, composed=False,
                  route_origin="preset", shape=None, profile_demands=None,
                  explicit_profiles=None, campaign_key=None, parent_cycle_id=None, profile=None,
                  route_plan=None, frameless=False, frame_legs=2):
    dispatch_terminal_commit.require_current_cleanup("route-compile")
    if route_origin not in ROUTE_ORIGINS: raise ValueError("invalid route origin")
    if (capability==ROUTE_FRAME_CAPABILITY) != (shape=="framed"):
        raise ValueError(f"compose-shape-invalid:{shape}")
    if capability==ROUTE_FRAME_CAPABILITY and frame_legs==1:
        recipe=_one_leg_frame_recipe(recipe)
    cwd=Path(cwd).resolve(strict=True); artifact=Path(artifact_root).resolve()
    if not cwd.is_absolute() or not artifact.is_absolute(): raise ValueError("cwd and artifact root must be absolute")
    slug_fields={}
    if slug is not None:
        # A caller-typed leading date is dropped once, here at the origin: every
        # locator built from this slug prefixes the record's own date, so BC_ResNet
        # 2026-09-10 accumulated names like `2026-09-10_2026-09-10-r5-...` and one
        # `2026-09-09_2026-09-10-r4-...` whose two dates disagreed. Migration
        # naming does not pass through here and keeps both dates by design.
        canonical_slug,slug_truncated=ARTIFACT_LOCATOR.slugify(
            ARTIFACT_LOCATOR.strip_leading_date(slug))
        slug_fields={"slug":canonical_slug,"slug_truncated":slug_truncated}
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
    resume_recipe = composed and RESOURCE_RESUME.recipe_selected(recipe) and effective == "quick"
    if composed and effective in ("direct","quick") and not resume_recipe:
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
        # compose fills every direct predicate itself (it has no --predicate
        # flag); label them so the record never claims the caller asserted them.
        predicate_source="compose-default" if route_origin=="compose" else "caller"
        selection_basis=[{"axis":"direct-predicate","signal":p,"source":predicate_source} for p in predicates]
    elif effective=="quick":
        if transport not in (None, "headless"):
            raise ValueError(f"invalid quick transport: {transport!r}")
        if frameless:
            # A leg of an approved route plan was framed already: its one-shot runs without the quick frame pair.
            recipe=_frameless_recipe(recipe)
        if (requested=="direct" and set(predicates)!=known_pred
                and registered_headless_evidence is None):
            # H6: an explicit direct request whose predicates do not all hold
            # used to be silently promoted to quick and died as an opaque
            # `quick-headless-unavailable`. Refuse instead and name the gap.
            # With checked quick evidence the promotion still compiles
            # (`test_ambiguous_quick`); the gaps stay recorded in
            # selection_basis either way.
            raise ValueError(
                "direct-predicate-gap:"
                +",".join(sorted(known_pred-set(predicates))))
        registered_headless_candidates=_validate_registered_headless_evidence(
            registered_headless_evidence
        )
        # Quick's supported harness inventory is carried entirely by this candidate
        # list -- `owner_route_binding._supported_owner_harnesses` reads it, and
        # `dispatch-owner.py` refuses an `--adapter` outside it. How many
        # harnesses it supports decides the pair's diversity, sealed on both
        # frame nodes by `_quick_frame_diversity` (shared with `verify_route`).
        #
        # Deliberately NOT touched: `EVIDENCE_CONSUMER_DISPATCH_DEPTH` and the
        # depth-2 evidence-consumer path. Quick has no depth-2 node, so making
        # that depth configurable would mean redefining five call sites plus
        # fallback-chain attachment for no gain here.
        _frame_diversity=_quick_frame_diversity(registered_headless_candidates)
        transport="headless"
        owner_model_profile=registry["owner_profile_by_intensity"]["quick"]
        # What quick's frame legs LOSE compared to standard+: no
        # `dispatch_evidence.tuples` per-field sealing of
        # parent_transport/parent_sandbox/child_harness, and no `fallback_hops`
        # chain at all. Recovery from a dead quick frame leg is an explicit
        # depth-0 re-launch, never a machine fallback hop. Do not read quick's
        # frame pair as carrying the standard+ guarantee.
        _quick_frame=lambda node_id,profile:{
                "id":node_id,"kind":"map-worker","depends_on":[],"role":"deep maker",
                "unit":"plan/frame","worker_type":"frame","dispatch_depth":1,
                "launch_authority":"depth-0","model_profile":profile,
                "inputs":["task"],
                "outputs":[f"shards/{node_id}/direction-brief.md"],
                "write_scope":[f"shards/{node_id}/**"],"resource_class":"normal",
                "execution_surface":"registered-headless","registered_worker":True,
                "completion_gate":"quick-frame",
                "harness_diversity":_frame_diversity,
                "continuation":{"kind":"human-gate","gate":"frame-review"},
                "advance_class":"runtime-eligible","commit_expected":False}
        nodes=[_quick_frame("frame","balanced-deep"),
               _quick_frame("frame-alternative","light"),
               {"id":"one-shot","kind":recipe["quick"]["worker_kind"],"dispatch_depth":1,"role":"orchestrator",
                "depends_on":["frame","frame-alternative"],
                "unit":"_kernel/owner","worker_type":"owner",
                "model_profile":owner_model_profile,
                "write_scope":recipe["quick"]["write_scope"],"resource_class":"normal",
                "execution_surface":"registered-headless","registered_worker":True,
                "completion_gate":"quick-complete",
                "terminal":True,"terminal_gate":"quick-complete"}]
        if recipe["quick"].get("inline_human_gates"):
            nodes[-1]["inline_human_gates"] = list(recipe["quick"]["inline_human_gates"])
        gates=["quick-frame","quick-complete"]
        if not _recipe_has_frame(recipe):
            nodes = [nodes[-1]]
            nodes[0]["depends_on"] = []
            gates = ["quick-complete"]
        if resume_recipe:
            nodes = RESOURCE_RESUME.nodes(recipe, owner_model_profile)
            gates = ["resource-exit", "quick-complete"]
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
    if effective not in ("direct","quick") and _single_owner_nodes(nodes):
        registered_headless_candidates = _validate_registered_headless_evidence(registered_headless_evidence)
    elif effective not in ("direct","quick"):
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
    if profile is not None:
        if profile not in PROFILE.PORTABLE_PROFILES:
            raise ValueError("profile-explicit-unknown:" + str(profile))
        explicit_profiles = {**{n["id"]: profile for n in nodes if not _no_model_node(n)},
                             "__owner__": profile, **(explicit_profiles or {})}
    profile_demands, explicit_profiles = _profile_input_maps(nodes, profile_demands, explicit_profiles)
    owner_demand = profile_demands.get("__owner__")
    owner_model_profile, owner_profile_selection = _resolve_owner_profile(
        effective, registry, owner_demand, explicit_profiles.get("__owner__"))
    resolved_owner_profile = owner_profile_selection["resolved_profile"]
    if owner_demand is not None or "__owner__" in explicit_profiles:
        # Quick's one-shot is the owner process, so there is one selection.
        # Semantic owner stages in standard+ remain independently selected.
        for node in nodes:
            if _owner_node(node, effective):
                node["model_profile"] = resolved_owner_profile
                node["profile_explicit"] = True
                node["profile_demand"] = owner_demand
    # The frame bootstrap tier ladder, applied for BOTH shapes at once, after
    # `resolved_owner_profile` and before `_seal_profile_demands`. Quick's
    # frame pair is built literally above by `_quick_frame`, and the five
    # standard+ recipes declare theirs in `topologies.json` -- both carry a
    # static `model_profile` that CANNOT be right, because the correct value
    # depends on the owner profile this route just resolved, which no static
    # recipe field can see. Stamping unconditionally is exactly what demotes
    # those static values to placeholders instead of letting one decision live
    # in two homes. The one home is `model_profile.FRAME_PROFILE_LADDER`.
    _stamp_frame_profiles(nodes, resolved_owner_profile, owner_demand)
    legacy_nodes = not composed or _versioned_subgraph(registry, recipe)
    _seal_profile_demands(nodes, profile_demands, explicit_profiles, legacy=legacy_nodes)
    for node in nodes:
        if _owner_node(node, effective) and node["model_profile"] != owner_model_profile:
            raise ValueError("owner-node-profile-selection-conflict:" + node["id"])
    dispatch_defaults_digest,dispatch_allocation,owner_harness_policy=_seal_dispatch_defaults(
        nodes, capability, owner_model_profile
    )
    spec_touch=any(_scope_touches_spec(scope) for node in nodes for scope in node["write_scope"])
    # Seal the same finite workload derivation used by legacy bound routes.
    _continuation_declared_nodes=len(nodes)
    _continuation_retry_slots=len(set(recipe["resume_retry_boundaries"]))
    _continuation_review_round_cap=REVIEW_ROUND_CAP.max_review_rounds(effective)
    _continuation_terminal_nodes=sum(1 for node in nodes if node.get("terminal") is True)
    _continuation_ordinary=derive_workload_ordinary(
        declared_nodes=_continuation_declared_nodes,
        retry_slots=_continuation_retry_slots,
        review_round_cap=_continuation_review_round_cap,
        terminal_nodes=_continuation_terminal_nodes,
    )
    continuation_budget={
      "contract_version":1,
      "declared_nodes":_continuation_declared_nodes,
      "review_round_cap":_continuation_review_round_cap,
      "retry_slots":_continuation_retry_slots,
      "terminal_nodes":_continuation_terminal_nodes,
      "gap":1,"retry":1,"reserved":TERMINAL_RESERVE_DEFAULT,
      "ordinary":_continuation_ordinary,
      "limit":_continuation_ordinary+TERMINAL_RESERVE_DEFAULT,
    }
    validation_basis=_validation_basis()
    payload={
      "schema_version":ROUTE_SCHEMA_VERSION,"capability":capability,"capability_mode":capability_mode,
      "requested_intensity":requested_intensity,"effective_intensity":effective,
      "owner_model_profile":owner_model_profile,
      "profile_selection_contract_version":1,
      "persona_independence_contract_version":1,
      "profile_demands":profile_demands,"explicit_profiles":explicit_profiles,
      "owner_profile_demand":owner_demand,"owner_profile_selection":owner_profile_selection,
      "execution_topology":("resource+verification" if resume_recipe else "inline" if effective=="direct" else recipe["quick"]["topology"] if effective=="quick" else recipe["topology_class"]),
      "owner_dispatch_depth":0 if effective=="direct" else (recipe["quick"]["owner_dispatch_depth"] if effective=="quick" else recipe["standard_plus"]["owner_dispatch_depth"]),
      "max_dispatch_depth":recipe["quick"]["max_dispatch_depth"] if effective=="quick" else (0 if effective=="direct" else recipe["standard_plus"]["max_dispatch_depth"]),
      "tracking":tracking,"tracked_gate_evidence":evidence,"spec_touch":spec_touch,
      "cwd":str(cwd),"artifact_root":str(artifact),"source_commit":_git_commit(cwd),
      "registry_digest":TOPO.registry_digest(registry),
      "capability_registry_digest":TOPO.capability_registry_digest(
          registry, capability, [recipe] if composed else ()),
      "dispatch_defaults_digest":dispatch_defaults_digest,
      "dispatch_allocation":dispatch_allocation,
      "owner_harness_policy":owner_harness_policy,
      "confirmation_mode":_seal_confirmation_mode(),
      "small_work_confirmation":_seal_small_work_confirmation(),
      "selection":{"direct_predicates":predicates,"promotion_signals":[{"signal":s,"source":"caller"} for s in signals],
                   "selection_basis":selection_basis,
                   "escalation_basis":[{"signal":s,"source":"caller"} for s in signals],
                   "transport":transport,"transport_evidence":transport_evidence,"inline_reason":inline_reason,
                   "route_origin":route_origin,"shape":shape or shape_for_intensity(effective)},
      "continuation_budget":continuation_budget,
      "nodes":nodes,"parallel_groups":_realized_parallel_groups(nodes),
      "conditional_extensions":_realize_conditional_extensions(recipe, effective),
      "completion_gates":gates,
      # The portable recipe owns whether this capability has a frame layer.
      # Quick must not extend it to capabilities outside the five recipes.
      "human_gates":(sorted(set(recipe["human_gates"])|{"frame-review"})
                     if effective=="quick" and _recipe_has_frame(recipe) else recipe["human_gates"]),
      # Quick binds the frame gate at `one-shot`'s entry: that is quick's only
      # fence point, and without it quick runs with no check at all. Only
      # `direct` (which has no owner and no worker) still binds nothing.
      "human_gate_bindings":json.loads(json.dumps(
          _quick_gate_bindings(recipe) if effective=="quick"
          else recipe["human_gate_bindings"] if effective!="direct" else [])),
      "workflow_contract":_workflow_contract(
          registry, nodes,
          _quick_gate_bindings(recipe) if effective=="quick"
          else recipe["human_gate_bindings"] if effective!="direct" else []),
      "resume_retry_boundaries":recipe["resume_retry_boundaries"],
      "dispatch_evidence":checked_dispatch,
      "dispatch_contract_version":DISPATCH_CONTRACT_VERSION,
      "registered_headless_candidates":registered_headless_candidates,
      "registered_headless_policy":"serial-attempt" if effective=="quick" else None,
      "unit_catalog_digest":unit_catalog_digest(),
      "validation_basis":validation_basis,
      "launch_compatibility_tuple":{
          "contract_version":LAUNCH_COMPATIBILITY_TUPLE_VERSION,
          **launch_compatibility_tuple(artifact_root=artifact,cwd=cwd),
      },
      "advance_generation":0,
      "runtime_support":{"terminal_commit":_seal_terminal_commit_support(validation_basis),
                         "terminal_commit_contract":RUNTIME_SUPPORT.TERMINAL_COMMIT_CONTRACT,
                         "terminal_handoff_contract":RUNTIME_SUPPORT.TERMINAL_HANDOFF_CONTRACT,
                         "producer_binding_contract":RUNTIME_SUPPORT.PRODUCER_BINDING_CONTRACT}}
    if GPU_SANDBOX.gpu_resource_nodes(payload):
        payload["codex_execution_sandbox"] = GPU_SANDBOX.select(payload)
    payload.update(slug_fields)
    for key, value in (("campaign_key", campaign_key), ("parent_cycle_id", parent_cycle_id)):
        if value is not None:
            _validate_campaign_selection(key, value)
            payload[key] = value
    if checked_dispatch is not None:
        payload["dispatch_evidence_scope_version"]=DISPATCH_EVIDENCE_SCOPE_VERSION
    if composed:
        payload["composed"]=True
        payload["composed_recipe"]=json.loads(json.dumps(recipe))
    if route_plan is not None:
        payload["route_plan"]=json.loads(json.dumps(route_plan))
    digest=route_hash(payload); payload["route_hash"]=digest; payload["route_id"]=ROUTE_IDENTITY.route_id_from_hash(digest)
    owner_attempt_id=_resolve_owner_attempt_id()
    payload["owner_attempt_id"]=owner_attempt_id
    payload["route_family_key"]=route_family_key(capability,cwd,capability_mode,owner_attempt_id)
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
                              registry_root_now, unit_catalog_root_now,
                              capability_digest_now=None):
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
        # The whole registry moved, but the parts this route derives from did
        # not (an edit to another capability): the sealed graph is still valid.
        sealed_capability = route.get("capability_registry_digest")
        if (axis == "registry" and capability_digest_now is not None
                and isinstance(sealed_capability, str) and sealed_capability == capability_digest_now):
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
                f"validator={own_digest}@{own_root}); re-run via the tooling "
                f"under {sealed_root} (the root that created the row)"
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

def _validate_campaign_selection(key, value):
    pattern = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}" if key == "campaign_key" else r"cyc_[a-f0-9]{32}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"route-{key.replace('_', '-')}-invalid")


def _sd160_legacy_registry(registry):
    """Reconstruct only the immediately preceding cross-harness declaration.

    This is an exact digest bridge, never a general stale-registry exemption.
    Any changed node, width, budget, role or other registry byte changes its
    digest and remains refused. Old sealed documents themselves are untouched.
    """
    legacy = json.loads(json.dumps(registry))
    changed = False
    for recipe in legacy["recipes"]:
        for graph in (v for v in recipe.values() if isinstance(v, dict)):
            for group in graph.get("parallel_groups", []):
                if group.get("independence_axes") == ["model-profile", "perspective"]:
                    group["independence_axes"] = ["cross-harness", "model-profile", "perspective"]
                    changed = True
    return legacy if changed else None


def _entry_scope_legacy_registry(registry):
    """Reconstruct only the catalog immediately before the refine preview mark."""
    legacy = json.loads(json.dumps(registry))
    row = ((legacy.get("part_catalog") or {}).get("parts") or {}).get("autopilot-refine:transaction")
    if not isinstance(row, dict) or row.get("start_approval") != "preview":
        return None
    recipe = next((item for item in legacy.get("recipes", [])
                   if item.get("capability") == "autopilot-refine"), None)
    review = next((node for node in ((recipe or {}).get("standard_plus") or {}).get("nodes", [])
                   if node.get("id") == "review"), None)
    if not isinstance(review, dict) or "reviews/refine/preview.md" not in review.get("outputs", []):
        return None
    del row["start_approval"]
    review["outputs"] = [output for output in review["outputs"] if output != "reviews/refine/preview.md"]
    return legacy


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
    if "work_request" in route:
        from work_start import validate_request
        validate_request(route["work_request"])
    _verify_selection_pins(route)
    if "route_plan" in route:
        # Only the field's own format is verified; the record it names may be gone without
        # making the route unverifiable (`next_leg` simply has nothing to read then).
        import route_plan as RP
        RP.validate_sealed(route["route_plan"])
    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        raise ValueError(
            f"legacy route schema_version={route.get('schema_version')!r} rejected for mutating/resume use"
        )
    for key in ("campaign_key", "parent_cycle_id"):
        if key in route:
            _validate_campaign_selection(key, route[key])
    if "slug" in route:
        if not isinstance(route["slug"],str) or not isinstance(route.get("slug_truncated"),bool):
            raise ValueError("invalid route slug metadata")
        try:
            canonical_slug,_=ARTIFACT_LOCATOR.slugify(route["slug"])
        except ARTIFACT_LOCATOR.LocatorError as exc:
            raise ValueError("invalid route slug") from exc
        if canonical_slug != route["slug"]:
            raise ValueError("route slug is not canonical")
    elif "slug_truncated" in route:
        raise ValueError("route slug metadata incomplete")
    if route.get("dispatch_contract_version") != DISPATCH_CONTRACT_VERSION:
        raise ValueError("legacy dispatch contract is read-only")
    scope_version=route.get("dispatch_evidence_scope_version")
    if scope_version not in (None, DISPATCH_EVIDENCE_SCOPE_VERSION):
        raise ValueError("unsupported dispatch evidence scope version")
    if route.get("route_hash") != route_hash(route):
        if gates_on():
            raise ValueError("stale or modified route hash")
        same_work_or_refuse("route-hash-mismatch")
    if route.get("route_id") != "rt-"+route["route_hash"].split(":",1)[1][:16]:
        if gates_on() or not re.fullmatch(r"rt-[0-9a-f]{16}", str(route.get("route_id") or "")):
            raise ValueError("invalid route id")
        same_work_or_refuse("route-id-mismatch", str(route.get("route_id")))
    if expected_cwd and Path(expected_cwd).resolve()!=Path(route["cwd"]):
        if gates_on():
            raise ValueError("route cwd mismatch")
        same_work_or_refuse("route-cwd-mismatch", str(expected_cwd))
        route = dict(route, cwd=str(Path(expected_cwd).resolve()))
    _verify_profile_contract(route)
    scope_version = route.get("entry_scope_contract_version")
    entry_scope = route.get("entry_execution_scope")
    if not valid_entry_execution_scope_marker(scope_version, entry_scope):
        raise ValueError("route nodes differ from the declared recipe")
    basis=_check_validation_basis(route, allow_stale_registry=allow_stale_registry)
    if basis is _DEGRADE_VALIDATION_BASIS:
        # An unsupported basis_version is a legitimate newer harness's route;
        # closure records it honestly as unproven rather than stranding it.
        return dict(route, _registry_current=False)
    registry=TOPO.load_registry()
    capability_digest_now=None
    if isinstance(route.get("capability_registry_digest"), str) and isinstance(route.get("capability"), str):
        extra=[route["composed_recipe"]] if route.get("composed") and isinstance(route.get("composed_recipe"), dict) else ()
        capability_digest_now=TOPO.capability_registry_digest(registry, route["capability"], extra)
    classification=classify_validation_basis(
        route, registry_digest_now=TOPO.registry_digest(registry),
        units_digest_now=unit_catalog_digest(),
        registry_root_now=TOPO.ROOT, unit_catalog_root_now=ROOT,
        capability_digest_now=capability_digest_now,
    )
    persona_version = route.get("persona_independence_contract_version")
    if persona_version is not None and (type(persona_version) is not int or persona_version != 1):
        raise ValueError("unsupported-persona-independence-contract-version")
    if (classification["registry"]["verdict"] != "current"
            and classification["unit_catalog"]["verdict"] == "current"
            and persona_version is None):
        legacy_registry = _sd160_legacy_registry(registry)
        if (legacy_registry is not None
                and route.get("registry_digest") == TOPO.registry_digest(legacy_registry)):
            # Reconstruct the old graph under its exact authenticated declaration;
            # launch/receipt consumers apply SD-160's effective persona policy.
            registry = legacy_registry
            classification = classify_validation_basis(
                route, registry_digest_now=TOPO.registry_digest(registry),
                units_digest_now=unit_catalog_digest(),
                registry_root_now=TOPO.ROOT, unit_catalog_root_now=ROOT,
            )
    if (classification["registry"]["verdict"] != "current"
            and classification["unit_catalog"]["verdict"] == "current"):
        legacy_registry = _entry_scope_legacy_registry(registry)
        if legacy_registry is not None and route.get("registry_digest") == TOPO.registry_digest(legacy_registry):
            extra = [route["composed_recipe"]] if route.get("composed") and isinstance(route.get("composed_recipe"), dict) else ()
            legacy_capability_digest = (TOPO.capability_registry_digest(legacy_registry, route["capability"], extra)
                                       if route.get("capability_registry_digest") is not None else None)
            classification = classify_validation_basis(
                route, registry_digest_now=TOPO.registry_digest(legacy_registry),
                units_digest_now=unit_catalog_digest(), registry_root_now=TOPO.ROOT,
                unit_catalog_root_now=ROOT, capability_digest_now=legacy_capability_digest)
            if classification["verdict"] == "current":
                registry = legacy_registry
    if classification["verdict"] != "current":
        if not allow_stale_registry:
            if gates_on():
                raise ValueError(classification["message"])
            same_work_or_refuse("route-registry-stale", classification["message"])
            _validate_output_scopes(route.get("nodes", []))
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
        resume_recipe = RESOURCE_RESUME.route_selected(route)
        if route.get("effective_intensity") in ("direct","quick") and not resume_recipe:
            raise ValueError("composed routes require a standard+ effective intensity")
        composed_recipe=route.get("composed_recipe")
        if not isinstance(composed_recipe, dict):
            raise ValueError("composed route lacks embedded composed_recipe")
        TOPO._validate_recipe(
            composed_recipe, registry,
            registry["owner_profile_by_intensity"]["standard"],
        )
        _validate_compose_owner_close(registry, composed_recipe, route["effective_intensity"])
        if resume_recipe and not _versioned_subgraph(registry, composed_recipe):
            raise ValueError("resume recipe differs from the declared catalog")
        expected_nodes=(RESOURCE_RESUME.nodes(composed_recipe, registry["owner_profile_by_intensity"]["quick"])
                        if resume_recipe else json.loads(json.dumps(composed_recipe["standard_plus"]["nodes"])))
        expected_nodes=_expand_parallel_groups(
            expected_nodes, composed_recipe["standard_plus"].get("parallel_groups"),
            route.get("effective_intensity"), route.get("capability"),
            auxiliary_check_units=registry.get("auxiliary_check_units"),
            persona_policy=persona_version == 1)
        _project_entry_scope_nodes(expected_nodes, entry_scope)
        if route.get("profile_selection_contract_version") == 1:
            # Same ladder, same order as the compiler: stamp, then seal (under the policy the
            # route's frame nodes were declared with).
            expected_nodes=_expected_nodes_under_frame_policy(
                expected_nodes, route, persona_version=persona_version,
                legacy=_versioned_subgraph(registry, composed_recipe),
                accepts=lambda nodes: ([_node_identity(n) for n in route.get("nodes",[])]
                                       == [_node_identity(n) for n in nodes]))
        if ([_node_identity(n) for n in route.get("nodes",[])]
                != [_node_identity(n) for n in expected_nodes]):
            raise ValueError("composed route nodes differ from embedded composed recipe")
        route_recipe=composed_recipe
    else:
        route_recipe=TOPO.resolve_recipe(
            registry, route.get("capability"), route.get("capability_mode")
        )
        decided=((route.get("selection") or {}).get("route_origin")=="compose"
                 and (route.get("selection") or {}).get("shape") in DECIDED_SHAPES
                 and not any(_frame_node(n) for n in route.get("nodes",[])))
        if (route.get("route_plan") is not None or decided) and route.get("effective_intensity")=="quick":
            # A leg of a plan, or a decided compose shape; a route sealed with its frame pair keeps it.
            route_recipe=_frameless_recipe(route_recipe)
        if (route.get("capability")==ROUTE_FRAME_CAPABILITY
                and [n.get("id") for n in route.get("nodes",[])]==TOPO.ROUTE_FRAME_ONE_LEG_NODE_IDS):
            route_recipe=_one_leg_frame_recipe(route_recipe)
        if route.get("effective_intensity") not in ("direct", "quick"):
            expected_nodes=json.loads(json.dumps(route_recipe["standard_plus"]["nodes"]))
            expected_nodes=_expand_parallel_groups(
                expected_nodes, route_recipe["standard_plus"].get("parallel_groups"),
                route.get("effective_intensity"), route.get("capability"),
                auxiliary_check_units=registry.get("auxiliary_check_units"),
            persona_policy=persona_version == 1)
            _project_entry_scope_nodes(expected_nodes, entry_scope)
            if route.get("profile_selection_contract_version") == 1:
                # Same ladder, same order as the compiler: stamp, then seal (under the policy the
                # route's frame nodes were declared with).
                def declared(nodes):
                    by_id = {n["id"]: n for n in nodes}
                    return not any(
                        (expected := by_id.get(node.get("id"))) and not _no_model_node(node) and any(
                            node.get(key) != expected.get(key)
                            for key in ("profile_demand", "profile_selection", "model_profile"))
                        for node in route.get("nodes", []))
                expected_nodes=_expected_nodes_under_frame_policy(
                    expected_nodes, route, persona_version=persona_version, legacy=True,
                    accepts=declared)
                by_id = {n["id"]: n for n in expected_nodes}
                for node in route.get("nodes", []):
                    expected = by_id.get(node.get("id"))
                    if expected and not _no_model_node(node) and any(
                        node.get(key) != expected.get(key)
                        for key in ("profile_demand", "profile_selection", "model_profile")):
                        raise ValueError("node-profile-declaration-mismatch:" + node["id"])
            # The remaining verifier owns field-level diagnostics.  This
            # census closes only the undeclared fanout hole: a rehashed route
            # may not add, remove, reorder, or rename recipe nodes.
            if ([n.get("id") for n in route.get("nodes", [])]
                    != [n.get("id") for n in expected_nodes]):
                raise ValueError("route nodes differ from the declared recipe")
            if (route.get("capability") == ROUTE_FRAME_CAPABILITY
                    and route.get("profile_selection_contract_version") == 1
                    and [_node_identity(n) for n in route.get("nodes", [])]
                    != [_node_identity(n) for n in expected_nodes]):
                # The framed route's model-less terminal is accepted only as exactly the recipe's node.
                raise ValueError("route-frame nodes differ from the sealed recipe")
    if (route.get("capability") == ROUTE_FRAME_CAPABILITY) != (route.get("selection", {}).get("shape") == "framed"):
        raise ValueError("route-frame-shape-mismatch")
    if route.get("capability") == ROUTE_FRAME_CAPABILITY and (
            route.get("composed") or route.get("effective_intensity") != "standard"):
        raise ValueError("route-frame-shape-mismatch")
    expected_extensions=_realize_conditional_extensions(
        route_recipe, route.get("effective_intensity")
    )
    if route.get("conditional_extensions") != expected_extensions:
        raise ValueError("route conditional extensions differ from the sealed recipe")
    route_node_ids={node.get("id") for node in route.get("nodes", [])}
    if any(not set(row["after"]) <= route_node_ids for row in expected_extensions):
        raise ValueError("route conditional extension anchor is not realized")
    # Mirror of the compiler: quick binds its own frame gate, only `direct`
    # binds nothing. Verifier and compiler must move together or a quick route
    # compiles and then refuses to verify.
    expected_bindings=json.loads(json.dumps(
        _quick_gate_bindings(route_recipe) if route.get("effective_intensity")=="quick"
        else route_recipe["human_gate_bindings"]
        if route.get("effective_intensity")!="direct" else []))
    if scope_version == 1:
        expected_bindings = [row for row in expected_bindings if row.get("gate") != "preview-disposition"]
    if route.get("human_gate_bindings") != expected_bindings:
        raise ValueError("route human gate bindings differ from the sealed recipe")
    if route.get("workflow_contract") != _workflow_contract(
            registry, route.get("nodes",[]), expected_bindings):
        raise ValueError("route workflow contract differs from the realized stage graph")
    if {row["gate"] for row in expected_bindings} - set(route.get("human_gates") or []):
        raise ValueError("route binds an undeclared human gate")
    if (scope_version != 1 and route.get("effective_intensity") != "direct"
            and "preview-disposition" in (route.get("human_gates") or [])):
        preview_raisers = [n for n in route.get("nodes", [])
                           if n.get("continuation") == {"kind": "human-gate", "gate": "preview-disposition"}
                           or "preview-disposition" in n.get("inline_human_gates", [])]
        preview_bindings = [b for b in expected_bindings if b.get("gate") == "preview-disposition"]
        if not preview_raisers or not preview_bindings:
            raise ValueError("preview-approval-boundary-missing")
    if route.get("owner_dispatch_depth") not in {0, 1} or route.get("max_dispatch_depth") not in {0, 1, 2}:
        raise ValueError("invalid qualified dispatch depth")
    if any(key in route for key in ("depth", "owner_depth", "max_depth")):
        raise ValueError("bare route dispatch-depth fields are forbidden")
    allocation = route.get("dispatch_allocation")
    if allocation is not None:
        required = {"strategy", "window", "harness_order"}
        optional = {"usage_gate_used_percent", "depth_affinity", "depth_affinity_weight", "usage_headroom_exponent",
                    "harness_weights", "owner_order"}
        if not isinstance(allocation, dict) or not required <= set(allocation) or set(allocation) - required - optional:
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
        affinity = allocation.get("depth_affinity", {})
        if not isinstance(affinity, dict) or not set(affinity) <= {"owner", "worker"}:
            raise ValueError("invalid dispatch_allocation depth affinity")
        if any(value not in DEFAULTS.DISPATCHABLE_HARNESSES for value in affinity.values()):
            raise ValueError("invalid dispatch_allocation depth affinity value")
        if "owner_order" in allocation and not DEFAULTS.valid_owner_order(allocation["owner_order"]):
            raise ValueError("invalid dispatch_allocation owner order")
        weight = allocation.get("depth_affinity_weight", 0.5)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0.0 <= weight <= 1.0:
            raise ValueError("invalid dispatch_allocation affinity weight")
        exponent = allocation.get("usage_headroom_exponent", 1)
        if isinstance(exponent, bool) or not isinstance(exponent, int) or not 1 <= exponent <= 4:
            raise ValueError("invalid dispatch_allocation headroom exponent")
        if "harness_weights" in allocation:
            weights = allocation["harness_weights"]
            if not isinstance(weights, dict) or any(
                name not in DEFAULTS.DISPATCHABLE_HARNESSES
                or isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0.0 < value <= 1.0
                for name, value in weights.items()
            ):
                raise ValueError("invalid dispatch_allocation harness weights")
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
    selected_owner, _ = _resolve_owner_profile(
        effective, registry, route.get("owner_profile_demand"),
        (route.get("explicit_profiles") or {}).get("__owner__"))
    if route.get("owner_model_profile") != selected_owner:
        raise ValueError("owner_model_profile differs from the portable owner selection")
    # The owner's own node (quick `one-shot`) must carry the profile the route
    # sealed for the owner (the policy check above already admitted it, `top`
    # included); a standard+ recipe's semantic owner node keeps the portable
    # policy profile below.
    owner_profile=route.get("owner_model_profile")
    expected_owner_profile=(
        None if effective=="direct" else registry["owner_profile_by_intensity"].get(effective)
    )
    for node in route.get("nodes",[]):
        if _owner_node(node, effective) and node.get("model_profile")!=owner_profile:
            raise ValueError(f"owner node {node.get('id')} profile differs from owner_model_profile")
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
        # A profile's policy may leave a pool harness out (for example OpenCode
        # is not used for deep work); it may never name one outside the pool.
        if not owner_set <= set(allocation["harness_order"]):
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
            # The `top` exception profile is unregistered on purpose, so only
            # the two node classes allowed to hold it skip the registered check:
            # the owner, and a depth-1 frame anchor leg.
            top_owner_node = profile == PROFILE.TOP_PROFILE and (
                _owner_node(node, effective) or _frame_node(node)
            )
            if not top_owner_node and (
                not isinstance(row, dict) or row.get("registered_topology") is not True
            ):
                raise ValueError(f"node {node.get('id')} has invalid registered model_profile")
        if (
            effective not in ("direct", "quick")
            and node.get("kind") == "capability-owner"
            and node.get("unit") == "_kernel/owner"
            and (
                node.get("dispatch_depth") != 1
                or (route.get("profile_selection_contract_version") is None
                    and node.get("model_profile") != expected_owner_profile)
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
        resume_recipe = RESOURCE_RESUME.route_selected(route)
        if (
            route.get("owner_dispatch_depth") != 1
            or route.get("max_dispatch_depth") != 1
            or selection.get("transport") != "headless"
            or selection.get("inline_reason") is not None
            # `serial-attempt` is a per-(route_id, route_node) attempt budget,
            # so it stays true unchanged with three nodes. `max_dispatch_depth`
            # stays 1 because the frame legs are depth 1 as well.
            or route.get("registered_headless_policy") != "serial-attempt"
            or len(route.get("nodes",[])) != (2 if resume_recipe else 3 if _recipe_has_frame(route_recipe) else 1)
        ):
            raise ValueError("quick route shape mismatch")
        candidates=_validate_registered_headless_evidence({
            "candidates":route.get("registered_headless_candidates")
        })
        if candidates != route.get("registered_headless_candidates"):
            raise ValueError("quick registered-headless evidence is not canonical")
        diversity=_quick_frame_diversity(candidates)
        if any(n.get("harness_diversity") != diversity
               for n in route.get("nodes",[]) if _frame_node(n)):
            raise ValueError("quick route frame harness diversity mismatch")
        node=next((n for n in route["nodes"] if n.get("id")=="one-shot"), None)
        if node is None:
            raise ValueError("quick route shape mismatch")
        expected_inline_gates = list(route_recipe["quick"].get("inline_human_gates", []))
        if scope_version == 1:
            expected_inline_gates = [gate for gate in expected_inline_gates if gate != "preview-disposition"]
        if node.get("inline_human_gates", []) != expected_inline_gates:
            raise ValueError("quick-inline-human-gates-mismatch")
        expected_scope = (route_recipe["standard_plus"]["nodes"][1]["write_scope"] if resume_recipe
                          else route_recipe["quick"]["write_scope"])
        if node.get("write_scope") != expected_scope:
            raise ValueError("quick-write-scope-mismatch")
        if (
            node.get("dispatch_depth") != 1
            or node.get("unit") != "_kernel/owner"
            or node.get("model_profile") != owner_profile
            or node.get("execution_surface") != "registered-headless"
            or node.get("registered_worker") is not True
            or node.get("fallback_hops")
            or sorted(node.get("depends_on") or []) !=
               (["resume-run"] if resume_recipe else ["frame","frame-alternative"] if _recipe_has_frame(route_recipe) else [])
        ):
            raise ValueError("quick node axes mismatch")
        frame_legs=[n for n in route["nodes"] if _frame_node(n)]
        if sorted(n.get("id") for n in frame_legs) != (
                ["frame","frame-alternative"] if _recipe_has_frame(route_recipe) else []):
            raise ValueError("quick route frame pair mismatch")
        for leg in frame_legs:
            if (
                leg.get("execution_surface") != "registered-headless"
                or leg.get("registered_worker") is not True
                or leg.get("fallback_hops")
                or leg.get("launch_authority") != "depth-0"
            ):
                raise ValueError("quick frame leg axes mismatch")
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
    if effective not in ("direct","quick") and _single_owner_nodes(route.get("nodes",[])):
        candidates = _validate_registered_headless_evidence({"candidates": route.get("registered_headless_candidates")})
        if (candidates != route.get("registered_headless_candidates")
                or route.get("registered_headless_policy") is not None
                or route.get("dispatch_evidence") is not None
                or route["nodes"][0].get("fallback_hops")
                or route.get("selection",{}).get("transport") != "headless"):
            raise ValueError("owner-only registered-headless evidence mismatch")
    elif route.get("effective_intensity") not in ("direct","quick"):
        if route.get("registered_headless_candidates") is not None:
            raise ValueError("nested route cannot substitute owner-only readiness")
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
        _index_published_route(path, payload)
        return
    with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write(data); fh.flush(); os.fsync(fh.fileno())
    _index_published_route(path, payload)


def _index_published_route(path, payload):
    if isinstance(payload, dict) and payload.get("route_id") and "nodes" in payload:
        import directory_record_index
        directory_record_index.published(path, payload, kind="route-children",
                                         classify=directory_record_index.route_keys,
                                         ignored=directory_record_index.ROUTE_SIDECARS)

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
def terminal_gate_observation(route, *, jobs=None, exact_terminal=False):
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
        marker=completion_dir(route["route_id"],jobs=jobs)/f"{node_id}.json"
        if owner_executed_terminal(node) and not (marker.exists() or marker.is_symlink()):
            rows[node_id]=_owner_terminal_observation(route,node,jobs=jobs)
        else:
            rows[node_id]=_marker_identity_row(route,node,node_id,node.get("terminal_gate"), jobs=jobs,
                                              exact_terminal=exact_terminal)
    for group_id,error in sorted(owner_merge_auxiliary_groups(route).items()):
        key=f"parallel_group:{group_id}"
        row=_arbitration_observation(route,group_id,error)
        if exact_terminal and row.get("passed"):
            try:
                raw=arbitration_path(route["route_id"],group_id).read_bytes()
                record=json.loads(raw); anchor=nodes[record["anchor_node"]]
                proof=_marker_identity_row(route,anchor,anchor["id"],anchor["completion_gate"],
                                           jobs=jobs,exact_terminal=True)
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


def _owner_terminal_observation(route,node,*,jobs=None):
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
        owners=[]
        for line in jobs.read_text(encoding="utf-8").splitlines():
            fields=line.split("\t")
            if len(fields)!=6: continue
            meta=parse_registry_metadata(fields[5])
            if meta.get("worker_type")!="owner" or meta.get("dispatch_depth")!="1": continue
            try:
                identity=ROUTE_IDENTITY.registered_node_identity(meta,node)
            except ValueError:
                continue
            if identity==(route["route_id"],route["route_hash"],node["id"]):
                owners.append((fields,meta))
        if not owners: return absent("owner-attempt-absent")
        fields,meta=owners[-1]
        binding,_=resolve_owner_route_lifecycle(jobs,owner_attempt_id=meta["attempt_id"])
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
        prerequisites=owner_terminal_prerequisites(route,node,jobs)
        if prerequisites:
            return absent("owner-prerequisite-unproven:"+json.dumps(prerequisites,sort_keys=True))
        # The owner's own operation is a node entry like any other: a PASS does not stand in
        # for a person's release of the gate sealed at its entry (refine's preview approval).
        try:
            owner_operation_fence(route,node,jobs=jobs)
        except DispatchContractError as exc:
            return {**absent(exc.reason),"gate_detail":exc.detail}
        terminal=inspect_terminal_attempt(meta.get("log_file"),worktree=route["cwd"],
                                          artifact_root_metadata=route["artifact_root"],worker_type="owner")
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


def owner_terminal_prerequisites(route,node,jobs):
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
                                   jobs=jobs,exact_terminal=predecessor.get("dispatch_depth") in (1,2))
        if not proof.get("passed"): missing[node_id]=proof["reason"]
    return missing

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

canonical_route_path = ROUTE_LINEAGE.canonical_route_path

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
    try:
        dfd=os.open(str(path.parent),os.O_RDONLY); os.fsync(dfd); os.close(dfd)
    except OSError:
        pass

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
             "head_commit":commit or _head_commit(route["cwd"]),"summary":summary,
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


def route_status(artifact_root, *, diagnostics=None, open_only=False):
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
    canonical=canonical_routes_dir(artifact_root)
    search_dirs=[canonical,root,root/"routes",root/"_routes",root/".routes"]
    by_route_id={}
    rows=[]
    for search_dir in search_dirs:
        if not search_dir.is_dir(): continue
        with os.scandir(search_dir) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
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
    claim=None, jobs=None, route_id=None, node_id=None,
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
            row = _find_attempt_row_metadata(Path(jobs), reviewer_attempt)
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
        ):
            return degraded("reviewer-attempt-foreign-route-node")
        # A reviewer that was launched and died produced no verdict. `done` plus
        # a non-`dead-*` note is the contract's own definition of "this attempt
        # finished its work"; `completed-review-blocking` stays admissible
        # because a FAIL verdict is a produced verdict.
        note = str(row.get("note") or "")
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
                              evidence_sha256=None):
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
        **axes,
    }
    existing_identity={
        "evidence_sha256":existing.get("evidence",{}).get("sha256"),
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
    if recovering and repair:
        atomic_write(canonical_path, existing)
    return existing

# SD-154 A-2: `evidence_digest` moved to `dispatch_contract.py` (imported
# above) so file/directory digest identity is the same definition on both
# sides of the module boundary -- this binding keeps every existing call
# site in this file unchanged.

def write_completion_marker(
    route, node, node_id, evidence, *,
    attempt_id=None, attempt_metadata=None, review_claim=None, jobs=None,
    owner_override=False, owner_chain=False, expected_evidence_sha256=None,
):
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
        owner_override=owner_override, owner_chain=owner_chain,
    )
    replayed=_completion_marker_replay(
        route,node,node_id,evidence,axes,directory,
        repair=expected_evidence_sha256 is None,
        evidence_sha256=sha if expected_evidence_sha256 is not None else None,
    )
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
            )
    if replayed is not None:
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
    if REVIEW_ROUND_CAP.is_round_capped_node(node):
        rows=()
        if jobs is not None:
            jobs_path=Path(jobs)
            if jobs_path.is_file():
                lines=jobs_path.read_text(encoding="utf-8",errors="replace").splitlines()
                generations = (review_lineage_routes(route, node_id)
                               if node.get("kind") == "review-worker" else [route])
                rows = [row for generation in reversed(generations)
                        for row in _review_round_rows(lines, generation["route_id"], node_id, jobs=jobs)]
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
            revisions=_dependency_revisions(route,node,jobs,reviewed_input=reviewed_input)
        round_census=REVIEW_ROUND_CAP.marker_round_census(
            route,node,rows,site=site,revisions=revisions,
            independently_reviewed=review_identity.get("review_independence")=="independent",
        )
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
        **({"round_census":round_census} if round_census else {}),
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
            yield handle
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


def _marker_identity_row(route, node, node_id, gate, *, jobs=None, exact_terminal=False):
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
        if gates_on():
            return {"passed": False, "reason": "completion-marker-identity-mismatch"}
        same_work_or_refuse("completion-marker-identity-mismatch", node_id)
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
    currency = evidence_currency(route, node, path, marker)
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
        if not completion_marker_is_current(route, node, path, marker):
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
        try:
            lines = registry.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []  # Existing semantic-history contract permits archived process rows.
        except OSError:
            return {"passed": False, "reason": "registry-unreadable"}
        if completion_conflict_attempt(marker, lines):
            return {"passed": False, "reason": "terminal-evidence-conflict"}
    if exact_terminal:
        if jobs is None or not completion_marker_is_current(route, node, path, marker):
            return {"passed": False, "reason": "completion-marker-not-current"}
        try:
            lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {"passed": False, "reason": "registry-unreadable", "current": False,
                    "attempt_id": marker.get("attempt_id")}
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
    if latest != attempt_id:
        return "completion-attempt-not-current"
    return None


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


def revision_basis_verdict(route, node, basis, answers, *, jobs=None, direction=None, reason=None):
    """SD-154 rule 2: prove a revision's closed basis, or raise
    `ValueError("revision-basis-unverified")` (or an invalid-basis error).

    `review-findings` requires every attempt in `answers` to be a real verdict
    row (13.59.2 classification) of a node downstream (`depends_on` transitive
    closure) of `node`, in the route's own registry -- SD-134 "직함이 아니라
    배정을 본다" read onto revision provenance. `user-direction` requires a
    non-empty `direction` (a gate release id or a readable memo path).
    `owner-correction` requires a non-empty `reason` string.
    """
    if basis == "user-direction":
        if not direction:
            raise ValueError("revision-basis-unverified")
        return {"basis": basis, "direction": direction}
    if basis == "owner-correction":
        if not reason:
            raise ValueError("revision-basis-unverified")
        return {"basis": basis, "reason": reason}
    if basis != "review-findings":
        raise ValueError(f"revision-basis-invalid:{basis}")
    answers = tuple(answers)
    if not answers or not jobs:
        raise ValueError("revision-basis-unverified")
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        raise ValueError("revision-basis-unverified")
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
        raise ValueError("revision-basis-unverified")
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
    budget = REVIEW_ROUND_CAP.round_budget(route, node, rows)
    if budget.state in ("blocked-live", "blocked-unsettled"):
        raise ValueError(f"review-input-revision-{budget.state}")
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
        artifact_root_metadata=selected.get("artifact_root") or route["artifact_root"], worker_type="review")
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
    currency = gate_currency(route, node, canonical_path, marker)
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
    revision_basis_verdict(route, node, basis, answers, jobs=jobs, direction=direction, reason=reason)
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
        downstream_sequence = _next_marker_sequence(directory, downstream_id)
        tombstone_marker = dict(downstream_marker)
        tombstone_marker["sequence"] = downstream_sequence
        tombstone_marker["state"] = "superseded-by-upstream-revision"
        tombstone_marker["superseded_by"] = {"node": node_id, "sequence": new_sequence}
        tombstones[downstream_id] = tombstone_marker
    return new_marker, tombstones


def preview_revision(route, node_id, evidence, *, basis, answers=(), direction=None,
                     reason=None, author_attempt_id, recorded_by="owner", jobs=None):
    """The complete writer proof, without lock creation or marker publication."""
    _, input_only = _revision_target(route, node_id, basis)
    if input_only:
        return preview_review_input_revision(
            route, node_id, evidence, answers=answers, author_attempt_id=author_attempt_id,
            recorded_by=recorded_by, jobs=jobs,
        )
    marker, tombstones = _producer_revision_plan(
        route, node_id, evidence, basis=basis, answers=answers, direction=direction,
        reason=reason, author_attempt_id=author_attempt_id, recorded_by=recorded_by, jobs=jobs,
    )
    return {"marker": marker, "tombstoned": list(tombstones)}


def publish_revision_locked(route, node_id, evidence, *, basis, answers=(), direction=None,
                            reason=None, author_attempt_id, recorded_by="owner", jobs=None):
    """Publish the shared producer proof and downstream tombstones under one lock."""
    _, input_only = _revision_target(route, node_id, basis)
    if input_only:
        return publish_review_input_revision(
            route, node_id, evidence, answers=answers, author_attempt_id=author_attempt_id,
            recorded_by=recorded_by, jobs=jobs,
        )
    directory = completion_dir(route["route_id"], jobs=jobs)
    with _exclusive_lock(directory / f".{node_id}.completion.lock"):
        marker, tombstones = _producer_revision_plan(
            route, node_id, evidence, basis=basis, answers=answers, direction=direction,
            reason=reason, author_attempt_id=author_attempt_id, recorded_by=recorded_by, jobs=jobs,
        )
        write_once(directory / f"{node_id}.{marker['sequence']}.json", marker)
        atomic_write(directory / f"{node_id}.json", marker)
        for downstream_id, tombstone in tombstones.items():
            write_once(directory / f"{downstream_id}.{tombstone['sequence']}.json", tombstone)
            atomic_write(directory / f"{downstream_id}.json", tombstone)
    return {"marker": marker, "tombstoned": list(tombstones)}


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
        _notify_reviewer_claim_ignored(
            route,node_id,marker,
            resolve_review_identity(
                node,axes,attempt_metadata,
                claim=review_claim,jobs=jobs,
                route_id=route["route_id"],node_id=node_id,
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

def review_round_records(lines, route_ids, node_id, *, jobs=None):
    """One round census for admission and closure, including every status."""
    rows=[]
    for line in lines:
        fields=line.split("\t")
        if len(fields)!=6:
            continue
        metadata=parse_registry_metadata(fields[5])
        if (metadata.get("route_id") or metadata.get("route")) not in route_ids:
            continue
        if metadata.get("route_node")!=node_id:
            continue
        if ROUTE_AUTHORITY.no_stage_authority(metadata):
            continue
        rows.append((fields,metadata))
    return REVIEW_ROUND_CAP.logical_round_records(rows,jobs=jobs)

def _review_round_rows(lines, route_id, node_id, *, jobs=None):
    return [(fields[1], meta) for fields, meta in review_round_records(lines, {route_id}, node_id,jobs=jobs)]

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

def _dependency_revisions(route, node, jobs=None, *, reviewed_input=None):
    """13.59.3 rule 7's closure-check eligibility set: every revision
    recorded on any node this one `depends_on`. `dispatch-node.py`'s
    `admit_round` calls this same function for its `round_budget`, so a
    marker published for a closure-check round is labeled
    `closure_class="closure-check"` the same way admission already saw it."""
    lineage = (review_lineage_routes(route, node["id"])
               if node.get("kind") == "review-worker" else [route])
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
    ] + _answered_fix_revisions(route, jobs)


def _answered_fix_revisions(route, jobs):
    """A person's approved fix for this route's FAIL-ended owner answers the FAIL rows its
    claim pinned (`dispatch_replacement.answered_fix_revisions`): the same closure-check
    basis a revision naming that FAIL gives, within the verdict ceiling cap + 1."""
    if jobs is None:
        return []
    import dispatch_replacement
    return dispatch_replacement.answered_fix_revisions(jobs, route.get("route_id"))


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
    # SD-153: `round_budget` is the one admission/closure decision every
    # surface reads. Owner-closure is admitted once a real round budget is
    # spent (`state="exhausted"`) OR two rounds in a row produced no verdict
    # at all (`state="verdictless-bound"`) -- either way automatic retries
    # are no longer the answer. `state="admit"` means the budget still has
    # room for a registered round, so owner-closure would be premature.
    try:
        budget=REVIEW_ROUND_CAP.round_budget(route,node,rounds)
    except ValueError:
        refuse("intensity-unknown",str(route.get("effective_intensity")))
    if budget.state=="blocked-live":
        # A live review worker may still write a second blocking artifact
        # nobody has read; the gate never closes over its head.
        live=[metadata.get("attempt_id") or "-" for status,metadata in rounds if status in _LIVE_ROW_STATUSES]
        refuse("round-still-open","attempt="+"|".join(live))
    if budget.state=="blocked-unsettled":
        refuse("round-unsettled")
    if budget.state=="admit":
        refuse("round-budget-not-exhausted",
               f"rounds={budget.verdict_rounds};max_round={budget.cap};"
               f"verdictless_streak={budget.verdictless_streak};bound={REVIEW_ROUND_CAP.VERDICTLESS_BOUND}")
    terminated=[(status,metadata) for status,metadata in rounds if status not in _LIVE_ROW_STATUSES]
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
        if existing.get("attempt_id")!=own and not superseded:
            refuse("node-already-complete",f"attempt={existing.get('attempt_id') or '-'}")
    terminal=inspect_terminal_attempt(
        row_metadata.get("log_file"),
        worktree=route["cwd"],
        artifact_root_metadata=row_metadata.get("artifact_root") or route.get("artifact_root"),
        worker_type="review",
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
        if row_status == "done" and meta.get("note") == REVIEW_BLOCKING_NOTE:
            reviewed = _owner_closure_eligibility(parent, node, node["id"], evidence, meta, lines,
                                                 rounds=rounds, check_canonical=False)
            encoded = reviewed["review_artifact_b64"]
            artifact = Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
            reviews.append({"attempt_id": identity, "route_id": parent["route_id"],
                            "route_hash": parent["route_hash"], "artifact": str(artifact),
                            "sha256": evidence_digest(artifact)})
    if check_dependencies:
        for dependency in node.get("depends_on", []):
            predecessor = next(n for n in route["nodes"] if n["id"] == dependency)
            path = completion_dir(route["route_id"], jobs=jobs) / f"{dependency}.json"
            if not completion_marker_is_current(route, predecessor, path):
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
                    _publish_completion_locked(
                        route,node,node_id,evidence,
                        attempt_id=attempt_id,
                        attempt_metadata=explicit_attempt_metadata,
                        review_claim=review_claim,
                        jobs=jobs_path,
                    )
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
            marker_eligible=(
                already_closed
                and (
                    (row_note=="completed-supervisor" and row_metadata.get("failure_class")=="pass")
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
            owner_chain=True,
        )
    _launch_open_cycle_checkpoint(route)
    return marker,{"status":"stage-gate-aggregated","sessions":len(manifest["sessions"])}

def stages_block(registry, recipe):
    """One recipe's parts for `stages` and the frame catalog (SD-165, 13.64.5).

    Recipe stages in recipe order, then the recipe's optional catalog parts,
    then the parts of other recipes this host can borrow. The catalog is the
    only source: a frame assembles a graph from this output alone.
    """
    capability=recipe["capability"]
    group_by_node={g.get("node"):g.get("id") for g in (recipe["standard_plus"].get("parallel_groups") or [])}
    gate_by_node={}
    for row in recipe.get("human_gate_bindings") or []:
        gate_by_node.setdefault(row.get("node"), []).append(row.get("gate"))
    aliases=(TOPO.part_catalog(registry).get("frame") or {}).get("aliases") or []
    def part_view(part_id,node,origin,*,optional=False,local=True):
        catalog_row=TOPO.part_row(registry,part_id)
        placement=catalog_row.get("optional") or {}
        produced={out for other in origin["standard_plus"]["nodes"] for out in other.get("outputs") or []}
        return {
            "id":node["id"] if local else part_id,"unit":node.get("unit"),
            "unit_choices":node.get("unit_choices") or catalog_row.get("unit_choices") or [],
            "parallel_group":group_by_node.get(node["id"]) if local else None,
            "human_gates":gate_by_node.get(node["id"], []) if local else [],
            "terminal":local and node.get("terminal") is True,
            "part":part_id,"summary":catalog_row.get("summary",""),"kind":node.get("kind"),
            "inputs":list(node.get("inputs") or []),
            "external_inputs":[name for name in node.get("inputs") or []
                               if name not in produced and not TOPO._is_semantic_output(name)],
            "optional_inputs":list(catalog_row.get("optional_inputs") or []),
            "outputs":list(node.get("outputs") or []),
            "shareable":catalog_row.get("shareable") is True,
            "start_approval":catalog_row.get("start_approval"),
            "optional":optional,
            "after":list(placement.get("after") or []),"before":list(placement.get("before") or []),
            "frame_alias":local and node["id"] in aliases and node.get("unit")=="plan/frame",
        }
    nodes=[part_view(f"{capability}:{node['id']}",node,recipe) for node in recipe["standard_plus"]["nodes"]]
    nodes+=[part_view(f"{capability}:{stage}",row["optional"]["node"],recipe,optional=True)
            for stage,row in TOPO.recipe_optional_parts(registry,recipe)]
    borrowable=[]
    for part_id in TOPO.borrowable_parts(registry,recipe):
        origin,node=TOPO.part_recipe(registry,part_id)
        borrowable.append(part_view(part_id,node,origin,optional="optional" in TOPO.part_row(registry,part_id),
                                    local=False))
    return {"capability":capability,"modes":list(recipe["modes"]),
            "topology_class":recipe["topology_class"],"nodes":nodes,"borrowable":borrowable,
            "frame_brief_inputs":TOPO.frame_brief_inputs(registry,capability)}


def _isolates_worktree(route):
    """Whether this route gets its own worktree: it changes source, and its artifact root is a real
    one (a temporary root is a fixture's, as for the route-chain ledger, which never touches the
    checkout around it)."""
    real_root = os.path.realpath(str(route.get("artifact_root") or ""))
    real_tmp = os.path.realpath(tempfile.gettempdir())
    return (real_root != real_tmp and not real_root.startswith(real_tmp + os.sep)
            and any(_node_mutates_worktree(node) for node in route.get("nodes") or []))


def _git(cwd, *args):
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def prepare_isolated_worktree(cwd, slug):
    """The isolated worktree a source-changing route runs in, when `cwd` is a primary checkout.

    `<repo>-wt/<slug>` (OPERATIONS §5.9 naming) on a new branch `<slug>` from the latest
    `origin/<default>` (the primary's current HEAD when it has local work), or
    the existing worktree at that path, reused as it is. Returns
    `{state: created|reused, path, cwd, branch, base}`, or `{state: skipped, reason}` when the
    caller's cwd stays the route cwd (not a primary checkout, or the path
    or branch is taken).
    Nothing here refuses: a skipped preparation leaves the work where it was asked to run."""
    if OWNER_WRITE_ADVISORY.git_topology(cwd) != "primary":
        return {"state": "skipped", "reason": "not-primary-checkout"}
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(slug or "")):
        return {"state": "skipped", "reason": "no-checkout-or-slug"}
    relative = os.path.relpath(os.path.realpath(cwd), os.path.realpath(top))
    path = Path(top).parent / f"{Path(top).name}-wt" / slug
    inside = lambda root: str(Path(root) / relative) if relative != "." and (Path(root) / relative).is_dir() else str(root)
    common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if path.exists():
        branch = _git(path, "symbolic-ref", "-q", "--short", "HEAD")
        if branch and common and _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir") == common:
            return {"state": "reused", "path": str(path), "cwd": inside(path), "branch": branch, "base": None}
        return {"state": "skipped", "reason": "path-occupied", "path": str(path)}
    default = (_git(top, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD") or "origin/main").split("/", 1)[-1]
    _git(top, "fetch", "-q", "origin", default)
    base = f"origin/{default}" if _git(top, "rev-parse", "--verify", "-q", f"origin/{default}") else "HEAD"
    # Local work must not pin a source-changing owner to the shared primary.
    # Keep its committed baseline, without copying or changing uncommitted files.
    # The prepared cwd is sealed before launch, so every stage shares it naturally.
    if (_git(top, "status", "--porcelain") != ""
            or _git(top, "merge-base", "--is-ancestor", "HEAD", base) is None):
        base = _git(top, "rev-parse", "HEAD") or "HEAD"
    if _git(top, "rev-parse", "--verify", "-q", f"refs/heads/{slug}"):
        made = _git(top, "worktree", "add", str(path), slug)
    else:
        made = _git(top, "worktree", "add", "-b", slug, str(path), base)
    if made is None:
        return {"state": "skipped", "reason": "worktree-add-failed", "path": str(path)}
    return {"state": "created", "path": str(path), "cwd": inside(path), "branch": slug, "base": base}


ROUTE_ID_FORM = re.compile(r"rt-[0-9a-f]{16}")


def resolve_route_argument(value, jobs=None):
    """`--route` as given: a route file, or a route ID (`rt-<16 hex>`, the form `resume_command`
    prints) looked up as the canonical record under the cwd's artifact root, then under
    `AGENT_ARTIFACT_ROOT`, then by the file the jobs registry or a route-chain ledger names for it
    (this session's, then the newest ledgers of every session). An ID that names no route file is
    refused (`route-id-unresolved:<id>`)."""
    text = str(value)
    if not ROUTE_ID_FORM.fullmatch(text) or Path(text).is_file():
        return Path(text)
    roots = [os.environ.get("AGENT_ARTIFACT_ROOT")]
    try:
        roots.insert(0, _compose_artifact_root(os.getcwd()))
    except ValueError:
        pass
    named = [canonical_route_path(root, text) for root in roots if root]
    try:
        for line in Path(jobs or _compose_default_jobs()).read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 6:
                meta = parse_registry_metadata(fields[5])
                for key in ("owner_route", "route"):
                    if meta.get(f"{key}_id") == text and meta.get(f"{key}_file"):
                        named.append(meta[f"{key}_file"])
    except OSError:
        pass
    rc = _route_chain_module()
    if rc is not None:
        # This session's ledger first, then the newest ledgers of every session (any event).
        own = rc.writer_identity()
        ledgers = [own] if own else []
        for harness in rc.HARNESSES:
            try:
                directory = Path(rc.state_root()) / harness
                ledgers += [(harness, path.stem) for path in sorted(
                    directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:rc.ANCHOR_SCAN_FILES]]
            except OSError:
                continue
        for anchor in ledgers:
            named += [line["route_file"] for line in rc.read_tail(*anchor) if line.get("route_id") == text]
            if any(Path(path).is_file() for path in named):
                break
    for path in named:
        if Path(path).is_file():
            return Path(path)
    raise ValueError(f"route-id-unresolved:{text} (pass the route file, or run from the route's checkout)")


def caller_open_route(cwd):
    """`(route_file | None, source, rows)`: the open route a bare `start` continues.

    This session's own newest open route first (its route-chain ledger, which every
    compose and start writes); otherwise the one open route sealed for this cwd under
    the cwd's artifact root, or, with none there, for the worktree compose made for it in the cwd's
    repository (`<repo>-wt/<route slug>`). `rows` are the open routes that were looked at, each with
    its `resume_command`, so a caller that finds none or several sees what is there."""
    from parent_next_directive import resume_command
    rc = _route_chain_module()
    anchor = rc.writer_identity() if rc is not None else None
    for line in reversed(rc.read_tail(*anchor) if anchor else []):
        path = Path(line["route_file"])
        if path.is_file() and not outcome_path(path).is_file():
            return str(path), "this-session", []
    try:
        artifact_root = _compose_artifact_root(cwd)
    except ValueError:
        return None, "none", []
    here = os.path.realpath(cwd)
    rows, same_repository = [], []
    for row in route_status(artifact_root):
        if row["closed"] or row.get("read_only"):
            continue
        try:
            sealed = json.loads(Path(row["route_file"]).read_text(encoding="utf-8"))
            sealed_cwd = sealed.get("cwd")
        except (OSError, ValueError):
            continue
        if not isinstance(sealed_cwd, str):
            continue
        found = {"route_id": row["route_id"], "capability": row["capability"], "route_file": row["route_file"],
                 "cwd": sealed_cwd, "slug": sealed.get("slug"),
                 "resume_command": resume_command(row["route_file"], agent_home=ROOT)}
        if os.path.realpath(sealed_cwd) == here:
            rows.append(found)
        else:
            same_repository.append(found)
    if not rows and same_repository:
        # A route compose moved into this repository's worktree for it (`prepare_isolated_worktree`:
        # `<repo>-wt/<route slug>`), not any route sealed elsewhere in the repository.
        top = _git(here, "rev-parse", "--show-toplevel")
        def moved(row):
            if not top or not row.get("slug"):
                return False
            home = os.path.realpath(Path(top).parent / f"{Path(top).name}-wt" / row["slug"])
            sealed = os.path.realpath(row["cwd"])
            return sealed == home or sealed.startswith(home + os.sep)
        rows = [row for row in same_repository if moved(row)]
    if len(rows) == 1:
        return rows[0]["route_file"], "cwd", rows
    return None, "ambiguous" if rows else "none", rows


def _compose_artifact_root(cwd):
    script=ROOT/"utilities"/"artifact-root.sh"
    result=subprocess.run(["sh",str(script),str(cwd)],text=True,capture_output=True,check=False)
    root=(result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""
    if result.returncode!=0 or not root:
        raise ValueError("compose-artifact-root-unresolved:"+(result.stderr or "").strip()[:200])
    return root


def compose_receipt(route, path, *, owner_harness=None):
    """The ordinary caller needs its choices and handle, not all sealed evidence."""
    return {
        "route_file": str(Path(path).resolve()), "route_id": route["route_id"],
        "selection": route.get("selection", {}),
        "cwd": route["cwd"], "artifact_root": route["artifact_root"],
        "effective_intensity": route["effective_intensity"],
        "owner_model_profile": route.get("owner_model_profile"),
        "nodes": [{key: node[key] for key in
                   ("id", "unit", "dispatch_depth", "model_profile", "depends_on", "terminal") if key in node}
                  for node in route["nodes"]],
        "human_gates": route.get("human_gates", []),
        "campaign": compose_campaign_selection(route),
        "advisories": OWNER_WRITE_ADVISORY.advisories(route, owner_harness=owner_harness),
    }


def _route_chain_module():
    """Lazy sys.path insert + import of `tools/fleet/route_chain` (`peer-message.py:461-465`
    idiom) — this file must not gain a hard import-time dependency on the Fleet package."""
    tools_dir = ROOT / "tools"
    try:
        if tools_dir.is_dir() and str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import route_chain as _rc  # noqa: WPS433
        return _rc
    except Exception:
        return None


_ROUTE_CHAIN_PARENT_SCAN_BYTES = 1024 * 1024


def _route_chain_parent_ledger(rc, parent_sid, source_route_id):
    """`(harness, session_id)` | None — the ledger-anchored parent lookup (plan §3 B-2,
    round 1 🔴-1 fix). The only signal this file trusts for "what harness is my depth-0
    parent" is whether that parent's OWN ledger (in exactly one harness directory) already
    carries the route being continued — never jobs.log `parent_harness` or
    `AGENT_DISPATCH_CALLER_HARNESS` (both name the OWNER's own harness at depth>=1, not the
    depth-0 parent's; R-3)."""
    if not parent_sid or not source_route_id:
        return None
    found = []
    for harness in rc.HARNESSES:
        try:
            path = rc.ledger_path(harness, parent_sid)
        except ValueError:
            continue
        if os.path.isfile(path):
            found.append(harness)
    if len(found) != 1:
        return None
    harness = found[0]
    # One lookup per continuation, not per Fleet tick, so read far past the display tail: a
    # long-lived parent can push the continued route's line beyond TAIL_BYTES (review 🟡-a).
    for line in rc.read_tail(harness, parent_sid, max_bytes=_ROUTE_CHAIN_PARENT_SCAN_BYTES):
        if line.get("route_id") == source_route_id:
            return harness, parent_sid
    return None


def _route_chain_identity(event, route):
    """`(harness, session_id, dispatch_depth, by_attempt)` | None — never raises (plan §3 B-1.2)."""
    try:
        depth = int(os.environ.get("AGENT_DISPATCH_DEPTH") or 0)
    except (TypeError, ValueError):
        return None
    if depth >= 2:
        return None
    if depth == 1:
        if event != "continuation":
            return None
        parent_sid = os.environ.get("AGENT_DISPATCH_PARENT_SESSION_ID")
        if not parent_sid:
            return None
        rc = _route_chain_module()
        if rc is None:
            return None
        anchor = _route_chain_parent_ledger(rc, parent_sid, (route or {}).get("source_route_id"))
        if anchor is None:
            return None
        harness, sid = anchor
        return harness, sid, depth, os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    rc = _route_chain_module()
    if rc is None:
        return None
    anchor = rc.writer_identity()
    if anchor is None:
        return None
    harness, sid = anchor
    return harness, sid, depth, None


def _current_owner_attempt(jobs, attempt_id):
    """The newest launched owner in this attempt's replacement lineage (itself when none).

    An answer sent with an older owner's id still reaches the owner doing the work now."""
    try:
        from dispatch_replacement import effective_attempts
        current = attempt_id
        for _ in range(32):
            effective, mapping = effective_attempts(jobs, {current})
            if not mapping:
                return current
            current = next(iter(effective))
        return current
    except (DispatchContractError, OSError, ValueError, StopIteration):
        return attempt_id


def _continue_after_answer(jobs, attempt_id, correction):
    """Continue a route whose owner ended BLOCKED, now that its answer is kept.

    The shared `start` does the work: its replacement owner receives the answer
    first (`dispatch_replacement` 'corrected'). Its receipt, `parent_next`
    included, is this command's receipt, so nothing else has to be run."""
    import dispatch_replacement
    from work_start import start_work, _rows as start_rows
    try:
        _, meta = start_rows(jobs)[attempt_id]
        route_path, _ = dispatch_replacement._route(jobs, attempt_id, meta)
        route = verify_route(json.loads(route_path.read_text()))
    except (DispatchContractError, OSError, ValueError, KeyError) as exc:
        return {**correction, "state": "retained", "reason": getattr(exc, "reason", None) or str(exc)[:200],
                "next_step": "The answer is kept, but this owner's route cannot continue (it is closed or "
                    "unreadable). Report that; compose the remaining work as a new route."}
    _record_route_chain(route, str(route_path), "start")
    access_change = _record_access_change(route, jobs)
    receipt = start_work(route, route_path, jobs)
    if access_change is not None:
        receipt = {**receipt, "access_change": access_change}
    if receipt.get("reason") == "replacement-parent-identity-unproven":
        # Only the route's parent launches the continuation: it is told, so nobody repeats the answer.
        try:
            from dispatch_supervision import ANSWER_AWAITING_PARENT, materialize
            materialize(jobs, {attempt_id}, reason=ANSWER_AWAITING_PARENT)
            receipt["parent_notified"] = True
            receipt["next_step"] = ("The answer is kept and this route's parent session was notified; its "
                "runtime starts the continuation when the notice reaches it, and the new owner receives the "
                "answer first. Nothing else to run here.")
        except Exception:  # noqa: BLE001 -- the kept answer and the plain instruction remain
            receipt["parent_notified"] = False
            receipt["next_step"] = ("The answer is kept. Only the session that started this route may launch its "
                "replacement owner: that session continues it with resume_command, and the answer goes first.")
    return {**receipt, "correction": correction}


def _record_route_chain(route, route_file, event, *, plan=None, plan_source=None):
    """Append one route-chain ledger line. Failures report their cause — route creation/start
    must never fail because of this sidecar (plan §3 B-1.3). Success/failure is only
    ever observable on stderr."""
    try:
        rc = _route_chain_module()
        if rc is None:
            return
        identity = _route_chain_identity(event, route)
        if identity is None:
            print("route_chain_written=0 reason=no-identity", file=sys.stderr)
            return
        harness, sid, depth, by_attempt = identity
        if not os.environ.get("FLEET_ROUTE_CHAIN_DIR"):
            artifact_root = route.get("artifact_root")
            if isinstance(artifact_root, str) and artifact_root:
                real_root = os.path.realpath(artifact_root)
                real_tmp = os.path.realpath(tempfile.gettempdir())
                if real_root == real_tmp or real_root.startswith(real_tmp + os.sep):
                    print("route_chain_written=0 reason=tmp-artifact-root", file=sys.stderr)
                    return
        if event == "start" and any(
            line.get("route_id") == route.get("route_id")
            for line in rc.read_tail(harness, sid)
        ):
            print("route_chain_written=0 reason=start-already-recorded", file=sys.stderr)
            return
        line = rc.build_line(
            route, event=event, harness=harness, session_id=sid, route_file=route_file,
            plan=plan, plan_source=plan_source, dispatch_depth=depth, by_attempt=by_attempt,
        )
        append_errors = []
        if rc.append(harness, sid, line, on_error=append_errors.append):
            print(f"route_chain_written=1 harness={harness}", file=sys.stderr)
        else:
            detail = append_errors[0] if append_errors else "unknown"
            print(f"route_chain_written=0 reason=append-failed detail={detail}", file=sys.stderr)
    except Exception as exc:
        try:
            print(f"route_chain_written=0 reason={exc}", file=sys.stderr)
        except Exception:
            pass


def _resolve_compose_plan(a, route_plan=None):
    """`(plan, plan_source)` for the compose CLI's `--plan` input (plan §3 B-1.5). An explicit
    `--plan` is validated eagerly — before any route work — and its failure is a normal
    `ValueError("compose-plan-invalid:...")`, never swallowed. With no explicit plan, an
    inherited value is read from this session's own ledger, but ONLY when the new route's
    own chain key still matches the ledger's current segment (D-9: a fresh campaign starts
    a fresh chain, so it must not silently inherit a different campaign's declared plan).
    Never raises for the inherited path — a ledger read failure just means no inheritance."""
    rc = _route_chain_module()
    if a.plan:
        if rc is None:
            raise ValueError("compose-plan-invalid:route-chain-unavailable")
        known = {r["capability"] for r in TOPO.load_registry()["recipes"]
                 if r["capability"] != ROUTE_FRAME_CAPABILITY}
        return rc.parse_plan(a.plan, known), "explicit"
    if route_plan is not None:
        # Display only: the approved legs' capability order, never a permission or a hash input.
        import route_plan as RP
        return RP.display_plan(route_plan["legs"]), "explicit"
    if rc is None:
        return None, None
    try:
        identity = _route_chain_identity("compose", None)
        if identity is None:
            return None, None
        harness, sid, _depth, _by = identity
        lines = rc.read_tail(harness, sid)
        segment = rc.current_segment(lines)
        if not segment:
            return None, None
        new_key = rc.chain_key({"campaign_key": a.campaign_key, "parent_cycle_id": a.parent_cycle})
        if rc.chain_key(segment[-1]) != new_key:
            return None, None
        plan, source = rc.inherited_plan(segment)
        return (plan, source) if plan else (None, None)
    except Exception:
        return None, None


def _session_latest_route(artifact_root, event="compose"):
    """This session's latest route-chain line in the same artifact root, or None."""
    rc = _route_chain_module()
    if rc is None:
        return None
    try:
        identity = _route_chain_identity(event, None)
        if identity is None:
            return None
        harness, sid, _depth, _by = identity
        root = os.path.realpath(artifact_root)
        for line in reversed(rc.read_tail(harness, sid)):
            if os.path.realpath(str(line.get("artifact_root") or "")) == root:
                return line
    except Exception:
        return None
    return None


def _session_campaign_key(artifact_root):
    """The campaign key of this session's latest route in the same artifact root, or None.

    Only that latest route counts: when it named no campaign (a parent cycle or an
    explicit `--unassigned`), nothing older is reached for."""
    line = _session_latest_route(artifact_root) or {}
    key = line.get("campaign_key")
    return key if isinstance(key, str) and key else None


def _emit_compiled_route(a,route,artifact_root,output=None):
    """Shared tail of compile/compose: runtime-root check, canonical write-once, owner binding, prints."""
    output=output if output is not None else getattr(a,"output",None)
    vbasis=route.get("validation_basis") or {}
    if vbasis.get("runtime_root_match") is False and not gates_on():
        same_work_or_refuse("launch-runtime-root-mismatch")
    if vbasis.get("runtime_root_match") is False and gates_on():
        launch_tuple=route.get("launch_compatibility_tuple") or {}
        expected=launch_tuple.get("registry_root")
        observed=launch_tuple.get("runtime_root")
        print("route_file_written=0 registered=0 started=0 child_spawned=0",file=sys.stderr)
        print(runtime_root_hint(),file=sys.stderr)
        raise ValueError(
            "launch-runtime-root-mismatch "
            f"expected={canonical(expected).decode()} observed={canonical(observed).decode()}"
        )
    expected_output=canonical_route_path(artifact_root,route["route_id"])
    if output:
        output_path=Path(output)
        if classify_route_location(output_path,artifact_root) != "canonical":
            raise ValueError("route-output-outside-canonical")
        if not route_path_is_exact(output_path,artifact_root,route["route_id"]):
            raise ValueError("route-output-alias-basename")
    else:
        output_path=expected_output
    write_once(output_path,route)
    retired=retire_stale_closure(output_path)
    if retired is not None:
        print(f"route_closure_retired={retired}",file=sys.stderr)
    # A registered depth-1 owner can compile its first route only after it
    # has started.  Attach those immutable bytes to the exact active owner
    # attempt; non-owner and ordinary interactive compiles remain no-ops.
    try:
        from owner_route_binding import (
            OwnerRouteBindingError,
            publish_owner_route_attachment_from_environment,
        )
        route_for_binding = dict(route)
        route_for_binding["route_file"] = str(output_path.resolve())
        attachment = publish_owner_route_attachment_from_environment(
            os.environ.get("AGENT_DISPATCH_JOBS", ""),
            target_route=route_for_binding,
            environ=os.environ,
        ) if os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") else None
    except OwnerRouteBindingError as exc:
        print(
            f"route_file_written=1 owner_route_binding_written=0 reason={exc}",
            file=sys.stderr,
        )
        raise ValueError(str(exc)) from exc
    if attachment is not None:
        print("owner_route_binding_written=1", file=sys.stderr)
    plan, plan_source = getattr(a, "_route_chain_plan", (None, None))
    _record_route_chain(route, str(output_path.resolve()), a.command,
                        plan=plan, plan_source=plan_source)
    print(f"route_file={output_path.resolve()}",file=sys.stderr)
    result = (compose_receipt(route, output_path, owner_harness=getattr(a, "owner", None))
              if a.command == "compose" and not getattr(a, "full_record", False) else route)
    if not getattr(a, "start", False):
        print(json.dumps(result,sort_keys=True))
    return output_path.resolve()

def _close_route_argument(a):
    """`close`/`finish --route` take a route file or an `rt-...` id; an id is looked up under the
    artifact root. Without one, `finish` takes this session's latest route there."""
    import artifact_producer
    route=a.route
    if route and (not artifact_producer._ROUTE_ID_RE.fullmatch(str(route)) or Path(route).exists()):
        return route
    root=getattr(a,"artifact_root",None) or os.environ.get("AGENT_ARTIFACT_ROOT") or _compose_artifact_root(os.getcwd())
    if not route:
        latest=(_session_latest_route(root,a.command) or {}).get("route_file")
        if not latest:
            raise ValueError(f"route-required: this session has no route under {root}; pass --route")
        return latest
    path=artifact_producer.resolve_route_argument(Path(root),route)
    if not path.is_file():
        raise ValueError(f"route-not-found: {route} under {root}/.runtime/routes")
    return str(path)


def _legacy_inline_finish(a, route, route_file, api, entry_error=None):
    """Close a legacy inline-shaped route with the one `finish` command.

    The gate is the shape, not the intensity: one inline depth-0
    unregistered terminal node has no stages to skip, so any intensity with
    that shape is safe here. Anything else re-raises the entry refusal.
    The caller's identity, registry, cycle-local evidence and
    already-closed checks run here exactly as `inline_finish.finish` runs
    them -- this path adds no bypass. Old explicit steps keep working.
    """
    import artifact_producer
    import inline_finish
    nodes = [n for n in (route.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    if (len(nodes) != 1 or nodes[0].get("execution_surface") != "inline"
            or nodes[0].get("dispatch_depth") != 0
            or nodes[0].get("registered_worker") is not False
            or nodes[0].get("terminal") is not True):
        if entry_error is not None:
            raise entry_error
        raise inline_finish.InlineFinishError("finish-inline-owner-sentinel-required")
    node, node_id = nodes[0], nodes[0].get("id")
    from dispatch_parent_completion import interactive_parent_identity
    from dispatch_contract import DispatchContractError, resolve_global_registry
    try:
        _harness, sid = interactive_parent_identity(os.environ)
    except DispatchContractError as exc:
        if str(exc) == "caller-harness-ambiguous":
            raise inline_finish.InlineFinishError("finish-caller-harness-ambiguous") from exc
        raise inline_finish.InlineFinishError("finish-caller-harness-invalid") from exc
    if not sid:
        raise inline_finish.InlineFinishError("finish-current-session-missing")
    if not sid or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or os.environ.get("AGENT_DISPATCH_DEPTH", "0") != "0":
        raise inline_finish.InlineFinishError("finish-registered-caller-ineligible")
    jobs = resolve_global_registry(Path(__file__).resolve().parents[1], None, 0, "read").path
    try:
        for line in inline_finish._registry_lines(jobs, bool(os.environ.get("AGENT_DISPATCH_JOBS"))):
            fields = line.split("\t")
            from dispatch_contract import parse_registry_metadata
            metadata = parse_registry_metadata(fields[5]) if len(fields) > 5 else {}
            if route["route_id"] in {metadata.get("route_id"), metadata.get("owner_route_id")}:
                raise inline_finish.InlineFinishError("finish-registered-route-ineligible")
    except inline_finish.InlineFinishError:
        raise
    except Exception as exc:
        raise inline_finish.InlineFinishError("finish-registry-unreadable") from exc
    if api.outcome_path(Path(route_file)).exists():
        raise inline_finish.InlineFinishError("finish-route-already-closed")
    evidence = Path(a.evidence).resolve()
    if not (evidence.is_file() or evidence.is_dir()):
        raise SystemExit("completion evidence missing")
    try:
        artifact_producer.require_cycle_output(Path(route["artifact_root"]), evidence,
                                               route_id=route["route_id"])
    except artifact_producer.ProducerError as exc:
        raise ValueError(f"{exc.code}: {exc.detail}") from exc
    try:
        summary_text = Path(a.summary_file).read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        raise SystemExit("completion summary missing")
    summary = (summary_text[0].strip() if summary_text and summary_text[0].strip() else f"legacy inline {node_id}")[:200]
    if not summary:
        raise SystemExit("completion summary missing")
    attempt_id = None
    explicit = None
    marker, _row = complete_node(route, node, node_id, evidence, jobs=None,
                                 attempt_id=attempt_id, explicit_attempt_metadata=explicit)
    outcome, _created = close_route(route, str(route_file), a.commit if getattr(a, "commit", None) else None,
                                    summary, allow_unproven=True)
    root = Path(route.get("artifact_root") or _compose_artifact_root(os.getcwd()))
    cycle = None
    try:
        cycle = artifact_producer.route_cycle_for(root, route)
    except Exception:
        cycle = None
    cycle_id = (cycle or {}).get("cycle_id") if isinstance(cycle, dict) else None
    finalized = None
    if cycle_id:
        finalized = artifact_producer.finalize(root, cycle_id=cycle_id, state="completed",
                                               allow_open_route=True)
    print(f"legacy-inline-finish node={node_id} outcome={bool(outcome)} finalized={bool(finalized)}", file=sys.stderr)
    return {"schema": "finish_receipt_v1", "route_id": route.get("route_id"),
            "route_hash": route.get("route_hash"), "state": "legacy-finished", "legacy": True,
            "node": node_id, "marker": marker, "outcome": outcome, "finalized": finalized}

def _route_autoclose(artifact_root, trigger, route=None):
    """The runtime closes routes nobody works on any more (utilities/route_autoclose.py).
    Bookkeeping only: it never fails or blocks the command that triggered it.
    With `route`, only that route's own campaign is swept; a campaign not yet begun has nothing to close."""
    try:
        import route_autoclose
        api=sys.modules.get(__name__)
        if api is None: return
        scope={}
        if route is not None:
            found=route_autoclose.campaign_of_route(Path(artifact_root),route)
            if found is None: return
            scope={"scope_campaign_id":found[0],"scope_dir":found[1],
                   "scope_key":route_autoclose.campaign_key_of(route)}
        route_autoclose.report(route_autoclose.sweep(artifact_root,api=api,trigger=trigger,**scope))
    except Exception as exc:  # noqa: BLE001
        print(f"route_autoclose error={type(exc).__name__}",file=sys.stderr)

def main():
    from dispatch_parent_completion import default_parent_harness
    p=argparse.ArgumentParser(allow_abbrev=False)
    sub=p.add_subparsers(dest="command",required=True,
                         parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    c=sub.add_parser("compile"); c.add_argument("--capability",required=True); c.add_argument("--capability-mode",default="default")
    c.add_argument("--slug",required=True)
    c.add_argument("--campaign-key",help="explicit work stream passed to the producer owner")
    c.add_argument("--parent-cycle",help="open or sealed predecessor cycle; causal link, not input approval")
    c.add_argument("--profile-demands", help="JSON file mapping node ids and __owner__ to full SD-88 demands")
    c.add_argument("--explicit-profiles", help="JSON file mapping demanded node ids to explicit profiles")
    c.add_argument("--intensity",default="auto"); c.add_argument("--cwd",required=True); c.add_argument("--artifact-root",required=True)
    c.add_argument("--predicate",action="append",default=[]); c.add_argument("--signal",action="append",default=[])
    c.add_argument("--transport",default=None); c.add_argument("--transport-evidence",default="caller-selected")
    c.add_argument("--inline-reason"); c.add_argument("--tracking",choices=sorted(TRACKING),default="tracked")
    c.add_argument("--dispatch-evidence",help="JSON file with checked nested tuples/native evidence")
    c.add_argument("--registered-headless-evidence",help="JSON file with checked quick or single-owner candidates")
    c.add_argument("--composed-recipe",help="JSON file with a compose-on-demand recipe (sealed composed: true)")
    # The tracked-gate fields take compose's defaults; an explicit value is recorded as given.
    c.add_argument("--spec-read",default=None,help="default: compose's spec check")
    c.add_argument("--drift-verdict",default=DEFAULT_DRIFT_VERDICT)
    c.add_argument("--workflow-mode",choices=sorted(TRACKING),default=None,help="default: --tracking")
    c.add_argument("--artifact-guard",default=DEFAULT_ARTIFACT_GUARD)
    c.add_argument("--output")
    cp=sub.add_parser("compose",help="preset-free work route: name the shape (and stage subgraph), defaults fill the rest")
    cp.add_argument("--slug",default=None,help="default: named from the task's first line with ASCII words")
    cp.add_argument("--start",action="store_true",help="prepare and start the selected work; the runtime owns frame launches and waiting")
    cp.add_argument("--prompt-file",type=Path,help="the user's task, stored with the route for frame and owner execution")
    cp.add_argument("--owner",choices=("claude","codex","opencode"),help="explicit owner runtime; otherwise use normal selection (same as --pin owner=<harness>)")
    cp.add_argument("--pin",action="append",default=[],metavar="TARGET=HARNESS[:MODEL[@EFFORT]]",
                    help="choose the tool (and optionally model and effort) once; the route seals it for the owner, "
                         "frame legs or depth-2 workers and every resume/replacement reuses it. TARGET is owner|frame|worker, "
                         "e.g. --pin owner=opencode:<provider/model>@<effort> --pin worker=opencode. "
                         "Top models are accepted for frame only (owner/worker keep the tool and drop the model with a warning)")
    cp.add_argument("--campaign-key",help="the work stream this route joins or creates (default: this session's latest campaign in the same artifact root; a folder name or near spelling of one active campaign joins it); `artifact_producer.py campaign-list` shows active keys. Size it as a stream with a one-sentence closing condition — not a project name, not a one-cycle task (join the stream that task serves)")
    cp.add_argument("--unassigned",action="store_true",help="explicit opt-out: keep this work in the root's degraded _unassigned container, proposing no stream")
    cp.add_argument("--parent-cycle",help="open or sealed predecessor cycle; causal link, not input approval")
    cp.add_argument("--plan",default=None,help="optional declared capability sequence for this session's route chain, e.g. research,draft,apply; shown in Fleet, not sealed into the route")
    cp.add_argument("--route-plan",default=None,metavar="RECORD#INDEX",help="runtime-generated: leg INDEX of an approved route decision record; compiles without frame nodes and seals its reference")
    cp.add_argument("--profile-demands", help="JSON file mapping node ids and __owner__ to full SD-88 demands")
    cp.add_argument("--explicit-profiles", help="JSON file mapping demanded node ids to explicit profiles")
    cp.add_argument("--profile", choices=sorted(PROFILE.PORTABLE_PROFILES),
                    help="explicit model budget for owner and model nodes; node-specific --explicit-profiles takes precedence")
    cp.add_argument("--shape",choices=COMPOSE_SHAPES,default=None,help="direct (inline) | solo (one registered owner) | staged (capability recipe, optionally narrowed by --graph) | framed (two top frame legs propose the route; --capability, --capability-mode, --graph and --profile are recorded as hints only); default staged with --graph, else direct")
    cp.add_argument("--graph",default=None,help="optional staged subgraph in your order (see `capability-route.py stages --capability <cap>` for valid ids); incompatible inherited parallel presets are omitted; optional :unit override, e.g. execute,test,report")
    cp.add_argument("--execution-scope", choices=("complete", "report"), default=None,
                    help="optional scope already selected in the existing start confirmation; it is not approval evidence")
    cp.add_argument("--capability",default=None,help=f"default {COMPOSE_DEFAULT_CAPABILITY}; a hint only with --shape framed"); cp.add_argument("--capability-mode",default=None)
    cp.add_argument("--intensity",default=None,help="default by shape: direct/quick/standard; staged accepts strong+")
    cp.add_argument("--cwd",default=None,help="default: current directory"); cp.add_argument("--artifact-root",default=None,help="default: utilities/artifact-root.sh for cwd")
    cp.add_argument("--signal",action="append",default=[],
                    help="promotion signal (repeatable), e.g. shared-contract: promotes shared spec/contract "
                         "work to a registered owner by forcing at least standard effective intensity, "
                         "even when --intensity would otherwise infer direct/quick")
    cp.add_argument("--spec-read",default="auto",help="auto: refuse when a spec/prd.md exists unless you name it here")
    cp.add_argument("--drift-verdict",default=None); cp.add_argument("--tracking",choices=sorted(TRACKING),default=None)
    cp.add_argument("--artifact-guard",default=None)
    cp.add_argument("--children",default=None,help="comma list of child harnesses to probe for staged/solo (default: every harness enabled in the dispatch-defaults policy, plus pinned ones)")
    cp.add_argument("--parent-harness",default=None,choices=("claude","codex","opencode"),help="default: actual parent runtime")
    cp.add_argument("--jobs",default=None,help="registry for the readiness probe (default AGENT_DISPATCH_JOBS or the stable state root)")
    cp.add_argument("--dispatch-evidence",help="checked evidence JSON (skips the live probe)")
    cp.add_argument("--registered-headless-evidence",help="checked quick or single-owner candidates JSON (skips the live probe)")
    cp.add_argument("--transport-evidence",default="compose-default")
    cp.add_argument("--explain",action="store_true",help="print the [경로] card and the sealed graph without writing the route")
    cp.add_argument("--output")
    cp.add_argument("--full-record",action="store_true",help="print all sealed evidence; default prints choices and the canonical route_file")
    cp.add_argument("--help-all",action="help",help="also show advanced/compatibility inputs")
    if "--help-all" not in sys.argv:
        advanced = {"route_plan", "parent_cycle", "profile_demands", "explicit_profiles", "intensity", "artifact_root",
                    "drift_verdict", "tracking", "artifact_guard", "parent_harness", "jobs",
                    "dispatch_evidence", "registered_headless_evidence", "transport_evidence", "output", "full_record"}
        for option in cp._actions:
            if option.dest in advanced:
                option.help = argparse.SUPPRESS
    start=sub.add_parser("start",help="continue one sealed work request; existing attempts are reused")
    start.add_argument("--route",type=Path,default=None,
                       help="default: this session's newest open route, else the one open route of this cwd")
    start.add_argument("--jobs",type=Path,default=None)
    start.add_argument("--wait",action="store_true",help="the receipt's bounded wait for a parent without an automatic carrier")
    start.add_argument("--interview",type=Path,help="semantic frame question; runtime owns its registration and cycle fields")
    start.add_argument("--answers",type=Path,help="actual native answers; runtime records intent and releases the gate")
    start.add_argument("--decision",choices=("proceed","revise","stop"),default="proceed")
    start.add_argument("--pin",action="append",default=[],metavar="TARGET=HARNESS[:MODEL[@EFFORT]]",
                       help="the route's parent moves an owner, frame or worker pin; the sealed route stays, the "
                            "change is recorded beside it and applies from that target's next launch")
    finish=sub.add_parser("finish",help="finish one current-session direct route and seal its exact producer cycle")
    finish.add_argument("--route",help="route file or route id (rt-...); defaults to this session's latest route here")
    finish.add_argument("--evidence",required=True,type=Path)
    finish.add_argument("--summary-file",required=True,type=Path)
    finish.add_argument("--commit",help="full result commit; defaults to current HEAD on first claim")
    correction=sub.add_parser("correct", help="deliver a correction to one existing owner; without a message file, inspect its receipts")
    correction.add_argument("--attempt-id", required=True)
    correction.add_argument("--jobs", type=Path)
    correction.add_argument("--message-file", type=Path)
    correction.add_argument("--request-id", help="stable idempotency key; defaults to the message digest")
    co=sub.add_parser("continuation")
    co.add_argument("--source-route",required=True)
    co.add_argument("--resume-from-node",required=True)
    co.add_argument("--requested-boundary",required=True)
    co.add_argument("--reason",required=True)
    co.add_argument("--artifact-root",help="defaults to the source route's own artifact root")
    co.add_argument("--output")
    co.add_argument("--dispatch-evidence",help="optional checked nested evidence for this continuation; source evidence stays unchanged")
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
    d=sub.add_parser("complete"); d.add_argument("--route",required=False,default=None); d.add_argument("--node",required=True); d.add_argument("--evidence",required=False,default=None); d.add_argument("--output")
    d.add_argument("--inline",action="store_true",help="owner runs a declared stage itself: derive route/attempt axes, keep the long form working")
    d.add_argument("--reason",help="one line naming what was done inline (stored with a synthesized evidence file when --evidence is omitted)")
    d.add_argument("--jobs",help="canonical registry path for a registered attempt "
                                 "(default AGENT_DISPATCH_JOBS once an attempt is named)")
    d.add_argument("--attempt-id",help="exact current attempt, or an official continuation's blocking source review "
                                       "(default AGENT_DISPATCH_ATTEMPT_ID when that row is this node's attempt)")
    d.add_argument("--check",action="store_true",help="read-only check of exact current or ancestor owner-closure authority; publishes nothing")
    d.add_argument("--dispatch-depth",type=int)
    d.add_argument("--transport")
    d.add_argument("--execution-surface")
    d.add_argument("--registered-worker",choices=("0","1","false","true"))
    d.add_argument("--fallback-hop")
    d.add_argument("--subsession-manifest",help="aggregate declared sub-sessions into this one stage gate")
    d.add_argument("--reviewer-attempt",
                   help="review-worker node only: the registered review attempt that produced the verdict; "
                        "verified against --jobs and downgraded to owner-inline when the row is absent "
                        "or is not worker_type=review")
    d.add_argument("--reviewer-subagent",
                   help="review-worker node only: transcript path of the native subagent that produced "
                        "the verdict; recorded with its sha256 and counted as independent review")
    ar=sub.add_parser("arbitrate"); ar.add_argument("--route",required=True)
    ar.add_argument("--group",required=True,help="realized auxiliary-bearing parallel group id")
    ar.add_argument("--evidence",required=True,help="owner merge record carrying auxiliary_findings_considered")
    ar.add_argument("--output")
    rv=sub.add_parser("revise",help="SD-154: record that a gate's evidence changed after its marker published")
    rv.add_argument("--route",required=True)
    rv.add_argument("--node",required=True)
    rv.add_argument("--evidence",required=True,help="the node's corrected gate evidence (file or directory)")
    rv.add_argument("--basis",required=True,choices=("review-findings","user-direction","owner-correction"))
    rv.add_argument("--answers",help="comma list of downstream review/test verdict attempt ids (basis=review-findings)")
    rv.add_argument("--direction",help="gate release id or readable memo path (basis=user-direction)")
    rv.add_argument("--reason",help="owner's stated reason (basis=owner-correction)")
    rv.add_argument("--jobs",help="canonical registry path; required to verify basis=review-findings")
    rv.add_argument("--author-attempt-id",help="the recording attempt; default AGENT_DISPATCH_ATTEMPT_ID")
    rv.add_argument("--recorded-by",default="owner")
    rv.add_argument("--output")
    cl=sub.add_parser("close"); cl.add_argument("--route",required=True,help="route file path or route id (rt-...)")
    cl.add_argument("--artifact-root",default=None,
                    help="for a route id: default AGENT_ARTIFACT_ROOT, else utilities/artifact-root.sh for the current directory")
    cl.add_argument("--commit",help="result commit; defaults to HEAD in the route cwd")
    cl.add_argument("--summary",help="one line naming what the route produced")
    cl.add_argument("--stop-resources", action="store_true",
                    help="also stop resource runs exactly linked to this route; default preserves them")
    cl.add_argument("--allow-unproven",action="store_true",
                     help="accepted for compatibility; close always records terminal_gate_proven=false with a "
                          "terminal-gate-unproven warning when the terminal node has not completed")
    st=sub.add_parser("status"); st.add_argument("--artifact-root",default=None,help="default: utilities/artifact-root.sh for cwd")
    st.add_argument("--open-only",action="store_true",help="list only routes with no recorded outcome")
    sg=sub.add_parser("stages",help="list a capability's (or every capability's) stage ids, in recipe order, for --graph")
    sg.add_argument("--capability",default=None,help="default: every capability")
    sg.add_argument("--json",action="store_true")
    a=p.parse_args()
    if a.command not in {"verify", "node", "status", "close", "finish", "stages"} and not (a.command == "complete" and a.check):
        dispatch_terminal_commit.require_current_cleanup("route-" + a.command)
    if a.command=="stages":
        registry=TOPO.load_registry()
        rows=[r for r in registry["recipes"] if r["capability"]!=ROUTE_FRAME_CAPABILITY
              and (a.capability is None or r["capability"]==a.capability)]
        if not rows:
            raise ValueError(f"unknown capability: {a.capability}")
        blocks=[stages_block(registry,recipe) for recipe in rows]
        if a.json:
            print(json.dumps(blocks,sort_keys=True))
        else:
            def line(node):
                choices=f" unit_choices={','.join(node['unit_choices'])}" if node["unit_choices"] else ""
                group=f" parallel_group={node['parallel_group']}" if node["parallel_group"] else ""
                gates=f" human_gate={','.join(node['human_gates'])}" if node["human_gates"] else ""
                terminal=" terminal=1" if node["terminal"] else ""
                approval=f" start_approval={node['start_approval']}" if node["start_approval"] else ""
                extra=f" optional_in={','.join(node['optional_inputs'])}" if node["optional_inputs"] else ""
                if node["after"]: extra+=f" after={','.join(node['after'])}"
                if node["before"]: extra+=f" before={','.join(node['before'])}"
                return (f"{node['id']} unit={node['unit']}{choices}{group}{gates}{terminal}"
                        f" part={node['part']} shareable={int(node['shareable'])} optional={int(node['optional'])}"
                        f"{approval} in={','.join(node['inputs'])}{extra} out={','.join(node['outputs'])}"
                        f" -- {node['summary']}")
            for block in blocks:
                print(f"capability={block['capability']} modes={','.join(block['modes'])} "
                      f"topology={block['topology_class']}")
                for node in block["nodes"]:
                    print("  "+line(node))
                for node in block["borrowable"]:
                    print("  borrow "+line(node))
        return 0
    if a.command=="compose":
        shape=a.shape or ("staged" if a.graph else "direct")
        if a.start and (a.explain or a.prompt_file is None):
            raise ValueError("compose-start-requires-task: use --start --prompt-file <task>, without --explain")
        cwd=a.cwd or os.getcwd()
        artifact_root=a.artifact_root or _compose_artifact_root(cwd)
        if a.campaign_key is None and a.parent_cycle is None and not a.unassigned:
            a.campaign_key=_session_campaign_key(artifact_root)
            if a.campaign_key:
                print(f"campaign_key_default={a.campaign_key} source=this-session-latest-route",file=sys.stderr)
            else:
                # A session's first compose joins this folder's one active stream; with several, the
                # refusal names them first.
                here=cwd_campaign_keys(artifact_root,cwd)
                if len(here)==1:
                    a.campaign_key=here[0]
                    print(f"campaign_key_default={a.campaign_key} source=this-folder-only-active-stream",file=sys.stderr)
        elif a.campaign_key is not None:
            a.campaign_key,given=compose_resolve_campaign_key(artifact_root,a.campaign_key)
            if given is not None:
                print(f"campaign_key_resolved={a.campaign_key} given={given}",file=sys.stderr)
        if a.slug is None:
            a.slug=compose_default_slug(a.prompt_file.read_text() if a.prompt_file else "",
                                        capability=a.capability or (None if shape=="framed" else COMPOSE_DEFAULT_CAPABILITY),
                                        shape=shape)
        route_plan_binding, route_plan_unreadable = None, False
        if a.route_plan:
            # Unreadable, mismatched or out of range: the same compose as without the argument, plus one card line.
            import route_plan as RP
            try:
                route_plan_binding = RP.read_route_plan(a.route_plan, artifact_root)
            except (OSError, ValueError):
                route_plan_unreadable = True
        a._route_chain_plan = _resolve_compose_plan(a, route_plan_binding)
        DISPATCH_DEFAULTS_WARNINGS.clear()
        pins=_parse_selection_pins(a.pin,a.owner)
        if route_plan_binding is not None:
            # A continuation leg keeps the pins its frame was composed with; a pin given here replaces only its own target.
            pins={**_inherited_selection_pins(route_plan_binding,artifact_root),**pins}
            if a.cwd is None:
                context=route_plan_binding["record"]["decision"]["first_leg_compose"]["context"]
                cwd=RP.leg_cwd(route_plan_binding["leg"],cwd,base_cwd=context["cwd"])
        pins,pin_warnings=_filter_top_pins(pins)
        owner_pin=(pins.get("owner") or {}).get("harness")
        compose_args=dict(
            capability=a.capability,
            capability_mode=a.capability_mode,shape=shape,graph=a.graph,
            slug=a.slug,cwd=cwd,artifact_root=artifact_root,intensity=a.intensity,signals=a.signal,
            campaign_key=a.campaign_key,parent_cycle_id=a.parent_cycle,unassigned=a.unassigned,
            spec_read=a.spec_read,drift_verdict=a.drift_verdict,tracking=a.tracking,
            artifact_guard=a.artifact_guard,
            children=[c.strip() for c in a.children.split(",") if c.strip()] if a.children else None,
            parent_harness=owner_pin or a.parent_harness or ("claude" if shape=="direct" else default_parent_harness("claude")),
            dispatch_evidence=json.loads(Path(a.dispatch_evidence).read_text()) if a.dispatch_evidence else None,
            registered_headless_evidence=(json.loads(Path(a.registered_headless_evidence).read_text())
                                          if a.registered_headless_evidence else None),
            transport_evidence=a.transport_evidence,jobs=a.jobs,
            profile_demands=json.loads(Path(a.profile_demands).read_text()) if a.profile_demands else None,
            explicit_profiles=json.loads(Path(a.explicit_profiles).read_text()) if a.explicit_profiles else None,
            profile=a.profile,
            work_request={"text":a.prompt_file.read_text(),"owner_harness":owner_pin} if a.prompt_file else None,
            selection_pins=pins or None,route_plan=route_plan_binding,
            execution_scope=a.execution_scope,
        )
        route=compose_route(**compose_args)
        worktree=None
        if (a.start and a.cwd is None and a.dispatch_evidence is None and shape!="direct"
                and not (route_plan_binding or {}).get("leg",{}).get("cwd")
                and _isolates_worktree(route)):
            # Work that changes source runs in its own worktree, not in the shared primary checkout.
            worktree=prepare_isolated_worktree(cwd,a.slug)
            print("worktree_prepared="+worktree["state"]+"".join(
                f" {key}={worktree[key]}" for key in ("path","branch","base","reason") if worktree.get(key)),file=sys.stderr)
            if worktree.get("cwd"):
                DISPATCH_DEFAULTS_WARNINGS.clear()
                route=compose_route(**{**compose_args,"cwd":worktree["cwd"]})
        for line in (*DISPATCH_DEFAULTS_WARNINGS,*pin_warnings):
            print(line,file=sys.stderr)
        _plan_for_card, _plan_source_for_card = a._route_chain_plan
        # Starting this route may create its campaign. Keep this invocation's
        # original choice instead of calling the newly-created stream a join.
        _campaign_for_card = compose_campaign_selection(route)
        if a.explain:
            print(compose_card(route, _plan_for_card, _plan_source_for_card, owner_harness=owner_pin,
                               route_plan_unreadable=route_plan_unreadable,
                               campaign_selection=_campaign_for_card),file=sys.stderr)
            print("route_file_written=0 explain=1",file=sys.stderr)
            print(json.dumps({"route_id":route["route_id"],"capability":route["capability"],
                              "effective_intensity":route["effective_intensity"],"shape":shape,
                              "composed":bool(route.get("composed")),
                              "nodes":[{"id":n["id"],"unit":n.get("unit"),"dispatch_depth":n.get("dispatch_depth"),
                                        "completion_gate":n.get("completion_gate"),"terminal":n.get("terminal") is True}
                                       for n in route["nodes"]],
                              "human_gates":route.get("human_gates"),"parallel_groups":route.get("parallel_groups"),
                              "campaign":_campaign_for_card,
                              "advisories":OWNER_WRITE_ADVISORY.advisories(route, owner_harness=owner_pin),
                              "tracked_gate_evidence":route.get("tracked_gate_evidence"),
                              **compose_observations(route)},sort_keys=True))
            return 0
        path = _emit_compiled_route(a,route,artifact_root)
        if a.start:
            from work_start import start_work
            jobs=Path(a.jobs or _compose_default_jobs())
            access_change=_record_access_change(route,jobs)
            print("\n".join(compose_decision_lines(route)), file=sys.stderr, flush=True)
            started=start_work(route,path,jobs)
            if access_change is not None:
                started={**started,"access_change":access_change}
            if worktree is not None and worktree.get("cwd"):
                started={**started,"worktree":worktree}
            print(json.dumps({**started, **compose_observations(route)},ensure_ascii=False),flush=True)
        # Bookkeeping runs after the work has started: the start does not depend on it (the sweep
        # never closes this route, and a cycle it seals is never the one this route begins or continues).
        _route_autoclose(artifact_root,"compose",route)
        print(compose_card(route, _plan_for_card, _plan_source_for_card, owner_harness=owner_pin,
                           route_plan_unreadable=route_plan_unreadable,
                           campaign_selection=_campaign_for_card, include_decisions=not a.start),file=sys.stderr)
        return 0
    if a.command=="correct":
        from dispatch_owner_input import submit, inspect, InputError
        jobs=Path(a.jobs or _compose_default_jobs())
        attempt=_current_owner_attempt(jobs,a.attempt_id)
        try:
            result=(submit(jobs,attempt,a.message_file.read_text(),a.request_id)
                    if a.message_file else inspect(jobs,attempt))
        except InputError as exc:
            print(json.dumps({"state":"not-admitted","reason":str(exc),
                              "next_step":"Retain the correction. Inspect the exact owner; do not restart it or treat a file edit as delivery."}))
            return 69
        if attempt!=a.attempt_id:
            result["redirected_from"]=a.attempt_id
        from dispatch_owner_input import blocked_owner_answers
        if a.message_file and (result.get("retained") or blocked_owner_answers(jobs,attempt)):
            # The owner had ended BLOCKED (also when it ended just after this answer was
            # queued): the answer continues the route now, in the same call, through the
            # one shared start (a replacement owner receives it).
            result=_continue_after_answer(jobs,attempt,result)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    if a.command=="start":
        from work_start import start_work
        if a.route is None:
            found,source,rows=caller_open_route(os.getcwd())
            if found is None:
                print(json.dumps({"state":"no-open-route" if source=="none" else "open-route-ambiguous",
                                  "cwd":os.getcwd(),"open_routes":rows,
                                  "next_step":("Run the resume_command of the route to continue."
                                               if rows else "No open route belongs to this session or cwd.")},
                                 ensure_ascii=False))
                return 2
            a.route=Path(found)
            print(f"route_default={found} source={source}",file=sys.stderr)
        a.route=resolve_route_argument(a.route,a.jobs)
        route=verify_route(json.loads(a.route.read_text()))
        jobs=Path(a.jobs or _compose_default_jobs())
        pin_change=_change_pins(route,jobs,a.pin) if a.pin else None
        access_change=_record_access_change(route,jobs)
        _record_route_chain(route, str(Path(a.route).resolve()), "start")
        print("\n".join(compose_decision_lines(route)), file=sys.stderr, flush=True)
        result=start_work(route,a.route,jobs,wait=a.wait,interview=a.interview,answers=a.answers,decision=a.decision)
        if pin_change is not None:
            result={**result,"pin_change":pin_change}
        if access_change is not None:
            result={**result,"access_change":access_change}
        print(json.dumps({**result, **compose_observations(route)},ensure_ascii=False))
        return 0
    if a.command=="compile":
        spec_read=(compose_spec_read(a.cwd,a.artifact_root,None) if a.spec_read is None else
                   {"satisfied":a.spec_read.lower() not in ("0","false","no"),"source":a.spec_read})
        gate={"spec_read":spec_read,
              "drift_verdict":a.drift_verdict,"workflow_mode":a.workflow_mode or a.tracking,
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
                slug=a.slug, campaign_key=a.campaign_key, parent_cycle_id=a.parent_cycle,
                profile_demands=json.loads(Path(a.profile_demands).read_text()) if a.profile_demands else None,
                explicit_profiles=json.loads(Path(a.explicit_profiles).read_text()) if a.explicit_profiles else None,
            )
        else:
            route=compile_route(
                a.capability,a.capability_mode,a.intensity,a.cwd,a.artifact_root,
                a.predicate,a.signal,a.transport,a.transport_evidence,a.inline_reason,
                a.tracking,gate,dispatch_evidence,registered_headless_evidence,
                slug=a.slug, campaign_key=a.campaign_key, parent_cycle_id=a.parent_cycle,
                profile_demands=json.loads(Path(a.profile_demands).read_text()) if a.profile_demands else None,
                explicit_profiles=json.loads(Path(a.explicit_profiles).read_text()) if a.explicit_profiles else None,
            )
        _emit_compiled_route(a,route,a.artifact_root)
    elif a.command=="continuation":
        source_path=Path(a.source_route).resolve(strict=True)
        source=verify_route(json.loads(source_path.read_text(encoding="utf-8")))
        # Keep the source path local to this invocation.  Do not attach it to
        # the verified route payload: continuation lineage hashes cover the
        # sealed route object, and a filesystem locator is runtime context,
        # not route identity.  Adding it here made valid owner-closure proof
        # markers fail their downstream currentness check.
        artifact=Path(a.artifact_root or source["artifact_root"]).resolve(strict=False)
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
        try:
            route=build_continuation_route(
                source,resume_from_node=a.resume_from_node,
                requested_boundary=a.requested_boundary,reason=a.reason,
                artifact_root=artifact,lineage_operation=a.lineage_operation,
                thread_id=a.thread_id,new_thread_id=a.new_thread_id,
                forked_from_id=a.forked_from_id,last_turn_id=a.last_turn_id,
                ephemeral=a.ephemeral,partial_group=partial,
                dispatch_evidence=(json.loads(Path(a.dispatch_evidence).read_text(encoding="utf-8"))
                                   if a.dispatch_evidence else None),
            )
        except ValueError:
            # Every other refusal on this command prints the zeroed receipt line
            # before raising; one raised from inside the builder must too, or a
            # reader cannot tell "nothing was launched" from "unknown".
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            raise
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
        if not immutable_code_root_equivalent(
            (launch_tuple.get("registry_root") or {}).get("path",""),
            (launch_tuple.get("runtime_root") or {}).get("path",""),
        ):
            print(
                "route_file_written=0 predecessor_attempts=0 registered=0 "
                "started=0 child_spawned=0",file=sys.stderr,
            )
            print(runtime_root_hint(),file=sys.stderr)
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
        # A continuation is an immutable route for ordinary callers.  For a
        # registered depth-1 owner, publish the forward candidate only after
        # that route has reached disk. The lifecycle adopts it only after an
        # exact child start; a failed proof never rewrites either input.
        try:
            from owner_route_binding import (
                OwnerRouteBindingError,
                publish_owner_route_advance_from_environment,
            )
            route_for_binding = dict(route)
            route_for_binding["route_file"] = str(output_path.resolve())
            advance = publish_owner_route_advance_from_environment(
                os.environ.get("AGENT_DISPATCH_JOBS", ""),
                source_route=source, target_route=route_for_binding,
                environ=os.environ,
            ) if os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") else None
        except OwnerRouteBindingError as exc:
            print(
                f"route_file_written=1 owner_route_advance_written=0 reason={exc}",
                file=sys.stderr,
            )
            raise ValueError(str(exc)) from exc
        if advance is not None:
            print("owner_route_advance_written=1", file=sys.stderr)
        # rule 6: a depth-1 owner records its continuation only once it actually advanced
        # the bound route (an unadvanced attempt leaves the predecessor route's own ●,
        # which is honest — nothing here claims progress the owner has not proven).
        if os.environ.get("AGENT_DISPATCH_DEPTH") != "1" or advance is not None:
            _record_route_chain(route, str(output_path.resolve()), "continuation")
        # Publication is a candidate. The existing producer begin path records
        # cycle admission when execution enters it; a refused batch never
        # transfers the predecessor's closing responsibility.
        print("cycle_binding_bound=0 cycle_binding_deferred=1 basis=execution-begin",file=sys.stderr)
        print(f"route_file={output_path.resolve()}",file=sys.stderr)
        print(json.dumps(route,sort_keys=True))
    elif a.command=="status":
        rows=route_status(a.artifact_root or _compose_artifact_root(os.getcwd()), open_only=a.open_only)
        from parent_next_directive import resume_command
        for row in rows:
            if not row["closed"] and not row.get("read_only"):
                row["resume_command"]=resume_command(row["route_file"],agent_home=ROOT)
        print(json.dumps(rows,sort_keys=True,indent=2))
    elif a.command=="finish":
        a.route=Path(_close_route_argument(a))
        raw=json.loads(a.route.read_text(encoding="utf-8"))
        route=verify_route(raw, raw.get("cwd"))
        if (route.get("capability") == "autopilot-refine"
                and route.get("entry_scope_contract_version") == 1):
            import artifact_producer
            output = artifact_producer.require_cycle_output(
                Path(route["artifact_root"]), Path(a.summary_file), route_id=route["route_id"])
            if output is not None:
                _require_entry_scope_review_preview(route, {"id": "one-shot"}, output)
        import inline_finish
        try:
            receipt=inline_finish.finish(a,route,a.route,sys.modules[__name__])
        except inline_finish.InlineFinishError as exc:
            legacy_root = False
            if str(exc) == "finish-route-cycle-missing":
                import artifact_producer
                legacy_root = artifact_producer.classify_root(
                    Path(route["artifact_root"]))["state"] == "inactive-with-legacy"
            if str(exc) not in {"finish-route-not-direct", "finish-inline-owner-sentinel-required"} and not legacy_root:
                raise
            receipt=_legacy_inline_finish(a,route,a.route,sys.modules[__name__], entry_error=exc)
        print(json.dumps(receipt,sort_keys=True))
    else:
        if a.command=="close":
            a.route=_close_route_argument(a)
        if a.command=="complete" and getattr(a, "inline", False) and not getattr(a, "route", None):
            try:
                from owner_route_binding import default_owner_route_file
                a.route = default_owner_route_file()
            except Exception as exc:
                raise ValueError(str(exc)) from exc
        if a.command=="complete" and not getattr(a, "inline", False):
            if not getattr(a, "route", None) or not getattr(a, "evidence", None):
                print("capability-route: complete requires --route and --evidence (omit only with --inline)", file=sys.stderr)
                raise SystemExit(2)
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
                        f"actual={canonical(mismatch.get('actual',mismatch)).decode()}"
                        " | " + runtime_root_hint(route),
                        file=sys.stderr,
                    )
                    print("registered=0 started=0 child_spawned=0",file=sys.stderr)
                    raise ValueError("launch-runtime-root-mismatch")
            print(f"route_id={route['route_id']}\nroute_hash={route['route_hash']}")
            if getattr(a,"cwd",None) and _COMMIT_SHA.fullmatch(str(route.get("source_commit") or "")):
                # Publish the shared lineage verdict for CLI consumers.
                verdict=source_lineage_verdict(route["cwd"],route["source_commit"])
                observed=(
                    route["source_commit"] if verdict.kind=="exact"
                    else verdict.commits[0] if verdict.commits else None
                )
                print("source_lineage="+json.dumps({
                    "kind":verdict.kind,"sealed":route["source_commit"],
                    "observed":observed,"distance":verdict.distance,
                    "branch":verdict.branch,"reason":verdict.reason,
                },sort_keys=True))
        elif a.command=="arbitrate":
            evidence=Path(a.evidence).resolve()
            if not evidence.is_file(): raise SystemExit("arbitration evidence missing")
            record=arbitrate_group(route,a.group,evidence)
            if a.output: atomic_write(a.output, record)
            print(json.dumps(record,sort_keys=True))
        elif a.command=="revise":
            evidence=Path(a.evidence).resolve()
            if not (evidence.is_file() or evidence.is_dir()):
                raise SystemExit("revision evidence missing")
            answers=tuple(x.strip() for x in (a.answers or "").split(",") if x.strip())
            author_attempt_id=a.author_attempt_id or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
            if not author_attempt_id:
                raise ValueError("revise-requires-author-attempt-id")
            result=publish_revision_locked(
                route,a.node,evidence,basis=a.basis,answers=answers,
                direction=a.direction,reason=a.reason,
                author_attempt_id=author_attempt_id,recorded_by=a.recorded_by,
                jobs=Path(a.jobs) if a.jobs else None,
            )
            marker=result.get("marker") or result["input_revision"]
            if a.output: atomic_write(a.output, marker)
            print(json.dumps(marker,sort_keys=True))
            print(f"tombstoned={','.join(result['tombstoned']) or '-'}",file=sys.stderr)
        elif a.command=="close":
            # An unproven terminal gate is recorded and warned about below, never refused:
            # the refusal was bypassed almost every time it fired.
            import route_parent_close
            outcome=route_parent_close.close(route, a.route, stop_resources=a.stop_resources,
                                            summary=a.summary, commit=a.commit)
            if outcome is None:
                outcome,created=close_route(route,a.route,a.commit,a.summary,allow_unproven=True)
            else:
                created=True
                from work_start import pin_handoff_continuation
                outcome=pin_handoff_continuation(route, a.route, route_parent_close.jobs_path(),
                                                {**outcome,"summary":a.summary or outcome.get("summary")})
                print(json.dumps(outcome,sort_keys=True))
                return
            print(json.dumps(outcome,sort_keys=True))
            if not created: print("capability-route: route already closed",file=sys.stderr)
            if outcome.get("review_independence_degraded"):
                print(
                    "capability-route: completed-review-degraded "
                    f"route_id={outcome['route_id']} "
                    f"nodes={','.join(outcome['review_independence_degraded'])} "
                    "-- these gates were not independently reviewed",
                    file=sys.stderr,
                )
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
                if getattr(a, "inline", False):
                    if a.dispatch_depth is None:
                        try:
                            a.dispatch_depth = int(node.get("dispatch_depth"))
                        except (TypeError, ValueError):
                            pass
                    if a.transport is None:
                        a.transport = "headless"
                    if a.execution_surface is None:
                        a.execution_surface = "inline"
                    if a.registered_worker is None:
                        a.registered_worker = "0"
                    if a.fallback_hop is None:
                        a.fallback_hop = "inline"
                    if not a.attempt_id:
                        owner_attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or ""
                        if owner_attempt:
                            a.attempt_id = f"{owner_attempt}-{a.node}-inline"
                    if not getattr(a, "evidence", None) and getattr(a, "reason", None):
                        try:
                            import artifact_producer as _ap
                            import hashlib as _hl
                            artifact_root = Path(route.get("artifact_root") or "")
                            record = _ap.route_cycle_for(artifact_root, route)
                            if record is None:
                                raise ValueError("inline-evidence-no-bound-cycle: pass --evidence with a cycle-local file")
                            out_dir = _ap.cycle_dir(artifact_root, record["campaign_id"],
                                                    record["cycle_id"], record) / "artifacts"
                            out_dir.mkdir(parents=True, exist_ok=True)
                            reason_text = f"# inline {a.node}\n\n{str(a.reason).strip()}\n"
                            digest = _hl.sha1(str(a.reason).strip().encode("utf-8")).hexdigest()[:8]
                            synth_path = out_dir / f"{route.get('route_id')}.{a.node}.{digest}.inline.md"
                            if synth_path.is_file():
                                if synth_path.read_text(encoding="utf-8") != reason_text:
                                    raise ValueError("inline-evidence-path-collision: retry with --evidence")
                                a._synthesized_evidence = None
                            else:
                                synth_path.write_text(reason_text, encoding="utf-8")
                                a._synthesized_evidence = str(synth_path)
                            a.evidence = str(synth_path)
                        except (OSError, ValueError, KeyError) as exc:
                            if isinstance(exc, ValueError) and str(exc).startswith(("inline-evidence-",)):
                                raise
                            raise ValueError(f"inline-evidence-unwritable:{exc}") from exc
                    if getattr(a, "reason", None):
                        print(f"inline-reason={a.reason}", file=sys.stderr)
                if not getattr(a, "evidence", None):
                    raise SystemExit("completion evidence missing")
                evidence=Path(a.evidence).resolve()
                # A completion artifact is one file or one directory of them; the
                # envelope inspector accepts both and `evidence_digest` names both.
                if not (evidence.is_file() or evidence.is_dir()):
                    raise SystemExit("completion evidence missing")
                # C-25b: check before completion runs, so a colliding `--output`
                # neither overwrites the pre-existing file's bytes nor leaves a
                # completion marker written behind a refused copy.
                if a.output and Path(a.output).exists():
                    raise ValueError("completion-output-exists")
                if a.reviewer_attempt and a.reviewer_subagent:
                    raise ValueError("reviewer-claim-conflict")
                review_claim=None
                if a.reviewer_attempt:
                    review_claim={"kind":"registered-worker","attempt_id":a.reviewer_attempt}
                elif a.reviewer_subagent:
                    review_claim={"kind":"native-subagent","transcript":a.reviewer_subagent}
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
                # A registered worker completing its own node may omit `--jobs` and
                # `--attempt-id`: the environment already names both. A call that states
                # attempt axes (the inline/unregistered form) or completes a resource run
                # takes nothing implicitly; an explicit value always wins.
                jobs,attempt_id=a.jobs,a.attempt_id
                inherited_jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None
                if explicit_attempt_metadata is None and node.get("kind")!="resource-runner":
                    if a.subsession_manifest:
                        jobs=jobs or inherited_jobs
                    else:
                        if attempt_id is None and (jobs or inherited_jobs):
                            attempt_id=_bound_env_attempt(route,node,jobs or inherited_jobs)
                        if attempt_id and not jobs:
                            jobs=inherited_jobs
                if a.check:
                    if not jobs or not attempt_id or a.output or review_claim or explicit_attempt_metadata or a.subsession_manifest:
                        raise ValueError("owner-closure-check-requires-exact-jobs-attempt-and-no-overrides")
                    if node.get("kind") != "review-worker":
                        # Owner closure only answers a blocking review; this read-only question has no
                        # answer elsewhere. Say so instead of refusing with an unrelated reason.
                        print(json.dumps({"result": "not-applicable", "read_only": True, "route_id": route["route_id"],
                                          "node_id": a.node, "reason": "owner-closure-review-nodes-only"},
                                         sort_keys=True))
                        return
                    proof = owner_closure_plan(route, node, evidence, jobs, attempt_id)
                    print(json.dumps({"result": "ready", "read_only": True, "route_id": route["route_id"],
                                      "node_id": a.node, "owner_closure_proof": proof}, sort_keys=True))
                    return
                if a.subsession_manifest:
                    if review_claim:
                        raise ValueError("reviewer-claim-unsupported-on-subsession-gate")
                    if not jobs or attempt_id or explicit_attempt_metadata is not None:
                        raise ValueError("subsession completion requires --jobs and forbids attempt axes")
                    route["_route_file"]=str(Path(a.route).resolve())
                    marker,row=complete_subsession_stage(
                        route,node,a.node,evidence,a.subsession_manifest,jobs,
                    )
                else:
                    try:
                        marker,row=complete_node(
                            route,node,a.node,evidence,
                            jobs=jobs,
                            attempt_id=attempt_id,
                            explicit_attempt_metadata=explicit_attempt_metadata,
                            review_claim=review_claim,
                        )
                    except Exception:
                        synth = getattr(a, "_synthesized_evidence", None)
                        if synth:
                            try:
                                Path(synth).unlink()
                            except OSError:
                                pass
                        raise
                if a.output: atomic_write(a.output, marker)
                print(json.dumps(marker,sort_keys=True))
                if row: print(json.dumps(row,sort_keys=True))
                if marker.get("review_independence") in ("degraded","owner-overridden"):
                    # Typed and on stderr, so an owner that closed its own review
                    # node cannot finish the stage without being told -- and so
                    # the §0.5 completion card has something to quote.
                    reason=(marker.get("reviewer_downgrade_reason")
                            or marker.get("review_gate_closure") or "-")
                    print(
                        "capability-route: completed-review-degraded "
                        f"route_id={route['route_id']} node={a.node} "
                        f"independence={marker['review_independence']} "
                        f"reviewer_kind={marker['reviewer_kind']} "
                        f"reason={reason}",
                        file=sys.stderr,
                    )

if __name__=="__main__":
    try: main()
    # OSError too: completion walks a worker-supplied tree, and a refusal there
    # must be a typed line, never a traceback (review S-a).
    except (ValueError,OSError,TOPO.TopologyError) as exc: print(f"capability-route: {exc}",file=sys.stderr); raise SystemExit(64)
    except Exception as exc:
        # A producer refusal (e.g. cutover-inactive) is a typed line too, never a traceback.
        import artifact_producer
        if not isinstance(exc, artifact_producer.ProducerError): raise
        print(f"capability-route: {exc}",file=sys.stderr); raise SystemExit(64)
