#!/usr/bin/env python3
"""Compile, verify, and complete immutable capability routes."""
from __future__ import annotations
import argparse, base64, contextlib, fcntl, hashlib, importlib.util, json, os, re, shlex, shutil, subprocess, sys, tempfile, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capability_topology", ROOT/"tools/capability_topology.py")
TOPO = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(TOPO)
DEFAULTS_SPEC = importlib.util.spec_from_file_location("dispatch_defaults", ROOT/"utilities/dispatch-defaults.py")
DEFAULTS = importlib.util.module_from_spec(DEFAULTS_SPEC); DEFAULTS_SPEC.loader.exec_module(DEFAULTS)
VALID_AFFINITY = DEFAULTS.AFFINITY_VALUES | {"unspecified"}
sys.path.insert(0, str(ROOT/"utilities"))
import artifact_locator as ARTIFACT_LOCATOR
import route_identity as ROUTE_IDENTITY
import dispatch_runtime_support as RUNTIME_SUPPORT
import dispatch_terminal_commit
import model_profile as PROFILE
import review_round_cap as REVIEW_ROUND_CAP
from dispatch_continuation_budget import COMPATIBILITY_FLOOR, TERMINAL_RESERVE_DEFAULT
from dispatch_contract import (
    row_is_subsession,
    CANONICAL_PARENT_TRANSPORTS,
    DispatchContractError,
    EXECUTION_SURFACES,
    FALLBACK_HOPS,
    PARENT_TRANSPORT_BY_DISPATCH_DEPTH,
    SUCCESS_NOTES,
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
    completion_marker_is_current,
    completion_attempt_readiness,
    completion_conflict_attempt,
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
    directory_artifact_reason,
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

canonical = ROUTE_IDENTITY.canonical
route_hash = ROUTE_IDENTITY.route_hash

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

def _first_parent_contains(path, ancestor, descendant):
    """True when `ancestor` is on `descendant`'s first-parent line in this tree.

    One probe for the whole file: `_grounding_cwd_lineage_ok` (sealed-vs-fresh
    grounding) and the continuation pin rebind ask the same git question, and a
    second copy would be a second place to forget the timeout.
    """
    try:
        probe=subprocess.run(
            ["git","-C",str(path),"rev-list","--first-parent",descendant],
            text=True,capture_output=True,timeout=30,
        )
    except (OSError,subprocess.SubprocessError):
        # Same set as `_inside_git_worktree`: two probes of the same shape must
        # not disagree about what counts as "cannot answer" (round 3, S4).
        # `TimeoutExpired` is a `SubprocessError`, so this only widens.
        return False
    return probe.returncode == 0 and ancestor in probe.stdout.split()

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
    return _first_parent_contains(path,sealed,actual)

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
            raise ValueError("continuation-human-gate-release-proof-drift")
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
            raise ValueError("continuation-human-gate-release-proof-drift")
    return proofs

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
        "campaign_key","parent_cycle_id",
        "requested_intensity",
        "effective_intensity","owner_model_profile","execution_topology",
        "owner_dispatch_depth","max_dispatch_depth","tracking",
        "tracked_gate_evidence","spec_touch","cwd","source_commit",
        "registry_digest","dispatch_defaults_digest","dispatch_allocation",
        "owner_harness_policy","selection","human_gates","human_gate_bindings",
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
    )
    route={key:json.loads(json.dumps(source_route[key]))
           for key in inherited_keys if key in source_route}
    route.update(result)
    if route.get("profile_selection_contract_version") == 1:
        retained = {node["id"] for node in route_nodes} | {"__owner__"}
        for key in ("profile_demands", "explicit_profiles"):
            route[key] = {k: v for k, v in route.get(key, {}).items() if k in retained}
    # Defect C: the pin must name the same commit the grounding tuple above sealed.
    source_commit,source_commit_rebind,rebind_declined=_continuation_source_commit(
        source_route,route_nodes,
    )
    if source_commit is not None:
        route["source_commit"]=source_commit
    if source_commit_rebind is not None:
        route["source_commit_rebind"]=source_commit_rebind
    if not rebind_declined:
        # A declined rebind deliberately keeps the older pin (an SD-67 retry the
        # launch guard must adjudicate), so pin and grounding legitimately differ
        # there and only there.
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
        if _inside_git_worktree(cwd) and not _first_parent_contains(cwd,inherited,rebound):
            raise ValueError("continuation-source-commit-rebind-lineage-unproven")
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

def worktree_mutating_scope(scope):
    """Does this write scope let a node mutate the worktree?

    The **one** definition of the rule. `worker-route-guard.py` imports this
    module and calls this function, so the decline below classifies a node
    exactly the way the guard that adjudicates it does. A second near-identical
    copy would be two answers to one question, and the drift would be silent.
    """
    if scope in ("target-artifact","source-scoped"):
        return True
    root=scope[:-3] if str(scope).endswith("/**") else scope
    return root=="source"

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

def continuation_lineage_route_ids(source_route):
    """Every route id in this continuation's lineage, nearest ancestor first.

    Shared with `worker-route-guard.py`, which asks the same question from the
    other side: the builder needs it to decide whether to keep a pin, the guard
    needs it to find the retry evidence that pin implies (SD-133). One
    definition, so the two can never disagree about what an ancestor is.

    A declined continuation records no attempts of its own, so asking the
    registry only about the immediate predecessor finds a clean slate one
    generation later and the decline evaporates (SD-128 review round 3, B1).
    The lineage is already carried: `source_route_id` names the predecessor and
    `supersession_edges` accumulates every earlier `from_route_id`.

    `source_route_id` and the first edge name the same route, so for a
    first-generation continuation they are redundant. They stop being redundant
    at the second generation, where a grandparent is reachable **only** through
    an inherited edge -- and that is the common shape, not the exception.
    """
    ids=[]
    candidates=[source_route.get("route_id"),source_route.get("source_route_id")]
    for edge in (source_route.get("supersession_edges") or []):
        if isinstance(edge,dict):
            candidates.append(edge.get("from_route_id"))
    for value in candidates:
        if isinstance(value,str) and value and value not in ids:
            ids.append(value)
    return ids

def _authoritative_lineage_registry(source_route):
    """The registry this lineage provably wrote to, or None if unprovable.

    Deliberately stricter than `_continuation_source_jobs`, which may fall
    through to the live canonical registry so that a continuation can still read
    *migrated markers* when a sealed release tree is pruned. Markers carry their
    own route binding and attempt link, so that substitution is safe there. Here
    the evidence is the **absence** of a row, and absence read out of a registry
    this lineage never wrote to is not evidence at all (review round 3, B2a: the
    compat window silently answers from a different `jobs.log`, which exists and
    is a regular file, and reports no rows).

    Only `exact` and `aliased` resolutions are accepted -- `aliased` because the
    migration journal digest-verifies the substitution.
    """
    jobs=(
        ((source_route.get("launch_compatibility_tuple") or {}).get("jobs_path") or {})
        .get("path")
    )
    if not isinstance(jobs,str) or not jobs or not Path(jobs).is_absolute():
        return None
    try:
        sealed=Path(jobs).expanduser().resolve(strict=False)
        resolution=resolve_dangling_registry(sealed)
    except (OSError,ValueError,DispatchContractError):
        return None
    if resolution.status=="exact":
        return sealed
    if resolution.status=="aliased":
        return resolution.jobs_path
    return None

