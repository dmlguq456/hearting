"""Capability route: shared constants, path-loaded helpers and route files.

`ROOT`, the path-loaded `capability_topology` / `ui_text` / `dispatch-defaults`
modules, the route constants, route-file and completion-directory paths,
write-once and atomic writes, the auxiliary-arbiter merge groups, and the
review-round records. It imports no other capability route module.

Split out of `capability-route.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import route_format
import route_identity as ROUTE_IDENTITY
import route_lineage as ROUTE_LINEAGE
import review_round_cap as REVIEW_ROUND_CAP
import route_authority as ROUTE_AUTHORITY
from dispatch_contract import (
    attempt_row_withdrawn_before_launch, attempt_row_terminal_never_started,
    parse_registry_metadata, resolve_agent_home, resolve_dispatch_state_root,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capability_topology", ROOT/"tools/capability_topology.py")
TOPO = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(TOPO)
UI_SPEC = importlib.util.spec_from_file_location("ui_text", ROOT/"tools/i18n/ui_text.py")
UI_TEXT = importlib.util.module_from_spec(UI_SPEC); UI_SPEC.loader.exec_module(UI_TEXT)
DEFAULTS_SPEC = importlib.util.spec_from_file_location("dispatch_defaults", ROOT/"utilities/dispatch-defaults.py")
DEFAULTS = importlib.util.module_from_spec(DEFAULTS_SPEC); DEFAULTS_SPEC.loader.exec_module(DEFAULTS)
VALID_AFFINITY = DEFAULTS.AFFINITY_VALUES | {"unspecified"}
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
DISPATCH_EVIDENCE_SCOPE_VERSION = route_format.DISPATCH_EVIDENCE_SCOPE_VERSION
BROKER_FIELDS = {"broker_root", "broker_instance"}  # historical v1
BROKER_FIELDS_V2 = {"broker_root"}                   # historical v2
DISPATCH_CONTRACT_VERSION = route_format.DISPATCH_CONTRACT_VERSION
FALLBACK_ORDER = ["same-harness-headless", "cross-harness-headless", "native-subagent", "inline"]
ROUTE_SCHEMA_VERSION = route_format.ROUTE_SCHEMA_VERSION
VALIDATION_BASIS_VERSION = route_format.VALIDATION_BASIS_VERSION
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
SELECTION_PIN_TARGETS = ROUTE_AUTHORITY.PIN_TARGETS
SELECTION_PIN_CONTRACT_VERSION = 1
# The harness is split at the first ":" and the effort at the last "@", so a
# model id may itself contain ":" (a provider tag) but never "@", whitespace,
# "|" or ",".  Deliberately narrower than model_profile.SAFE_VALUE.
SELECTION_PIN_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$")
SELECTION_PIN_EFFORT = re.compile(r"^[a-z]+$")


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


AUXILIARY_ARBITER_OWNER_MERGE = "owner-merge"
AUXILIARY_ARBITER_NODE = "node"


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


def _safe_group_id(group_id):
    return "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in str(group_id)
    )


def arbitration_path(route_id, group_id):
    return completion_dir(route_id)/f"{_safe_group_id(group_id)}.arbitration.json"

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
        if attempt_row_withdrawn_before_launch(fields):
            continue
        rows.append((fields,metadata))
    return REVIEW_ROUND_CAP.logical_round_records(rows,jobs=jobs)

def _review_round_rows(lines, route_id, node_id, *, jobs=None):
    return [(fields[1], meta) for fields, meta in review_round_records(lines, {route_id}, node_id,jobs=jobs)
            if not attempt_row_terminal_never_started(fields)]


def _git(cwd, *args):
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


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
