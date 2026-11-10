"""Artifact producer: constants, file primitives, record paths and records.

The bottom layer of the producer modules: the layout constants, atomic and
exclusive writes, cycle/campaign/campaign-runtime records, cutover state and
root classification, and the cycle-record index observation. It imports no
other producer module.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import artifact_admission
import artifact_payload
import artifact_identity
import artifact_index
import artifact_lifecycle
import artifact_locator
import artifact_manifest
import artifact_directory_bundle
import artifact_campaign
import route_lineage
import directory_record_index
from runtime_observation import ObservationContext, stable_read_bytes


_HISTORICAL_OBSERVATIONS = ObservationContext(
    max_entries=48, max_weight=32 * 1024 * 1024
)
_ARTIFACT_INDEX_PASS: contextvars.ContextVar = contextvars.ContextVar(
    "artifact_index_observation_scope", default=None
)


@contextlib.contextmanager
def artifact_index_observation_scope():
    """Pin each root's first immutable index projection for one observer pass."""
    current = _ARTIFACT_INDEX_PASS.get()
    if current is not None:
        yield
        return
    token = _ARTIFACT_INDEX_PASS.set({})
    try:
        yield
    finally:
        _ARTIFACT_INDEX_PASS.reset(token)
# Large parsed documents must not evict the small consistency facts and
# index projections that unchanged historical owners reuse.
_HISTORICAL_PROJECTIONS = ObservationContext(
    max_entries=8192, max_weight=8 * 1024 * 1024
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
# CORE §3 derived read entrance (`utilities/artifact_entrance.py`). These are not
# canonical records: admission, census and residue never take them as input, and
# a same-named file without the generator's first-line marker is left to its
# owner (never overwritten or moved).
ENTRANCE_NAMES = ("README.md", "index.html", "all-campaigns.md", "all-campaigns.html")
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
# A project status document (one page: a `**한 줄**:` line, `## 결정 필요`, `## 다음`).
# Directly under a cycle's `artifacts/` it is that cycle's automatic primary, so Cairn
# pins it as the project's status row and Fleet's project line reads the same file.
STATUS_DOCUMENT_NAMES = ("현황.md", "STATUS.md")
# What a person opens the artifact store for: reports, summaries, the canonical PRD and
# the status document. Their manifest rows carry `audience: "user"`; other rows carry no
# audience, and a reader keeps its own rule for them. Names compare NFC, case-folded.
USER_DOCUMENT_NAMES = frozenset({
    "final_report.md", "final_report.html", "report.md", "report.html", "summary.md",
    "audit-report.md", "현황.md", "status.md",
})
# CORE §3 top-level `C-INT` names that are not a cycle bucket: support material is
# kept in the manifest but is not auto-nominated as a cycle's primary artifact.
SUPPORT_SEGMENTS = frozenset({"_internal", "shards"})
# Folders whose files are work records or copies, never the reader's document itself,
# whatever their name: work logs, verification evidence, snapshots and support material.
RECORD_SEGMENTS = frozenset({"dev_logs", "test_logs", "evidence", "versions"}) | SUPPORT_SEGMENTS
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


def _is_payload_directory(rel: str) -> bool:
    return rel in {"artifacts", "_internal"} or rel.startswith(("artifacts/", "_internal/"))


def _excluded_by_roots(rel: str, roots: Iterable[str]) -> bool:
    return any(rel == root or rel.startswith(root + "/") for root in roots)


def _walk_cycle_files(directory: Path, *, marker_observations: Optional[Dict[str, Any]] = None,
                      excluded_roots: Optional[List[str]] = None,
                      excluded: Optional[List[str]] = None,
                      skip_directories: Sequence[str] = (), read_markers: bool = True) -> List[Path]:
    """Walk cycle files while pruning self-excluded payload directories before descent."""
    observations = marker_observations if marker_observations is not None else {}
    roots = excluded_roots if excluded_roots is not None else []
    excluded_paths = excluded if excluded is not None else []
    skipped = set(skip_directories)
    # A request's execution scopes own their own manifests. Never include a
    # later execution's files in the frame's immutable output inventory.
    import artifact_request
    request_members = set(artifact_request.members(directory.parents[2], directory, strict=False)) if (
        directory.parent.parent.name == "campaigns") else set()
    out: List[Path] = []
    for current, dirs, files in os.walk(str(directory), followlinks=False):
        dirs.sort()
        for name in list(dirs):
            path = Path(current) / name
            rel = path.relative_to(directory).as_posix()
            if path in request_members:
                dirs.remove(name)
                continue
            if os.path.islink(path):
                out.append(path)
                dirs.remove(name)
                continue
            if _is_payload_directory(rel) and (name.startswith(".") or name == "__pycache__"):
                if rel + "/" not in excluded_paths:
                    excluded_paths.append(rel + "/")
                dirs.remove(name)
                continue
            if rel in skipped:
                dirs.remove(name)
                continue
            if _is_payload_directory(rel):
                observation = observations.get(rel)
                if observation is None and read_markers:
                    payload_root = directory / rel.split("/", 1)[0]
                    observation = artifact_payload.observe_directory(path, payload_root=payload_root)
                    observations[rel] = observation
                if observation is not None and observation[0]:
                    if rel not in roots:
                        roots.append(rel)
                    if rel + "/" not in excluded_paths:
                        excluded_paths.append(rel + "/")
                    dirs.remove(name)
        out.extend(Path(current) / name for name in sorted(files))
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
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            digest_value, size = artifact_directory_bundle.copy_file_streaming(entry, dst)
        except artifact_directory_bundle.DirectoryBundleError as exc:
            violations.append(f"{exc.code}:{rel}")
            continue
        rows.append((rel, digest_value, size))
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
    if record and record.get("journal_migration"):
        from artifact_journal_migration import resolve_backup
        return resolve_backup(root, record)
    if record and (record.get("relocation") or {}).get("artifact_root"):
        raise ProducerError("cycle-relocated", str(record["relocation"]))
    if record and record.get("request_binding"):
        import artifact_request
        try:
            # The shared locator follows an explicit move/rename as it does
            # for ordinary cycles. Scans pass the known anchor to avoid a
            # recursive lookup while rebuilding a missing index.
            original = Path(root) / record["request_binding"]["path"] / Path(*artifact_request.EXECUTIONS) / cycle_id
            found = artifact_locator.locate(root, cycle_id, candidates=lambda: [original], heal=False)
            if found is not None:
                return found
            return artifact_request.execution_dir(root, record)
        except (ValueError, artifact_locator.LocatorError) as exc:
            raise ProducerError("request-binding-invalid", str(exc)) from exc
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
    return name.startswith(".") or name in RUNTIME_OWNED_EXACT or name in ENTRANCE_NAMES


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


def _admitted_cycle_rows(root: Path, cycle_id: str) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    """One cycle's admitted `manifests` and `cycles` index rows.

    The small head projection of the current index generation answers first;
    a missing or stale head falls back to the full index (`load_index` keeps
    the parsed generation for the rest of this process), and an index that
    does not parse as one keeps the raw read this function always had.
    """
    head = artifact_admission.load_index_head(root)
    if head is not None:
        return head.manifests.get(cycle_id) or {}, head.cycles.get(cycle_id) or {}
    path = artifact_admission._index_path(root)
    if path.is_file() and not path.is_symlink():
        try:
            index = artifact_admission.load_index(root)
            return index.manifests.get(cycle_id) or {}, index.cycles.get(cycle_id) or {}
        except Exception:  # noqa: BLE001 -- the raw read below decides as before
            pass
    raw = _read_json(path) or {}
    return ((raw.get("manifests") or {}).get(cycle_id) or {},
            (raw.get("cycles") or {}).get(cycle_id) or {})


def read_cycle_record(root: Path, cycle_id: str) -> Optional[Dict[str, Any]]:
    if not artifact_identity.is_well_formed(cycle_id, "cycle"):
        return None
    record = _read_json(cycle_record_path(root, cycle_id))
    if record is not None:
        return record
    # A producer record is a cache. Read the already admitted revision if the
    # cache was removed; this recovers a past result, never a new PASS.
    row, cycle_row = _admitted_cycle_rows(root, cycle_id)
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
    for _seen_at, quiet in list(_QUIET_HISTORY.values()):
        quiet.discard(record["cycle_id"])


def _write_cycle_binding(directory: Path, campaign_id: str, cycle_id: str,
                         *, started_on: Optional[str] = None) -> None:
    marker = Path(directory) / artifact_locator.CYCLE_BINDING
    data = artifact_locator.cycle_binding_bytes(campaign_id, cycle_id, started_on=started_on)
    if marker.is_file() and not marker.is_symlink():
        if marker.read_bytes() != data:
            raise ProducerError("cycle-binding-conflict", str(marker))
        return
    _write_exclusive(marker, data)


@dataclass(frozen=True)
class _ArtifactIndexObservation:
    """Small immutable read projection; admission writers keep their own IndexDocument."""

    artifact_root_id: Optional[str]
    manifests: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]
    cycles: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]
    cycle_ids: tuple[Any, ...]
    projected_cycle_ids: tuple[str, ...]
    matching_cycle_ids: tuple[str, ...]
    routes_valid: bool
    schema_error: str
    weight: int
    route_cycles: tuple[tuple[str, str], ...] = ()

    def manifest_row(self, cycle_id: str):
        low, high = 0, len(self.manifests)
        while low < high:
            middle = (low + high) // 2
            key, values = self.manifests[middle]
            if key < cycle_id:
                low = middle + 1
            elif key > cycle_id:
                high = middle
            else:
                return dict(values)
        return None

    def cycle_row(self, cycle_id: str):
        low, high = 0, len(self.cycles)
        while low < high:
            middle = (low + high) // 2
            key, values = self.cycles[middle]
            if key < cycle_id:
                low = middle + 1
            elif key > cycle_id:
                high = middle
            else:
                return dict(values)
        return None