def _prior_registry_attempt(source_route, node_id):
    """Did this continuation's lineage already record an attempt on this node?

    **Fail closed.** The rebind is declined -- return True -- whenever the answer
    cannot be *proved* negative. Three things must all hold before an absent row
    is read as "this node never ran":

    1. the registry is provably the one the lineage wrote to
       (`_authoritative_lineage_registry`),
    2. the lineage is non-empty, and
    3. that registry actually contains at least one row for the lineage.

    (3) is what separates "no attempt on this node" from "this registry never saw
    this route". `registry_rows` returns `[]` for a missing file and for a
    truncated one alike, so an empty read is ambiguous by construction -- and
    deleting or truncating `jobs.log` is exactly the shape an operator produces
    (review round 3, B2b). Declining on an unprovable answer is never worse than
    the behaviour before defect C was fixed: the pin simply stays inherited.
    """
    jobs=_authoritative_lineage_registry(source_route)
    if jobs is None:
        return True
    lineage=continuation_lineage_route_ids(source_route)
    if not lineage:
        return True
    try:
        rows=_stage_fallback().registry_route_rows(jobs,lineage)
    except (OSError,ValueError):
        return True
    if not rows:
        return True
    return any(
        row.get("attempt_id") and row.get("route_node")==str(node_id)
        for row in rows
    )

def _retry_mutation_node(source_route, continuation_nodes):
    """The first node this continuation re-runs that is an SD-67 mutation retry.

    Not just the resume node: a continuation resuming at `plan` still carries
    `execute`, and in a real `autopilot-code` route `execute` is the only
    worktree-mutating node. Asking about the resume node alone let a plan-time
    resume re-pin the route, after which `execute` met `head == source_commit`
    and passed the guard on the trivial branch -- walking past the very gate
    the decline exists to protect (review round 2, B1).
    """
    for node in continuation_nodes or ():
        if not _node_mutates_worktree(node):
            continue
        if _prior_registry_attempt(source_route,node.get("id")):
            return node
    return None

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
        raise ValueError(
            f"continuation-source-commit-grounding-mismatch: pin={source_commit} grounding={base}"
        )

def _continuation_source_commit(source_route, continuation_nodes):
    """Seal the resume-time HEAD as the continuation's `source_commit`.

    The source route pinned the HEAD it was compiled at, but a continuation seals
    the *current* worktree HEAD into `launch_compatibility_tuple.grounding_roots.cwd`.
    Inheriting the old pin made the route contradict itself: after depth-0
    fast-forwarded the worktree, worker-route-guard refused every pre-mutation node
    with `route-source-commit-mismatch` (defect C, route rt-d7541f1033ae677f).
    Two sources, one value: the pin and the grounding must name the same commit.

    Returns `(source_commit, rebind_record, rebind_declined)`.

    The rebind is **declined** -- the inherited pin is kept, exactly as before this
    fix -- when *any* node this continuation will re-run mutates the worktree and
    the source route already recorded an attempt on it. That is SD-67's mutation
    retry, whose evidence gate lives in `worker-route-guard.py`. Re-pinning there
    would let a continuation launder a retry past that gate, which
    `OPERATIONS §5.10` forbids ("never re-pin the route to manufacture this
    evidence").

    A declined rebind **refuses** the mutation node; it does not hand it to an
    adjudicating guard. The guard looks its retry evidence up under
    `route["route_id"]`, which is the continuation's own id, while the prior rows
    were written under the source route's -- so it finds none and raises
    `route-source-commit-mismatch`. That is precisely what main does today, so
    declining is never a regression, but the operator is refused rather than
    adjudicated. Teaching the guard to follow `source_route_id` is a separate
    change with its own spec question, not something to smuggle in here.
    """
    inherited=source_route.get("source_commit")
    cwd=source_route.get("cwd")
    if not inherited or not cwd:
        return inherited,None,False
    if _retry_mutation_node(source_route,continuation_nodes) is not None:
        return inherited,None,True
    head=_git_commit(cwd)
    if head == inherited or head == "unversioned" or inherited == "unversioned":
        return inherited,None,False
    if not _first_parent_contains(cwd,inherited,head):
        raise ValueError(
            f"continuation-source-commit-diverged: expected={inherited} observed={head}"
        )
    return head,{
        "contract_version":CONTINUATION_SOURCE_COMMIT_REBIND_VERSION,
        "inherited_source_commit":inherited,
        "rebound_source_commit":head,
        "basis":"first-parent-descendant",
        "cwd":str(Path(cwd).resolve(strict=False)),
    },False

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


def _owner_profile_policy_gap(owner_profile, effective, registry):
    """None when `owner_profile` is what the portable intensity policy admits
    for `effective`, else the typed gap: `top-requires-owner` (the exception
    profile on a direct route, which has no owner) or `mismatch`. One rule
    read by both the compiler and `verify_route` (review R1 B1: the compiler
    admitted `top` above the intensity's expected owner profile while verify
    still demanded equality, so every `top` route compiled and then could
    not bind, launch, harvest, or close)."""

    if effective == "direct":
        if owner_profile == PROFILE.TOP_PROFILE:
            return "top-requires-owner"
        return None if owner_profile is None else "mismatch"
    expected = registry["owner_profile_by_intensity"].get(effective)
    if owner_profile == PROFILE.TOP_PROFILE:
        return None
    return None if not expected or owner_profile == expected else "mismatch"


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

    Zero supported harnesses cannot frame at all. One is a recorded
    degradation, not a refusal (user decision, 2026-09-10): both legs run on
    that harness with their two perspectives and the route says so."""

    harnesses = sorted({
        row.get("harness") for row in candidates or []
        if row.get("status") == "supported" and row.get("harness")
    })
    if not harnesses:
        raise ValueError("quick-frame-harness-unavailable")
    return ("cross-harness" if len(harnesses) >= 2
            else "single-harness:" + harnesses[0])


def _stamp_frame_profiles(nodes, owner_profile, owner_demand):
    """Stamp every frame leg's `model_profile` from the one tier ladder.

    ONE function called by BOTH the compiler and `verify_route`'s expected-node
    recomputation. It has to be shared: the verifier rebuilds the node list
    from the recipe and compares field by field, so a ladder applied on only
    one side reports every standard+ route as
    `node-profile-declaration-mismatch:frame` -- which is exactly what happened
    the first time this was written inline in the compiler.

    Runs BEFORE `_seal_profile_demands` on both sides, because that is what
    turns the stamped profile into the node's sealed selection."""

    rungs = PROFILE.frame_profile_for_owner(owner_profile)
    for node in nodes:
        if not _frame_node(node):
            continue
        # `frame` is the anchor leg (the one raised a tier); every other leg of
        # the pair -- today only `frame-alternative` -- stays at the owner's
        # working tier so the pair keeps two genuinely different voices.
        profile = rungs["anchor" if node.get("id") == "frame" else "others"]
        node["model_profile"] = profile
        if profile == PROFILE.TOP_PROFILE:
            # `top` is not a portable profile, so it cannot be sealed through
            # the legacy "explicit profile, no demand" path -- the resolver
            # refuses that with `profile-demand-required`. Give the anchor a
            # real explicit selection instead: the owner's own demand when the
            # caller supplied one (same judgment, same evidence, one
            # decision), otherwise the frame shape's intrinsic demand, whose
            # reasons say in as many words that the shape is speaking rather
            # than task-specific evidence somebody gathered.
            node["profile_explicit"] = True
            node["profile_demand"] = json.loads(json.dumps(
                owner_demand or PROFILE.FRAME_ANCHOR_SHAPE_DEMAND))
    return nodes


