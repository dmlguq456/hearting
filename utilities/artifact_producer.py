#!/usr/bin/env python3
"""W7C artifact write-cutover: producer begin/finalize lifecycle.

Correction to the W7 relocation: new cycle output is written in place under
`<artifact-root>/campaigns/<campaign-locator>/<cycle-locator>/artifacts/` and the
typed IDs are issued by `begin` *before* the first write (D-2, D-4).  The
step-1 modules stay the only lineage authorities: `artifact_identity` issues
IDs, `artifact_manifest` validates the closed D-6 schema, `artifact_index`
guards uniqueness, and `artifact_admission` owns the root identity, the global
mutex, and the derived index.

Layout (D-2, closed):

    campaigns/<campaign-locator>/campaign.json              mutable campaign record
    campaigns/<campaign-locator>/<cycle-locator>/.cycle.json stable-ID locator binding + started_on
    campaigns/<campaign-locator>/<cycle-locator>/artifacts  producer output (open)
    campaigns/<campaign-locator>/<cycle-locator>/manifest.json finalize commit point
    shared/<spec|analysis|research>/<ref>/reference.json
    shared/<kind>/<ref>/revisions/<rrev>/...          immutable revision
    .runtime/artifact-producer/v1/cutover.json        cutover state (approval-gated)
    .runtime/artifact-producer/v1/cycles/<cyc>.json   cycle record open|sealed|abandoned
    .runtime/artifact-producer/v1/journal/<cyc>.json  finalize crash journal
    .runtime/artifact-producer/v1/shared-journal/<rrev>.json
    .runtime/artifact-producer/v1/campaigns/<camp>.json     history lines waiting for the recorder; what is kept of a deleted campaign

A cycle's place, mark and existence are changed by `cycle-move`, `cycle-mark` and `delete` (§45 D-126);
a folder moved, renamed or removed by hand is found by the next listing, `begin` or campaign close
(`reconcile_root`).  None of them asks for approval or a confirmation.

Two cutover states.  While `cutover.json` is absent (`inactive`) the legacy
top-level buckets remain writable (compatibility window) and `begin` reports
`layout=legacy`; once the approval package activates the root, `begin` issues
a cycle and every new write outside an open cycle's `artifacts/` is denied.
Shared revisions are immutable in both states and are only created by
`admit-shared` from a sealed cycle.  Research is admitted to `shared/` only
with an explicit promotion (D-3).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402
import artifact_cycle_titles  # noqa: E402
import artifact_identity  # noqa: E402
import artifact_index  # noqa: E402
import artifact_lifecycle  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_manifest  # noqa: E402
import artifact_campaign  # noqa: E402
import route_identity  # noqa: E402
import route_lineage  # noqa: E402
import directory_record_index  # noqa: E402
import dispatch_contract  # noqa: E402
import dispatch_lock_order  # noqa: E402
import dispatch_terminal_commit  # noqa: E402
from dispatch_contract import (  # noqa: E402
    _PROCESS_IDENTITY_METADATA_KEYS,
    REVIEW_GOVERNED_LEASE_KIND,
    REVIEW_GOVERNED_LEASE_NONCE_RE,
    encode_review_output_locator,
    review_governed_lease_is_held,
    review_lease_record_digest,
    process_start_ticks,
    resolve_dispatch_state_root,
    resolve_agent_home,
    review_output_binding_digest,
    review_output_write_authorized,
    validate_review_output_binding,
    review_holder_disposition,
)
from dispatch_lifecycle import (
    FiniteWatchdogBudget,
    begin_finite_watchdog,
    remaining_watchdog_seconds,
)

PRODUCER_REL = ".runtime/artifact-producer/v1"
CONTRACT = "artifact-producer/v1"
REVISION_RECORD_NAME = "revision.json"
ALGORITHM_VERSION = "w7c-producer/v1"
OK, BLOCKED, USAGE = 0, 65, 64

# D-86: the one-line hint attached to a legacy-top-level-write-denied result.
# The `reason` token itself (compared verbatim by fleet_cutover_gate's
# negative probe) never changes; this hint rides in a separate field/detail.
LEGACY_WRITE_HINT = ("run `artifact_producer.py begin --route <route file>` first; if begin already ran, "
                     "export its --env-file output (AGENT_ARTIFACT_*) into this shell, then retry")

# D-81: campaign.json `related[]` row kinds (producer-internal API only).
RELATED_KINDS = ("related", "precedes", "supersedes")

# Attached to a first-publication `finalize --allow-open-route` response whose
# cycle sealed `state: active` (D-6): closing the route later, proven or not,
# cannot retroactively make this cycle `completed`.
PROVISIONAL_SEAL_WARNING = (
    "sealed provisionally active: closing the route later, with or without proof, cannot make "
    "this cycle completed; campaign closure lists it as sealed-unproven. Order for new cycles: "
    "complete -> close -> finalize."
)

COMPAT_OVERRIDE_NAME = "compat-override.json"
INACTIVE_FALLBACK_ENV = "AGENT_ARTIFACT_INACTIVE_FALLBACK"
ROOT_CLASSES = ("active", "inactive-with-legacy", "inactive-empty", "malformed")
ACTIVATION_KINDS = ("approval", "bootstrap-empty-root")
RUNTIME_OWNED_EXACT = ("_scratch",)          # `.`-prefix is a separate predicate
COMPAT_OVERRIDE_FIELDS = ("schema_version", "contract", "canonical_root",
                          "reason", "issuer", "created_at", "expires_at")
WAIVER_FIELDS = ("reason", "issuer", "created_at", "expires_at")

# Record states a route can bind again once the cycle is closed (§45 D-123): the
# cycle still has its folder and manifest.  Zero-row closes (`no-lineage`,
# `abandoned`) removed the folder and `dropped` never had one.
CLOSED_BINDABLE_STATES = frozenset({"sealed"})

# SD-117 §13.34.5-(2): a cycle's abandonment sealing decision must always
# name why -- a closed enum, disjoint from review verdict vocabulary
# (PASS/FAIL/BLOCKED, allow/deny) so the two can never be confused (E47-7).
ABANDON_REASONS = frozenset({
    "operator-decision",
    "route-unrecoverable",
    "lease-expired-no-publisher",
    "operator-override-live-review",
})
REVIEW_LEASE_REL = "review-leases"

INTENSITIES = ("direct", "quick", "standard", "strong", "thorough", "adversarial")
ENTRY_CAPABILITIES = (
    "analyze-project", "analyze-user", "audit", "autopilot-apply", "autopilot-code",
    "autopilot-design", "autopilot-draft", "autopilot-lab", "autopilot-refine",
    "autopilot-research", "autopilot-ship", "autopilot-spec",
)
STAGE_CAPABILITIES = (
    "code-plan", "code-execute", "code-refine", "code-report", "code-test",
    "design-init", "design-refs", "design-tokens", "design-components",
    "design-review", "design-handoff", "draft-strategy", "draft-refine",
)
# Compiler-internal capabilities: no Skill and no person invokes them, but the route
# compiler seals routes for them and a producer cycle must be issuable for that route.
INTERNAL_CAPABILITIES = ("route-frame",)
CANONICAL_ROOTS = ("campaigns", "shared")
# Legacy capability buckets (CORE.md §3 C-DUR) plus the undeclared containers.
LEGACY_TOP_LEVEL = (
    "analysis_project", "research", "spec", "plans", "documents", "experiments",
    "designs", "_internal", "reviews", "shards", "routes", "_routes", "notes",
    "proposals", "spec-research-alternative", "research-alternative", "release-config",
    "evidence", "dev_logs", "test_logs", "user_profile",
)
SHARED_KINDS = {
    "spec": "shared-spec",
    "analysis": "cumulative-analysis",
    "research": "shared-research",
}
BUCKET_TYPES = {
    "plans": "plan", "documents": "document", "designs": "design", "spec": "spec",
    "research": "research", "experiments": "experiment", "analysis_project": "analysis",
    "analysis": "analysis", "reviews": "review", "release-config": "release-config",
    "apply-log": "apply-log", "user_profile": "profile",
}
CAPABILITY_BUCKETS = {
    "analyze-project": "analysis_project", "analyze-user": "user_profile", "audit": "reviews",
    "autopilot-apply": "apply-log", "autopilot-code": "plans", "autopilot-design": "designs",
    "autopilot-draft": "documents", "autopilot-lab": "experiments", "autopilot-refine": "documents",
    "autopilot-research": "research", "autopilot-ship": "release-config", "autopilot-spec": "spec",
}


def default_bucket(capability: str) -> str:
    return CAPABILITY_BUCKETS.get(capability, capability if capability in BUCKET_TYPES else "analysis")


MEDIA_TYPES = {
    ".md": "text/markdown", ".json": "application/json", ".yaml": "application/yaml",
    ".yml": "application/yaml", ".txt": "text/plain", ".csv": "text/csv",
    ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf",
    ".py": "text/x-python", ".sh": "text/x-shellscript", ".log": "text/plain",
    ".jsonl": "application/x-ndjson", ".toml": "application/toml",
}
PRIMARY_CANDIDATES = (
    "final_report.md", "report.md", "report.html", "prd.md", "plan.md", "handoff.md", "verdict.json",
)
# CORE §3 top-level `C-INT` names that are not a cycle bucket: support material is
# kept in the manifest but is not auto-nominated as a cycle's primary artifact.
SUPPORT_SEGMENTS = frozenset({"_internal", "shards"})
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProducerError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# small filesystem helpers
# ---------------------------------------------------------------------------


def _rfc3339(now: Optional[float] = None) -> str:
    t = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + "Z"


def _rfc3339_precise(now: Optional[float] = None) -> str:
    t = time.time() if now is None else float(now)
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive(path: Path, data: bytes, mode: int = 0o644) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _ensure_dir(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ProducerError("path-not-directory", str(path))
        return
    path.mkdir(parents=True, exist_ok=True)


def _walk_files(top: Path) -> List[Path]:
    out: List[Path] = []
    for current, dirs, files in os.walk(str(top), followlinks=False):
        dirs.sort()
        for name in sorted(files):
            out.append(Path(current) / name)
        for name in list(dirs):
            if os.path.islink(os.path.join(current, name)):
                out.append(Path(current) / name)
                dirs.remove(name)
    return out


def _copy_tree_files(source: Path, target: Path) -> Tuple[List[Tuple[str, str, int]], List[str]]:
    """Copy regular files only. Returns (rows, violations)."""
    rows: List[Tuple[str, str, int]] = []
    violations: List[str] = []
    if source.is_file():
        entries = [source]
        base = source.parent
    else:
        entries = _walk_files(source)
        base = source
    for entry in entries:
        rel = entry.relative_to(base).as_posix()
        if os.path.islink(str(entry)):
            violations.append(f"symlink-forbidden:{rel}")
            continue
        if not entry.is_file():
            violations.append(f"non-regular-file:{rel}")
            continue
        data = entry.read_bytes()
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive(dst, data)
        rows.append((rel, _digest(data), len(data)))
    for current, _dirs, _files in os.walk(str(target)):
        _fsync_dir(Path(current))
    return rows, violations


# ---------------------------------------------------------------------------
# state paths
# ---------------------------------------------------------------------------


def producer_dir(root: Path) -> Path:
    return Path(root) / PRODUCER_REL


def cutover_path(root: Path) -> Path:
    return producer_dir(root) / "cutover.json"


def compat_override_path(root: Path) -> Path:
    return producer_dir(root) / COMPAT_OVERRIDE_NAME


def cycle_record_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / "cycles" / f"{cycle_id}.json"


def journal_path(root: Path, cycle_id: str) -> Path:
    return producer_dir(root) / "journal" / f"{cycle_id}.json"


def shared_journal_path(root: Path, revision_id: str) -> Path:
    return producer_dir(root) / "shared-journal" / f"{revision_id}.json"


def campaign_dir(root: Path, campaign_id: str, record: Optional[Mapping[str, Any]] = None) -> Path:
    """Resolve an existing campaign by record identity, with old-ID fallback.

    Creation sites must pass a record containing its persisted ``locator``;
    this function never derives a readable path from the stable ID.
    """
    root = Path(root)

    def candidates() -> Iterator[Path]:
        if record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
            try:
                campaigns = artifact_locator.safe_child(root, root, "campaigns")
                yield artifact_locator.safe_child(root, campaigns, record["locator"])
            except artifact_locator.LocatorError:
                return

    found = artifact_locator.locate(root, campaign_id, candidates=candidates)
    if found is not None and (found / "campaign.json").is_file():
        return found
    try:
        campaigns = artifact_locator.safe_child(root, root, "campaigns")
        if record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
            return artifact_locator.safe_child(root, campaigns, record["locator"])
        return artifact_locator.safe_child(root, campaigns, campaign_id)
    except artifact_locator.LocatorError as exc:
        raise ProducerError("record-locator-invalid", exc.detail or exc.code) from exc


def cycle_dir(root: Path, campaign_id: str, cycle_id: str,
              record: Optional[Mapping[str, Any]] = None) -> Path:
    """Resolve an existing cycle, accepting readable, hybrid, and old layouts."""
    root = Path(root)
    if record is None:
        record = read_cycle_record(root, cycle_id)
    if record and (record.get("relocation") or {}).get("artifact_root"):
        raise ProducerError("cycle-relocated", str(record["relocation"]))
    campaign_record = read_campaign(root, campaign_id)

    def owner_campaign_candidates() -> Iterator[Path]:
        if campaign_record is not None and campaign_record.get("locator"):
            try:
                campaigns = artifact_locator.safe_child(root, root, "campaigns")
                yield artifact_locator.safe_child(root, campaigns, campaign_record["locator"])
            except artifact_locator.LocatorError:
                return

    def cycle_candidates() -> Iterator[Path]:
        if record is None or record.get("campaign_id") != campaign_id:
            return
        try:
            parent = campaign_dir(root, campaign_id, campaign_record)
        except ProducerError:
            return
        if record.get("locator"):
            try:
                yield artifact_locator.safe_child(root, parent, record["locator"])
            except artifact_locator.LocatorError:
                pass
        try:
            cycles = artifact_locator.safe_child(root, parent, "cycles")
            yield artifact_locator.safe_child(root, cycles, cycle_id)
        except artifact_locator.LocatorError:
            pass

    found = artifact_locator.locate(
        root, cycle_id, candidates=cycle_candidates,
        owner_campaign_id=campaign_id, owner_campaign_candidates=owner_campaign_candidates,
    )
    if found is not None:
        return found
    parent = campaign_dir(root, campaign_id, campaign_record)
    try:
        if record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
            return artifact_locator.safe_child(root, parent, record["locator"])
        cycles = artifact_locator.safe_child(root, parent, "cycles")
        return artifact_locator.safe_child(root, cycles, cycle_id)
    except artifact_locator.LocatorError as exc:
        raise ProducerError("record-locator-invalid", exc.detail or exc.code) from exc


def read_cutover(root: Path) -> Dict[str, Any]:
    value = _read_json(cutover_path(root))
    if value is None:
        return {"state": "inactive"}
    return value


def is_active(root: Path) -> bool:
    return read_cutover(root).get("state") == "active"


RELAYOUT_STATE_NAME = "relayout.json"


def relayout_state_path(root: Path) -> Path:
    return producer_dir(root) / RELAYOUT_STATE_NAME


def read_relayout_state(root: Path) -> Dict[str, Any]:
    """W7I Cycle B per-root state. Absent means the D-91 transition window is
    still open (slugless pre-W7I routes are named by derivation, not refused)."""
    value = _read_json(relayout_state_path(root))
    if value is None:
        return {"state": "pending", "transition_window": "open"}
    return value


def transition_window_closed(root: Path) -> bool:
    return read_relayout_state(root).get("transition_window") == "closed"


def _is_runtime_owned_top_level(name: str) -> bool:
    return name.startswith(".") or name in RUNTIME_OWNED_EXACT


def _legacy_content_names(root: Path, *, exhaustive: bool = False) -> List[str]:
    """Top-level names holding non-runtime content.

    Mirrors `_walk_files`'s symlink policy: never follow a symlink, but count
    it as content. Stops at the first hit unless `exhaustive` is set, so the
    hot-path predicate never walks a large legacy tree.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    found: List[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if _is_runtime_owned_top_level(name):
            continue
        has_content = False
        if entry.is_symlink():
            has_content = True
        elif entry.is_file():
            has_content = True
        elif entry.is_dir():
            for current, dirs, files in os.walk(str(entry), followlinks=False):
                if files:
                    has_content = True
                    break
                linked = [d for d in dirs if os.path.islink(os.path.join(current, d))]
                if linked:
                    has_content = True
                    break
        if has_content:
            found.append(name)
            if not exhaustive:
                return found
    return found


def classify_root(root: Path, *, collect_legacy_top_level: bool = False) -> Dict[str, Any]:
    """D-72 root classification. Never creates or modifies anything.

    Returns {"state": active|inactive-with-legacy|inactive-empty|malformed,
             "root": str, "cutover_state": str|None, "activation_kind": str|None,
             "identity": {"repository_id":…, "artifact_root_id":…}|None,
             "reason": str|None,
             "legacy_top_level": List[str], "legacy_top_level_complete": bool}
    """
    root = Path(root)
    result: Dict[str, Any] = {
        "state": None, "root": str(root), "cutover_state": None, "activation_kind": None,
        "identity": None, "reason": None,
        "legacy_top_level": [], "legacy_top_level_complete": collect_legacy_top_level,
    }
    path = cutover_path(root)
    cutover: Optional[Dict[str, Any]] = None
    if path.exists():
        cutover = _read_json(path)
        if cutover is None:
            result["state"] = "malformed"
            result["reason"] = "cutover-record-unreadable"
            return result
    if cutover is not None and cutover.get("state") not in ("active", "inactive"):
        result["state"] = "malformed"
        result["reason"] = "cutover-schema-unknown"
        return result
    try:
        identity = artifact_lifecycle.read_root_identity(root)
    except artifact_lifecycle.LifecycleError:
        result["state"] = "malformed"
        result["reason"] = "root-identity-invalid"
        return result
    if cutover is not None and cutover.get("state") == "active":
        cutover_root_id = (cutover.get("identity") or {}).get("artifact_root_id")
        if identity is None or cutover_root_id != identity.artifact_root_id:
            result["state"] = "malformed"
            result["reason"] = "identity-conflict"
            return result
        result["state"] = "active"
        result["cutover_state"] = "active"
        result["activation_kind"] = cutover.get("activation_kind", "approval")
        result["identity"] = {"repository_id": identity.repository_id,
                              "artifact_root_id": identity.artifact_root_id}
        return result
    if not root.exists():
        result["state"] = "inactive-empty"
        return result
    try:
        names = _legacy_content_names(root, exhaustive=collect_legacy_top_level)
    except OSError:
        result["state"] = "malformed"
        result["reason"] = "root-unreadable"
        return result
    result["legacy_top_level"] = names
    result["state"] = "inactive-with-legacy" if names else "inactive-empty"
    return result


def validate_time_bounded_grant(payload: Any, *, canonical_root: Path,
                                required_fields: Sequence[str],
                                now: Optional[float] = None) -> Dict[str, Any]:
    """Shared fail-closed rule for D-74 compat overrides and D-75 waivers.

    Returns {"status": "accepted"|"rejected",
             "reason": None|"malformed"|"expired"|"foreign-root",
             "expires_at": str|None}
    """
    if not isinstance(payload, dict):
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    for field in required_fields:
        value = payload.get(field)
        if value is None or value == "":
            return {"status": "rejected", "reason": "malformed", "expires_at": None}
    if "schema_version" in required_fields and payload.get("schema_version") != 1:
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    if "contract" in required_fields and payload.get("contract") != CONTRACT:
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    expires_at = payload.get("expires_at")
    try:
        expires_ts = _rfc3339_to_epoch(str(expires_at))
    except (ValueError, OverflowError):
        return {"status": "rejected", "reason": "malformed", "expires_at": None}
    if "canonical_root" in payload:
        if os.path.realpath(str(payload["canonical_root"])) != os.path.realpath(str(canonical_root)):
            return {"status": "rejected", "reason": "foreign-root", "expires_at": expires_at}
    when = time.time() if now is None else now
    if expires_ts <= when:
        return {"status": "rejected", "reason": "expired", "expires_at": expires_at}
    return {"status": "accepted", "reason": None, "expires_at": expires_at}


def read_compat_override(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    """{"status": "absent"|"accepted"|"rejected",
        "reason": None|"override-malformed"|"override-expired"|"override-foreign-root",
        "path": str, "expires_at": str|None}"""
    path = compat_override_path(root)
    if not path.exists():
        return {"status": "absent", "reason": None, "path": str(path), "expires_at": None}
    payload = _read_json(path)
    verdict = validate_time_bounded_grant(
        payload, canonical_root=root, required_fields=COMPAT_OVERRIDE_FIELDS, now=now)
    reason = f"override-{verdict['reason']}" if verdict["reason"] else None
    return {"status": verdict["status"], "reason": reason, "path": str(path),
            "expires_at": verdict["expires_at"]}


def inactive_fallback_level() -> str:
    """`warn` unless AGENT_ARTIFACT_INACTIVE_FALLBACK is exactly `deny`;
    any other non-empty value is fail-closed to `deny`."""
    value = os.environ.get(INACTIVE_FALLBACK_ENV, "")
    if value in ("", "warn"):
        return "warn"
    return "deny"


def legacy_fallback_state(root: Path, *, now: Optional[float] = None,
                          classification: Optional[Mapping[str, Any]] = None
                          ) -> Optional[Dict[str, Any]]:
    """D-74 typed block; None unless the root is `inactive-with-legacy`."""
    klass = classification if classification is not None else classify_root(root)
    if klass["state"] != "inactive-with-legacy":
        return None
    return {
        "level": inactive_fallback_level(),
        "reason": "cutover-inactive-legacy-root",
        "override": read_compat_override(root, now=now),
    }


def _fallback_blocks(fallback: Optional[Mapping[str, Any]]) -> bool:
    return bool(fallback) and fallback["level"] == "deny" \
        and fallback["override"]["status"] != "accepted"


def read_cycle_record(root: Path, cycle_id: str) -> Optional[Dict[str, Any]]:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        return None
    record = _read_json(cycle_record_path(root, cycle_id))
    if record is not None:
        return record
    # A producer record is a cache. Read the already admitted revision if the
    # cache was removed; this recovers a past result, never a new PASS.
    index = _read_json(artifact_admission._index_path(root)) or {}
    row = (index.get("manifests") or {}).get(cycle_id) or {}
    cycle_row = (index.get("cycles") or {}).get(cycle_id) or {}
    digest = row.get("manifest_digest")
    document = artifact_lifecycle.find_manifest_snapshot(root, cycle_id, manifest_digest=digest) if digest else None
    if document is None:
        return None
    cycle = document.get("cycle") or {}
    routes = document.get("routes") or []
    if not routes or cycle.get("cycle_id") != cycle_id:
        return None
    campaign_path = artifact_locator.locate(root, cycle.get("campaign_id"))
    campaign = _read_json(campaign_path / "campaign.json") if campaign_path is not None else None
    if campaign is not None and cycle_id not in (campaign.get("cycles") or []):
        return None  # an explicit removal from membership is not a missing cache
    source = str((document.get("producer") or {}).get("source_revision", "")).split("/")
    if len(source) < 2:
        return None
    route = routes[0]
    where = Path(str(cycle_row.get("cycle_path") or ""))
    return {"schema_version": 1, "contract": CONTRACT, "cycle_id": cycle_id,
            "campaign_id": cycle["campaign_id"], "producer_id": document["producer"]["producer_id"],
            "capability": source[0], "route_capability": source[0], "intensity": source[1],
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "route_file": str(route_lineage.canonical_route_path(root, route["route_id"])),
            "node_id": None, "state": "sealed", "cycle_state": cycle["state"],
            "parent_cycle_id": cycle.get("parent_cycle_id"), "started_on": cycle.get("started_on"),
            "sealed_on": cycle.get("started_on"), "manifest_digest": digest,
            "locator": where.name, "slug": where.name, "title": where.name, "locator_suffix": ""}


def _record_content_digest(record: Mapping[str, Any]) -> str:
    return _digest(_canonical({key: value for key, value in record.items()
                               if not key.startswith("control_") and key != "history_pending"}))


def _write_cycle_record(root: Path, record: Dict[str, Any], *, exclusive: bool) -> None:
    path = cycle_record_path(root, record["cycle_id"])
    _ensure_dir(path.parent)
    data = _json_bytes(dict(record, control_record_digest=_record_content_digest(record)))
    if exclusive:
        _write_exclusive(path, data, 0o600)
    else:
        _write_atomic(path, data, 0o600)

    directory_record_index.published(path, record, kind="cycle-routes",
                                     classify=directory_record_index.cycle_keys)


def _write_cycle_binding(directory: Path, campaign_id: str, cycle_id: str,
                         *, started_on: Optional[str] = None) -> None:
    marker = Path(directory) / artifact_locator.CYCLE_BINDING
    data = artifact_locator.cycle_binding_bytes(campaign_id, cycle_id, started_on=started_on)
    if marker.is_file() and not marker.is_symlink():
        if marker.read_bytes() != data:
            raise ProducerError("cycle-binding-conflict", str(marker))
        return
    _write_exclusive(marker, data)


def list_cycle_records(root: Path, *, route_ids=None) -> List[Dict[str, Any]]:
    directory = producer_dir(root) / "cycles"
    rows: List[Dict[str, Any]] = []
    if route_ids is not None:
        route_ids = set(route_ids)
        rows = directory_record_index.select(directory, route_ids, kind="cycle-routes",
                                              classify=directory_record_index.cycle_keys, read_json=_read_json)
    else:
        for entry in sorted(directory.iterdir(), key=lambda p: p.name) if directory.is_dir() else []:
            if entry.suffix == ".json" and entry.name != ".cycle-routes-index.json":
                value = _read_json(entry)
                if value is not None:
                    rows.append(value)
    index = _read_json(artifact_admission._index_path(root)) or {}
    if route_ids is None:
        known = {row.get("cycle_id") for row in rows}
        candidates = set(index.get("cycles") or {}) - known
    else:
        # Admitted manifests recover removed producer records. Their existing
        # route projection narrows recovery too; unindexed legacy rows retain
        # the original read-and-filter fallback.
        projected, matching = set(), set()
        projection = index.get("routes") or {}
        valid_projection = isinstance(projection, dict)
        for bucket in projection.values() if valid_projection else ():
            if not isinstance(bucket, dict):
                valid_projection = False
                break
            for route_id, item in bucket.items():
                if (not isinstance(route_id, str) or not isinstance(item, dict)
                        or not isinstance(item.get("cycle_id"), str)):
                    valid_projection = False
                    break
                projected.add(item["cycle_id"])
                if route_id in route_ids:
                    matching.add(item["cycle_id"])
            if not valid_projection:
                break
        if not valid_projection:
            # A damaged optional projection cannot suppress legacy recovery.
            projected, matching = set(), set()
        known = {row.get("cycle_id") for row in rows}
        candidates = (matching | (set(index.get("cycles") or {}) - projected)) - known - {None}
    for cycle_id in sorted(candidates):
        record = read_cycle_record(root, cycle_id)
        if record is not None and (route_ids is None or record.get("route_id") in route_ids):
            rows.append(record)
    return rows


# ---------------------------------------------------------------------------
# D-120: route lineage binding -- "route W may write cycle C" is judged fresh
# from the sealed route file and the cycle record's begin values every time,
# never from the producer's own audit copy (`route_bindings[]`, which
# `bind_cycle_route` writes and only readers consume). SD-155's
# `verified_route_lineage` is the one hash-verified walk this admission and
# `route_cycle_for` both stand on.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    allow: bool
    reason: Optional[str]
    detail: str
    path: List[Dict[str, Any]]
    next_action: Optional[str]


_D120_NEXT_ACTION = {
    "route-lineage-unverified": ("restore the sealed route file, or start a new route; a continuation that "
                                  "changed capability needs a new child cycle (--parent-cycle)"),
    "route-hash-drift": "restore the sealed route file",
    "cycle-route-binding-mismatch": "start a new cycle (--parent-cycle to keep it linked)",
    "cycle-route-binding-mismatch:material-input": "start a new child cycle (--parent-cycle)",
    "cycle-route-binding-mismatch:lineage-fork": "continue this branch in a new child cycle (--parent-cycle)",
    "cycle-route-binding-mismatch:superseded-route": "close and seal from the end route instead",
}


def _d120_next_action(reason: str) -> str:
    return _D120_NEXT_ACTION.get(reason, _D120_NEXT_ACTION.get(reason.split(":", 1)[0], "retry"))


def _routes_dir(root: Path) -> Path:
    return route_lineage.canonical_route_path(root, "x").parent


def _qualified_continuation(entry_stem: str, candidate: Any, route_id: Optional[str] = None,
                            route_hash_value: Optional[str] = None) -> bool:
    """Whether a route file is a sealed continuation edge (of ``route_id`` when given).

    The one qualification rule shared by `_lineage_children` and
    `closed_lineage_handover`: not its own source, contract version 1, source id
    and hash match, and the route hash recomputes -- a tampered sibling proves
    nothing and is not part of any lineage.
    """
    if not isinstance(candidate, dict) or candidate.get("continuation_contract_version") != 1:
        return False
    source_id = candidate.get("source_route_id")
    if entry_stem == source_id or (route_id is not None and source_id != route_id):
        return False
    if route_hash_value is not None and candidate.get("source_route_hash") != route_hash_value:
        return False
    return bool(directory_record_index.route_keys(entry_stem + ".json", candidate))


class _RouteEdges:
    """What one read of a routes directory found: its sealed continuation edges by source.

    `files` keeps, per file name, the (size, mtime_ns) it was parsed at and the edge it held, so a
    rescan parses only what is new or changed.  `children` maps (source route id, source route
    hash) to the edges naming it, in file-name order.  `trusted` says the whole answer may be
    served again without listing the directory: its signature is the one before and after the read,
    old enough that a write in the same clock tick cannot hide behind it, and every file parsed."""
    __slots__ = ("signature", "files", "children", "trusted")

    def __init__(self, signature, files, children, trusted) -> None:
        self.signature, self.files, self.children, self.trusted = signature, files, children, trusted


_ROUTE_EDGES: Dict[str, _RouteEdges] = {}
_ROUTE_EDGES_SETTLE_NS = 2_000_000_000


def _routes_dir_signature(directory: Path) -> Optional[Tuple[int, int, int]]:
    try:
        info = os.stat(str(directory))
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def _edge_stamps_hold(directory: Path, held: _RouteEdges) -> bool:
    """The files that held an edge still have the size and mtime they were parsed at.

    A file rewritten in place leaves the directory's signature as it was; route files are written
    once (`write_once`), so this only looks at the few that held an edge."""
    for name, (size, mtime_ns, edge) in held.files.items():
        if edge is None:
            continue
        try:
            info = os.stat(str(directory / name))
        except OSError:
            return False
        if (info.st_size, info.st_mtime_ns) != (size, mtime_ns):
            return False
    return True


def _route_edges(root: Path) -> Mapping[Tuple[str, Any], List[Dict[str, Any]]]:
    """The routes directory's continuation edges, read at most once per process while it is unchanged.

    D-120 allows a child index bound to the directory listing ("구현은 디렉터리 목록 digest에
    결속된 자식 색인을 캐시로 둘 수 있다"); correctness never depends on one.  A directory whose
    signature moved, or one too fresh to trust, is read again, and a file whose size and mtime
    are what the last read saw is not parsed again.  A missing or unreadable cache is a fresh scan."""
    directory = _routes_dir(root)
    key = str(directory)
    before = _routes_dir_signature(directory)
    if before is None:
        _ROUTE_EDGES.pop(key, None)
        return {}
    held = _ROUTE_EDGES.get(key)
    if held is not None and held.trusted and held.signature == before and _edge_stamps_hold(directory, held):
        return held.children
    known = held.files if held is not None else {}
    files: Dict[str, Tuple[int, int, Optional[Dict[str, Any]]]] = {}
    children: Dict[Tuple[str, Any], List[Dict[str, Any]]] = {}
    complete = True
    try:
        names = sorted(entry.name for entry in os.scandir(str(directory)) if entry.name.endswith(".json"))
    except OSError:
        _ROUTE_EDGES.pop(key, None)
        return {}
    for name in names:
        entry = directory / name
        try:
            info = os.stat(str(entry))
        except OSError:
            complete = False
            continue
        stamp = (info.st_size, info.st_mtime_ns)
        remembered = known.get(name)
        if remembered is not None and remembered[:2] == stamp:
            edge = remembered[2]
        else:
            candidate = _read_json(entry)
            if candidate is None:
                complete = False
            edge = candidate if _qualified_continuation(entry.stem, candidate) else None
        files[name] = (*stamp, edge)
        if edge is not None:
            children.setdefault((edge.get("source_route_id"), edge.get("source_route_hash")), []).append(edge)
    after = _routes_dir_signature(directory)
    trusted = (complete and after == before
               and time.time_ns() - before[1] > _ROUTE_EDGES_SETTLE_NS)
    if len(_ROUTE_EDGES) >= 8:
        _ROUTE_EDGES.clear()   # a process rarely reads more than one root
    _ROUTE_EDGES[key] = _RouteEdges(before, files, children, trusted)
    return children


def _lineage_children(root: Path, route_id: str, route_hash_value: str) -> List[Dict[str, Any]]:
    """Every continuation whose sealed edge names ``route_id``/``route_hash_value`` as its source.

    A listing-bound disposable index survives CLI processes. Only the selected
    sealed child files are parsed on a hit; missing/stale/corrupt indexes scan
    the original records and repair themselves."""
    return [copy.deepcopy(child)
            for child in _indexed_route_children(root, route_id, route_hash_value)
            if not _closed_unexecuted_continuation(root, child)]


def _indexed_route_children(root: Path, route_id: str, route_hash_value: str):
    def classify(name, candidate):
        return ([directory_record_index.route_key(candidate["source_route_id"], candidate.get("source_route_hash"))]
                if _qualified_continuation(Path(name).stem, candidate) else [])
    try:
        return directory_record_index.select(
            _routes_dir(root), [directory_record_index.route_key(route_id, route_hash_value)],
            kind="route-children", classify=classify, read_json=_read_json,
            ignored=directory_record_index.ROUTE_SIDECARS)
    except OSError:
        return list(_route_edges(root).get((route_id, route_hash_value), []))


def _closed_unexecuted_continuation(root: Path, route: Mapping[str, Any]) -> bool:
    """A closed, unused candidate cannot take an executed predecessor's cycle.

    Audit bindings and unconsumed registrations are history, not execution.
    Read the exact route registry and markers afresh; missing or unreadable
    evidence stays unknown. Started or unresolved launches stay attached.
    """
    outcome = _read_json(route_lineage.canonical_route_path(root, route["route_id"]).with_suffix(".outcome.json"))
    if (not isinstance(outcome, dict)
            or outcome.get("route_id") != route["route_id"]
            or outcome.get("route_hash") != route["route_hash"]
            or outcome.get("terminal_gate_proven") is not False
            or _indexed_route_children(root, route["route_id"], route["route_hash"])):
        return False
    try:
        module = artifact_lifecycle._load_capability_route()
        sealed_jobs = ((route.get("launch_compatibility_tuple") or {}).get("jobs_path") or {}).get("path")
        if not isinstance(sealed_jobs, str) or not Path(sealed_jobs).is_absolute():
            return False
        resolution = module.resolve_dangling_registry(Path(sealed_jobs))
        if resolution.status not in ("exact", "aliased"):
            return False
        jobs = resolution.jobs_path
        before = jobs.stat()
        lines = jobs.read_text(encoding="utf-8").splitlines()
        after = jobs.stat()
        if any(getattr(before, key) != getattr(after, key)
               for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            return False
        for line in lines:
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 6:
                return False
            meta = dispatch_contract.parse_registry_metadata(fields[5])
            if route["route_id"] in {meta.get("route_id"), meta.get("route"), meta.get("owner_route_id")}:
                from route_authority import continuation_attempt_state
                if continuation_attempt_state(meta) != "unstarted":
                    return False
        markers = module.completion_dir(route["route_id"], jobs=jobs)
        if markers.exists() and any(markers.iterdir()):
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


class LineageHandover(NamedTuple):
    """`closed`: the cycle's whole qualifying lineage tree has an outcome.
    `handed_over`: routes of that tree that begin another cycle (empty unless closed)."""
    closed: bool
    handed_over: frozenset


def closed_lineage_handover(root: Path, record: Mapping[str, Any]) -> LineageHandover:
    """Whether ``record``'s lineage is closed, and which routes it handed to other cycles.

    The tree is every material-input-qualifying continuation below the cycle's
    begin route, read through `_route_edges` (one scan of the routes directory). It is closed only
    when every route in it has an outcome; a live tree keeps D-120's "which cycle
    continues" judgment untouched (`(False, frozenset())`). In a closed tree the
    routes that begin a *different* cycle belong to that cycle, so this cycle
    seals on its own stretch, up to just before them.
    """
    begin_route = load_route(root, Path(record["route_file"]))
    if begin_route["route_hash"] != record["route_hash"]:
        raise ProducerError("route-hash-drift", record["cycle_id"])
    tree = {begin_route["route_id"]}
    queue = [begin_route]
    while queue:
        node = queue.pop()
        if not route_is_closed(root, node):
            return LineageHandover(False, frozenset())
        for child in _lineage_children(root, node["route_id"], node["route_hash"]):
            if (child.get("capability") != record.get("capability")
                    or child.get("effective_intensity") != record.get("intensity")):
                continue
            if child["route_id"] in tree:
                return LineageHandover(False, frozenset())  # a cycle in the lineage is never closed
            tree.add(child["route_id"])
            queue.append(child)
    others = {rec.get("route_id") for rec in list_cycle_records(root, route_ids=tree)
              if rec.get("cycle_id") != record.get("cycle_id")}
    return LineageHandover(True, frozenset(tree & others) - {begin_route["route_id"]})


def _handed_over_routes(root: Path, record: Mapping[str, Any]) -> frozenset:
    try:
        return closed_lineage_handover(root, record).handed_over
    except (ProducerError, KeyError, OSError):
        return frozenset()


def route_cycle_for(root: Path, route: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The one cycle begun by a route in ``route``'s verified lineage, if any.

    Shared by every "find the cycle for this route" call site (env resolution,
    begin resume, checkpoint, `require_cycle_output`). The cycle's state does not
    decide whether it can be found (§45 D-123): a continuation binds a closed
    cycle again and writes into it. An open cycle is the one the lineage is
    working in now; two or more open cycles whose begin route sits in the same
    verified lineage is the existing `route-cycle-binding-ambiguous` refusal.
    With none open, the closed cycle begun nearest to ``route`` in the lineage
    is the one, provided D-120's judgment still admits the route to it (a
    continuation that changed capability or intensity does not bind it and
    begins a child cycle instead).
    """
    try:
        lineage = route_lineage.verified_route_lineage(dict(route), artifact_root=root)
    except route_lineage.RouteLineageError as exc:
        raise ProducerError(exc.code, exc.detail) from exc
    nearness = {r.get("route_id"): index for index, r in enumerate(lineage)}  # [route, parent, ..., begin route]
    matches = list_cycle_records(root, route_ids=nearness)
    opened = [rec for rec in matches if rec.get("state") == "open" and not rec.get("deleted_at")]
    if len(opened) > 1:
        raise ProducerError("route-cycle-binding-ambiguous", route.get("route_id", ""))
    if opened:
        return opened[0]
    # A cycle deleted while its route ran is still the route's cycle until the route ends (D-126):
    # `finalize` closes it as a cycle with no output.  A live one, begun since, takes precedence.
    deleted_open = [rec for rec in matches if rec.get("state") == "open" and rec.get("deleted_at")]
    if len(deleted_open) == 1:
        return deleted_open[0]
    closed = sorted((rec for rec in matches if rec.get("state") in CLOSED_BINDABLE_STATES),
                    key=lambda rec: nearness[rec.get("route_id")])
    if closed and cycle_route_admission(root, closed[0], route).allow:
        return closed[0]
    return None


def cycle_route_admission(root: Path, record: Mapping[str, Any], route: Mapping[str, Any],
                          *, finalize: bool = False, validation_only: bool = False) -> Admission:
    """D-120's one lineage judgment for a cycle route.

    Steps 0-4 as spelled out in artifact-path-contract §42: ``route``'s
    hash-verified lineage, the begin route's membership and hash in that
    lineage, material-input (capability/intensity) parity along the path, and
    sibling-fork detection at every path node but ``route`` itself. The cycle's
    state is not a step (§45 D-123): a closed cycle admits its lineage's routes
    again. ``finalize=True`` adds D-120's finalize-only rule: a route with a
    still-attached T(C) continuation child cannot seal
    (`...:superseded-route`). ``validation_only`` is kept for callers that
    read a manifest back; it changes nothing here.
    """
    try:
        lineage = route_lineage.verified_route_lineage(dict(route), artifact_root=root)
    except route_lineage.RouteLineageError as exc:
        reason = "route-lineage-unverified"
        return Admission(False, reason, str(exc), [], _d120_next_action(reason))
    by_id = {r["route_id"]: r for r in lineage}
    begin_id = record.get("route_id")
    if begin_id not in by_id:
        reason = "cycle-route-binding-mismatch"
        return Admission(False, reason, f"begin={begin_id} not in lineage of {route.get('route_id')}",
                         [], _d120_next_action(reason))
    if by_id[begin_id].get("route_hash") != record.get("route_hash"):
        reason = "route-hash-drift"
        return Admission(False, reason, str(record.get("cycle_id", "")), [], _d120_next_action(reason))
    # lineage is [W, parent, ..., A]; the admitted path P runs begin (A) -> W.
    begin_index = next(i for i, r in enumerate(lineage) if r["route_id"] == begin_id)
    path = list(reversed(lineage[: begin_index + 1]))
    for node in path:
        if node.get("capability") != record.get("capability") or node.get("effective_intensity") != record.get("intensity"):
            reason = "cycle-route-binding-mismatch:material-input"
            return Admission(False, reason, f"route={node['route_id']}", path, _d120_next_action(reason))
    handed_over: Optional[frozenset] = None  # computed only when a refusal is about to be issued
    for node in path[:-1]:
        siblings = _lineage_children(root, node["route_id"], node["route_hash"])
        qualifying = [s for s in siblings
                      if s.get("capability") == record.get("capability")
                      and s.get("effective_intensity") == record.get("intensity")]
        for sibling in qualifying:
            if sibling["route_id"] not in by_id:
                if handed_over is None:
                    handed_over = _handed_over_routes(root, record)
                if sibling["route_id"] in handed_over:
                    continue  # closed lineage: that branch belongs to another cycle
                reason = "cycle-route-binding-mismatch:lineage-fork"
                return Admission(False, reason, f"branch={node['route_id']} siblings={sibling['route_id']},{path[path.index(node)+1]['route_id']}",
                                 path, _d120_next_action(reason))
    if finalize:
        qualifying = [s for s in _lineage_children(root, route["route_id"], route["route_hash"])
                      if s.get("capability") == record.get("capability")
                      and s.get("effective_intensity") == record.get("intensity")]
        if qualifying:
            if handed_over is None:
                handed_over = _handed_over_routes(root, record)
            qualifying = [s for s in qualifying if s["route_id"] not in handed_over]
        if qualifying:
            reason = "cycle-route-binding-mismatch:superseded-route"
            return Admission(False, reason, route["route_id"], path, _d120_next_action(reason))
    return Admission(True, None, "", path, None)


def _inline_producer_binding_check(root: Path, cycle_id: str,
                                   binding: Mapping[str, Any]) -> None:
    """Check a direct finish against D-120's verified route lineage.

    The cycle record names its *begin* route. A continuation may be the route
    that closes and seals it, so comparing the two route IDs would reject a
    valid finish and would bypass the common lineage predicate.
    """
    record = read_cycle_record(root, cycle_id)
    # A campaign deleted since is the campaign it was (§45 D-126): its runtime record stands in.
    campaign = campaign_or_tombstone(root, record["campaign_id"]) if record else None
    identity = artifact_lifecycle.read_root_identity(root)
    route_id = binding.get("route_id")
    if not isinstance(route_id, str) or not _ROUTE_ID_RE.fullmatch(route_id):
        raise ProducerError("inline-producer-binding-mismatch", "route-id")
    path = route_lineage.canonical_route_path(root, route_id)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProducerError("inline-producer-binding-mismatch", "route-missing") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or path.resolve() != path:
        raise ProducerError("inline-producer-binding-mismatch", "route-kind")
    route = load_route(root, path)
    # The binding's campaign id/key are the campaign the cycle belonged to when it
    # was bound; a cycle moved since is the same cycle (§45 D-123), so identity
    # is judged by cycle, producer, route and the stored identity digest.
    if (not record or not campaign or not identity
            or binding.get("kind") != "inline_producer_binding_v1"
            or binding.get("cycle_id") != cycle_id
            or binding.get("producer_id") != record.get("producer_id")
            or binding.get("artifact_root_id") != identity.artifact_root_id
            or binding.get("route_hash") != route.get("route_hash")
            or not dispatch_terminal_commit.cycle_identity_matches(
                record, binding.get("cycle_record_digest"), campaign_id=binding.get("campaign_id"))
            or not binding.get("inline_finish_id")
            or not binding.get("terminal_marker_digest")
            or not binding.get("evidence_sha256")):
        raise ProducerError("inline-producer-binding-mismatch", cycle_id)
    admission = cycle_route_admission(
        root, record, route, finalize=True, validation_only=record.get("state") == "sealed")
    if not admission.allow:
        raise ProducerError(admission.reason or "inline-producer-binding-mismatch", admission.detail)
    import inline_finish
    finish = inline_finish.pending_state(root, route_id)
    intent = (finish or {}).get("intent") or {}
    if (not finish or finish.get("inline_finish_id") != binding["inline_finish_id"]
            or finish.get("terminal_marker_digest") != binding["terminal_marker_digest"]
            or intent.get("route_id") != route_id
            or intent.get("route_hash") != binding["route_hash"]
            or intent.get("cycle_id") != cycle_id
            or intent.get("producer_id") != record["producer_id"]
            or intent.get("artifact_root_id") != identity.artifact_root_id
            or intent.get("evidence_sha256") != binding["evidence_sha256"]):
        raise ProducerError("inline-producer-binding-mismatch", "finish-intent")


def resolve_cycle_manifest_route(root: Path, record: Mapping[str, Any],
                                 document: Mapping[str, Any],
                                 route_id: Optional[str] = None) -> Tuple[Path, Dict[str, Any]]:
    """Resolve and read-only admit the route a cycle manifest sealed.

    The cycle record's route tuple remains the begin identity.  A manifest may
    name its verified continuation leaf, so its route row is resolved
    through the canonical route directory and admitted against that begin
    identity.  A closed cycle a later route closed again carries one row per
    closing route (§45 D-127); `route_id` names the one a caller finished
    with, and the latest is meant when none is named.  This helper is safe for
    both pre-seal recovery and sealed replay; it never changes cycle state or
    consults ``route_bindings``.
    """
    rows = document.get("routes") if isinstance(document, Mapping) else None
    if (not isinstance(rows, list) or not rows or not all(isinstance(r, Mapping) for r in rows)
            or (len(rows) != 1 and not record.get("sealed_on") and record.get("state") != "sealed")):
        raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
    if route_id is not None:
        named = [r for r in rows if r.get("route_id") == route_id]
        if len(named) != 1:
            raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
        row = named[0]
    else:
        row = rows[-1]
    identity = artifact_lifecycle.read_root_identity(Path(root))
    if (identity is None or row.get("artifact_root_id") != identity.artifact_root_id
            or document.get("artifact_root_id") != identity.artifact_root_id
            or document.get("cycle", {}).get("cycle_id") != record.get("cycle_id")):
        raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
    route_id = row.get("route_id")
    if not isinstance(route_id, str) or not re.fullmatch(r"rt-[A-Za-z0-9][A-Za-z0-9._-]{0,126}", route_id):
        raise ProducerError("completion-route-composite-mismatch", "route-id")
    route_path = route_lineage.canonical_route_path(root, route_id)
    try:
        info = route_path.lstat()
    except OSError as exc:
        raise ProducerError("route-lineage-unverified", f"canonical-route-missing:{route_id}") from exc
    if not stat.S_ISREG(info.st_mode) or route_path.is_symlink() or route_path.resolve() != route_path:
        raise ProducerError("route-lineage-unverified", f"canonical-route-kind:{route_id}")
    target_check = artifact_lifecycle.validate_route_target(route_path, Path(root), route_id)
    if not target_check.ok:
        reason = target_check.reasons[0]
        raise ProducerError(reason.code, reason.detail)
    route = load_route(Path(root), route_path, expected_identity=row)
    # Only the latest row has to be the lineage's leaf; an earlier closing route is
    # judged by its place in the lineage, not by being the last one.
    admission = cycle_route_admission(Path(root), record, route, finalize=row is rows[-1], validation_only=True)
    if not admission.allow:
        raise ProducerError(admission.reason or "route-lineage-unverified", admission.detail)
    return route_path, route


def _route_binding_entry(route_row: Mapping[str, Any], root: Path, *, is_begin: bool) -> Dict[str, Any]:
    return {
        "route_id": route_row["route_id"],
        "route_hash": route_row["route_hash"],
        "route_file": str(route_lineage.canonical_route_path(root, route_row["route_id"])),
        "basis": "begin" if is_begin else "continuation",
        "continuation_id": None if is_begin else route_row["route_id"],
        "source_route_id": None if is_begin else route_row.get("source_route_id"),
    }


def _binding_core(entry: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: entry.get(key) for key in
            ("route_id", "route_hash", "route_file", "basis", "continuation_id", "source_route_id")}


def _bind_cycle_route_locked(root: Path, record: Mapping[str, Any], route: Mapping[str, Any]) -> Dict[str, Any]:
    """`bind_cycle_route`'s body, for a caller that already holds the admission lock."""
    admission = cycle_route_admission(root, record, route)
    if not admission.allow:
        raise ProducerError(admission.reason, admission.detail)
    expected = [_route_binding_entry(row, root, is_begin=(i == 0)) for i, row in enumerate(admission.path)]
    # A record that never had `route_bindings` at all reads as the compat
    # single-element view (begin route only) -- a legitimate, common state.
    # A record whose `route_bindings` field is *present but empty* is not:
    # every real writer only ever stores a non-empty list (at least the begin
    # entry), so an explicit `[]` can only be tampering (A-25.9 (k)) and must
    # not be silently absorbed into the same "nothing bound yet" prefix match.
    if "route_bindings" not in record:
        stored = [{**_route_binding_entry(admission.path[0], root, is_begin=True),
                  "bound_at": record.get("started_on")}]
    else:
        stored = list(record.get("route_bindings") or [])
    stored_core = [_binding_core(e) for e in stored]
    expected_core = [_binding_core(e) for e in expected]
    advisory = None
    if stored and stored_core == expected_core[: len(stored_core)]:
        new_list = stored + [dict(e, bound_at=_rfc3339()) for e in expected[len(stored_core):]]
    else:
        index = 0
        field = "route_id"
        for index in range(min(len(stored_core), len(expected_core))):
            mismatched = [k for k in expected_core[index] if stored_core[index].get(k) != expected_core[index].get(k)]
            if mismatched:
                field = mismatched[0]
                break
        else:
            index = min(len(stored_core), len(expected_core))
        advisory = f"route-binding-record-drift:index={index};field={field}"
        new_list = [dict(e, bound_at=(stored[i].get("bound_at") if i < len(stored) else _rfc3339()))
                    for i, e in enumerate(expected)]
    written = new_list != stored
    if written:
        _write_cycle_record(root, {**record, "route_bindings": new_list}, exclusive=False)
    return {"written": written, "advisory": advisory}


def bind_cycle_route(root: Path, cycle_id: str, route: Mapping[str, Any]) -> Dict[str, Any]:
    """D-120's one audit-record writer: `route_bindings[]` derived from the sealed lineage.

    Re-judges admission under the admission lock and only writes on allow.
    The write is append-if-prefix, replace-with-advisory otherwise -- never an
    input to any judgment (D-120 "권한은 봉인 파일에서만 나온다"). Returns
    ``{"written": bool, "advisory": str|None}``.
    """
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        return _bind_cycle_route_locked(root, record, route)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _write_journal(root: Path, cycle_id: str, **fields: Any) -> None:
    path = journal_path(root, cycle_id)
    _ensure_dir(path.parent)
    payload = {"schema_version": 1, "cycle_id": cycle_id, "updated_at": _rfc3339()}
    payload.update(fields)
    _write_atomic(path, _json_bytes(payload), 0o600)


def _remove_journal(root: Path, cycle_id: str) -> None:
    try:
        journal_path(root, cycle_id).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# route helpers
# ---------------------------------------------------------------------------


_ROUTE_ID_RE = re.compile(r"^rt-[0-9a-f]{6,}$")


def resolve_route_argument(root: Path, value: "str | Path") -> Path:
    """Accept either a route file path or a bare route id.

    A `quick` dispatch-depth-1 owner is handed only the route **id** — the
    prompt and the wrapper args carry `--route-id`, never the file path — so an
    owner that passed that id straight through used to die with
    `route-unreadable` naming the id it was given. A bare id is unambiguous: it
    resolves to exactly one canonical location under the artifact root. Resolve
    it instead of refusing. Anything else is returned untouched and is read as
    the path it is, so an explicit path always wins.
    """

    text = str(value)
    if _ROUTE_ID_RE.match(text):
        candidate = artifact_lifecycle.canonical_route_path(Path(root), text)
        if candidate.is_file():
            return candidate
    return Path(value)


def load_route(root: Path, route_file: Path, *,
               expected_identity: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    route_file = resolve_route_argument(root, route_file)
    route = _read_json(Path(route_file))
    if route is None:
        raise ProducerError("route-unreadable", str(route_file))
    for key in ("route_id", "route_hash", "capability", "effective_intensity", "artifact_root", "nodes"):
        if key not in route:
            raise ProducerError("route-invalid", f"missing {key}")
    if Path(str(route["artifact_root"])).resolve() != Path(root).resolve():
        raise ProducerError("route-artifact-root-mismatch")
    if route["effective_intensity"] not in INTENSITIES:
        raise ProducerError("route-invalid", "effective_intensity")
    if route.get("route_hash") != route_identity.route_hash(route):
        raise ProducerError("route-invalid", "stale or modified route hash")
    if "resplit_cycle_key" in route:
        nonce = route.get("resplit_route_nonce")
        if not isinstance(nonce, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", nonce):
            raise ProducerError("route-invalid", "resplit route nonce")
        expected_route_id = "rt-" + nonce.split(":", 1)[-1][:16]
    else:
        expected_route_id = "rt-" + str(route["route_hash"]).split(":", 1)[-1][:16]
    if route.get("route_id") != expected_route_id:
        raise ProducerError("route-invalid", "route id/hash mismatch")
    if "slug" in route:
        if not isinstance(route.get("slug"), str) or not isinstance(route.get("slug_truncated"), bool):
            raise ProducerError("route-invalid", "slug metadata")
        try:
            canonical_slug, _truncated = artifact_locator.slugify(route["slug"])
        except artifact_locator.LocatorError as exc:
            raise ProducerError("route-invalid", "slug metadata") from exc
        if canonical_slug != route["slug"]:
            raise ProducerError("route-invalid", "slug is not canonical")
    elif "slug_truncated" in route:
        raise ProducerError("route-invalid", "slug metadata incomplete")
    if expected_identity is not None and (
            route.get("route_id") != expected_identity.get("route_id")
            or route.get("route_hash") != expected_identity.get("route_hash")):
        raise ProducerError("completion-route-hash-mismatch", str(expected_identity.get("route_id", "")))
    return route


def route_is_closed(root: Path, route: Mapping[str, Any]) -> bool:
    try:
        outcome = artifact_lifecycle.canonical_outcome_path(root, route["route_id"])
    except artifact_lifecycle.LifecycleError:
        return False
    return outcome.is_file()


def _route_node(route: Mapping[str, Any], node_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not node_id or node_id == "-":
        return None
    for row in route.get("nodes", []):
        if isinstance(row, dict) and row.get("id") == node_id:
            return row
    raise ProducerError("route-node-unknown", node_id)


# ---------------------------------------------------------------------------
# campaign records
# ---------------------------------------------------------------------------


def _campaign_path(root: Path, campaign_id: str,
                   record: Optional[Mapping[str, Any]] = None, *, creating: bool = False) -> Path:
    if creating and record is not None and record.get("campaign_id") == campaign_id and record.get("locator"):
        # A campaign directory being created does not exist yet, so any
        # lookup would fail and fall through to a needless lenient full scan.
        # The record's own `locator` is authoritative at creation time.
        try:
            campaigns = artifact_locator.safe_child(root, root, "campaigns")
            return artifact_locator.safe_child(root, campaigns, record["locator"]) / "campaign.json"
        except artifact_locator.LocatorError as exc:
            raise ProducerError("record-locator-invalid", exc.detail or exc.code) from exc
    return campaign_dir(root, campaign_id, record) / "campaign.json"


def read_campaign(root: Path, campaign_id: str) -> Optional[Dict[str, Any]]:
    if not artifact_identity.is_well_formed(campaign_id, "campaign"):
        return None
    found = artifact_locator.locate(root, campaign_id)
    if found is not None:
        record = _read_json(found / "campaign.json")
        if record is not None and record.get("campaign_id") == campaign_id:
            return artifact_campaign.fold_campaign(root, found / "campaign.json", record)
    fallback = _read_json(Path(root) / "campaigns" / campaign_id / "campaign.json")
    if fallback is not None and fallback.get("campaign_id") == campaign_id:
        return artifact_campaign.fold_campaign(root, Path(root) / "campaigns" / campaign_id / "campaign.json", fallback)
    return None


def find_campaign_by_key(root: Path, key: str) -> Optional[Dict[str, Any]]:
    return next((row for row in _campaigns_by_key(root, key) if row.get("state") == "active"), None)


def _campaigns_by_key(root: Path, key: str) -> List[Dict[str, Any]]:
    campaigns = Path(root) / "campaigns"
    if not campaigns.is_dir():
        return []
    rows = []
    for entry in artifact_locator.iter_campaign_dirs(root):
        record = _read_json(entry / "campaign.json")
        if record and record.get("key") == key:
            folded = artifact_campaign.fold_campaign(root, entry / "campaign.json", record)
            folded["_campaign_path"] = str(entry / "campaign.json")
            rows.append(folded)
    return rows


def classify_campaign_key(rows: Sequence[Mapping[str, Any]], key: str) -> Dict[str, Any]:
    matches = [row for row in rows if row.get("key") == key]
    invalid = [row for row in matches if row.get("state") == "invalid"]
    if invalid:
        return {"mode": "blocked", "code": invalid[0].get("state_error") or "campaign-state-invalid"}
    active = [row for row in matches if row.get("state") == "active"]
    closed = [row for row in matches if row.get("state") == "satisfied"]
    dead = [row for row in matches if row.get("state") in {"abandoned", "superseded"}]
    if len(closed) > 1:
        return {"mode": "blocked", "code": "campaign-key-reopen-ambiguous"}
    if len(active) > 1:
        return {"mode": "blocked", "code": "campaign-key-ambiguous"}
    if active:
        return {"mode": "join", "campaign_id": active[0].get("campaign_id")}
    if closed:
        return {"mode": "reopen", "campaign_id": closed[0].get("campaign_id")}
    if dead:
        return {"mode": "blocked", "code": "campaign-not-active"}
    return {"mode": "create", "campaign_id": None}


def _write_campaign(root: Path, record: Dict[str, Any], *, exclusive: bool) -> None:
    path = _campaign_path(root, record["campaign_id"], record, creating=exclusive)
    artifact_campaign.check_campaign_write(root, path, record)
    _ensure_dir(path.parent)
    if exclusive:
        _write_exclusive(path, _json_bytes(record))
    else:
        _write_atomic(path, _json_bytes(record))


# ---------------------------------------------------------------------------
# activate / status
# ---------------------------------------------------------------------------


def activate(
    root: Path,
    *,
    repository_id: str,
    artifact_root_id: str,
    w7: Optional[Mapping[str, Any]] = None,
    approval_receipt_sha256: Optional[str] = None,
    activation_kind: str = "approval",
    adopt_existing_identity: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if not artifact_identity.is_well_formed(repository_id, "repository"):
        raise ProducerError("identity-malformed", "repository_id")
    if not artifact_identity.is_well_formed(artifact_root_id, "artifact_root"):
        raise ProducerError("identity-malformed", "artifact_root_id")
    if activation_kind not in ACTIVATION_KINDS:
        raise ProducerError("activation-kind-unknown", activation_kind)
    requested_ids = (repository_id, artifact_root_id)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        existing = artifact_lifecycle.read_root_identity(root)
        if existing is None:
            payload = artifact_identity.RootIdentity(
                schema_version=1,
                artifact_root_id=artifact_root_id,
                repository_id=repository_id,
                issued_at=_rfc3339(now),
                producer_contract_version=artifact_manifest.CONTRACT_VERSION,
            ).to_payload()
            identity_path = artifact_admission._root_identity_path(root)
            _ensure_dir(identity_path.parent)
            _write_exclusive(identity_path, _json_bytes(payload), 0o600)
            identity = artifact_identity.RootIdentity.parse(payload)
            identity_state = "created"
        else:
            if adopt_existing_identity:
                repository_id = existing.repository_id
                artifact_root_id = existing.artifact_root_id
            elif (existing.repository_id, existing.artifact_root_id) != requested_ids:
                raise ProducerError("identity-conflict", "root identity already frozen with other ids")
            identity = existing
            identity_state = "adopted" if (adopt_existing_identity and
                (existing.repository_id, existing.artifact_root_id) != requested_ids) else "matched"
        current = read_cutover(root)
        if current.get("state") == "active":
            if current.get("identity", {}).get("artifact_root_id") != artifact_root_id:
                raise ProducerError("cutover-identity-conflict")
            return {"status": "already-active", "cutover": current, "identity": identity_state}
        body = {
            "schema_version": 1,
            "contract": CONTRACT,
            "state": "active",
            "activated_at": _rfc3339(now),
            "identity": {"repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id},
            "w7": dict(w7 or {}),
            "approval_receipt_sha256": approval_receipt_sha256,
            "activation_kind": activation_kind,
        }
        _ensure_dir(producer_dir(root))
        _write_exclusive(cutover_path(root), _json_bytes(body), 0o600)
        return {"status": "activated", "cutover": body, "identity": identity_state}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def status(root: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = artifact_lifecycle.read_root_identity(root)
    records = list_cycle_records(root)
    counts: Dict[str, int] = {}
    for row in records:
        counts[row.get("state", "?")] = counts.get(row.get("state", "?"), 0) + 1
    journal_dir = producer_dir(root) / "journal"
    pending = sorted(p.stem for p in journal_dir.glob("*.json")) if journal_dir.is_dir() else []
    klass = classify_root(root)
    fallback = legacy_fallback_state(root, classification=klass)
    specs = [r for r in records if r.get("capability") == "autopilot-spec" and r.get("state") == "sealed"]
    references = list_references(root, "spec") if specs else []
    # Read shared revision metadata once per status call, not once per cycle.
    # This is an in-call projection, never a cache or publication authority.
    lineage: Dict[str, Any] = {}
    for reference in references:
        ref_id = reference["shared_reference_id"]
        for revision_id in reversed(reference.get("revisions", [])):
            revision = _read_json(root / "shared/spec" / ref_id / "revisions" / revision_id / REVISION_RECORD_NAME)
            if revision is not None:
                source = revision.get("source")
                source_cycle = source.get("cycle_id") if isinstance(source, Mapping) else None
                if isinstance(source_cycle, str):
                    lineage.setdefault(source_cycle, {}).setdefault(ref_id, []).append((revision_id, revision))
    publications = [completed_spec_publication(root, cycle_id=r["cycle_id"], references=references,
                                              known_publications=lineage.get(r["cycle_id"], {}))
                    for r in specs]
    return {
        "artifact_root": str(root),
        "cutover": read_cutover(root),
        "identity": identity.to_payload() if identity else None,
        "cycle_counts": counts,
        "open_cycles": [r["cycle_id"] for r in records if r.get("state") == "open"],
        "pending_journals": pending,
        "shared_spec_publications": [p for p in publications if p["status"] != "not-applicable"],
        "root_classification": klass["state"],
        "activation_kind": read_cutover(root).get("activation_kind", "approval") if klass["state"] == "active" else None,
        "legacy_fallback": fallback,
    }


# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------


# SD-163: read-only lookup of a prior cycle's same-named output. Every failure
# below is "not found"; nothing here refuses, gates, or writes.
INPUT_SOURCE_MAX_DEPTH = 6
INPUT_SOURCE_MAX_ENTRIES = 4096
INPUT_SOURCE_MAX_CANDIDATES = 16


def _input_target(name: Any) -> Optional[Tuple[Tuple[str, ...], bool]]:
    """Return (path components, is_dir) for a declared input, or None when it is not a plain path."""
    if not isinstance(name, str):
        return None
    is_dir = name.endswith("/**")
    parts = tuple((name[:-3] if is_dir else name).split("/"))
    if any(part in ("", ".", "..") or any(char in part for char in "*?[<>") for part in parts):
        return None
    return parts, is_dir


def _cycle_artifacts_dir(root: Path, cycle_id: str) -> Optional[Path]:
    record = read_cycle_record(root, cycle_id)
    if record is None:
        return None
    artifacts = cycle_dir(root, record["campaign_id"], cycle_id, record) / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        return None
    resolved = artifacts.resolve(strict=True)
    return resolved if resolved.is_relative_to(Path(root).resolve()) else None


def _scan_cycle_artifacts(artifacts: Path) -> Optional[List[Tuple[str, bool]]]:
    """List (relative posix path, is_dir) below artifacts/; symlinks are skipped, caps give None."""
    found: List[Tuple[str, bool]] = []
    stack = [(artifacts, 0)]
    visited = 0
    while stack:
        directory, depth = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > INPUT_SOURCE_MAX_ENTRIES:
                    return None
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not entry.is_file(follow_symlinks=False):
                    continue
                found.append((Path(entry.path).relative_to(artifacts).as_posix(), is_dir))
                if is_dir and depth < INPUT_SOURCE_MAX_DEPTH:
                    stack.append((Path(entry.path), depth + 1))
    return found


def _session_cycle_ids(root: Path, campaign: Mapping[str, Any], route_chain_identity,
                       begin_cycle: Mapping[str, str]) -> set:
    """Cycles of the routes this session composed for the campaign, read from its route-chain ledger.

    A ledger line names a route, never a cycle. A cycle record names its begin route, so a line
    maps to a cycle directly when that route began one, and through its verified lineage when it is
    a continuation of a route that did. The cycle's own audit copy of rebound routes is not read.
    """
    try:
        harness, session_id = route_chain_identity[:2]
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import route_chain
        if route_chain.WRITER_SUPPORT.get(harness) != "env":
            return set()
        resolved = Path(root).resolve()
        composed = {line["route_id"]
                    for line in route_chain.read_tail(harness, session_id, max_bytes=1024 * 1024)
                    if line.get("event") in route_chain.COMPOSING_EVENTS
                    and line.get("campaign_key") == campaign.get("key")
                    and Path(str(line.get("artifact_root") or "")).resolve() == resolved}
    except Exception:
        return set()
    cycles = set()
    for route_id in composed:
        found = begin_cycle.get(route_id)
        if found is None:
            try:
                route = load_route(root, route_lineage.canonical_route_path(root, route_id))
                for ancestor in route_lineage.verified_route_lineage(route, artifact_root=root):
                    found = found or begin_cycle.get(ancestor["route_id"])
            except Exception:
                continue
        if found is not None:
            cycles.add(found)
    return cycles


def same_flow_source_cycle(root: Path, campaign_id: str, *, capability: str,
                           route_chain_identity=None, before: Optional[str] = None) -> Optional[str]:
    """The most recent cycle of the same flow, or None; never another flow's cycle.

    Order: the composing session's own route cycle in this campaign, then the
    latest cycle whose route capability is ``capability``. ``before`` keeps only
    cycles that started earlier than that cycle.
    """
    campaign = read_campaign(root, campaign_id)
    cycles = (campaign or {}).get("cycles")
    if (not campaign or campaign.get("degraded") is True or campaign.get("key") == UNASSIGNED_KEY
            or not isinstance(cycles, list) or not cycles):
        return None
    if before is not None:
        if before not in cycles:
            return None
        cycles = cycles[:cycles.index(before)]
    records = {row.get("cycle_id"): row for row in list_cycle_records(root)
               if row.get("campaign_id") == campaign_id and row.get("cycle_id") in cycles}
    begin_cycle = {rec["route_id"]: cycle_id for cycle_id, rec in records.items() if rec.get("route_id")}
    session_cycles = (_session_cycle_ids(root, campaign, route_chain_identity, begin_cycle)
                      if route_chain_identity else set())
    for cycle_id in reversed(cycles):
        if cycle_id in session_cycles:
            return cycle_id
    for cycle_id in reversed(cycles):
        rec = records.get(cycle_id)
        route_capability = rec.get("route_capability") if rec else None
        if not route_capability and rec and rec.get("route_file"):
            try:
                route_file = Path(rec["route_file"]).resolve(strict=True)
                if route_file.is_relative_to(Path(root).resolve()):
                    route_capability = (_read_json(route_file) or {}).get("capability")
            except Exception:
                pass
        if route_capability == capability:
            return cycle_id
    return None


def input_source_cycle(root: Path, *, parent_cycle_id: Optional[str] = None,
                       campaign_key: Optional[str] = None, capability: Optional[str] = None,
                       route_chain_identity=None) -> Optional[str]:
    """The one source cycle: the parent, else the joined campaign's most recent cycle."""
    if parent_cycle_id:
        return parent_cycle_id
    if not campaign_key or campaign_key == UNASSIGNED_KEY:
        return None
    decision = classify_campaign_key(list_campaign_summaries(root, active_only=False), campaign_key)
    if decision.get("mode") not in ("join", "reopen"):
        return None
    return (same_flow_source_cycle(root, decision["campaign_id"], capability=capability,
                                   route_chain_identity=route_chain_identity)
            if capability else None)


def input_source_finder(root: Path, *, parent_cycle_id: Optional[str] = None,
                        campaign_key: Optional[str] = None, capability: Optional[str] = None,
                        route_chain_identity=None):
    """Return find(name) -> {"cycle_id", "path"} | None, resolving the source lazily and once."""
    root = Path(root)
    memo: Dict[str, Optional[Dict[str, str]]] = {}
    loaded: Dict[str, Any] = {}

    def lookup(name: str) -> Optional[Dict[str, str]]:
        if not loaded:
            loaded["entries"] = None
            cycle_id = input_source_cycle(root, parent_cycle_id=parent_cycle_id, campaign_key=campaign_key,
                                          capability=capability, route_chain_identity=route_chain_identity)
            artifacts = _cycle_artifacts_dir(root, cycle_id) if cycle_id else None
            if artifacts is not None:
                loaded.update(cycle_id=cycle_id, artifacts=artifacts,
                              entries=_scan_cycle_artifacts(artifacts))
        target = _input_target(name)
        if target is None or loaded["entries"] is None:
            return None
        parts, is_dir = target
        rels = [rel for rel, entry_dir in loaded["entries"]
                if entry_dir == is_dir and tuple(rel.split("/"))[-len(parts):] == parts]
        if not rels or len(rels) > INPUT_SOURCE_MAX_CANDIDATES:
            return None
        # Shortest cycle-relative path by character length, then lexicographic; never by recency.
        chosen = loaded["artifacts"] / min(rels, key=lambda rel: (len(rel), rel))
        resolved = chosen.resolve(strict=True)
        if not resolved.is_relative_to(loaded["artifacts"]):
            return None
        return {"cycle_id": loaded["cycle_id"], "path": resolved.relative_to(root.resolve()).as_posix()}

    def find(name: str) -> Optional[Dict[str, str]]:
        if name not in memo:
            try:
                memo[name] = lookup(name)
            except Exception:  # SD-163: every lookup failure is "not found"
                memo[name] = None
        return dict(memo[name]) if memo[name] else None

    return find


def _composing_anchor(route_id, root=None):
    """The depth-0 session whose route-chain ledger says it composed ``route_id``, else None.

    A route file is written once, so its mtime bounds the ledgers that can hold the compose line.
    """
    try:
        created = None
        try:
            created = (Path(root) / ".runtime" / "routes" / f"{route_id}.json").stat().st_mtime if root else None
        except OSError:
            pass
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from fleet import route_chain
        return route_chain.composing_anchor(route_id, not_before=created)
    except Exception:
        return None


def _parent_output_dir(root: Path, record: Mapping[str, Any], route: Optional[Mapping[str, Any]],
                       route_chain_identity=None) -> Optional[str]:
    """Absolute artifacts/ of the source cycle: parent, else sealed input_sources, else the campaign's previous cycle."""
    try:
        cycle_id = record.get("parent_cycle_id")
        for node in (route or {}).get("nodes") or []:
            if cycle_id:
                break
            sources = node.get("input_sources") if isinstance(node, Mapping) else None
            for source in (sources.values() if isinstance(sources, Mapping) else ()):
                cycle_id = cycle_id or (source.get("cycle_id") if isinstance(source, Mapping) else None)
        cycle_id = cycle_id or same_flow_source_cycle(
            root, record["campaign_id"], capability=(route or {}).get("capability") or record.get("route_capability"),
            route_chain_identity=route_chain_identity, before=record["cycle_id"])
        directory = _cycle_artifacts_dir(root, cycle_id) if cycle_id and cycle_id != record["cycle_id"] else None
        return str(directory) if directory else None
    except Exception:  # SD-163: every lookup failure is "not found"
        return None


def _env_for(root: Path, record: Mapping[str, Any], route: Optional[Mapping[str, Any]] = None,
             route_chain_identity=None) -> Dict[str, str]:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    env = {
        "AGENT_ARTIFACT_ROOT": str(root),
        "AGENT_ARTIFACT_CAMPAIGN_ID": record["campaign_id"],
        "AGENT_ARTIFACT_CYCLE_ID": record["cycle_id"],
        "AGENT_ARTIFACT_PRODUCER_ID": record["producer_id"],
        "AGENT_ARTIFACT_CYCLE_DIR": str(directory),
        "AGENT_ARTIFACT_OUTPUT_DIR": str(directory / "artifacts"),
    }
    # This is an already-declared producer context. Carrying it through the
    # existing artifact env lets the next explicit same-campaign cycle join
    # without an agent remembering a group ID or changing route lineage.
    import artifact_workflow_groups
    group_id = artifact_workflow_groups.group_for_cycle(root, record["campaign_id"], record["cycle_id"])
    if group_id:
        env["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"] = group_id
    if route_chain_identity is None and route:
        route_chain_identity = _composing_anchor(route.get("route_id"), root)
    parent_output = _parent_output_dir(root, record, route, route_chain_identity)
    if parent_output:
        env["AGENT_ARTIFACT_PARENT_OUTPUT_DIR"] = parent_output
    return env


# Total time an owner launch waits for the admission lock while a holder on a
# slow disk keeps it. Past it nothing has started and the caller is told to run
# start again later.
OWNER_LAUNCH_ADMISSION_WAIT_SECONDS = 120.0


def _begin_waiting_for_admission(root: Path, **kwargs: Any) -> Dict[str, Any]:
    """`begin`, asking again while the admission lock is held, up to the launch wait bound.

    AdmissionBusy surfaces when the lock is taken, before anything is written,
    and the steps ahead of it are read-only, so asking again is safe. Past the
    bound nothing has started: the typed error tells the caller to run start
    again later.
    """
    deadline = time.monotonic() + OWNER_LAUNCH_ADMISSION_WAIT_SECONDS
    while True:
        try:
            return begin(root, **kwargs)
        except artifact_admission.AdmissionBusy:
            if time.monotonic() >= deadline:
                raise ProducerError(
                    "admission-busy",
                    f"admission lock held past {OWNER_LAUNCH_ADMISSION_WAIT_SECONDS:g}s; "
                    "nothing started; run start again later")


def bind_owner_launch(args, jobs: Path, *, environ=None) -> Optional[Dict[str, Any]]:
    """Publish the owner binding at the registered launch seam (§13.53.3).

    The wrapper has claimed the owner row but has not spawned it yet. Reusing
    begin's resume-only success path preserves every admission check and keeps
    settlement read-only.
    """
    environ = os.environ if environ is None else environ
    if not dispatch_contract.is_runtime_owner_launch(args) or not environ.get("AGENT_ARTIFACT_CYCLE_ID"):
        return None
    owner_binding = getattr(args, "owner_route_binding", None)
    route_file = (owner_binding.get("route_file") if isinstance(owner_binding, Mapping)
                  else getattr(owner_binding, "route_file", None)) or getattr(args, "route_file", None)
    if not route_file:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        root = Path(route["artifact_root"]).resolve()
        validated_route = load_route(root, Path(route_file))
        existing_open = route_cycle_for(root, validated_route)
        env_cycle = environ["AGENT_ARTIFACT_CYCLE_ID"]
        if existing_open is not None and existing_open["cycle_id"] != env_cycle:
            raise ProducerError("producer-binding-mismatch",
                                f"launch-cycle={env_cycle} bound={existing_open['cycle_id']}")
        result = _begin_waiting_for_admission(
            root, route_file=Path(route_file), capability=route["capability"],
            intensity=route["effective_intensity"], require_cycle=True,
            jobs=Path(jobs), owner_attempt_id=args.attempt_id, resume_only=True)
        if result.get("cycle_id") != env_cycle:
            raise ProducerError("producer-binding-mismatch",
                                f"launch-cycle={env_cycle} bound={result.get('cycle_id', '')}")
        return result
    except ProducerError:
        raise
    except Exception as exc:
        detail = str(getattr(exc, "detail", "") or exc)
        raise ProducerError(getattr(exc, "code", type(exc).__name__), detail) from exc


def prepare_route_artifact_env(route_file: Path, *, start: bool, jobs: Path,
                               require_cycle: bool = True) -> Dict[str, str]:
    """Resolve the route's own output context; callers need not copy begin's env.

    Start owns idempotent preparation. Readiness checks only read an existing
    cycle. No inherited cycle or 'latest' directory participates in selection.
    """
    raw = _read_json(route_file)
    if not isinstance(raw, dict) or not raw.get("artifact_root"):
        raise ProducerError("route-artifact-root-missing", str(route_file))
    root = Path(raw["artifact_root"]).resolve()
    route = load_route(root, route_file)
    if start:
        context = (route.get("work_request") or {}).get("workflow_group_context")
        selection = {}
        if context:
            from work_start import group_context_matches_campaign
            if not group_context_matches_campaign(route, context):
                raise ProducerError("workflow-group-campaign-mismatch", context["campaign_id"])
            selection = {"workflow_group_id": context["group_id"]}
        return _begin_waiting_for_admission(
            root, route_file=route_file, capability=route["capability"],
            intensity=route["effective_intensity"], require_cycle=require_cycle, jobs=jobs, **selection)["env"]
    record = route_cycle_for(root, route)
    if record is None:
        return {"AGENT_ARTIFACT_ROOT": str(root), **{name: "" for name in (
            "AGENT_ARTIFACT_CAMPAIGN_ID", "AGENT_ARTIFACT_CYCLE_ID", "AGENT_ARTIFACT_PRODUCER_ID",
            "AGENT_ARTIFACT_CYCLE_DIR", "AGENT_ARTIFACT_OUTPUT_DIR")}}
    return _env_for(root, record, route)


def _route_naming(
    route: Mapping[str, Any], campaign: Optional[Mapping[str, Any]],
    *, title: Optional[str], goal: Optional[str], root: Optional[Path] = None,
) -> Tuple[str, str, str, bool]:
    """Return canonical slug, display title, source, and truncation fact.

    Slugless records are pre-W7I routes.  During the explicit transition
    window they remain usable and are marked so migration can distinguish
    them from routes that sealed a slug.  D-91 closes that window when the
    root's relayout completes; from then on a slugless route is a typed
    refusal, never a silent derived name.
    """
    route_slug = route.get("slug")
    if isinstance(route_slug, str) and route_slug:
        slug, normalized_truncated = artifact_locator.slugify(route_slug)
        return slug, str(title or slug), "route", bool(route.get("slug_truncated")) or normalized_truncated
    if root is not None and transition_window_closed(root):
        raise ProducerError("route-slug-missing", str(route.get("route_id") or "route"))
    candidates = []
    if campaign is not None:
        candidates.extend((campaign.get("slug"), campaign.get("title")))
    candidates.extend((title, goal))
    if campaign is not None:
        candidates.append(campaign.get("goal"))
    raw = next((str(value) for value in candidates if isinstance(value, str) and value.strip()), "unnamed")
    slug, truncated = artifact_locator.slugify(raw, fallback="unnamed")
    display_title = str(title or (campaign or {}).get("title") or slug)
    return slug, display_title, "derived-legacy-route", truncated


UNASSIGNED_KEY = "_unassigned"


def _campaign_naming(campaign_key: Optional[str]) -> Tuple[str, str, str, bool]:
    """Return (slug, title, slug_source, truncated) for a *campaign* record.

    A campaign is the work stream the agent proposed; its locator and title
    come from that proposal, never from the first route that happened to
    join it.  Deriving the campaign name from the first cycle's slug produced
    ``<date>_tf-rehancer-analysis-cx`` for the stream ``tf-rehancer-icassp``
    (TF-Rehancer 2026-09-15).  The reserved `_unassigned` container keeps its
    fixed name.
    """
    if campaign_key is None:
        return "unassigned", UNASSIGNED_KEY, "reserved", False
    slug, truncated = artifact_locator.slugify(campaign_key, fallback="stream")
    return slug, campaign_key, "campaign-key", truncated


RECOVERED_SOURCE = "retirement-backup-mtime"


def default_backup_store() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "hearting" / "artifact-retirement"


def _retirement_mtime_index(run_dir: Path) -> Dict[str, int]:
    """``source path -> mtime`` of a retirement archive, cached beside it.

    The archive is the pre-migration bytes with their original mtimes (the
    W7C/W7G copies lost theirs). Listing a multi-GB gzip means decompressing
    it once; the cache is keyed by the seal's archive digest."""
    import tarfile
    seal = _read_json(run_dir / "backup-seal.json") or {}
    cache_path = run_dir / "mtime-index.json"
    cached = _read_json(cache_path)
    if (isinstance(cached, dict) and cached.get("archive_sha256") == seal.get("archive_sha256")
            and isinstance(cached.get("members"), dict)):
        return {str(k): int(v) for k, v in cached["members"].items()}
    members: Dict[str, int] = {}
    with tarfile.open(run_dir / "retired-sources.tar.gz", "r:gz") as archive:
        for member in archive:
            if member.isfile():
                members[member.name] = int(member.mtime)
    try:
        _write_atomic(cache_path, _json_bytes({"schema_version": 1, "archive_sha256": seal.get("archive_sha256"),
                                               "members": members}))
    except OSError:
        pass  # the cache is a convenience; the archive stays the source
    return members


def _retirement_digest_index(store: Path, root_id: str) -> Tuple[Dict[str, Tuple[str, int, str]], List[str]]:
    """``sha256 -> (source path, mtime, run)`` over every retirement run of a root."""
    by_sha: Dict[str, Tuple[str, int, str]] = {}
    runs: List[str] = []
    base = store / root_id
    if not base.is_dir():
        return by_sha, runs
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest = run_dir / "retired-manifest.jsonl"
        if not manifest.is_file() or not (run_dir / "retired-sources.tar.gz").is_file():
            continue
        mtimes = _retirement_mtime_index(run_dir)
        runs.append(run_dir.name)
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            source, sha = row.get("source"), row.get("sha256")
            if isinstance(source, str) and isinstance(sha, str) and source in mtimes:
                by_sha.setdefault(sha, (source, mtimes[source], run_dir.name))
    return by_sha, runs


def recover_cycle_times(root: Path, *, backup_store: Optional[Path] = None,
                        apply: bool = False) -> Dict[str, Any]:
    """Recover the start time of cycles that only know their work's date.

    W7G resplit and W7H residue cycles were built from copies whose mtimes
    were the copy time, so their records carry a date (or the resplit run
    time) and no clock. The retirement backup of the same root keeps the
    original files with their original mtimes, and its manifest keys them by
    sha256. Each sealed artifact revision's ``content_digest`` therefore leads
    back to the original file; the earliest such mtime is the cycle's
    ``recovered_started_on`` (UTC, second precision), kept beside the
    untouched ``started_on`` with its evidence. An mtime is a *last* write:
    when the earliest one lands after the folder's date (a later bulk
    rewrite), it cannot be the start, so it is stored as evidence only
    (``recovered_earliest_write``) and the display keeps the date. Earlier
    than the folder date means the folder date was wrong (a copy date) and
    the recovered time wins. Dry run by default; ``apply``
    holds the producer admission lock, journals every record pre-image under
    ``.runtime/artifact-producer/v1/time-recovery/`` and rebuilds the indexes.
    """
    root = Path(root).resolve()
    store = Path(backup_store) if backup_store is not None else default_backup_store()
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing", str(root))
    by_sha, runs = _retirement_digest_index(store, identity.artifact_root_id)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT) if apply else None
    journal: List[Dict[str, Any]] = []
    try:
        rows: List[Dict[str, Any]] = []
        for record_path in sorted((producer_dir(root) / "cycles").glob("cyc_*.json")):
            record = _read_json(record_path)
            if not isinstance(record, dict):
                continue
            if not (record.get("derived_from_cycle_id") or record.get("started_on_source")):
                continue  # a producer-born cycle already has its real clock
            cycle_id = record["cycle_id"]
            row: Dict[str, Any] = {"cycle_id": cycle_id, "locator": record.get("locator"),
                                   "previous": record.get("recovered_started_on")}
            try:
                directory = artifact_locator.resolve_path(root, cycle_id)
            except artifact_locator.LocatorError as exc:
                rows.append({**row, "action": "unresolved", "detail": exc.code}); continue
            if directory is None:
                rows.append({**row, "action": "unresolved", "detail": "no-directory"}); continue
            manifest = _read_json(directory / "manifest.json")
            if not isinstance(manifest, dict):
                rows.append({**row, "action": "no-manifest"}); continue
            digests = [str(rev.get("content_digest", "")).split(":", 1)[-1]
                       for rev in manifest.get("artifact_revisions", []) if isinstance(rev, dict)]
            hits = [by_sha[d] for d in digests if d in by_sha]
            row.update({"matched": len(hits), "total": len(digests)})
            if not digests:
                rows.append({**row, "action": "no-artifacts"}); continue
            if not hits:
                rows.append({**row, "action": "no-match"}); continue
            earliest = min(h[1] for h in hits)
            latest = max(h[1] for h in hits)
            recovered = _rfc3339(earliest)
            folder_date = str(record.get("locator") or "")[:10]
            usable = not folder_date or recovered[:10] <= folder_date
            evidence = {"matched": len(hits), "total": len(digests), "earliest": recovered,
                        "latest": _rfc3339(latest), "backup_runs": sorted({h[2] for h in hits})}
            row.update({"recovered_started_on": recovered if usable else None,
                        "earliest_write": recovered, "latest_write": evidence["latest"],
                        "backup_run": evidence["backup_runs"],
                        "folder_date_agrees": recovered[:10] == folder_date,
                        "display": "recovered" if usable else "evidence-only"})
            if (record.get("recovered_earliest_write") == recovered
                    and record.get("recovered_started_on") == (recovered if usable else None)):
                rows.append({**row, "action": "already"}); continue
            row["action"] = "recovered" if apply else "would-recover"
            if apply:
                journal.append({"cycle_id": cycle_id, "pre": dict(record)})
                record["recovered_earliest_write"] = recovered
                record["recovered_started_on_evidence"] = evidence
                if usable:
                    record["recovered_started_on"] = recovered
                    record["recovered_started_on_source"] = RECOVERED_SOURCE
                else:
                    record.pop("recovered_started_on", None)
                    record.pop("recovered_started_on_source", None)
                _write_cycle_record(root, record, exclusive=False)
            rows.append(row)
        counts: Dict[str, int] = {}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        result: Dict[str, Any] = {"status": "applied" if apply else "dry-run", "artifact_root": str(root),
                                  "backup_store": str(store), "backup_runs": runs, "counts": counts,
                                  "cycles": rows}
        if apply and journal:
            journal_dir = producer_dir(root) / "time-recovery"
            _ensure_dir(journal_dir)
            journal_file = journal_dir / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                                          + "-" + os.urandom(3).hex() + ".jsonl")
            _write_exclusive(journal_file, "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
                                                    for entry in journal).encode("utf-8"), 0o600)
            result["journal"] = str(journal_file)
            artifact_locator.rebuild_indexes(root)
        return result
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)


def backfill_cycle_bindings(root: Path, *, apply: bool = False) -> Dict[str, Any]:
    """Add ``started_on`` to readable-layout ``.cycle.json`` bindings that predate it.

    The value follows ``artifact_locator.display_started_on``: a resplit cycle's
    work date (D-79 ``resplit_started_on``, date-only), else the record's own
    ``started_on``, else the sealed manifest's. Nothing is estimated from
    directory names or mtimes: the field is display data and the record wins
    (D-88). A binding that
    already carries a different time is reported as ``conflict`` and left
    alone. Dry run by default; ``apply`` holds the producer admission lock,
    replaces each binding atomically and rebuilds the indexes. Idempotent.
    """
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT) if apply else None
    try:
        rows: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        for campaign_path in artifact_locator.iter_campaign_dirs(root):
            for entry, layout in artifact_locator.iter_cycle_dirs(campaign_path):
                if layout != "readable":
                    continue
                rel = entry.relative_to(root).as_posix()
                try:
                    binding = artifact_locator.read_cycle_binding(entry)
                except artifact_locator.LocatorError as exc:
                    rows.append({"path": rel, "action": "invalid", "detail": exc.code})
                    continue
                if binding is None:
                    rows.append({"path": rel, "action": "no-binding"})
                    continue
                cycle_id = binding["cycle_id"]
                record = artifact_locator.read_cycle_record(root, cycle_id) or {}
                manifest = artifact_locator._read_json(entry / "manifest.json") or {}
                started_on = artifact_locator.display_started_on(record, manifest)
                source = None
                if started_on is not None:
                    # `display_started_on` may have trimmed a placeholder clock; match on the prefix.
                    source = ("record:resplit_started_on" if str(record.get("resplit_started_on")).startswith(started_on)
                              else "record" if str(record.get("started_on")).startswith(started_on) else "manifest")
                row: Dict[str, Any] = {"cycle_id": cycle_id, "path": rel, "source": source,
                                       "started_on": started_on}
                if started_on is None:
                    row["action"] = "missing"
                elif "started_on" in binding:
                    row["action"] = "present" if binding["started_on"] == started_on else "conflict"
                    if row["action"] == "conflict":
                        row["binding_started_on"] = binding["started_on"]
                else:
                    row["action"] = "added" if apply else "would-add"
                    if apply:
                        data = artifact_locator.cycle_binding_bytes(
                            binding["campaign_id"], cycle_id, started_on=started_on)
                        _write_atomic(entry / artifact_locator.CYCLE_BINDING, data)
                rows.append(row)
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        if apply and counts.get("added"):
            artifact_locator.rebuild_indexes(root)
        return {"status": "applied" if apply else "dry-run", "artifact_root": str(root),
                "counts": counts, "cycles": rows}
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)


def list_campaign_summaries(root: Path, *, active_only: bool = True) -> List[Dict[str, Any]]:
    """Pure read-only listing of the root's campaigns for callers that
    must show the agent which work streams already exist (compose).

    Each row uses the same validated campaign event fold as admission.
    This performs no reconcile, history flush, or index heal; writer
    commands (`campaign-close`, `compose`, `campaign-recover`) own repairs.
    """
    root = Path(root)
    rows: List[Dict[str, Any]] = []
    for entry in artifact_locator.iter_campaign_dirs(root):
        try:
            record, _ = artifact_campaign.read_json(root, entry / "campaign.json")
        except artifact_campaign.CampaignError:
            continue
        if not isinstance(record.get("campaign_id"), str):
            continue
        try:
            folded = artifact_campaign.campaign_state(root, entry / "campaign.json", record)
            state = folded.state
            projection_pending = folded.projection_pending
            state_error = None
        except artifact_campaign.CampaignError as exc:
            state, state_error = "invalid", exc.code
            projection_pending = False
        if active_only and state != "active":
            continue
        cycles = record.get("cycles")
        marks: Dict[str, int] = {}
        for cycle_id in cycles if isinstance(cycles, list) else []:
            mark = cycle_disposition(read_cycle_record(root, cycle_id) or {})
            if mark is not None:
                marks[mark["kind"]] = marks.get(mark["kind"], 0) + 1
        rows.append({
            **({"dispositions": marks, "all_set_aside": sum(marks.values()) == len(cycles)} if marks else {}),
            "campaign_id": record["campaign_id"],
            "key": record.get("key"),
            "title": record.get("title"),
            "goal": record.get("goal"),
            "locator": entry.name,
            "state": state,
            **({"state_error": state_error} if state_error else {}),
            "projection_pending": projection_pending,
            "degraded": record.get("degraded") is True,
            "cycle_count": len(cycles) if isinstance(cycles, list) else 0,
            "created_on": str(record.get("created_on") or ""),
        })
    rows.sort(key=lambda row: (row["created_on"], str(row["key"])), reverse=True)
    return rows


def _campaign_degradation(campaign: Mapping[str, Any]) -> Dict[str, Any]:
    if campaign.get("degraded") is True:
        return {"degraded": True, "degraded_reason": campaign.get("degraded_reason", "campaign-unassigned")}
    return {}


def _begin_cycle_record(
    root: Path,
    *,
    route_file: Path,
    capability: str,
    intensity: str,
    node_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    campaign_key: Optional[str] = None,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    parent_cycle_id: Optional[str] = None,
    workflow_group_id: Optional[str] = None,
    workflow_stage_label: Optional[str] = None,
    require_cycle: bool = False,
    shared_reference_pins: Optional[Sequence[Mapping[str, Any]]] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
    jobs: Optional[Path] = None,
    owner_attempt_id: Optional[str] = None,
    resume_only: bool = False,
) -> Dict[str, Any]:
    dispatch_terminal_commit.require_current_cleanup("producer-begin", jobs=jobs)
    root = Path(root).resolve()
    if capability not in ENTRY_CAPABILITIES + STAGE_CAPABILITIES + INTERNAL_CAPABILITIES:
        raise ProducerError("capability-unknown", capability)
    if intensity not in INTENSITIES:
        raise ProducerError("intensity-unknown", intensity)
    resolved_route_file = resolve_route_argument(root, Path(route_file))
    route = load_route(root, resolved_route_file)
    # A sealed proposal survives dispatch; CLI may confirm, never override it.
    for field, supplied in (("campaign_key", campaign_key), ("parent_cycle_id", parent_cycle_id)):
        if field in route and supplied is not None and supplied != route[field]:
            raise ProducerError("route-campaign-selection-conflict", field)
    campaign_key = route.get("campaign_key", campaign_key)
    parent_cycle_id = route.get("parent_cycle_id", parent_cycle_id)
    if campaign_key is not None and (not isinstance(campaign_key, str) or not _KEY_RE.fullmatch(campaign_key)):
        raise ProducerError("campaign-key-invalid", str(campaign_key))
    if parent_cycle_id is not None and not artifact_identity.is_well_formed(parent_cycle_id, "cycle"):
        raise ProducerError("parent-cycle-invalid", str(parent_cycle_id))
    route_capability = route["capability"]
    if capability in ENTRY_CAPABILITIES + INTERNAL_CAPABILITIES and route_capability != capability:
        raise ProducerError("route-capability-mismatch", f"{route_capability}!={capability}")
    if route["effective_intensity"] != intensity:
        raise ProducerError("route-intensity-mismatch", f"{route['effective_intensity']}!={intensity}")
    node = _route_node(route, node_id)
    if route_is_closed(root, route):
        raise ProducerError("route-already-closed", route["route_id"])
    alloc = allocator or artifact_identity.IdAllocator()
    klass = classify_root(root)
    if klass["state"] == "malformed":
        raise ProducerError("cutover-record-malformed", klass["reason"])
    if klass["state"] == "inactive-empty":
        if resume_only:
            raise ProducerError("producer-binding-required", "route-cycle-absent")
        # D-73: bootstrap-first identity. MUST stay above the admission lock at
        # :547 -- activate() acquires the same lock and would self-deadlock.
        activate(root,
                 repository_id=alloc.allocate("repository"),
                 artifact_root_id=alloc.allocate("artifact_root"),
                 activation_kind="bootstrap-empty-root",
                 adopt_existing_identity=True,
                 now=now)
    elif klass["state"] == "inactive-with-legacy":
        fallback = legacy_fallback_state(root, now=now, classification=klass)
        if _fallback_blocks(fallback):
            raise ProducerError("cutover-inactive-fallback-denied",
                                fallback["override"]["reason"] or "override-absent")
        if require_cycle:
            raise ProducerError("cutover-inactive", "activation required before cycle issuance")
        return {
            "status": "legacy-compat",
            "layout": "legacy",
            "route_id": route["route_id"],
            "reason": "cutover-inactive",
            "env": {"AGENT_ARTIFACT_ROOT": str(root)},
            "legacy_fallback": fallback,
        }
    # active, or just bootstrapped: fall through to the existing cycle path.
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    # SD-120 A1: only registered owner/worker contexts participate.  The
    # canonical jobs path and attempt identity come from the runtime; callers
    # do not get a route/owner override surface.
    binding_jobs = Path(jobs or os.environ.get("AGENT_DISPATCH_JOBS", ""))
    if not binding_jobs.is_absolute() or not binding_jobs.is_file():
        binding_jobs = None  # type: ignore[assignment]
    binding_owner = owner_attempt_id or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if node_id is not None:
        binding_owner = os.environ.get("AGENT_DISPATCH_PARENT_ATTEMPT_ID", binding_owner)
    owner_begin = node_id is None
    if binding_jobs is not None and binding_owner:
        try:
            owner_binding = dispatch_terminal_commit.validate_owner_route(
                jobs=binding_jobs, route_file=resolved_route_file, owner_attempt_id=binding_owner)
            existing_open = route_cycle_for(root, route)
            binding_path = dispatch_terminal_commit.producer_binding_path(
                root, owner_binding.route_id, binding_owner)
            if binding_path.exists():
                loaded = dispatch_terminal_commit.load_producer_binding(
                    artifact_root=root, route_id=owner_binding.route_id, owner_attempt_id=binding_owner)
                if existing_open is None or loaded.binding.get("cycle_id") != existing_open.get("cycle_id"):
                    raise dispatch_terminal_commit.TerminalCommitError("transaction-conflict", str(binding_path))
            elif not owner_begin and existing_open is None:
                raise dispatch_terminal_commit.TerminalCommitError("producer-binding-required", str(binding_path))
        except dispatch_terminal_commit.TerminalCommitError as exc:
            raise ProducerError(exc.code, exc.detail) from exc
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        # W7G owns a root-wide resplit from R2 through R3.  Check its atomic
        # claim while holding the same admission mutex used for locator and
        # campaign updates, so begin cannot race the gap before its first
        # journal is durable or lose a campaign cycles[] update.
        resplit_lock = producer_dir(root) / "resplit.lock"
        if resplit_lock.exists() or resplit_lock.is_symlink():
            detail = _read_json(resplit_lock)
            raise ProducerError("resplit-in-progress", json.dumps(detail or {}, sort_keys=True))
        resumable = route_cycle_for(root, route)
        if resumable is not None and resumable.get("deleted_at"):
            resumable = None  # a deleted folder is never resumed; the route's next cycle is a new one
        if resume_only and resumable is None:
            raise ProducerError("producer-binding-required", "route-cycle-absent")
        campaign: Optional[Dict[str, Any]] = None
        parent = None
        campaign_reopen_event_id = None
        requested_selection = None
        if campaign_id:
            campaign = read_campaign(root, campaign_id)
            if campaign is None:
                raise ProducerError("campaign-unknown", campaign_id)
            requested_selection = {"by": "campaign_id", "value": campaign_id}
        elif campaign_key:
            keyed = _campaigns_by_key(root, campaign_key)
            choice = classify_campaign_key(keyed, campaign_key)
            if choice["mode"] == "blocked":
                raise ProducerError(choice["code"], campaign_key)
            campaign = next((row for row in keyed if row.get("campaign_id") == choice.get("campaign_id")), None)
            requested_selection = {"by": "campaign_key", "value": campaign_key}
        if parent_cycle_id:
            parent = read_cycle_record(root, parent_cycle_id)
            if parent is None:
                raise ProducerError("parent-cycle-not-joinable", parent_cycle_id)
            if parent.get("deleted_at") and read_campaign(root, parent["campaign_id"]) is None:
                parent = None
                parent_cycle_id = None
            # §45 D-123: a parent is a reference to any cycle of this root, in any
            # state and any campaign; it selects a campaign only when none was named.
            if campaign is None and parent is not None:
                campaign = read_campaign(root, parent["campaign_id"])
                if campaign is None:
                    raise ProducerError("campaign-unknown", parent["campaign_id"])
                requested_selection = {"by": "parent_cycle", "value": parent_cycle_id}
        import artifact_workflow_groups
        # Ambient context is accepted only for the exact selected campaign;
        # the parent-cycle edge alone never selects or implies a group.
        inherited_group_id = (
            os.environ.get("AGENT_ARTIFACT_WORKFLOW_GROUP_ID")
            if campaign is not None and os.environ.get("AGENT_ARTIFACT_CAMPAIGN_ID") == campaign["campaign_id"]
            else None
        )
        selected_group_id = workflow_group_id or inherited_group_id
        if selected_group_id and owner_begin:
            if campaign is None:
                raise ProducerError("workflow-group-campaign-required")
            if workflow_group_id and inherited_group_id and workflow_group_id != inherited_group_id:
                raise ProducerError("workflow-group-context-conflict", workflow_group_id)
            try:
                early_title = (resumable.get("title") if resumable is not None
                               else _route_naming(route, campaign, title=title, goal=goal, root=root)[1])
                early_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(early_title)
                artifact_workflow_groups.preflight_join_locked(
                    root, campaign["campaign_id"], selected_group_id, early_label,
                    cycle_id=resumable["cycle_id"] if resumable is not None else None)
            except artifact_workflow_groups.WorkflowGroupError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        if workflow_stage_label is not None:
            try:
                artifact_workflow_groups._text(workflow_stage_label, 40, "stage-label-invalid")
            except artifact_workflow_groups.WorkflowGroupError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        if campaign is not None:
            if campaign.get("state") == "satisfied":
                try:
                    reopened = artifact_campaign._reopen_locked(
                        root, _campaign_path(root, campaign["campaign_id"], campaign),
                        route_id=route["route_id"], requested_selection=requested_selection)
                except artifact_campaign.CampaignError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
                campaign_reopen_event_id = reopened.get("event_id")
                campaign = read_campaign(root, campaign["campaign_id"])
            if campaign is None or campaign.get("state") != "active":
                raise ProducerError("campaign-not-active", campaign_id or parent_cycle_id or campaign_key)
            if campaign_key is not None and campaign.get("key") != campaign_key:
                raise ProducerError("campaign-key-mismatch", campaign_key)
        # Idempotent per route: one open cycle per verified lineage (D-120). A
        # continuation resuming an ancestor's open cycle is the same idempotent
        # path with `rebound=True` and an extended audit record.
        if resumable is not None:
            record = resumable
            admission = cycle_route_admission(root, record, route)
            if not admission.allow:
                raise ProducerError(admission.reason, admission.detail)
            bound_campaign = read_campaign(root, record["campaign_id"])
            if bound_campaign is not None and bound_campaign.get("state") == "satisfied" and owner_begin:
                try:
                    reopened = artifact_campaign._reopen_locked(
                        root, _campaign_path(root, bound_campaign["campaign_id"], bound_campaign),
                        route_id=route["route_id"])
                except artifact_campaign.CampaignError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
                campaign_reopen_event_id = reopened.get("event_id")
                bound_campaign = read_campaign(root, record["campaign_id"])
            if bound_campaign is None or bound_campaign.get("state") != "active":
                raise ProducerError("campaign-not-active", record["campaign_id"])
            if ((campaign is not None and campaign["campaign_id"] != record["campaign_id"])
                    or (campaign_key is not None and campaign_key != bound_campaign.get("key"))
                    or (parent_cycle_id is not None and parent_cycle_id != record.get("parent_cycle_id"))):
                raise ProducerError("cycle-campaign-selection-conflict", record["cycle_id"])
            rebound = record.get("route_id") != route["route_id"]
            # D-120: only an owner `begin --route <continuation>` writes the
            # audit trail. A worker `begin --node` judges the same admission
            # (raised above) but never mutates the record.
            if rebound and owner_begin:
                _bind_cycle_route_locked(root, record, route)
            if selected_group_id and owner_begin:
                stage_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(record.get("title"))
                try:
                    artifact_workflow_groups.join_at_begin_locked(
                        root, record["campaign_id"], record["cycle_id"], selected_group_id, stage_label)
                except artifact_workflow_groups.WorkflowGroupError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            if binding_jobs is not None and binding_owner:
                try:
                    dispatch_terminal_commit.publish_producer_binding(
                        artifact_root=root, jobs=binding_jobs, route_file=resolved_route_file,
                        owner_attempt_id=binding_owner, cycle_id=record["cycle_id"],
                        owner_begin=owner_begin)
                except dispatch_terminal_commit.TerminalCommitError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            title_updated = False
            if owner_begin and title is not None:
                # A repeated owner begin is the existing metadata edit surface.
                # Reread after a continuation bind to retain its route audit.
                current = read_cycle_record(root, record["cycle_id"])
                directory = cycle_dir(root, current["campaign_id"], current["cycle_id"], current)
                if current.get("state") == "open" and not (directory / "manifest.json").exists():
                    display_title = _route_naming(route, bound_campaign, title=title, goal=goal, root=root)[1]
                    if current.get("title") != display_title:
                        record = {**current, "title": display_title}
                        _write_cycle_record(root, record, exclusive=False)
                        title_updated = True
            return {
                "status": "resumed", "layout": "cycle", "campaign_id": record["campaign_id"],
                "cycle_id": record["cycle_id"], "producer_id": record["producer_id"],
                "cycle_dir": str(cycle_dir(root, record["campaign_id"], record["cycle_id"], record)),
                "env": _env_for(root, record, route),
                **({"rebound": True} if rebound else {}),
                **({"title_updated": True} if title_updated else {}),
                **({"campaign_reopened": True, "campaign_reopen_event_id": campaign_reopen_event_id}
                   if campaign_reopen_event_id else {}),
                **_campaign_degradation(bound_campaign),
            }
        if campaign is not None:
            artifact_locator.prepare_index_update(root, [campaign["campaign_id"]])
        index = artifact_admission.load_index(root)
        if campaign is None and campaign_key is None:
            campaign = find_campaign_by_key(root, UNASSIGNED_KEY)
        campaign_created = False
        slug, display_title, slug_source, slug_truncated = _route_naming(
            route, campaign, title=title, goal=goal, root=root)
        started_on = _rfc3339(now)
        if campaign is None:
            new_campaign_id = alloc.allocate("campaign")
            while new_campaign_id in index.stable_ids:
                new_campaign_id = alloc.allocate("campaign")
            # The campaign is named from the proposed stream key; the route
            # slug names only this cycle (CONVENTIONS "Campaign or cycle").
            campaign_slug, campaign_title, campaign_slug_source, campaign_slug_truncated = (
                _campaign_naming(campaign_key))
            locator, locator_suffix = artifact_locator.allocate_locator(
                root / "campaigns", started_on, campaign_slug)
            campaign = {
                "schema_version": 1,
                "contract": CONTRACT,
                "campaign_id": new_campaign_id,
                "key": campaign_key or UNASSIGNED_KEY,
                **({"degraded": True, "degraded_reason": "campaign-unassigned"} if campaign_key is None else {}),
                "slug": campaign_slug,
                "title": campaign_title,
                "slug_source": campaign_slug_source,
                "slug_truncated": campaign_slug_truncated,
                "locator": locator,
                "locator_suffix": locator_suffix,
                "goal": (goal or f"{route_capability} cycle output") if campaign_key else "Work stream not proposed",
                "completion_criterion": {"statement": artifact_campaign.DEFAULT_COMPLETION_CRITERION},
                "state": "active",
                "created_on": started_on,
                "cycles": [],
            }
            campaign_created = True
            # The readable locator is persisted before children are created.
            _write_campaign(root, campaign, exclusive=True)
        else:
            # Existing W7 records keep their physical path until Cycle B, but
            # missing display fields are filled from this route for hybrid joins.
            campaign = dict(campaign)
            changed = False
            existing_key = campaign.get("key")
            fill_slug, fill_title, fill_source, fill_truncated = _campaign_naming(
                None if existing_key in (None, UNASSIGNED_KEY) else str(existing_key))
            for key, value in (
                ("slug", fill_slug), ("title", fill_title), ("slug_source", fill_source),
                ("slug_truncated", fill_truncated),
            ):
                if key not in campaign:
                    campaign[key] = value
                    changed = True
            # A campaign promoted out of `_unassigned` by the §37 metadata
            # amendment keeps the reserved placeholder title (the amendment
            # writes key/goal only); every later manifest would seal
            # `campaign.title = "_unassigned"`.  The key is the stream name.
            if existing_key not in (None, UNASSIGNED_KEY) and campaign.get("title") == UNASSIGNED_KEY:
                campaign["title"] = fill_title
                changed = True
            if changed:
                _write_campaign(root, campaign, exclusive=False)
        new_cycle_id = alloc.allocate("cycle")
        while new_cycle_id in index.stable_ids or cycle_record_path(root, new_cycle_id).exists():
            new_cycle_id = alloc.allocate("cycle")
        producer_id = alloc.allocate("producer")
        campaign_path = campaign_dir(root, campaign["campaign_id"], campaign)
        cycle_locator, cycle_locator_suffix = artifact_locator.allocate_locator(
            campaign_path, started_on, slug)
        record = {
            "schema_version": 1,
            "contract": CONTRACT,
            "cycle_id": new_cycle_id,
            "campaign_id": campaign["campaign_id"],
            "producer_id": producer_id,
            "parent_cycle_id": parent_cycle_id,
            **({"parent_cycle_state_at_begin": parent["state"]} if parent is not None else {}),
            "capability": capability,
            "route_capability": route_capability,
            "intensity": intensity,
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_file": str(resolved_route_file.resolve()),
            "node_id": node["id"] if node else None,
            "state": "open",
            "started_on": started_on,
            "sealed_on": None,
            "manifest_digest": None,
            "slug": slug,
            "title": display_title,
            "slug_source": slug_source,
            "slug_truncated": slug_truncated,
            "locator": cycle_locator,
            "locator_suffix": cycle_locator_suffix,
        }
        if shared_reference_pins is not None:
            record["shared_reference_pins"] = [dict(pin) for pin in shared_reference_pins]
        target = campaign_path / cycle_locator
        if target.exists():
            raise ProducerError("cycle-dir-exists", str(target))
        # Order: durable record first (crash before dir => recover drops the
        # record), then the folder.  Nothing here is visible to the index until
        # finalize's manifest commit point.
        _write_cycle_record(root, record, exclusive=True)
        _ensure_dir(target / "artifacts")
        campaign["cycles"] = list(campaign.get("cycles", [])) + [new_cycle_id]
        _write_campaign(root, campaign, exclusive=False)
        _write_cycle_binding(target, campaign["campaign_id"], new_cycle_id, started_on=started_on)
        if binding_jobs is not None and binding_owner:
            try:
                dispatch_terminal_commit.publish_producer_binding(
                    artifact_root=root, jobs=binding_jobs, route_file=resolved_route_file,
                    owner_attempt_id=binding_owner, cycle_id=new_cycle_id,
                    owner_begin=owner_begin)
            except dispatch_terminal_commit.TerminalCommitError as exc:
                raise ProducerError(exc.code, exc.detail) from exc
        artifact_locator.update_indexes(root, [campaign["campaign_id"]])
        if selected_group_id and owner_begin:
            stage_label = workflow_stage_label or artifact_workflow_groups.stage_label_from_title(display_title)
            try:
                artifact_workflow_groups.join_at_begin_locked(
                    root, campaign["campaign_id"], new_cycle_id, selected_group_id, stage_label)
            except artifact_workflow_groups.WorkflowGroupError as exc:
                # The issued cycle is resumable by this exact route. Its next
                # begin retries the same group append under admission lock.
                raise ProducerError(exc.code, exc.detail) from exc
        return {
            "status": "begun", "layout": "cycle", "campaign_id": campaign["campaign_id"],
            "cycle_id": new_cycle_id, "producer_id": producer_id, "cycle_dir": str(target),
            "campaign_created": campaign_created, "env": _env_for(root, record, route),
            **({"campaign_reopened": True, "campaign_reopen_event_id": campaign_reopen_event_id}
               if campaign_reopen_event_id else {}),
            **_campaign_degradation(campaign),
        }
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _observe_after_begin(root: Path, result: Mapping[str, Any]) -> None:
    """§45 D-124: an owner `begin` has the cycles it builds on looked at (detached, once per interval).

    The parent the new cycle names and the cycle a continuing route resumes may have changed since
    they closed.  The launcher never raises and never waits; this must not make `begin` slower or fail."""
    try:
        import artifact_checkpoint_trigger

        cycle_id = result.get("cycle_id")
        record = read_cycle_record(root, cycle_id) if cycle_id else None
        wanted = [record.get("parent_cycle_id")] if record else []
        if result.get("status") == "resumed":
            wanted.append(cycle_id)
        for observed in dict.fromkeys(item for item in wanted if item):
            artifact_checkpoint_trigger.launch(trigger="begin", key=f"begin-{observed}",
                                               artifact_root=str(root), cycle_id=observed)
    except Exception:  # noqa: BLE001 -- observing other cycles never fails a begin
        pass


def begin(root: Path, **kwargs: Any) -> Dict[str, Any]:
    if kwargs.get("node_id") is None:
        reconcile_root(root)  # §45 D-126: what was moved or removed by hand is found before a cycle is begun
    result = _begin_cycle_record(root, **kwargs)
    if kwargs.get("node_id") is None and result.get("layout") == "cycle":
        _observe_after_begin(Path(root).resolve(), result)
        deliver_pending_history(root)
    if result.get("title_updated"):
        # The existing publisher rereads the current open manifest under the
        # admission -> checkpoint lock order. No checkpoint scan or payload
        # rehash is needed for a title-only change.
        artifact_cycle_titles.emit_after_checkpoint(root, {"cycle_id": result["cycle_id"]})
    if (kwargs.get("node_id") is None and result.get("layout") == "cycle"
            and kwargs.get("capability") == "autopilot-spec"):
        # The admission lock has been released. Standard+ seeds before a review
        # worker can write verdict.json. Direct has no preceding review worker;
        # its transaction selects the seed through the existing --spec-root.
        route = load_route(Path(root).resolve(), resolve_route_argument(Path(root).resolve(), Path(kwargs["route_file"])))
        if route.get("spec_touch") and route["effective_intensity"] != "direct":
            import importlib.util
            module_path = Path(__file__).with_name("spec-transaction.py")
            spec = importlib.util.spec_from_file_location("spec_transaction_preseed", module_path)
            transaction = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(transaction)
            transaction.preseed_owner_cycle(Path(root).resolve(), Path(result["cycle_dir"]), route=route)
    return result


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _media_type(rel: str) -> str:
    return MEDIA_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def _bucket_type(rel: str) -> str:
    first = rel.split("/", 1)[0]
    return BUCKET_TYPES.get(first, "file")


def _unmanifestable_reason(rel: str) -> Optional[str]:
    """Why a relocated legacy file cannot carry a D-6 locator (None when it can)."""
    for part in rel.split("/"):
        if part.startswith("."):
            return "hidden-component"
        if not artifact_manifest._LOCATOR_COMPONENT_RE.match(part):
            return "invalid-component"
    return None


_TEMPORARY_FILE_SUFFIXES = (".pyc", ".swp", "~", ".tmp", ".part")


def _outside_inclusion_rule(rel: str) -> bool:
    """§45 D-123's one inclusion rule: what a cycle's manifest never lists.

    A hidden component (a dot-prefixed one, which covers `.git/`, `.pytest_cache/`
    and Emacs `.#*` locks), `__pycache__/`, `*.pyc`, editor temporaries (`*.swp`,
    `*~`) and `*.tmp`/`*.part` are runtime residue, not output.  `_internal/` and
    other support paths, binary files and large files are output.  Symbolic links
    are the other half of the rule and are judged by `lstat`, not by name.
    """
    parts = rel.split("/")
    return (any(part.startswith(".") or part == "__pycache__" for part in parts)
            or parts[-1].endswith(_TEMPORARY_FILE_SUFFIXES))


def _enumerate_output(directory: Path, *, exclude_hidden: bool = False,
                      excluded: Optional[List[str]] = None,
                      exclude_symlinks: bool = False,
                      excluded_symlinks: Optional[List[str]] = None) -> Tuple[List[Tuple[str, bytes]], List[str]]:
    """Regular files under `artifacts/` that the inclusion rule keeps.

    Finalize and the later refreshes share one rule (`_outside_inclusion_rule`
    plus symbolic links): a path outside it is left out of the manifest and
    reported through `excluded`, a link through `excluded_symlinks`; it never
    fails the close and needs no flag.  A link is only lstat-ed, never followed
    or read.  `exclude_symlinks` is accepted for old callers and changes
    nothing.  `exclude_hidden` additionally leaves out files whose path cannot
    be a D-6 locator (a component longer than the locator limit or outside its
    alphabet), reported through `excluded` (W7E retrospective seal of relocated
    legacy trees)."""
    paths, violations = _output_paths(directory, exclude_hidden=exclude_hidden, excluded=excluded,
                                      excluded_symlinks=excluded_symlinks)
    if violations:
        return [], violations
    return [(rel, entry.read_bytes()) for rel, entry in paths], violations


def _output_paths(directory: Path, *, exclude_hidden: bool = False,
                  excluded: Optional[List[str]] = None,
                  excluded_symlinks: Optional[List[str]] = None) -> Tuple[List[Tuple[str, Path]], List[str]]:
    """`_enumerate_output` without its reads: the kept `(path, file)` list and the violations.

    Nothing is opened here (links and kinds are `lstat`-ed), so a first close can list a cycle
    before the admission lock and read each file only once, in chunks."""
    paths: List[Tuple[str, Path]] = []
    violations: List[str] = []
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    for entry in _walk_files(directory):
        rel = entry.relative_to(directory).as_posix()
        if rel == artifact_locator.CYCLE_BINDING:
            # Machine-owned locator binding, not user output or manifest data.
            continue
        if os.path.islink(str(entry)):
            if excluded_symlinks is not None:
                excluded_symlinks.append(rel)
            continue
        if rel.startswith("artifacts/") and _outside_inclusion_rule(rel):
            if excluded is not None:
                excluded.append(rel)
            continue
        if not entry.is_file():
            violations.append(f"non-regular-file:{rel}")
            continue
        if not rel.startswith("artifacts/"):
            violations.append(f"file-outside-artifacts:{rel}")
            continue
        if exclude_hidden and _unmanifestable_reason(rel) is not None:
            if excluded is not None:
                excluded.append(rel)
            continue
        locator = artifact_manifest.validate_locator_path(rel)
        if not locator.ok:
            violations.extend(f"{v.code}:{rel}" for v in locator.violations)
            continue
        paths.append((rel, entry))
    # Validate the whole collection before reading payload bytes. A rejected
    # path must not silently vanish, or surface only after manifest allocation.
    return ([], violations) if violations else (paths, violations)


def _is_support_locator(rel: str) -> bool:
    """A path through a CORE §3 support name that is not a cycle bucket (`_internal/`, `shards/`)."""
    return any(part in SUPPORT_SEGMENTS for part in rel.split("/")[1:])


def _output_placements(root: Path, record: Mapping[str, Any]) -> List[Dict[str, str]]:
    path = producer_dir(root) / "bucket-placements" / f"{record['cycle_id']}.json"
    rows = (_read_json(path) or {}).get("moves", [])
    return [row for row in rows if isinstance(row, dict) and all(
        isinstance(row.get(key), str) and row[key].startswith("artifacts/")
        and not any(part in {"", ".", ".."} for part in row[key].split("/"))
        for key in ("from", "to"))]


def resolve_placed_output(path: Path) -> Path:
    """Follow this cycle's recorded move without changing sealed evidence bytes."""
    path = Path(path)
    if not path.is_absolute() or os.path.lexists(path):
        return path
    for output in path.parents:
        if output.name != "artifacts":
            continue
        directory = output.parent
        binding = _read_json(directory / ".cycle.json") or {}
        if not isinstance(binding, dict):
            continue
        cid = binding.get("cycle_id", "")
        if not artifact_identity.is_well_formed(cid, "cycle"):
            continue
        for root in list(directory.parents)[:4]:
            record = read_cycle_record(root, cid)
            if record is None:
                continue
            if cycle_dir(root, record["campaign_id"], cid, record) != directory:
                continue
            relative = path.relative_to(directory).as_posix()
            placed = _placed_locator(relative, _output_placements(root, record))
            candidate = directory / placed
            if candidate.resolve().is_relative_to(output.resolve()) and candidate.exists():
                return candidate
    return path


def placed_output_proof(path: Path, *, route_id: str, route_hash: str) -> Optional[Dict[str, str]]:
    """Read a route-bound move and current manifest digest for terminal repair.

    The manifest digest describes the file *now*. Neither it nor the placement
    ledger claims to know the file's bytes at the worker's earlier handoff.
    """
    origin = Path(path)
    if not origin.is_absolute() or os.path.lexists(origin):
        return None
    for output in origin.parents:
        if output.name != "artifacts":
            continue
        directory = output.parent
        binding = _read_json(directory / ".cycle.json") or {}
        if not isinstance(binding, dict):
            continue
        cid = binding.get("cycle_id", "")
        if not artifact_identity.is_well_formed(cid, "cycle"):
            continue
        for root in list(directory.parents)[:4]:
            record = read_cycle_record(root, cid)
            if (record is None or record.get("route_id") != route_id
                    or record.get("route_hash") != route_hash
                    or record.get("campaign_id") != binding.get("campaign_id")
                    or cycle_dir(root, record["campaign_id"], cid, record) != directory):
                continue
            placement_path = producer_dir(root) / "bucket-placements" / f"{cid}.json"
            manifest_path = producer_dir(root) / "open-manifests" / f"{cid}.json"
            try:
                placement_bytes = placement_path.read_bytes()
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes)
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            checkpoint = _read_json(checkpoint_state_path(root, cid)) or {}
            if not isinstance(checkpoint, dict):
                continue
            if (checkpoint.get("manifest_id") != manifest.get("manifest_id")
                    or checkpoint.get("manifest_revision_id") != manifest.get("manifest_revision_id")
                    or checkpoint.get("route_id") != route_id):
                continue
            relative = origin.relative_to(directory).as_posix()
            try:
                moves = _output_placements(root, record)
            except (AttributeError, TypeError):
                continue
            applicable = [row for row in moves if relative == row["from"]
                          or relative.startswith(row["from"] + "/")]
            if len(applicable) != 1:
                continue
            placed = _placed_locator(relative, moves)
            if placed == relative or not placed:
                continue
            target = directory / placed
            try:
                canonical_output = output.resolve()
                canonical_target = target.resolve(strict=True)
                canonical_target.relative_to(canonical_output)
                if canonical_target != target or not target.is_file() or not os.access(target, os.R_OK):
                    continue
                content = target.read_bytes()
            except (OSError, ValueError):
                continue
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            revisions = [row for row in (manifest.get("artifact_revisions") or [])
                         if isinstance(row, dict)
                         and isinstance(row.get("locator"), dict)
                         and row["locator"].get("path") == placed
                         and row.get("content_digest") == digest
                         and row.get("byte_size") == len(content)
                         and isinstance(row.get("provenance"), dict)
                         and row["provenance"].get("producer_route_id") == route_id]
            routes = [row for row in (manifest.get("routes") or [])
                      if isinstance(row, dict) and row.get("route_id") == route_id
                      and row.get("route_hash") == route_hash]
            cycle = manifest.get("cycle")
            if (not isinstance(cycle, dict)
                    or cycle.get("cycle_id") != cid
                    or cycle.get("campaign_id") != record["campaign_id"]
                    or manifest.get("manifest_id") is None
                    or not manifest.get("manifest_revision_id")
                    or len(revisions) != 1 or len(routes) != 1):
                continue
            return {
                "origin": str(origin), "destination": str(target),
                "cycle_id": cid, "manifest_revision_id": manifest["manifest_revision_id"],
                "artifact_revision_id": revisions[0]["artifact_revision_id"],
                "current_content_digest": digest,
                "placement_sha256": hashlib.sha256(placement_bytes).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            }
    return None


def _placed_locator(locator: Optional[str], moved: Sequence[Mapping[str, str]]) -> Optional[str]:
    if not locator:
        return locator
    relative = locator if locator.startswith("artifacts/") else "artifacts/" + locator
    for row in moved:
        if relative == row["from"] or relative.startswith(row["from"] + "/"):
            return row["to"] + relative[len(row["from"]):]
    return locator


def official_spec_primary(route: Mapping[str, Any], names: Sequence[str],
                          preferred: Optional[str] = None) -> Optional[str]:
    """An actual root/scoped component PRD, never a snapshot or terminal report."""
    if route.get("capability") != "autopilot-spec":
        return None
    components = spec_scope_components(
        scope for node in route.get("nodes", []) for scope in node.get("write_scope", []))
    available = set(names)
    candidates = []
    if not components:
        candidates.append("artifacts/spec/prd.md")
    if components is None:
        components = tuple(sorted({Path(name).parts[2] for name in names
            if len(Path(name).parts) == 4 and name.startswith("artifacts/spec/")
            and name.endswith("/prd.md") and Path(name).parts[2] != "_internal"}))
    candidates.extend(f"artifacts/spec/{component}/prd.md" for component in components)
    actual = [name for name in candidates if name in available]
    return preferred if preferred in actual else next(iter(actual), None)


def official_spec_primary_path(root: Path, record: Mapping[str, Any], route: Mapping[str, Any],
                               preferred: Optional[Path] = None) -> Optional[Path]:
    """Read the known canonical paths in this exact cycle; no recursive report search."""
    if route.get("capability") != "autopilot-spec":
        return None
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record).resolve()
    directory.relative_to(Path(root).resolve())
    spec = directory / "artifacts/spec"
    components = spec_scope_components(
        scope for node in route.get("nodes", []) for scope in node.get("write_scope", []))
    paths = [spec / "prd.md"]
    if components is None:
        if spec.is_dir() and not spec.is_symlink():
            paths.extend(p / "prd.md" for p in spec.iterdir() if p.is_dir() and not p.is_symlink()
                         and p.name != "_internal")
    else:
        paths.extend(spec / component / "prd.md" for component in components)
    names = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                continue
            path.resolve(strict=True).relative_to(directory / "artifacts")
            names.append(path.relative_to(directory).as_posix())
        except (OSError, ValueError):
            continue
    try:
        wanted = preferred.relative_to(directory).as_posix() if preferred is not None else None
    except ValueError:
        wanted = None
    selected = official_spec_primary(route, names, wanted)
    return directory / selected if selected is not None else None


def _choose_primary(rows: Sequence[Tuple[str, bytes]], primary: Optional[str],
                    support: Sequence[str] = (), route: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    # A `support` row is attached evidence, not this cycle's output, so it is never
    # auto-nominated as the primary artifact -- an explicit `primary` still wins.
    # Support-material paths are skipped the same way while any durable output
    # exists; a cycle holding nothing else keeps its first row so a completed
    # cycle still carries the primary role its outcome criterion requires.
    support_set = set(support)
    names = [rel for rel, _ in rows if rel not in support_set]
    if primary:
        candidate = primary if primary.startswith("artifacts/") else "artifacts/" + primary
        if candidate not in names:
            shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
            raise ProducerError(
                "primary-artifact-missing",
                f"{primary} (expected a cycle-relative path under artifacts/; cycle outputs: {shown or 'none'})",
            )
        return candidate
    durable = [rel for rel in names if not _is_support_locator(rel)] or names
    official = official_spec_primary(route or {}, durable)
    if official is not None:
        return official
    for wanted in PRIMARY_CANDIDATES:
        for rel in durable:
            if rel.endswith("/" + wanted) or rel == "artifacts/" + wanted:
                return rel
    documents = [rel for rel in durable if Path(rel).suffix.lower() in {".md", ".html", ".htm"}]
    return next(iter(documents or durable), None)


def _shared_pin_reference_path(root: Path, kind: str, ref_id: str) -> Path:
    return _reference_path(root, kind, ref_id)


def _shared_pin_revision_path(root: Path, kind: str, ref_id: str, rrev_id: str) -> Path:
    return Path(root) / "shared" / kind / ref_id / "revisions" / rrev_id / "revision.json"


def _resolve_shared_pin(
    root: Path, pin: Mapping[str, Any], provenance_fn: Optional[Any] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """D-78-a: resolve one `shared_reference_pins[]` entry from disk.

    Returns (shared_reference_row, shared_reference_revision_row). Raises a
    typed `ProducerError` when the pin is malformed or does not resolve --
    the finalize caller must never silently drop a pin (D-78-a: unresolved or
    digest-mismatched pins hold the seal, they do not fall back to `[]`).
    """
    if not isinstance(pin, Mapping):
        raise ProducerError("shared-reference-pin-invalid", "pin-not-an-object")
    kind = pin.get("kind")
    ref_id = pin.get("shared_reference_id")
    rrev_id = pin.get("shared_reference_revision_id")
    expected_digest = pin.get("content_digest")
    if kind not in SHARED_KINDS:
        raise ProducerError("shared-reference-pin-invalid", f"kind:{kind}")
    if not isinstance(ref_id, str) or not artifact_identity.is_well_formed(ref_id, "shared_reference"):
        raise ProducerError("shared-reference-pin-invalid", f"shared_reference_id:{ref_id}")
    if not isinstance(rrev_id, str) or not artifact_identity.is_well_formed(rrev_id, "shared_reference_revision"):
        raise ProducerError("shared-reference-pin-invalid", f"shared_reference_revision_id:{rrev_id}")
    if expected_digest is not None and not isinstance(expected_digest, str):
        raise ProducerError("shared-reference-pin-invalid", "content_digest")
    reference = _read_json(_shared_pin_reference_path(root, kind, ref_id))
    if reference is None:
        raise ProducerError("shared-reference-pin-unresolved", f"reference:{kind}:{ref_id}")
    revision = _read_json(_shared_pin_revision_path(root, kind, ref_id, rrev_id))
    if revision is None:
        raise ProducerError("shared-reference-pin-unresolved", f"revision:{kind}:{ref_id}:{rrev_id}")
    content_digest = revision.get("content_digest")
    if not isinstance(content_digest, str):
        raise ProducerError("shared-reference-pin-unresolved", f"revision-digest:{kind}:{ref_id}:{rrev_id}")
    if expected_digest is not None and expected_digest != content_digest:
        raise ProducerError("shared-reference-pin-digest-mismatch", f"{ref_id}:{rrev_id}")
    ref_row = {
        "shared_reference_id": ref_id, "kind": reference.get("kind"),
        "title": str(reference.get("title") or ""),
    }
    rev_row: Dict[str, Any] = {
        "shared_reference_revision_id": rrev_id, "shared_reference_id": ref_id,
        "content_digest": content_digest, "updated_at": revision.get("created_on"),
    }
    if provenance_fn is not None:
        rev_row["provenance"] = provenance_fn(content_digest)
    return ref_row, rev_row


def validate_shared_reference_pins(root: Path, pins: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Pure D-78-a validation (no manifest, no provenance) -- R2 calls this
    before finalize so an unresolved pin holds early. Returns a list of
    `{"index", "code", "detail"}` violation rows; empty means every pin
    resolves."""
    root = Path(root).resolve()
    violations: List[Dict[str, Any]] = []
    for i, pin in enumerate(pins):
        try:
            _resolve_shared_pin(root, pin)
        except ProducerError as exc:
            violations.append({"index": i, "code": exc.code, "detail": exc.detail})
    return violations


def _cycle_relative_primary(primary: Optional[str], directory: Path) -> Optional[str]:
    """Map an absolute `--primary` that points inside this cycle's `artifacts/`
    onto the cycle-relative form `_choose_primary` expects. Anything else is
    returned unchanged so the existing `primary-artifact-missing` verdict still
    names what the caller passed (2026-09-16 DX report: an absolute path failed
    with no hint that only cycle-relative locators are accepted)."""
    if not primary or not os.path.isabs(primary):
        return primary
    try:
        rel = Path(primary).resolve().relative_to(Path(directory).resolve())
    except (OSError, ValueError):
        return primary
    rel_posix = rel.as_posix()
    return rel_posix if rel_posix.startswith("artifacts/") else primary


def build_manifest(
    root: Path,
    record: Mapping[str, Any],
    route: Mapping[str, Any],
    rows: Sequence[Tuple[str, bytes]],
    *,
    state: str,
    primary: Optional[str],
    allow_open_route: bool,
    allocator: artifact_identity.IdAllocator,
    now: Optional[float],
    abandon_reason: Optional[str] = None,
    support_locators: Sequence[str] = (),
    reserved: Optional["InterimReservation"] = None,
    interim: bool = False,
    facts: Optional[Sequence[Tuple[str, str, int]]] = None,
) -> Dict[str, Any]:
    """Build the D-6 cycle document.

    `facts` rows are `(locator, content_digest, byte_size)`; when absent they are
    computed from `rows`.  `reserved` carries the IDs an open-cycle checkpoint
    already published, so the sealed document keeps them.  `interim` builds that
    checkpoint document itself: `cycle.state` is `open`, with no cycle or route
    terminal event and no route-closure requirement.
    """
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is None:
        raise ProducerError("campaign-unknown", record["campaign_id"])
    if facts is None:
        facts = [(rel, _digest(data), len(data)) for rel, data in rows]
    man_id = (reserved.manifest_id if reserved is not None and reserved.manifest_id
              else allocator.allocate("manifest"))
    mrev_id = allocator.allocate("manifest_revision")
    when = _rfc3339(now)
    # `support_locators` are `artifacts/`-relative locators the caller marks as
    # attached evidence rather than cycle output (W7G D-79 relocates lump-external
    # loose files into a cycle this way). Empty by default, so an ordinary cycle's
    # manifest bytes are unchanged.
    support_rels = {"artifacts/" + rel.lstrip("/") for rel in support_locators}
    primary_rel = _choose_primary([(rel, None) for rel, _d, _s in facts], primary,
                                  support=support_rels, route=route)

    def provenance(digest: str, recorded_in: Optional[str] = None) -> Dict[str, Any]:
        return {
            "source_manifest_id": man_id, "source_revision_id": recorded_in or mrev_id,
            "producer_route_id": route["route_id"], "algorithm_version": ALGORITHM_VERSION,
            "schema_version": 1, "source_digest": digest,
        }

    artifacts: List[Dict[str, Any]] = []
    revisions: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for rel, digest, byte_size in facts:
        # A path the open-cycle checkpoint already published keeps its artifact
        # ID; its revision ID is kept only while the content is unchanged.
        kept = reserved.artifacts.get(rel) if reserved is not None else None
        art_id = kept.artifact_id if kept else allocator.allocate("artifact")
        # A revision already reserved for this content (current, or earlier and
        # returned to) keeps its ID, provenance and recording event, so its rows
        # are identical in every interim document and the sealed one.
        same = reserved.revision_for(rel, digest) if reserved is not None else None
        arev_id = same.artifact_revision_id if same else allocator.allocate("artifact_revision")
        revision_provenance = provenance(digest, same.recorded_in if same else None)
        inner = rel[len("artifacts/"):]
        artifacts.append({
            "artifact_id": art_id, "cycle_id": record["cycle_id"],
            "role": "support" if rel in support_rels else ("primary" if rel == primary_rel else "output"),
            "type": _bucket_type(inner), "capability": record["capability"], "title": inner,
        })
        revisions.append({
            "artifact_revision_id": arev_id, "artifact_id": art_id, "revision_sequence": 1,
            "content_digest": digest, "byte_size": byte_size, "media_type": _media_type(rel),
            "locator": {"kind": "cycle-relative", "path": rel}, "provenance": revision_provenance,
        })
        reuse_event = same is not None and same.event_id and same.stream_id and same.recorded_at
        events.append({
            "event_id": same.event_id if reuse_event else allocator.allocate("event"),
            "stream_id": same.stream_id if reuse_event else allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": art_id,
            "actor": {"kind": "producer", "id": record["producer_id"]},
            "recorded_at": same.recorded_at if reuse_event else when,
            "provenance": revision_provenance, "evidence_ids": [], "payload": {"locator": rel},
        })
    cycle_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    routes_row = {
        "artifact_root_id": identity.artifact_root_id, "route_id": route["route_id"],
        "route_hash": route["route_hash"], "terminal_marker": "pending",
        "terminal_evidence_id": "",
    }
    closed = False if interim else route_is_closed(root, route)
    if not closed and not allow_open_route and not interim:
        raise ProducerError(
            "route-not-closed",
            f"{route['route_id']}: required order: complete -> close -> finalize -> admit-shared; "
            "complete the terminal node using verified cycle-local evidence, then close the route",
        )
    # D-6: a `completed` cycle must bind a route.terminal.recorded event, which
    # only exists once the route is closed.  Sealing an open route therefore
    # records a provisional `active` cycle (lineage committed, completion not
    # claimed); `abandoned` needs no terminal evidence.
    if interim:
        cycle_state = artifact_manifest.INTERIM_CYCLE_STATE
    elif state == "abandoned":
        cycle_state = "abandoned"
    elif closed:
        cycle_state = "completed"
    else:
        cycle_state = "active"
    if cycle_state not in ("active", artifact_manifest.INTERIM_CYCLE_STATE):
        payload = {"abandon_reason": abandon_reason} if cycle_state == "abandoned" else {}
        events.append({
            "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": f"cycle.{cycle_state}", "target_id": record["cycle_id"],
            "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": payload,
        })
    if closed and cycle_state == "completed":
        terminal_event_id = allocator.allocate("event")
        events.append({
            "event_id": terminal_event_id, "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "route.terminal.recorded", "target_id": record["cycle_id"],
            "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": {},
        })
        routes_row["terminal_evidence_id"] = terminal_event_id
    # D-78-a: pins are the sole source of shared_references[]/shared_reference_revisions[].
    # No pins => both stay `[]` and the manifest bytes are unchanged from before this feature.
    shared_references: List[Dict[str, Any]] = []
    shared_reference_revisions: List[Dict[str, Any]] = []
    seen_shared_reference_ids: set = set()
    for pin in record.get("shared_reference_pins") or []:
        ref_row, rev_row = _resolve_shared_pin(root, pin, provenance)
        if ref_row["shared_reference_id"] not in seen_shared_reference_ids:
            shared_references.append(ref_row)
            seen_shared_reference_ids.add(ref_row["shared_reference_id"])
        shared_reference_revisions.append(rev_row)
    document = {
        "schema_version": 2, "manifest_kind": "artifact.cycle",
        "manifest_id": man_id, "manifest_revision_id": mrev_id,
        "repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id,
        "campaign": {
            "campaign_id": campaign["campaign_id"], "goal": str(campaign.get("goal", "")),
            "completion_criterion": {"statement": str((campaign.get("completion_criterion") or {}).get("statement", ""))},
            "title": str(campaign.get("title", "")), "state": str(campaign.get("state", "active")),
        },
        "cycle": {
            "cycle_id": record["cycle_id"], "campaign_id": campaign["campaign_id"],
            "parent_cycle_id": record.get("parent_cycle_id"),
            "started_on": record["started_on"], "input_digest": _digest(_canonical({
                # D-120 fixed-input boundary: the begin route's identity, not
                # the route sealing the cycle (`route`, which may be a
                # continuation's rebound lineage extension).
                "route_id": record["route_id"], "route_hash": record["route_hash"],
                "capability": record["capability"], "intensity": record["intensity"],
            })),
            "outcome_criterion": {"required_artifact_roles": ["primary"] if facts else [], "decision_required": False},
            "state": cycle_state,
        },
        "artifacts": artifacts, "artifact_revisions": revisions,
        "shared_references": shared_references, "shared_reference_revisions": shared_reference_revisions,
        "routes": [routes_row], "events": events,
        "producer": {
            "producer_id": record["producer_id"], "contract_version": artifact_manifest.CONTRACT_VERSION,
            "source_revision": f"{record['capability']}/{record['intensity']}/{ALGORITHM_VERSION}",
        },
    }
    if closed and cycle_state == "completed":
        # D-120: the route sealing this cycle (`route`, R) may be a
        # continuation the begin record's own `route_file` never names --
        # rebind the terminal evidence from R's own canonical file, not the
        # begin route's.
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_lineage.canonical_route_path(root, route["route_id"]),
            expected_root_id=identity.artifact_root_id
        )
        if sealed_route.get("route_hash") != route["route_hash"]:
            raise ProducerError("route-hash-drift", route["route_id"])
        try:
            document = artifact_lifecycle._derive_terminal_evidence(document, binding, sealed_route)
        except artifact_lifecycle.LifecycleError as exc:
            raise ProducerError(exc.code, exc.detail)
    return document


# ---------------------------------------------------------------------------
# open-cycle checkpoint: the interim manifest
# ---------------------------------------------------------------------------
#
# A consumer that mirrors artifacts (Cairn) can only read a sealed cycle's
# `manifest.json`.  A checkpoint publishes the same closed D-6 document for a
# cycle that is still open -- `cycle.state` is `open` -- under
# `.runtime/artifact-producer/v1/open-manifests/<cyc>.json`.
#
# IDs come from one cumulative reservation ledger,
# `checkpoints/<cyc>.ids.json`: every locator a checkpoint ever published keeps
# its artifact ID there, even while a later checkpoint leaves the file out
# (grown past a size limit, briefly missing), and a revision keeps its ID,
# provenance and recording event while its digest is unchanged.  Every later
# checkpoint and the final `finalize` assign IDs from that ledger, so a
# consumer row survives the seal.  Sealing removes the interim files.
# `checkpoints/<cyc>.json` is bookkeeping only (interval, last result, digest
# cache).

OPEN_MANIFEST_DIR = "open-manifests"
CHECKPOINT_DIR = "checkpoints"
CHECKPOINT_TRIGGERS = ("explicit", "stage-complete", "supervisor-poll", "turn-end", "begin")
CHECKPOINT_INTERVAL_ENV = "AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL"
CHECKPOINT_MIN_INTERVAL_SECONDS = 900.0
# An automatic trigger never publishes a first interim document for a cycle
# whose files have not changed for this long: that cycle is not live work.
CHECKPOINT_STALE_SECONDS = 24 * 3600.0
CHECKPOINT_FINALIZE_LOCK_SECONDS = 60.0
RESERVATION_SCHEMA_VERSION = 1
# Earlier revisions a locator keeps in the ledger, so content that returns to
# an earlier digest returns to that revision's ID.
RESERVATION_HISTORY_LIMIT = 16
# A route is finalized by the release it was sealed to.  Only a release that
# carries this file keeps interim IDs at the seal, so a cycle whose route is
# sealed to an older release publishes no interim document.
INTERIM_SUPPORT_MARKER = "utilities/artifact_checkpoint_trigger.py"
# Weights, archives, and array dumps are not something a reader opens; they
# stay on disk and are declared only by the sealed manifest.
CHECKPOINT_EXCLUDED_SUFFIXES = frozenset({
    ".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".onnx", ".h5", ".hdf5", ".pkl",
    ".pickle", ".joblib", ".npy", ".npz", ".tflite", ".pb", ".tar", ".zip", ".gz", ".tgz",
    ".xz", ".bz2", ".7z", ".zst", ".tfrecord", ".arrow", ".parquet", ".lmdb", ".mdb",
})
_STALE_TEMP_SECONDS = 3600.0


@dataclass(frozen=True)
class CheckpointLimits:
    max_walk_entries: int = 20000
    max_files: int = 2000
    max_total_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 32 * 1024 * 1024

    def to_payload(self) -> Dict[str, int]:
        return {"max_walk_entries": self.max_walk_entries, "max_files": self.max_files,
                "max_total_bytes": self.max_total_bytes, "max_file_bytes": self.max_file_bytes}


@dataclass(frozen=True)
class ReservedRevision:
    artifact_id: str
    artifact_revision_id: str
    content_digest: str
    # The manifest revision that first recorded this artifact revision and its
    # `artifact.revision.recorded` event; reused while the digest is unchanged.
    recorded_in: Optional[str] = None
    event_id: Optional[str] = None
    stream_id: Optional[str] = None
    recorded_at: Optional[str] = None


@dataclass(frozen=True)
class InterimReservation:
    manifest_id: Optional[str]
    artifacts: Dict[str, ReservedRevision]
    dropped: int = 0
    # locator -> content_digest -> an earlier revision of the same artifact
    history: Dict[str, Dict[str, ReservedRevision]] = field(default_factory=dict)

    def revision_for(self, locator: str, digest: str) -> Optional[ReservedRevision]:
        current = self.artifacts.get(locator)
        if current is not None and current.content_digest == digest:
            return current
        return self.history.get(locator, {}).get(digest) if current is not None else None


def _without_event_reuse(reserved: InterimReservation) -> InterimReservation:
    """The same IDs, with every reused provenance/event field dropped."""
    def bare(row: ReservedRevision) -> ReservedRevision:
        return replace(row, recorded_in=None, event_id=None, stream_id=None, recorded_at=None)
    return InterimReservation(
        reserved.manifest_id, {loc: bare(row) for loc, row in reserved.artifacts.items()}, reserved.dropped,
        {loc: {d: bare(row) for d, row in rows.items()} for loc, rows in reserved.history.items()},
    )


def open_manifest_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / OPEN_MANIFEST_DIR / f"{cycle_id}.json"


def checkpoint_state_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return producer_dir(root) / CHECKPOINT_DIR / f"{cycle_id}.json"


def reservation_path(root: Path, cycle_id: str) -> Path:
    return checkpoint_state_path(root, cycle_id).with_suffix(".ids.json")


def _checkpoint_lock_path(root: Path, cycle_id: str) -> Path:
    return checkpoint_state_path(root, cycle_id).with_suffix(".lock")


CLOSED_CYCLE_REFRESH_OFF_FILE = "cycle-refresh.off"


def closed_cycle_refresh_off() -> bool:
    """A machine-local switch that pauses the §45 closed-cycle refresh (and its sweep).

    `${XDG_CONFIG_HOME:-~/.config}/hearting/cycle-refresh.off` present = paused.  It lets a
    reader that does not yet follow updated closed-cycle manifests (Cairn sync) catch up
    before closed cycles start changing; open-cycle checkpoints are unaffected.  Absent
    file, the default, = on.  Read on every call so all three runtimes follow it at once."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    try:
        return os.path.exists(os.path.join(base, "hearting", CLOSED_CYCLE_REFRESH_OFF_FILE))
    except (OSError, ValueError):
        return False


def checkpoint_interval_seconds() -> float:
    raw = os.environ.get(CHECKPOINT_INTERVAL_ENV, "")
    try:
        value = float(raw) if raw else CHECKPOINT_MIN_INTERVAL_SECONDS
    except ValueError:
        return CHECKPOINT_MIN_INTERVAL_SECONDS
    return value if value >= 0 else CHECKPOINT_MIN_INTERVAL_SECONDS


@contextlib.contextmanager
def _checkpoint_lock(root: Path, cycle_id: str, *, timeout: float):
    """Per-cycle lock shared by checkpoint (ID assignment + writes) and finalize
    (ID reuse through interim removal).  Never taken before the admission lock."""
    path = _checkpoint_lock_path(root, cycle_id)
    _ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProducerError("checkpoint-lock-busy", cycle_id)
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _well_formed_or_none(value: Any, kind: str) -> Optional[str]:
    return value if isinstance(value, str) and artifact_identity.is_well_formed(value, kind) else None


def _reserved_revision(row: Any, artifact_id: str) -> Optional[ReservedRevision]:
    """One revision entry, checked by ID format only.  Reused provenance and
    event fields must also pass the manifest's own value rules; a field that
    does not is dropped (a fresh one is issued), never carried into a seal."""
    if not isinstance(row, Mapping):
        return None
    arev_id = _well_formed_or_none(row.get("artifact_revision_id"), "artifact_revision")
    digest = row.get("content_digest")
    if arev_id is None or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return None
    recorded_at = row.get("recorded_at")
    return ReservedRevision(
        artifact_id, arev_id, digest,
        recorded_in=_well_formed_or_none(row.get("recorded_in"), "manifest_revision"),
        event_id=_well_formed_or_none(row.get("event_id"), "event"),
        stream_id=_well_formed_or_none(row.get("stream_id"), "stream"),
        recorded_at=(recorded_at if isinstance(recorded_at, str)
                     and artifact_manifest._RFC3339_RE.match(recorded_at) else None),
    )


def _reserved_row(locator: Any, row: Any) -> Optional[Tuple[ReservedRevision, List[ReservedRevision]]]:
    """One ledger row (current revision + earlier ones), checked by ID format and
    locator grammar only -- a later manifest schema or contract version never
    invalidates a reservation."""
    if not isinstance(locator, str) or not isinstance(row, Mapping):
        return None
    if not artifact_manifest.validate_locator_path(locator).ok:
        return None
    art_id = _well_formed_or_none(row.get("artifact_id"), "artifact")
    current = _reserved_revision(row, art_id) if art_id else None
    if current is None:
        return None
    raw = row.get("history")
    earlier = [_reserved_revision(item, art_id) for item in (raw if isinstance(raw, list) else [])]
    return current, [rev for rev in earlier if rev is not None]


def _reservation_from_rows(manifest_id: Any, rows: Iterable[Tuple[Any, Any]]) -> InterimReservation:
    kept: Dict[str, ReservedRevision] = {}
    history: Dict[str, Dict[str, ReservedRevision]] = {}
    seen_artifacts: Set[str] = set()
    seen_revisions: Set[str] = set()
    seen_events: Set[str] = set()
    seen_streams: Set[str] = set()
    dropped = 0

    def unique_events(rev: ReservedRevision) -> ReservedRevision:
        # A duplicated event or stream ID would make the sealed manifest invalid;
        # keep the revision and let it record a fresh event instead.
        if (rev.event_id in seen_events or rev.stream_id in seen_streams
                or not (rev.event_id and rev.stream_id and rev.recorded_at)):
            return replace(rev, event_id=None, stream_id=None, recorded_at=None)
        seen_events.add(rev.event_id)
        seen_streams.add(rev.stream_id)
        return rev

    for locator, row in rows:
        parsed = _reserved_row(locator, row)
        # A duplicated artifact or revision ID would make the next manifest
        # invalid; drop the row.
        if (parsed is None or locator in kept or parsed[0].artifact_id in seen_artifacts
                or parsed[0].artifact_revision_id in seen_revisions):
            dropped += 1
            continue
        current, earlier = parsed
        kept[locator] = unique_events(current)
        seen_artifacts.add(current.artifact_id)
        seen_revisions.add(current.artifact_revision_id)
        for rev in earlier:
            if (rev.artifact_revision_id in seen_revisions or rev.content_digest == current.content_digest
                    or rev.content_digest in history.get(locator, {})):
                continue
            history.setdefault(locator, {})[rev.content_digest] = unique_events(rev)
            seen_revisions.add(rev.artifact_revision_id)
    return InterimReservation(_well_formed_or_none(manifest_id, "manifest"), kept, dropped, history)


def _reservation_from_document(document: Mapping[str, Any]) -> InterimReservation:
    """Fallback when the ledger is gone: the published interim document's rows."""
    events = {row.get("target_id"): row for row in document.get("events") or []
              if isinstance(row, Mapping) and row.get("event_type") == "artifact.revision.recorded"}
    rows = []
    for revision in document.get("artifact_revisions") or []:
        if not isinstance(revision, Mapping):
            continue
        locator = (revision.get("locator") or {}).get("path") if isinstance(revision.get("locator"), Mapping) else None
        event = events.get(revision.get("artifact_id")) or {}
        provenance = revision.get("provenance") if isinstance(revision.get("provenance"), Mapping) else {}
        rows.append((locator, {
            "artifact_id": revision.get("artifact_id"),
            "artifact_revision_id": revision.get("artifact_revision_id"),
            "content_digest": revision.get("content_digest"),
            "recorded_in": provenance.get("source_revision_id"),
            "event_id": event.get("event_id"), "stream_id": event.get("stream_id"),
            "recorded_at": event.get("recorded_at"),
        }))
    return _reservation_from_rows(document.get("manifest_id"), rows)


def read_interim_reservation(root: Path, record: Mapping[str, Any]) -> Tuple[Optional[InterimReservation], str]:
    """The cycle's ID reservation and a status word: `absent`, `present`,
    `document-fallback` (ledger missing, rebuilt from the interim document),
    `unreadable`, or `identity-mismatch`.  For the last two the returned
    reservation is the document fallback, when one can be read."""
    cid = record["cycle_id"]
    fallback: Optional[InterimReservation] = None
    document = _read_json(open_manifest_path(root, cid))
    if document is not None and isinstance(document.get("cycle"), Mapping) \
            and document["cycle"].get("cycle_id") == cid:
        fallback = _reservation_from_document(document)
    ledger_path = reservation_path(root, cid)
    try:
        ledger_path.lstat()
    except FileNotFoundError:
        return (fallback, "document-fallback") if fallback is not None else (None, "absent")
    except OSError:
        return fallback, "unreadable"
    payload = _read_json(ledger_path)
    if payload is None or not isinstance(payload.get("artifacts"), Mapping):
        return fallback, "unreadable"
    if (payload.get("cycle_id") != cid or payload.get("campaign_id") != record.get("campaign_id")
            or payload.get("route_id") != record.get("route_id")):
        return fallback, "identity-mismatch"
    return _reservation_from_rows(payload.get("manifest_id"), sorted(payload["artifacts"].items())), "present"


def _ledger_row(current: ReservedRevision, earlier: Mapping[str, ReservedRevision]) -> Dict[str, Any]:
    row = {key: value for key, value in asdict(current).items() if value is not None}
    kept = [rev for digest, rev in earlier.items() if digest != current.content_digest]
    if kept:
        row["history"] = [
            {key: value for key, value in asdict(rev).items() if value is not None and key != "artifact_id"}
            for rev in kept[-RESERVATION_HISTORY_LIMIT:]
        ]
    return row


def _reservation_payload(record: Mapping[str, Any], document: Mapping[str, Any],
                         previous: Optional[InterimReservation]) -> Dict[str, Any]:
    """Union of the previous reservation and this document -- never shrinks.  A
    locator whose content changed keeps its earlier revision in `history`."""
    rows: Dict[str, Dict[str, Any]] = {}
    if previous is not None:
        for locator, row in previous.artifacts.items():
            rows[locator] = _ledger_row(row, previous.history.get(locator, {}))
    events = {row["target_id"]: row for row in document["events"]
              if row["event_type"] == "artifact.revision.recorded"}
    for revision in document["artifact_revisions"]:
        locator = revision["locator"]["path"]
        event = events.get(revision["artifact_id"], {})
        current = ReservedRevision(
            revision["artifact_id"], revision["artifact_revision_id"], revision["content_digest"],
            recorded_in=revision["provenance"]["source_revision_id"],
            event_id=event.get("event_id"), stream_id=event.get("stream_id"),
            recorded_at=event.get("recorded_at"),
        )
        earlier: Dict[str, ReservedRevision] = {}
        if previous is not None:
            earlier = dict(previous.history.get(locator, {}))
            before = previous.artifacts.get(locator)
            if before is not None and before.content_digest != current.content_digest:
                earlier.pop(before.content_digest, None)
                earlier[before.content_digest] = before
        rows[locator] = _ledger_row(current, earlier)
    return {
        "schema_version": RESERVATION_SCHEMA_VERSION, "cycle_id": record["cycle_id"],
        "campaign_id": record["campaign_id"], "route_id": record.get("route_id"),
        "manifest_id": document["manifest_id"], "artifacts": dict(sorted(rows.items())),
    }


def remove_interim(root: Path, cycle_id: str) -> None:
    """Drop a cycle's interim document, reservation and bookkeeping (idempotent)."""
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        return
    for path in (open_manifest_path(root, cycle_id), reservation_path(root, cycle_id),
                 checkpoint_state_path(root, cycle_id), _checkpoint_lock_path(root, cycle_id)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _sweep_orphan_interims(root: Path, *, now: Optional[float] = None) -> List[str]:
    """Remove interim files of cycles that are no longer open.  A cycle record
    that exists but cannot be read keeps its files (a transient read error must
    not cost a live cycle its IDs); abandoned temporary files are dropped."""
    clock = time.time() if now is None else now
    removed: List[str] = []
    for name in (OPEN_MANIFEST_DIR, CHECKPOINT_DIR):
        directory = producer_dir(root) / name
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(".") and ".tmp-" in entry.name:
                try:
                    if clock - entry.lstat().st_mtime > _STALE_TEMP_SECONDS:
                        entry.unlink()
                except OSError:
                    pass
                continue
            cycle_id = entry.name.split(".", 1)[0]
            if not artifact_identity.is_well_formed(cycle_id, "cycle") or cycle_id in removed:
                continue
            try:
                cycle_record_path(root, cycle_id).lstat()
            except FileNotFoundError:
                record: Optional[Dict[str, Any]] = {"state": "absent"}
            except OSError:
                continue
            else:
                record = read_cycle_record(root, cycle_id)
                if record is None:
                    continue
            if record.get("state") != "open":
                remove_interim(root, cycle_id)
                removed.append(cycle_id)
    return removed


def _route_release_supports_interim(route: Mapping[str, Any]) -> Tuple[bool, str]:
    tuple_ = route.get("launch_compatibility_tuple")
    runtime = tuple_.get("runtime_root") if isinstance(tuple_, Mapping) else None
    path = runtime.get("path") if isinstance(runtime, Mapping) else None
    if not isinstance(path, str) or not path:
        return False, "runtime-root-unsealed"
    return (Path(path) / INTERIM_SUPPORT_MARKER).is_file(), path


def _checkpoint_scan(directory: Path, previous: Mapping[str, Any], limits: CheckpointLimits) -> Dict[str, Any]:
    """Bounded walk of `artifacts/`.  Returns `facts` (locator, digest, size),
    the refreshed digest cache, exclusion counts and the newest file mtime, or a
    `skip` reason as soon as a limit is exceeded (the walk stops there)."""
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        return {"skip": "artifacts-dir-missing"}
    excluded = {"hidden": 0, "binary": 0, "oversize": 0, "non-regular": 0, "invalid-locator": 0}
    candidates: List[Tuple[str, str, os.stat_result]] = []
    visited = total = 0
    newest = 0.0
    for current, dirs, files in os.walk(str(artifacts), followlinks=False):
        kept_dirs = []
        for name in sorted(dirs):
            if name.startswith("."):
                excluded["hidden"] += 1
            elif os.path.islink(os.path.join(current, name)):
                excluded["non-regular"] += 1
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            visited += 1
            if visited > limits.max_walk_entries:
                return {"skip": "walk-limit", "visited": visited}
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError:
                excluded["non-regular"] += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                excluded["non-regular"] += 1
                continue
            newest = max(newest, info.st_mtime)
            if name.startswith("."):
                excluded["hidden"] += 1
                continue
            if os.path.splitext(name)[1].lower() in CHECKPOINT_EXCLUDED_SUFFIXES:
                excluded["binary"] += 1
                continue
            if info.st_size > limits.max_file_bytes:
                excluded["oversize"] += 1
                continue
            rel = Path(path).relative_to(directory).as_posix()
            locator = artifact_manifest.validate_locator_path(rel)
            if not locator.ok:
                excluded["invalid-locator"] += 1
                reason = locator.violations[0].code
                reasons = excluded.setdefault("invalid-locator-reasons", {})
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            candidates.append((rel, path, info))
            total += info.st_size
            if len(candidates) > limits.max_files:
                return {"skip": "file-count-limit", "files": len(candidates)}
            if total > limits.max_total_bytes:
                return {"skip": "byte-size-limit", "bytes": total}
    facts: List[Tuple[str, str, int]] = []
    stats: Dict[str, List[Any]] = {}
    for rel, path, info in sorted(candidates):
        old = previous.get(rel)
        if (isinstance(old, list) and len(old) == 3 and old[0] == info.st_size
                and old[1] == info.st_mtime_ns and isinstance(old[2], str)):
            digest, size = old[2], info.st_size
        else:
            try:
                data = Path(path).read_bytes()
            except OSError:
                excluded["non-regular"] += 1
                continue
            digest, size = _digest(data), len(data)
        facts.append((rel, digest, size))
        stats[rel] = [size, info.st_mtime_ns, digest]
    return {"facts": facts, "stats": stats, "excluded": excluded, "newest_mtime": newest,
            "total_bytes": sum(size for _rel, _digest_value, size in facts)}


def _checkpoint_target(root: Path, cycle_id: Optional[str], route_file: Optional[Path]) -> Optional[Dict[str, Any]]:
    if cycle_id:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        return record
    if route_file is None:
        raise ProducerError("checkpoint-target-required", "--cycle or --route")
    route = load_route(root, route_file)
    return route_cycle_for(root, route)


def checkpoint(
    root: Path,
    *,
    cycle_id: Optional[str] = None,
    route_file: Optional[Path] = None,
    trigger: str = "explicit",
    now: Optional[float] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    limits: Optional[CheckpointLimits] = None,
) -> Dict[str, Any]:
    """Look at the cycle a trigger names; an automatic trigger then looks at the rest of the root.

    An open cycle gets its interim manifest (`_checkpoint_cycle`); a closed one is observed
    for changed files (`refresh_cycle`, §45 D-124).  An automatic trigger (not `explicit`)
    then spends what is left of the same budget on the root's other closed cycles
    (`refresh_sweep`); that never changes the named cycle's result except for a `refresh` key
    naming what the sweep did."""
    if cycle_id:
        _observe_control_changes(Path(root).resolve(), cycle_id, now=now)
    budget = RefreshBudget.unlimited() if trigger == "explicit" else RefreshBudget()
    deliver_pending_history(root)  # §45 D-125: lines an earlier trigger could not hand over
    result = _checkpoint_cycle(root, cycle_id=cycle_id, route_file=route_file, trigger=trigger, now=now,
                               allocator=allocator, limits=limits, budget=budget)
    if trigger != "explicit":
        try:
            sweep = refresh_sweep(Path(root).resolve(), trigger=trigger, now=now, budget=budget,
                                  skip_cycle_id=result.get("cycle_id"), allocator=allocator)
        except Exception:  # noqa: BLE001 -- the sweep never changes what the trigger returns
            sweep = {}
        if sweep.get("visited"):
            result["refresh"] = {key: sweep[key] for key in ("visited", "refreshed", "skipped", "cursor")
                                 if key in sweep}
    return result


def _checkpoint_cycle(
    root: Path,
    *,
    cycle_id: Optional[str] = None,
    route_file: Optional[Path] = None,
    trigger: str = "explicit",
    now: Optional[float] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    limits: Optional[CheckpointLimits] = None,
    budget: Optional[RefreshBudget] = None,
) -> Dict[str, Any]:
    """Publish (or refresh) an open cycle's interim manifest.

    Every outcome that is not an error is a result: `emitted`, `unchanged`, or
    `skipped` with a `reason`.  Only a live cycle is published: the cycle is
    open with no sealed manifest, and its route is readable, not closed, and
    sealed to a release that keeps interim IDs at the seal.  Scans are
    rate-limited per cycle (`checkpoint_interval_seconds`) for every trigger,
    including an explicit one.  The scan runs unlocked; every write -- the
    reservation, the document and the bookkeeping -- happens under the cycle's
    checkpoint lock after the cycle is re-read.
    """
    root = Path(root).resolve()
    if trigger not in CHECKPOINT_TRIGGERS:
        raise ProducerError("checkpoint-trigger-invalid", trigger)
    clock = time.time() if now is None else float(now)
    try:
        record = _checkpoint_target(root, cycle_id, route_file)
    except ProducerError as exc:
        if exc.code == "route-hash-drift":
            return {"status": "skipped", "reason": "route-hash-drift", "trigger": trigger}
        raise
    if record is None:
        return {"status": "skipped", "reason": "no-open-cycle", "trigger": trigger}
    cid = record["cycle_id"]
    base = {"cycle_id": cid, "route_id": record.get("route_id"), "trigger": trigger}

    def skipped(reason: str, **extra: Any) -> Dict[str, Any]:
        return {"status": "skipped", "reason": reason, **base, **extra}

    if record.get("state") == "sealed":
        observed = refresh_cycle(root, cid, trigger=trigger, now=now, allocator=allocator, budget=budget)
        if observed["status"] == "skipped":
            return {"status": "skipped", "reason": observed["reason"], **base}
        return {"status": observed["status"], **base,
                **{key: observed[key] for key in ("changes", "complete", "cursor", "manifest_digest")
                   if key in observed}}
    if record.get("state") != "open":
        return skipped("cycle-not-open", cycle_state=record.get("state"))
    directory = cycle_dir(root, record["campaign_id"], cid, record)
    if (directory / "manifest.json").exists():
        return skipped("sealed-manifest-present")
    try:
        route = load_route(root, Path(record["route_file"]))
    except ProducerError as exc:
        return skipped("route-unreadable", detail=exc.code)
    if route["route_hash"] != record.get("route_hash"):
        return skipped("route-hash-drift")
    if route_is_closed(root, route):
        return skipped("route-closed")
    supported, release = _route_release_supports_interim(route)
    if not supported:
        return skipped("route-release-predates-interim", runtime_root=release)
    state_path = checkpoint_state_path(root, cid)
    state = _read_json(state_path) or {}
    observed_scan = state.get("last_scan_at")
    interval = checkpoint_interval_seconds()
    if (isinstance(observed_scan, (int, float)) and not isinstance(observed_scan, bool)
            and clock - observed_scan < interval):
        return skipped("min-interval", next_eligible_at=_rfc3339(observed_scan + interval))
    limits = limits or CheckpointLimits()
    previous_stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    scan = _checkpoint_scan(directory, previous_stats, limits)
    try:
        with _checkpoint_lock(root, cid, timeout=0.0):
            result = _checkpoint_commit(
                root, record, route, directory, scan, observed_scan=observed_scan, clock=clock,
                trigger=trigger, limits=limits, interval=interval, base=base,
                allocator=allocator or artifact_identity.IdAllocator(),
            )
    except ProducerError as exc:
        if exc.code == "checkpoint-lock-busy":
            return skipped("busy")
        raise
    if result["status"] in {"emitted", "unchanged"}:
        artifact_cycle_titles.emit_after_checkpoint(root, record)
    return result


def _checkpoint_commit(
    root: Path, record: Mapping[str, Any], route: Mapping[str, Any], directory: Path,
    scan: Mapping[str, Any], *, observed_scan: Any, clock: float, trigger: str,
    limits: CheckpointLimits, interval: float, base: Mapping[str, Any],
    allocator: artifact_identity.IdAllocator,
) -> Dict[str, Any]:
    cid = record["cycle_id"]
    fresh = read_cycle_record(root, cid)
    if fresh is None:
        return {"status": "skipped", "reason": "cycle-record-unreadable", **base}
    if fresh.get("state") != "open":
        # Sealed or dropped while this checkpoint scanned: whatever interim
        # files remain (the lock this call just re-created, at least) are garbage.
        remove_interim(root, cid)
        return {"status": "skipped", "reason": "cycle-not-open", **base}
    if (directory / "manifest.json").exists():
        # Still open with a manifest present: a seal in flight or a torn one.
        # The reservation stays -- a re-run finalize must find it.
        return {"status": "skipped", "reason": "sealed-manifest-present", **base}
    state_path = checkpoint_state_path(root, cid)
    state = _read_json(state_path) or {}
    if state.get("last_scan_at") != observed_scan:
        # Another checkpoint scanned and settled while this one scanned; its
        # result is newer than nothing and this scan may predate its files.
        return {"status": "skipped", "reason": "superseded", **base}
    interim_path = open_manifest_path(root, cid)
    had_interim = interim_path.exists()
    previous_stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    bookkeeping: Dict[str, Any] = {
        "schema_version": 1, "cycle_id": cid, "campaign_id": fresh["campaign_id"],
        "route_id": fresh.get("route_id"), "cycle_path": os.path.relpath(str(directory), str(root)),
        "open_manifest": os.path.relpath(str(interim_path), str(root)),
        "last_scan_at": state.get("last_scan_at"), "last_scan_on": state.get("last_scan_on"),
        "last_trigger": trigger, "last_emitted_at": state.get("last_emitted_at"),
        "output_digest": state.get("output_digest"), "limits": limits.to_payload(),
        "min_interval_seconds": interval, "stats": previous_stats,
    }

    def settle(status: str, reason: Optional[str], *, scanned: bool = True, **extra: Any) -> Dict[str, Any]:
        if scanned:
            bookkeeping.update({"last_scan_at": clock, "last_scan_on": _rfc3339(clock)})
        bookkeeping.update({"last_status": status, "last_reason": reason})
        bookkeeping.update({key: value for key, value in extra.items() if key in {
            "stats", "excluded", "artifact_count", "total_bytes", "output_digest",
            "last_emitted_at", "manifest_id", "manifest_revision_id"}})
        _ensure_dir(state_path.parent)
        _write_atomic(state_path, _json_bytes(bookkeeping), 0o644)
        result = {"status": status, **base, **{k: v for k, v in extra.items() if k != "stats"}}
        if reason:
            result["reason"] = reason
        return result

    if scan.get("skip"):
        detail = {key: value for key, value in scan.items() if key != "skip"}
        return settle("skipped", scan["skip"], limit_detail=detail)
    facts = scan["facts"]
    common = {"stats": scan["stats"], "excluded": scan["excluded"],
              "artifact_count": len(facts), "total_bytes": scan["total_bytes"]}
    if not had_interim and not facts:
        return settle("skipped", "no-output", scanned=False, **common)
    if (trigger != "explicit" and not had_interim and scan["newest_mtime"]
            and clock - scan["newest_mtime"] > CHECKPOINT_STALE_SECONDS):
        return settle("skipped", "stale-cycle", scanned=False,
                      newest_file_on=_rfc3339(scan["newest_mtime"]), **common)
    reserved, reservation = read_interim_reservation(root, fresh)
    if reservation in ("unreadable", "identity-mismatch"):
        # Never re-issue IDs over a reservation this code cannot read.
        return settle("skipped", f"reservation-{reservation}", **common)
    output_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    reserved_matches = reserved is not None and all(
        rel in reserved.artifacts and reserved.artifacts[rel].content_digest == digest
        for rel, digest, _size in facts)
    if (had_interim and reservation == "present" and reserved_matches
            and state.get("output_digest") == output_digest):
        return settle("unchanged", None, path=str(interim_path), **common)
    try:
        document = build_manifest(
            root, fresh, route, (), state="completed", primary=None, allow_open_route=True,
            allocator=allocator, now=clock, reserved=reserved, interim=True, facts=facts,
        )
    except ProducerError as exc:
        return settle("skipped", exc.code, detail=exc.detail, **common)
    report = artifact_manifest.validate_interim(document)
    if not report.ok:
        return settle("skipped", "interim-invalid",
                      detail=";".join(v.code for v in report.violations), **common)
    # The reservation is written before the document: a crash in between leaves
    # IDs reserved but unpublished, never published but unreserved.
    ledger = reservation_path(root, cid)
    _ensure_dir(ledger.parent)
    _write_atomic(ledger, _json_bytes(_reservation_payload(fresh, document, reserved)), 0o644)
    _ensure_dir(interim_path.parent)
    _write_atomic(interim_path, artifact_manifest.canonical_bytes(document), 0o644)
    return settle(
        "emitted", None, path=str(interim_path), output_digest=output_digest,
        last_emitted_at=clock, manifest_id=document["manifest_id"],
        manifest_revision_id=document["manifest_revision_id"],
        reservation=reservation, **common,
    )


def _remove_empty_cycle(root: Path, record: Mapping[str, Any]) -> None:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
    artifacts = directory / "artifacts"
    binding = directory / artifact_locator.CYCLE_BINDING
    if binding.is_file() and not binding.is_symlink():
        binding.unlink()
    for path in (artifacts, directory):
        try:
            path.rmdir()
        except OSError as exc:
            raise ProducerError("cycle-dir-not-empty", str(path)) from exc
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is not None:
        campaign["cycles"] = [c for c in campaign.get("cycles", []) if c != record["cycle_id"]]
        if not campaign["cycles"] and campaign.get("key", "").endswith(record["route_id"]):
            # Campaign created by this begin and never populated: drop it.
            try:
                campaign_path = campaign_dir(root, campaign["campaign_id"], campaign)
                _campaign_path(root, campaign["campaign_id"], campaign).unlink()
                campaign_path.rmdir()
            except OSError:
                _write_campaign(root, campaign, exclusive=False)
        else:
            _write_campaign(root, campaign, exclusive=False)
    artifact_locator.update_indexes(root, [record["campaign_id"]])


def _review_lease_dir(root: Path, cycle_id: str) -> Path:
    return Path(root) / PRODUCER_REL / REVIEW_LEASE_REL / cycle_id


def _review_lease_path(root: Path, cycle_id: str, attempt_id: str) -> Path:
    return _review_lease_dir(root, cycle_id) / f"{attempt_id}.json"


def _rfc3339_to_epoch(value: str) -> float:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _lease_record_is_live(
    record: Optional[Dict[str, Any]], *, root: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """SD-105/SD-90 evidence hierarchy, cited not redefined (plan.md §6.2):
    exact PID/start/PGID identity, a finite deadline, and judgment-impossible
    inputs (corrupt record, unreadable /proc, clock anomaly, missing field)
    read as *live* -- conservative, so an undecidable lease never lets an
    abandon through (E47-4)."""

    if record is None:
        return False
    if record.get("released_at") is not None:
        return False
    deadline = record.get("deadline")
    if not isinstance(deadline, str):
        return True
    try:
        deadline_ts = _rfc3339_to_epoch(deadline)
    except (ValueError, OverflowError):
        return True
    when = time.time() if now is None else now
    if _is_v2_review_lease(record):
        if record.get("expired") is not False:
            return True
        acquired_at = record.get("acquired_at")
        try:
            acquired_ts = (
                _rfc3339_to_epoch(acquired_at)
                if isinstance(acquired_at, str) else None
            )
        except (ValueError, OverflowError):
            return True
        if root is None or acquired_ts is None or acquired_ts > when or deadline_ts <= acquired_ts:
            return True
        metadata = dict(record)
        metadata["review_cycle_id"] = record.get("cycle_id", "")
        # The record stores the sealed fields while the jobs row mirrors the
        # digest.  Reconstruct that mirror for the closed disposition so a
        # dead exact holder can unblock abandon instead of being treated as
        # malformed forever.
        metadata["review_lease_record_digest"] = review_lease_record_digest(record)
        disposition = review_holder_disposition(record, metadata, root, now=when)
        if disposition.state in {"live", "malformed"}:
            return True
        if disposition.state == "dead":
            return False
        # Valid but unobservable holders remain conservative until the finite
        # stale ceiling, after which time permits recovery but never grants a
        # write.
        return when <= deadline_ts
    if when > deadline_ts:
        return False
    pid = record.get("pid")
    pid_start = record.get("pid_start")
    pgid = record.get("pgid")
    if not isinstance(pid, int) or not isinstance(pid_start, str) or not pid_start or not isinstance(pgid, int):
        return True
    actual_start = process_start_ticks(pid)
    if actual_start is None:
        return False
    if actual_start != pid_start:
        return False
    try:
        actual_pgid = os.getpgid(pid)
    except OSError:
        return False
    return actual_pgid == pgid


def _is_v2_review_lease(record: object) -> bool:
    """Identify the exact-output lease without changing the v1 union seam."""

    return isinstance(record, Mapping) and record.get("schema_version") == 2


def _live_review_lease(
    root: Path, cycle_id: str, *, now: Optional[float] = None
) -> Optional[Path]:
    lease_dir = _review_lease_dir(root, cycle_id)
    if not lease_dir.is_dir():
        return None
    conservative: Optional[Path] = None
    for path in sorted(lease_dir.glob("*.json")):
        record = _read_json(path)
        if record is None:
            # The glob already proved this file exists, so an unparseable
            # read here is corruption, not absence -- conservative live
            # (E47-4), unlike `_lease_record_is_live(None)` below which
            # means "no lease file at this specific path".
            conservative = conservative or path
            continue
        if _lease_record_is_live(record, root=root, now=now):
            # Completed sealing cares about a live exact-report lease wherever
            # it appears in a mixed v1/v2 directory. Preserve the first
            # conservative v1/corrupt candidate for abandon semantics, but let
            # any live v2 report lease win this single union seam.
            if _is_v2_review_lease(record):
                return path
            conservative = conservative or path
    return conservative


def _raise_if_recovery_fenced(root: Path, cycle_id: str, *, now: Optional[float] = None) -> None:
    """Keep recovery from sealing a cycle under a live v2 review lease.

    Recovery is itself a publication path: both a journal roll-forward and
    discovery of an already-published manifest call ``_commit_sealed``.  The
    normal finalize fence therefore has to be repeated immediately before
    that mutation.  Legacy v1 leases remain governed by their existing
    abandon-only policy; only the exact v2 report lease blocks recovery.
    """
    lease_path = _live_review_lease(root, cycle_id, now=now)
    if lease_path is None:
        return
    lease = _read_json(lease_path)
    if _is_v2_review_lease(lease):
        raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)


def _path_entry_present(path: Path) -> bool:
    """Return false only for an absent directory entry.

    Publication admission is fail-closed.  A dangling symlink, directory,
    special node, unreadable regular file, or lookup error is still an entry;
    none may be collapsed into the same state as ENOENT by ``exists`` or
    ``is_file``.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _raise_if_review_publication_started(
    root: Path, record: Mapping[str, Any]
) -> None:
    """Refuse a new v2 lease once either publication commit path has begun."""

    cycle_id = str(record.get("cycle_id", ""))
    directory = cycle_dir(
        root, str(record.get("campaign_id", "")), cycle_id, record,
    )
    for entry in (journal_path(root, cycle_id), directory / "manifest.json"):
        if _path_entry_present(entry):
            raise ProducerError(
                "review-lease-admission-after-publication",
                f"{cycle_id}: {entry}",
            )


def prepare_review_output_binding(
    root: Path, *, cycle_id: str, producer_id: str, attempt_id: str,
    review_output: str | Path, capability: str, unit: str,
    worktree: str | Path,
) -> Dict[str, Any]:
    """Validate the immutable cycle/route side before registry mutation."""

    root_input = Path(root)
    canonical_root = root_input.resolve(strict=False)
    worktree_input = Path(worktree)
    canonical_worktree = worktree_input.resolve(strict=False)
    if (
        not root_input.is_absolute() or str(root_input) != str(canonical_root)
        or not worktree_input.is_absolute()
        or str(worktree_input) != str(canonical_worktree)
    ):
        raise ProducerError("review-binding-root-not-canonical", str(root))
    record = read_cycle_record(canonical_root, cycle_id)
    if record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    if record.get("producer_id") != producer_id:
        raise ProducerError("review-binding-producer-mismatch", producer_id)
    # Fast preclaim refusal.  review_lease_acquire repeats this check while it
    # owns the canonical admission lock, closing publication after prepare.
    # Only a publication still in flight (cycle not yet closed) is refused: a
    # finished close does not stop a later review (§45 D-123).
    if record.get("state") == "open":
        _raise_if_review_publication_started(canonical_root, record)
    route = load_route(canonical_root, Path(str(record.get("route_file", ""))))
    expected_capability = str(record.get("capability", ""))
    if (
        capability != expected_capability
        or route.get("capability") != expected_capability
        or route.get("route_id") != record.get("route_id")
        or route.get("route_hash") != record.get("route_hash")
    ):
        raise ProducerError("review-binding-capability-mismatch", capability)
    if unit != "qa/code-review":
        raise ProducerError("review-binding-unit-mismatch", unit)
    if Path(str(route.get("cwd", ""))).resolve(strict=False) != canonical_worktree:
        raise ProducerError("review-binding-worktree-mismatch", str(worktree))
    if Path(str(route.get("artifact_root", ""))).resolve(strict=False) != canonical_root:
        raise ProducerError("review-binding-artifact-root-mismatch", str(root))
    output_input = Path(review_output)
    output = output_input.resolve(strict=False)
    if not output_input.is_absolute() or str(output_input) != str(output):
        raise ProducerError("review-output-path-not-canonical", str(review_output))
    artifacts = (
        cycle_dir(canonical_root, record["campaign_id"], cycle_id, record)
        / "artifacts"
    ).resolve(strict=False)
    try:
        locator = output.relative_to(canonical_root).as_posix()
        cycle_locator = output.relative_to(artifacts)
    except ValueError as exc:
        raise ProducerError("review-output-outside-cycle", str(output)) from exc
    if not cycle_locator.parts or cycle_locator.parts[0] != "plans":
        raise ProducerError("review-output-bucket-forbidden", str(output))
    if output.is_dir() or output.exists() and not output.is_file():
        raise ProducerError("review-output-target-invalid", str(output))
    current = output
    while current != artifacts:
        if current.is_symlink():
            raise ProducerError("review-output-symlink", str(current))
        current = current.parent
    binding: Dict[str, Any] = {
        "schema_version": 2,
        "attempt_id": attempt_id,
        "cycle_id": cycle_id,
        "producer_id": producer_id,
        "worktree": str(canonical_worktree),
        "artifact_root": str(canonical_root),
        "capability": capability,
        "unit": unit,
        "output_path": str(output),
    }
    binding["digest"] = review_output_binding_digest(binding)
    binding["locator_b64"] = encode_review_output_locator(locator)
    return binding


def review_lease_acquire(
    root: Path, *, cycle_id: str, attempt_id: str, deadline_seconds: float = 900.0,
    now: Optional[float] = None, review_output: Optional[str | Path] = None,
    binding: Optional[Mapping[str, Any]] = None,
    governed_identity: Optional[Mapping[str, Any]] = None,
    jobs: Optional[str | Path] = None,
    watchdog_budget: Optional[FiniteWatchdogBudget] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        try:
            path.lstat()
            existing_path = True
        except FileNotFoundError:
            existing_path = False
        existing = _read_json(path)
        when = time.time() if now is None else now
        v2_requested = review_output is not None or binding is not None
        if v2_requested and watchdog_budget is None:
            watchdog_budget = begin_finite_watchdog(
                deadline_seconds, origin_epoch=when
            )
        if v2_requested and not isinstance(watchdog_budget, FiniteWatchdogBudget):
            raise ProducerError("review-lease-budget-invalid", attempt_id)
        if v2_requested and watchdog_budget is not None:
            if remaining_watchdog_seconds(watchdog_budget) <= 0:
                raise ProducerError("review-lease-budget-exhausted", attempt_id)
            if when < watchdog_budget.origin_epoch:
                raise ProducerError("review-lease-budget-contradictory", attempt_id)
        # E47-9: the same (cycle, attempt) re-acquiring its own still-live
        # lease is an idempotent no-op -- zero state change.
        existing_live = existing is not None and _lease_record_is_live(
            existing, root=root, now=when
        )
        schema_version = 1
        exact_output = None
        registry_metadata: Mapping[str, object] = {}
        if v2_requested:
            if review_output is None or binding is None or jobs is None:
                raise ProducerError("review-lease-binding-incomplete", cycle_id)
            if not isinstance(binding, Mapping):
                raise ProducerError("review-lease-binding-invalid", attempt_id)
            admission_record = read_cycle_record(root, cycle_id)
            if admission_record is not None and admission_record.get("state") == "open":
                # This is the authoritative race-closing check: the admission
                # mutex above is still held and publication uses that same
                # mutex.  No recovery or nested lock acquisition occurs here.
                _raise_if_review_publication_started(root, admission_record)
            try:
                canonical_binding = prepare_review_output_binding(
                    root, cycle_id=cycle_id,
                    producer_id=str(binding.get("producer_id", "")),
                    attempt_id=attempt_id, review_output=review_output,
                    capability=str(binding.get("capability", "")),
                    unit=str(binding.get("unit", "")),
                    worktree=str(binding.get("worktree", "")),
                )
                checked_binding = validate_review_output_binding(
                    jobs, attempt_id=attempt_id, output_path=review_output,
                    cycle_id=cycle_id,
                    producer_id=str(canonical_binding["producer_id"]),
                    capability=str(canonical_binding["capability"]),
                    unit=str(canonical_binding["unit"]),
                    worktree=str(canonical_binding["worktree"]),
                    artifact_root=str(canonical_binding["artifact_root"]),
                )
            except Exception as exc:
                if isinstance(exc, (OSError, ValueError, TypeError)):
                    raise ProducerError("review-lease-binding-invalid", attempt_id) from exc
                raise ProducerError(getattr(exc, "reason", "review-lease-binding-invalid"), attempt_id) from exc
            exact_output = Path(checked_binding["output_path"])
            closed_fields = (
                "schema_version", "attempt_id", "cycle_id", "producer_id",
                "worktree", "artifact_root", "capability", "unit",
                "output_path", "digest", "locator_b64",
            )
            if any(binding.get(key) != canonical_binding.get(key) for key in closed_fields):
                raise ProducerError("review-lease-binding-mismatch", attempt_id)
            if checked_binding["digest"] != canonical_binding["digest"]:
                raise ProducerError("review-binding-digest-mismatch", attempt_id)
            identity = dict(governed_identity or {})
            required_identity = ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns")
            if not all(identity.get(key) not in (None, "") for key in required_identity):
                raise ProducerError("review-governed-identity-incomplete", cycle_id)
            if not str(identity.get("pid")).isdigit() or not str(identity.get("pgid")).isdigit():
                raise ProducerError("review-governed-identity-invalid", cycle_id)
            registry_metadata = checked_binding.get("_registry_metadata", {})
            if not isinstance(registry_metadata, Mapping):
                raise ProducerError("review-lease-binding-invalid", attempt_id)
            if any(
                str(registry_metadata.get(key, "")) != str(identity.get(key, ""))
                for key in _PROCESS_IDENTITY_METADATA_KEYS
            ):
                raise ProducerError("review-lease-identity-mismatch", attempt_id)
            nonce = identity.get("review_governed_lease_nonce", registry_metadata.get("review_governed_lease_nonce"))
            if not isinstance(nonce, str) or REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(nonce) is None:
                raise ProducerError("review-governed-lease-nonce-invalid", attempt_id)
            if (
                registry_metadata.get("review_governed_lease") != REVIEW_GOVERNED_LEASE_KIND
                or registry_metadata.get("review_governed_lease_nonce") != nonce
            ):
                raise ProducerError("review-governed-lease-nonce-mismatch", attempt_id)
            if not review_governed_lease_is_held(root, registry_metadata):
                raise ProducerError("review-governed-lease-not-held", attempt_id)
            schema_version = 2
            binding_digest = str(canonical_binding["digest"])
        else:
            identity = {}
            binding_digest = None
        if existing_live:
            if schema_version == 2:
                expected_existing = {
                    "schema_version": 2, "cycle_id": cycle_id,
                    "attempt_id": attempt_id, "producer_id": canonical_binding["producer_id"],
                    "worktree": canonical_binding["worktree"],
                    "artifact_root": canonical_binding["artifact_root"],
                    "capability": canonical_binding["capability"],
                    "unit": canonical_binding["unit"],
                    "review_output_path": str(exact_output),
                    "review_output_digest": binding_digest,
                    "review_governed_lease": registry_metadata.get("review_governed_lease"),
                    "review_governed_lease_nonce": registry_metadata.get("review_governed_lease_nonce"),
                }
                if any(existing.get(key) != value for key, value in expected_existing.items()):
                    raise ProducerError("review-lease-binding-mismatch", attempt_id)
                for key in _PROCESS_IDENTITY_METADATA_KEYS:
                    if str(existing.get(key, "")) != str(identity.get(key, "")):
                        raise ProducerError("review-lease-identity-mismatch", attempt_id)
                digest = review_lease_record_digest(existing)
                return {
                    "status": "already-held", "cycle_id": cycle_id,
                    "attempt_id": attempt_id,
                    "registry_metadata": {
                        "review_lease_acquired_at": existing.get("acquired_at", ""),
                        "review_lease_deadline": existing.get("deadline", ""),
                        "review_lease_record_digest": digest,
                    },
                }
            return {"status": "already-held", "cycle_id": cycle_id, "attempt_id": attempt_id}
        if schema_version == 2 and existing_path:
            raise ProducerError("review-lease-existing-invalid", attempt_id)
        pid = int(identity.get("pid", os.getpid()))
        pid_start = str(identity.get("pid_start", process_start_ticks(pid) or ""))
        pgid = int(identity.get("pgid", os.getpgid(pid)))
        lease_seconds = (
            watchdog_budget.timeout_seconds
            if schema_version == 2 and watchdog_budget is not None
            else max(1.0, deadline_seconds)
        )
        record = {
            "schema_version": schema_version, "cycle_id": cycle_id, "attempt_id": attempt_id,
            "pid": pid, "pid_start": pid_start,
            "pgid": pgid, "acquired_at": _rfc3339(when),
            # The audit wall deadline belongs to the one launch-origin clock,
            # not to the later lease-acquisition moment.  ``acquired_at``
            # remains the real acquisition observation for future-timestamp
            # rejection and audit coherence.
            "deadline": (_rfc3339_precise(
                watchdog_budget.origin_epoch + watchdog_budget.timeout_seconds
                if schema_version == 2 and watchdog_budget is not None
                else when + lease_seconds
            ) if schema_version == 2 else _rfc3339(when + lease_seconds)),
            "released_at": None, "expired": False,
        }
        if schema_version == 2:
            record.update({
                "review_output_path": str(exact_output),
                "review_output_digest": binding_digest,
                "worktree": canonical_binding["worktree"],
                "artifact_root": canonical_binding["artifact_root"],
                "capability": canonical_binding["capability"],
                "unit": canonical_binding["unit"],
                "producer_id": canonical_binding["producer_id"],
                "review_governed_lease": registry_metadata.get("review_governed_lease"),
                "review_governed_lease_nonce": registry_metadata.get("review_governed_lease_nonce"),
                "watchdog_timeout_seconds": watchdog_budget.timeout_seconds,
                "watchdog_origin_monotonic_ns": watchdog_budget.origin_monotonic_ns,
                "watchdog_deadline_monotonic_ns": watchdog_budget.deadline_monotonic_ns,
                "watchdog_origin_epoch": watchdog_budget.origin_epoch,
                "watchdog_deadline_epoch": watchdog_budget.origin_epoch + watchdog_budget.timeout_seconds,
                "watchdog_budget_digest": watchdog_budget.digest,
            })
            for key in _PROCESS_IDENTITY_METADATA_KEYS:
                if key in identity:
                    record[key] = identity[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, _json_bytes(record), 0o600)
        result = {"status": "acquired", "cycle_id": cycle_id, "attempt_id": attempt_id}
        if schema_version == 2:
            result["registry_metadata"] = {
                "review_lease_acquired_at": record["acquired_at"],
                "review_lease_deadline": record["deadline"],
                "review_lease_record_digest": review_lease_record_digest(record),
            }
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_release(
    root: Path, *, cycle_id: str, attempt_id: str, now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        existing = _read_json(path)
        if existing is None or existing.get("released_at") is not None:
            # E47-9: releasing an already-released (or never-acquired) lease
            # is an idempotent no-op.
            return {"status": "already-released", "cycle_id": cycle_id, "attempt_id": attempt_id}
        existing["released_at"] = _rfc3339(time.time() if now is None else now)
        _write_atomic(path, _json_bytes(existing), 0o600)
        return {"status": "released", "cycle_id": cycle_id, "attempt_id": attempt_id}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_status(root: Path, *, cycle_id: str, attempt_id: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    if attempt_id is not None:
        record = _read_json(_review_lease_path(root, cycle_id, attempt_id))
        return {
            "cycle_id": cycle_id, "attempt_id": attempt_id,
            "live": _lease_record_is_live(record, root=root),
        }
    live_path = _live_review_lease(root, cycle_id)
    return {"cycle_id": cycle_id, "live": live_path is not None}


_SEALED_CYCLE_STATES = {"active", "completed", "abandoned"}


def _valid_cycle_state(value: Any) -> bool:
    """`value` is a genuine member of the cycle work-state enum only if it is a
    *string* member of it.

    The type check has to live inside this predicate: JSON can put a list or
    dict in this slot, and a bare `value in _SEALED_CYCLE_STATES` raises
    `TypeError: unhashable type` for either -- an exception that is not a
    `ProducerError` and so is not caught by `main()`'s `except ProducerError
    as exc:` arm. Every state comparison, manifest side or record-cache side,
    goes through this one function; nowhere else tests set membership
    directly.
    """
    return isinstance(value, str) and value in _SEALED_CYCLE_STATES


def _record_cycle_manifest_path(root: Path, record: Mapping[str, Any]) -> Path:
    """Resolve this record's manifest without scanning other cycle bindings."""
    root = Path(root).resolve()
    campaign_id = record.get("campaign_id")
    cycle_id = record.get("cycle_id")
    if not artifact_identity.is_well_formed(campaign_id, "campaign") or not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("sealed-cycle-state-unknown", f"{cycle_id}: record identity invalid")
    try:
        campaigns = artifact_locator.safe_child(root, root, "campaigns")
    except artifact_locator.LocatorError as exc:
        raise ProducerError("sealed-cycle-state-unknown", exc.detail or exc.code) from exc
    matches = []
    if campaigns.is_dir() and not campaigns.is_symlink():
        for candidate in campaigns.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            campaign = _read_json(candidate / "campaign.json")
            if campaign is not None and campaign.get("campaign_id") == campaign_id:
                matches.append(candidate)
    if len(matches) != 1:
        raise ProducerError(
            "sealed-cycle-state-unknown",
            f"{cycle_id}: campaign-locator={'missing' if not matches else 'ambiguous'}",
        )
    parent = matches[0]
    locator = record.get("locator")
    try:
        if locator:
            cycle_path = artifact_locator.safe_child(root, parent, locator)
            binding = artifact_locator.read_cycle_binding(cycle_path)
        else:
            cycle_path = artifact_locator.safe_child(
                root, artifact_locator.safe_child(root, parent, "cycles"), cycle_id
            )
            # Legacy ``cycles/<cycle_id>`` directories predate cycle bindings.
            # If one is present it must still agree with the record; absence is
            # allowed only because the stable ID is the legacy path component.
            binding = artifact_locator.read_cycle_binding(cycle_path)
    except artifact_locator.LocatorError as exc:
        detail = exc.code if not exc.detail else f"{exc.code}: {exc.detail}"
        raise ProducerError("sealed-cycle-state-unknown", detail) from exc
    if binding is not None and (
        binding.get("campaign_id") != campaign_id
        or binding.get("cycle_id") != cycle_id
    ):
        raise ProducerError(
            "sealed-cycle-state-unknown",
            f"{cycle_id}: cycle-binding-identity-mismatch",
        )
    return cycle_path / "manifest.json"


def _published_cycle_state(root: Path, record: Mapping[str, Any]) -> str:
    """Return the *work* state of a sealed cycle.

    `record["state"] == "sealed"` is a storage fact -- it means an immutable
    snapshot exists, not that the work is done (D-10: the four completion
    results are independent). The work state is the published manifest's
    `cycle.state` (the D-6 folded state); `record["cycle_state"]` is a cache
    `_commit_sealed` copied from that same document. The cache is only a
    fallback when the canonical source is entirely absent. If the canonical
    source exists but cannot be trusted -- unreadable, wrong shape, naming a
    different cycle, or holding a value outside the enum -- this raises a
    typed refusal instead of ever returning a false success. The only
    exception this function can raise is `ProducerError`.
    """
    cycle_id = str(record.get("cycle_id", "?"))
    cached = record.get("cycle_state")
    record_state = cached if _valid_cycle_state(cached) else None
    try:
        manifest_path = cycle_dir(root, record["campaign_id"], cycle_id, record) / "manifest.json"
    except artifact_locator.LocatorError:
        # The global index can fail because of an unrelated binding. Resolve
        # this record through its own campaign/cycle locator before deciding
        # that the canonical manifest is absent.
        manifest_path = _record_cycle_manifest_path(root, record)
    except (ProducerError, OSError, KeyError, TypeError):
        manifest_path = _record_cycle_manifest_path(root, record)
    if manifest_path is None:
        raise ProducerError("sealed-cycle-state-unreadable", f"{cycle_id}: manifest=path-unresolved")
    try:
        manifest_stat = manifest_path.lstat()
    except FileNotFoundError:
        # Canonical source absent is the *only* case that falls back to the
        # cache (compatibility for W7G/W7I/W7H relocation roots carrying
        # legacy sealed records). A directory, special node, or symlink is
        # present-but-invalid and must never be mistaken for absence.
        if record_state is not None:
            return record_state
        shown = "missing" if cached is None else repr(cached)
        raise ProducerError("sealed-cycle-state-unknown", f"{cycle_id}: manifest=absent record={shown}")
    except OSError as exc:
        raise ProducerError(
            "sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} lookup-error={exc.__class__.__name__}:{exc}"
        ) from exc
    if not stat.S_ISREG(manifest_stat.st_mode):
        raise ProducerError(
            "sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} entry-kind=non-regular"
        )
    document = _read_json(manifest_path)
    if document is None:  # unparsable JSON, symlink, or encoding error
        raise ProducerError("sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} unparsable")
    # Structure checks come before any field access. `_read_json:168` already
    # guarantees a dict, but the invariant is pinned here too so it keeps
    # holding even if `_read_json` is loosened later.
    if not isinstance(document, Mapping):
        raise ProducerError(
            "sealed-cycle-state-unreadable",
            f"{cycle_id}: manifest={manifest_path} document-structure type={type(document).__name__}",
        )
    cycle = document.get("cycle")
    if not isinstance(cycle, Mapping):
        raise ProducerError(
            "sealed-cycle-state-unreadable",
            f"{cycle_id}: manifest={manifest_path} cycle-structure type={type(cycle).__name__}",
        )
    manifest_cycle_id = cycle.get("cycle_id")  # `!=` is safe against any type
    if manifest_cycle_id != record.get("cycle_id"):
        raise ProducerError(
            "sealed-cycle-state-unreadable",
            f"{cycle_id}: manifest={manifest_path} manifest_cycle_id={manifest_cycle_id!r} "
            f"record_cycle_id={record.get('cycle_id')!r}",
        )
    manifest_state = cycle.get("state")
    if not _valid_cycle_state(manifest_state):  # non-string and out-of-enum share one gate
        raise ProducerError(
            "sealed-cycle-state-unreadable", f"{cycle_id}: manifest={manifest_path} cycle_state={manifest_state!r}"
        )
    if record_state is not None and record_state != manifest_state:
        raise ProducerError("sealed-cycle-state-ambiguous", f"{cycle_id}: manifest={manifest_state} record={record_state}")
    # A damaged cache (non-string, out-of-enum, or absent) does not block
    # success once the canonical source is valid -- the canonical source wins.
    return manifest_state


def _authorize_active_cleanup(root: Path, operation: str, target: Path, cycle_id: Optional[str]) -> None:
    try:
        dispatch_terminal_commit.require_current_cleanup(operation, target=target, cycle_id=cycle_id)
    except dispatch_terminal_commit.TerminalCommitError as exc:
        raise ProducerError("cleanup-scope-violation", exc.detail) from exc


def _finalize_route(root: Path, record: Mapping[str, Any], *, deadline: Optional[float] = None) -> Dict[str, Any]:
    """D-120 finalize: R is the unique T(C) leaf -- the route with no
    material-input-qualifying continuation child. In a closed lineage the
    children that begin another cycle are that cycle's, so R stops before them
    (`closed_lineage_handover`). `--cycle`-only finalize has
    no other way to name R; a completion controller that already knows its
    exact route can seal it directly by checking `cycle_route_admission(...,
    finalize=True)` itself instead of calling this walk.  A background sweep passes its
    `deadline` (`time.monotonic()`): the walk stops before a level it has no time for and
    raises `scan-in-progress`, so the cycle stays open for the next sweep.
    """
    begin_route = load_route(root, Path(record["route_file"]))
    if begin_route["route_hash"] != record["route_hash"]:
        raise ProducerError("route-hash-drift", record["cycle_id"])
    current = begin_route
    visited = {current["route_id"]}
    handed_over: Optional[frozenset] = None  # computed once, and only when a child begins another cycle
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise ProducerError("scan-in-progress")
        candidates = [c for c in _lineage_children(root, current["route_id"], current["route_hash"])
                      if c.get("capability") == record.get("capability")
                      and c.get("effective_intensity") == record.get("intensity")]
        if candidates and handed_over is None:
            begin_ids = {rec.get("route_id") for rec in list_cycle_records(
                root, route_ids={c["route_id"] for c in candidates})
                         if rec.get("cycle_id") != record.get("cycle_id")}
            if any(c["route_id"] in begin_ids for c in candidates):
                handed_over = _handed_over_routes(root, record)
        if handed_over:
            candidates = [c for c in candidates if c["route_id"] not in handed_over]
        if not candidates:
            return current
        if len(candidates) > 1:
            raise ProducerError(
                "cycle-route-binding-mismatch:lineage-fork",
                f"{current['route_id']}:{','.join(sorted(c['route_id'] for c in candidates))}",
            )
        nxt = candidates[0]
        if nxt["route_id"] in visited:
            raise ProducerError("route-lineage-unverified", f"cycle:{nxt['route_id']}")
        visited.add(nxt["route_id"])
        current = nxt


def finalize(
    root: Path,
    *,
    cycle_id: str,
    state: str = "completed",
    primary: Optional[str] = None,
    publication: str = "not-offered",
    allow_open_route: bool = False,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
    crash_after_manifest: bool = False,
    exclude_hidden: bool = False,
    adopt_root_outputs: Sequence[str] = (),
    abandon_reason: Optional[str] = None,
    force_abandon_ignoring_lease: bool = False,
    support_locators: Sequence[str] = (),
    expected_binding: Optional[Mapping[str, Any]] = None,
    lock_timeout: Optional[float] = None,
    exclude_symlinks: bool = False,
    _admission_lock_fd: Optional[int] = None,
    _recovery_scope: str = "root",
    _prescan: Optional[_Prescan] = None,
    _last_try: bool = False,
    _scan_budget: Optional["RefreshBudget"] = None,
) -> Dict[str, Any]:
    """`lock_timeout` bounds the lock waits (default: the refresh, admission and
    checkpoint defaults); a background sweep passes 0 so a held lock defers it.
    The first close of an open cycle reads its files before the admission lock; `_scan_budget`
    (a background sweep's time) lets it stop between files and answer `deferred`.
    Symbolic links are always left out of the manifest and recorded as
    `excluded_symlinks` on the result and the cycle record (`exclude_symlinks`
    is accepted for old callers and changes nothing)."""
    root = Path(root).resolve()
    if _recovery_scope != "exact":
        dispatch_terminal_commit.require_current_cleanup("producer-finalize")
    _authorize_active_cleanup(root, "finalize-forward-recovery", root, cycle_id)
    if expected_binding is None:
        _observe_control_changes(root, cycle_id, now=now)
    if state not in {"completed", "abandoned"}:
        raise ProducerError("finalize-state-invalid", state)
    if _admission_lock_fd is not None:
        raise ProducerError("finalize-reentry-forbidden", cycle_id)
    # PRD §13.53.4(3) names the producer admission mutex as the lock that must
    # be released before `finalize()` is entered. The check above only catches
    # a caller that *passes* its fd; a caller holding the lock in its own
    # variable would otherwise block on `flock` for the full admission timeout
    # and surface as "busy". Refuse it here, typed, at the boundary.
    try:
        dispatch_lock_order.assert_not_held("producer-admission", "producer-finalize")
    except dispatch_lock_order.LockOrderError as error:
        raise ProducerError("finalize-reentry-forbidden", error.detail or cycle_id) from error
    alloc = allocator or artifact_identity.IdAllocator()
    # A cycle's files are read here, before the admission lock: a closed cycle closed again is
    # compared with its manifest, an open one is read for its first close (named files being
    # adopted are moved under the lock, so that close reads in it).  Under the lock only `lstat`
    # has to confirm what was read (§45 D-124); a scan found out of date is read once more
    # (`_prescan` on that second call).
    prescan = _prescan if _last_try else _prescan_cycle(
        root, cycle_id, unless_recorded=expected_binding, include_open=not adopt_root_outputs,
        exclude_hidden=exclude_hidden, budget=_scan_budget)
    lock_fd = _admission_lock_fd
    interim_guard = contextlib.ExitStack()
    sweep_unresolved: List[Dict[str, Any]] = []
    sealed: Optional[Dict[str, Any]] = None
    kept_scan: Optional[_Prescan] = None

    def _finish(payload: Dict[str, Any]) -> Dict[str, Any]:
        # duplicate-copy: an unrelated campaign the recovery sweep had to
        # isolate (§3.6a) is surfaced on every return, not swallowed.
        if sweep_unresolved:
            payload = dict(payload)
            payload["recovery_unresolved"] = sweep_unresolved
        return payload

    try:
        if prescan is not None and prescan.open_cycle:
            # First close.  The cycle's own refresh lock (before the admission lock, as a refresh
            # takes them) holds off a second close of it while `lstat` confirms what was read; the
            # admission lock then covers only the short write section below.
            if not prescan.complete:
                _keep_prescan_digests(root, cycle_id, prescan)
                return {"status": "deferred", "reason": "scan-budget", "cycle_id": cycle_id}
            interim_guard.enter_context(_refresh_lock(
                root, cycle_id, timeout=CHECKPOINT_FINALIZE_LOCK_SECONDS if lock_timeout is None else lock_timeout))
            misses = _prescan_misses(prescan.directory, prescan, prescan.paths)
            if misses and not _last_try:
                raise _StaleScan(cycle_id)
            if misses and _scan_budget is not None:
                _keep_prescan_digests(root, cycle_id, prescan)
                return {"status": "deferred", "reason": "files-changed-during-read", "cycle_id": cycle_id}
            if misses:
                _reread_misses(prescan, misses)
        if lock_fd is None:
            lock_fd = artifact_admission._acquire_lock(
                root, artifact_admission.LOCK_TIMEOUT_DEFAULT if lock_timeout is None else lock_timeout, now=now)
        if (expected_binding and isinstance(expected_binding, Mapping)
                and expected_binding.get("kind") == "inline_producer_binding_v1"):
            # The admission mutex is held. Check the actual route's verified
            # lineage, including valid continuations of the begin route.
            _inline_producer_binding_check(root, cycle_id, expected_binding)
        import inline_finish
        finish = inline_finish.pending_for_cycle(root, cycle_id)
        if finish and finish.get("state") != "finished":
            permitted = (
                isinstance(expected_binding, Mapping)
                and expected_binding.get("kind") == "inline_producer_binding_v1"
                and expected_binding.get("inline_finish_id") == finish.get("inline_finish_id")
                and finish.get("state") == "route-closed"
            )
            if not permitted:
                raise ProducerError("finish-in-progress", cycle_id)
        if _recovery_scope == "root":
            pre = read_cycle_record(root, cycle_id)
            sweep = _recover_locked(root, now=now,
                                    target_campaign_id=pre["campaign_id"] if pre else None)
            sweep_unresolved = sweep.get("unresolved", [])
        elif _recovery_scope == "exact":
            _recover_exact_cycle_locked(root, cycle_id, expected_binding, now=now)
        else:
            raise ProducerError("recovery-scope-invalid", _recovery_scope)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("deleted_at"):
            return _finish(_finalize_deleted_locked(root, record, state=state, now=now,
                                                    abandon_reason=abandon_reason))
        if record.get("state") == "sealed":
            # Storage sealing is not task completion: the manifest commit is an
            # immutable snapshot, and the *published* cycle state
            # (`_published_cycle_state`) is the only thing a re-finalize
            # request can be judged against. Any request whose `state` does
            # not match that published state is a conflict -- no flag makes
            # it idempotent, because the storage schema does not persist the
            # request fingerprint needed to prove "same retry" (D-8).
            published = _published_cycle_state(root, record)
            if published != state:
                if published == "active" and state == "completed":
                    refreshed = _refresh_cycle_locked(root, record, now=now, allocator=alloc,
                                                      crash_after_manifest=crash_after_manifest,
                                                      prescan=prescan, last_try=_last_try)
                    latest = read_cycle_record(root, cycle_id)
                    completed_state = _published_cycle_state(root, latest) if latest else None
                    if completed_state != "completed":
                        raise ProducerError(
                            "finalize-state-conflict",
                            f"{cycle_id}: requested=completed published_cycle_state=active storage_state=sealed",
                        )
                    return _finish({**refreshed, "storage_state": "sealed", "cycle_state": "completed"})
                raise ProducerError(
                    "finalize-state-conflict",
                    f"{cycle_id}: requested={state} published_cycle_state={published} storage_state=sealed",
                )
            # General relocation compatibility may judge an absent manifest
            # from its state cache. Terminal exact callers still require every
            # durable proof; an already-sealed status is never that proof.
            manifest_path = _record_cycle_manifest_path(root, record)
            if expected_binding is not None:
                if _binding_route_unrecorded(manifest_path, expected_binding):
                    # A route continuing this closed cycle finishes it again (§45 D-127): the
                    # document gets that route's own terminal record before it is proved.
                    _refresh_cycle_locked(root, record, now=now, allocator=alloc, prescan=prescan,
                                          last_try=_last_try)
                    record = read_cycle_record(root, cycle_id)
                verified = _verify_sealed_cycle_locked(root, record, expected_binding)
                return _finish({**verified, "storage_state": "sealed", "cycle_state": published})
            if _path_entry_present(manifest_path):
                # §45 D-123/D-124: closing again is the refresh -- the closed
                # cycle's files are compared with its manifest and the next
                # document is published when they differ.
                refreshed = _refresh_cycle_locked(root, record, now=now, allocator=alloc,
                                                  crash_after_manifest=crash_after_manifest,
                                                  prescan=prescan, last_try=_last_try)
                return _finish({**refreshed, "storage_state": "sealed", "cycle_state": published})
            return _finish({"status": "already-sealed", "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest"),
                    "storage_state": "sealed", "cycle_state": published})
        if record.get("state") != "open":
            raise ProducerError("cycle-not-open", record.get("state", "?"))
        first_close_read = prescan is not None and prescan.open_cycle
        if not first_close_read and not (_last_try or adopt_root_outputs):
            raise _StaleScan(cycle_id)  # the cycle was not open when it was read: read it, lock released
        # The open-cycle checkpoint assigns IDs under this lock; holding it until
        # the interim document is removed keeps a concurrent checkpoint from
        # republishing IDs the sealed manifest did not take.
        interim_guard.enter_context(_checkpoint_lock(
            root, cycle_id, timeout=CHECKPOINT_FINALIZE_LOCK_SECONDS if lock_timeout is None else lock_timeout))
        # A live review lease protects the report's exact write window from
        # both terminal outcomes.  The check remains under the producer
        # admission lock and happens before any terminal mutation.
        live_review = _live_review_lease(root, cycle_id, now=now)
        live_record = _read_json(live_review) if live_review is not None else None
        if state == "abandoned":
            # SD-117 L1 before L3 (plan-check C-2): live-lease enforcement
            # comes first -- a live registered review lease refuses the
            # abandon outright, zero events, zero record-state change
            # (E47-2), before the abandon_reason vocabulary is even
            # consulted.
            if not force_abandon_ignoring_lease and live_review is not None:
                lease = live_record
                reason = ("cycle-finalize-blocked-live-review"
                          if _is_v2_review_lease(lease)
                          else "cycle-abandon-blocked-live-review")
                raise ProducerError(reason, cycle_id)
            if force_abandon_ignoring_lease and abandon_reason not in (None, "operator-override-live-review"):
                raise ProducerError("abandon-reason-required", str(abandon_reason))
            if force_abandon_ignoring_lease:
                abandon_reason = "operator-override-live-review"
            if abandon_reason not in ABANDON_REASONS:
                raise ProducerError("abandon-reason-required", str(abandon_reason))
        elif live_review is not None:
            # SD-120 completed settlement consumes the v1/v2 union. Keep the
            # legacy abandon override above and the v2-only root recovery
            # policy separate from this completed publication boundary.
            raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        # D-120: the route that seals this cycle is the unique T(C) leaf, not
        # necessarily the begin route -- an inherited (rebound) cycle's begin
        # route may long since have a continuation writing it.
        try:
            route = _finalize_route(root, record,
                                    deadline=_scan_budget.deadline() if _scan_budget is not None else None)
        except ProducerError as exc:
            if exc.code != "scan-in-progress":
                raise
            if prescan is not None and prescan.open_cycle:
                _keep_prescan_digests(root, cycle_id, prescan)   # the next try reads only what is left
            return _finish({"status": "deferred", "reason": "scan-budget", "cycle_id": cycle_id})
        admission = cycle_route_admission(root, record, route, finalize=True)
        if not admission.allow:
            raise ProducerError(admission.reason, admission.detail)
        if _bind_cycle_route_locked(root, record, route)["written"]:
            # `_commit_sealed` below reseals from this local copy; refresh it
            # so the binding write just made is not clobbered back to stale.
            record = read_cycle_record(root, cycle_id)
        artifact_locator.prepare_index_update(root, [record["campaign_id"]])
        adopted_root_outputs: List[str] = []
        if adopt_root_outputs and state != "abandoned":
            raise ProducerError("root-output-adoption-requires-abandoned")
        adoption_moves: List[Tuple[str, Path, Path]] = []
        seen_adoptions: set[str] = set()
        for name in adopt_root_outputs:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ProducerError("root-output-adoption-name-invalid", name)
            if name in seen_adoptions:
                raise ProducerError("root-output-adoption-duplicate", name)
            seen_adoptions.add(name)
            source = directory / name
            target = directory / "artifacts" / name
            if source.is_symlink() or not source.is_file():
                raise ProducerError("root-output-adoption-source-invalid", name)
            if target.exists() or target.is_symlink():
                raise ProducerError("root-output-adoption-target-exists", name)
            adoption_moves.append((name, source, target))
        for name, source, target in adoption_moves:
            os.replace(source, target)
            adopted_root_outputs.append(name)
        placements = _output_placements(root, record)
        primary = _placed_locator(_cycle_relative_primary(primary, directory), placements)
        support_locators = tuple(_placed_locator(value, placements) for value in support_locators)
        excluded_hidden: List[str] = []
        excluded_symlinks: List[str] = []
        if first_close_read:
            if prescan.error is not None:
                raise prescan.error
            violations, facts = prescan.violations, prescan.facts
            excluded_hidden, excluded_symlinks = list(prescan.excluded), list(prescan.excluded_symlinks)
        else:
            # Only for files adopted just now (they were moved in under this lock) or a cycle
            # whose first read could not be made: read here, as the close always did.
            rows, violations = _enumerate_output(directory, exclude_hidden=exclude_hidden, excluded=excluded_hidden,
                                                 excluded_symlinks=excluded_symlinks)
            facts = [(rel, _digest(data), len(data)) for rel, data in rows]
        if violations:
            raise ProducerError("output-invalid", ";".join(violations))
        if not facts:
            # D-9: no durable output, no lineage. Unchanged except that an
            # abandoned empty cycle also carries its sealed abandon_reason
            # (E47-5: `_remove_empty_cycle` call and returned `status` stay
            # byte-identical either way).
            _remove_empty_cycle(root, record)
            record = dict(record)
            record["state"] = "abandoned" if state == "abandoned" else "no-lineage"
            record["sealed_on"] = _rfc3339(now)
            if state == "abandoned":
                record["abandon_reason"] = abandon_reason
            _write_cycle_record(root, record, exclusive=False)
            remove_interim(root, cycle_id)
            return _finish({"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False})
        # §45 D-123: a parent is a reference to a cycle of this root, not an
        # ordering constraint; the child closes whether the parent is open,
        # closed, or already deleted. The index only needs to know it is a cycle
        # of this root.
        parent_ids = (frozenset({record["parent_cycle_id"]})
                      if record.get("parent_cycle_id") and read_cycle_record(root, record["parent_cycle_id"])
                      else frozenset())
        reserved, reservation = read_interim_reservation(root, record)
        identity = artifact_lifecycle.read_root_identity(root)
        index = artifact_admission.load_index(root)
        # A reservation never blocks a seal: when a reused field makes the
        # document fail, rebuild with fresh events, then with fresh IDs, and say so.
        attempts: List[Tuple[Optional[InterimReservation], Optional[str]]] = [(reserved, None)]
        if reserved is not None:
            attempts += [(_without_event_reuse(reserved), "events-fresh"), (None, "ids-fresh")]
        for candidate, rebuilt in attempts:
            document = build_manifest(
                root, record, route, (), state=state,
                primary=_cycle_relative_primary(primary, directory),
                allow_open_route=allow_open_route, allocator=alloc, now=now,
                abandon_reason=abandon_reason, support_locators=support_locators,
                reserved=candidate, facts=facts,
            )
            report = artifact_manifest.validate(document)
            if not report.ok:
                failure = ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
                continue
            digest = artifact_manifest.manifest_digest(document)
            index_report = artifact_index.check(
                index, document, idempotency_key=cycle_id, manifest_digest=digest,
                repository_id=identity.repository_id if identity else None,
                known_parent_cycle_ids=parent_ids,
            )
            if not index_report.ok:
                failure = ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
                continue
            reserved, interim_rebuilt, failure = candidate, rebuilt, None
            break
        if failure is not None:
            raise failure
        if state == "completed" and route_is_closed(root, route):
            completion = artifact_lifecycle.evaluate_cycle_completion(
                document, content_root=directory,
                route_file=route_lineage.canonical_route_path(root, route["route_id"]),
                publication=publication, expected_root_id=identity.artifact_root_id if identity else None,
                inline_finish_id=(expected_binding.get("inline_finish_id")
                                  if isinstance(expected_binding, Mapping) else None),
                payload_verified=first_close_read,  # read in chunks and `lstat`-confirmed above
            )
            if not completion.ok:
                raise ProducerError(
                    "completion-rejected",
                    ";".join(f"{v.code}:{v.detail}" for v in completion.reasons),
                )
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            raise ProducerError("manifest-already-present", str(manifest_path))
        cycle_path = os.path.relpath(str(directory), str(root))
        if excluded_symlinks:
            # Written before the manifest is published so a crash after the commit
            # point recovers from the on-disk record without losing the exclusion.
            record = dict(record)
            record["excluded_symlinks"] = excluded_symlinks
            _write_cycle_record(root, record, exclusive=False)
        manifest_bytes = artifact_manifest.canonical_bytes(document)
        _write_journal(root, cycle_id, state="sealing", manifest_digest=digest, cycle_path=cycle_path,
                       manifest_revision_id=document["manifest_revision_id"])
        # §45 D-124: the document is kept as published before it is published.
        artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, manifest_bytes)
        # COMMIT POINT: exclusive manifest creation.
        _write_exclusive(manifest_path, manifest_bytes)
        if crash_after_manifest:  # test hook: simulate a crash after the commit point
            raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest publish")
        try:
            _write_journal(root, cycle_id, state="published", manifest_digest=digest, cycle_path=cycle_path)
            # The admission lock has been held since `index` was read, so it is still current.
            _commit_sealed(root, record, document, digest, now=now, index=index,
                           excluded_count=len(excluded_hidden) + len(excluded_symlinks))
        except BaseException as exc:
            raise artifact_admission.AdmissionRecoveryRequired(
                f"cycle {cycle_id} manifest published but post-publish update failed; run recover"
            ) from exc
        sealed_result = {"excluded_hidden": excluded_hidden, "excluded_symlinks": excluded_symlinks,
            "adopted_root_outputs": adopted_root_outputs,
            "status": "sealed", "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
            "manifest_digest": digest, "manifest_path": str(manifest_path),
            "artifact_count": len(facts), "lineage_committed": True, "cycle_state": document["cycle"]["state"],
            "storage_state": "sealed",
        }
        if reservation != "absent":
            # The interim document's IDs were (or, when unusable, were not)
            # carried into the sealed manifest; say which.
            kept = reserved.artifacts if reserved is not None else {}
            sealed_result["interim_ids"] = reservation
            if interim_rebuilt:
                sealed_result["interim_ids_rebuilt"] = interim_rebuilt
            sealed_result["interim_ids_kept"] = sum(
                1 for row in document["artifact_revisions"]
                if row["locator"]["path"] in kept
                and kept[row["locator"]["path"]].artifact_id == row["artifact_id"])
        if document["cycle"]["state"] == "active":
            sealed_result["provisional"] = True
            sealed_result["warning"] = PROVISIONAL_SEAL_WARNING
        sealed = _finish(sealed_result)
        if first_close_read:
            kept_scan = prescan
    except _StaleScan:
        pass  # read again below, with the lock released
    finally:
        interim_guard.close()
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)
    if sealed is not None:
        if kept_scan is not None:
            _keep_prescan_digests(root, cycle_id, kept_scan)  # the first refresh need not hash it all again
        return sealed
    return finalize(
        root, cycle_id=cycle_id, state=state, primary=primary, publication=publication,
        allow_open_route=allow_open_route, allocator=alloc, now=now, crash_after_manifest=crash_after_manifest,
        exclude_hidden=exclude_hidden, adopt_root_outputs=adopt_root_outputs, abandon_reason=abandon_reason,
        force_abandon_ignoring_lease=force_abandon_ignoring_lease, support_locators=support_locators,
        expected_binding=expected_binding, lock_timeout=lock_timeout, exclude_symlinks=exclude_symlinks,
        _recovery_scope=_recovery_scope, _last_try=True, _scan_budget=_scan_budget,
        _prescan=_prescan_cycle(root, cycle_id, unless_recorded=expected_binding,
                                include_open=not adopt_root_outputs, exclude_hidden=exclude_hidden,
                                budget=_scan_budget))


def _finalize_deleted_locked(root: Path, record: Mapping[str, Any], *, state: str, now: Optional[float],
                             abandon_reason: Optional[str]) -> Dict[str, Any]:
    """A cycle whose folder was deleted (§45 D-126) is closed by what is left of it.

    A cycle that was still running ends as a cycle with no output -- the existing `no-lineage`
    (or `abandoned`) record -- and nothing is written to disk: the folder is not made again and no
    manifest is published, so the route's work is never a material success.  A cycle that had
    closed is as it was closed; the answer carries `deleted`."""
    cycle_id = record["cycle_id"]
    if record.get("state") == "open":
        ended = dict(record)
        ended["state"] = "abandoned" if state == "abandoned" else "no-lineage"
        ended["sealed_on"] = _rfc3339(now)
        if state == "abandoned":
            ended["abandon_reason"] = abandon_reason if abandon_reason in ABANDON_REASONS else "route-unrecoverable"
        _write_cycle_record(root, ended, exclusive=False)
        remove_interim(root, cycle_id)
        return {"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False, "deleted": True}
    if _closed_record(record):
        return {"status": "already-sealed", "cycle_id": cycle_id, "manifest_digest": record.get("manifest_digest"),
                "storage_state": "sealed", "cycle_state": record.get("cycle_state"), "deleted": True}
    return {"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False, "deleted": True}


def _recover_exact_cycle_locked(root: Path, cycle_id: str, expected_binding: Optional[Mapping[str, Any]] = None,
                                *, now: Optional[float] = None) -> Dict[str, Any]:
    """Recover only one cycle journal while the admission lock is held."""
    journal_path = producer_dir(root) / "journal" / f"{cycle_id}.json"
    journal = _read_json(journal_path)
    if _path_entry_present(journal_path) and journal is None:
        raise ProducerError("cycle-journal-invalid", cycle_id)
    if journal and journal.get("cycle_id", cycle_id) != cycle_id:
        raise ProducerError("cycle-journal-identity-mismatch", cycle_id)
    if journal and expected_binding and journal.get("manifest_digest") != expected_binding.get("manifest_digest", journal.get("manifest_digest")):
        raise ProducerError("cycle-journal-binding-mismatch", cycle_id)
    if expected_binding:
        record = read_cycle_record(root, cycle_id)
        for key in ("campaign_id", "cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and (record is None or record.get(key) != expected):
                raise ProducerError("cycle-journal-binding-mismatch", key)
    record = read_cycle_record(root, cycle_id)
    if record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    if _live_review_lease(root, cycle_id, now=now) is not None:
        raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
    if record.get("state") == "sealed":
        if journal is not None and _refresh_journal_resume(root, record, journal, now=now):
            record = read_cycle_record(root, cycle_id)
        if expected_binding and _binding_route_unrecorded(_record_cycle_manifest_path(root, record), expected_binding):
            # A route that continues this closed cycle has no record in it yet; there is
            # nothing to recover, and `finalize` writes that record before it proves it.
            return {"status": "already-sealed", "cycle_id": cycle_id}
        return _verify_sealed_cycle_locked(root, record, expected_binding)
    if record.get("state") != "open":
        raise ProducerError("cycle-not-open", cycle_id)
    directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
    path = directory / "manifest.json"
    if _path_entry_present(path):
        document = _read_json(path)
        if (journal is None or document is None
                or (root / str(journal.get("cycle_path", ""))).resolve() != directory.resolve()
                or artifact_manifest.manifest_digest(document) != journal.get("manifest_digest")):
            raise ProducerError("cycle-journal-manifest-mismatch", cycle_id)
        identity = artifact_lifecycle.read_root_identity(root)
        manifest_route_file, _manifest_route = resolve_cycle_manifest_route(root, record, document)
        completion = artifact_lifecycle.evaluate_cycle_completion(
            document, content_root=directory, route_file=manifest_route_file,
            expected_root_id=identity.artifact_root_id if identity else None,
            inline_finish_id=expected_binding.get("inline_finish_id") if isinstance(expected_binding, Mapping) else None)
        if not completion.ok:
            raise ProducerError("completion-rejected", ";".join(v.code for v in completion.reasons))
        _commit_sealed(root, record, document, journal["manifest_digest"], now=now)
        return _verify_sealed_cycle_locked(root, read_cycle_record(root, cycle_id), expected_binding)
    if journal is not None:
        # Nothing crossed the manifest commit point; the same cycle can
        # re-enter finalize. No other journal or cycle is read or changed.
        _drop_unpublished_snapshot(root, cycle_id, journal)
        _remove_journal(root, cycle_id)
    return {"status": "open", "cycle_id": cycle_id}


def _earlier_documents(root: Path, cycle_id: str, document: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The cycle's preserved documents other than `document` itself."""
    return [old for _raw, old in artifact_lifecycle.read_manifest_snapshots(root, cycle_id)
            if old.get("manifest_revision_id") != document.get("manifest_revision_id")]


def _verify_sealed_cycle_locked(root: Path, record: Mapping[str, Any],
                                expected_binding: Optional[Mapping[str, Any]] = None,
                                expected_manifest_digest: Optional[str] = None) -> Dict[str, Any]:
    """Prove a closed cycle's record, current manifest and index agree (§45 D-127).

    What is checked is identity and consistency: the cycle record, the current
    `manifest.json` and the index row all describe the same document, and the
    terminal record that closed the cycle is in it.  Whether the files still
    hold those bytes is not asked here -- a later edit is the next refresh's
    business, never a reason to call a finished cycle unfinished.
    `expected_manifest_digest` is the revision a caller recorded when it
    finished; it may be an earlier one, and the result then says the cycle was
    updated since (`updated_since`) while still returning that recorded digest.
    """
    if record.get("state") != "sealed" or not record.get("sealed_on"):
        raise ProducerError("already-sealed-mismatch", "cycle-record")
    if expected_binding is not None:
        for key in ("cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and record.get(key) != expected:
                raise ProducerError("already-sealed-mismatch", key)
        if (expected_binding.get("cycle_record_digest")
                and not dispatch_terminal_commit.cycle_identity_matches(
                    record, expected_binding["cycle_record_digest"],
                    campaign_id=expected_binding.get("campaign_id"))):
            raise ProducerError("already-sealed-mismatch", "cycle-identity")
    manifest_path = _record_cycle_manifest_path(root, record)
    directory = manifest_path.parent
    try:
        raw = manifest_path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProducerError("already-sealed-mismatch", str(manifest_path)) from exc
    canonical = artifact_manifest.canonical_bytes(document)
    digest = artifact_manifest.manifest_digest(document)
    if raw != canonical or digest != record.get("manifest_digest"):
        raise ProducerError("already-sealed-mismatch", "manifest")
    identity = artifact_lifecycle.read_root_identity(root)
    index = artifact_admission.load_index(root)
    row = index.manifests.get(record["cycle_id"]) if hasattr(index, "manifests") else None
    if not isinstance(row, dict) or row.get("manifest_digest") != digest:
        raise ProducerError("already-sealed-mismatch", "index")
    if expected_binding is not None:
        cycle_id = record["cycle_id"]
        cycle = document.get("cycle", {})
        if (identity is None or index.artifact_root_id != identity.artifact_root_id
                or document.get("artifact_root_id") != identity.artifact_root_id
                or cycle.get("cycle_id") != cycle_id
                or cycle.get("campaign_id") != record["campaign_id"]
                or document.get("producer", {}).get("producer_id") != record["producer_id"]):
            raise ProducerError("already-sealed-mismatch", "index-identity")
        # Use the canonical writer's projection, scoped to this exact cycle.
        # Root-wide rebuild/repair could touch unrelated open cycles and is
        # not evidence that this transaction's two index rows were applied.
        expected_index = artifact_index.apply(
            artifact_index.empty(identity.artifact_root_id), document,
            cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=cycle_id,
        )
        if (row != expected_index.manifests[cycle_id]
                or index.cycles.get(cycle_id) != expected_index.cycles[cycle_id]):
            raise ProducerError("already-sealed-mismatch", "index-projection")
    updated_since = False
    if expected_manifest_digest is not None and expected_manifest_digest != digest:
        # The caller finished at an earlier revision of this cycle.  A preserved
        # copy proves it existed; with none (the cycle changed before it was
        # first observed), the caller's own terminal record is the proof.
        updated_since = True
    manifest_route_file, _manifest_route = resolve_cycle_manifest_route(
        root, record, document, route_id=(expected_binding or {}).get("route_id"))
    if document.get("cycle", {}).get("state") == "completed":
        completion = artifact_lifecycle.evaluate_cycle_completion(
            document, content_root=directory, route_file=manifest_route_file,
            expected_root_id=identity.artifact_root_id if identity else None,
            inline_finish_id=(expected_binding.get("inline_finish_id")
                              if isinstance(expected_binding, Mapping) else None),
            preserved=_earlier_documents(root, record["cycle_id"], document))
        if not completion.ok:
            raise ProducerError("already-sealed-mismatch", "completion-evidence")
    result = {"status": "already-sealed", "cycle_id": record["cycle_id"],
              "manifest_digest": expected_manifest_digest or digest}
    if updated_since:
        result["current_manifest_digest"] = digest
        result["updated_since"] = True
    return result


# ---------------------------------------------------------------------------
# refresh: the next manifest document of a closed cycle (§45 D-124)
# ---------------------------------------------------------------------------

_HASH_CHUNK_BYTES = 1024 * 1024


def _stream_file_facts(path: Path) -> Tuple[str, int]:
    """`(digest, size)` of one regular file read in chunks, never as one buffer.

    The file is opened without following a link, and a size or mtime that moves
    while it is read discards the answer (a write in flight is not a revision)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ProducerError("non-regular-file", str(path))
        hasher = hashlib.sha256()
        size = 0
        with os.fdopen(os.dup(fd), "rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
                size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != after.st_size:
        raise ProducerError("output-changed-while-reading", str(path))
    return "sha256:" + hasher.hexdigest(), size


def _scan_cycle_facts(directory: Path, *, excluded: List[str],
                      excluded_symlinks: List[str],
                      known: Optional[Mapping[str, Sequence[Any]]] = None,
                      fingerprints: Optional[Dict[str, List[int]]] = None) -> List[Tuple[str, str, int]]:
    """The manifest's file facts `(path, digest, size)` under the one inclusion rule.

    Same rule as `_enumerate_output`, read in chunks.  A path a manifest cannot
    name (not a regular file, not a valid locator) is left out and listed as
    excluded: a refresh never turns such a file into a failure.  `known` maps a
    path to `(size, mtime_ns, inode, digest)` from a scan made before the lock was
    taken: a file whose fingerprint still matches keeps that digest instead of
    being read again.  `fingerprints`, when given, receives each file's fingerprint as it was
    before its bytes were read, so a later `lstat` tells whether the digest still stands."""
    facts: List[Tuple[str, str, int]] = []
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    for entry in _walk_files(directory):
        rel = entry.relative_to(directory).as_posix()
        if rel == artifact_locator.CYCLE_BINDING or not rel.startswith("artifacts/"):
            continue
        if os.path.islink(str(entry)):
            excluded_symlinks.append(rel)
            continue
        if _outside_inclusion_rule(rel) or not entry.is_file() or not artifact_manifest.validate_locator_path(rel).ok:
            excluded.append(rel)
            continue
        remembered = (known or {}).get(rel)
        try:
            st = os.lstat(str(entry))
        except OSError:
            st = None
        if remembered is not None and st is not None and list(remembered[:3]) == _fingerprint(st):
            facts.append((rel, remembered[3], st.st_size))
            if fingerprints is not None:
                fingerprints[rel] = _fingerprint(st)
            continue
        digest, size = _stream_file_facts(entry)
        facts.append((rel, digest, size))
        if fingerprints is not None and st is not None:
            fingerprints[rel] = _fingerprint(st)
    return facts


def _refreshed_document(root: Path, record: Mapping[str, Any], document: Mapping[str, Any],
                        facts: Sequence[Tuple[str, str, int]], *, allocator: artifact_identity.IdAllocator,
                        now: Optional[float],
                        removable: Optional[Iterable[str]] = None,
                        ) -> Tuple[Optional[Dict[str, Any]], Dict[str, List[str]]]:
    """The cycle's next document, or `None` when no file differs from `document`.

    Unchanged rows are carried over untouched; a changed file keeps its
    `artifact_id` and gets a new revision; a new file gets a new artifact; a file
    that is gone leaves the rows (its events stay).  A row the inclusion rule
    would not list today is neither changed nor removed.  `cycle`, `routes[]` and
    the terminal fields are never touched.  `removable`, when given, names the only
    declared paths a missing fact may remove: a scan that stopped early has not seen
    the rest of the tree, so a file it did not visit is never read as deleted."""
    declared = {row.get("locator", {}).get("path"): row
                for row in document.get("artifact_revisions", []) or [] if isinstance(row, dict)}
    present = {rel: (digest, size) for rel, digest, size in facts}
    changes: Dict[str, List[str]] = {"added": [], "modified": [], "removed": []}
    for rel, (digest, size) in sorted(present.items()):
        row = declared.get(rel)
        if row is None:
            changes["added"].append(rel)
        elif row.get("content_digest") != digest or row.get("byte_size") != size:
            changes["modified"].append(rel)
    removable_set = None if removable is None else set(removable)
    for rel in sorted(declared):
        if (rel not in present and not _outside_inclusion_rule(rel)
                and (removable_set is None or rel in removable_set)):
            changes["removed"].append(rel)
    if not any(changes.values()):
        return None, changes
    revision_id = allocator.allocate("manifest_revision")
    when = _rfc3339(now)
    routes = [row for row in document.get("routes", []) or [] if isinstance(row, dict)]
    route_id = routes[-1]["route_id"] if routes else record.get("route_id")

    def provenance(digest: str) -> Dict[str, Any]:
        return {"source_manifest_id": document["manifest_id"], "source_revision_id": revision_id,
                "producer_route_id": route_id, "algorithm_version": ALGORITHM_VERSION,
                "schema_version": 1, "source_digest": digest}

    removed = set(changes["removed"])
    gone_artifacts = {row["artifact_id"] for rel, row in declared.items() if rel in removed}
    modified = set(changes["modified"])
    artifacts = [dict(row) for row in document.get("artifacts", []) or []
                 if isinstance(row, dict) and row.get("artifact_id") not in gone_artifacts]
    revisions: List[Dict[str, Any]] = []
    events = [dict(row) for row in document.get("events", []) or []]
    for row in document.get("artifact_revisions", []) or []:
        rel = row.get("locator", {}).get("path")
        if rel in removed:
            continue
        if rel in modified:
            digest, size = present[rel]
            row = dict(row, artifact_revision_id=allocator.allocate("artifact_revision"),
                       content_digest=digest, byte_size=size, media_type=_media_type(rel),
                       provenance=provenance(digest))
            events.append({
                "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
                "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": row["artifact_id"],
                "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
                "provenance": row["provenance"], "evidence_ids": [], "payload": {"locator": rel}})
        revisions.append(dict(row))
    for rel in changes["added"]:
        digest, size = present[rel]
        artifact_id = allocator.allocate("artifact")
        inner = rel[len("artifacts/"):]
        artifacts.append({"artifact_id": artifact_id, "cycle_id": record["cycle_id"], "role": "output",
                          "type": _bucket_type(inner), "capability": record["capability"], "title": inner})
        revisions.append({
            "artifact_revision_id": allocator.allocate("artifact_revision"), "artifact_id": artifact_id,
            "revision_sequence": 1, "content_digest": digest, "byte_size": size, "media_type": _media_type(rel),
            "locator": {"kind": "cycle-relative", "path": rel}, "provenance": provenance(digest)})
        events.append({
            "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": artifact_id,
            "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
            "provenance": provenance(digest), "evidence_ids": [], "payload": {"locator": rel}})
    refreshed = dict(document)
    refreshed.update(manifest_revision_id=revision_id, artifacts=artifacts, artifact_revisions=revisions,
                     events=events)
    return refreshed, changes


def _drop_unpublished_snapshot(root: Path, cycle_id: str, journal: Optional[Mapping[str, Any]]) -> None:
    """A journal that never reached its commit point leaves no copy behind: a
    preserved document is a published one."""
    revision_id = (journal or {}).get("manifest_revision_id")
    if not isinstance(revision_id, str) or not artifact_identity.is_well_formed(revision_id, "manifest_revision"):
        return
    try:
        artifact_lifecycle.manifest_snapshot_path(root, cycle_id, revision_id).unlink()
    except (OSError, artifact_lifecycle.LifecycleError):
        pass


def _leaf_terminal_additions(root: Path, record: Mapping[str, Any], document: Mapping[str, Any],
                             facts: Sequence[Tuple[str, str, int]], *,
                             allocator: artifact_identity.IdAllocator, revision_id: str,
                             now: Optional[float]) -> Optional[Dict[str, Any]]:
    """The route row and terminal record a lineage leaf adds when it closes a closed cycle again.

    §45 D-127: a route that continues a closed cycle and closes leaves one more completion
    record; the earlier route's row and record stay as they were.  A route already in the
    document, one still open, one the lineage does not admit, or one whose terminal proof
    does not verify adds nothing -- a refresh never fails for want of one."""
    try:
        leaf = _finalize_route(root, record)
    except ProducerError:
        return None
    if (artifact_lifecycle.manifest_route_row(document, route_id=leaf["route_id"]) is not None
            or not route_is_closed(root, leaf)):
        return None
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None or not cycle_route_admission(root, record, leaf, finalize=True).allow:
        return None
    try:
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_lineage.canonical_route_path(root, leaf["route_id"]),
            expected_root_id=identity.artifact_root_id)
    except artifact_lifecycle.LifecycleError:
        return None
    if sealed_route.get("route_hash") != leaf["route_hash"]:
        return None
    event_id = allocator.allocate("event")
    cycle_digest = _digest(_canonical([[rel, digest] for rel, digest, _size in facts]))
    event = {
        "event_id": event_id, "stream_id": allocator.allocate("stream"), "stream_sequence": 1,
        "event_type": "route.terminal.recorded", "target_id": record["cycle_id"],
        "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": _rfc3339(now),
        "provenance": {"source_manifest_id": document["manifest_id"], "source_revision_id": revision_id,
                       "producer_route_id": leaf["route_id"], "algorithm_version": ALGORITHM_VERSION,
                       "schema_version": 1, "source_digest": cycle_digest},
        "evidence_ids": [], "payload": {}}
    row = {"artifact_root_id": identity.artifact_root_id, "route_id": leaf["route_id"],
           "route_hash": leaf["route_hash"], "terminal_marker": "pending", "terminal_evidence_id": event_id}
    return {"row": row, "event": event, "binding": binding, "route": sealed_route}


def _provisional_completion_context(root: Path, record: Mapping[str, Any],
                                    expected_root_id: Optional[str]) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """The live conditions under which a provisional active cycle may become completed.

    Route, lease, closed/admitted route, exact proven non-abandoned outcome: the one place both
    the candidate builder and the publication re-check ask, so they cannot drift apart."""
    try:
        route = _finalize_route(root, record)
    except ProducerError:
        return None
    if (route.get("artifact_root") is None
            or record.get("abandon_reason") or record.get("deleted_at")
            or _live_review_lease(root, record["cycle_id"]) is not None
            or not route_is_closed(root, route)
            or not cycle_route_admission(root, record, route, finalize=True).allow):
        return None
    try:
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, route_lineage.canonical_route_path(root, route["route_id"]),
            expected_root_id=expected_root_id)
        outcome_raw = Path(binding.outcome_file).read_bytes()
        outcome = json.loads(outcome_raw.decode("utf-8"))
        route_module = artifact_lifecycle._load_capability_route()
        gates = route_module.terminal_gate_observation(
            sealed_route, exact_terminal=outcome.get("terminal_commit_id") is not None)
        from workflow_state import WorkflowLedger
        workflow = WorkflowLedger(
            sealed_route["route_id"], sealed_route["route_hash"],
            jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None).read_only_state()
        if workflow["workflow_state"] == "CANCELLED" or workflow["journal_unreadable"]:
            return None
    except (artifact_lifecycle.LifecycleError, OSError, ValueError, UnicodeError):
        return None
    if (sealed_route.get("route_hash") != route.get("route_hash")
            or route_module.terminal_gate_proven(gates) is not True):
        return None
    if (outcome.get("route_id") != binding.route_id
            or outcome.get("route_hash") != binding.route_hash
            or outcome.get("route_file") != binding.route_file
            or outcome.get("terminal_gate_proven") is not True
            or outcome.get("autoclose") is not None
            or outcome.get("disposition") in ("abandoned", "operator-decision", "cancelled")):
        return None
    return binding, sealed_route, outcome


def _evaluate_provisional_completion(root: Path, record: Mapping[str, Any], candidate: Mapping[str, Any],
                                     binding: Any, outcome: Mapping[str, Any]) -> bool:
    """First-completion proof of a completed candidate: required roles, payload, marker, terminal digests."""
    try:
        decision = artifact_lifecycle.evaluate_cycle_completion(
            candidate, content_root=_record_cycle_manifest_path(root, record).parent,
            route_file=route_lineage.canonical_route_path(root, binding.route_id),
            expected_root_id=binding.artifact_root_id, payload_verified=False,
            inline_finish_id=outcome.get("inline_finish_id"))
    except (artifact_lifecycle.LifecycleError, OSError, ValueError):
        return False
    return decision.ok


def _provisional_completion_holds(root: Path, record: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """Re-ask, at publication, what the candidate was built on (called under the admission lock)."""
    context = _provisional_completion_context(root, record, candidate.get("artifact_root_id"))
    if context is None:
        return False
    binding, _sealed_route, outcome = context
    return _evaluate_provisional_completion(root, record, candidate, binding, outcome)


def _provisional_completion_projection(root: Path, record: Mapping[str, Any], document: Mapping[str, Any],
                                       facts: Sequence[Tuple[str, str, int]], *, revision_id: str,
                                       allocator: artifact_identity.IdAllocator,
                                       now: Optional[float]) -> Optional[Dict[str, Any]]:
    """Bind exact late terminal proof to one provisional active cycle revision."""
    if not isinstance(document.get("cycle"), Mapping) or document["cycle"].get("state") != "active":
        return None
    context = _provisional_completion_context(root, record, document.get("artifact_root_id"))
    if context is None:
        return None
    binding, sealed_route, outcome = context
    rows = [row for row in document.get("routes", []) or [] if isinstance(row, dict)
            and row.get("artifact_root_id") == binding.artifact_root_id
            and row.get("route_id") == binding.route_id and row.get("route_hash") == binding.route_hash]
    if len(rows) != 1 or rows[0].get("terminal_marker") != "pending" or rows[0].get("terminal_evidence_id"):
        return None
    candidate = json.loads(json.dumps(document))
    candidate["manifest_revision_id"] = revision_id
    candidate["cycle"]["state"] = "completed"
    when = _rfc3339(now)
    cycle_digest = _digest(_canonical([[rel, digest, size] for rel, digest, size in facts]))
    provenance = {"source_manifest_id": document["manifest_id"], "source_revision_id": revision_id,
                  "producer_route_id": binding.route_id, "algorithm_version": ALGORITHM_VERSION,
                  "schema_version": 1, "source_digest": cycle_digest}
    cycle_event = {"event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
                   "stream_sequence": 1, "event_type": "cycle.completed", "target_id": record["cycle_id"],
                   "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
                   "provenance": provenance, "evidence_ids": [], "payload": {}}
    terminal_event = {"event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
                      "stream_sequence": 1, "event_type": "route.terminal.recorded",
                      "target_id": record["cycle_id"],
                      "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": when,
                      "provenance": provenance, "evidence_ids": [], "payload": {}}
    row = next(row for row in candidate["routes"] if row.get("artifact_root_id") == binding.artifact_root_id
               and row.get("route_id") == binding.route_id and row.get("route_hash") == binding.route_hash)
    row["terminal_evidence_id"] = terminal_event["event_id"]
    candidate["events"] = list(candidate.get("events", [])) + [cycle_event, terminal_event]
    try:
        candidate = artifact_lifecycle._derive_terminal_evidence(candidate, binding, sealed_route)
    except (artifact_lifecycle.LifecycleError, OSError, ValueError):
        return None
    return candidate if _evaluate_provisional_completion(root, record, candidate, binding, outcome) else None


def _binding_route_unrecorded(manifest_path: Path, binding: Mapping[str, Any]) -> bool:
    """Whether the binding names a route (by id, or by hash) the current manifest has no row for."""
    document = _read_json(manifest_path) if _path_entry_present(manifest_path) else None
    if document is None or not (binding.get("route_id") or binding.get("route_hash")):
        return False
    return artifact_lifecycle.manifest_route_row(
        document, route_id=binding.get("route_id"), route_hash=binding.get("route_hash")) is None


def _refresh_journal_resume(root: Path, record: Mapping[str, Any], journal: Mapping[str, Any],
                            *, now: Optional[float] = None) -> bool:
    """Finish a refresh that stopped between its first durable step and its last.

    The journal names the document the refresh meant to publish; its preserved
    copy holds the same bytes.  When `manifest.json` is still the earlier
    document, the copy is published as it was; when it already is the new one,
    only the record and index remain.  A manifest that is neither is not this
    refresh's to settle.  Returns whether the journal was a refresh."""
    if journal.get("state") != "refreshing":
        return False
    cycle_id = record["cycle_id"]
    manifest_path = _record_cycle_manifest_path(root, record)
    new_digest = journal.get("manifest_digest")
    previous_digest = journal.get("previous_manifest_digest")
    current = _read_json(manifest_path)
    current_digest = artifact_manifest.manifest_digest(current) if current is not None else None
    if current_digest == previous_digest and current_digest != new_digest:
        copy = artifact_lifecycle.find_manifest_snapshot(root, cycle_id, manifest_digest=new_digest)
        if copy is None:
            # Nothing was published and the copy never became durable: drop the journal.
            _remove_journal(root, cycle_id)
            return True
        _write_atomic(manifest_path, artifact_manifest.canonical_bytes(copy))
        current, current_digest = copy, new_digest
    if current_digest != new_digest:
        # A person changed/deleted the current file after publication. Leave
        # those bytes alone and abandon this old write intent automatically.
        carried = journal.get("history_pending")
        if isinstance(carried, list):
            _write_cycle_record(root, _with_pending(record, _merge_pending(record.get("history_pending") or [],
                                [entry for entry in carried if isinstance(entry, dict)])),
                                exclusive=False)
            _flush_cycle_pending_locked(root, cycle_id)
        _remove_journal(root, cycle_id)
        return True
    carried = journal.get("history_pending")
    if isinstance(carried, list):
        # History lines the refresh could not hand over before it stopped belong to the record.
        record = _with_pending(record, _merge_pending(record.get("history_pending") or [],
                               [entry for entry in carried if isinstance(entry, dict)]))
    _commit_sealed(root, record, current, new_digest, now=now, previous_digest=previous_digest)
    return True


class _StaleScan(Exception):
    """What a closed cycle's files were read as before the lock no longer holds under it."""


@dataclass
class _Prescan:
    """A cycle's files as they were read with no lock held: the manifest digest they were
    compared with, the `(path, digest, size)` facts, and each file's fingerprint.

    `open_cycle` marks the first close of a cycle with no manifest yet: `directory` and `paths`
    (every file it listed) are what the refresh lock's `lstat` check walks, `violations` and
    `error` are what listing it found (raised under the lock, where the close always raised them),
    and `complete` is false when a budget stopped the reading."""
    manifest_digest: str
    facts: List[Tuple[str, str, int]]
    fingerprints: Dict[str, List[int]]
    excluded: List[str]
    excluded_symlinks: List[str]
    open_cycle: bool = False
    complete: bool = True
    directory: Optional[Path] = None
    paths: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    error: Optional[ProducerError] = None


def _prescan_cycle(root: Path, cycle_id: str, *,
                   unless_recorded: Optional[Mapping[str, Any]] = None,
                   include_open: bool = False, exclude_hidden: bool = False,
                   budget: Optional["RefreshBudget"] = None) -> Optional[_Prescan]:
    """Walk and hash a cycle's files before any lock is taken (§45 D-124).

    An explicit `finalize` or `admit-shared` of a closed cycle compares its files with the manifest;
    reading them here means the admission lock only has to `lstat` what changed.  `None` when
    there is nothing to read (the cycle is not closed, has no manifest, or `unless_recorded` names
    a route the manifest already holds) or the read could not be made: an optimisation, never an
    error.  With `include_open`, `finalize` reads an open cycle's files the same way for its first
    close; `budget` then stops the reading between files (`complete` false), keeping the digests read."""
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None or record.get("deleted_at"):
            return None
        if record.get("state") == "open":
            return _prescan_open_cycle(root, record, exclude_hidden=exclude_hidden, budget=budget) \
                if include_open else None
        if record.get("state") != "sealed":
            return None
        manifest_path = _record_cycle_manifest_path(root, record)
        if not _path_entry_present(manifest_path):
            return None
        if unless_recorded is not None and not _binding_route_unrecorded(manifest_path, unless_recorded):
            return None
        if not stat.S_ISREG(os.lstat(str(manifest_path)).st_mode):
            return None  # a link or a pipe is for the locked command to refuse, never to be read here
        document = json.loads(manifest_path.read_bytes().decode("utf-8"))
        scan = _Prescan(artifact_manifest.manifest_digest(document), [], {}, [], [])
        scan.facts = _scan_cycle_facts(
            manifest_path.parent, excluded=scan.excluded, excluded_symlinks=scan.excluded_symlinks,
            known=_read_refresh_state(root, cycle_id).get("files", {}), fingerprints=scan.fingerprints)
        return scan
    except Exception:  # noqa: BLE001 -- only an optimisation
        return None


def _prescan_open_cycle(root: Path, record: Mapping[str, Any], *, exclude_hidden: bool,
                        budget: Optional["RefreshBudget"]) -> Optional[_Prescan]:
    """The first close's read: list an open cycle, then read each file once, outside every lock.

    A file whose fingerprint is the one an earlier look left in the refresh bookkeeping keeps
    that digest.  A file that moves while it is read has no fingerprint here, so the `lstat`
    check under the lock finds it.  A budget is looked at between files, never inside one, and
    the first file read is always finished."""
    cycle_id = record["cycle_id"]
    directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
    if _path_entry_present(directory / "manifest.json"):
        return None  # a close that stopped after its manifest is for recovery to finish
    scan = _Prescan("", [], {}, [], [], open_cycle=True, directory=directory)
    try:
        paths, scan.violations = _output_paths(directory, exclude_hidden=exclude_hidden, excluded=scan.excluded,
                                               excluded_symlinks=scan.excluded_symlinks)
    except ProducerError as error:
        scan.error = error  # raised where the locked close always raised it
        return scan
    scan.paths = [rel for rel, _entry in paths]
    known = _read_refresh_state(root, cycle_id).get("files", {})
    for rel, entry in paths:
        try:
            before = os.lstat(str(entry))
        except OSError:
            continue
        fingerprint = _fingerprint(before)
        remembered = known.get(rel)
        if remembered is not None and list(remembered[:3]) == fingerprint:
            scan.facts.append((rel, remembered[3], before.st_size))
            scan.fingerprints[rel] = fingerprint
            continue
        if budget is not None and budget.hashed_files and budget.hash_exhausted():
            scan.complete = False
            break
        try:
            digest, size = _stream_file_facts(entry)
            after = os.lstat(str(entry))
        except (ProducerError, OSError):
            continue  # moved while it was read, or not a plain file now
        if budget is not None:
            budget.hashed_bytes += size
            budget.hashed_files += 1
        if _fingerprint(after) == fingerprint:
            scan.facts.append((rel, digest, size))
            scan.fingerprints[rel] = fingerprint
    return scan


def _keep_prescan_digests(root: Path, cycle_id: str, prescan: _Prescan) -> None:
    """Leave the digests a first close read where the next look finds them (best effort).

    A close that read only part of a big cycle continues from them, and the first refresh after
    the close does not hash the whole cycle again.  `observed_at` is left as it was: this is no
    refresh, so it starts no minimum interval."""
    try:
        state = _read_refresh_state(root, cycle_id)
        files = dict(state.get("files", {}))
        for rel, digest, _size in prescan.facts:
            if rel in prescan.fingerprints:
                files[rel] = [*prescan.fingerprints[rel], digest]
        _write_refresh_state(root, cycle_id, cursor=None, files=files, observed_at=state.get("observed_at"))
    except (OSError, ProducerError):
        pass


def _prescan_misses(directory: Path, prescan: _Prescan, rels: Sequence[str], *,
                    absent: Sequence[str] = ()) -> List[str]:
    """Under a lock, with `lstat` only: the files whose state is no longer what the scan saw.

    `rels` are files the scan read (their fingerprint must be the same), `absent` are files it
    found gone (they must still be)."""
    misses: List[str] = []
    for rel in rels:
        try:
            held = _fingerprint(os.lstat(str(directory / rel))) == prescan.fingerprints.get(rel)
        except OSError:
            held = False
        if not held:
            misses.append(rel)
    for rel in absent:
        try:
            os.lstat(str(directory / rel))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            pass
        misses.append(rel)
    return misses


def _prescan_holds(directory: Path, prescan: _Prescan, changes: Mapping[str, Sequence[str]]) -> bool:
    """Under the lock, with `lstat` only: is what the scan saw of the changed files still true?"""
    return not _prescan_misses(directory, prescan, list(changes.get("added", [])) + list(changes.get("modified", [])),
                               absent=changes.get("removed", []))


def _reread_misses(prescan: _Prescan, misses: Sequence[str]) -> None:
    """The first close's second look, for a scan that no longer held: read only the files that moved.

    The refresh lock is held and the admission lock is not.  A file that is gone leaves the
    facts; one that still moves while it is read fails the close with `output-changed-while-reading`."""
    directory = prescan.directory
    read = {rel: (digest, size) for rel, digest, size in prescan.facts}
    gone = set()
    for rel in misses:
        path = directory / rel
        try:
            before = os.lstat(str(path))
            read[rel] = _stream_file_facts(path)
            after = os.lstat(str(path))
        except (FileNotFoundError, NotADirectoryError):
            read.pop(rel, None)
            prescan.fingerprints.pop(rel, None)
            gone.add(rel)
            continue
        if _fingerprint(after) != _fingerprint(before):
            raise ProducerError("output-changed-while-reading", str(path))
        prescan.fingerprints[rel] = _fingerprint(before)
    prescan.paths = [rel for rel in prescan.paths if rel not in gone]
    prescan.facts = [(rel, *read[rel]) for rel in prescan.paths if rel in read]


def _refresh_cycle_locked(root: Path, record: Mapping[str, Any], *, now: Optional[float] = None,
                          allocator: Optional[artifact_identity.IdAllocator] = None,
                          crash_after_manifest: bool = False,
                          prescan: Optional[_Prescan] = None, last_try: bool = True,
                          trigger: str = "finalize") -> Dict[str, Any]:
    """Re-finalize a closed cycle: publish its next manifest document if any file changed.

    The admission lock is held and no folder is walked or hashed under it: `prescan` is the
    files as they were read before the lock, and only the changed files' fingerprints, the removed
    files' absence and the manifest digest are looked at again here (§45 D-124).  A scan that no
    longer holds raises `_StaleScan` for the caller to release the lock and read again; with
    `last_try` it publishes no file change this time instead (the next look finds the same one).
    Order, so that a crash anywhere resumes by itself (`_refresh_journal_resume`): the earlier
    document is preserved (a cycle closed before copies existed is first seen here), the journal
    names the new document, the new document is preserved before it is published,
    `manifest.json` is replaced atomically, then the index row is swapped on its earlier digest and
    the record follows.  Nothing changed means nothing is written."""
    cycle_id = record["cycle_id"]
    manifest_path = _record_cycle_manifest_path(root, record)
    directory = manifest_path.parent
    try:
        raw = manifest_path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProducerError("already-sealed-mismatch", str(manifest_path)) from exc
    digest = artifact_manifest.manifest_digest(document)
    # The index row is read once, here, and answers for the whole refresh: a manifest the
    # index does not carry is not something to publish a next document over.
    index = artifact_admission.load_index(root)
    row = index.manifests.get(cycle_id)
    if not isinstance(row, dict) or row.get("manifest_digest") != digest:
        previous = artifact_lifecycle.find_manifest_snapshot(
            root, cycle_id, manifest_digest=record.get("manifest_digest"))
        if previous is None or not isinstance(row, dict) or row.get("manifest_digest") != record.get("manifest_digest"):
            return {"status": "already-sealed", "cycle_id": cycle_id, "refreshed": False,
                    "manifest_digest": record.get("manifest_digest"), "changes": {"added": [], "modified": [], "removed": []}}
        # The current file is ordinary editable material. Continue observing
        # payloads against the recorded revision without changing past proof.
        document = previous
        raw = artifact_manifest.canonical_bytes(document)
        digest = record["manifest_digest"]
    alloc = allocator or artifact_identity.IdAllocator()
    deferred = False
    refreshed: Optional[Dict[str, Any]] = None
    if prescan is not None:
        excluded, excluded_symlinks = list(prescan.excluded), list(prescan.excluded_symlinks)
        facts = prescan.facts
        refreshed, changes = _refreshed_document(root, record, document, facts, allocator=alloc, now=now)
        deferred = refreshed is not None and not _prescan_holds(directory, prescan, changes)
    else:
        deferred = True
    if deferred:
        if not last_try:
            raise _StaleScan(cycle_id)
        # The document as it is: no file change is published this time, a terminal record still can be.
        excluded, excluded_symlinks, refreshed = [], [], None
        facts = [(r["locator"]["path"], r["content_digest"], r["byte_size"])
                 for r in document.get("artifact_revisions", []) or [] if isinstance(r, dict)]
        changes = {"added": [], "modified": [], "removed": []}
    revision_id = refreshed["manifest_revision_id"] if refreshed is not None else alloc.allocate("manifest_revision")
    provisional = _provisional_completion_projection(root, record, refreshed or document, facts,
                                                     revision_id=revision_id, allocator=alloc, now=now)
    if provisional is not None:
        refreshed = provisional
        additions = None
    else:
        additions = _leaf_terminal_additions(root, record, document, facts, allocator=alloc,
                                             revision_id=revision_id, now=now)
    if additions is not None:
        base = json.loads(json.dumps(refreshed if refreshed is not None
                                     else dict(document, manifest_revision_id=revision_id)))
        base["routes"] = list(base.get("routes", [])) + [additions["row"]]
        base["events"] = list(base.get("events", [])) + [additions["event"]]
        try:
            refreshed = artifact_lifecycle._derive_terminal_evidence(base, additions["binding"], additions["route"])
        except artifact_lifecycle.LifecycleError:
            additions = None
    result = {"status": "already-sealed", "cycle_id": cycle_id, "refreshed": refreshed is not None,
              "changes": changes, "excluded_hidden": excluded, "excluded_symlinks": excluded_symlinks,
              "manifest_digest": digest}
    if deferred:
        result["deferred"] = "files-changed-during-read"
    if additions is not None:
        result["terminal_added"] = additions["row"]["route_id"]
    if refreshed is None:
        carried = list(record.get("history_pending") or [])
        if carried and _history_deliver_locked(root, carried):
            _write_history_pending(root, record, [])
        return result
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, raw)
    earlier = _earlier_documents(root, cycle_id, refreshed)
    report = artifact_manifest.validate_update(refreshed, preserved=earlier, previous=document)
    if not report.ok:
        raise ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
    new_digest = artifact_manifest.manifest_digest(refreshed)
    new_raw = artifact_manifest.canonical_bytes(refreshed)
    identity = artifact_lifecycle.read_root_identity(root)
    index_report = artifact_index.check(
        index, refreshed, idempotency_key=cycle_id, manifest_digest=new_digest,
        repository_id=identity.repository_id if identity else None,
        replaces_manifest_digest=digest)
    if not index_report.ok:
        raise ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
    cycle_path = os.path.relpath(str(directory), str(root))
    # §45 D-125: the file lines go to the recorder before the new document is published; a
    # recorder that is not there (or fails) leaves them in the record, so the explicit command
    # still publishes current bytes and the next trigger hands the lines over.
    lines = _refresh_history_lines(root, record, directory, document, refreshed, changes, trigger=trigger,
                                   now=time.time() if now is None else float(now))
    pending = _merge_pending(record.get("history_pending") or [], lines)
    if _history_deliver_locked(root, pending):
        pending = []
    _write_journal(root, cycle_id, state="refreshing", manifest_digest=new_digest,
                   previous_manifest_digest=digest, cycle_path=cycle_path,
                   manifest_revision_id=refreshed["manifest_revision_id"], history_pending=pending)
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, new_raw)
    # COMMIT POINT: atomic replacement of the current document.
    _write_atomic(manifest_path, new_raw)
    if crash_after_manifest:  # test hook: simulate a crash after the commit point
        raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest refresh")
    try:
        _commit_sealed(root, _with_pending(record, pending), refreshed, new_digest, now=now, index=index,
                       previous_digest=digest)
    except BaseException as exc:
        raise artifact_admission.AdmissionRecoveryRequired(
            f"cycle {cycle_id} manifest refreshed but post-publish update failed; run recover") from exc
    result.update(manifest_digest=new_digest, manifest_path=str(manifest_path))
    return result


# ---------------------------------------------------------------------------
# refresh without a long lock: bounded scan, cursor, history (§45 D-124, D-125)
# ---------------------------------------------------------------------------
#
# A closed cycle is looked at in three steps and only the last one takes a lock:
#
#   1. walk, lstat and hash the files with no lock at all, inside one budget
#      (`RefreshBudget`), reusing the digest of a file whose (size, mtime_ns,
#      inode) is what the last scan saw;
#   2. under the cycle's own *refresh* lock (a file under `checkpoints/refresh/`
#      that nothing but a refresh takes, so the open-cycle order "admission, then
#      checkpoint" of `finalize` is never crossed) re-check the changed files and
#      the manifest digest with `lstat` only;
#   3. two short admission sections: the history lines, then the manifest, index
#      row and record.  A recorder that is not there leaves the lines in the
#      record's `history_pending`; a recorder that fails leaves the manifest as
#      it was, and the next trigger finds the same change again.
#
# Nothing here raises to the command that triggered it: a lock that is busy, a
# file that moved while it was read or a recorder that failed is a `skipped`.

REFRESH_DIR = "refresh"
REFRESH_MAX_WALK_ENTRIES = 20000
REFRESH_MAX_HASH_BYTES = 256 * 1024 * 1024
REFRESH_MAX_SECONDS = 60.0
REFRESH_ADMISSION_WAIT_SECONDS = 5.0
REFRESH_ABSENCE_CHECK_FLOOR = 1000
_UNLIMITED = float("inf")
_HISTORY_TOKEN = re.compile(r"^[\x21-\x7e]{1,128}$")


class RefreshBudget:
    """One refresh run's share of walking, hashing and time.

    The budget is looked at between files, never inside one, and a run always
    finishes the first file it starts hashing, so a file larger than the whole
    byte budget is still reflected by a single run.  `unlimited()` is for the
    caller that asked for the answer now (an explicit `finalize` or checkpoint)."""

    def __init__(self, max_walk_entries: float = REFRESH_MAX_WALK_ENTRIES,
                 max_hash_bytes: float = REFRESH_MAX_HASH_BYTES,
                 max_seconds: float = REFRESH_MAX_SECONDS) -> None:
        self.max_walk_entries = max_walk_entries
        self.max_hash_bytes = max_hash_bytes
        self.max_seconds = max_seconds
        self.walked = 0
        self.hashed_bytes = 0
        self.hashed_files = 0
        self._started = time.monotonic()

    @classmethod
    def unlimited(cls) -> "RefreshBudget":
        return cls(_UNLIMITED, _UNLIMITED, _UNLIMITED)

    def walk_exhausted(self) -> bool:
        return self.walked >= self.max_walk_entries or time.monotonic() - self._started >= self.max_seconds

    def hash_exhausted(self) -> bool:
        return self.hashed_bytes >= self.max_hash_bytes or time.monotonic() - self._started >= self.max_seconds

    def exhausted(self) -> bool:
        return self.walk_exhausted() or self.hash_exhausted()

    def deadline(self) -> Optional[float]:
        """The `time.monotonic()` value its time share ends at, `None` when it has none."""
        return None if self.max_seconds == _UNLIMITED else self._started + self.max_seconds


def _fingerprint(st: os.stat_result) -> List[int]:
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def _refresh_state_dir(root: Path) -> Path:
    return producer_dir(root) / CHECKPOINT_DIR / REFRESH_DIR


def _refresh_state_path(root: Path, cycle_id: str) -> Path:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("cycle-id-invalid", str(cycle_id))
    return _refresh_state_dir(root) / f"{cycle_id}.json"


def _refresh_cursor_path(root: Path) -> Path:
    return _refresh_state_dir(root) / "cursor.json"


@contextlib.contextmanager
def _refresh_lock(root: Path, cycle_id: str, *, timeout: float):
    """The cycle's refresh lock.  Only a refresh and the first close of an open cycle take it, and
    both take it before the admission lock, so it can be held while that lock is waited for."""
    path = _refresh_state_path(root, cycle_id).with_suffix(".lock")
    _ensure_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProducerError("refresh-lock-busy", cycle_id)
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _read_refresh_state(root: Path, cycle_id: str) -> Dict[str, Any]:
    """The last scan's bookkeeping: `cursor`, `observed_at` and `files`
    (path -> [size, mtime_ns, inode, digest]).  It has no authority: a missing or
    odd file just means the next run starts over."""
    try:
        raw = _read_json(_refresh_state_path(root, cycle_id))
    except ProducerError:
        return {}
    if not isinstance(raw, dict):
        return {}
    files = raw.get("files")
    kept: Dict[str, List[Any]] = {}
    if isinstance(files, dict):
        for rel, row in files.items():
            if (isinstance(rel, str) and isinstance(row, list) and len(row) == 4
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in row[:3])
                    and isinstance(row[3], str)):
                kept[rel] = row
    cursor = raw.get("cursor")
    observed = raw.get("observed_at")
    return {"cursor": cursor if isinstance(cursor, str) and cursor else None, "files": kept,
            "observed_at": observed if isinstance(observed, (int, float)) and not isinstance(observed, bool) else None}


def _write_refresh_state(root: Path, cycle_id: str, *, cursor: Optional[str], files: Mapping[str, Sequence[Any]],
                         observed_at: Optional[float]) -> None:
    path = _refresh_state_path(root, cycle_id)
    _ensure_dir(path.parent)
    _write_atomic(path, _json_bytes({"schema_version": 1, "cycle_id": cycle_id, "cursor": cursor,
                                     "observed_at": observed_at, "files": dict(sorted(files.items()))}), 0o644)


@dataclass
class _RefreshScan:
    facts: Dict[str, Tuple[str, int, List[int]]] = field(default_factory=dict)
    visited: Set[str] = field(default_factory=set)
    excluded: List[str] = field(default_factory=list)
    excluded_symlinks: List[str] = field(default_factory=list)
    complete: bool = True
    cursor: Optional[str] = None
    from_top: bool = True


def _pruned_directory(parts: Sequence[str]) -> bool:
    """A directory the inclusion rule leaves out whole (hidden component or `__pycache__`)."""
    return any(part.startswith(".") or part == "__pycache__" for part in parts)


def _walk_sorted(top: str, prefix: Tuple[str, ...], after: Optional[Tuple[str, ...]]):
    """Entries under `top` in name order, depth first, as `(parts, DirEntry, is_dir)`.

    `after` is the last entry an earlier run finished: everything up to it is
    skipped without a `stat`.  A directory the rule leaves out is yielded once and
    not entered."""
    try:
        entries = sorted(os.scandir(top), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        parts = prefix + (entry.name,)
        is_dir = entry.is_dir(follow_symlinks=False)
        enter = is_dir and not _pruned_directory(parts)
        if after is not None:
            head = after[:len(parts)]
            if parts < head:
                continue
            if parts == after:
                if enter:
                    yield from _walk_sorted(entry.path, parts, None)
                continue
            if parts == head:
                if enter:
                    yield from _walk_sorted(entry.path, parts, after)
                continue
        yield parts, entry, is_dir
        if enter:
            yield from _walk_sorted(entry.path, parts, None)


def _bounded_scan(directory: Path, *, cursor: Optional[str], known: Mapping[str, Sequence[Any]],
                  budget: RefreshBudget) -> _RefreshScan:
    """Walk a closed cycle's `artifacts/` with no lock and read what the budget allows.

    The same inclusion rule as the close (`_outside_inclusion_rule`, symbolic
    links by `lstat`).  A file whose fingerprint matches `known` is not read; any
    other is hashed in chunks, and a file that moves while it is read is left out
    of this run.  The budget is looked at between entries; the run stops with a
    cursor naming the last entry it finished."""
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    after = tuple(cursor.split("/")) if cursor else None
    scan = _RefreshScan(from_top=after is None)
    last_parts: Optional[Tuple[str, ...]] = None
    processed = 0
    for parts, entry, is_dir in _walk_sorted(str(artifacts), (), after):
        if processed and budget.walk_exhausted():
            scan.complete, scan.cursor = False, "/".join(last_parts) if last_parts else cursor
            return scan
        budget.walked += 1
        processed += 1
        rel = "artifacts/" + "/".join(parts)
        if entry.is_symlink():
            scan.excluded_symlinks.append(rel)
            last_parts = parts
            continue
        if is_dir:
            if _pruned_directory(parts):
                scan.excluded.append(rel + "/")
            last_parts = parts
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            scan.visited.add(rel)
            last_parts = parts
            continue
        if not stat.S_ISREG(st.st_mode) or _outside_inclusion_rule(rel) \
                or not artifact_manifest.validate_locator_path(rel).ok:
            scan.excluded.append(rel)
            last_parts = parts
            continue
        scan.visited.add(rel)
        fingerprint = _fingerprint(st)
        remembered = known.get(rel)
        if remembered is not None and list(remembered[:3]) == fingerprint:
            scan.facts[rel] = (remembered[3], st.st_size, fingerprint)
            last_parts = parts
            continue
        if budget.hashed_files and budget.hash_exhausted():
            scan.visited.discard(rel)
            scan.complete, scan.cursor = False, "/".join(last_parts) if last_parts else cursor
            return scan
        try:
            digest, size = _stream_file_facts(Path(entry.path))
            after_stat = os.lstat(entry.path)
        except (ProducerError, OSError):
            last_parts = parts
            continue  # moved while it was read, or not a plain file now: the next run looks again
        budget.hashed_bytes += size
        budget.hashed_files += 1
        if _fingerprint(after_stat) != fingerprint:
            last_parts = parts
            continue
        scan.facts[rel] = (digest, size, fingerprint)
        last_parts = parts
    return scan


def _confirmed_absent(directory: Path, document: Mapping[str, Any], scan: _RefreshScan,
                      budget: RefreshBudget) -> List[str]:
    """Declared files this run did not see and that are really gone (`lstat`).

    A scan that stopped early has not seen the rest of the tree, so a file it did not
    visit is never read as deleted: only an `lstat` that finds nothing is."""
    gone: List[str] = []
    remaining = (_UNLIMITED if budget.max_walk_entries == _UNLIMITED
                 else max(0, int(budget.max_walk_entries) - budget.walked))
    allowance = remaining + REFRESH_ABSENCE_CHECK_FLOOR
    for row in document.get("artifact_revisions", []) or []:
        rel = row.get("locator", {}).get("path") if isinstance(row, dict) else None
        if not isinstance(rel, str) or rel in scan.visited or _outside_inclusion_rule(rel):
            continue
        if allowance <= 0:
            break
        allowance -= 1
        budget.walked += 1
        try:
            os.lstat(str(directory / rel))
        except (FileNotFoundError, NotADirectoryError):
            gone.append(rel)
        except OSError:
            continue
    return gone


# -- the history lines of D-125 --------------------------------------------


def _history_module():
    """The one history recorder, or `None` when it is not importable (not merged yet)."""
    try:
        import artifact_history  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 -- a missing or broken recorder is a pending line, never a failure
        return None
    if not all(callable(getattr(artifact_history, name, None)) for name in ("make_event", "publish_events_locked")):
        return None
    return artifact_history


def _history_id(prefix: str, *parts: str) -> str:
    """A stable ID, so that a line delivered twice after a crash is the same line."""
    return prefix + "_" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _history_actor(default_by: str) -> Dict[str, Any]:
    """`make_event` actor keyword, from the recorder's own rules: a person or an agent session through
    `actor_from_env(default_by)`, a runtime that observed a change without seeing who wrote it
    (`rule`) with the session markers it carries.  Without a recorder the line keeps only `actor_by`."""
    module = _history_module()
    if module is None:
        return {"actor_by": default_by}
    try:
        if default_by != "rule":
            return {"actor": module.actor_from_env(default_by)}

        def marker(name: str) -> Optional[str]:
            value = os.environ.get(name)
            return value if isinstance(value, str) and _HISTORY_TOKEN.match(value) else None

        attempt = marker("AGENT_DISPATCH_ATTEMPT_ID")
        return {"actor": module.make_actor(
            "rule", session=attempt, harness=marker("AGENT_DISPATCH_CURRENT_HARNESS"),
            route=marker("AGENT_ROUTE_ID"), attempt=attempt)}
    except Exception:  # noqa: BLE001 -- an actor the recorder cannot build is recorded as the default
        return {"actor_by": default_by}


def _history_by(default_by: str) -> str:
    """Who the recorder says is acting (`human`, `agent`, `rule`), for a record field that names it."""
    actor = _history_actor(default_by)
    return str((actor.get("actor") or {}).get("by") or actor.get("actor_by") or default_by)


def _history_file_ref(row: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if row is None:
        return {"value": None}
    return {"digest": row["content_digest"], "bytes": row["byte_size"]}


def _refresh_history_lines(root: Path, record: Mapping[str, Any], directory: Path,
                           previous: Mapping[str, Any], refreshed: Mapping[str, Any],
                           changes: Mapping[str, Sequence[str]], *, trigger: str,
                           now: float) -> List[Dict[str, Any]]:
    """One `make_event` argument set for each file added, changed or removed since the close.

    File content is only ever a digest and a size.  The IDs come from the cycle, the
    new document and the file, and the time is fixed here, so a line that is built again
    for the same change is the same line."""
    old_rows = {row["locator"]["path"]: row for row in previous.get("artifact_revisions", []) or []}
    new_rows = {row["locator"]["path"]: row for row in refreshed.get("artifact_revisions", []) or []}
    cycle_id = record["cycle_id"]
    revision_id = refreshed["manifest_revision_id"]
    transaction_id = _history_id("htxn", "refresh", cycle_id, revision_id)
    actor = _history_actor("rule")
    cycle_rel = Path(os.path.relpath(str(directory), str(root))).as_posix()
    lines: List[Dict[str, Any]] = []
    for operation, key in (("add", "added"), ("update", "modified"), ("delete", "removed")):
        for rel in changes.get(key, []):
            before, after = old_rows.get(rel), new_rows.get(rel)
            target = after or before
            if target is None:
                continue
            lines.append({
                "kind": "artifact", "target_type": "artifact", "target_id": target["artifact_id"],
                "target_path": f"{cycle_rel}/{rel}", "operation": operation, "field": rel,
                "before": _history_file_ref(before), "after": _history_file_ref(after),
                "reason": trigger, "transaction_id": transaction_id,
                "event_id": _history_id("hevt", "refresh", cycle_id, revision_id, operation, rel),
                "now": now, **actor})
    return lines


def _close_history_line(root: Path, record: Mapping[str, Any], document: Mapping[str, Any], digest: str,
                        directory: Path, *, excluded: int, now: Optional[float]) -> Dict[str, Any]:
    """The one lifecycle line a first close leaves: state, manifest digest and revision, file count."""
    revision_id = document["manifest_revision_id"]
    route_id = record.get("route_id")
    return {
        "kind": "lifecycle", "target_type": "cycle", "target_id": record["cycle_id"],
        "target_path": Path(os.path.relpath(str(directory), str(root))).as_posix(),
        "operation": "update", "field": "state", "before": {"value": "open"},
        "after": {"value": {"state": document["cycle"]["state"], "manifest_digest": digest,
                            "revision_id": revision_id,
                            "files": len(document.get("artifact_revisions", []) or []), "excluded": excluded}},
        "reason": f"finalize {route_id}" if route_id else "finalize",
        "transaction_id": _history_id("htxn", "close", record["cycle_id"], revision_id),
        "event_id": _history_id("hevt", "close", record["cycle_id"], revision_id),
        "now": time.time() if now is None else float(now), **_history_actor("human")}


def _history_deliver_locked(root: Path, lines: Sequence[Mapping[str, Any]]) -> bool:
    """Hand lines to the recorder while the admission lock is held.

    `False` when there is no recorder or it failed: the caller keeps the lines (and
    must not call a change recorded).  A line the recorder rejects as malformed is
    dropped rather than kept forever: it could never be delivered."""
    if not lines:
        return True
    module = _history_module()
    if module is None:
        return False
    try:
        events = []
        for line in lines:
            try:
                events.append(module.make_event(**dict(line)))
            except Exception:  # noqa: BLE001
                continue
        if events:
            module.publish_events_locked(root, events)
        return True
    except Exception:  # noqa: BLE001 -- the recorder failing never fails the change
        return False


def _merge_pending(carried: Sequence[Mapping[str, Any]], lines: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    seen = {entry.get("event_id") for entry in carried}
    return [dict(entry) for entry in carried] + [dict(line) for line in lines if line.get("event_id") not in seen]


def _with_pending(record: Mapping[str, Any], pending: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    updated = dict(record)
    if pending:
        updated["history_pending"] = [dict(entry) for entry in pending]
    else:
        updated.pop("history_pending", None)
    return updated


def _write_history_pending(root: Path, record: Mapping[str, Any], pending: Sequence[Mapping[str, Any]]) -> None:
    """Rewrite a cycle record with only its `history_pending` changed (the admission lock is held).

    No locator row input is touched, so this is a leaf record write with no index effect."""
    _write_cycle_record(root, _with_pending(record, pending), exclusive=False)


def _deliver_pending(root: Path, cycle_id: str, *, timeout: float) -> str:
    """Hand a cycle record's `history_pending` lines to the recorder and clear what was taken."""
    if _history_module() is None:
        return "pending"
    fd = artifact_admission._acquire_lock(root, timeout)
    try:
        record = read_cycle_record(root, cycle_id)
        pending = (record or {}).get("history_pending") or []
        if not pending:
            return "none"
        if not _history_deliver_locked(root, pending):
            return "pending"
        _write_history_pending(root, record, [])
        return "delivered"
    finally:
        artifact_admission._release_lock(root, fd)


def _recheck_candidate(directory: Path, manifest_path: Path, raw: bytes, scan: _RefreshScan,
                       changes: Mapping[str, Sequence[str]]) -> bool:
    """Under the refresh lock, with `lstat` only: is what the scan saw still true?"""
    try:
        if manifest_path.read_bytes() != raw:
            return False
        for rel in list(changes.get("added", [])) + list(changes.get("modified", [])):
            if _fingerprint(os.lstat(str(directory / rel))) != scan.facts[rel][2]:
                return False
        for rel in changes.get("removed", []):
            try:
                os.lstat(str(directory / rel))
            except (FileNotFoundError, NotADirectoryError):
                continue
            return False
    except OSError:
        return False
    return True


def _remember_scan(root: Path, cycle_id: str, scan: _RefreshScan, state: Mapping[str, Any], *,
                   published: bool, observe: bool, clock: float) -> None:
    """Keep the scan's fingerprints and cursor where the next run finds them.

    Written when a document was published, when the run stopped early, and when a sweep
    looks at a cycle for the first time; never for a cycle a trigger named that had
    nothing to say (that run touches nothing)."""
    previous = dict(state.get("files", {}))
    files = {rel: [*fp, digest] for rel, (digest, _size, fp) in scan.facts.items()}
    if not (scan.complete and scan.from_top):
        files = {**previous, **files}
    if not (published or not scan.complete or (observe and files != previous)
            or (scan.complete and state.get("cursor") is not None)):
        return
    try:
        _write_refresh_state(root, cycle_id, cursor=scan.cursor, files=files, observed_at=clock)
    except (OSError, ProducerError):
        pass


def refresh_cycle(
    root: Path, cycle_id: str, *, trigger: str = "explicit", now: Optional[float] = None,
    budget: Optional[RefreshBudget] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    observe: bool = False, crash_after_manifest: bool = False,
) -> Dict[str, Any]:
    """Observe one closed cycle (§45 D-124): publish its next manifest document if a file changed.

    Every outcome is a result, none is an error for the caller: `emitted` (a new
    document is current), `unchanged`, or `skipped` with a `reason` (busy, the cycle moved
    while it was scanned, the recorder was unavailable, ...) -- a skipped run changed
    nothing and the next trigger finds the same work again.  A run that changes nothing
    writes nothing.  `explicit` ignores the interval and the budget."""
    root = Path(root).resolve()
    _observe_control_changes(root, cycle_id, now=now)
    clock = time.time() if now is None else float(now)
    base = {"cycle_id": cycle_id, "trigger": trigger}
    if budget is None:
        budget = RefreshBudget.unlimited() if trigger == "explicit" else RefreshBudget()

    def skipped(reason: str, **extra: Any) -> Dict[str, Any]:
        return {"status": "skipped", "reason": reason, **base, **extra}

    if closed_cycle_refresh_off():
        return skipped("refresh-off")
    try:
        return _refresh_cycle_observed(root, cycle_id, trigger=trigger, clock=clock, budget=budget,
                                       allocator=allocator, observe=observe,
                                       crash_after_manifest=crash_after_manifest, base=base, skipped=skipped)
    except (artifact_admission.AdmissionBusy, dispatch_lock_order.LockOrderError) as exc:
        return skipped("busy", detail=str(exc))
    except ProducerError as exc:
        return skipped("busy" if exc.code == "refresh-lock-busy" else exc.code, detail=exc.detail)
    except (artifact_lifecycle.LifecycleError, artifact_locator.LocatorError, OSError, ValueError) as exc:
        return skipped(type(exc).__name__, detail=str(exc))


def _refresh_cycle_observed(root: Path, cycle_id: str, *, trigger: str, clock: float, budget: RefreshBudget,
                            allocator: Optional[artifact_identity.IdAllocator], observe: bool,
                            crash_after_manifest: bool, base: Mapping[str, Any], skipped) -> Dict[str, Any]:
    record = read_cycle_record(root, cycle_id)
    if record is None:
        return skipped("cycle-unknown")
    if record.get("deleted_at"):
        return skipped("cycle-deleted")
    if record.get("state") != "sealed":
        return skipped("cycle-not-closed", cycle_state=record.get("state"))
    manifest_path = _record_cycle_manifest_path(root, record)
    if not _path_entry_present(manifest_path):
        return skipped("manifest-absent")
    if journal_path(root, cycle_id).exists():
        return skipped("journal-pending")  # the next locked command finishes it
    raw = manifest_path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return skipped("manifest-unreadable")
    digest = artifact_manifest.manifest_digest(document)
    if record.get("manifest_digest") != digest:
        return {"status": "unchanged", **base, "manifest_digest": record.get("manifest_digest")}
    state = _read_refresh_state(root, cycle_id)
    observed_at = state.get("observed_at")
    if (trigger != "explicit" and observed_at is not None and state.get("cursor") is None
            and clock - observed_at < checkpoint_interval_seconds()):
        return skipped("min-interval", next_eligible_at=_rfc3339(observed_at + checkpoint_interval_seconds()))
    directory = manifest_path.parent
    scan = _bounded_scan(directory, cursor=None if trigger == "explicit" else state.get("cursor"),
                         known=state.get("files", {}), budget=budget)
    gone = _confirmed_absent(directory, document, scan, budget)
    alloc = allocator or artifact_identity.IdAllocator()
    facts = [(rel, digest_, size) for rel, (digest_, size, _fp) in sorted(scan.facts.items())]
    refreshed, changes = _refreshed_document(root, record, document, facts, allocator=alloc, now=clock,
                                             removable=gone)
    if scan.complete:
        revision_id = refreshed["manifest_revision_id"] if refreshed is not None else alloc.allocate("manifest_revision")
        provisional = _provisional_completion_projection(root, record, refreshed or document, facts,
                                                         revision_id=revision_id, allocator=alloc, now=clock)
        if provisional is not None:
            refreshed = provisional
    result = {"status": "unchanged", **base, "changes": changes, "complete": scan.complete,
              "cursor": scan.cursor, "walked": budget.walked, "hashed_bytes": budget.hashed_bytes,
              "excluded_hidden": scan.excluded, "excluded_symlinks": scan.excluded_symlinks,
              "manifest_digest": digest}
    if refreshed is None:
        if record.get("history_pending"):
            result["history"] = _deliver_pending(root, cycle_id, timeout=REFRESH_ADMISSION_WAIT_SECONDS)
        _remember_scan(root, cycle_id, scan, state, published=False, observe=observe, clock=clock)
        return result
    new_digest = artifact_manifest.manifest_digest(refreshed)
    earlier = {old.get("manifest_revision_id"): old for old in _earlier_documents(root, cycle_id, refreshed)}
    earlier[document.get("manifest_revision_id")] = document
    report = artifact_manifest.validate_update(refreshed, preserved=list(earlier.values()), previous=document)
    if not report.ok:
        return skipped("manifest-invalid", detail=";".join(v.code for v in report.violations))
    lines = _refresh_history_lines(root, record, directory, document, refreshed, changes, trigger=trigger, now=clock)
    with _refresh_lock(root, cycle_id, timeout=0.0):
        outcome = _publish_refresh(
            root, cycle_id, manifest_path, raw, digest, refreshed, new_digest, scan, changes, lines,
            now=clock, crash_after_manifest=crash_after_manifest)
    if outcome is not None:
        return skipped(outcome)
    _remember_scan(root, cycle_id, scan, state, published=True, observe=observe, clock=clock)
    result.update(status="emitted", manifest_digest=new_digest, manifest_path=str(manifest_path),
                  history="pending" if not _history_published(root, cycle_id) else "published")
    return result


def _history_published(root: Path, cycle_id: str) -> bool:
    record = read_cycle_record(root, cycle_id) or {}
    return not record.get("history_pending")


def _publish_refresh(root: Path, cycle_id: str, manifest_path: Path, raw: bytes, digest: str,
                     refreshed: Mapping[str, Any], new_digest: str, scan: _RefreshScan,
                     changes: Mapping[str, Sequence[str]], lines: Sequence[Mapping[str, Any]], *,
                     now: float, crash_after_manifest: bool) -> Optional[str]:
    """Publish a prepared next document; `None` on success, else why this run skipped.

    The refresh lock is held and the admission lock is not.  The files are re-checked with
    `lstat`, then one admission section does the rest in the D-125 order: the record, the
    current document, a provisional completion's live conditions and the index row are read again, the history lines go to the recorder, the
    document is replaced, the index row is swapped and the record follows.  Anything that no
    longer holds writes nothing, history included, so a refresh that was not published leaves no
    line behind.  A recorder that is not there leaves the lines in the record; one that fails
    leaves the manifest as it was."""
    directory = manifest_path.parent
    if not _recheck_candidate(directory, manifest_path, raw, scan, changes):
        return "superseded"
    fd = artifact_admission._acquire_lock(root, REFRESH_ADMISSION_WAIT_SECONDS)
    try:
        record = read_cycle_record(root, cycle_id)
        if (record is None or record.get("state") != "sealed" or record.get("manifest_digest") != digest
                or _record_cycle_manifest_path(root, record) != manifest_path):
            return "superseded"
        if not _recheck_candidate(directory, manifest_path, raw, scan, changes):
            return "superseded"
        # An active -> completed candidate was decided from observations taken before this
        # lock; a lease, route or outcome that moved since keeps the document as it is.
        if ((refreshed.get("cycle") or {}).get("state") == "completed"
                and (json.loads(raw.decode("utf-8")).get("cycle") or {}).get("state") == "active"
                and not _provisional_completion_holds(root, record, refreshed)):
            return "superseded"
        index = artifact_admission.load_index(root)  # the one read of this locked section
        row = index.manifests.get(cycle_id)
        if not isinstance(row, dict) or row.get("manifest_digest") != digest:
            return "index-mismatch"
        identity = artifact_lifecycle.read_root_identity(root)
        index_report = artifact_index.check(
            index, refreshed, idempotency_key=cycle_id, manifest_digest=new_digest,
            repository_id=identity.repository_id if identity else None, replaces_manifest_digest=digest)
        if not index_report.ok:
            return "index-rejected"
        new_raw = artifact_manifest.canonical_bytes(refreshed)
        cycle_path = os.path.relpath(str(directory), str(root))
        pending = _merge_pending(record.get("history_pending") or [], lines)
        artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, raw)
        if _history_module() is not None:
            if not _history_deliver_locked(root, pending):
                return "history-unavailable"  # history first, manifest second: the same change is found again
            pending = []
        _write_journal(root, cycle_id, state="refreshing", manifest_digest=new_digest,
                       previous_manifest_digest=digest, cycle_path=cycle_path,
                       manifest_revision_id=refreshed["manifest_revision_id"], history_pending=pending)
        artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, new_raw)
        # COMMIT POINT: atomic replacement of the current document.
        _write_atomic(manifest_path, new_raw)
        if crash_after_manifest:  # test hook: simulate a crash after the commit point
            raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest refresh")
        try:
            _commit_sealed(root, _with_pending(record, pending), refreshed, new_digest, now=now,
                           index=index, previous_digest=digest)
        except BaseException as exc:
            raise artifact_admission.AdmissionRecoveryRequired(
                f"cycle {cycle_id} manifest refreshed but post-publish update failed; run recover") from exc
    finally:
        artifact_admission._release_lock(root, fd)
    return None


def _cycle_ids_with_records(root: Path) -> List[str]:
    directory = producer_dir(root) / "cycles"
    if not directory.is_dir():
        return []
    return sorted(entry.stem for entry in directory.iterdir()
                  if entry.suffix == ".json" and artifact_identity.is_well_formed(entry.stem, "cycle"))


def refresh_sweep(
    root: Path, *, trigger: str = "turn-end", now: Optional[float] = None,
    budget: Optional[RefreshBudget] = None, first_cycle_id: Optional[str] = None,
    skip_cycle_id: Optional[str] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
) -> Dict[str, Any]:
    """Look at the root's closed cycles in turn, with what is left of one budget (§45 D-124).

    `first_cycle_id` is the cycle a trigger names, looked at before the rest whatever its
    state.  The rest follow a cursor that moves around all closed cycles in ID order: no
    date or count leaves a cycle out, and an old one is reached on a later round.  The cursor
    is bookkeeping under `checkpoints/refresh/`, with no authority; a run that could not finish
    a cycle leaves it before that cycle so the next run resumes there."""
    root = Path(root).resolve()
    clock = time.time() if now is None else float(now)
    budget = budget or (RefreshBudget.unlimited() if trigger == "explicit" else RefreshBudget())
    out: Dict[str, Any] = {"status": "swept", "trigger": trigger, "visited": 0, "refreshed": [], "skipped": [],
                           "cursor": None}
    if closed_cycle_refresh_off():
        out["status"] = "off"
        return out

    def look(cycle_id: str, *, observe: bool) -> Dict[str, Any]:
        try:
            result = refresh_cycle(root, cycle_id, trigger=trigger, now=clock, budget=budget,
                                   allocator=allocator, observe=observe)
        except Exception as exc:  # noqa: BLE001 -- one cycle's trouble never stops the sweep
            return {"status": "skipped", "reason": type(exc).__name__}
        if result["status"] == "emitted":
            out["refreshed"].append(cycle_id)
        elif result["status"] == "skipped" and result.get("reason") != "cycle-not-closed":
            out["skipped"].append({"cycle_id": cycle_id, "reason": result.get("reason")})
        return result

    if first_cycle_id:
        look(first_cycle_id, observe=False)
        out["visited"] += 1
    cursor_path = _refresh_cursor_path(root)
    cursor = _read_json(cursor_path) or {}
    last_sweep = cursor.get("last_sweep_at")
    if (trigger != "explicit" and isinstance(last_sweep, (int, float)) and not isinstance(last_sweep, bool)
            and clock - last_sweep < checkpoint_interval_seconds()):
        out["rotation"] = "min-interval"
        return out
    ids = _cycle_ids_with_records(root)
    if not ids:
        return out
    after = cursor.get("after") if isinstance(cursor.get("after"), str) else None
    start = 0
    if after is not None:
        start = next((i for i, cid in enumerate(ids) if cid > after), 0)
    last_done, looked = after, 0
    for cycle_id in ids[start:] + ids[:start]:
        if looked and budget.exhausted():
            break
        if cycle_id in (first_cycle_id, skip_cycle_id):
            last_done = cycle_id
            continue
        record = read_cycle_record(root, cycle_id)
        if record is None or record.get("state") != "sealed":
            last_done = cycle_id
            continue
        result = look(cycle_id, observe=True)
        looked += 1
        out["visited"] += 1
        if result["status"] != "skipped" and not result.get("complete", True):
            break  # unfinished: the next run resumes inside this cycle
        last_done = cycle_id
    out["cursor"] = last_done
    if looked:
        try:
            _ensure_dir(cursor_path.parent)
            _write_atomic(cursor_path, _json_bytes({"schema_version": 1, "after": last_done,
                                                    "last_sweep_at": clock}), 0o644)
        except (OSError, ProducerError):
            pass
    return out


def finalize_exact_cycle(root: Path, *, cycle_id: str, expected_binding: Mapping[str, Any],
                         state: str = "completed", **kwargs: Any) -> Dict[str, Any]:
    """Finalize one bound cycle under one admission lock, without root recovery."""
    root = Path(root).resolve()
    if expected_binding.get("kind") == "inline_producer_binding_v1":
        # Fast refusal before entering finalize; finalize repeats this *under*
        # its admission lock, so this precheck grants no write authority.
        _inline_producer_binding_check(root, cycle_id, expected_binding)
    _authorize_active_cleanup(root, "finalize-forward-recovery", root, cycle_id)
    # Enter public finalize with no lock held. It owns the one admission
    # boundary encompassing exact recovery, lease check and manifest commit.
    return finalize(root, cycle_id=cycle_id, state=state,
                    expected_binding=expected_binding, _recovery_scope="exact", **kwargs)


def _verify_deleted_cycle(record: Mapping[str, Any], expected_binding: Optional[Mapping[str, Any]],
                          expected_manifest_digest: Optional[str]) -> Dict[str, Any]:
    """What is left of a deleted cycle (§45 D-127): its record.  The finish is read as it was recorded, and
    the cycle's absence is said (`deleted`), not made a failure.  The cycle's identity is still checked."""
    if expected_binding is not None:
        for key in ("cycle_id", "producer_id"):
            expected = expected_binding.get(key)
            if expected is not None and record.get(key) != expected:
                raise ProducerError("already-sealed-mismatch", key)
        if (expected_binding.get("cycle_record_digest")
                and not dispatch_terminal_commit.cycle_identity_matches(
                    record, expected_binding["cycle_record_digest"],
                    campaign_id=expected_binding.get("campaign_id"))):
            raise ProducerError("already-sealed-mismatch", "cycle-identity")
    result: Dict[str, Any] = {"status": "already-sealed", "cycle_id": record["cycle_id"], "deleted": True,
                              "deleted_at": record.get("deleted_at")}
    digest = expected_manifest_digest or record.get("manifest_digest")
    if digest:
        result["manifest_digest"] = digest
    return result


def verify_finalized_cycle(root: Path, *, cycle_id: str, expected_binding: Mapping[str, Any],
                           expected_manifest_digest: Optional[str] = None):
    """Read-only proof under admission lock; never repairs an unsealed cycle.

    A cycle edited or refreshed after it closed is still finalized (§45 D-127):
    the proof is the record, the current manifest and the index agreeing, not the
    files keeping their bytes.  `expected_manifest_digest` names the revision the
    caller recorded when it finished (see `_verify_sealed_cycle_locked`)."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        if expected_binding.get("kind") == "inline_producer_binding_v1":
            _inline_producer_binding_check(root, cycle_id, expected_binding)
        if _live_review_lease(root, cycle_id) is not None:
            raise ProducerError("cycle-finalize-blocked-live-review", cycle_id)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("deleted_at"):
            return _verify_deleted_cycle(record, expected_binding, expected_manifest_digest)
        return _verify_sealed_cycle_locked(root, record, expected_binding,
                                           expected_manifest_digest=expected_manifest_digest)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _commit_sealed(
    root: Path, record: Mapping[str, Any], document: Mapping[str, Any], digest: str, *, now: Optional[float],
    index: Optional[artifact_index.IndexDocument] = None, previous_digest: Optional[str] = None,
    excluded_count: Optional[int] = None,
) -> None:
    """Apply a published manifest to the index and the cycle record.

    The first close admits the cycle to the index.  A later document of an
    already closed cycle (`previous_digest` names the one it replaces) swaps the
    cycle's row instead, on that digest (§45 D-124), keeps `sealed_on`, and does
    not announce a close again.  `index`, when given, is the one read the caller
    already made under the admission lock."""
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    if index is None:
        index = artifact_admission.load_index(root)
    row = index.manifests.get(record["cycle_id"])
    already_closed = record.get("state") == "sealed"
    if row is None:
        index = artifact_index.apply(
            index, document, cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=record["cycle_id"],
        )
        artifact_admission._write_index(root, index)
    elif row.get("manifest_digest") != digest:
        if previous_digest is None or row.get("manifest_digest") != previous_digest:
            raise ProducerError("already-sealed-mismatch", "index")
        index = artifact_index.apply(
            index, document, cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=record["cycle_id"],
        )
        artifact_admission._write_index(root, index)
    sealed = dict(record)
    sealed["state"] = "sealed"
    if not (already_closed and record.get("sealed_on")):
        sealed["sealed_on"] = _rfc3339(now)
    sealed["manifest_digest"] = digest
    sealed["cycle_state"] = document["cycle"]["state"]
    if not already_closed:
        # §45 D-125: a first close leaves one history line.  A recorder that is not there (or
        # fails) leaves the line in the record for the next trigger; it never fails the close.
        line = _close_history_line(
            root, sealed, document, digest, directory, now=now,
            excluded=excluded_count if excluded_count is not None
            else len(record.get("excluded_symlinks") or []))
        pending = _merge_pending(sealed.get("history_pending") or [], [line])
        sealed = _with_pending(sealed, [] if _history_deliver_locked(root, pending) else pending)
    _write_cycle_record(root, sealed, exclusive=False)
    _write_journal(root, record["cycle_id"], state="committed", manifest_digest=digest,
                   cycle_path=os.path.relpath(str(directory), str(root)))
    _remove_journal(root, record["cycle_id"])
    remove_interim(root, record["cycle_id"])
    artifact_locator.update_indexes(root, [record["campaign_id"]])
    if already_closed:
        return
    artifact_cycle_titles.emit_after_seal_locked(root, sealed, document, directory / "manifest.json")
    try:
        import artifact_workflow_group_review  # lazy: it imports this module
        artifact_workflow_group_review.launch_after_seal(root, sealed)  # groups and metadata, one job
    except Exception:  # noqa: BLE001 -- the review trigger never changes a seal
        pass


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


def _is_recoverable_locator_defect(exc: BaseException) -> bool:
    """§3.6a's isolation only ever catches a *locator* defect on the record
    being resolved -- never any other `ProducerError` (a live-review fence, a
    digest mismatch, ...), which must keep propagating unconditionally."""

    if isinstance(exc, artifact_locator.LocatorError):
        return True
    return isinstance(exc, ProducerError) and exc.code == "record-locator-invalid"


def _recover_locked(root: Path, *, now: Optional[float] = None,
                    target_campaign_id: Optional[str] = None) -> Dict[str, Any]:
    """Root-scope crash recovery. Visits every open record and journal entry.

    A record whose campaign is not `target_campaign_id` is isolated (§3.6a):
    a locator defect while resolving *that* record (a hand-copied campaign
    folder, a binding conflict) is reported in ``unresolved`` and the record
    is left exactly as found -- no write, still ``open``, journal untouched --
    so the next sweep retries it unchanged. A record whose campaign *is* the
    target still raises, because that is the campaign this operation touches.
    ``target_campaign_id=None`` (bare ``recover()``) isolates every record.
    """

    result: Dict[str, Any] = {
        "rolled_forward": [], "rolled_back": [], "dropped": [], "open": [], "unresolved": [],
    }
    journal_dir = producer_dir(root) / "journal"
    if journal_dir.is_dir():
        for entry in sorted(journal_dir.glob("*.json")):
            journal = _read_json(entry)
            if journal is None:
                entry.unlink()
                continue
            cycle_id = journal.get("cycle_id", entry.stem)
            record = read_cycle_record(root, cycle_id)
            if record is None:
                entry.unlink()
                result["dropped"].append(cycle_id)
                continue
            manifest_path = root / str(journal.get("cycle_path", "")) / "manifest.json"
            document = _read_json(manifest_path)
            if journal.get("state") == "refreshing" and record.get("state") == "sealed":
                # A refresh of a closed cycle stopped part-way; finish it as it was meant.
                _raise_if_recovery_fenced(root, cycle_id, now=now)
                _refresh_journal_resume(root, record, journal, now=now)
                result["rolled_forward"].append(cycle_id)
                continue
            if document is not None and artifact_manifest.manifest_digest(document) == journal.get("manifest_digest"):
                _raise_if_recovery_fenced(root, cycle_id, now=now)
                try:
                    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                    _commit_sealed(root, record, document, journal["manifest_digest"], now=now)
                except (artifact_locator.LocatorError, ProducerError) as exc:
                    if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                        raise
                    result["unresolved"].append({
                        "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
                        "code": exc.code, "detail": exc.detail, "phase": "journal",
                    })
                    continue
                result["rolled_forward"].append(cycle_id)
            else:
                # Publication was prepared from validated bytes. A later edit
                # or deletion is history, not authority for a different close.
                original = artifact_lifecycle.find_manifest_snapshot(
                    root, cycle_id, manifest_digest=journal.get("manifest_digest"))
                if original is not None:
                    _raise_if_recovery_fenced(root, cycle_id, now=now)
                    try:
                        artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                        _commit_sealed(root, record, original, journal["manifest_digest"], now=now)
                    except (artifact_locator.LocatorError, ProducerError) as exc:
                        if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                            raise
                        result["unresolved"].append({
                            "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
                            "code": exc.code, "detail": exc.detail, "phase": "journal",
                        })
                        continue
                    result["rolled_forward"].append(cycle_id)
                    continue
                # Crash before the commit point: cycle stays open.
                _drop_unpublished_snapshot(root, cycle_id, journal)
                entry.unlink()
                result["rolled_back"].append(cycle_id)
    for record in list_cycle_records(root):
        if (record.get("state") != "open" or record.get("deleted_at")
                or (record.get("relocation") or {}).get("artifact_root")):
            continue
        try:
            directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
            if not directory.is_dir():
                dropped = dict(record)
                dropped["state"] = "dropped"
                dropped["sealed_on"] = _rfc3339(now)
                _write_cycle_record(root, dropped, exclusive=False)
                result["dropped"].append(record["cycle_id"])
                continue
            if (directory / "manifest.json").is_file():
                document = _read_json(directory / "manifest.json")
                if document is not None:
                    _raise_if_recovery_fenced(root, record["cycle_id"], now=now)
                    digest = artifact_manifest.manifest_digest(document)
                    published = artifact_lifecycle.find_manifest_snapshot(root, record["cycle_id"], manifest_digest=digest)
                    if published is None:
                        # Older interrupted publications may have no copy or
                        # journal. Use their actual terminal proof, never a
                        # state field alone, before admitting a new result.
                        identity = artifact_lifecycle.read_root_identity(root)
                        cycle = document.get("cycle") or {}
                        valid_identity = (identity is not None
                                          and document.get("artifact_root_id") == identity.artifact_root_id
                                          and cycle.get("cycle_id") == record["cycle_id"]
                                          and cycle.get("campaign_id") == record["campaign_id"]
                                          and (document.get("producer") or {}).get("producer_id") == record["producer_id"])
                        try:
                            route_file, _ = resolve_cycle_manifest_route(root, record, document)
                            completion = artifact_lifecycle.evaluate_cycle_completion(
                                document, content_root=directory, route_file=route_file,
                                expected_root_id=identity.artifact_root_id if identity else None)
                            valid = valid_identity and completion.ok
                        except (ProducerError, artifact_lifecycle.LifecycleError, OSError, ValueError):
                            valid = False
                        if not valid:
                            result["open"].append(record["cycle_id"])
                            continue
                    artifact_locator.prepare_index_update(root, [record["campaign_id"]])
                    _commit_sealed(root, record, document, artifact_manifest.manifest_digest(document), now=now)
                    result["rolled_forward"].append(record["cycle_id"])
                    continue
        except (artifact_locator.LocatorError, ProducerError) as exc:
            if not _is_recoverable_locator_defect(exc) or record["campaign_id"] == target_campaign_id:
                raise
            result["unresolved"].append({
                "cycle_id": record["cycle_id"], "campaign_id": record["campaign_id"],
                "code": exc.code, "detail": exc.detail, "phase": "open",
            })
            continue
        result["open"].append(record["cycle_id"])
    shared_dir = producer_dir(root) / "shared-journal"
    if shared_dir.is_dir():
        for entry in sorted(shared_dir.glob("*.json")):
            journal = _read_json(entry)
            try:
                _validate_shared_journal(root, entry, journal)
            except ProducerError as exc:
                result["unresolved"].append({"revision_id": entry.stem, "code": exc.code,
                                             "detail": exc.detail, "phase": "shared"})
                continue
            staging = root / journal["staging"]
            target = root / journal["target"]
            if journal.get("state") == "staging" and staging.is_dir():
                reference = _read_json(_reference_path(root, journal["kind"], journal["reference_id"])) or {}
                if target.exists() or journal["expected_previous_revision_id"] != reference.get("latest_revision_id"):
                    result["unresolved"].append({"revision_id": entry.stem, "code": "shared-base-mismatch",
                                                 "detail": "staging ownership or base changed", "phase": "shared"})
                    continue
                shutil.rmtree(str(staging))
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
            elif target.is_dir():
                try:
                    _commit_shared(root, journal)
                except ProducerError as exc:
                    result["unresolved"].append({"revision_id": journal.get("revision_id"),
                                                 "code": exc.code, "detail": exc.detail, "phase": "shared"})
                    continue
                result["rolled_forward"].append(journal.get("revision_id", entry.stem))
            elif journal.get("state") == "published":
                result["unresolved"].append({"revision_id": entry.stem, "code": "shared-journal-mismatch",
                                             "detail": "published target missing", "phase": "shared"})
            else:
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
    removed_interims = _sweep_orphan_interims(root)
    if removed_interims:
        result["interims_removed"] = removed_interims
    return result


def recover(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    dispatch_terminal_commit.require_current_cleanup("root-recover")
    root = Path(root).resolve()
    # §45 D-126: what was moved or removed by hand is found first, and the folders are read before
    # the lock is taken (D-124).
    reconcile_root(root, now=now)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        step1 = artifact_admission._recover_locked(root, now=now)
        producer = _recover_locked(root, now=now)
        locator_index = artifact_locator.verify_indexes(root, repair=True)
        # duplicate-copy: an unrelated campaign's defect stops only itself
        # (locator_index stays "problems", no repair write for it) and the
        # producer sweep's own isolated records (Step 3.9), never this call's
        # exit status -- `recover` keeps reporting success, typed, so a caller
        # retrying its own unrelated seal is not blocked by someone else's
        # hand-copied folder (Step 3.9b).
        status = ("recovered"
                  if not producer.get("unresolved") and locator_index.get("status") in {"current", "rebuilt"}
                  else "recovered-with-problems")
        return {"status": status, "admission": step1, "producer": producer, "locator_index": locator_index}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# shared admission
# ---------------------------------------------------------------------------


def _reference_path(root: Path, kind: str, ref_id: str) -> Path:
    return Path(root) / "shared" / kind / ref_id / "reference.json"


# Shared kinds a root holds exactly one canonical reference of unless told
# otherwise. `analysis` lineages are per subject (a root legitimately carries
# several); `research` promotions are per promotion.
CANONICAL_SINGLE_REFERENCE_KINDS = ("spec",)


def list_references(root: Path, kind: str) -> List[Dict[str, Any]]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "reference.json")
        if record and record.get("shared_reference_id"):
            out.append(record)
    return out


def find_reference_by_key(root: Path, kind: str, key: str) -> Optional[Dict[str, Any]]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "reference.json")
        if record and record.get("key") == key:
            return record
    return None


# --- D-87: component-set preservation -------------------------------------
#
# A shared reference may carry several top-level components (`stage-dispatch/`,
# `agent-fleet-dashboard/`, ...).  `admit_shared` copies only the source tree,
# so a partial admit silently drops every component the source omits and
# `latest_revision_id` then points at the reduced set.  On 2026-09-03 two
# admits one minute apart took `ref_4d540b57...` from 246 files / 3 components
# to 41 files / 1 component.  The contract (a) is a typed refusal, (b) an
# explicit `--drop-component`, and (d) a read-only adjacent-pair check.
# Carry-forward is deliberately NOT implemented: (c) rejects it, because a
# revision must only contain what its admit actually carried.


def component_set(paths: Iterable[str]) -> Set[str]:
    """The component set of a revision or tree: the first path segment of every
    relative path.  A top-level file is its own component -- `rrev_15cf1d9f`
    admitted `prd.md` and friends flat, so defining components as "top-level
    directories" would read that revision as empty.  `revision.json` is
    revision metadata, not content."""
    components: Set[str] = set()
    for path in paths:
        rel = str(path).strip().lstrip("./")
        if not rel or rel == REVISION_RECORD_NAME:
            continue
        components.add(rel.split("/", 1)[0])
    return components


def _scan_component_set(source_path: Path) -> Set[str]:
    """Component set of the incoming tree, read-only.

    Deliberately does not reuse `_copy_tree_files`: contract (a) requires that a
    refusal create no staging directory at all, and that helper writes as it
    walks."""
    if source_path.is_file():
        return component_set([source_path.name])
    return component_set(
        entry.relative_to(source_path).as_posix() for entry in _walk_files(source_path)
    )


def _revision_component_set(
    root: Path, kind: str, ref_id: str, revision_id: str, *, fallback_to_disk: bool = False
) -> Set[str]:
    revision_dir = Path(root) / "shared" / kind / ref_id / "revisions" / revision_id
    record = _read_json(revision_dir / REVISION_RECORD_NAME)
    if record is None:
        # W7-relocated / adopted revisions (`artifact_cutover._adopt_reference`)
        # carry no revision record; the bytes on disk are their only truth.  Use
        # the directory's top-level entries so D-87 still guards them instead of
        # hard-blocking every later admit on the reference.  Only a revision
        # that is absent on disk as well is an error.
        # The read-only `check-components` surface keeps reporting such a
        # revision as unreadable; only the admit guard opts into the scan.
        if fallback_to_disk and revision_dir.is_dir():
            return _scan_component_set(revision_dir)
        raise ProducerError("revision-record-missing", f"{ref_id}/{revision_id}")
    return component_set(
        str(row.get("path", "")) for row in (record.get("files") or []) if isinstance(row, Mapping)
    )


def _latest_component_set(
    root: Path, kind: str, reference: Optional[Mapping[str, Any]]
) -> Optional[Set[str]]:
    """The previous latest revision's component set, or `None` when there is no
    predecessor to regress against (A17-6: the first revision is exempt)."""
    if not reference:
        return None
    latest = reference.get("latest_revision_id")
    if not latest:
        return None
    return _revision_component_set(
        root, kind, reference["shared_reference_id"], str(latest), fallback_to_disk=True
    )


def check_component_sets(
    root: Path,
    kind: str,
    reference_id: str,
    *,
    from_revision: Optional[str] = None,
    to_revision: Optional[str] = None,
) -> Dict[str, Any]:
    """D-87 (d): read-only check of `components(new) >= components(old) - dropped(new)`
    over adjacent revision pairs.  Writes nothing.

    `from_revision`/`to_revision` bound the inspected window.  The bound is not
    cosmetic: run unbounded over `ref_4d540b57...` this reports nine violations
    reaching back to seq 8, because a partial admit was a chronic pattern long
    before the 2026-09-03 incident.  A caller asking about one incident needs to
    ask about its window.
    """
    root = Path(root).resolve()
    reference = _read_json(_reference_path(root, kind, reference_id))
    if reference is None:
        raise ProducerError("reference-unknown", reference_id)
    revisions = [str(value) for value in (reference.get("revisions") or [])]
    if from_revision is not None:
        if from_revision not in revisions:
            raise ProducerError("revision-unknown", from_revision)
        revisions = revisions[revisions.index(from_revision):]
    if to_revision is not None:
        if to_revision not in revisions:
            raise ProducerError("revision-unknown", to_revision)
        revisions = revisions[: revisions.index(to_revision) + 1]
    pairs: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    sets: Dict[str, Optional[Set[str]]] = {}
    for revision_id in revisions:
        try:
            sets[revision_id] = _revision_component_set(root, kind, reference_id, revision_id)
        except ProducerError:
            sets[revision_id] = None
            unreadable.append(revision_id)
    violations = 0
    for older, newer in zip(revisions, revisions[1:]):
        old_set, new_set = sets[older], sets[newer]
        if old_set is None or new_set is None:
            pairs.append({"old": older, "new": newer, "dropped": [], "missing": [],
                          "verdict": "unknown"})
            continue
        record = _read_json(
            Path(root) / "shared" / kind / reference_id / "revisions" / newer / REVISION_RECORD_NAME
        ) or {}
        dropped = sorted(
            str(row.get("name", ""))
            for row in (record.get("dropped_components") or [])
            if isinstance(row, Mapping)
        )
        missing = sorted(old_set - new_set - set(dropped))
        if missing:
            violations += 1
        pairs.append({"old": older, "new": newer, "dropped": dropped, "missing": missing,
                      "verdict": "regressed" if missing else "ok"})
    return {"status": "checked", "kind": kind, "reference_id": reference_id,
            "pairs": pairs, "violations": violations, "unreadable": sorted(unreadable)}


SPEC_BASE_RECEIPT = "_internal/shared-base.json"


def _check_shared_base(expected: Optional[str], actual: Optional[str]) -> None:
    if expected != actual:
        raise ProducerError("shared-base-mismatch",
                            f"base={expected or 'none'} latest={actual or 'none'}; "
                            "merge against latest in a new cycle before admission")


def _sealed_source_files(directory: Path, record: Mapping[str, Any], source_rel: str,
                         source_path: Path) -> List[Tuple[str, str, int]]:
    """Bind both the receipt and payload to the sealed manifest, not mutable disk."""
    document = _read_json(directory / "manifest.json")
    if document is None or artifact_manifest.manifest_digest(document) != record.get("manifest_digest"):
        raise ProducerError("source-manifest-mismatch", source_rel)
    expected = {}
    for row in document.get("artifact_revisions", []):
        path = row.get("locator", {}).get("path", "")
        if path == source_rel or path.startswith(source_rel + "/"):
            rel = path[len(source_rel) + 1:] if path != source_rel else source_path.name
            expected[rel] = (row["content_digest"], row["byte_size"])
    paths = sorted(source_path.rglob("*")) if source_path.is_dir() else [source_path]
    actual = {}
    for path in paths:
        if path.is_symlink():
            raise ProducerError("source-invalid", str(path))
        if path.is_file():
            rel = path.relative_to(source_path).as_posix() if source_path.is_dir() else path.name
            data = path.read_bytes()
            actual[rel] = (_digest(data), len(data))
    if actual != expected:
        raise ProducerError("source-manifest-mismatch", source_rel)
    return [(rel, digest, size) for rel, (digest, size) in sorted(actual.items())]


def _cycle_document_for_digest(root: Path, cycle_id: str, record: Optional[Mapping[str, Any]],
                               digest: Optional[str]) -> Optional[Mapping[str, Any]]:
    """The cycle's manifest document carrying `digest`: the current one, else a preserved copy."""
    if not digest:
        return None
    if record is not None:
        try:
            current = _read_json(_record_cycle_manifest_path(root, record))
        except ProducerError:
            current = None
        if current is not None and artifact_manifest.manifest_digest(current) == digest:
            return current
    try:
        return artifact_lifecycle.find_manifest_snapshot(root, cycle_id, manifest_digest=digest)
    except artifact_lifecycle.LifecycleError:
        return None


def _published_source_rows(document: Mapping[str, Any], source_rel: str) -> List[Tuple[str, str, int]]:
    """The files a manifest document declares under `source_rel`, as `_sealed_source_files` lists them."""
    rows = []
    for row in document.get("artifact_revisions", []) or []:
        path = row.get("locator", {}).get("path", "")
        if path == source_rel or path.startswith(source_rel + "/"):
            rel = path[len(source_rel) + 1:] if path != source_rel else Path(source_rel).name
            rows.append((rel, row["content_digest"], row["byte_size"]))
    return sorted(rows)


def _check_published_provenance(root: Path, revision: Mapping[str, Any], expected_rows: Sequence[Mapping[str, Any]],
                                *, code: str) -> None:
    """A finished publication is judged by what it recorded (§45 D-127): the source
    manifest document it was published from, found as the current one or a
    preserved copy, must still declare the files the publication took.  A source
    that was edited, moved or deleted since changes nothing; a cycle that left no
    copy and is gone is taken on the publication's own record."""
    source = revision.get("source", {})
    cycle_id = source.get("cycle_id", "")
    record = read_cycle_record(root, cycle_id) if artifact_identity.is_well_formed(cycle_id, "cycle") else None
    document = _cycle_document_for_digest(root, cycle_id, record, source.get("manifest_digest"))
    if document is None:
        return
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError(code, "source path")
    if _published_source_rows(document, rel) != sorted((r["path"], r["sha256"], r["byte_size"]) for r in expected_rows):
        raise ProducerError(code, "source identity")


def _verify_completed_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any]) -> None:
    """A finished spec publication: its immutable shared bytes and its own recorded proof.

    The merge is not recomputed (the source it read may have changed since).
    What was recorded must agree with itself and with the immutable revisions it
    names: the output holds the digest the proof claims, the proof's source
    inventory hashes to its own digest, and the base and latest revisions the
    merge read still carry the digests the proof recorded."""
    proof = revision.get("spec_merge")
    if not isinstance(proof, dict):
        raise ProducerError("shared-merge-proof-invalid", "missing merge proof")
    ref_id = reference["shared_reference_id"]
    output, _record = _verified_shared_spec(root, ref_id, revision["shared_reference_revision_id"])
    try:
        files = proof["source_files"]
        valid = (proof["source_content_digest"] == _spec_inventory_digest(files)
                 and proof["output_content_digest"] == _spec_inventory_digest(_spec_inventory(output)))
        for revision_key, digest_key in (("base_revision_id", "base_content_digest"),
                                         ("latest_revision_id", "latest_content_digest")):
            _tree, shared = _verified_shared_spec(root, ref_id, proof[revision_key])
            valid = valid and shared["content_digest"] == proof[digest_key]
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ProducerError("shared-merge-proof-invalid", revision["shared_reference_revision_id"])
    _check_published_provenance(root, revision, files, code="shared-merge-proof-invalid")


def _spec_admission_base(root: Path, source_path: Path, reference_id: str,
                         base_revision: Optional[str]) -> Optional[str]:
    receipt_path = source_path / SPEC_BASE_RECEIPT
    receipt = _read_json(receipt_path) if source_path.is_dir() else None
    if receipt is None and os.path.lexists(receipt_path):
        raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
    if receipt is None:
        if base_revision is None:
            raise ProducerError("shared-base-required", "spec admission needs a seeded receipt or --base-revision")
        base = None if base_revision == "none" else base_revision
    else:
        if receipt.get("schema_version") != 1 or not all(k in receipt for k in ("reference_id", "revision_id", "content_digest")):
            raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
        base = receipt["revision_id"]
        if base is not None and not artifact_identity.is_well_formed(base, "shared_reference_revision"):
            raise ProducerError("shared-base-invalid", str(base))
        if base is not None:
            if receipt["reference_id"] != reference_id:
                raise ProducerError("shared-base-reference-mismatch", reference_id)
            revision = _read_json(root / "shared/spec" / reference_id / "revisions" / str(base) / REVISION_RECORD_NAME)
            if revision is None or revision.get("content_digest") != receipt["content_digest"]:
                raise ProducerError("shared-base-invalid", str(base))
        elif receipt["reference_id"] is not None or receipt["content_digest"] is not None:
            raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
        if base_revision is not None:
            _check_shared_base(None if base_revision == "none" else base_revision, base)
    if base is not None and not artifact_identity.is_well_formed(base, "shared_reference_revision"):
        raise ProducerError("shared-base-invalid", str(base))
    return base


def _spec_bytes(directory: Path, *, revision: bool = False) -> Dict[str, bytes]:
    """Read a complete regular-file tree, rejecting links and special files."""
    if directory.is_symlink() or not directory.is_dir():
        raise ProducerError("shared-base-invalid", str(directory))
    result = {}
    for path in _walk_files(directory):
        rel = path.relative_to(directory).as_posix()
        if path.is_symlink() or not path.is_file():
            raise ProducerError("shared-base-invalid", rel)
        if revision and rel == REVISION_RECORD_NAME:
            continue
        result[rel] = path.read_bytes()
    return result


def _spec_inventory(tree: Mapping[str, bytes]) -> List[Dict[str, Any]]:
    return [{"path": p, "sha256": _digest(data), "byte_size": len(data)}
            for p, data in sorted(tree.items())]


def _spec_inventory_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    return _digest(_canonical([[r["path"], r["sha256"], r["byte_size"]] for r in rows]))


def _verified_shared_spec(root: Path, ref_id: str, revision_id: str):
    """Validate each immutable input against its own inventory, including bytes."""
    if not artifact_identity.is_well_formed(revision_id, "shared_reference_revision"):
        raise ProducerError("shared-base-invalid", str(revision_id))
    directory = root / "shared/spec" / ref_id / "revisions" / revision_id
    for parent in (directory, directory.parent, directory.parent.parent):
        if parent.is_symlink():
            raise ProducerError("shared-base-invalid", str(parent))
    record = _read_json(directory / REVISION_RECORD_NAME)
    if (not isinstance(record, dict) or record.get("shared_reference_id") != ref_id
            or record.get("shared_reference_revision_id") != revision_id):
        raise ProducerError("shared-base-invalid", revision_id)
    tree = _spec_bytes(directory, revision=True)
    rows = record.get("files")
    try:
        valid = (isinstance(rows, list)
                 and sorted(rows, key=lambda r: r["path"]) == _spec_inventory(tree)
                 and record.get("content_digest") == _spec_inventory_digest(rows))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ProducerError("shared-revision-integrity", revision_id)
    return tree, record


def spec_scope_components(scopes: Iterable[str]) -> Optional[Tuple[str, ...]]:
    """The components already named by a spec write scope; None means the whole tree."""
    selected = set()
    for scope in scopes:
        parts = str(scope).split("/")
        if parts[0] != "spec":
            continue
        if (len(parts) < 3 or any(c in parts[1] for c in "*?[")
                or parts[1] in {"_internal", "<component>"}):
            return None
        selected.add(parts[1])
    return tuple(sorted(selected))


def _spec_seed_components(source_tree: Mapping[str, bytes]) -> Optional[Tuple[str, ...]]:
    raw = source_tree.get(SPEC_BASE_RECEIPT)
    if raw is None:
        return None
    try:
        receipt = json.loads(raw)
        components = receipt.get("components")
        if components is None:
            return None  # existing whole-tree receipts retain their meaning
        valid = (isinstance(components, list) and bool(components)
                 and components == sorted(set(components))
                 and all(isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", c) for c in components)
                 and receipt.get("seed_complete") is True
                 and receipt.get("component_seeds") == {c: True for c in components})
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ProducerError("shared-base-invalid", "component seed")
    outside = [p for p in source_tree if p != SPEC_BASE_RECEIPT and p.split("/", 1)[0] not in components]
    if outside:
        raise ProducerError("source-manifest-mismatch", "outside seeded components: " + ",".join(sorted(outside)))
    return tuple(components)



def _merge_spec_publication(root: Path, reference: Mapping[str, Any], base_id: str,
                            latest_id: str, source_tree: Mapping[str, bytes], drop_decisions: Sequence[Mapping[str, str]] = ()):
    import spec_merge
    ids = reference.get("revisions", [])
    components = _spec_seed_components(source_tree)
    if (not base_id or not latest_id or base_id not in ids or latest_id not in ids
            or len(ids) != len(set(ids)) or ids.index(base_id) > ids.index(latest_id)
            or (base_id == latest_id and components is None)):
        raise ProducerError("shared-base-mismatch", f"unproven ancestry: {base_id} -> {latest_id}")
    ref_id = reference["shared_reference_id"]
    base, base_record = _verified_shared_spec(root, ref_id, base_id)
    latest, latest_record = _verified_shared_spec(root, ref_id, latest_id)
    candidate = dict(source_tree)
    if components is not None:
        # O is a scoped delta: absence outside its scope is no decision to delete.
        candidate = {**{p: b for p, b in base.items()
                        if p.split("/", 1)[0] not in components}, **candidate}
    dropped = {row["name"] for row in drop_decisions}
    missing = component_set(base) - component_set(candidate) - dropped
    if missing:
        raise ProducerError("component-set-regressed", ",".join(sorted(missing)))
    if dropped - component_set(latest):
        raise ProducerError("drop-component-unknown", ",".join(sorted(dropped - component_set(latest))))
    # A component deletion also conflicts with additions to that component.
    # File-wise merge alone could leave only the newly added files alive.
    for component in component_set(base):
        def subtree(tree):
            return {p: b for p, b in tree.items() if p.split("/", 1)[0] == component}
        b, o, l = subtree(base), subtree(candidate), subtree(latest)
        if (not o and l and l != b) or (not l and o and o != b):
            raise ProducerError("shared-spec-conflict", f"{component}: component-delete-modify")
    try:
        merged, evidence = spec_merge.merge_trees(base, candidate, latest)
    except spec_merge.MergeConflict as exc:
        raise ProducerError("shared-spec-conflict", str(exc)) from exc
    source_files = _spec_inventory(source_tree)
    proof = {"schema_version": 1, "base_revision_id": base_id,
             "base_content_digest": base_record["content_digest"],
             "latest_revision_id": latest_id,
             "latest_content_digest": latest_record["content_digest"],
             "source_files": source_files, "source_content_digest": _spec_inventory_digest(source_files),
             "dropped_components": list(drop_decisions),
             "evidence": evidence, "output_content_digest": _spec_inventory_digest(_spec_inventory(merged))}
    return merged, proof


def _verify_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any],
                             *, completed: bool = False):
    """Recompute a derived publication for recovery and exact retry; never rebase it.

    `completed` is a publication already in the reference's revision list: it is
    judged by its record and its immutable bytes (§45 D-127), not by the source
    as it is today.  A publication still waiting to be committed keeps the strict
    check that its source is the input it took."""
    if completed:
        return _verify_completed_spec_publication(root, reference, revision)
    proof = revision.get("spec_merge")
    if not isinstance(proof, dict):
        raise ProducerError("shared-merge-proof-invalid", "missing merge proof")
    source = revision.get("source", {})
    record = read_cycle_record(root, source.get("cycle_id", ""))
    if record is None or record.get("state") != "sealed" or record.get("manifest_digest") != source.get("manifest_digest"):
        raise ProducerError("shared-merge-proof-invalid", "source identity")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError("shared-merge-proof-invalid", "source path")
    source_rows = _sealed_source_files(directory, record, rel, directory / rel)
    source_tree = _spec_bytes(directory / rel)
    if source_rows != [(r["path"], r["sha256"], r["byte_size"]) for r in _spec_inventory(source_tree)]:
        raise ProducerError("source-manifest-mismatch", rel)
    recorded_base = _spec_admission_base(root, directory / rel, reference["shared_reference_id"],
                                         proof.get("base_revision_id"))
    merged, expected = _merge_spec_publication(root, reference, recorded_base,
                                              proof.get("latest_revision_id"), source_tree, revision.get("dropped_components", []))
    output, _ = _verified_shared_spec(root, reference["shared_reference_id"],
                                      revision["shared_reference_revision_id"])
    if proof != expected or output != merged:
        raise ProducerError("shared-merge-proof-invalid", revision["shared_reference_revision_id"])


def _verify_exact_spec_publication(root: Path, reference: Mapping[str, Any], revision: Mapping[str, Any],
                                   *, completed: bool = False):
    """Absence of a merge proof must mean an exact source copy, not lost proof.

    `completed` judges a finished publication by its own record: the shared bytes
    are the files the source manifest it names declared (§45 D-127)."""
    ids = reference.get("revisions", [])
    revision_id = revision["shared_reference_revision_id"]
    if revision_id in ids:
        index = ids.index(revision_id)
        base_id = ids[index - 1] if index else None
    else:
        base_id = reference.get("latest_revision_id")
    if "spec_base_revision_id" in revision and revision["spec_base_revision_id"] != base_id:
        raise ProducerError("shared-journal-mismatch", "publication ancestry")
    if base_id and not _legacy_adopted_spec_base(root, reference, base_id):
        _verified_shared_spec(root, reference["shared_reference_id"], base_id)
    if completed:
        output, _record = _verified_shared_spec(root, reference["shared_reference_id"], revision_id)
        _check_published_provenance(root, revision, _spec_inventory(output), code="shared-merge-proof-invalid")
        return
    source = revision.get("source", {})
    record = read_cycle_record(root, source.get("cycle_id", ""))
    if record is None or record.get("manifest_digest") != source.get("manifest_digest"):
        raise ProducerError("shared-journal-mismatch", "source identity")
    rel = source.get("path", "")
    if not rel.startswith("artifacts/") or ".." in rel.split("/"):
        raise ProducerError("shared-journal-mismatch", "source path")
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    if os.path.lexists(directory / rel / SPEC_BASE_RECEIPT):
        source_base = _spec_admission_base(root, directory / rel, reference["shared_reference_id"], None)
        _check_shared_base(source_base, base_id)
    rows = _sealed_source_files(directory, record, rel, directory / rel)
    output, _ = _verified_shared_spec(root, reference["shared_reference_id"],
                                     revision["shared_reference_revision_id"])
    if rows != [(r["path"], r["sha256"], r["byte_size"]) for r in _spec_inventory(output)]:
        raise ProducerError("shared-merge-proof-invalid", "nonexact publication lacks merge proof")


def _legacy_adopted_spec_base(root: Path, reference: Mapping[str, Any], base_id: str) -> bool:
    # Missing/malformed canonical metadata is corruption, not legacy evidence.
    # Old adoptions without an exact captured roster remain unproven.
    return (reference.get("adopted_from") == "w7-e2-e3-relocation"
            and base_id in reference.get("adopted_revision_ids", [])
            and base_id in reference.get("revisions", [])
            and not os.path.lexists(root / "shared/spec" / reference["shared_reference_id"]
                                   / "revisions" / base_id / REVISION_RECORD_NAME))


def _validate_shared_journal(root: Path, entry: Path, journal) -> None:
    """A malformed recovery record grants no deletion or publication authority."""
    if not isinstance(journal, dict):
        raise ProducerError("shared-journal-mismatch", "unreadable journal")
    kind, ref, rev = (journal.get(k) for k in ("kind", "reference_id", "revision_id"))
    if (not isinstance(kind, str) or kind not in SHARED_KINDS or not artifact_identity.is_well_formed(ref, "shared_reference")
            or not artifact_identity.is_well_formed(rev, "shared_reference_revision") or entry.stem != rev
            or not isinstance(journal.get("state"), str) or journal.get("state") not in {"staging", "published"}
            or "expected_previous_revision_id" not in journal):
        raise ProducerError("shared-journal-mismatch", entry.name)
    prefix = Path("shared") / kind / ref / "revisions"
    staging, target = journal.get("staging"), journal.get("target")
    if (not isinstance(staging, str) or Path(staging).parent != prefix
            or not re.fullmatch(r"\.admitting-[0-9a-f]{16}", Path(staging).name)
            or target != (prefix / rev).as_posix()):
        raise ProducerError("shared-journal-mismatch", "recovery paths")
    for rel in (Path(staging), Path(target)):
        for part in (rel, *rel.parents):
            if (root / part).is_symlink():
                raise ProducerError("shared-journal-mismatch", "symlink recovery path")
    source = journal.get("source_path", "")
    record = read_cycle_record(root, journal.get("cycle_id", ""))
    if not isinstance(source, str) or not source.startswith("artifacts/") or ".." in source.split("/"):
        raise ProducerError("shared-journal-mismatch", "source identity")
    if journal.get("state") == "published" or (root / target).is_dir():
        # The immutable revision is in place: the journal only waits to be
        # committed.  It names the source manifest document it was published
        # from, found as the current one or a preserved copy (§45 D-127); the
        # cycle may have been refreshed, edited or deleted since.
        digest = journal.get("source_manifest_digest")
        if record is not None and record.get("manifest_digest") != digest:
            copies = artifact_lifecycle.read_manifest_snapshots(root, journal.get("cycle_id", ""))
            if copies and not any(artifact_manifest.manifest_digest(doc) == digest for _raw, doc in copies):
                raise ProducerError("shared-journal-mismatch", "source identity")
        return
    if record is None or record.get("manifest_digest") != journal.get("source_manifest_digest"):
        raise ProducerError("shared-journal-mismatch", "source identity")
    if kind == "spec":
        directory = cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
        _sealed_source_files(directory, record, source, directory / source)


def _commit_shared(root: Path, journal: Mapping[str, Any]) -> None:
    kind = journal["kind"]
    ref_id = journal["reference_id"]
    reference = _read_json(_reference_path(root, kind, ref_id))
    if reference is None:
        reference = {
            "schema_version": 1, "contract": CONTRACT, "shared_reference_id": ref_id,
            "kind": SHARED_KINDS[kind], "key": journal.get("key"), "title": journal.get("title"),
            "created_on": journal.get("created_on"), "latest_revision_id": None, "revisions": [],
        }
    revision = _read_json(root / "shared" / kind / ref_id / "revisions" / journal["revision_id"] / REVISION_RECORD_NAME)
    if (revision is None or revision.get("shared_reference_id") != ref_id
            or revision.get("shared_reference_revision_id") != journal["revision_id"]
            or revision.get("source", {}).get("cycle_id") != journal.get("cycle_id")
            or revision.get("source", {}).get("path") != journal.get("source_path")
            or revision.get("source", {}).get("manifest_digest") != journal.get("source_manifest_digest")):
        raise ProducerError("shared-journal-mismatch", journal["revision_id"])
    if journal["revision_id"] not in reference["revisions"]:
        if "expected_previous_revision_id" not in journal:
            raise ProducerError("shared-base-required", "recovery journal lacks base revision")
        _check_shared_base(journal["expected_previous_revision_id"], reference.get("latest_revision_id"))
    completed = journal["revision_id"] in reference["revisions"]
    if "spec_merge" in journal or "spec_merge" in revision:
        if journal.get("spec_merge") != revision.get("spec_merge"):
            raise ProducerError("shared-journal-mismatch", "merge proof")
        _verify_spec_publication(root, reference, revision, completed=completed)
        if journal.get("expected_previous_revision_id") != revision["spec_merge"]["latest_revision_id"]:
            raise ProducerError("shared-journal-mismatch", "merge parent")
    elif kind == "spec":
        _verify_exact_spec_publication(root, reference, revision, completed=completed)
        if "spec_base_revision_id" in revision and revision["spec_base_revision_id"] != journal.get("expected_previous_revision_id"):
            raise ProducerError("shared-journal-mismatch", "source base")
    if journal["revision_id"] not in reference["revisions"]:
        if "expected_previous_revision_id" not in journal:
            raise ProducerError("shared-base-required", "recovery journal lacks base revision")
        _check_shared_base(journal["expected_previous_revision_id"], reference.get("latest_revision_id"))
        reference["revisions"] = list(reference["revisions"]) + [journal["revision_id"]]
        reference["latest_revision_id"] = journal["revision_id"]
        reference["updated_on"] = journal.get("created_on")
        path = _reference_path(root, kind, ref_id)
        _ensure_dir(path.parent)
        _write_atomic(path, _json_bytes(reference))
    # An already committed journal is cleanup only: never rewind a newer latest.
    try:
        shared_journal_path(root, journal["revision_id"]).unlink()
    except FileNotFoundError:
        pass


_UNKNOWN_BASE = object()


def _finished_spec_publication(root: Path, reference: Mapping[str, Any], cycle_id: str, source_rel: str,
                               record: Mapping[str, Any], source_path: Path,
                               base_revision: Optional[str], known_publications=None):
    """The earlier publication this `admit-shared` call repeats, if any.

    It is a retry when a revision of this reference was published from this
    cycle's `source_rel` out of one of the cycle's manifest documents (the
    current one or a preserved earlier one) and the call names the same base
    the publication was made from.  A call that names a newer base is a new
    publication of whatever the source holds now, and is not a retry.  When the
    call names no base and the source is gone or unseeded, it can only be the
    retry."""
    digests = {record.get("manifest_digest")}
    digests.update(artifact_manifest.manifest_digest(doc)
                   for _raw, doc in artifact_lifecycle.read_manifest_snapshots(root, cycle_id))
    if base_revision is not None:
        caller_base: Any = None if base_revision == "none" else base_revision
    else:
        receipt = _read_json(source_path / SPEC_BASE_RECEIPT) if source_path.is_dir() else None
        caller_base = receipt.get("revision_id") if isinstance(receipt, dict) and "revision_id" in receipt \
            else _UNKNOWN_BASE
    publications = (known_publications if known_publications is not None else (
        (prior_id, _read_json(root / "shared/spec" / reference["shared_reference_id"] / "revisions" / prior_id
                             / REVISION_RECORD_NAME) or {})
        for prior_id in reversed(list(reference.get("revisions", [])))))
    for prior_id, prior in publications:
        provenance = prior.get("source", {})
        if (provenance.get("cycle_id") != cycle_id or provenance.get("path") != source_rel
                or provenance.get("manifest_digest") not in digests):
            continue
        prior_base = (prior["spec_merge"].get("base_revision_id") if isinstance(prior.get("spec_merge"), dict)
                      else prior.get("spec_base_revision_id"))
        if caller_base is not _UNKNOWN_BASE and caller_base != prior_base:
            continue
        return prior_id, prior
    return None, None


def completed_spec_publication(root: Path, *, cycle_id: str, settle: bool = False,
                               references: Optional[Sequence[Mapping[str, Any]]] = None,
                               known_publications: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Finish publication after the work is complete, without changing its outcome.

    Status reads actual source lineage. Normal completion retries the existing
    admission transaction, with its seed/base/CAS and immutable-revision recovery.
    No extra completion gate, mutable completion flag, or model rerun is involved.
    """
    root = Path(root).resolve()
    result = {"cycle_id": cycle_id, "status": "not-applicable"}
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            return {**result, "status": "pending", "reason": "cycle-unknown"}
        if record.get("capability") != "autopilot-spec":
            return result
        if record.get("state") != "sealed" or _published_cycle_state(root, record) != "completed":
            return {**result, "reason": "spec-work-not-completed"}
        result.update(status="pending", reason="shared-publication-required")
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        source_rel = _placed_locator("artifacts/spec", _output_placements(root, record))
        source_path = directory / source_rel
        seed_path = source_path / SPEC_BASE_RECEIPT
        seed = _read_json(seed_path)
        if _path_entry_present(seed_path) and seed is None:
            raise ProducerError("shared-base-invalid", SPEC_BASE_RECEIPT)
        reference_id = seed.get("reference_id") if seed is not None else None
        if reference_id is not None and not artifact_identity.is_well_formed(reference_id, "shared_reference"):
            raise ProducerError("shared-base-invalid", "reference identity")
        refs = list(references) if references is not None else list_references(root, "spec")
        if reference_id is not None:
            refs = [r for r in refs if r.get("shared_reference_id") == reference_id]
            if len(refs) != 1:
                raise ProducerError("reference-unknown", reference_id)
        # An already admitted source remains finished even after edits/deletion
        # or a newer latest. Its original seed base never becomes today's base.
        finished = []
        for reference in refs:
            revision_id, revision = _finished_spec_publication(
                root, reference, cycle_id, source_rel, record, source_path, None,
                known_publications=(known_publications.get(reference["shared_reference_id"], [])
                                    if known_publications is not None and not settle else None))
            if revision is not None:
                finished.append((reference, revision_id, revision))
        if len(finished) > 1:
            raise ProducerError("shared-reference-ambiguous", cycle_id)
        if finished:
            reference, revision_id, revision = finished[0]
            reference_id = reference["shared_reference_id"]
            result.update(status="admitted", shared_reference_id=reference_id,
                          shared_reference_revision_id=revision_id,
                          content_digest=revision.get("content_digest"))
            result.pop("reason", None)
            if not settle:
                return result
        elif len(refs) > 1:
            raise ProducerError("shared-reference-ambiguous", "spec completion lacks a unique seed/reference")
        else:
            # Reuse the sealed manifest's canonical route admission, including
            # verified continuations of the cycle's original begin identity.
            document = _read_json(directory / "manifest.json") or {}
            _route_file, route = resolve_cycle_manifest_route(root, record, document)
            primary = official_spec_primary_path(root, record, route)
            if primary is None:
                return {**result, "reason": "official-spec-prd-missing"}
            result["primary_path"] = str(primary)
            if not settle:
                return result
        admission = admit_shared(root, cycle_id=cycle_id, kind="spec", source="spec",
                                 reference_id=reference_id)
        # The existing API validates and commits, or reuses the exact earlier
        # revision. Only its actual return makes publication successful.
        return {**result, "status": "admitted", "admission": admission,
                "shared_reference_id": admission["shared_reference_id"],
                "shared_reference_revision_id": admission["shared_reference_revision_id"],
                "content_digest": admission.get("content_digest"), "reason": None}
    except (ProducerError, artifact_admission.AdmissionBusy, artifact_admission.AdmissionRecoveryRequired,
            OSError, ValueError, KeyError, TypeError) as exc:
        return {**result, "status": "pending", "reason": getattr(exc, "code", type(exc).__name__),
                "detail": str(exc)}


def admit_shared(
    root: Path,
    *,
    cycle_id: str,
    kind: str,
    source: str,
    reference_id: Optional[str] = None,
    key: Optional[str] = None,
    title: Optional[str] = None,
    promote_research: bool = False,
    promotion_evidence: Optional[str] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    drop_components: Sequence[str] = (),
    drop_reason: Optional[str] = None,
    allow_new_reference: bool = False,
    base_revision: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if kind not in SHARED_KINDS:
        raise ProducerError("shared-kind-not-admissible", kind)
    if kind == "research":
        if not promote_research:
            raise ProducerError("research-promotion-required",
                                "research is admitted to shared/ only with an explicit promotion")
        if not promotion_evidence:
            raise ProducerError("research-promotion-evidence-required")
    if reference_id and not artifact_identity.is_well_formed(reference_id, "shared_reference"):
        raise ProducerError("reference-id-malformed", reference_id)
    if key and not _KEY_RE.match(key):
        raise ProducerError("reference-key-invalid", key)
    alloc = allocator or artifact_identity.IdAllocator()
    prescan = _prescan_cycle(root, cycle_id)  # the cycle's own files, read before the lock (§45 D-124)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        pre = read_cycle_record(root, cycle_id)
        sweep = _recover_locked(root, now=now, target_campaign_id=pre["campaign_id"] if pre else None)
        sweep_unresolved = sweep.get("unresolved", [])
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("state") != "sealed":
            raise ProducerError("cycle-not-sealed", record.get("state", "?"))
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        source_rel = source if source.startswith("artifacts/") else "artifacts/" + source
        if ".." in source_rel.split("/"):
            raise ProducerError("source-unsafe", source)
        source_rel = _placed_locator(source_rel, _output_placements(root, record))
        source_path = directory / source_rel
        reference: Optional[Dict[str, Any]] = None
        if reference_id:
            reference = _read_json(_reference_path(root, kind, reference_id))
            if reference is None:
                raise ProducerError("reference-unknown", reference_id)
        elif key:
            reference = find_reference_by_key(root, kind, key)
        if reference is None and kind in CANONICAL_SINGLE_REFERENCE_KINDS and not allow_new_reference:
            # Defect K (cairn 2026-09-03): `--key cairn-spec` missed the
            # canonical `spec` reference and silently minted a second one, after
            # which "the latest spec" flipped on every admit. A second reference
            # of a canonical-singular kind is an explicit act, never a miss.
            # The documented flow (`admit-shared --kind spec`, no key) keeps
            # working: with exactly one reference it is the canonical one.
            existing = list_references(root, kind)
            listing = ", ".join(f"{r['shared_reference_id']}(key={r.get('key')})" for r in existing)
            if key is None and len(existing) == 1:
                reference = existing[0]
            elif key is None and len(existing) > 1:
                raise ProducerError("shared-reference-ambiguous",
                                    f"{kind}: {listing}; pass --reference <id> or --key <key>")
            elif key is not None and existing:
                raise ProducerError(
                    "shared-reference-exists",
                    f"{kind}: {listing}; the key {key!r} matches none of them -- pass --reference <id> "
                    "(or --key of an existing one), or --new-reference to add another",
                )
        if reference is None:
            reference_id = alloc.allocate("shared_reference")
            created = True
        else:
            reference_id = reference["shared_reference_id"]
            created = False
        if kind == "spec" and reference is not None:
            # A publication that already finished is returned as it is (§45 D-127),
            # before the source is read: the report may have been edited, moved or
            # deleted since, and none of that makes the finished publication unfinished.
            finished_id, finished = _finished_spec_publication(
                root, reference, cycle_id, source_rel, record, source_path, base_revision)
            if finished is not None:
                if "spec_merge" in finished:
                    _verify_spec_publication(root, reference, finished, completed=True)
                else:
                    _verify_exact_spec_publication(root, reference, finished, completed=True)
                prior_dir = root / "shared" / kind / reference_id / "revisions" / finished_id
                return {"status": "reused", "kind": SHARED_KINDS[kind],
                        "shared_reference_id": reference_id, "shared_reference_revision_id": finished_id,
                        "reference_created": False, "revision_dir": str(prior_dir),
                        "content_digest": finished.get("content_digest"), "file_count": finished["file_count"],
                        "promotion": finished["promotion"],
                        **({"spec_merge": finished["spec_merge"]} if "spec_merge" in finished else {})}
        if os.path.islink(str(source_path)) or not source_path.exists():
            raise ProducerError("source-missing", source_rel)
        evidence_rel: Optional[str] = None
        evidence_digest: Optional[str] = None
        if kind == "research":
            assert promotion_evidence is not None
            evidence_rel = promotion_evidence if promotion_evidence.startswith("artifacts/") else "artifacts/" + promotion_evidence
            evidence_rel = _placed_locator(evidence_rel, _output_placements(root, record))
            evidence_path = directory / evidence_rel
            if os.path.islink(str(evidence_path)) or not evidence_path.is_file():
                raise ProducerError("research-promotion-evidence-missing", evidence_rel)
            evidence_digest = _digest(evidence_path.read_bytes())
        # §45 D-123/D-124: a new publication takes the files as they are now, so the
        # cycle's manifest is brought up to them first (it may have changed since the close).
        if _path_entry_present(_record_cycle_manifest_path(root, record)):
            if _refresh_cycle_locked(root, record, now=now, prescan=prescan, trigger="admit-shared")["refreshed"]:
                record = read_cycle_record(root, cycle_id)
        # An unresolved publication intent for this exact source cannot be
        # replaced by a second publication. Preserve it for checked recovery.
        for issue in sweep_unresolved:
            pending_id = issue.get("revision_id")
            if not pending_id:
                continue
            pending = _read_json(shared_journal_path(root, pending_id))
            if (pending is None or not all(pending.get(k) for k in
                    ("reference_id", "cycle_id", "source_path", "source_manifest_digest")) or (pending.get("reference_id") == reference_id
                    and pending.get("cycle_id") == cycle_id and pending.get("source_path") == source_rel
                    and pending.get("source_manifest_digest") == record.get("manifest_digest"))):
                raise ProducerError("shared-publication-unresolved", str(pending_id))
        dropped = sorted({str(name) for name in drop_components if str(name)})
        drop_decisions = [{"name": name, "reason": drop_reason or "unspecified"} for name in dropped]
        source_rows = _sealed_source_files(directory, record, source_rel, source_path) if kind == "spec" else None
        merged_tree = None
        merge_proof = None
        if kind == "spec":
            # Initial unseeded publications have no predecessor to overwrite.
            expected = (_spec_admission_base(root, source_path, reference_id, base_revision)
                        if reference is not None or base_revision is not None or (source_path / SPEC_BASE_RECEIPT).exists()
                        else None)
            latest_id = (reference or {}).get("latest_revision_id")
            # Canonical revisions with an inventory are verified even on the
            # exact-base path; legacy adopted revisions retain their old path.
            if expected:
                if not _legacy_adopted_spec_base(root, reference or {}, expected):
                    _verified_shared_spec(root, reference_id, expected)
            source_tree = _spec_bytes(source_path)
            components = _spec_seed_components(source_tree)
            if expected != latest_id or (components is not None and expected is not None):
                if not expected or not latest_id:
                    _check_shared_base(expected, latest_id)
                if expected not in reference.get("revisions", []) or latest_id not in reference.get("revisions", []):
                    _check_shared_base(expected, latest_id)
                source_tree = _spec_bytes(source_path)
                if source_rows != [(r["path"], r["sha256"], r["byte_size"])
                                   for r in _spec_inventory(source_tree)]:
                    raise ProducerError("source-manifest-mismatch", source_rel)
                base_tree, _ = _verified_shared_spec(root, reference_id, expected)
                # Preserve the original omission guard relative to the actual
                # base. Only additions from latest have carry-forward authority.
                missing_base = (component_set(base_tree) - component_set(source_tree) - set(drop_components)
                                if components is None else set())
                if missing_base:
                    raise ProducerError("component-set-regressed", ",".join(sorted(missing_base)))
                merged_tree, merge_proof = _merge_spec_publication(root, reference, expected, latest_id, source_tree, drop_decisions)
        # D-87 (a): refuse before anything exists.  This sits above the id
        # allocation, the journal write and the staging directory on purpose --
        # the contract requires a refused admit to leave no revision, no journal
        # and no staging behind.
        previous = _latest_component_set(root, kind, reference)
        if previous is not None:
            unknown = sorted(set(dropped) - previous)
            if unknown:
                raise ProducerError("drop-component-unknown", ",".join(unknown))
            incoming_components = (component_set(merged_tree) if merged_tree is not None
                                   else _scan_component_set(source_path))
            missing = sorted(previous - incoming_components - set(dropped))
            if missing:
                raise ProducerError(
                    "component-set-regressed",
                    "source omits components carried by the previous latest revision: "
                    + ",".join(missing)
                    + "; admit the whole tree or drop them explicitly with --drop-component",
                )
        elif dropped:
            raise ProducerError("drop-component-unknown", ",".join(dropped))
        revision_id = alloc.allocate("shared_reference_revision")
        revisions_dir = Path(root) / "shared" / kind / reference_id / "revisions"
        _ensure_dir(revisions_dir)
        target = revisions_dir / revision_id
        if target.exists():
            raise ProducerError("revision-exists", str(target))
        staging = revisions_dir / f".admitting-{os.urandom(8).hex()}"
        journal = {
            "schema_version": 1, "state": "staging", "kind": kind, "reference_id": reference_id,
            "revision_id": revision_id, "key": key or (reference or {}).get("key"),
            "title": title or (reference or {}).get("title") or source_rel,
            "created_on": _rfc3339(now), "staging": os.path.relpath(str(staging), str(root)),
            "target": os.path.relpath(str(target), str(root)), "cycle_id": cycle_id,
            "expected_previous_revision_id": (reference or {}).get("latest_revision_id"),
            "source_path": source_rel, "source_manifest_digest": record.get("manifest_digest"),
        }
        if merge_proof is not None:
            journal["spec_merge"] = merge_proof
        _ensure_dir(shared_journal_path(root, revision_id).parent)
        _write_exclusive(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        os.makedirs(str(staging))
        try:
            if merged_tree is None:
                rows, violations = _copy_tree_files(source_path, staging)
            else:
                rows, violations = [], []
                for rel, data in sorted(merged_tree.items()):
                    dst = staging / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _write_exclusive(dst, data)
                    rows.append((rel, _digest(data), len(data)))
                for current, _, _ in os.walk(staging):
                    _fsync_dir(Path(current))
            if violations:
                raise ProducerError("source-invalid", ";".join(violations))
            if not rows:
                raise ProducerError("source-empty", source_rel)
            if source_rows is not None and merged_tree is None and sorted(rows) != source_rows:
                raise ProducerError("source-manifest-mismatch", source_rel)
            content_digest = _digest(_canonical([[rel, digest, size] for rel, digest, size in rows]))
            sequence = len((reference or {}).get("revisions", [])) + 1
            revision = {
                "schema_version": 1, "contract": CONTRACT,
                "shared_reference_revision_id": revision_id, "shared_reference_id": reference_id,
                "kind": SHARED_KINDS[kind], "sequence": sequence, "content_digest": content_digest,
                "file_count": len(rows), "byte_size": sum(size for _, _, size in rows),
                "created_on": journal["created_on"],
                "source": {
                    "campaign_id": record["campaign_id"], "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest"), "path": source_rel,
                    "capability": record.get("capability"), "route_id": record.get("route_id"),
                },
                "promotion": (
                    {"kind": "explicit", "evidence": evidence_rel, "evidence_digest": evidence_digest}
                    if kind == "research" else {"kind": "canonical-shared-kind"}
                ),
                "files": [{"path": rel, "sha256": digest, "byte_size": size} for rel, digest, size in rows],
            }
            if merge_proof is not None:
                revision["spec_merge"] = merge_proof
            elif kind == "spec":
                revision["spec_base_revision_id"] = expected
            # D-87 (b): an explicit removal is named and reasoned in the record.
            # The key is omitted entirely when nothing was dropped, so an
            # ordinary admit's revision record is byte-identical to before
            # (A17-4). `content_digest` covers `files[]` only, so this key never
            # moves the digest either way.
            if dropped:
                revision["dropped_components"] = drop_decisions
            _write_exclusive(staging / REVISION_RECORD_NAME, _json_bytes(revision))
            _fsync_dir(staging)
        except BaseException:
            shutil.rmtree(str(staging), ignore_errors=True)
            try:
                shared_journal_path(root, revision_id).unlink()
            except FileNotFoundError:
                pass
            raise
        if target.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
            raise ProducerError("revision-exists", str(target))
        # COMMIT POINT: no-replace rename of the staged immutable revision.
        os.rename(str(staging), str(target))
        _fsync_dir(revisions_dir)
        journal["state"] = "published"
        _write_atomic(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        _commit_shared(root, journal)
        result = {
            "status": "admitted", "kind": SHARED_KINDS[kind], "shared_reference_id": reference_id,
            "shared_reference_revision_id": revision_id, "reference_created": created,
            "revision_dir": str(target), "content_digest": content_digest, "file_count": len(rows),
            "promotion": revision["promotion"],
        }
        if merge_proof is not None:
            result["spec_merge"] = merge_proof
        if sweep_unresolved:
            result["recovery_unresolved"] = sweep_unresolved
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# campaign relationships and supersession side records (D-81)
# ---------------------------------------------------------------------------


def _find_campaign_by_key_any_state(root: Path, key: str) -> Optional[Dict[str, Any]]:
    """Like `find_campaign_by_key`, but not restricted to `state == "active"` --
    a `related[]` row may point at a campaign that is already superseded."""
    rows = _campaigns_by_key(root, key)
    return rows[0] if rows else None


def validate_related(root: Path, related: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """D-81 pure validation of `campaign.json` `related[]` rows -- no write.
    Returns a list of `{"index", "code", "detail"}` violation rows; empty
    means every row resolves."""
    root = Path(root).resolve()
    violations: List[Dict[str, Any]] = []
    for i, row in enumerate(related):
        if not isinstance(row, Mapping):
            violations.append({"index": i, "code": "campaign-related-invalid", "detail": "not-an-object"})
            continue
        kind = row.get("kind")
        if kind not in RELATED_KINDS:
            violations.append({"index": i, "code": "campaign-related-invalid", "detail": f"kind:{kind}"})
            continue
        campaign_id = row.get("campaign_id")
        key = row.get("key")
        if not campaign_id and not key:
            violations.append({"index": i, "code": "campaign-related-invalid",
                               "detail": "missing-campaign_id-and-key"})
            continue
        found = read_campaign(root, campaign_id) if campaign_id else None
        if found is None and key:
            found = _find_campaign_by_key_any_state(root, key)
        if found is None:
            violations.append({"index": i, "code": "campaign-related-unresolved",
                               "detail": str(campaign_id or key)})
    return violations


def set_campaign_related(root: Path, campaign_id: str, *, related: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """D-81: producer-internal API. `campaigns/<camp>/campaign.json` is the
    `campaign-record-machine-managed` write surface -- no general writer, hook,
    or agent may call this."""
    root = Path(root).resolve()
    violations = validate_related(root, related)
    if violations:
        first = violations[0]
        raise ProducerError(first["code"], first["detail"])
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        campaign = read_campaign(root, campaign_id)
        if campaign is None:
            raise ProducerError("campaign-unknown", campaign_id)
        campaign = dict(campaign)
        campaign["related"] = [dict(row) for row in related]
        _write_campaign(root, campaign, exclusive=False)
        return {"status": "updated", "campaign_id": campaign_id, "related": campaign["related"]}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def mark_cycle_superseded(
    root: Path, cycle_id: str, *, superseded_by: Sequence[str], superseded_event_id: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The older name of `cycle-mark --superseded-by` (D-81, now D-126): the mark is the record's
    `disposition`, written by the one disposition writer.  The cycle's state, files and manifest
    stay as they were, so a marked cycle is as finished and as writable as before."""
    out = cycle_mark(root, cycle_id, superseded_by=list(superseded_by), superseded_event_id=superseded_event_id,
                     now=now)
    mark = out["disposition"]
    return {"status": "updated", "cycle_id": cycle_id, "disposition": mark,
            "superseded_by": list(mark.get("superseded_by") or []), "superseded_event_id": superseded_event_id}


def mark_campaign_superseded(root: Path, campaign_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """D-81: a campaign may be marked `superseded` only once every cycle it owns is set aside:
    marked (`cycle-mark`, or the earlier `state: superseded`) or deleted."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        campaign = read_campaign(root, campaign_id)
        if campaign is None:
            raise ProducerError("campaign-unknown", campaign_id)
        for cycle_id in campaign.get("cycles", []):
            record = read_cycle_record(root, cycle_id)
            if record is None or not (record.get("deleted_at") or cycle_disposition(record) is not None):
                raise ProducerError("campaign-has-live-cycles", campaign_id)
        artifact_locator.prepare_index_update(root, [campaign_id])
        updated = dict(campaign)
        updated["state"] = "superseded"
        _write_campaign(root, updated, exclusive=False)
        artifact_locator.update_indexes(root, [campaign_id])
        return {"status": "updated", "campaign_id": campaign_id, "state": "superseded"}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# §45 D-126: move, mark and delete a cycle; a change made by hand is found again
# ---------------------------------------------------------------------------
#
# Three commands change where a cycle is, what it is marked as, and whether it is
# there at all (`cycle-move`, `cycle-mark`, `delete`).  None asks for approval or a
# confirmation; each leaves history lines for the recorder (D-125), and a line the
# recorder cannot take waits in `history_pending` of the cycle record (campaign-level
# lines: of the campaign's runtime record) until the next trigger hands it over.
#
# A move or a deletion made by hand (a folder renamed, carried to another campaign or
# removed) is found by the next listing, begin or campaign close: `reconcile_root`
# reads the folders, sees which cycle and campaign IDs sit where, and makes the
# records say the same.  It uses the one procedure the commands use, so a command that
# stopped half way is finished by the next look.

CAMPAIGN_RUNTIME_DIR = "campaigns"
_UNSET = object()
_CLOSED_RECORD_STATES = frozenset({"sealed", "superseded"})
_HAND_ACTOR = "rule"


def campaign_runtime_path(root: Path, campaign_id: str) -> Path:
    if not artifact_identity.is_well_formed(campaign_id, "campaign"):
        raise ProducerError("campaign-id-invalid", str(campaign_id))
    return producer_dir(root) / CAMPAIGN_RUNTIME_DIR / f"{campaign_id}.json"


def campaign_runtime_record(root: Path, campaign_id: str) -> Optional[Dict[str, Any]]:
    """What the root's runtime keeps about a campaign beside its own `campaign.json`:
    history lines not yet handed over and, once the folder is gone, who it was."""
    try:
        return _read_json(campaign_runtime_path(root, campaign_id))
    except ProducerError:
        return None


def read_campaign_tombstone(root: Path, campaign_id: str) -> Optional[Dict[str, Any]]:
    record = campaign_runtime_record(root, campaign_id)
    return record if record is not None and record.get("deleted_at") else None


def _write_campaign_runtime_record(root: Path, record: Mapping[str, Any]) -> None:
    path = campaign_runtime_path(root, record["campaign_id"])
    _ensure_dir(path.parent)
    _write_atomic(path, _json_bytes(dict(record)), 0o600)


def campaign_or_tombstone(root: Path, campaign_id: str) -> Optional[Dict[str, Any]]:
    """The campaign's record, or what is kept of it after its folder was deleted."""
    return read_campaign(root, campaign_id) or read_campaign_tombstone(root, campaign_id)


def cycle_disposition(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """How a cycle is marked (D-126), reading the earlier `state: superseded` the same way."""
    mark = record.get("disposition")
    if isinstance(mark, dict) and mark.get("kind") in ("discarded", "superseded"):
        return dict(mark)
    if record.get("state") == "superseded":
        return {"kind": "superseded", "superseded_by": list(record.get("superseded_by") or []),
                "reason": None, "marked_at": record.get("sealed_on"), "marked_by": "rule"}
    return None


def _disposition_value(mark: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if mark is None:
        return {"value": None}
    value: Dict[str, Any] = {"kind": mark["kind"]}
    if mark.get("superseded_by"):
        value["superseded_by"] = list(mark["superseded_by"])
    return {"value": value}


def _command_stamp() -> str:
    return str(time.time_ns())


def _command_line(*, command: str, stamp: str, target_type: str, target_id: str, target_path: str,
                  operation: str, field: str, before: Mapping[str, Any], after: Mapping[str, Any],
                  reason: Optional[str], now: Optional[float], by: str = "human") -> Dict[str, Any]:
    """One `make_event` argument set for a command run.  The IDs come from the run, so a line that
    is handed over twice after a crash is the same line, and two runs of one command are two lines."""
    return {
        "kind": "lifecycle", "target_type": target_type, "target_id": target_id, "target_path": target_path,
        "operation": operation, "field": field, "before": dict(before), "after": dict(after),
        "reason": reason or command,
        "transaction_id": _history_id("htxn", command, target_id, stamp),
        "event_id": _history_id("hevt", command, target_type, target_id, field, operation, stamp),
        "now": time.time() if now is None else float(now), **_history_actor(by)}


def _with_cycle_lines(record: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return _with_pending(record, _merge_pending(record.get("history_pending") or [], lines))


def _flush_cycle_pending_locked(root: Path, cycle_id: str) -> None:
    record = read_cycle_record(root, cycle_id)
    pending = (record or {}).get("history_pending") or []
    if pending and _history_deliver_locked(root, pending):
        _write_history_pending(root, record, [])


def _campaign_lines_locked(root: Path, campaign_id: str, lines: Sequence[Mapping[str, Any]]) -> None:
    """Keep campaign-level lines in the campaign's runtime record, then try to hand them over."""
    runtime = campaign_runtime_record(root, campaign_id) or {
        "schema_version": 1, "contract": CONTRACT, "campaign_id": campaign_id}
    pending = _merge_pending(runtime.get("history_pending") or [], lines)
    if pending:
        runtime["history_pending"] = pending
    else:
        runtime.pop("history_pending", None)
    _write_campaign_runtime_record(root, runtime)
    _flush_campaign_pending_locked(root, campaign_id)


def _flush_campaign_pending_locked(root: Path, campaign_id: str) -> None:
    runtime = campaign_runtime_record(root, campaign_id)
    pending = (runtime or {}).get("history_pending") or []
    if not pending or not _history_deliver_locked(root, pending):
        return
    runtime = dict(runtime)
    runtime.pop("history_pending", None)
    _write_campaign_runtime_record(root, runtime)


def record_campaign_state_line(root: Path, campaign_id: str, *, before: str, after: str,
                               reason: Optional[str], event_id: Optional[str], command: str) -> None:
    """D-125: a campaign close or reopen leaves one `state` line (the admission lock is held)."""
    path = None
    try:
        path = campaign_dir(root, campaign_id)
    except ProducerError:
        pass
    rel = Path(os.path.relpath(str(path), str(Path(root)))).as_posix() if path is not None else f"campaigns/{campaign_id}"
    line = _command_line(command=command, stamp=event_id or _command_stamp(), target_type="campaign",
                         target_id=campaign_id, target_path=rel, operation="update", field="state",
                         before={"value": before}, after={"value": after}, reason=reason, now=None)
    _campaign_lines_locked(root, campaign_id, [line])


def deliver_pending_history(root: Path, *, wait: float = 0.0) -> int:
    """Hand every pending line of the root's cycles and campaigns to the recorder; the number handed over.

    Cheap when nothing waits and when there is no recorder.  Never raises: a busy lock or a
    recorder that fails leaves the lines where they are for the next trigger."""
    try:
        root = Path(root).resolve()
        if _history_module() is None:
            return 0
        waiting: List[Tuple[str, str]] = []
        for kind, directory in (("cycle", producer_dir(root) / "cycles"),
                                ("campaign", producer_dir(root) / CAMPAIGN_RUNTIME_DIR)):
            try:
                names = sorted(os.listdir(str(directory)))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    if b'"history_pending"' in (directory / name).read_bytes():
                        waiting.append((kind, name[:-5]))
                except OSError:
                    continue
        if not waiting:
            return 0
        if artifact_admission.holds_lock(root):
            fd = None
        else:
            fd = artifact_admission.try_acquire_lock(root) if wait <= 0 else artifact_admission._acquire_lock(root, wait)
            if fd is None:
                return 0
        handed = 0
        try:
            for kind, identifier in waiting:
                if kind == "cycle":
                    before = (read_cycle_record(root, identifier) or {}).get("history_pending") or []
                    _flush_cycle_pending_locked(root, identifier)
                    after = (read_cycle_record(root, identifier) or {}).get("history_pending") or []
                else:
                    before = (campaign_runtime_record(root, identifier) or {}).get("history_pending") or []
                    _flush_campaign_pending_locked(root, identifier)
                    after = (campaign_runtime_record(root, identifier) or {}).get("history_pending") or []
                handed += max(0, len(before) - len(after))
        finally:
            if fd is not None:
                artifact_admission._release_lock(root, fd)
        return handed
    except Exception:  # noqa: BLE001 -- handing lines over never fails the trigger
        return 0


def _closed_record(record: Mapping[str, Any]) -> bool:
    return record.get("state") in _CLOSED_RECORD_STATES


def cycle_record_closed(record: Optional[Mapping[str, Any]]) -> bool:
    """A cycle that was finished (its manifest is `manifest.json`), whatever it is marked as since."""
    return isinstance(record, Mapping) and _closed_record(record)


def _cycle_rel(root: Path, directory: Path) -> str:
    return Path(os.path.relpath(str(directory), str(Path(root)))).as_posix()


def _last_known_path(root: Path, record: Mapping[str, Any], where: Optional[str] = None) -> str:
    """Where a cycle (or, with no `locator`, a campaign) last sat, for a line that needs a path when the
    folder is already gone: the path it was found at, else what the locator map still says, else what
    its record names."""
    if where:
        return where
    try:
        published = artifact_locator._load_index(root) or {}
    except Exception:  # noqa: BLE001 -- an unreadable map only means the record's own words are used
        published = {}
    campaign_id, locator = record.get("campaign_id"), record.get("locator")
    last = published.get(record.get("cycle_id"))
    if isinstance(last, str) and last:
        return last
    if locator:
        base = published.get(campaign_id)
        return f"{base if isinstance(base, str) and base else f'campaigns/{campaign_id}'}/{locator}"
    return f"campaigns/{campaign_id or record.get('cycle_id')}"


def _locator_suffix(base: str, locator: str) -> str:
    return locator[len(base):] if locator.startswith(base) and re.fullmatch(r"(-\d+)?", locator[len(base):]) else ""


def _read_manifest_raw(directory: Path) -> Optional[Tuple[bytes, Dict[str, Any]]]:
    path = directory / "manifest.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return (raw, document) if isinstance(document, dict) else None


def _observe_control_changes(root: Path, cycle_id: str, *, now: Optional[float] = None,
                             directory: Optional[Path] = None, locator_cache: bool = False) -> None:
    """Log ordinary record edits/deletions through the existing recorder.

    No payload is restored. Bookkeeping can be read from its admitted revision
    and written again by an ordinary operation. Observation is best effort and
    never changes a completion decision or fails the caller.
    """
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None or record.get("deleted_at") or not _closed_record(record):
            return
        directory = directory or _record_cycle_manifest_path(root, record).parent
        if not directory.is_dir() or directory.is_symlink():
            return
        binding = artifact_locator.cycle_binding_bytes(record["campaign_id"], cycle_id,
                                                       started_on=record.get("started_on"))
        journal = _read_json(journal_path(root, cycle_id)) or {}
        manifest_expected = (journal.get("manifest_digest") if journal.get("state") == "refreshing"
                             else record.get("manifest_digest"))
        paths = [(directory / "manifest.json", manifest_expected),
                 (directory / artifact_locator.CYCLE_BINDING, _digest(binding)),
                 (cycle_record_path(root, cycle_id), record.get("control_record_digest") or _record_content_digest(record))]
        if locator_cache:
            expected = artifact_locator.index_observation_baselines(root)
            paths += [(root / "campaigns" / name, expected.get(name)) for name in ("INDEX.json", "INDEX.md")]
        prior = dict(record.get("control_observations") or {})
        baselines = dict(record.get("control_baselines") or {})
        stamp = _command_stamp()
        updates = {}
        lines = []
        for path, expected in paths:
            rel = path.relative_to(root).as_posix()
            if path.parent == root / "campaigns":
                after = _digest(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
            elif path == cycle_record_path(root, cycle_id):
                parsed = _read_json(path)
                after = _record_content_digest(parsed) if parsed is not None else None
            elif path.is_symlink() or not path.is_file():
                after = None
            elif path.name == "manifest.json":
                parsed = _read_json(path)
                after = artifact_manifest.manifest_digest(parsed) if parsed is not None else _digest(path.read_bytes())
            else:
                after = _digest(path.read_bytes())
            before = prior.get(rel, expected) if baselines.get(rel, expected) == expected else expected
            baselines[rel] = expected
            if after == expected:
                if before != after:
                    updates[rel] = after
                continue
            if before == after:
                continue
            updates[rel] = _record_content_digest(record) if path == cycle_record_path(root, cycle_id) else after
            lines.append({"kind": "artifact", "target_type": "artifact", "target_id": cycle_id,
                          "target_path": rel, "operation": "delete" if after is None else "update",
                          "field": "content", "before": {"value": before}, "after": {"value": after},
                          "reason": "filesystem-change", **_history_actor("rule"),
                          "transaction_id": _history_id("htxn", cycle_id, rel, stamp),
                          "event_id": _history_id("hevt", cycle_id, rel, stamp),
                          "now": time.time() if now is None else float(now)})
        if not lines and not updates:
            return
        fd = artifact_admission.try_acquire_lock(root)
        if fd is None:
            return
        try:
            current = read_cycle_record(root, cycle_id)
            if current != record:
                return
            prior.update(updates)
            pending = _merge_pending(record.get("history_pending") or [], lines)
            _write_cycle_record(root, _with_pending(dict(record, control_observations=prior,
                                                       control_baselines=baselines), pending), exclusive=False)
            _flush_cycle_pending_locked(root, cycle_id)
        finally:
            artifact_admission._release_lock(root, fd)
    except Exception:  # noqa: BLE001 -- a history observation cannot block ordinary work
        pass


def _preserve_current_manifest(root: Path, record: Mapping[str, Any], directory: Path) -> None:
    """Keep the manifest as it is now (a cycle closed before copies existed has none yet)."""
    if not _closed_record(record):
        return
    found = _read_manifest_raw(directory)
    if found is None:
        return
    try:
        artifact_lifecycle.preserve_manifest_snapshot(root, record["cycle_id"], found[0])
    except artifact_lifecycle.LifecycleError:
        pass


# -- cycle-mark -------------------------------------------------------------------


def cycle_mark(root: Path, cycle_id: str, *, discard: bool = False, superseded_by: Optional[Sequence[str]] = None,
               clear: bool = False, primary: Optional[str] = None, reason: Optional[str] = None,
               now: Optional[float] = None, superseded_event_id: Optional[str] = None) -> Dict[str, Any]:
    """D-126: mark a cycle discarded or superseded, take the mark off, or name its primary document.

    The first three change one field of the cycle record, `disposition`: the folder, the files,
    the manifest and whether the cycle is finished are as they were.  `primary` makes another
    document the one that stands for the cycle: a new manifest document that differs from the
    last one only in two `role` values (the earlier document is kept as it was published)."""
    root = Path(root).resolve()
    chosen = [name for name, on in (("--discard", discard), ("--superseded-by", superseded_by is not None),
                                    ("--clear", clear), ("--primary", primary is not None)) if on]
    if len(chosen) != 1:
        raise ProducerError("request-invalid", "cycle-mark takes exactly one of --discard, --superseded-by, --clear, --primary")
    if primary is not None:
        return _mark_primary(root, cycle_id, primary, reason=reason, now=now)
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        directory = None
        try:
            directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        except (ProducerError, artifact_locator.LocatorError):
            pass
        before = cycle_disposition(record)
        marked_by = _history_by("human")
        when = _rfc3339(now)
        if clear:
            if "disposition" not in record:
                return {"status": "unchanged", "cycle_id": cycle_id}
            updated = {key: value for key, value in record.items() if key != "disposition"}
            after_mark, operation = None, "update"  # the recorder deletes only a `state`; a taken-off mark is an update to nothing
        else:
            if discard:
                mark: Dict[str, Any] = {"kind": "discarded"}
            else:
                named = list(dict.fromkeys(superseded_by or ()))
                for other in named:
                    if not artifact_identity.is_well_formed(other, "cycle") or read_cycle_record(root, other) is None:
                        raise ProducerError("cycle-unknown", str(other))
                mark = {"kind": "superseded"}
                if named:
                    mark["superseded_by"] = named
                if superseded_event_id:
                    mark["superseded_event_id"] = superseded_event_id
            mark.update(reason=reason if isinstance(reason, str) and reason.strip() else None,
                        marked_at=when, marked_by=marked_by)
            updated = dict(record, disposition=mark)
            after_mark, operation = mark, ("add" if before is None else "update")
        if directory is not None:
            _preserve_current_manifest(root, record, directory)
        line = _command_line(
            command="cycle-mark", stamp=stamp, target_type="cycle", target_id=cycle_id,
            target_path=_last_known_path(root, record, _cycle_rel(root, directory) if directory is not None else ""),
            operation=operation,
            field="disposition", before=_disposition_value(before), after=_disposition_value(after_mark),
            reason=reason, now=now)
        _write_cycle_record(root, _with_cycle_lines(updated, [line]), exclusive=False)
        _flush_cycle_pending_locked(root, cycle_id)
        result = {"status": "cleared" if clear else "marked", "cycle_id": cycle_id}
        if after_mark is not None:
            result["disposition"] = after_mark
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


# -- the next manifest document of a closed cycle, for a change that is not a file change ---


def _publish_document_locked(root: Path, record: Mapping[str, Any], directory: Path, raw: bytes,
                             document: Mapping[str, Any], refreshed: Dict[str, Any],
                             lines: Sequence[Mapping[str, Any]], *, now: Optional[float],
                             record_after: Optional[Mapping[str, Any]] = None,
                             moved_fields: Sequence[str] = (),
                             index: Optional[artifact_index.IndexDocument] = None,
                             cycle_path: Optional[str] = None,
                             digest: Optional[str] = None) -> Dict[str, Any]:
    """Publish `refreshed` as the cycle's current document (the admission lock is held).

    The same order a refresh keeps: the earlier document is preserved, the history lines go to the
    recorder (what it cannot take waits in the record), the new document is preserved, the manifest is
    replaced atomically, then the index row is swapped on the earlier digest and the record follows.
    `record_after` is the record the caller already changed (a move); `cycle_path` is where the folder
    is now; `digest` is the earlier document's digest when the caller already has it.  Returns the
    record as written."""
    cycle_id = record["cycle_id"]
    manifest_path = directory / "manifest.json"
    if digest is None:
        digest = artifact_manifest.manifest_digest(document)
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, raw)
    earlier = {old.get("manifest_revision_id"): old for old in _earlier_documents(root, cycle_id, refreshed)}
    earlier[document.get("manifest_revision_id")] = dict(document)
    report = artifact_manifest.validate_update(refreshed, preserved=list(earlier.values()), previous=document,
                                               changeable_cycle_fields=moved_fields)
    if not report.ok:
        raise ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
    new_digest = artifact_manifest.manifest_digest(refreshed)
    new_raw = artifact_manifest.canonical_bytes(refreshed)
    if index is None:
        index = artifact_admission.load_index(root)  # the one read of this locked section
    row = index.manifests.get(cycle_id)
    if isinstance(row, dict) and row.get("manifest_digest") != digest:
        raise ProducerError("already-sealed-mismatch", "index")
    identity = artifact_lifecycle.read_root_identity(root)
    index_report = artifact_index.check(
        index, refreshed, idempotency_key=cycle_id, manifest_digest=new_digest,
        repository_id=identity.repository_id if identity else None,
        replaces_manifest_digest=digest if isinstance(row, dict) else None,
        known_parent_cycle_ids=(set(artifact_admission._producer_cycle_ids(root)) |
            ({record["parent_cycle_id"]} if record.get("parent_cycle_id") and
             (record.get("relocation") or {}).get("external_parent_root") else set())))
    if not index_report.ok:
        raise ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
    base_record = dict(record_after if record_after is not None else record)
    pending = _merge_pending(base_record.get("history_pending") or [], lines)
    if _history_deliver_locked(root, pending):
        pending = []
    where = cycle_path or _cycle_rel(root, directory)
    _write_journal(root, cycle_id, state="refreshing", manifest_digest=new_digest, previous_manifest_digest=digest,
                   cycle_path=where, manifest_revision_id=refreshed["manifest_revision_id"], history_pending=pending)
    artifact_lifecycle.preserve_manifest_snapshot(root, cycle_id, new_raw)
    # COMMIT POINT: atomic replacement of the current document.
    _write_atomic(manifest_path, new_raw)
    index = artifact_index.apply(index, refreshed, cycle_path=where, manifest_digest=new_digest,
                                 idempotency_key=cycle_id)
    artifact_admission._write_index(root, index)
    written = _with_pending(dict(base_record, manifest_digest=new_digest,
                                 cycle_state=refreshed["cycle"]["state"]), pending)
    _write_cycle_record(root, written, exclusive=False)
    _remove_journal(root, cycle_id)
    return written


def _next_document(document: Mapping[str, Any], allocator: artifact_identity.IdAllocator) -> Dict[str, Any]:
    refreshed = json.loads(json.dumps(document))
    refreshed["manifest_revision_id"] = allocator.allocate("manifest_revision")
    return refreshed


def _mark_primary(root: Path, cycle_id: str, primary: str, *, reason: Optional[str],
                  now: Optional[float]) -> Dict[str, Any]:
    """`cycle-mark --primary`: the named document becomes the cycle's primary one (D-126)."""
    stamp = _command_stamp()

    def current() -> Tuple[Dict[str, Any], Path, Optional[Tuple[bytes, Dict[str, Any]]], str]:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if not _closed_record(record) or record.get("deleted_at"):
            raise ProducerError("request-invalid", f"{cycle_id}: --primary names a document of a closed cycle")
        directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        rel = _cycle_relative_primary(primary, directory)
        if not rel or not rel.startswith("artifacts/"):
            raise ProducerError("request-invalid", f"--primary is a cycle-relative path (artifacts/...): {primary}")
        return record, directory, _read_manifest_raw(directory), rel

    record, directory, found, rel = current()
    listed = lambda doc: any(row.get("locator", {}).get("path") == rel for row in doc.get("artifact_revisions", []) or [])
    if found is None:
        raise ProducerError("request-invalid", f"{cycle_id}: manifest-absent")
    if not listed(found[1]):
        # The file may be one the cycle has not been observed with yet: look once, then ask again.
        refresh_cycle(root, cycle_id, trigger="explicit", now=now)
        record, directory, found, rel = current()
        if found is None or not listed(found[1]):
            raise ProducerError("request-invalid", f"{cycle_id}: primary-not-in-manifest: {rel}")
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        record, directory, found, rel = current()
        raw, document = found  # type: ignore[misc]
        revisions = {row["locator"]["path"]: row for row in document.get("artifact_revisions", []) or []}
        target = revisions.get(rel)
        if target is None:
            raise ProducerError("request-invalid", f"{cycle_id}: primary-not-in-manifest: {rel}")
        artifacts = [dict(row) for row in document.get("artifacts", []) or []]
        old = [row for row in artifacts if row.get("role") == "primary"]
        if len(old) == 1 and old[0]["artifact_id"] == target["artifact_id"]:
            return {"status": "unchanged", "cycle_id": cycle_id, "primary": rel}
        path_of = {row["artifact_id"]: path for path, row in revisions.items()}
        old_path = path_of.get(old[0]["artifact_id"]) if old else None
        for row in artifacts:
            if row["artifact_id"] == target["artifact_id"]:
                row["role"] = "primary"
            elif row.get("role") == "primary":
                row["role"] = "output"
        refreshed = _next_document(document, artifact_identity.IdAllocator())
        refreshed["artifacts"] = artifacts
        line = _command_line(
            command="cycle-mark", stamp=stamp, target_type="cycle", target_id=cycle_id,
            target_path=_cycle_rel(root, directory), operation="update", field="primary",
            before={"value": old_path}, after={"value": rel}, reason=reason, now=now)
        _publish_document_locked(root, record, directory, raw, document, refreshed, [line], now=now)
        artifact_locator.update_indexes(root, [record["campaign_id"]])
        _flush_cycle_pending_locked(root, cycle_id)
        return {"status": "marked", "cycle_id": cycle_id, "primary": rel,
                "manifest_revision_id": refreshed["manifest_revision_id"]}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# -- cycle-move -------------------------------------------------------------------------


def _resolve_campaign_argument(root: Path, value: str) -> Dict[str, Any]:
    """A campaign by its ID or by its key (an active one first, any state otherwise)."""
    if artifact_identity.is_well_formed(value, "campaign"):
        campaign = read_campaign(root, value)
    else:
        rows = _campaigns_by_key(root, value)
        campaign = next((row for row in rows if row.get("state") == "active"), rows[0] if rows else None)
    if campaign is None:
        raise ProducerError("campaign-unknown", str(value))
    return campaign


def _parent_chain_has(root: Path, start: Optional[str], target: str) -> bool:
    seen: Set[str] = set()
    node = start
    while node and node not in seen:
        if node == target:
            return True
        seen.add(node)
        node = (read_cycle_record(root, node) or {}).get("parent_cycle_id")
    return False


def _drop_group_membership(root: Path, campaign_id: str, cycle_ids: Sequence[str]) -> None:
    """A cycle that left a campaign no longer belongs to that campaign's workflow groups.

    Best effort and outside the admission lock: a declaration that cannot be read keeps its rows."""
    try:
        import artifact_workflow_groups
        plan = artifact_workflow_groups.prepare_withdrawal(root, campaign_id, list(cycle_ids))
        if plan is not None:
            artifact_workflow_groups.apply(root, plan, lock_timeout=0)
    except Exception:  # noqa: BLE001
        pass


def _edit_campaign_members(root: Path, folder: Optional[Path], cycle_id: str, *, joining: bool) -> None:
    """The cycle joins or leaves the member list of the campaign whose folder is `folder`.

    Read and written at the folder it is in, never through the locator: while a cycle is between two
    campaigns the locator's scan of either one would refuse."""
    if folder is None:
        return
    path = folder / "campaign.json"
    raw = _read_json(path)
    if raw is None:
        return
    campaign = artifact_campaign.fold_campaign(root, path, raw)
    members = [cid for cid in campaign.get("cycles", []) if cid != cycle_id]
    if joining:
        members.append(cycle_id)
    if members == list(campaign.get("cycles", [])):
        return
    updated = dict(campaign, cycles=members)
    artifact_campaign.check_campaign_write(root, path, updated)
    _write_atomic(path, _json_bytes(updated))


def _adopt_location_locked(root: Path, record: Mapping[str, Any], new_directory: Path, *,
                           command: str, stamp: str, reason: Optional[str], now: Optional[float],
                           by: str, parent: Any = _UNSET, old_campaign_folder: Optional[Path] = None,
                           index: Optional[artifact_index.IndexDocument] = None,
                           manifest: Any = _UNSET, manifest_digest: Optional[str] = None,
                           ) -> Tuple[Dict[str, Any], List[str]]:
    """Make the records say what the folders say: the cycle now sits at `new_directory`.

    The one procedure behind `cycle-move` and the finding of a hand-made move (the admission lock is
    held; the folder is already where it is).  The cycle's own binding, record, campaign lists, current
    manifest (a new document, the earlier one preserved) and index row follow, in an order a stopped run
    is finished by the next look.  `parent` (when given) is the new parent cycle ID or `None`.  Returns
    the record as written and the campaign IDs whose locator index is now stale.  `manifest` (the
    `_read_manifest_raw` result, `None` for none) and its `manifest_digest` are what a reconcile read
    before it took the lock; left out, the folder's manifest is read here."""
    cycle_id = record["cycle_id"]
    target_campaign_dir = new_directory.parent
    target = _read_json(target_campaign_dir / "campaign.json")
    if target is None or not artifact_identity.is_well_formed(target.get("campaign_id"), "campaign"):
        raise ProducerError("campaign-unknown", target_campaign_dir.name)
    old_campaign_id, new_campaign_id = record["campaign_id"], target["campaign_id"]
    started = record.get("started_on")
    where = _cycle_rel(root, new_directory)
    lines: List[Dict[str, Any]] = []
    changed: List[str] = []
    updated = dict(record)
    if old_campaign_id != new_campaign_id:
        updated["campaign_id"] = new_campaign_id
        updated["moved_at"] = _rfc3339(now)
        lines.append(_command_line(
            command=command, stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
            operation="move", field="campaign", before={"value": old_campaign_id},
            after={"value": new_campaign_id}, reason=reason, now=now, by=by))
        changed += [old_campaign_id, new_campaign_id]
    if updated.get("locator") != new_directory.name:
        if old_campaign_id == new_campaign_id:
            before_path = _cycle_rel(root, new_directory.parent / str(record.get("locator") or cycle_id))
            lines.append(_command_line(
                command=command, stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
                operation="move", field="path", before={"value": before_path}, after={"value": where},
                reason=reason, now=now, by=by))
        base = artifact_locator.locator_base(started, record.get("slug") or "") if started else ""
        updated["locator"] = new_directory.name
        updated["locator_suffix"] = _locator_suffix(base, new_directory.name)
        changed.append(new_campaign_id)
    if parent is not _UNSET and parent != record.get("parent_cycle_id"):
        updated["parent_cycle_id"] = parent
        if parent is None:
            updated.pop("parent_cycle_state_at_begin", None)
        else:
            updated["parent_cycle_state_at_begin"] = (read_cycle_record(root, parent) or {}).get("state", "open")
        lines.append(_command_line(
            command=command, stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
            operation="update", field="parent", before={"value": record.get("parent_cycle_id")},
            after={"value": parent}, reason=reason, now=now, by=by))
        changed.append(new_campaign_id)
    # The binding the folder carries names the campaign it sits under.
    binding = artifact_locator.read_cycle_binding(new_directory / artifact_locator.CYCLE_BINDING)
    if binding is None or binding.get("campaign_id") != new_campaign_id or binding.get("cycle_id") != cycle_id:
        _write_atomic(new_directory / artifact_locator.CYCLE_BINDING, artifact_locator.cycle_binding_bytes(
            new_campaign_id, cycle_id, started_on=started if artifact_locator.started_on_is_valid(started) else None))
    # The campaign lists: the cycle leaves one and joins the other.
    if old_campaign_id != new_campaign_id:
        _edit_campaign_members(root, old_campaign_folder, cycle_id, joining=False)
        _edit_campaign_members(root, target_campaign_dir, cycle_id, joining=True)
    pending_record = _with_cycle_lines(updated, lines)
    if manifest is _UNSET:
        manifest = _read_manifest_raw(new_directory) if _closed_record(record) else None
    if manifest is not None:
        raw, document = manifest
        observed_digest = manifest_digest or artifact_manifest.manifest_digest(document)
        if observed_digest != record.get("manifest_digest"):
            previous = artifact_lifecycle.find_manifest_snapshot(
                root, cycle_id, manifest_digest=record.get("manifest_digest"))
            if previous is not None:
                document = previous
                raw = artifact_manifest.canonical_bytes(document)
                manifest_digest = record["manifest_digest"]
        refreshed = _next_document(document, artifact_identity.IdAllocator())
        if (record.get("relocation") or {}).get("original_root"):
            import artifact_cross_root_move
            refreshed = artifact_cross_root_move.relocate_document(root, refreshed, record["relocation"])
        campaign_row = dict(refreshed.get("campaign") or {})
        campaign_row.update(campaign_id=new_campaign_id, goal=str(target.get("goal", "")),
                            title=str(target.get("title", "")),
                            completion_criterion={"statement": str(
                                (target.get("completion_criterion") or {}).get("statement", ""))})
        refreshed["campaign"] = campaign_row
        refreshed["cycle"] = dict(refreshed["cycle"], campaign_id=new_campaign_id,
                                  parent_cycle_id=updated.get("parent_cycle_id"))
        if refreshed["campaign"] == document.get("campaign") and refreshed["cycle"] == document.get("cycle"):
            written = pending_record  # nothing the document says changed (only a folder name)
            _write_cycle_record(root, written, exclusive=False)
            _swap_index_path_locked(root, cycle_id, where, index=index)
        else:
            written = _publish_document_locked(
                root, record, new_directory, raw, document, refreshed, lines, now=now, record_after=updated,
                moved_fields=("campaign_id", "parent_cycle_id"), index=index, cycle_path=where,
                digest=manifest_digest)
    else:
        written = pending_record
        _write_cycle_record(root, written, exclusive=False)
        if _closed_record(record):
            _swap_index_path_locked(root, cycle_id, where, index=index)
    return written, list(dict.fromkeys(changed))


def _swap_index_path_locked(root: Path, cycle_id: str, where: str, *,
                            index: Optional[artifact_index.IndexDocument] = None) -> None:
    index = index if index is not None else artifact_admission.load_index(root)
    row = index.cycles.get(cycle_id)
    if isinstance(row, dict) and row.get("cycle_path") != where:
        cycles = dict(index.cycles)
        cycles[cycle_id] = dict(row, cycle_path=where)
        artifact_admission._write_index(root, replace(index, cycles=cycles))


def _retarget_index_paths(root: Path, old_prefix: str, new_prefix: str) -> None:
    """A campaign folder was renamed: the index rows of its cycles name the new folder (one read, one write)."""
    index = artifact_admission.load_index(root)
    cycles = dict(index.cycles)
    changed = False
    for cycle_id, row in index.cycles.items():
        path = row.get("cycle_path") if isinstance(row, dict) else None
        if isinstance(path, str) and (path == old_prefix or path.startswith(old_prefix + "/")):
            cycles[cycle_id] = dict(row, cycle_path=new_prefix + path[len(old_prefix):])
            changed = True
    if changed:
        artifact_admission._write_index(root, replace(index, cycles=cycles))


def cycle_move(root: Path, cycle_id: Optional[str] = None, *, campaign: Optional[str] = None, parent: Optional[str] = None,
               no_parent: bool = False, reason: Optional[str] = None, now: Optional[float] = None,
               target_artifact_root: Optional[Path] = None, source_campaign: Optional[str] = None,
               attach_logs: Sequence[str] = (), dry_run: bool = False) -> Dict[str, Any]:
    """D-126: move a cycle to another campaign and/or change its parent.

    The cycle keeps its ID.  The folder moves under the target campaign (a name taken there gets
    the smallest unused `-2`, `-3` ... suffix, D-90), and the binding, record, campaign lists,
    manifest (a new document; the earlier one is preserved), index and locator index follow.  A
    target that is closed is opened again, as a `begin` would; one that is set aside takes the cycle
    without changing its own state.  Open cycles move too (they have no manifest to write)."""
    root = Path(root).resolve()
    target_root = Path(target_artifact_root).resolve() if target_artifact_root else root
    if target_root != root or source_campaign is not None or attach_logs or dry_run:
        import artifact_cross_root_move as cross_move
        return cross_move.move(root, target_root, cycle_id=cycle_id, source_campaign=source_campaign,
                               campaign=campaign, attach_logs=attach_logs, dry_run=dry_run,
                               parent=parent, no_parent=no_parent, reason=reason, now=now)
    if parent is not None and no_parent:
        raise ProducerError("request-invalid", "--parent and --no-parent cannot be combined")
    if campaign is None and parent is None and not no_parent:
        raise ProducerError("request-invalid", "cycle-move changes the campaign and/or the parent")
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    stale: List[str] = []
    old_campaign_id = None
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("deleted_at"):
            raise ProducerError("cycle-unknown", f"{cycle_id}: deleted")
        old_campaign_id = record["campaign_id"]
        new_parent: Any = _UNSET
        if no_parent:
            new_parent = None
        elif parent is not None:
            if not artifact_identity.is_well_formed(parent, "cycle") or read_cycle_record(root, parent) is None:
                raise ProducerError("parent-cycle-not-joinable", str(parent))
            if parent == cycle_id or _parent_chain_has(root, parent, cycle_id):
                raise ProducerError("parent-cycle-invalid", str(parent))
            new_parent = parent
        target = _resolve_campaign_argument(root, campaign) if campaign is not None else None
        source = cycle_dir(root, old_campaign_id, cycle_id, record)
        destination = source
        if target is not None and target["campaign_id"] != old_campaign_id:
            artifact_locator.prepare_index_update(root, [old_campaign_id, target["campaign_id"]])
            if target.get("state") == "satisfied":
                try:
                    artifact_campaign._reopen_locked(
                        root, _campaign_path(root, target["campaign_id"], target), reason="cycle-move")
                except artifact_campaign.CampaignError as exc:
                    raise ProducerError(exc.code, exc.detail) from exc
            target_dir = campaign_dir(root, target["campaign_id"], target)
            if not source.is_dir() or source.is_symlink():
                raise ProducerError("record-locator-invalid", str(source))
            keep = target_dir / str(record.get("locator") or source.name)
            if os.path.lexists(str(keep)):
                locator_name, _suffix = artifact_locator.allocate_locator(
                    target_dir, record["started_on"], record.get("slug") or "")
            else:
                locator_name = keep.name
            destination = target_dir / locator_name
            # The folder moves first; whatever stops after this is what the next look finishes.
            os.rename(str(source), str(destination))
            _fsync_dir(target_dir)
            _fsync_dir(source.parent)
        if destination == source and new_parent is _UNSET:
            return {"status": "unchanged", "cycle_id": cycle_id, "campaign_id": old_campaign_id,
                    "cycle_dir": str(source)}
        written, stale = _adopt_location_locked(
            root, record, destination, command="cycle-move", stamp=stamp, reason=reason, now=now,
            by="human", parent=new_parent, old_campaign_folder=source.parent)
        stale = list(dict.fromkeys(stale + [old_campaign_id, written["campaign_id"]]))
        artifact_locator.update_indexes(root, stale)
        _flush_cycle_pending_locked(root, cycle_id)
    finally:
        artifact_admission._release_lock(root, lock_fd)
    if old_campaign_id != written["campaign_id"]:
        _drop_group_membership(root, old_campaign_id, [cycle_id])
    return {"status": "moved", "cycle_id": cycle_id, "campaign_id": written["campaign_id"],
            "parent_cycle_id": written.get("parent_cycle_id"), "cycle_dir": str(destination)}


# -- delete -------------------------------------------------------------------------------


def _tombstone_cycle_locked(root: Path, record: Mapping[str, Any], *, where: str, command: str, stamp: str,
                            reason: Optional[str], now: Optional[float], by: str,
                            digest: Optional[str]) -> Dict[str, Any]:
    """The cycle record says the cycle is deleted (the admission lock is held).

    The record stays, with the time and its history line, so the ID is never issued again and an earlier
    reference reads "deleted".  An open cycle's interim files go with it."""
    line = _command_line(
        command=command, stamp=stamp, target_type="cycle", target_id=record["cycle_id"], target_path=where,
        operation="delete", field="state", before={"value": {"manifest_digest": digest, "path": where}},
        after={"value": None}, reason=reason, now=now, by=by)
    updated = dict(record, deleted_at=_rfc3339(now), deleted_by=command)  # a record of who, not a gate
    written = _with_cycle_lines(updated, [line])
    _write_cycle_record(root, written, exclusive=False)
    if record.get("state") == "open":
        remove_interim(root, record["cycle_id"])
    return written


def _retire_rows(root: Path, cycle_ids: Sequence[str]) -> None:
    """Drop the cycles' current rows from the index; every ID they declared stays owned (one read, one write)."""
    index = artifact_admission.load_index(root)
    retired = index
    for cycle_id in cycle_ids:
        retired = artifact_index.retire(retired, cycle_id)
    if retired is not index:
        artifact_admission._write_index(root, retired)


def _remove_folder(root: Path, directory: Path) -> None:
    root_resolved = Path(root).resolve()
    if directory.is_symlink():
        raise ProducerError("record-locator-invalid", str(directory))
    if not directory.exists():
        return
    resolved = directory.resolve()
    if root_resolved not in resolved.parents or (root_resolved / "campaigns") not in resolved.parents:
        raise ProducerError("record-locator-invalid", str(directory))
    shutil.rmtree(directory)
    _fsync_dir(directory.parent)


def _reparent_children_locked(root: Path, deleted_ids: Sequence[str], *, command: str, stamp: str,
                              reason: Optional[str], now: Optional[float], by: str,
                              folders: Optional[Mapping[str, Path]] = None
                              ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """No surviving cycle names a cycle that is gone as its parent (the admission lock is held).

    Whoever names a deleted cycle (the ones deleted now, an earlier delete, or an ID with no record)
    takes the nearest ancestor that is still there, or none, as `cycle-move --parent` would: the
    record, a closed cycle's manifest (a new document) and the index follow and one history line is
    left.  A child whose folder cannot be found has its record mended and nothing else; it is reported
    with `folder: "missing"`.  Returns the rows `{cycle_id, before, after}` and the campaign IDs whose
    locator index is now stale.  `folders` are the paths a reconcile already found."""
    records = {record["cycle_id"]: record for record in list_cycle_records(root) if record.get("cycle_id")}
    gone = set(deleted_ids)

    def departed(cycle_id: str) -> bool:
        record = records.get(cycle_id)
        return cycle_id in gone or record is None or bool(record.get("deleted_at"))

    def surviving_ancestor(child_id: str, start: str) -> Optional[str]:
        seen: Set[str] = set()
        node: Optional[str] = start
        while node and node not in seen and node != child_id:
            if not departed(node):
                return node
            seen.add(node)
            node = (records.get(node) or {}).get("parent_cycle_id")
        return None

    rows: List[Dict[str, Any]] = []
    stale: List[str] = []
    for child_id, child in sorted(records.items()):
        before = child.get("parent_cycle_id")
        if child.get("deleted_at") or not before or not departed(before):
            continue
        after = surviving_ancestor(child_id, before)
        folder = (folders or {}).get(child_id)
        if folder is None:
            try:
                folder = cycle_dir(root, child["campaign_id"], child_id, child)
            except (ProducerError, artifact_locator.LocatorError):
                folder = None
        row: Dict[str, Any] = {"cycle_id": child_id, "before": before, "after": after}
        if folder is not None and folder.is_dir() and (folder.parent / "campaign.json").is_file():
            _written, touched = _adopt_location_locked(
                root, child, folder, command=command, stamp=stamp, reason=reason, now=now, by=by, parent=after)
            stale += touched
        else:
            updated = dict(child, parent_cycle_id=after)
            if after is None:
                updated.pop("parent_cycle_state_at_begin", None)
            else:
                updated["parent_cycle_state_at_begin"] = (records.get(after) or {}).get("state", "open")
            line = _command_line(
                command=command, stamp=stamp, target_type="cycle", target_id=child_id,
                target_path=_last_known_path(root, child), operation="update", field="parent",
                before={"value": before}, after={"value": after}, reason=reason, now=now, by=by)
            _write_cycle_record(root, _with_cycle_lines(updated, [line]), exclusive=False)
            row["folder"] = "missing"
        _flush_cycle_pending_locked(root, child_id)
        rows.append(row)
    return rows, stale


def delete_cycle(root: Path, cycle_id: str, *, reason: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """D-126: delete a cycle's folder and take it out of the index and the lists.

    The record stays (with `deleted_at`), the preserved manifest copies stay, and what the cycle
    published to `shared/` stays.  The current manifest is copied *before* the folder goes, so a
    cycle closed before copies existed still has its last document.  A run that stopped between its
    steps is finished by the next call.  A route that was still running ends as a route with no
    output (`finalize` says so); nothing puts the folder back."""
    root = Path(root).resolve()
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        try:
            directory = cycle_dir(root, record["campaign_id"], cycle_id, record)
        except (ProducerError, artifact_locator.LocatorError):
            directory = None
        finishing = bool(record.get("deleted_at"))
        if finishing and (directory is None or not directory.exists()):
            return {"status": "already-deleted", "cycle_id": cycle_id}
        campaign_id = record["campaign_id"]
        if not finishing:
            where = _last_known_path(root, record, _cycle_rel(root, directory) if directory is not None else "")
            digest = record.get("manifest_digest")
            if directory is not None:
                _preserve_current_manifest(root, record, directory)
                found = _read_manifest_raw(directory)
                if found is not None:
                    digest = artifact_manifest.manifest_digest(found[1])
            _tombstone_cycle_locked(root, record, where=where, command="delete", stamp=stamp, reason=reason,
                                    now=now, by="human", digest=digest)
        if directory is not None:
            _remove_folder(root, directory)
        campaign = read_campaign(root, campaign_id)
        if campaign is not None and cycle_id in campaign.get("cycles", []):
            _write_campaign(root, dict(campaign, cycles=[c for c in campaign["cycles"] if c != cycle_id]), exclusive=False)
        _retire_rows(root, [cycle_id])
        reparented, touched = _reparent_children_locked(
            root, [cycle_id], command="delete", stamp=stamp, reason=reason, now=now, by="human")
        artifact_locator.update_indexes(root, list(dict.fromkeys([campaign_id] + touched)))
        _flush_cycle_pending_locked(root, cycle_id)
    finally:
        artifact_admission._release_lock(root, lock_fd)
    _drop_group_membership(root, campaign_id, [cycle_id])
    done = {"status": "deleted", "cycle_id": cycle_id, "campaign_id": campaign_id}
    return dict(done, reparented=reparented) if reparented else done


def delete_campaign(root: Path, campaign: str, *, reason: Optional[str] = None,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """D-126: delete a campaign's folder with every cycle in it.

    Each member leaves what a deleted cycle leaves, and the campaign's own identity, last path and
    time are kept in `.runtime/artifact-producer/v1/campaigns/<id>.json` (that is a record of the
    root's runtime, not a folder or a `campaign.json`: nothing lists the campaign again)."""
    root = Path(root).resolve()
    stamp = _command_stamp()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        if artifact_identity.is_well_formed(campaign, "campaign") and read_campaign(root, campaign) is None:
            if read_campaign_tombstone(root, campaign) is not None:
                return {"status": "already-deleted", "campaign_id": campaign}
        found = _resolve_campaign_argument(root, campaign)
        campaign_id = found["campaign_id"]
        folder = campaign_dir(root, campaign_id, found)
        last_path = _cycle_rel(root, folder)
        by = "human"
        members, _detached = artifact_campaign.campaign_records(root, campaign_id)
        gone: List[str] = []
        for member in members:
            if member.get("deleted_at"):
                continue
            member_dir = None
            try:
                member_dir = cycle_dir(root, campaign_id, member["cycle_id"], member)
            except (ProducerError, artifact_locator.LocatorError):
                pass
            digest = member.get("manifest_digest")
            if member_dir is not None:
                _preserve_current_manifest(root, member, member_dir)
            _tombstone_cycle_locked(
                root, member, where=_cycle_rel(root, member_dir) if member_dir is not None else "",
                command="delete", stamp=stamp, reason=reason, now=now, by=by, digest=digest)
            gone.append(member["cycle_id"])
        runtime = campaign_runtime_record(root, campaign_id) or {
            "schema_version": 1, "contract": CONTRACT, "campaign_id": campaign_id}
        runtime.update(key=found.get("key"), title=found.get("title"), goal=found.get("goal"),
                       locator=found.get("locator"), last_path=last_path, deleted_at=_rfc3339(now),
                       cycles=list(found.get("cycles", [])), state=found.get("state"))
        line = _command_line(
            command="delete", stamp=stamp, target_type="campaign", target_id=campaign_id, target_path=last_path,
            operation="delete", field="state", before={"value": {"manifest_digest": None, "path": last_path}},
            after={"value": None}, reason=reason, now=now, by=by)
        runtime["history_pending"] = _merge_pending(runtime.get("history_pending") or [], [line])
        _write_campaign_runtime_record(root, runtime)
        _remove_folder(root, folder)
        _retire_rows(root, gone)
        reparented, touched = _reparent_children_locked(
            root, gone, command="delete", stamp=stamp, reason=reason, now=now, by=by)
        artifact_locator.update_indexes(root, list(dict.fromkeys([campaign_id] + touched)))
        for cycle_id in gone:
            _flush_cycle_pending_locked(root, cycle_id)
        _flush_campaign_pending_locked(root, campaign_id)
    finally:
        artifact_admission._release_lock(root, lock_fd)
    done = {"status": "deleted", "campaign_id": campaign_id, "cycle_ids": gone}
    return dict(done, reparented=reparented) if reparented else done


# -- a move or a deletion made by hand ---------------------------------------------------


@dataclass
class _LayoutScan:
    campaigns: Dict[str, Path]
    cycles: Dict[str, Path]
    folder_campaign: Dict[str, str]   # cycle ID -> ID of the campaign folder it sits in
    duplicates: Set[str]
    complete: bool                    # every folder was read and every entry was one this scan understands

    def mapping(self, root: Path) -> Dict[str, str]:
        found = {identifier: _cycle_rel(root, path) for identifier, path in self.campaigns.items()}
        found.update({identifier: _cycle_rel(root, path) for identifier, path in self.cycles.items()})
        return found


def _folder_cycle_id(folder: Path, manifest: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """The surviving identity, shared by the scan and its locked confirmation."""
    try:
        binding = artifact_locator.read_cycle_binding(folder)
    except artifact_locator.LocatorError:
        binding = None
    if binding is not None:
        return binding["cycle_id"]
    document = manifest if manifest is not None else (_read_json(folder / "manifest.json") or {})
    cycle_id = (document.get("cycle") or {}).get("cycle_id")
    return cycle_id if artifact_identity.is_well_formed(cycle_id, "cycle") else None


def _scan_layout(root: Path) -> _LayoutScan:
    """Where every campaign and cycle ID sits, read from the folders alone (a `lstat` and a few small files each).

    A symbolic link, an unreadable folder, a folder that carries no ID or an ID seen twice makes the
    answer `complete=False`: such a scan can say where something moved to, never that something is gone."""
    scan = _LayoutScan({}, {}, {}, set(), True)
    base = Path(root) / "campaigns"
    try:
        names = sorted(os.listdir(str(base)))
    except FileNotFoundError:
        return scan
    except OSError:
        scan.complete = False
        return scan
    for name in names:
        if name.startswith("."):
            continue
        entry = base / name
        try:
            mode = os.lstat(str(entry)).st_mode
        except OSError:
            scan.complete = False
            continue
        if stat.S_ISLNK(mode):
            scan.complete = False
            continue
        if not stat.S_ISDIR(mode):
            continue
        campaign = _read_json(entry / "campaign.json")
        campaign_id = campaign.get("campaign_id") if campaign else None
        if not artifact_identity.is_well_formed(campaign_id, "campaign"):
            scan.complete = False
            continue
        if campaign_id in scan.campaigns:
            scan.duplicates.add(campaign_id)
            scan.complete = False
            continue
        scan.campaigns[campaign_id] = entry
        try:
            children = sorted(os.listdir(str(entry)))
        except OSError:
            scan.complete = False
            continue
        for child in children:
            path = entry / child
            if child.startswith(".") or child == artifact_locator.CAMPAIGN_EVENTS_DIR:
                continue
            try:
                mode = os.lstat(str(path)).st_mode
            except OSError:
                scan.complete = False
                continue
            if stat.S_ISLNK(mode):
                scan.complete = False
                continue
            if not stat.S_ISDIR(mode):
                continue
            if child == "cycles":
                scan.complete = False  # an old layout: moves are not judged from it
                continue
            cycle_id = _folder_cycle_id(path)
            if cycle_id is None:
                scan.complete = False
                continue
            if cycle_id in scan.cycles or cycle_id in scan.duplicates:
                scan.duplicates.add(cycle_id)
                scan.cycles.pop(cycle_id, None)
                scan.folder_campaign.pop(cycle_id, None)
                scan.complete = False
                continue
            scan.cycles[cycle_id] = path
            scan.folder_campaign[cycle_id] = campaign_id
    return scan


def _hand_changes(root: Path, scan: _LayoutScan, published: Mapping[str, str]) -> Dict[str, Any]:
    """What the folders say that the records do not (read-only)."""
    changes: Dict[str, Any] = {"campaign_paths": [], "cycle_moves": [], "cycle_revive": [],
                               "cycle_gone": [], "campaign_gone": []}
    for campaign_id, folder in sorted(scan.campaigns.items()):
        campaign = _read_json(folder / "campaign.json") or {}
        # A record that never carried a readable locator (an old W7 campaign) is not rewritten.
        if isinstance(campaign.get("locator"), str) and campaign["locator"] != folder.name:
            changes["campaign_paths"].append((campaign_id, folder))
    for cycle_id, folder in sorted(scan.cycles.items()):
        if published.get(cycle_id) == _cycle_rel(root, folder):
            continue
        record = read_cycle_record(root, cycle_id)
        if record is not None and record.get("deleted_at"):
            # Only what a reconcile took for gone comes back with its folder; an explicit `delete` stays deleted.
            if record.get("deleted_by") == "reconcile" and read_campaign_tombstone(
                    root, scan.folder_campaign[cycle_id]) is None:
                changes["cycle_revive"].append((cycle_id, folder))
            continue
        if record is None:
            continue
        if record.get("campaign_id") != scan.folder_campaign[cycle_id] or (
                isinstance(record.get("locator"), str) and record["locator"] != folder.name):
            changes["cycle_moves"].append((cycle_id, folder))
    if scan.complete:
        for identifier in sorted(published):
            if identifier in scan.cycles or identifier in scan.campaigns or identifier in scan.duplicates:
                continue
            if artifact_identity.is_well_formed(identifier, "cycle"):
                record = read_cycle_record(root, identifier)
                if record is not None and not record.get("deleted_at") and _closed_record(record):
                    changes["cycle_gone"].append(identifier)
            elif artifact_identity.is_well_formed(identifier, "campaign"):
                if read_campaign_tombstone(root, identifier) is None:
                    changes["campaign_gone"].append(identifier)
    return changes


class _PreManifest(NamedTuple):
    """A closed cycle's manifest as read before the lock, with the `lstat` fingerprint it was read under."""
    found: Optional[Tuple[bytes, Dict[str, Any]]]
    digest: Optional[str]
    fingerprint: Optional[Tuple[int, int, int]]


def _manifest_fingerprint(folder: Path) -> Optional[Tuple[int, int, int]]:
    try:
        seen = os.lstat(str(Path(folder) / "manifest.json"))
    except OSError:
        return None
    return (seen.st_size, seen.st_mtime_ns, seen.st_ino)


def _preread_manifests(root: Path, changes: Mapping[str, Any]) -> Dict[str, _PreManifest]:
    """Read, with no lock held, each moved cycle's surviving manifest and identity."""
    out: Dict[str, _PreManifest] = {}
    for cycle_id, folder in list(changes["cycle_moves"]) + list(changes["cycle_revive"]):
        record = read_cycle_record(root, cycle_id)
        if record is None:
            continue
        fingerprint = _manifest_fingerprint(folder)
        found = _read_manifest_raw(folder)
        out[cycle_id] = _PreManifest(
            found, artifact_manifest.manifest_digest(found[1]) if found is not None else None, fingerprint)
    return out


def reconcile_root(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    """D-126: find what was moved, renamed or removed by hand and make the records say so.

    A cycle folder carried to another campaign, a folder renamed, a folder or a whole campaign removed
    is noticed by the next listing, begin or campaign close: the cycle (or campaign) keeps its ID, the
    records, lists, manifest and index follow, and one history line (by `rule`) is left.  No error, no
    recovery command.  Nothing is taken for gone unless every folder of the root could be read; a
    copy, a link or a folder with no ID is left alone.  Best effort: it never fails its caller."""
    try:
        root = Path(root).resolve()
        if not is_active(root):
            return {"status": "inactive"}
        # The folders are read with no lock held (§45 D-124); the lock only confirms what was found.
        # A change that no longer holds when the lock is taken is looked for once more.
        for _attempt in range(2):
            published = artifact_locator._load_index(root)
            if published is None:
                # INDEX is a disposable cache. The admission rows retain
                # last locations even when the entire campaigns tree is gone.
                index = artifact_admission.load_index(root)
                published = {}
                for cycle_id, row in index.cycles.items():
                    where = row.get("cycle_path") if isinstance(row, dict) else None
                    record = read_cycle_record(root, cycle_id)
                    if isinstance(where, str) and record is not None:
                        published[cycle_id] = where
                        published[record["campaign_id"]] = str(Path(where).parent)
            scan = _scan_layout(root)
            ordered = sorted(scan.cycles, key=lambda cid: ((read_cycle_record(root, cid) or {}).get("started_on") or "", cid))
            for position, cycle_id in enumerate(ordered):
                _observe_control_changes(root, cycle_id, now=now, directory=scan.cycles[cycle_id], locator_cache=position == 0)
            if scan.mapping(root) == published:
                return {"status": "unchanged"}
            changes = _hand_changes(root, scan, published)
            if not any(changes.values()):
                return {"status": "unchanged"}
            seen: frozenset = frozenset()
            if changes["cycle_gone"] or changes["campaign_gone"]:
                changes, seen = _look_again_before_gone(root, changes)
                if not any(changes.values()):
                    return {"status": "unchanged"}
            result = _reconcile_locked_run(root, now, scan, published, changes, seen,
                                           _preread_manifests(root, changes))
            if result is not None:
                return result
        return {"status": "skipped", "reason": "layout-changed-during-scan"}
    except Exception as exc:  # noqa: BLE001 -- finding a hand-made change never fails the command that asked
        return {"status": "skipped", "reason": type(exc).__name__, "detail": str(exc)}


def _revive_cycle_locked(root: Path, record: Mapping[str, Any], folder: Path, *, stamp: str,
                         now: Optional[float], by: str,
                         old_campaign_folder: Optional[Path],
                         manifest: Any = _UNSET, manifest_digest: Optional[str] = None) -> List[str]:
    """A cycle a reconcile took for gone has its folder again (the admission lock is held).

    The tombstone comes off, the location is adopted as a hand-made move is, the cycle joins its campaign's
    list, the index gets its row from the current manifest, and one line says it is back.  Returns the campaign
    IDs whose locator index is now stale.  `manifest` and `manifest_digest` are what the reconcile read
    before the lock (see `_adopt_location_locked`)."""
    cycle_id = record["cycle_id"]
    where = _cycle_rel(root, folder)
    revived = {key: value for key, value in record.items() if key not in ("deleted_at", "deleted_by")}
    index = artifact_admission.load_index(root)  # the one read of this locked section
    found = manifest if manifest is not _UNSET else (_read_manifest_raw(folder) if _closed_record(record) else None)
    if found is not None and (cycle_id not in index.cycles or cycle_id not in index.manifests):
        # The row comes back first, from the manifest the folder carries; a changed campaign is then an
        # ordinary replacement of that row.
        digest = manifest_digest or artifact_manifest.manifest_digest(found[1])
        index = artifact_index.apply(index, found[1], cycle_path=where, manifest_digest=digest,
                                     idempotency_key=cycle_id)
        artifact_admission._write_index(root, index)
        revived["manifest_digest"] = digest
    written, touched = _adopt_location_locked(
        root, revived, folder, command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
        old_campaign_folder=old_campaign_folder, index=index, manifest=found,
        manifest_digest=manifest_digest)
    _edit_campaign_members(root, folder.parent, cycle_id, joining=True)
    line = _command_line(
        command="reconcile", stamp=stamp, target_type="cycle", target_id=cycle_id, target_path=where,
        operation="update", field="path", before={"value": None}, after={"value": where},
        reason="reconcile", now=now, by=by)
    _write_cycle_record(root, _with_cycle_lines(written, [line]), exclusive=False)
    return touched + [written["campaign_id"]]


def _look_again_before_gone(root: Path, changes: Mapping[str, Any]) -> Tuple[Dict[str, Any], frozenset]:
    """A folder carried from a campaign the scan had not reached yet into one it had already read is
    in neither: before anything is taken for gone, the folders are read once more (no lock held).

    What the second reading sees is left out of the gone lists (the next look finds where it moved to),
    and a second reading that is not complete takes nothing for gone.  Returns the changes and the cycle
    IDs the second reading saw."""
    again = _scan_layout(root)
    if not again.complete:
        return dict(changes, cycle_gone=[], campaign_gone=[]), frozenset()
    seen = frozenset(again.cycles) | frozenset(again.duplicates)
    return dict(
        changes, cycle_gone=[item for item in changes["cycle_gone"] if item not in seen],
        campaign_gone=[item for item in changes["campaign_gone"]
                       if item not in again.campaigns and item not in again.duplicates]), seen


def _hand_changes_hold(root: Path, changes: Mapping[str, Any], published: Mapping[str, str],
                       manifests: Optional[Mapping[str, _PreManifest]] = None) -> bool:
    """Under the lock, with `lstat` and the small files that name an ID: is each change the scan found still there?"""
    def gone(relative: str) -> bool:
        try:
            os.lstat(str(Path(root) / relative))
        except (FileNotFoundError, NotADirectoryError):
            return True
        return False

    def folder_is(path: Path) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(str(path)).st_mode)
        except OSError:
            return False

    for campaign_id, folder in changes["campaign_paths"]:
        campaign = _read_json(folder / "campaign.json") if folder_is(folder) else None
        if not campaign or campaign.get("campaign_id") != campaign_id or campaign.get("locator") == folder.name:
            return False
    for cycle_id, folder in list(changes["cycle_moves"]) + list(changes["cycle_revive"]):
        pre = (manifests or {}).get(cycle_id)
        document = (pre.found[1] if pre is not None and pre.found is not None else {}) \
            if manifests is not None else None
        if not folder_is(folder) or _folder_cycle_id(folder, document) != cycle_id:
            return False
    for identifier in list(changes["cycle_gone"]) + list(changes["campaign_gone"]):
        if identifier not in published or not gone(published[identifier]):
            return False
    return True


def _reconcile_locked_run(root: Path, now: Optional[float], scan: _LayoutScan, published: Mapping[str, str],
                          changes: Mapping[str, Any], seen: frozenset = frozenset(),
                          manifests: Optional[Mapping[str, _PreManifest]] = None) -> Optional[Dict[str, Any]]:
    """Apply what `reconcile_root` found, under the admission lock.  `None` when what was found
    no longer holds, for the caller to look again.  `manifests` are the closed cycles' manifests read
    before the lock; under it only their `lstat` fingerprint is looked at (a manifest that changed since
    is for the next look).  Left out, they are read here."""
    held = artifact_admission.holds_lock(root)
    lock_fd = None if held else artifact_admission._acquire_lock(root, REFRESH_ADMISSION_WAIT_SECONDS, now=now)
    try:
        current_index = artifact_locator._load_index(root)
        if ((current_index is not None and current_index != published)
                or not _hand_changes_hold(root, changes, published, manifests)):
            return None
        if manifests is not None:
            for cycle_id, folder in list(changes["cycle_moves"]) + list(changes["cycle_revive"]):
                current = read_cycle_record(root, cycle_id)
                if current is None:
                    continue
                pre = manifests.get(cycle_id)
                if pre is None or pre.fingerprint != _manifest_fingerprint(folder):
                    return None
        stamp = _command_stamp()
        by = _HAND_ACTOR
        stale: List[str] = []
        result: Dict[str, Any] = {"status": "reconciled", "campaign_paths": [], "cycle_moves": [],
                                  "cycle_gone": [], "campaign_gone": []}
        for campaign_id, folder in changes["campaign_paths"]:
            campaign = _read_json(folder / "campaign.json") or {}
            old_rel = f"campaigns/{campaign.get('locator')}"
            base = artifact_locator.locator_base(str(campaign.get("created_on") or ""), campaign.get("slug") or "") \
                if campaign.get("created_on") else ""
            folded = artifact_campaign.fold_campaign(root, folder / "campaign.json", campaign)
            updated = dict(folded, locator=folder.name, locator_suffix=_locator_suffix(base, folder.name))
            artifact_campaign.check_campaign_write(root, folder / "campaign.json", updated)
            _write_atomic(folder / "campaign.json", _json_bytes(updated))
            _campaign_lines_locked(root, campaign_id, [_command_line(
                command="reconcile", stamp=stamp, target_type="campaign", target_id=campaign_id,
                target_path=_cycle_rel(root, folder), operation="update", field="path",
                before={"value": old_rel}, after={"value": _cycle_rel(root, folder)}, reason="reconcile",
                now=now, by=by)])
            _retarget_index_paths(root, old_rel, _cycle_rel(root, folder))
            stale.append(campaign_id)
            result["campaign_paths"].append(campaign_id)
        left: Dict[str, List[str]] = {}
        for cycle_id, folder in changes["cycle_moves"]:
            record = read_cycle_record(root, cycle_id)
            if record is None:
                continue
            was = record["campaign_id"]
            pre = (manifests or {}).get(cycle_id)
            read = {"manifest": pre.found, "manifest_digest": pre.digest} if pre is not None else {}
            written, touched = _adopt_location_locked(
                root, record, folder, command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
                old_campaign_folder=scan.campaigns.get(was), **read)
            _flush_cycle_pending_locked(root, cycle_id)
            stale += touched + [was]
            if was != written["campaign_id"]:
                left.setdefault(was, []).append(cycle_id)
            result["cycle_moves"].append(cycle_id)
        for cycle_id, folder in changes["cycle_revive"]:
            record = read_cycle_record(root, cycle_id)
            if record is None or record.get("deleted_by") != "reconcile" or not record.get("deleted_at"):
                continue
            was = record["campaign_id"]
            pre = (manifests or {}).get(cycle_id)
            read = {"manifest": pre.found, "manifest_digest": pre.digest} if pre is not None else {}
            touched = _revive_cycle_locked(root, record, folder, stamp=stamp, now=now, by=by,
                                           old_campaign_folder=scan.campaigns.get(was), **read)
            _flush_cycle_pending_locked(root, cycle_id)
            stale += touched + [was]
            result["cycle_moves"].append(cycle_id)  # back where a folder is: no field of its own
        gone_cycles: List[str] = []
        for cycle_id in changes["cycle_gone"]:
            record = read_cycle_record(root, cycle_id)
            if record is None or record.get("deleted_at") or (record.get("relocation") or {}).get("artifact_root"):
                continue
            where = _last_known_path(root, record, published.get(cycle_id, ""))
            _tombstone_cycle_locked(root, record, where=where, command="reconcile", stamp=stamp,
                                    reason="reconcile", now=now, by=by, digest=record.get("manifest_digest"))
            _edit_campaign_members(root, scan.campaigns.get(record["campaign_id"]), cycle_id, joining=False)
            stale.append(record["campaign_id"])
            gone_cycles.append(cycle_id)
            result["cycle_gone"].append(cycle_id)
        for campaign_id in changes["campaign_gone"]:
            last_path = published.get(campaign_id, "") or f"campaigns/{campaign_id}"
            members = [r for r in list_cycle_records(root) if r.get("campaign_id") == campaign_id]
            for member in members:
                if (member.get("deleted_at") or not _closed_record(member) or member["cycle_id"] in seen
                        or (member.get("relocation") or {}).get("artifact_root")):
                    continue
                _tombstone_cycle_locked(root, member, where=_last_known_path(root, member, published.get(member["cycle_id"], "")),
                                        command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
                                        digest=member.get("manifest_digest"))
                gone_cycles.append(member["cycle_id"])
            runtime = campaign_runtime_record(root, campaign_id) or {
                "schema_version": 1, "contract": CONTRACT, "campaign_id": campaign_id}
            runtime.update(last_path=last_path, deleted_at=_rfc3339(now),
                           cycles=[m["cycle_id"] for m in members])
            _write_campaign_runtime_record(root, runtime)
            _campaign_lines_locked(root, campaign_id, [_command_line(
                command="reconcile", stamp=stamp, target_type="campaign", target_id=campaign_id,
                target_path=last_path, operation="delete", field="state",
                before={"value": {"manifest_digest": None, "path": last_path}}, after={"value": None},
                reason="reconcile", now=now, by=by)])
            stale.append(campaign_id)
            result["campaign_gone"].append(campaign_id)
        if gone_cycles:
            _retire_rows(root, gone_cycles)
            for cycle_id in gone_cycles:
                _flush_cycle_pending_locked(root, cycle_id)
            reparented, touched = _reparent_children_locked(
                root, gone_cycles, command="reconcile", stamp=stamp, reason="reconcile", now=now, by=by,
                folders=scan.cycles)
            stale += touched
            if reparented:
                result["reparented"] = reparented
        if stale:
            artifact_locator.update_indexes(root, list(dict.fromkeys(stale)))
    finally:
        if lock_fd is not None:
            artifact_admission._release_lock(root, lock_fd)
    for campaign_id, cycle_ids in left.items():
        _drop_group_membership(root, campaign_id, cycle_ids)
    return result


# ---------------------------------------------------------------------------
# write policy (used by hooks and writers)
# ---------------------------------------------------------------------------


def _relative(root: Path, target: Path) -> Optional[str]:
    root = Path(root).resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Resolve the deepest existing ancestor so a not-yet-created target still
    # normalizes; the leaf is appended unchanged.
    probe = candidate
    tail: List[str] = []
    while not probe.exists() and probe.parent != probe:
        tail.insert(0, probe.name)
        probe = probe.parent
    resolved = probe.resolve()
    for part in tail:
        resolved = resolved / part
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _quick_refine_write_gate(root: Path, target: Path, route=None) -> None:
    relative = _relative(Path(root).resolve(), Path(target))
    if relative is None:
        return
    parts = relative.split("/")
    if parts[:1] == ["campaigns"]:
        index = 4 if len(parts) > 2 and parts[2] == "cycles" else 3
        if len(parts) <= index or parts[index] != "artifacts":
            return
        parts = parts[index + 1:]
    if len(parts) < 2 or parts[0] not in {"documents", "research"}:
        return
    if route is None:
        path = os.environ.get("AGENT_ROUTE_FILE") or os.environ.get("AGENT_OWNER_ROUTE_FILE")
        if not path:
            return  # The route/material guard independently requires a binding.
        route = _read_json(Path(path))
        if not isinstance(route, dict):
            raise ProducerError("inline-gate-route-unreadable")
    if route.get("capability") != "autopilot-refine":
        return
    scoped = (type(route.get("entry_scope_contract_version")) is int
              and route.get("entry_scope_contract_version") == 1
              and route.get("entry_execution_scope") in {"complete", "report"})
    if scoped and route.get("entry_execution_scope") == "report":
        raise ProducerError("legacy-top-level-write-denied", "report scope is read-only")
    if scoped and route.get("entry_execution_scope") == "complete":
        return
    if "_internal" in parts:
        return
    if route.get("effective_intensity") != "quick":
        return
    node = next((n for n in route.get("nodes", []) if n.get("id") == "one-shot"), {})
    if node.get("inline_human_gates") != ["preview-disposition"]:
        raise ProducerError("inline-gate-binding-missing")
    import workflow_state as WS
    try:
        WS.require_inline_gate_release(route, node, jobs=os.environ.get("AGENT_DISPATCH_JOBS") or None)
    except (WS.WorkflowStateError, OSError, ValueError) as exc:
        raise ProducerError("quick-preview-approval-required", str(exc)) from exc


def require_cycle_output(
    root: Path, target: Path, *, cycle_id: Optional[str] = None, route_id: Optional[str] = None,
) -> Optional[Path]:
    """Bind writes and completion evidence to the producer's issued cycle.

    Route lookup recovers omitted environment context from producer records;
    directory names, recency and a caller-supplied output path are not
    authority. When the sealed route file for `route_id` is readable, lookup
    and write admission both go through the lineage-aware D-120 path
    (`route_cycle_for` + `cycle_route_admission`), so a continuation may write
    the cycle its lineage opened. Only a genuinely missing canonical route
    file permits the legacy exact begin-route match, including an explicitly
    selected legacy cycle. Existing but unreadable or malformed proof refuses.
    """
    record = read_cycle_record(root, cycle_id) if cycle_id else None
    if cycle_id and record is None:
        raise ProducerError("cycle-unknown", cycle_id)
    lineage_checked = False
    route = None
    if route_id:
        if not isinstance(route_id, str) or not _ROUTE_ID_RE.fullmatch(route_id):
            raise ProducerError("route-lineage-unverified", f"route={route_id}")
        route_path = route_lineage.canonical_route_path(root, route_id)
        try:
            route_stat = route_path.lstat()
        except FileNotFoundError:
            route_stat = None
        except OSError as exc:
            raise ProducerError("route-lineage-unverified", f"route-unreadable={route_id}") from exc
        if (route_stat is not None and (not stat.S_ISREG(route_stat.st_mode)
                or route_path.is_symlink() or route_path.resolve() != route_path)):
            raise ProducerError("route-lineage-unverified", f"route-kind={route_id}")
        route = _read_json(route_path) if route_stat is not None else None
        if isinstance(route, dict) and route.get("route_id") == route_id:
            if record is None:
                record = route_cycle_for(root, route)
            if record is not None:
                admission = cycle_route_admission(root, record, route)
                if not admission.allow:
                    raise ProducerError(admission.reason, admission.detail)
                if cycle_id:
                    # An explicit selector must not bypass the shared lookup's
                    # refusal of multiple open cycles in the same lineage.
                    selected = route_cycle_for(root, route)
                    if selected is None or selected.get("cycle_id") != record.get("cycle_id"):
                        raise ProducerError("cycle-route-binding-mismatch", f"cycle={cycle_id} route={route_id}")
                lineage_checked = True
        elif route_stat is not None:
            # Existing but malformed proof is never a legacy missing route.
            raise ProducerError("route-lineage-unverified", f"route={route_id}")
        elif record is None:
            # Legacy records can lack a canonical route. Preserve only their
            # original exact begin-route match; continuation still needs proof.
            candidates = list_cycle_records(root, route_ids={route_id})
            opened = [item for item in candidates if item.get("state") == "open"]
            candidates = opened or candidates
            if len(candidates) > 1:
                raise ProducerError("route-cycle-binding-ambiguous", route_id)
            record = candidates[0] if candidates else None
    if record is None:
        return None
    if route_id and not lineage_checked and record.get("route_id") != route_id:
        raise ProducerError("cycle-route-binding-mismatch", f"cycle={record['cycle_id']} route={route_id}")
    output = cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
    try:
        Path(target).resolve().relative_to(output.resolve())
    except ValueError as exc:
        raise ProducerError("artifact-outside-bound-cycle", f"cycle={record['cycle_id']} output_dir={output}") from exc
    return output


def check_write(root: Path, target: Path) -> Dict[str, Any]:
    """Classify one prospective write under the artifact root.

    Returns {verdict: allow|deny, reason, layout, cutover, bucket, cycle_id}.
    """
    root = Path(root).resolve()
    rel = _relative(root, Path(target))
    active = is_active(root)
    base = {"cutover": "active" if active else "inactive", "target": str(target)}
    try:
        _quick_refine_write_gate(root, target)
    except ProducerError as exc:
        return {**base, "verdict": "deny", "reason": exc.code, "detail": exc.detail, "layout": "inline-gate"}
    try:
        _authorize_active_cleanup(root, "partial-report", Path(target), None)
    except ProducerError as exc:
        return {**base, "verdict": "deny", "reason": exc.code, "layout": "cleanup"}
    if rel is None:
        return {**base, "verdict": "allow", "reason": "outside-artifact-root", "layout": None}
    parts = rel.split("/")
    top = parts[0]
    if top.startswith(".") or top == "_scratch":
        return {**base, "verdict": "allow", "reason": "runtime-owned", "layout": "runtime"}
    if top == "shared":
        return {**base, "verdict": "deny", "reason": "shared-revision-immutable", "layout": "shared"}
    if top == "campaigns":
        try:
            locator_mapping, _rows = artifact_locator.scan_index(root)
            target_resolved = Path(target).resolve(strict=False)
            campaign_parts = rel.split("/")
            if len(campaign_parts) >= 3:
                cycle_relative = "/".join(campaign_parts[:3])
                cycle_id = next((identifier for identifier, path in locator_mapping.items()
                                 if path == cycle_relative), None)
                cycle_record = read_cycle_record(root, cycle_id) if cycle_id else None
                if cycle_record:
                    import inline_finish
                    pending = inline_finish.pending_for_cycle(root, cycle_id)
                    if pending and pending.get("state") != "finished":
                        return {**base, "verdict":"deny", "reason":"finish-in-progress",
                                "layout":"cycle", "cycle_id":cycle_id}
        except ProducerError as exc:
            return {**base, "verdict":"deny", "reason":exc.code, "detail":exc.detail, "layout":"cycle"}
        except (OSError, ValueError) as exc:
            return {**base, "verdict":"deny", "reason":"finish-state-unreadable", "detail":str(exc), "layout":"cycle"}
        try:
            require_cycle_output(
                root, target, cycle_id=os.environ.get("AGENT_ARTIFACT_CYCLE_ID"),
                # A node's active route is its write authority. The enclosing
                # owner must not mask a foreign or invalid child route.
                route_id=os.environ.get("AGENT_ROUTE_ID") or os.environ.get("AGENT_OWNER_ROUTE_ID"),
            )
        except ProducerError as exc:
            return {**base, "verdict": "deny", "reason": exc.code, "detail": exc.detail, "layout": "cycle"}
        legacy = len(parts) >= 5 and parts[2] == "cycles"
        readable = len(parts) >= 4 and parts[2] != "cycles"
        if not legacy and not readable:
            return {**base, "verdict": "deny", "reason": "campaign-record-machine-managed", "layout": "cycle"}
        artifacts_index = 4 if legacy else 3
        cycle_path = root.joinpath(*parts[:artifacts_index])
        campaign_path = root / "campaigns" / parts[1]
        campaign = _read_json(campaign_path / "campaign.json") or {}
        campaign_id = campaign.get("campaign_id")
        cycle_id = None
        record = None
        try:
            locator_mapping, _rows = artifact_locator.scan_index(root)
            cycle_relative = cycle_path.resolve().relative_to(root).as_posix()
            cycle_id = next(
                (identifier for identifier, path in locator_mapping.items() if path == cycle_relative),
                None,
            )
        except (artifact_locator.LocatorError, OSError, RuntimeError, ValueError):
            cycle_id = None
        if isinstance(cycle_id, str):
            record = read_cycle_record(root, cycle_id)
        manifest = _read_json(cycle_path / "manifest.json")
        manifest_cycle = manifest.get("cycle") if isinstance(manifest, dict) else None
        if cycle_id is None and isinstance(manifest_cycle, dict):
            cycle_id = manifest_cycle.get("cycle_id")
        if len(parts) <= artifacts_index or parts[artifacts_index] != "artifacts":
            return {**base, "verdict": "deny", "reason": "outside-cycle-artifacts", "layout": "cycle",
                    "cycle_id": cycle_id}
        try:
            observed = os.lstat(target)
            node_kind = ("symlink" if stat.S_ISLNK(observed.st_mode) else
                         "regular" if stat.S_ISREG(observed.st_mode) else
                         "directory" if stat.S_ISDIR(observed.st_mode) else "special")
        except FileNotFoundError:
            node_kind = "missing"
        except OSError:
            node_kind = "special"
        classification = artifact_manifest.classify_artifact_path(
            str(root), campaign_path.relative_to(root).as_posix(),
            cycle_path.relative_to(root).as_posix(), "payload", rel, node_kind,
            prospective=True,
        )
        if not classification.allowed:
            reason = classification.reason or "outside-cycle-artifacts"
            return {**base, "verdict": "deny", "reason": reason,
                    "layout": "cycle", "cycle_id": cycle_id}
        if record is None:
            return {**base, "verdict": "deny", "reason": "cycle-unknown", "layout": "cycle", "cycle_id": cycle_id}
        # §45 D-123: a closed cycle takes writes like an open one; only where the
        # path is (above) and which cycle owns it (here) decide.
        bucket = parts[artifacts_index + 1] if len(parts) > artifacts_index + 2 else None
        return {**base, "verdict": "allow", "reason": "open-cycle-artifacts", "layout": "cycle",
                "cycle_id": cycle_id, "campaign_id": campaign_id, "bucket": bucket,
                "output_dir": str(cycle_path / "artifacts")}
    if active:
        denial = {**base, "verdict": "deny", "reason": "legacy-top-level-write-denied", "layout": "legacy",
                  "bucket": top, "hint": LEGACY_WRITE_HINT}
        # Item 7: name where the caller's own cycle actually expects this write,
        # so a stage worker's error names its fix rather than just the refusal.
        # The reason token above stays exactly what it was (D-86: the fleet
        # cutover gate compares it verbatim).
        output_dir = os.environ.get("AGENT_ARTIFACT_OUTPUT_DIR")
        if output_dir:
            denial["expected_output_dir"] = output_dir
        return denial
    klass = classify_root(root)
    if klass["state"] == "malformed":
        # Same reason string as begin(). A damaged cutover record does not
        # slip out through an unmarked legacy allow (D-74: no unmarked allow
        # on any of the three surfaces).
        return {**base, "verdict": "deny", "reason": "cutover-record-malformed",
                "layout": "legacy", "bucket": top, "detail": klass["reason"]}
    fallback = legacy_fallback_state(root, classification=klass)
    if _fallback_blocks(fallback):
        return {**base, "verdict": "deny", "reason": "cutover-inactive-fallback-denied",
                "layout": "legacy", "bucket": top, "legacy_fallback": fallback}
    result = {**base, "verdict": "allow", "reason": "legacy-compat-window", "layout": "legacy", "bucket": top}
    if fallback is not None:
        result["legacy_fallback"] = fallback
    return result


def cycle_bucket(root: Path, target: Path) -> Optional[Tuple[str, str]]:
    """Return (bucket, cycle_id) for a path inside a cycle's artifacts."""
    rel = _relative(Path(root), Path(target))
    if rel is None:
        return None
    parts = rel.split("/")
    if parts[0] != "campaigns":
        return None
    legacy = len(parts) >= 7 and parts[2] == "cycles" and parts[4] == "artifacts"
    readable = len(parts) >= 6 and parts[2] != "cycles" and parts[3] == "artifacts"
    if not legacy and not readable:
        return None
    bucket_index = 5 if legacy else 4
    artifacts_index = 4 if legacy else 3
    cycle_path = Path(root).resolve().joinpath(*parts[:artifacts_index])
    try:
        mapping, _rows = artifact_locator.scan_index(Path(root))
        relative = cycle_path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (artifact_locator.LocatorError, OSError, RuntimeError, ValueError):
        return None
    for cycle_id, path in mapping.items():
        if path != relative:
            continue
        if read_cycle_record(root, cycle_id):
            return parts[bucket_index], cycle_id
    return None


def resolve_output_dir(root: Path, bucket: str, *, cycle_dir_hint: Optional[str] = None) -> Tuple[Path, str]:
    """Where a writer must place `<bucket>/...` output: cycle layout or legacy."""
    root = Path(root).resolve()
    _authorize_active_cleanup(root, "partial-report", root, os.environ.get("AGENT_ARTIFACT_CYCLE_ID"))
    hint = cycle_dir_hint or os.environ.get("AGENT_ARTIFACT_CYCLE_DIR")
    if hint:
        directory = Path(hint)
        verdict = check_write(root, directory / "artifacts" / bucket / "probe")
        if verdict["verdict"] != "allow":
            raise ProducerError(verdict["reason"], str(directory))
        return directory / "artifacts" / bucket, "cycle"
    if is_active(root):
        raise ProducerError("legacy-top-level-write-denied", f"{bucket}: {LEGACY_WRITE_HINT}")
    klass = classify_root(root)
    if klass["state"] == "malformed":
        raise ProducerError("cutover-record-malformed", klass["reason"] or bucket)
    fallback = legacy_fallback_state(root, classification=klass)
    if _fallback_blocks(fallback):
        raise ProducerError("cutover-inactive-fallback-denied", bucket)
    return root / bucket, "legacy"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _route_autoclose(root: Path, trigger: str, campaign: Optional[str] = None) -> None:
    """Close routes nobody works on before campaign bookkeeping reads them
    (route_autoclose.py).  Bookkeeping only: it never fails the command.
    `campaign` limits `campaign-close` bookkeeping to that campaign."""
    try:
        import artifact_cutover
        import route_autoclose
        campaign_id = None
        scope = {}
        if campaign is not None:
            path = artifact_campaign.campaign_path(root, campaign)
            record, _raw = artifact_campaign.read_json(root, path)
            campaign_id = record.get("campaign_id")
            scope = {"scope_campaign_id": campaign_id, "scope_dir": path.parent,
                     "scope_key": record.get("key")}
        route_autoclose.report(route_autoclose.sweep(
            root, api=artifact_cutover._route_module(), trigger=trigger,
            campaign_id=campaign_id, **scope))
    except Exception as exc:  # noqa: BLE001
        print(f"route_autoclose error={type(exc).__name__}", file=sys.stderr)


def _print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True))


def _checkpoint_cli(args: argparse.Namespace) -> Dict[str, Any]:
    route_file: Optional[Path] = None
    root_value = args.artifact_root or os.environ.get("AGENT_ARTIFACT_ROOT") or ""
    if args.route:
        route_file = Path(args.route)
        if not root_value:
            raw = _read_json(route_file)
            root_value = str((raw or {}).get("artifact_root") or "")
    cycle_id = args.cycle or (None if args.route else os.environ.get("AGENT_ARTIFACT_CYCLE_ID") or None)
    if not root_value:
        raise ProducerError("checkpoint-target-required", "--artifact-root or $AGENT_ARTIFACT_ROOT")
    root = Path(root_value)
    if route_file is not None:
        route_file = resolve_route_argument(root, route_file)
    return checkpoint(root, cycle_id=cycle_id, route_file=route_file, trigger=args.trigger)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))

    p = sub.add_parser("activate")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--repository-id", required=True)
    p.add_argument("--artifact-root-id", required=True)
    p.add_argument("--w7-campaign-id")
    p.add_argument("--w7-cycle-id")
    p.add_argument("--w7-handoff-sha256")
    p.add_argument("--w7-map-sha256")
    p.add_argument("--w7-shared", action="append", default=[], help="kind=ref_id:rrev_id")
    p.add_argument("--approval-receipt-sha256")

    p = sub.add_parser("status")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("campaign-list", help="summarize the root's campaigns (keys, titles, cycle counts)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--all-states", action="store_true", help="include satisfied/superseded campaigns")

    p = sub.add_parser("campaign-export", help="pure read of every campaign's current state (artifact-campaign-current/v1)")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("cycle-binding-backfill",
                       help="add started_on to .cycle.json bindings written before the field existed")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--apply", action="store_true", help="write the bindings (default: dry run)")

    p = sub.add_parser("cycle-time-recovery",
                       help="recover migrated cycles' start times from the retirement backup's original mtimes")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--backup-store", help="retirement store (default: $XDG_STATE_HOME/hearting/artifact-retirement)")
    p.add_argument("--apply", action="store_true", help="write recovered_started_on into records (default: dry run)")

    p = sub.add_parser("cycle-display-titles-backfill",
                       help="declare cycle display titles for Cairn from sealed cycles (default: dry run)")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--apply", action="store_true", help="write the declaration (default: dry run)")
    p.add_argument("--report", help="write a markdown report to this path")
    p.add_argument("--out", help="write the candidate declaration bytes to this path (outside the artifact root)")
    p.add_argument("--reader-dir", help="Cairn app_dir to gate --apply against (default: ~/.config/cairn-sync/config.json)")
    p.add_argument("--expect-post-digest", help="refuse --apply unless the computed post-digest matches")
    p.add_argument("--restore-journal", help="restore the declaration from a prior --apply's journal")

    for command in ("campaign-status", "campaign-close", "campaign-reopen", "campaign-recover"):
        p = sub.add_parser(command, help="inspect, close, reopen, or recover a campaign")
        p.add_argument("--artifact-root", required=True)
        p.add_argument("--campaign", required=True, help="campaign ID or campaign.json path")
        if command in {"campaign-close", "campaign-reopen"}:
            p.add_argument("--reason")

    p = sub.add_parser("cycle-move", help="move cycles, merge campaigns or attach logs (keeps IDs)")
    p.add_argument("--artifact-root", required=True)
    selector = p.add_mutually_exclusive_group(required=True)
    selector.add_argument("--cycle")
    selector.add_argument("--source-campaign")
    p.add_argument("--target-artifact-root")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--attach-logs", action="append", default=[])
    p.add_argument("--campaign", help="target campaign: its ID or its key")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--parent", help="the cycle this one follows")
    group.add_argument("--no-parent", action="store_true", help="take the parent link off")
    p.add_argument("--reason")

    p = sub.add_parser("cycle-mark", help="mark a cycle discarded or superseded, take the mark off, "
                                          "or name the document that stands for it")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--discard", action="store_true")
    modes.add_argument("--superseded-by", help="comma-separated IDs of the cycles that replace this one")
    modes.add_argument("--clear", action="store_true", help="take the mark off")
    modes.add_argument("--primary", help="cycle-relative path (artifacts/...) of the document that stands for the cycle")
    p.add_argument("--reason")

    p = sub.add_parser("delete", help="delete a cycle or a whole campaign (the records and preserved copies stay)")
    p.add_argument("--artifact-root", required=True)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--cycle")
    target.add_argument("--campaign", help="campaign ID or key")
    p.add_argument("--reason")

    p = sub.add_parser("begin")
    p.add_argument("--artifact-root", required=True)
    p.add_argument(
        "--route",
        required=True,
        help="path to the route file, or a bare route id resolved under the artifact root",
    )
    p.add_argument("--node", default=None)
    p.add_argument("--capability", required=True)
    p.add_argument("--intensity", required=True)
    p.add_argument("--campaign", help="existing campaign id to add this cycle to")
    p.add_argument("--campaign-key",
                   help="the work stream this cycle belongs to; reuses the active "
                        "campaign holding that key. Defaults to the sealed route's key. "
                        "With no campaign/key/parent selection, uses the root's "
                        "_unassigned campaign and reports degraded=true. Size: a stream with a "
                        "one-sentence closing condition — not a project name, not a one-cycle task")
    p.add_argument("--title")
    p.add_argument("--goal")
    p.add_argument("--parent-cycle",
                   help="open or sealed predecessor; inherits its campaign and records "
                        "causality, never input or completion approval")
    p.add_argument("--workflow-group-id", help="explicit existing group for this campaign; "
                   "the same-campaign producer context is inherited when omitted")
    p.add_argument("--workflow-stage-label", help="display label for an explicitly grouped cycle")
    p.add_argument("--require-cycle", action="store_true")
    p.add_argument("--shared-reference", action="append", default=[],
                   help="<kind>:<ref>:<rrev>[:<content_digest>], repeatable")
    p.add_argument("--env-file", help="write KEY=VALUE lines for the producer environment")

    p = sub.add_parser("finalize")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--state", default="completed", choices=["completed", "abandoned"])
    p.add_argument("--primary", help="primary artifact as a cycle-relative locator "
                   "(artifacts/<file> or artifacts/<folder>/<file>); an absolute path inside this cycle's artifacts/ is accepted")
    p.add_argument("--publication", default="not-offered")
    p.add_argument("--allow-open-route", action="store_true")
    p.add_argument("--adopt-root-output", action="append", default=[])
    p.add_argument("--abandon-reason", choices=sorted(ABANDON_REASONS))
    p.add_argument("--force-abandon-ignoring-lease", action="store_true")

    p = sub.add_parser("checkpoint",
                       help="publish an open cycle's interim manifest (the sealed schema with "
                            "cycle.state=open) for readers such as Cairn; rate-limited per cycle")
    p.add_argument("--artifact-root",
                   help="default: $AGENT_ARTIFACT_ROOT, else the route file's artifact_root")
    p.add_argument("--cycle", help="default: $AGENT_ARTIFACT_CYCLE_ID when --route is absent")
    p.add_argument("--route", help="route file or bare route id; selects the route's one open cycle")
    p.add_argument("--trigger", default="explicit", choices=CHECKPOINT_TRIGGERS)

    p = sub.add_parser("review-lease")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--attempt")
    p.add_argument("--deadline-seconds", type=float, default=900.0)
    p.add_argument("operation", choices=["acquire", "release", "status"])

    p = sub.add_parser("admit-shared")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--reference")
    p.add_argument("--key")
    p.add_argument("--title")
    p.add_argument("--promote-research", action="store_true")
    p.add_argument("--promotion-evidence")
    p.add_argument("--drop-component", action="append", default=[], metavar="NAME",
                   help="D-87 (b): drop this top-level component from the reference "
                        "(repeatable); the only way to shrink the component set")
    p.add_argument("--drop-reason", help="reason recorded with --drop-component")
    p.add_argument("--base-revision", help="actual base revision of an unseeded spec, or none for initial publication")
    p.add_argument("--new-reference", action="store_true",
                   help="allow a second reference of a canonical-singular kind (spec) when neither --reference nor --key matches")

    p = sub.add_parser("check-components")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--from-revision", help="first revision of the inspected window")
    p.add_argument("--to-revision", help="last revision of the inspected window")

    p = sub.add_parser("recover")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("check-write")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--file", required=True)

    p = sub.add_parser("resolve-output")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--cycle-dir")

    args = parser.parse_args(argv)
    try:
        if args.command not in {"check-write"}:
            dispatch_terminal_commit.require_current_cleanup("producer-" + args.command)
        if args.command == "checkpoint":
            _print(_checkpoint_cli(args))
            return OK
        root = Path(args.artifact_root)
        if args.command == "activate":
            w7: Dict[str, Any] = {}
            if args.w7_campaign_id:
                w7["campaign_id"] = args.w7_campaign_id
            if args.w7_cycle_id:
                w7["cycle_id"] = args.w7_cycle_id
            if args.w7_handoff_sha256:
                w7["handoff_sha256"] = args.w7_handoff_sha256
            if args.w7_map_sha256:
                w7["compatibility_map_sha256"] = args.w7_map_sha256
            shared: Dict[str, Any] = {}
            for row in args.w7_shared:
                kind, _, ids = row.partition("=")
                ref, _, rrev = ids.partition(":")
                shared[kind] = {"shared_reference_id": ref, "shared_reference_revision_id": rrev}
            if shared:
                w7["shared"] = shared
            result = activate(root, repository_id=args.repository_id, artifact_root_id=args.artifact_root_id,
                              w7=w7, approval_receipt_sha256=args.approval_receipt_sha256)
        elif args.command == "status":
            result = status(root)
        elif args.command == "campaign-list":
            rows = list_campaign_summaries(root, active_only=not args.all_states)
            result = {"status": "ok", "artifact_root": str(Path(root).resolve()), "campaigns": rows}
        elif args.command == "campaign-export":
            # Pure read: captured bytes, no writes or locks; only an
            # OS-unreadable root is exit 65, never a content verdict.
            try:
                result = artifact_campaign.export_current(root)
            except OSError as exc:
                _print({"status": "blocked", "code": "root-unreadable", "detail": str(exc)[:200]})
                return 65
        elif args.command == "cycle-binding-backfill":
            result = backfill_cycle_bindings(root, apply=args.apply)
        elif args.command == "cycle-time-recovery":
            result = recover_cycle_times(root, apply=args.apply,
                                         backup_store=Path(args.backup_store) if args.backup_store else None)
        elif args.command == "cycle-display-titles-backfill":
            if args.restore_journal:
                result = artifact_cycle_titles.restore_backfill(root, Path(args.restore_journal))
            else:
                result = artifact_cycle_titles.backfill(
                    root, apply=args.apply,
                    report_path=Path(args.report) if args.report else None,
                    out_path=Path(args.out) if args.out else None,
                    reader_dir=Path(args.reader_dir) if args.reader_dir else None,
                    expect_post_digest=args.expect_post_digest,
                )
            _print(result)
            return BLOCKED if str(result.get("status", "")).startswith("refused") else OK
        elif args.command == "campaign-status":
            import artifact_cross_root_move as cross_move
            try:
                historical_path, historical = cross_move._campaign(
                    root, args.campaign,
                    read_record=lambda path: artifact_campaign.read_json(root, path)[0])
            except ProducerError:
                historical = {}
            if historical.get("relocation"):
                result = {"status": historical["state"], "state": historical["state"],
                          "campaign_id": historical["campaign_id"], "canonical": historical["relocation"]}
            else:
                # Pure read: no route sweep, no reconcile, no history flush.
                # Writer paths (`campaign-close`, `compose`) keep the sweep.
                result = artifact_campaign.status(root, args.campaign)
        elif args.command == "campaign-close":
            _route_autoclose(root, "campaign-close", campaign=args.campaign)
            reconcile_root(root)
            result = artifact_campaign.close(root, args.campaign, reason=args.reason)
        elif args.command == "campaign-reopen":
            result = artifact_campaign.reopen(root, args.campaign, reason=args.reason)
        elif args.command == "campaign-recover":
            result = artifact_campaign.recover(root, args.campaign)
        elif args.command == "cycle-move":
            result = cycle_move(root, args.cycle, campaign=args.campaign, parent=args.parent,
                                no_parent=args.no_parent, reason=args.reason, target_artifact_root=args.target_artifact_root,
                                source_campaign=args.source_campaign, attach_logs=args.attach_logs, dry_run=args.dry_run)
        elif args.command == "cycle-mark":
            result = cycle_mark(
                root, args.cycle, discard=args.discard,
                superseded_by=[v.strip() for v in args.superseded_by.split(",") if v.strip()]
                if args.superseded_by is not None else None,
                clear=args.clear, primary=args.primary, reason=args.reason)
        elif args.command == "delete":
            result = (delete_cycle(root, args.cycle, reason=args.reason) if args.cycle
                      else delete_campaign(root, args.campaign, reason=args.reason))
        elif args.command == "begin":
            pins: List[Dict[str, Any]] = []
            for row in args.shared_reference:
                parts = row.split(":", 3)
                if len(parts) < 3:
                    raise ProducerError("shared-reference-pin-invalid", row)
                kind, ref_id, rrev_id = parts[0], parts[1], parts[2]
                pin: Dict[str, Any] = {
                    "kind": kind, "shared_reference_id": ref_id, "shared_reference_revision_id": rrev_id,
                }
                if len(parts) > 3:
                    pin["content_digest"] = parts[3]
                pins.append(pin)
            result = begin(root, route_file=Path(args.route), capability=args.capability,
                           intensity=args.intensity, node_id=args.node, campaign_id=args.campaign,
                           campaign_key=args.campaign_key, title=args.title, goal=args.goal,
                           parent_cycle_id=args.parent_cycle,
                           workflow_group_id=args.workflow_group_id,
                           workflow_stage_label=args.workflow_stage_label,
                           require_cycle=args.require_cycle,
                           shared_reference_pins=pins or None)
            if args.env_file:
                lines = "".join(f"{k}={v}\n" for k, v in result.get("env", {}).items())
                Path(args.env_file).write_text(lines, encoding="utf-8")
        elif args.command == "finalize":
            result = finalize(root, cycle_id=args.cycle, state=args.state, primary=args.primary,
                              publication=args.publication, allow_open_route=args.allow_open_route,
                              adopt_root_outputs=args.adopt_root_output,
                              abandon_reason=args.abandon_reason,
                              force_abandon_ignoring_lease=args.force_abandon_ignoring_lease)
            if result.get("warning"):
                print(result["warning"], file=sys.stderr)
            record = read_cycle_record(root, args.cycle)
            if record is not None and record.get("capability") == "autopilot-spec":
                # Legacy normal completion/recovery has no owner envelope.
                # Finalize has released its lock before existing admission runs.
                result["shared_publication"] = completed_spec_publication(root, cycle_id=args.cycle, settle=True)
        elif args.command == "review-lease":
            if args.operation == "acquire":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_acquire(root, cycle_id=args.cycle, attempt_id=args.attempt,
                                              deadline_seconds=args.deadline_seconds)
            elif args.operation == "release":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_release(root, cycle_id=args.cycle, attempt_id=args.attempt)
            else:
                result = review_lease_status(root, cycle_id=args.cycle, attempt_id=args.attempt)
        elif args.command == "admit-shared":
            result = admit_shared(root, cycle_id=args.cycle, kind=args.kind, source=args.source,
                                  reference_id=args.reference, key=args.key, title=args.title,
                                  promote_research=args.promote_research,
                                  promotion_evidence=args.promotion_evidence,
                                  drop_components=args.drop_component,
                                  drop_reason=args.drop_reason,
                                  allow_new_reference=args.new_reference, base_revision=args.base_revision)
        elif args.command == "check-components":
            result = check_component_sets(root, args.kind, args.reference,
                                          from_revision=args.from_revision,
                                          to_revision=args.to_revision)
            _print(result)
            return OK if result["violations"] == 0 else BLOCKED
        elif args.command == "recover":
            result = recover(root)
        elif args.command == "check-write":
            result = check_write(root, Path(args.file))
            _print(result)
            return OK if result["verdict"] == "allow" else BLOCKED
        elif args.command == "resolve-output":
            directory, layout = resolve_output_dir(root, args.bucket, cycle_dir_hint=args.cycle_dir)
            result = {"status": "ok", "output_dir": str(directory), "layout": layout}
            if layout == "legacy":
                fallback = legacy_fallback_state(root)
                if fallback is not None:
                    result["legacy_fallback"] = fallback
        else:  # pragma: no cover
            parser.error("unknown command")
            return USAGE
    except ProducerError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except artifact_cycle_titles.CycleTitlesError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except artifact_admission.AdmissionBusy as exc:
        _print({"status": "blocked", "reason": "admission-busy", "detail": str(exc)})
        return BLOCKED
    except artifact_admission.AdmissionRecoveryRequired as exc:
        _print({"status": "blocked", "reason": "recovery-required", "detail": str(exc)})
        return BLOCKED
    except (artifact_lifecycle.LifecycleError, artifact_campaign.CampaignError) as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except (OSError, ValueError) as exc:
        _print({"status": "blocked", "reason": "request-invalid", "detail": str(exc)})
        return BLOCKED
    _print(result)
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