def _artifact_index_observation(root: Path, *, cycle_id: str | None = None,
                                route_ids=None) -> _ArtifactIndexObservation:
    """Parse one index generation once across different cycle/route queries.

    Stable-id ownership, the bulk of large indexes, is validated but discarded.
    The bounded cache retains only cycle/manifest/route projections. Writers
    still load independent mutable IndexDocuments under their admission lock.
    """
    root = Path(root).resolve()
    path = artifact_admission._index_path(root)
    route_key = None if route_ids is None else tuple(sorted(set(route_ids)))
    key = ("artifact-index-projection", str(root))
    pass_observations = _ARTIFACT_INDEX_PASS.get()

    def load():
        head = artifact_admission.load_index_head(root)
        if head is not None:
            def head_rows(source):
                return tuple(sorted((name, tuple(sorted(value.items())))
                                    for name, value in source.items()
                                    if isinstance(name, str) and isinstance(value, dict)))
            manifest_rows = head_rows(head.manifests)
            cycle_rows = head_rows(head.cycles)
            cycle_ids = tuple(head.cycles)
            projected, route_cycles = set(), set()
            routes_valid = True
            for bucket in head.routes.values():
                if not isinstance(bucket, dict):
                    routes_valid = False
                    break
                for route_id, item in bucket.items():
                    if (not isinstance(route_id, str) or not isinstance(item, dict)
                            or not isinstance(item.get("cycle_id"), str)):
                        routes_valid = False
                        break
                    referenced_cycle = item["cycle_id"]
                    projected.add(referenced_cycle)
                    route_cycles.add((route_id, referenced_cycle))
                if not routes_valid:
                    break
            if not routes_valid:
                projected.clear()
                route_cycles.clear()
            weight = 256
            for collection in (manifest_rows, cycle_rows):
                for name, fields in collection:
                    weight += len(name) + 48
                    weight += sum(len(field) + len(str(value)) + 24 for field, value in fields)
            weight += sum(len(cycle_id) + 48 for cycle_id in projected)
            weight += sum(len(route) + len(cycle) + 64 for route, cycle in route_cycles)
            weight += sum(len(str(value)) + 16 for value in cycle_ids)
            return _ArtifactIndexObservation(
                head.artifact_root_id, manifest_rows, cycle_rows, cycle_ids,
                tuple(sorted(projected)), (), routes_valid, "", weight,
                tuple(sorted(route_cycles)),
            )
        payload = json.loads(stable_read_bytes(path).decode("utf-8"))
        schema_error = ""
        parsed = None
        try:
            parsed = artifact_index._parse_owned(payload)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            schema_error = str(exc)
        if not isinstance(payload, dict):
            payload = {}
        manifest_payload = payload.get("manifests") or {}
        cycle_payload = payload.get("cycles") or {}

        def rows(source):
            if not isinstance(source, dict):
                return ()
            return tuple(sorted((name, tuple(sorted(value.items())))
                                for name, value in source.items()
                                if isinstance(name, str) and isinstance(value, dict)))

        try:
            cycle_ids = tuple(cycle_payload) if cycle_payload else ()
        except TypeError:
            cycle_ids = ()
        projected, route_cycles = set(), set()
        routes_valid = True
        projection = payload.get("routes") or {}
        if not isinstance(projection, dict):
            routes_valid = False
        else:
            for bucket in projection.values():
                if not isinstance(bucket, dict):
                    routes_valid = False
                    break
                for route_id, item in bucket.items():
                    if (not isinstance(route_id, str) or not isinstance(item, dict)
                            or not isinstance(item.get("cycle_id"), str)):
                        routes_valid = False
                        break
                    referenced_cycle = item["cycle_id"]
                    projected.add(referenced_cycle)
                    route_cycles.add((route_id, referenced_cycle))
                if not routes_valid:
                    break
        if not routes_valid:
            projected.clear()
            route_cycles.clear()
        manifest_rows = rows(manifest_payload)
        cycle_rows = rows(cycle_payload)
        root_id = parsed.artifact_root_id if parsed is not None else payload.get("artifact_root_id")
        weight = 256
        for collection in (manifest_rows, cycle_rows):
            for name, fields in collection:
                weight += len(name) + 48
                weight += sum(len(field) + len(str(value)) + 24 for field, value in fields)
        weight += sum(len(cycle_id) + 48 for cycle_id in projected)
        weight += sum(len(route) + len(cycle) + 64 for route, cycle in route_cycles)
        weight += sum(len(str(value)) + 16 for value in cycle_ids)
        return _ArtifactIndexObservation(
            root_id, manifest_rows, cycle_rows, cycle_ids,
            tuple(sorted(projected)), (),
            routes_valid, schema_error, weight, tuple(sorted(route_cycles)),
        )

    size_weight = lambda value: value.weight
    if pass_observations is not None and str(root) in pass_observations:
        observed = pass_observations[str(root)]
        if isinstance(observed, Exception):
            raise observed
    else:
        try:
            observed = _HISTORICAL_OBSERVATIONS.memoize(
                key, [path], load, weight=size_weight
            )
        except Exception as exc:
            if pass_observations is not None:
                pass_observations[str(root)] = exc
            raise
        if pass_observations is not None:
            pass_observations[str(root)] = observed
    if route_key is None:
        return observed
    wanted_routes = set(route_key or ())
    return replace(observed,
        cycle_ids=observed.cycle_ids if cycle_id is None else (),
        matching_cycle_ids=tuple(sorted({cycle for route, cycle in observed.route_cycles
                                        if route in wanted_routes})),
        projected_cycle_ids=observed.projected_cycle_ids if route_key is not None else (),
        routes_valid=observed.routes_valid if route_key is not None else True)


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
    try:
        index = _artifact_index_observation(root, route_ids=route_ids)
    except (OSError, ValueError, UnicodeError):
        index = _ArtifactIndexObservation(None, (), (), (), (), (), False, "", 1)
    if route_ids is None:
        known = {row.get("cycle_id") for row in rows}
        candidates = set(index.cycle_ids) - known
    else:
        # Admitted manifests recover removed producer records. Their existing
        # route projection narrows recovery too; unindexed legacy rows retain
        # the original read-and-filter fallback.
        projected = set(index.projected_cycle_ids)
        matching = set(index.matching_cycle_ids)
        if not index.routes_valid:
            # A damaged optional projection cannot suppress legacy recovery.
            projected, matching = set(), set()
        known = {row.get("cycle_id") for row in rows}
        candidates = (matching | (set(index.cycle_ids) - projected)) - known - {None}
    for cycle_id in sorted(candidates):
        record = read_cycle_record(root, cycle_id)
        if record is not None and (route_ids is None or record.get("route_id") in route_ids):
            rows.append(record)
    return rows


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
            result = {"status": "already-active", "cutover": current, "identity": identity_state}
            from artifact_root_alias import ensure_legacy_alias
            alias = ensure_legacy_alias(root, identity)
            if alias is not None:
                result['root_alias'] = alias
            return result
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
        result = {"status": "activated", "cutover": body, "identity": identity_state}
        from artifact_root_alias import ensure_legacy_alias
        alias = ensure_legacy_alias(root, identity)
        if alias is not None:
            result['root_alias'] = alias
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _rfc3339_to_epoch(value: str) -> float:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


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