def _recipe_has_frame(recipe):
    return any(_frame_node(node) for node in recipe["standard_plus"]["nodes"])


def _quick_gate_bindings(recipe):
    """Quick's single human gate binding: the frame pair fences `one-shot`."""

    bindings = ([{"gate": "frame-review", "node": "one-shot", "position": "entry"}]
                if _recipe_has_frame(recipe) else [])
    bindings.extend({"gate": gate, "node": "one-shot", "position": "terminal"}
                    for gate in recipe["quick"].get("inline_human_gates", []))
    return bindings


def _seal_profile_demands(nodes, profile_demands=None, explicit_profiles=None, *, legacy=False):
    demands = profile_demands or {}
    explicit_profiles = explicit_profiles or {}
    for node in nodes:
        if node.get("kind") == "resource-runner":
            continue
        node_id = node["id"]
        demand = demands.get(node_id, node.get("profile_demand"))
        supplied = node_id in demands or "profile_demand" in node
        if supplied:
            demand = PROFILE.normalize_profile_demand(demand)
        explicit = explicit_profiles.get(node_id)
        if node.get("profile_explicit") and node_id not in explicit_profiles:
            explicit = node.get("model_profile")
        if not supplied:
            if node_id in explicit_profiles:
                raise ValueError("profile-demand-required:" + node_id)
            explicit = node.get("model_profile", "light")
        selection = PROFILE.resolve_profile_demand(
            demand, explicit_profile=explicit, legacy=legacy,
            existing_versioned_stage=legacy,
        )
        node["profile_demand"] = demand
        node["profile_selection"] = selection
        node["model_profile"] = selection["resolved_profile"]
    return nodes


def _profile_input_maps(nodes, demands, explicit):
    valid = {n["id"] for n in nodes if n.get("kind") != "resource-runner"} | {"__owner__"}
    normalized = {}
    for label, values in (("profile_demands", demands), ("explicit_profiles", explicit)):
        if values is not None and (not isinstance(values, dict) or set(values) - valid):
            raise ValueError("profile-input-unknown-node:" + label)
    for key, value in (demands or {}).items():
        normalized[key] = PROFILE.normalize_profile_demand(value)
    frame_ids = {n["id"] for n in nodes if _frame_node(n)}
    for key, value in (explicit or {}).items():
        if key not in normalized or value not in PROFILE.KNOWN_PROFILES:
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
            if "__owner__" in demands else owner_profile),
        legacy=True, existing_versioned_stage=True)
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
        profile=route.get("owner_model_profile") or "light", existing_versioned_stage=True,
    )
    for node in route.get("nodes", []):
        if node.get("kind") == "resource-runner":
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
                         explicit_profiles=None, campaign_key=None, parent_cycle_id=None):
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
        explicit_profiles=explicit_profiles)

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
COMPOSE_SHAPES = ("direct", "solo", "staged")
SHAPE_INTENSITY = {"direct": "direct", "solo": "quick", "staged": "standard"}
INTENSITY_SHAPE = {"direct": "direct", "quick": "solo"}
ROUTE_ORIGINS = ("preset", "compose")
COMPOSE_DEFAULT_CAPABILITY = "autopilot-code"
COMPOSE_DEFAULT_CHILDREN = ("claude", "codex")
COMPOSE_SPEC_CANDIDATES = ("spec/prd.md",)


def shape_for_intensity(effective):
    return INTENSITY_SHAPE.get(effective, "staged")


def parse_graph_spec(text):
    """`execute,test,report` or `execute:dev/refactor,test` -> [(id, unit|None)]."""
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
        rows.append((node_id, unit.strip() or None))
    ids = [row[0] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("compose-graph-duplicate-node")
    return rows


def _compose_inputs(base_nodes, base_node, kept):
    """Keep the base node's declared inputs wherever they can still exist.

    An input stays when it is not produced by any recipe node (an external
    literal such as `task`/`spec`/`source`), when its producer is a kept
    node, or when it is a semantic token of the tree (`source-diff`, …) that
    no node has to author. An input whose only producer was dropped is
    removed -- the stage brief (`dispatch_stage_advance.render_stage_brief`)
    prints `inputs` verbatim, so a dropped node's file must not be promised.
    Nothing is added: the chain edge lives in `depends_on`, and a full-graph
    compose must yield exactly the preset's inputs. (Canary review round 1
    B1, round 2 M2.)
    """
    producers = {}
    for candidate in base_nodes.values():
        for output in candidate.get("outputs") or []:
            producers.setdefault(output, set()).add(candidate["id"])
    inputs = []
    for item in base_node.get("inputs") or []:
        owners = producers.get(item)
        if owners is None or owners & kept or TOPO._is_semantic_output(item):
            if item not in inputs:
                inputs.append(item)
    return inputs or ["task"]


def compose_subgraph_recipe(registry, base_recipe, graph_spec):
    """Cut the caller's stage subgraph out of the capability's own recipe.

    The nodes keep their unit, kind, gate, write scope, profile and permissions;
    only the edges change: the subgraph is re-linked in the caller's order, the
    last node becomes the terminal, a human gate a kept node raised is rebound
    to the entry of the node that now follows it (dropped when nothing
    follows), and a parallel group survives only when its anchor is kept and is
    not the new terminal (G6). Validation stays with `_validate_recipe` --
    this function never re-implements a rule, it only assembles.
    """
    base_nodes = {node["id"]: node for node in base_recipe["standard_plus"]["nodes"]}
    ids = [node_id for node_id, _ in graph_spec]
    unknown = [node_id for node_id in ids if node_id not in base_nodes]
    if unknown:
        raise ValueError(
            "compose-graph-unknown-node:" + ",".join(unknown)
            + " (available: " + ",".join(base_nodes) + ")"
        )
    nodes = []
    overrides = {}
    for index, (node_id, unit) in enumerate(graph_spec):
        node = json.loads(json.dumps(base_nodes[node_id]))
        if unit:
            choices = node.get("unit_choices")
            if choices is not None and unit not in choices:
                raise ValueError(
                    f"compose-unit-not-in-choices:{node_id}:{unit} (choices: {','.join(choices)})"
                )
            if node.get("kind") in ("capability-owner", "resource-runner"):
                raise ValueError(f"compose-unit-override-reserved:{node_id}")
            node["unit"] = unit
            node["role"] = TOPO._unit_frontmatter(unit)["role"]
            overrides[node_id] = unit
        previous = nodes[-1] if nodes else None
        node["depends_on"] = [previous["id"]] if previous else []
        node["inputs"] = _compose_inputs(base_nodes, base_nodes[node_id], set(ids))
        node.pop("terminal", None)
        node.pop("terminal_gate", None)
        node.pop("continuation", None)
        node.pop("parallel_group", None)
        nodes.append(node)
    terminal = nodes[-1]
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
    for index, node in enumerate(nodes[:-1]):
        base = base_nodes[node["id"]]
        continuation = base.get("continuation") or {}
        if continuation.get("kind") == "human-gate":
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
        if (row.get("position") == "entry" and node_id in kept_ids
                and not (base_nodes[node_id].get("depends_on") or [])
                and row.get("gate") not in gates):
            bindings.append({"gate": row["gate"], "node": node_id, "position": "entry"})
            gates.append(row["gate"])
    groups = [
        json.loads(json.dumps(group))
        for group in base_recipe["standard_plus"].get("parallel_groups") or []
        if group["node"] in kept_ids and group["node"] != terminal["id"]
    ]
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
        "artifact_scope": json.loads(json.dumps(base_recipe["artifact_scope"])),
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
        "resume_retry_boundaries": list(ids),
        "compose": {"origin": "compose", "shape": "staged", "graph": list(ids),
                    "unit_overrides": overrides, "base_capability": base_recipe["capability"]},
    }
    if groups:
        recipe["standard_plus"]["parallel_groups"] = groups
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
        return compose_subgraph_recipe(registry, base, graph) == recipe
    except (ValueError, KeyError, TypeError, IndexError):
        return False


def _compose_default_jobs():
    inherited = os.environ.get("AGENT_DISPATCH_JOBS")
    if inherited:
        return Path(inherited)
    return stable_state_root(os.environ) / "jobs.log"


def _compose_readiness(cwd, jobs, parent_harness, children):
    spec = importlib.util.spec_from_file_location(
        "hearting_dispatch_readiness", ROOT / "utilities" / "dispatch-readiness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.generate(
            worktree=Path(cwd), jobs=Path(jobs),
            owner_harnesses=[parent_harness], child_harnesses=list(children),
        )
    except module.ReadinessError as exc:
        raise ValueError(f"compose-readiness-unavailable:{exc}") from exc


def compose_spec_read(cwd, artifact_root, explicit):
    """`auto` is honest, not permissive: with no spec candidate it records the
    absence; with one present it refuses and names the file the caller must
    read and assert (`--spec-read <source>`). The spec-read gate is a real
    invariant (WORKFLOW §7.0); compose only removes the boilerplate case."""
    if explicit not in (None, "", "auto"):
        return {"satisfied": explicit.lower() not in ("0", "false", "no"), "source": explicit}
    present = []
    for root in (Path(cwd), Path(artifact_root)):
        for rel in COMPOSE_SPEC_CANDIDATES:
            candidate = root / rel
            if candidate.is_file():
                present.append(str(candidate))
    if present:
        raise ValueError("compose-spec-read-required:" + ",".join(sorted(set(present))))
    return {"satisfied": True, "source": "compose-auto: no spec/prd.md under cwd or artifact root"}


def compose_route(*, capability, capability_mode, shape, graph, slug, cwd, artifact_root,
                  intensity=None, signals=(), spec_read=None, drift_verdict=None,
                  tracking=None, artifact_guard=None, children=None, parent_harness="claude",
                  dispatch_evidence=None, registered_headless_evidence=None,
                  transport_evidence="compose-default", jobs=None, profile_demands=None, explicit_profiles=None,
                  campaign_key=None, parent_cycle_id=None):
    """Resolve every default, then compile through the ordinary sealer."""
    if shape not in COMPOSE_SHAPES:
        raise ValueError(f"compose-shape-invalid:{shape}")
    if shape != "staged" and graph:
        raise ValueError(f"compose-graph-only-staged:{shape}")
    if shape == "staged" and not graph:
        raise ValueError("compose-graph-required")
    registry = TOPO.load_registry()
    base = next((r for r in registry["recipes"] if r["capability"] == capability), None)
    if base is None:
        raise ValueError(f"compose-capability-unknown:{capability}")
    if capability_mode is None:
        capability_mode = "dev" if "dev" in base["modes"] else sorted(base["modes"])[0]
    if capability_mode not in base["modes"]:
        raise ValueError(f"compose-mode-unknown:{capability_mode} (modes: {','.join(sorted(base['modes']))})")
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
    if tracking is None:
        tracking = "tracked" if shape == "staged" else "untracked"
    gate = {
        "spec_read": compose_spec_read(cwd, artifact_root, spec_read),
        "drift_verdict": drift_verdict or "no-spec-impact: compose default (caller asserted no spec-significant change)",
        "workflow_mode": tracking,
        "artifact_guard": {"satisfied": True, "source": artifact_guard or "compose-prechecked"},
    }
    predicates = list(base["direct_predicates"]) if shape == "direct" else []
    signals = sorted(set(signals or ()))
    if shape == "direct" and signals:
        raise ValueError("compose-direct-signals-conflict")
    readiness = None
    if shape == "staged" and dispatch_evidence is None:
        readiness = _compose_readiness(cwd, jobs or _compose_default_jobs(), parent_harness,
                                       children or COMPOSE_DEFAULT_CHILDREN)
        dispatch_evidence = {"tuples": readiness["tuples"], "native_subagent": []}
    if shape == "solo" and registered_headless_evidence is None:
        readiness = readiness or _compose_readiness(cwd, jobs or _compose_default_jobs(),
                                                    parent_harness, children or COMPOSE_DEFAULT_CHILDREN)
        registered_headless_evidence = {"candidates": readiness["candidates"]}
    common = dict(
        signals=signals, transport=None, transport_evidence=transport_evidence,
        tracking=tracking, tracked_gate_evidence=gate, slug=slug,
        campaign_key=campaign_key, parent_cycle_id=parent_cycle_id,
        dispatch_evidence=dispatch_evidence,
        registered_headless_evidence=registered_headless_evidence,
        route_origin="compose", shape=shape,
        profile_demands=profile_demands, explicit_profiles=explicit_profiles,
    )
    if shape == "staged":
        recipe = compose_subgraph_recipe(registry, base, parse_graph_spec(graph))
        route = compile_composed_route(
            recipe, capability_mode, requested, cwd, artifact_root,
            predicates=predicates, inline_reason=None, **common)
    else:
        route = compile_route(
            capability, capability_mode, requested, cwd, artifact_root,
            predicates=predicates, inline_reason="atomic-direct" if shape == "direct" else None,
            **common)
    return route