# One read pass of a long-lived reader (a runner pass, one completed-publication
# check) shares observations that the pass treats as pinned: immutable shared
# revisions it already proved unchanged, and the campaign-id index of a
# campaigns directory. Nested scopes reuse the outer one; nothing outlives it.
_READ_PASS: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar(
    "artifact_producer_read_pass", default=None)


@contextlib.contextmanager
def shared_revision_read_scope():
    """Pin immutable-revision proofs and campaign lookups for one read pass."""
    if _READ_PASS.get() is not None:
        yield
        return
    token = _READ_PASS.set({})
    try:
        yield
    finally:
        _READ_PASS.reset(token)


def _read_pass_entry(name: str, factory):
    """This read pass's value for ``name`` (created on first use), or ``None``."""
    scope = _READ_PASS.get()
    if scope is None:
        return None
    if name not in scope:
        scope[name] = factory()
    return scope[name]


def _campaign_directories(campaigns: Path, campaign_id: str) -> List[Path]:
    """Campaign directories whose ``campaign.json`` names ``campaign_id``."""
    index = _read_pass_entry("campaign-index", dict)
    if index is not None and str(campaigns) in index:
        return list(index[str(campaigns)].get(campaign_id, ()))
    by_id: Dict[str, List[Path]] = {}
    matches = []
    if campaigns.is_dir() and not campaigns.is_symlink():
        for candidate in campaigns.iterdir():
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            campaign = _read_json(candidate / "campaign.json")
            value = campaign.get("campaign_id") if campaign is not None else None
            if isinstance(value, str):
                by_id.setdefault(value, []).append(candidate)
            if campaign is not None and value == campaign_id:
                matches.append(candidate)
    if index is not None:
        index[str(campaigns)] = by_id
    return matches