def compose_card(route):
    """One-line `[경로]` notice the acting session pastes instead of a card."""
    shape = route.get("selection", {}).get("shape") or shape_for_intensity(route["effective_intensity"])
    ids = [node["id"] for node in route["nodes"]]
    graph = "→".join(ids) if route.get("composed") else (ids[0] if ids else "-")
    gates = ",".join(sorted({row["gate"] for row in route.get("human_gate_bindings") or []})) or "없음"
    return (
        f"[경로] {route['capability']} · {shape}({route['effective_intensity']}) {graph}"
        f" · route {route['route_id']} · origin compose · 사람 게이트 {gates}\n"
        f"  cwd {route['cwd']} · slug {route.get('slug', '-')}"
    )


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
                         registered_headless_evidence=None, slug=None, composed=False,
                  route_origin="preset", shape=None, profile_demands=None,
                  explicit_profiles=None, campaign_key=None, parent_cycle_id=None):
    dispatch_terminal_commit.require_current_cleanup("route-compile")
    if route_origin not in ROUTE_ORIGINS: raise ValueError("invalid route origin")
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
        # Quick's cross-harness guarantee is carried entirely by this candidate
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
    profile_demands, explicit_profiles = _profile_input_maps(nodes, profile_demands, explicit_profiles)
    owner_demand = profile_demands.get("__owner__")
    owner_profile_selection = PROFILE.resolve_profile_demand(
        owner_demand, explicit_profile=(explicit_profiles.get("__owner__") if owner_demand
                                       else owner_model_profile or "light"),
        legacy=True, existing_versioned_stage=True,
    )
    resolved_owner_profile = owner_profile_selection["resolved_profile"]
    # A direct route seals no owner profile; the one thing it must still
    # refuse is an explicit `top` (direct runs inline in the main session,
    # which already IS the top model's home -- there is no owner to give it to).
    gap = _owner_profile_policy_gap(
        resolved_owner_profile if effective != "direct" or resolved_owner_profile == PROFILE.TOP_PROFILE else None,
        effective, registry)
    if gap == "top-requires-owner":
        raise ValueError("owner-profile-top-requires-owner")
    if gap:
        raise ValueError("owner-profile-eligibility-conflict")
    if effective != "direct":
        owner_model_profile = resolved_owner_profile
    if resolved_owner_profile == PROFILE.TOP_PROFILE:
        # Review R1 B2 / R2 B1: the owner's own node -- and only it -- seals
        # the same explicit `top` selection the owner did; otherwise the route
        # claims `top` while its node says `balanced-deep`, and the launched
        # owner's route guard refuses the mismatch.
        for node in nodes:
            if _owner_node(node, effective):
                node["model_profile"] = PROFILE.TOP_PROFILE
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
    dispatch_defaults_digest,dispatch_allocation,owner_harness_policy=_seal_dispatch_defaults(
        nodes, capability, owner_model_profile
    )
    spec_touch=any(_scope_touches_spec(scope) for node in nodes for scope in node["write_scope"])
    # SD-116 WP4 (D47-9): the compiler now seals the continuation budget into
    # the route itself, sealed into `route_hash` (it is added before the hash
    # is computed, unlike `owner_attempt_id`/`route_family_key`). `ordinary`
    # uses the identical `max(COMPATIBILITY_FLOOR, declared_nodes+retry_slots)`
    # derivation `dispatch_continuation_budget.resolve_continuation_budget()`
    # already used for the pre-WP4 "bound-route" path, so `ordinary` never
    # shrinks below the pre-SD-116 `limit` for the same route shape (D47-9).
    _continuation_declared_nodes=len(nodes)
    _continuation_retry_slots=len(set(recipe["resume_retry_boundaries"]))
    _continuation_ordinary=max(
        COMPATIBILITY_FLOOR, _continuation_declared_nodes+_continuation_retry_slots)
    continuation_budget={
      "contract_version":1,
      "declared_nodes":_continuation_declared_nodes,
      "review_round_cap":REVIEW_ROUND_CAP.max_review_rounds(effective),
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
      "profile_demands":profile_demands,"explicit_profiles":explicit_profiles,
      "owner_profile_demand":owner_demand,"owner_profile_selection":owner_profile_selection,
      "execution_topology":("inline" if effective=="direct" else recipe["quick"]["topology"] if effective=="quick" else recipe["topology_class"]),
      "owner_dispatch_depth":0 if effective=="direct" else (recipe["quick"]["owner_dispatch_depth"] if effective=="quick" else recipe["standard_plus"]["owner_dispatch_depth"]),
      "max_dispatch_depth":recipe["quick"]["max_dispatch_depth"] if effective=="quick" else (0 if effective=="direct" else recipe["standard_plus"]["max_dispatch_depth"]),
      "tracking":tracking,"tracked_gate_evidence":evidence,"spec_touch":spec_touch,
      "cwd":str(cwd),"artifact_root":str(artifact),"source_commit":_git_commit(cwd),
      "registry_digest":TOPO.registry_digest(registry),
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
    if route.get("route_hash") != route_hash(route): raise ValueError("stale or modified route hash")
    if route.get("route_id") != "rt-"+route["route_hash"].split(":",1)[1][:16]: raise ValueError("invalid route id")
    if expected_cwd and Path(expected_cwd).resolve()!=Path(route["cwd"]): raise ValueError("route cwd mismatch")
    _verify_profile_contract(route)
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
        if route.get("profile_selection_contract_version") == 1:
            # Same ladder, same order as the compiler: stamp, then seal.
            _stamp_frame_profiles(expected_nodes, route.get("owner_model_profile"),
                                  route.get("owner_profile_demand"))
            _seal_profile_demands(expected_nodes, route.get("profile_demands"),
                                  route.get("explicit_profiles"),
                                  legacy=_versioned_subgraph(registry, composed_recipe))
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
            if route.get("profile_selection_contract_version") == 1:
                # Same ladder, same order as the compiler: stamp, then seal.
                _stamp_frame_profiles(expected_nodes, route.get("owner_model_profile"),
                                      route.get("owner_profile_demand"))
                _seal_profile_demands(expected_nodes, route.get("profile_demands"),
                                      route.get("explicit_profiles"), legacy=True)
                by_id = {n["id"]: n for n in expected_nodes}
                for node in route.get("nodes", []):
                    expected = by_id.get(node.get("id"))
                    if expected and node.get("kind") != "resource-runner" and any(
                        node.get(key) != expected.get(key)
                        for key in ("profile_demand", "profile_selection", "model_profile")):
                        raise ValueError("node-profile-declaration-mismatch:" + node["id"])
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
    # Mirror of the compiler: quick binds its own frame gate, only `direct`
    # binds nothing. Verifier and compiler must move together or a quick route
    # compiles and then refuses to verify.
    expected_bindings=json.loads(json.dumps(
        _quick_gate_bindings(route_recipe) if route.get("effective_intensity")=="quick"
        else route_recipe["human_gate_bindings"]
        if route.get("effective_intensity")!="direct" else []))
    if route.get("human_gate_bindings") != expected_bindings:
        raise ValueError("route human gate bindings differ from the sealed recipe")
    if route.get("workflow_contract") != _workflow_contract(
            registry, route.get("nodes",[]), expected_bindings):
        raise ValueError("route workflow contract differs from the realized stage graph")
    if {row["gate"] for row in expected_bindings} - set(route.get("human_gates") or []):
        raise ValueError("route binds an undeclared human gate")
    if route.get("effective_intensity") != "direct" and "preview-disposition" in (route.get("human_gates") or []):
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
        optional = {"usage_gate_used_percent", "depth_affinity", "depth_affinity_weight", "usage_headroom_exponent"}
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
        weight = allocation.get("depth_affinity_weight", 0.5)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0.0 <= weight <= 1.0:
            raise ValueError("invalid dispatch_allocation affinity weight")
        exponent = allocation.get("usage_headroom_exponent", 1)
        if isinstance(exponent, bool) or not isinstance(exponent, int) or not 1 <= exponent <= 4:
            raise ValueError("invalid dispatch_allocation headroom exponent")
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
    gap=_owner_profile_policy_gap(route.get("owner_model_profile"), effective, registry)
    if gap=="top-requires-owner":
        raise ValueError("owner-profile-top-requires-owner")
    if gap:
        raise ValueError("owner_model_profile differs from the portable intensity policy")
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
            # `serial-attempt` is a per-(route_id, route_node) attempt budget,
            # so it stays true unchanged with three nodes. `max_dispatch_depth`
            # stays 1 because the frame legs are depth 1 as well.
            or route.get("registered_headless_policy") != "serial-attempt"
            or len(route.get("nodes",[])) != (3 if _recipe_has_frame(route_recipe) else 1)
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
        if node.get("inline_human_gates", []) != route_recipe["quick"].get("inline_human_gates", []):
            raise ValueError("quick-inline-human-gates-mismatch")
        if node.get("write_scope") != route_recipe["quick"]["write_scope"]:
            raise ValueError("quick-write-scope-mismatch")
        if (
            node.get("dispatch_depth") != 1
            or node.get("unit") != "_kernel/owner"
            or node.get("model_profile") != owner_profile
            or node.get("execution_surface") != "registered-headless"
            or node.get("registered_worker") is not True
            or node.get("fallback_hops")
            or sorted(node.get("depends_on") or []) !=
               (["frame","frame-alternative"] if _recipe_has_frame(route_recipe) else [])
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
    sidecar as closure truth (the material-route-guard bind gate) then refuses
    the new cycle, and the refusal is unrecoverable by recompiling, because
    recompiling is what lands here.

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
                            terminal_marker_digest=None):
    if not isinstance(existing, dict):
        return False
    if existing.get("route_id") != route_id or existing.get("route_hash") != route_hash:
        return False
    if terminal_commit_id is None and owner_attempt_id is None and producer_binding_digest is None:
        # Legacy callers retain the historical route/hash/optional-marker rule.
        return (terminal_marker_digest is None
                or existing.get("terminal_marker_digest") == terminal_marker_digest)
    if terminal_commit_id is not None and existing.get("terminal_commit_id") == terminal_commit_id:
        return True
    return (owner_attempt_id is not None and producer_binding_digest is not None
            and terminal_marker_digest is not None
            and existing.get("terminal_owner_attempt_id") == owner_attempt_id
            and existing.get("producer_binding_digest") == producer_binding_digest
            and existing.get("terminal_marker_digest") == terminal_marker_digest)

def close_route(route, route_file, commit=None, summary=None, publication=None,
                allow_unproven=True, jobs=None, expected_terminal_marker_digest=None,
                terminal_commit_id=None, expected_owner_attempt_id=None,
                expected_producer_binding_digest=None):
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
        existing=json.loads(target.read_text(encoding="utf-8"))
        if not _outcome_replay_matches(existing, route_id=route["route_id"], route_hash=route["route_hash"],
                terminal_commit_id=terminal_commit_id, owner_attempt_id=expected_owner_attempt_id,
                producer_binding_digest=expected_producer_binding_digest,
                terminal_marker_digest=expected_terminal_marker_digest):
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
                    terminal_marker_digest=expected_terminal_marker_digest):
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
    canonical=canonical_routes_dir(artifact_root)
    search_dirs=[canonical,root,root/"routes",root/"_routes",root/".routes"]
    by_route_id={}
    rows=[]
    for search_dir in search_dirs:
        if not search_dir.is_dir(): continue
        for path in sorted(search_dir.glob("*.json")):
            # SD-OPEN-54 (#15): typed sidecars beside a route record (`.outcome.json`,
            # `.gate-release.json` -- the workflow-supervisor gate ledger) are never
            # route candidates; the ledger used to be read as a route, fail
            # `route-malformed-missing-required-keys`, and turn every quiescence
            # observation of the root fail-closed (hearting rt-5d862a3d..., cairn W15d).
            if route_sidecar_kind(path) is not None: continue
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
        "evidence_sha256":evidence_digest(evidence),
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