def _record_cycle_manifest_path(root: Path, record: Mapping[str, Any]) -> Path:
    """Resolve this record's manifest without scanning other cycle bindings."""
    root = Path(root).resolve()
    if record.get("request_binding"):
        return cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "manifest.json"
    if record.get("journal_migration"):
        from artifact_journal_migration import resolve_backup
        return resolve_backup(root, record) / "manifest.json"
    campaign_id = record.get("campaign_id")
    cycle_id = record.get("cycle_id")
    if not artifact_identity.is_well_formed(campaign_id, "campaign") or not artifact_identity.is_well_formed(cycle_id, "cycle"):
        raise ProducerError("sealed-cycle-state-unknown", f"{cycle_id}: record identity invalid")
    try:
        campaigns = artifact_locator.safe_child(root, root, "campaigns")
    except artifact_locator.LocatorError as exc:
        raise ProducerError("sealed-cycle-state-unknown", exc.detail or exc.code) from exc
    matches = _campaign_directories(campaigns, campaign_id)
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


def _earlier_documents(root: Path, cycle_id: str, document: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The cycle's preserved documents other than `document` itself."""
    return [old for _raw, old in artifact_lifecycle.read_manifest_snapshots(root, cycle_id)
            if old.get("manifest_revision_id") != document.get("manifest_revision_id")]


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


# ---------------------------------------------------------------------------
# shared admission
# ---------------------------------------------------------------------------


def _reference_path(root: Path, kind: str, ref_id: str) -> Path:
    return Path(root) / "shared" / kind / ref_id / "reference.json"


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
_CLOSED_RECORD_STATES = frozenset({"sealed", "superseded"})


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


def _closed_record(record: Mapping[str, Any]) -> bool:
    return record.get("state") in _CLOSED_RECORD_STATES


def cycle_record_closed(record: Optional[Mapping[str, Any]]) -> bool:
    """A cycle that was finished (its manifest is `manifest.json`), whatever it is marked as since."""
    return isinstance(record, Mapping) and _closed_record(record)


def _cycle_rel(root: Path, directory: Path) -> str:
    return Path(os.path.relpath(str(directory), str(Path(root)))).as_posix()

# The cycles whose record the last root observation of this process saw with no waiting history
# line.  `deliver_pending_history` in the same start need not open those records again.  A record
# this process writes since leaves the set; a line another process leaves after the look waits for
# the next trigger, as one left just after a delivery always did (§45 D-125).  A quiet cycle is
# vouched for by `HISTORY_QUIET_NAME`, the `lstat` identities of records last read with no waiting
# line, so its record is read once per identity, not per start.  That file is beside the memo, not
# in it: an older release rewrites the memo whole and would drop a field it does not know.  The
# delivery inherits the memo's NFS attribute-cache delay above: a line written on another host may
# go with a later trigger than it would have.
_QUIET_HISTORY: Dict[str, Tuple[float, Set[str]]] = {}


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