def evidence_digest(evidence):
    """One sha256 over an artifact that may be a file OR a directory of files.

    A worker's artifact is legitimately either shape, and the envelope inspector
    accepts both, so the marker has to be able to name either. A directory is
    digested over its sorted root-relative paths and contents, so the value is
    stable across runs and changes when any member does. Symlinks are recorded by
    their target text and never followed: a marker must describe the tree it was
    given, not wherever that tree points today.

    Anything that cannot be attested raises rather than returning a digest. In
    particular a path that is neither a file nor a directory raises instead of
    yielding the empty-directory constant — otherwise "deleted before the gate"
    and "empty at completion" would verify as the same artifact. A non-regular
    member (FIFO, socket, device) is refused rather than read: reading a FIFO
    blocks forever, and a completion digest must not be able to wedge.
    """
    path=Path(evidence)
    if path.is_symlink():
        raise ValueError(f"evidence-symlink-not-attestable:{path}")
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        raise ValueError(f"evidence-not-a-file-or-directory:{path}")
    # The SAME rule the envelope inspector applies, imported rather than
    # restated: an empty or oversized directory must be refused at every door,
    # including the documented manual `complete --evidence` one.
    refusal=directory_artifact_reason(path)
    if refusal:
        raise ValueError(f"evidence-{refusal.removeprefix('artifact-')}:{path}")
    digest=hashlib.sha256(b"artifact-directory-v1\0")
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).parts):
        # `os.fsencode` and not `.encode("utf-8")`: a filename is bytes, and a
        # non-UTF-8 name arrives surrogate-escaped and would raise on encode.
        relative=os.fsencode(child.relative_to(path))
        if child.is_symlink():
            digest.update(b"L\0"+relative+b"\0"+os.fsencode(os.readlink(child)))
        elif child.is_dir():
            digest.update(b"D\0"+relative+b"\0")
        elif child.is_file():
            digest.update(b"F\0"+relative+b"\0")
            try:
                digest.update(hashlib.sha256(child.read_bytes()).digest())
            except OSError as exc:
                # Typed, never a traceback: this runs inside completion.
                raise ValueError(f"evidence-member-unreadable:{child}") from exc
        else:
            raise ValueError(f"evidence-member-not-regular:{child}")
    return digest.hexdigest()

def write_completion_marker(
    route, node, node_id, evidence, *,
    attempt_id=None, attempt_metadata=None, review_claim=None, jobs=None,
    owner_override=False, owner_chain=False,
):
    _migrate_completion_dir_forward(route["route_id"])
    directory=completion_dir(route["route_id"])
    canonical_path=directory/f"{node_id}.json"
    sha=evidence_digest(evidence)
    axes=_marker_attempt_axes(node, attempt_id, attempt_metadata)
    review_identity=resolve_review_identity(
        node, axes, attempt_metadata,
        claim=review_claim, jobs=jobs,
        route_id=route["route_id"], node_id=node_id,
        owner_override=owner_override, owner_chain=owner_chain,
    )
    replayed=_completion_marker_replay(route,node,node_id,evidence,axes,directory)
    if replayed is not None:
        # A replay is the same completion, so provenance is deliberately not in
        # marker identity -- but a caller that named a reviewer this time and
        # gets the old marker back deserves to be told the claim was dropped,
        # rather than reading exit 0 as "recorded".
        _notify_reviewer_claim_ignored(
            route,node_id,replayed,review_identity,review_claim,
        )
        return replayed
    sequence=_next_marker_sequence(directory,node_id)
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
        return {"passed": False, "reason": "completion-marker-identity-mismatch"}
    evidence = marker.get("evidence") or {}
    try:
        digest = evidence_digest(Path(evidence["path"]))
    except (OSError, KeyError, TypeError, ValueError):
        return {"passed": False, "reason": "completion-evidence-unreadable"}
    if digest != evidence.get("sha256"):
        return {"passed": False, "reason": "completion-evidence-hash-mismatch"}
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
        matches = []
        for line in Path(jobs).read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            meta = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
            if meta.get("route_id") == route["route_id"] and meta.get("route_node") == node_id:
                matches.append((fields, meta))
        if (not matches or matches[-1][0][1] != "done"
                or matches[-1][1].get("failure_class") != "pass"
                or matches[-1][1].get("attempt_id") != marker.get("attempt_id")
                or completion_attempt_readiness(route, node, marker, Path(jobs)).state != "ready"):
            return {"passed": False, "reason": "completion-attempt-not-current"}
        return {"passed": True, "reason": "completion-marker-verified", "current": True,
                "node_id": node_id, "attempt_id": marker["attempt_id"], "completion_gate": gate,
                "marker_digest": hashlib.sha256(marker_bytes).hexdigest(), "evidence_digest": digest,
                "evidence": evidence["path"], "attempt_readiness": "quiescent"}
    # Gate currentness is the pre-A2a marker identity/evidence contract.  The
    # jobs lock is used by mutation-time claim checks, not to reclassify an
    # already valid marker or require a registry attempt row here.  This also
    # preserves legacy inline markers whose attempt is intentionally absent.
    if jobs is None:
        return {"passed": True, "reason": "completion-marker-verified",
                "evidence": evidence.get("path")}
    return {"passed": True, "reason": "completion-marker-verified",
            "evidence": evidence.get("path"), "current": True,
            "attempt_readiness": "unchecked", "attempt_id": marker.get("attempt_id")}


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
):
    """Publish marker history, exact-attempt link, and canonical marker under one node lock."""

    _validate_auxiliary_arbiter(route, node, evidence)
    axes=_marker_attempt_axes(node,attempt_id,attempt_metadata)
    evidence_sha=evidence_digest(evidence)
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

    if marker is None:
        marker=write_completion_marker(
            route,node,node_id,evidence,
            attempt_id=attempt_id,
            attempt_metadata=attempt_metadata,
            review_claim=review_claim,
            jobs=jobs,
            owner_override=owner_override,
            owner_chain=owner_chain,
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
    return marker, row


# OPERATIONS §5.10 "Review verdict is a result, not a worker death" -- the
# owner-closure completion path for a review row that ended
# `completed-review-blocking`. Evidence-bound on purpose: a bare flag, a memo
# that names no attempt, a `dead-*` row, or an unexhausted round budget all keep
# the SD-94 fail-closed refusal. Every refusal is typed `owner-closure-*`.
_OWNER_CLOSURE_SUFFIX=".owner-closure.md"
_OWNER_CLOSURE_VERDICT="closed-by-owner"
_REGISTRY_UNSAFE_CHARS=(",","=","\t","\n","\r")
_LIVE_ROW_STATUSES={"open","running"}

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

def _review_round_rows(lines, route_id, node_id):
    """Registry rows of one route node that count as review rounds.

    Mirrors `dispatch-node.py prior_round_attempts`: same route/node,
    sub-sessions (`stage_authority=0`) excluded, legacy `route=` key honoured
    read-only. Returns every status; the caller separates live from terminated."""
    rows=[]
    for line in lines:
        fields=line.split("\t")
        if len(fields)!=6:
            continue
        metadata=parse_registry_metadata(fields[5])
        if (metadata.get("route_id") or metadata.get("route"))!=route_id:
            continue
        if metadata.get("route_node")!=node_id:
            continue
        if str(metadata.get("stage_authority","1"))=="0":
            continue
        rows.append((fields[1],metadata))
    return rows

def _owner_closure_eligibility(route, node, node_id, evidence, row_metadata, lines):
    """Admit `complete` on a `completed-review-blocking` row, or raise a typed refusal.

    Returns the closure facts the caller seals on the row. Checks, in order:
    the node is a review node and the row a review worker; no review round of
    the node is still open/running and the terminated rounds exhaust the
    budget; the node has no canonical marker from another attempt; the exact
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
    rounds=_review_round_rows(lines,route["route_id"],node_id)
    live=[metadata.get("attempt_id") or "-" for status,metadata in rounds if status in _LIVE_ROW_STATUSES]
    if live:
        # A live review worker may still write a second blocking artifact
        # nobody has read; the gate never closes over its head.
        refuse("round-still-open","attempt="+"|".join(live))
    terminated=[(status,metadata) for status,metadata in rounds if status not in _LIVE_ROW_STATUSES]
    try:
        max_round=REVIEW_ROUND_CAP.max_review_rounds(route["effective_intensity"])
    except ValueError:
        refuse("intensity-unknown",str(route.get("effective_intensity")))
    if len(terminated)<max_round:
        refuse("round-budget-not-exhausted",f"rounds={len(terminated)};max_round={max_round}")
    own=row_metadata.get("attempt_id")
    canonical=completion_dir(route["route_id"])/f"{node_id}.json"
    if canonical.is_file():
        try:
            existing=json.loads(canonical.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            refuse("node-already-complete","canonical-marker-unreadable")
        if existing.get("attempt_id")!=own:
            # SD-70: one node, one exact attempt. A second closure would
            # overwrite the canonical marker and leave two rows claiming it.
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
            if row_metadata.get("subsession_id") or str(row_metadata.get("stage_authority", "1")).lower() in {"0", "false"}:
                raise ValueError("subsession-has-no-stage-gate-authority")
            if ROUTE_IDENTITY.registered_node_identity(row_metadata, node) != (
                route["route_id"], route["route_hash"], node_id
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
            # OPERATIONS §5.10 owner-closure extension: a review row that ended
            # `completed-review-blocking` is marker-eligible only through the
            # evidence-bound owner-closure gate; it raises its own typed refusal.
            owner_closure=None
            sealed_pipe=None
            if already_closed and row_note==REVIEW_BLOCKING_NOTE:
                owner_closure=_owner_closure_eligibility(
                    route,node,node_id,evidence,row_metadata,lines,
                )
                # Seal the closure facts through the one sanitizing writer
                # every other terminal value uses (keys allowlisted in
                # ATTEMPT_TERMINAL_EVIDENCE_KEYS, ',' -> ';', immutability
                # checks) -- and compute it BEFORE the marker is published so a
                # refused seal publishes nothing.
                try:
                    sealed_pipe=_updated_attempt_metadata(
                        row_fields[5],
                        {
                            "gate_closure":"owner-closure",
                            "owner_closure":owner_closure["evidence"],
                            "review_artifact_b64":owner_closure["review_artifact_b64"],
                        },
                        terminal=True,
                    )
                except DispatchContractError as exc:
                    raise ValueError(f"owner-closure-seal-refused:{exc.reason}") from exc
                marker_eligible=True
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
                review_claim=review_claim,
                jobs=jobs_path,
                # SD-94 owner-closure: the row IS a review worker's, so the
                # ordinary rules would call this gate `independent` -- the exact
                # inversion of what happened, which is that the owner ruled over
                # a review that returned FAIL.
                owner_override=owner_closure is not None,
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
            or metadata.get("note") not in SUCCESS_NOTES
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
    return marker,{"status":"stage-gate-aggregated","sessions":len(manifest["sessions"])}

def _compose_artifact_root(cwd):
    script=ROOT/"utilities"/"artifact-root.sh"
    result=subprocess.run(["sh",str(script),str(cwd)],text=True,capture_output=True,check=False)
    root=(result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""
    if result.returncode!=0 or not root:
        raise ValueError("compose-artifact-root-unresolved:"+(result.stderr or "").strip()[:200])
    return root


def _emit_compiled_route(a,route,artifact_root,output=None):
    """Shared tail of compile/compose: runtime-root check, canonical write-once, owner binding, prints."""
    output=output if output is not None else getattr(a,"output",None)
    vbasis=route.get("validation_basis") or {}
    if vbasis.get("runtime_root_match") is False:
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
    print(f"route_file={output_path.resolve()}",file=sys.stderr)
    print(json.dumps(route,sort_keys=True))

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
    c=sub.add_parser("compile"); c.add_argument("--capability",required=True); c.add_argument("--capability-mode",default="default")
    c.add_argument("--slug",required=True)
    c.add_argument("--campaign-key",help="explicit work stream passed to the producer owner")
    c.add_argument("--parent-cycle",help="open or sealed predecessor cycle; causal link, not input approval")
    c.add_argument("--profile-demands", help="JSON file mapping node ids and __owner__ to full SD-88 demands")
    c.add_argument("--explicit-profiles", help="JSON file mapping demanded node ids to explicit profiles")
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
    cp=sub.add_parser("compose",help="preset-free work route: name the shape (and stage subgraph), defaults fill the rest")
    cp.add_argument("--slug",required=True)
    cp.add_argument("--campaign-key",help="explicit work stream passed to the producer owner")
    cp.add_argument("--parent-cycle",help="open or sealed predecessor cycle; causal link, not input approval")
    cp.add_argument("--profile-demands", help="JSON file mapping node ids and __owner__ to full SD-88 demands")
    cp.add_argument("--explicit-profiles", help="JSON file mapping demanded node ids to explicit profiles")
    cp.add_argument("--shape",choices=COMPOSE_SHAPES,default=None,help="direct (inline) | solo (one registered depth-1 owner) | staged (owner + your stage subgraph); default staged when --graph is given, else direct")
    cp.add_argument("--graph",default=None,help="comma list of the capability's stage ids in your order, optional :unit override, e.g. execute,test,report or execute:dev/refactor,test")
    cp.add_argument("--capability",default=COMPOSE_DEFAULT_CAPABILITY); cp.add_argument("--capability-mode",default=None)
    cp.add_argument("--intensity",default=None,help="default by shape: direct/quick/standard; staged accepts strong+")
    cp.add_argument("--cwd",default=None,help="default: current directory"); cp.add_argument("--artifact-root",default=None,help="default: utilities/artifact-root.sh for cwd")
    cp.add_argument("--signal",action="append",default=[])
    cp.add_argument("--spec-read",default="auto",help="auto: refuse when a spec/prd.md exists unless you name it here")
    cp.add_argument("--drift-verdict",default=None); cp.add_argument("--tracking",choices=sorted(TRACKING),default=None)
    cp.add_argument("--artifact-guard",default=None)
    cp.add_argument("--children",default=None,help="comma list of child harnesses to probe for staged/solo (default claude,codex)")
    cp.add_argument("--parent-harness",default="claude",choices=("claude","codex","opencode"))
    cp.add_argument("--jobs",default=None,help="registry for the readiness probe (default AGENT_DISPATCH_JOBS or the stable state root)")
    cp.add_argument("--dispatch-evidence",help="checked evidence JSON (skips the live probe)")
    cp.add_argument("--registered-headless-evidence",help="checked quick candidates JSON (skips the live probe)")
    cp.add_argument("--transport-evidence",default="compose-default")
    cp.add_argument("--explain",action="store_true",help="print the [경로] card and the sealed graph without writing the route")
    cp.add_argument("--output")
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
    cl=sub.add_parser("close"); cl.add_argument("--route",required=True)
    cl.add_argument("--commit",help="result commit; defaults to HEAD in the route cwd")
    cl.add_argument("--summary",help="one line naming what the route produced")
    cl.add_argument("--allow-unproven",action="store_true",
                     help="permit sealing terminal_gate_proven=false for a route closed before its terminal node completed")
    st=sub.add_parser("status"); st.add_argument("--artifact-root",required=True)
    st.add_argument("--open-only",action="store_true",help="list only routes with no recorded outcome")
    a=p.parse_args()
    if a.command not in {"verify", "node", "status", "close"}:
        dispatch_terminal_commit.require_current_cleanup("route-" + a.command)
    if a.command=="compose":
        shape=a.shape or ("staged" if a.graph else "direct")
        cwd=a.cwd or os.getcwd()
        artifact_root=a.artifact_root or _compose_artifact_root(cwd)
        route=compose_route(
            capability=a.capability,capability_mode=a.capability_mode,shape=shape,graph=a.graph,
            slug=a.slug,cwd=cwd,artifact_root=artifact_root,intensity=a.intensity,signals=a.signal,
            campaign_key=a.campaign_key,parent_cycle_id=a.parent_cycle,
            spec_read=a.spec_read,drift_verdict=a.drift_verdict,tracking=a.tracking,
            artifact_guard=a.artifact_guard,
            children=[c.strip() for c in a.children.split(",") if c.strip()] if a.children else None,
            parent_harness=a.parent_harness,
            dispatch_evidence=json.loads(Path(a.dispatch_evidence).read_text()) if a.dispatch_evidence else None,
            registered_headless_evidence=(json.loads(Path(a.registered_headless_evidence).read_text())
                                          if a.registered_headless_evidence else None),
            transport_evidence=a.transport_evidence,jobs=a.jobs,
            profile_demands=json.loads(Path(a.profile_demands).read_text()) if a.profile_demands else None,
            explicit_profiles=json.loads(Path(a.explicit_profiles).read_text()) if a.explicit_profiles else None,
        )
        print(compose_card(route),file=sys.stderr)
        if a.explain:
            print("route_file_written=0 explain=1",file=sys.stderr)
            print(json.dumps({"route_id":route["route_id"],"capability":route["capability"],
                              "effective_intensity":route["effective_intensity"],"shape":shape,
                              "composed":bool(route.get("composed")),
                              "nodes":[{"id":n["id"],"unit":n.get("unit"),"dispatch_depth":n.get("dispatch_depth"),
                                        "completion_gate":n.get("completion_gate"),"terminal":n.get("terminal") is True}
                                       for n in route["nodes"]],
                              "human_gates":route.get("human_gates"),"parallel_groups":route.get("parallel_groups"),
                              "tracked_gate_evidence":route.get("tracked_gate_evidence")},sort_keys=True))
            return 0
        _emit_compiled_route(a,route,artifact_root)
        return 0
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
        # The launch-sealed owner tuple names the exact bytes supplied on this
        # CLI invocation.  Keep that path attached to the verified source so a
        # multi-hop continuation does not pair the R0 file with R1 metadata.
        source["route_file"] = str(source_path)
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
        try:
            route=build_continuation_route(
                source,resume_from_node=a.resume_from_node,
                requested_boundary=a.requested_boundary,reason=a.reason,
                artifact_root=artifact,lineage_operation=a.lineage_operation,
                thread_id=a.thread_id,new_thread_id=a.new_thread_id,
                forked_from_id=a.forked_from_id,last_turn_id=a.last_turn_id,
                ephemeral=a.ephemeral,partial_group=partial,
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
                        f"actual={canonical(mismatch.get('actual',mismatch)).decode()}"
                        " | " + runtime_root_hint(route),
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
            outcome,created=close_route(route,a.route,a.commit,a.summary,allow_unproven=a.allow_unproven)
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
                if a.subsession_manifest:
                    if review_claim:
                        raise ValueError("reviewer-claim-unsupported-on-subsession-gate")
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
                        review_claim=review_claim,
                    )
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
