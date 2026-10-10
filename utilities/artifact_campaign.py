"""Campaign closure and reopen events, their validated fold, and projection.

Cycle/route success is not inferred from campaign satisfaction. Closure is an
explicit producer event; reopens append a transition without erasing history.

A row's provisional keys (`route_closed`, `terminal_gate_proven`,
`terminal_gate_reasons`, `route_outcome_digest`) appear only on a provisional
member and hold the route outcome sidecar's values as of the close that
produced them; they are never re-observed later.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import NamedTuple, Optional

import artifact_admission as admission
import artifact_identity as identity
import artifact_index as index_module
import artifact_lifecycle as lifecycle
import artifact_locator as locator
import artifact_manifest as manifest

CONTRACT_V1 = "artifact-campaign-closure/v1"
CONTRACT = "artifact-campaign-closure/v2"
LEGACY_EVENT_NAME = "campaign.satisfied.json"
EVENTS_DIR = locator.CAMPAIGN_EVENTS_DIR
# The sentence a new campaign records when its begin names none.  The earlier
# fixed sentence is still read as an ordinary criterion; neither needs a reason to close.
DEFAULT_COMPLETION_CRITERION = "에이전트가 스트림 목표 충족을 판단해 닫는다"
LEGACY_DEFAULT_COMPLETION_CRITERION = "every cycle sealed with a manifest"


def _exact_cycle_control(root: Path, campaign_dir: Path, cycle_dir: Path, name: str) -> bool:
    path = cycle_dir / name
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return False
    return manifest.classify_artifact_path(
        str(root), campaign_dir.relative_to(root).as_posix(),
        cycle_dir.relative_to(root).as_posix(), "control",
        path.relative_to(root).as_posix(), "regular",
    ).allowed


def _exact_campaign_control(root: Path, campaign_path_value: Path, path: Path,
                            *, prospective: bool = False) -> bool:
    campaign_dir = Path(campaign_path_value).parent
    try:
        mode = os.lstat(path).st_mode
        node_kind = ("symlink" if stat.S_ISLNK(mode) else
                    "regular" if stat.S_ISREG(mode) else
                    "directory" if stat.S_ISDIR(mode) else "special")
    except FileNotFoundError:
        node_kind = "missing"
    except OSError:
        node_kind = "special"
    return manifest.classify_artifact_path(
        str(root), campaign_dir.relative_to(root).as_posix(), None, "control",
        Path(path).relative_to(root).as_posix(), node_kind, prospective=prospective,
    ).allowed
STATE_FIELDS = ("state", "satisfied_on", "satisfaction_event_id")
MAX_JSON = 16 * 1024 * 1024

# A cycle sealed `state: active` (route open at seal time, D-6) whose route has
# since closed. Never written to a record or event; computed for display only,
# so relabeling it later costs nothing against a signed statement.
PROVISIONAL_DISPOSITION = "sealed-unproven"


class CampaignError(Exception):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, str(detail)
        self.context = detail
        super().__init__(code + (": " + self.detail if self.detail else ""))


def disposition(row):
    if row.get("route_closed") is True and row.get("state") == "active":
        return PROVISIONAL_DISPOSITION
    return row["state"]


# One definition of campaign membership for producer cycle records.
# `artifact_producer._remove_empty_cycle` drops an output-less cycle from
# `campaign.cycles` but keeps its record (state `abandoned` or `no-lineage`)
# so the sealed abandon reason stays auditable.  Such a record is detached,
# not a member: counting it against `campaign.cycles` reported a permanent
# `campaign-membership-drift` (TF-Rehancer root, 2026-09-15, three abandoned
# records with no directory).  Writer and verifier both read this set.
DETACHED_STATES = frozenset({"abandoned", "no-lineage"})


def is_member_record(record):
    # A deleted cycle (§45 D-126) keeps its record but is no longer a member of anything.
    return (isinstance(record, dict) and record.get("state") not in DETACHED_STATES
            and not record.get("deleted_at")
            and not (record.get("relocation") or {}).get("artifact_root"))


def campaign_records(root, campaign_id, reads=None):
    """Split this campaign's producer records into (members, detached)."""
    records = Path(root) / ".runtime/artifact-producer/v1/cycles"
    members, detached = [], []
    for entry in sorted(records.glob("*.json")):
        record, _ = read_json(root, entry, reads=reads)
        if record.get("campaign_id") != campaign_id:
            continue
        (members if is_member_record(record) else detached).append(record)
    return members, detached


def detached_rows(detached):
    return [{"cycle_id": record.get("cycle_id"), "state": record.get("state"),
             "abandon_reason": record.get("abandon_reason"), "sealed_on": record.get("sealed_on"),
             "locator": record.get("locator")} for record in detached]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _safe(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise CampaignError("campaign-path-outside-root", path) from exc
    if ".." in parts:
        raise CampaignError("campaign-path-outside-root", path)
    current = root
    for part in ("", *parts):
        current = current / part
        if current.is_symlink():
            raise CampaignError("campaign-symlink", current)
    return path


def read_json(root, path, reads=None):
    if reads is not None:
        return reads.read_json(root, path)
    return _read_json_raw(root, path)


def _read_json_raw(root, path):
    path = _safe(root, path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON:
                raise CampaignError("campaign-input-kind-or-size", path)
            raw = stream.read(MAX_JSON + 1)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value, raw
    except (OSError, ValueError, UnicodeError) as exc:
        raise CampaignError("campaign-input-invalid", path) from exc


def campaign_path(root, selection, *, heal: bool = True):
    root = Path(root).resolve()
    if identity.is_well_formed(str(selection), "campaign"):
        directory = locator.find_path_by_id(root, str(selection), heal=heal)
        if directory is None:
            raise CampaignError("campaign-unknown", selection)
        path = directory / "campaign.json"
    else:
        path = Path(selection)
        if not path.is_absolute():
            path = root / path
        if path.name != "campaign.json":
            path = path / "campaign.json"
    _safe(root / "campaigns", path)
    if path.parent.parent != root / "campaigns":
        raise CampaignError("campaign-path-invalid", path)
    return path


def _id(prefix, value):
    return prefix + "_" + hashlib.sha256(canonical(value)).hexdigest()[:32]


class CampaignState(NamedTuple):
    state: str
    satisfied_on: Optional[str]
    satisfaction_event_id: Optional[str]
    events: tuple
    last_sequence: int
    stream_id: Optional[str]
    projection_pending: bool
    record: dict


def _validate_event_row(root, path, event, raw, expected_sequence, campaign_id, stream_id=None):
    violations = []
    manifest._v_event_row(event, "$", violations)
    if (violations or raw != canonical(event) + b"\n"
            or event.get("stream_sequence") != expected_sequence
            or event.get("event_id") != _id("evt", {k: v for k, v in event.items() if k != "event_id"})
            or event.get("target_id") != campaign_id
            or (stream_id is not None and event.get("stream_id") != stream_id)
            or not isinstance(event.get("stream_id"), str)
            or event.get("provenance", {}).get("algorithm_version") != CONTRACT):
        raise CampaignError("campaign-event-invalid", {"path": str(path), "violations": violations[:3]})
    return event


def _rename_evidence_map(root, campaign_dir, required, reads=None):
    """Read only the named historical revisions, before current fallback.

    A present historical revision is authoritative even when malformed. Its
    errors cannot be concealed by the current manifest for the same revision.
    All reads participate in the export's immutable input capture.
    """
    found = {}
    for key in sorted(required):
        cycle_id, _manifest_id, revision_id = key
        candidate = root / lifecycle.MANIFEST_SNAPSHOT_REL / cycle_id / (revision_id + ".json")
        try:
            document, raw = read_json(root, candidate, reads=reads)
        except CampaignError as exc:
            if (exc.code == "campaign-input-missing"
                    or isinstance(exc.__cause__, FileNotFoundError)):
                continue
            raise CampaignError("campaign-event-invalid", {
                "path": str(candidate), "detail": "rename-evidence-tampered"}) from exc
        found[key] = (candidate, document, raw)
    if required.issubset(found):
        return found
    for entry, _layout in (reads.cycle_dirs(campaign_dir) if reads is not None else locator.iter_cycle_dirs(campaign_dir)):
        candidate = entry / "manifest.json"
        try:
            mode = os.lstat(candidate).st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            continue
        try:
            document, raw = read_json(root, candidate, reads=reads)
        except CampaignError:
            continue
        cycle = document.get("cycle") if isinstance(document, dict) else None
        key = (cycle.get("cycle_id") if isinstance(cycle, dict) else None,
               document.get("manifest_id"), document.get("manifest_revision_id"))
        if all(isinstance(part, str) for part in key) and key in required:
            found.setdefault(key, (candidate, document, raw))
    return found


def _bind_root(root, campaign_dir, payload, snapshot, record_campaign_id, event_path, reads=None):
    """Stable RootIdentity binding for a close event after a root rename.

    Historical paths are provenance, including when the path is unchanged.
    Every close verifies current RootIdentity. A new v2 snapshot at its
    original path supplies both stable IDs directly. A renamed or legacy
    snapshot additionally binds each row, byte-exact, to a surviving manifest:
    its `artifact_root_id` must equal the current one — a root ID alone never
    authorizes the rename — and each row's `(manifest_id,
    manifest_revision_id)` must resolve to preserved or current manifest bytes
    whose SHA256 equals the row's `manifest_digest` and whose document agrees
    on manifest, revision, artifact-root, repository, and campaign identities.
    Missing evidence, a foreign root or repository, tamper, invalid IDs, and a
    malformed identity stay explicit `campaign-event-invalid` errors.  Event and
    history bytes are never modified.
    """
    recorded = payload.get("root") if isinstance(payload, dict) else None
    if not isinstance(recorded, str) or not recorded:
        raise CampaignError("campaign-event-invalid", {"path": str(event_path), "detail": "rename-root-missing"})
    try:
        current = reads.read_root_identity(root) if reads is not None else lifecycle.read_root_identity(root)
    except (lifecycle.LifecycleError, CampaignError) as exc:
        raise CampaignError("campaign-event-invalid",
                            {"path": str(event_path), "detail": "rename-identity-unreadable",
                             "error": exc.args[0] if exc.args else ""}) from exc
    if current is None:
        # Without a stable identity there is no evidence a rename can bind to;
        # the historical absolute path stays required, as before.
        raise CampaignError("campaign-event-invalid", {"path": str(event_path), "detail": "rename-evidence-missing"})
    if (not isinstance(snapshot, dict) or snapshot.get("artifact_root_id") != current.artifact_root_id
            or ("repository_id" in snapshot and snapshot["repository_id"] != current.repository_id)):
        raise CampaignError("campaign-event-invalid", {"path": str(event_path), "detail": "rename-binding-mismatch"})
    if (recorded == str(Path(root).resolve())
            and payload.get("contract") == CONTRACT
            and snapshot.get("repository_id") == current.repository_id):
        # New v2 snapshots bind both stable IDs in their authenticated body.
        # At the original path no manifest fallback is needed to prove them,
        # including byte-preserved formatted legacy seals.
        return None
    rows = snapshot.get("cycles")
    if not isinstance(rows, list) or not rows:
        raise CampaignError("campaign-event-invalid", {"path": str(event_path), "detail": "rename-evidence-missing"})
    required = {(row.get("cycle_id"), row.get("manifest_id"), row.get("manifest_revision_id"))
                for row in rows if isinstance(row, dict)
                and identity.is_well_formed(row.get("cycle_id"), "cycle")
                and identity.is_well_formed(row.get("manifest_id"), "manifest")
                and identity.is_well_formed(row.get("manifest_revision_id"), "manifest_revision")}
    evidence = _rename_evidence_map(Path(root).resolve(), campaign_dir, required, reads=reads)
    bound_manifests = 0
    for row in rows:
        if not isinstance(row, dict):
            raise CampaignError("campaign-event-invalid",
                                {"path": str(event_path), "detail": "rename-binding-mismatch"})
        cycle_id, manifest_id, revision_id, row_digest = (row.get("cycle_id"), row.get("manifest_id"),
                                                          row.get("manifest_revision_id"), row.get("manifest_digest"))
        if (payload.get("contract") == CONTRACT and row.get("state") == "open"
                and row.get("route_closed") is True
                and identity.is_well_formed(cycle_id, "cycle")
                and isinstance(row.get("route_id"), str) and row["route_id"]
                and all(row.get(key) is None for key in
                        ("manifest_id", "manifest_revision_id", "manifest_digest", "index_digest"))):
            # Official v2 closes can include an already-ended route whose
            # cycle has no manifest. The authenticated snapshot retains that
            # row; surviving manifest rows must still prove this repository.
            continue
        if (not identity.is_well_formed(cycle_id, "cycle")
                or not identity.is_well_formed(manifest_id, "manifest")
                or not identity.is_well_formed(revision_id, "manifest_revision")
                or not isinstance(row_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", row_digest)):
            raise CampaignError("campaign-event-invalid",
                                {"path": str(event_path), "detail": "rename-binding-mismatch",
                                 "cycle_id": cycle_id if isinstance(cycle_id, str) else None})
        if reads is not None:
            reads.read_raw(root, Path(root) / lifecycle.MANIFEST_SNAPSHOT_REL / cycle_id / (revision_id + ".json"))
        match = evidence.get((cycle_id, manifest_id, revision_id))
        if match is None:
            raise CampaignError("campaign-event-invalid",
                                {"path": str(event_path), "detail": "rename-evidence-missing", "cycle_id": cycle_id})
        _entry, document, raw = match
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        campaign = document.get("campaign") if isinstance(document, dict) else None
        cycle = document.get("cycle") if isinstance(document, dict) else None
        if (actual != row_digest or document.get("manifest_id") != manifest_id
                or document.get("manifest_revision_id") != revision_id
                or document.get("artifact_root_id") != current.artifact_root_id
                or document.get("repository_id") != current.repository_id
                or not isinstance(cycle, dict) or cycle.get("cycle_id") != cycle_id
                or cycle.get("campaign_id") != record_campaign_id
                or not isinstance(campaign, dict) or campaign.get("campaign_id") != record_campaign_id):
            raise CampaignError("campaign-event-invalid",
                                {"path": str(event_path),
                                 "detail": ("rename-evidence-tampered" if actual != row_digest
                                            else "rename-binding-mismatch"),
                                 "cycle_id": cycle_id})
        bound_manifests += 1
    if not bound_manifests:
        raise CampaignError("campaign-event-invalid",
                            {"path": str(event_path), "detail": "rename-evidence-missing"})
    return None


def _validate_v1(root, path, record, reads=None):
    event_path = path.parent / LEGACY_EVENT_NAME
    if event_path.is_symlink():
        raise CampaignError("campaign-symlink", event_path)
    if reads is not None:
        if reads.read_raw(root, event_path) is None:
            return None
    elif not event_path.exists():
        return None
    if not _exact_campaign_control(root, path, event_path):
        raise CampaignError("campaign-event-invalid", {"path": str(event_path), "detail": "control-path-required"})
    event, raw = read_json(root, event_path, reads=reads)
    violations = []
    manifest._v_event_row(event, "$", violations)
    payload = event.get("payload", {})
    snapshot = payload.get("snapshot", {}) if isinstance(payload, dict) else {}
    campaign = snapshot.get("campaign", {}) if isinstance(snapshot, dict) else {}
    approval = payload.get("approval", {}) if isinstance(payload, dict) else {}
    harness, session = approval.get("harness"), approval.get("session_id")
    actor_id = "native-user:" + hashlib.sha256((str(harness) + ":" + str(session)).encode()).hexdigest()
    if (violations or raw != canonical(event) + b"\n"
            or event.get("event_type") != "campaign.satisfied"
            or payload.get("contract") != CONTRACT_V1
            or event.get("stream_sequence") != 1
            or event.get("event_id") != _id("evt", {k: v for k, v in event.items() if k != "event_id"})
            or event.get("target_id") != record.get("campaign_id")
            or campaign.get("campaign_id") != record.get("campaign_id")
            or campaign.get("state") != "active"
            or event.get("actor") != {"kind": "user", "id": actor_id}
            or approval.get("actor_id") != actor_id):
        raise CampaignError("campaign-event-invalid", {"path": str(event_path), "violations": violations[:3]})
    _validate_close_provenance(event, payload, event_path)
    _bind_root(root, path.parent, payload, snapshot, record.get("campaign_id"), event_path, reads=reads)
    return event


def _validate_close_provenance(event, payload, path):
    snapshot = payload.get("snapshot") or {}
    source = event.get("provenance") or {}
    rows = snapshot.get("cycles") or []
    if (snapshot.get("root") != payload.get("root")
            or source.get("source_digest") != digest(snapshot)
            or not isinstance(rows, list)
            or not any(isinstance(row, dict) and all(row.get(key) == source.get(provenance_key)
                for key, provenance_key in (("manifest_id", "source_manifest_id"),
                    ("manifest_revision_id", "source_revision_id"), ("route_id", "producer_route_id")))
                for row in rows)):
        raise CampaignError("campaign-event-invalid", {"path": str(path), "detail": "closure-provenance-mismatch"})


def _read_stream(root, path, record, start_sequence, stream_id, reads=None):
    directory = path.parent / EVENTS_DIR
    if directory.is_symlink():
        raise CampaignError("campaign-symlink", directory)
    if reads is not None:
        entries = reads.entries(directory)
        if reads.is_missing(directory):
            return []
    elif not directory.exists():
        return []
    else:
        entries = sorted(directory.iterdir())
    if not directory.is_dir():
        raise CampaignError("campaign-event-invalid", {"path": str(directory), "detail": "not-directory"})
    rows = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        match = re.fullmatch(r"(\d{6})\.json", entry.name)
        if not match:
            raise CampaignError("campaign-event-invalid", {"path": str(entry), "detail": "unexpected-entry"})
        sequence = int(match.group(1))
        if entry.is_symlink():
            raise CampaignError("campaign-symlink", entry)
        if not _exact_campaign_control(root, path, entry):
            raise CampaignError("campaign-event-invalid", {"path": str(entry), "detail": "control-path-required"})
        event, raw = read_json(root, entry, reads=reads)
        _validate_event_row(root, entry, event, raw, sequence, record.get("campaign_id"), stream_id)
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
            raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "payload.contract"})
        event_type = event.get("event_type")
        if event_type == "campaign.satisfied":
            snapshot = payload.get("snapshot")
            closure = payload.get("closure")
            if (set(payload) != {"contract", "root", "snapshot", "closure"}
                    or not isinstance(payload.get("root"), str) or not payload["root"]
                    or not isinstance(snapshot, dict)
                    or not isinstance(snapshot.get("campaign"), dict)
                    or snapshot["campaign"].get("campaign_id") != record.get("campaign_id")
                    or not isinstance(closure, dict) or set(closure) != {"reason", "closed_by"}
                    or not isinstance(closure.get("reason"), str) or not closure["reason"].strip()
                    or not isinstance(closure.get("closed_by"), dict)
                    or set(closure["closed_by"]) != {"harness", "session_id"}
                    or any(value is not None and not isinstance(value, str)
                           for value in closure["closed_by"].values())
                    or event.get("actor", {}).get("kind") != "producer"
                    or not str(event.get("actor", {}).get("id", "")).startswith("agent:")):
                raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "payload"})
            _validate_close_provenance(event, payload, entry)
            closed_by = closure["closed_by"]
            expected_actor = ("agent:%s:%s" % (closed_by["harness"], closed_by["session_id"])
                              if closed_by["harness"] and closed_by["session_id"] else "agent:unknown")
            if event["actor"]["id"] != expected_actor:
                raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "actor"})
            _bind_root(root, path.parent, payload, snapshot, record.get("campaign_id"), entry, reads=reads)
        elif event_type == "campaign.reopened":
            if (event.get("actor", {}).get("kind") != "producer"
                    or not (str(event.get("actor", {}).get("id", "")).startswith("agent:")
                            or str(event.get("actor", {}).get("id", "")).startswith("route:"))):
                raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "actor"})
            if payload.get("reason") == "cycle-begin":
                expected = {"contract", "reason", "reopens_event_id", "route_id", "requested_selection"}
                if (not isinstance(payload.get("route_id"), str)
                        or event.get("actor", {}).get("id") != "route:" + payload["route_id"]
                        or not isinstance(payload.get("requested_selection"), dict)):
                    raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "payload"})
            else:
                expected = {"contract", "reason", "reopens_event_id"}
                if not isinstance(payload.get("reason"), str) or not payload["reason"].strip():
                    raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "payload.reason"})
            if set(payload) != expected:
                raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "payload"})
        elif event_type == "campaign.superseded":
            if (set(payload) != {"contract", "operation_id", "canonical", "previous_state"}
                    or not isinstance(payload.get("operation_id"), str)
                    or payload.get("previous_state") not in {"active", "satisfied"}
                    or not isinstance(payload.get("canonical"), dict)
                    or set(payload["canonical"]) != {"artifact_root", "campaign_id"}
                    or not Path(payload["canonical"].get("artifact_root", "")).is_absolute()
                    or not identity.is_well_formed(payload["canonical"].get("campaign_id"), "campaign")
                    or event.get("actor", {}).get("kind") != "producer"):
                raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "payload"})
        else:
            raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "event_type"})
        rows.append((sequence, "stream", event))
    expected_numbers = list(range(start_sequence, start_sequence + len(rows)))
    if [row[0] for row in rows] != expected_numbers:
        raise CampaignError("campaign-event-sequence-invalid", "gap-or-duplicate")
    return rows


def campaign_state(root, path, record=None, reads=None):
    root, path = Path(root).resolve(), Path(path)
    if not path.is_absolute():
        path = root / path
    _safe(root, path)
    if record is None:
        record, _ = read_json(root, path, reads=reads)
    if not isinstance(record, dict):
        raise CampaignError("campaign-input-invalid", path)
    if (("schema_version" in record and (type(record["schema_version"]) is not int or record["schema_version"] != 1))
            or ("contract" in record and record["contract"] != "artifact-producer/v1")):
        raise CampaignError("campaign-schema-invalid", path)
    if not identity.is_well_formed(record.get("campaign_id"), "campaign"):
        raise CampaignError("campaign-id-invalid", path)
    if "state" in record and record["state"] not in ("active", "satisfied", "abandoned", "superseded"):
        raise CampaignError("campaign-state-invalid", path)
    legacy = _validate_v1(root, path, record, reads=reads)
    rows = []
    stream_id = None
    if legacy is not None:
        rows.append((1, "v1", legacy))
        stream_id = legacy.get("stream_id")
    rows.extend(_read_stream(root, path, record, 2 if legacy else 1, stream_id, reads=reads))
    if rows:
        stream_id = rows[0][2].get("stream_id")
        if any(event.get("stream_id") != stream_id for _seq, _origin, event in rows):
            raise CampaignError("campaign-event-invalid", {"field": "stream_id"})
    folded = manifest.fold_campaign_closure([row[2] for row in rows])
    if folded.error:
        raise CampaignError(folded.error[0], folded.error[1])
    # Historical manifest-only campaign views may predate the explicit mutable
    # state field; they have always represented an open stream.
    state = folded.state if rows else (record.get("state") or "active")
    if not rows:
        if record.get("state") == "satisfied" or record.get("satisfied_on") or record.get("satisfaction_event_id"):
            raise CampaignError("campaign-projection-conflict", path)
        if state not in {"active", "abandoned", "superseded"}:
            raise CampaignError("campaign-state-invalid", state)
    else:
        if record.get("state") == "abandoned" or (record.get("state") == "superseded" and folded.state != "superseded"):
            raise CampaignError("campaign-projection-conflict", path)
        satisfaction_ids = {event.get("event_id") for _, _, event in rows
                            if event.get("event_type") == "campaign.satisfied"}
        if (record.get("satisfaction_event_id") is not None
                and record.get("satisfaction_event_id") not in satisfaction_ids):
            raise CampaignError("campaign-projection-conflict", path)
    last = folded.last_satisfied if state == "satisfied" else None
    projected = project(record, state, last)
    pending = any(record.get(key) != projected.get(key) for key in STATE_FIELDS)
    return CampaignState(state, last.get("recorded_at") if last else None,
                         last.get("event_id") if last else None, tuple(rows),
                         rows[-1][0] if rows else 0, stream_id, pending, record)


def project(record, state_or_fold, last_satisfied=None):
    value = dict(record)
    if isinstance(state_or_fold, CampaignState):
        state, last_satisfied = state_or_fold.state, next(
            (event for _, _, event in state_or_fold.events
             if event.get("event_id") == state_or_fold.satisfaction_event_id), None)
    else:
        state = state_or_fold
    value["state"] = state
    if state == "satisfied" and last_satisfied:
        value["satisfied_on"] = last_satisfied["recorded_at"]
        value["satisfaction_event_id"] = last_satisfied["event_id"]
    else:
        value.pop("satisfied_on", None)
        value.pop("satisfaction_event_id", None)
    return value


def fold_campaign(root, path, campaign, reads=None):
    folded = campaign_state(root, path, campaign, reads=reads)
    return project(campaign, folded)


def check_campaign_write(root, path, proposed):
    if not Path(path).exists():
        if (proposed.get("state") == "satisfied" or proposed.get("satisfied_on")
                or proposed.get("satisfaction_event_id")):
            raise CampaignError("campaign-terminal-write-conflict", path)
        return
    folded = campaign_state(root, path)
    expected = project(proposed, folded)
    if folded.events:
        if any(proposed.get(key) != expected.get(key) for key in STATE_FIELDS):
            raise CampaignError("campaign-terminal-write-conflict", path)
    elif (proposed.get("state") == "satisfied" or proposed.get("satisfied_on")
          or proposed.get("satisfaction_event_id")):
        raise CampaignError("campaign-terminal-write-conflict", path)


def _validate_runlog(root, path, campaign, ids):
    value = campaign.get("runlog")
    if value is None:
        return
    keys = {"contract", "path", "sha256", "source_cycle_id", "source_locator"}
    if not isinstance(value, dict) or set(value) != keys:
        raise CampaignError("campaign-runlog-invalid", "shape")
    source_cycle_id = value.get("source_cycle_id")
    if (value.get("contract") != "campaign-runlog/v1" or value.get("path") != "RUNLOG.md"
            or value.get("source_locator") != "experiments/_RUNLOG.md"
            or not identity.is_well_formed(source_cycle_id, "cycle")
            or source_cycle_id in ids
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("sha256")))):
        raise CampaignError("campaign-runlog-invalid", "contract")
    runlog = _safe(path.parent, path.parent / "RUNLOG.md")
    try:
        fd = os.open(runlog, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CampaignError("campaign-runlog-invalid", "kind")
            actual = "sha256:" + hashlib.sha256(stream.read()).hexdigest()
    except OSError as exc:
        raise CampaignError("campaign-runlog-missing", runlog) from exc
    if actual != value["sha256"]:
        raise CampaignError("campaign-runlog-digest-mismatch", runlog)


def _route_outcome(root, record, document, reads=None):
    """`None` when the cycle's route is still open; else `(outcome, raw)`.

    Route-closed is judged by the canonical outcome sidecar's existence, not
    by `route_file` (relocation-tolerant) or `schema_version`. A sidecar that
    exists but whose identity does not match the record and the manifest's
    `routes[]` row is a typed integrity error, not "open".
    """
    try:
        outcome_path = lifecycle.canonical_outcome_path(root, record["route_id"])
    except lifecycle.LifecycleError as exc:
        raise CampaignError("campaign-cycle-route-outcome-mismatch", record.get("cycle_id")) from exc
    if not os.path.lexists(outcome_path):
        return None
    outcome, raw = read_json(root, outcome_path, reads=reads)
    routes = document.get("routes")
    matching = ([row for row in routes if isinstance(row, dict) and row.get("route_id") == record["route_id"]]
                if isinstance(routes, list) else [])
    if (outcome.get("route_id") != record.get("route_id")
            or outcome.get("route_hash") != record.get("route_hash")
            or len(matching) != 1
            or matching[0].get("route_hash") != outcome.get("route_hash")
            or matching[0].get("terminal_marker") != "pending"):
        raise CampaignError("campaign-cycle-route-outcome-mismatch", record.get("cycle_id"))
    return outcome, raw


def _open_refusal_detail(pending):
    entries = [f"cycle={cid} route={rid} route_state=open" for cid, rid in pending]
    shown = "; ".join(entries[:10])
    if len(entries) > 10:
        shown += f" +{len(entries) - 10} more"
    return (f"open={len(pending)} {shown} "
            "next=capability-route.py complete --route <route_file> --node <terminal node> "
            "then close --route <route_file> (closing without it records the route as unproven); "
            "then rerun campaign-status")


def _cycle_rows(root, path, campaign, reads=None):
    ids = campaign.get("cycles")
    if (not isinstance(ids, list) or not ids or not all(isinstance(cid, str) for cid in ids)
            or len(set(ids)) != len(ids)):
        raise CampaignError("campaign-membership-invalid")
    _validate_runlog(root, path, campaign, ids)
    records = root / ".runtime/artifact-producer/v1/cycles"
    if reads is not None:
        root_id = reads.read_root_identity(root)
    else:
        root_id = lifecycle.read_root_identity(root)
    if root_id is None:
        raise CampaignError("root-identity-missing")
    if admission.load_index(root).artifact_root_id != root_id.artifact_root_id:
        raise CampaignError("campaign-index-root-mismatch")
    # A campaign list alone cannot hide an open member or an unregistered tree.
    # Detached records (see `is_member_record`) are reported, never counted.
    member_records, detached = campaign_records(root, campaign["campaign_id"], reads=reads)
    members = {record.get("cycle_id") for record in member_records}
    if members != set(ids):
        raise CampaignError("campaign-membership-drift", {
            "campaign_id": campaign["campaign_id"],
            "listed_cycle_ids": sorted(ids),
            "producer_cycle_ids": sorted(members),
            "next_step": "reconcile-campaign-membership-with-producer-records",
        })
    directories = {}
    for entry, layout in locator.iter_cycle_dirs(path.parent):
        _safe(path.parent, entry)
        if _exact_cycle_control(root, path.parent, entry, ".cycle.json"):
            binding, _ = read_json(root, entry / ".cycle.json", reads=reads)
        elif _exact_cycle_control(root, path.parent, entry, "manifest.json"):
            # Historical sealed cycles predate .cycle.json. Their immutable
            # manifest plus producer record and index still prove the binding.
            document, _ = read_json(root, entry / "manifest.json", reads=reads)
            binding = document.get("cycle", {})
        elif layout == "legacy-id" and entry.name in ids:
            binding = {"cycle_id": entry.name, "campaign_id": campaign["campaign_id"]}
        else:
            # Undeclared material is not silently made a cycle or a new
            # residual-zero requirement. Declared members still must resolve.
            continue
        if not isinstance(binding, dict) or binding.get("campaign_id") != campaign["campaign_id"]:
            raise CampaignError("campaign-cycle-binding-mismatch", entry)
        cid = binding.get("cycle_id")
        if not isinstance(cid, str) or cid in directories:
            raise CampaignError("campaign-cycle-binding-duplicate", entry)
        directories[cid] = entry
    if set(directories) != set(ids):
        raise CampaignError("campaign-cycle-bindings-incomplete")
    rows = []
    pending_open = []
    for cid in sorted(ids):
        if not identity.is_well_formed(cid, "cycle"):
            raise CampaignError("campaign-cycle-id-invalid", cid)
        record, _ = read_json(root, records / (cid + ".json"), reads=reads)
        directory = directories[cid]
        _safe(path.parent, directory)
        expected_directory = (path.parent / str(record["locator"]) if record.get("locator")
                              else path.parent / "cycles" / cid)
        if directory != expected_directory:
            raise CampaignError("campaign-cycle-locator-invalid", cid)
        if (record.get("state") not in {"sealed", "superseded"} or not record.get("sealed_on")
                or not _exact_cycle_control(root, path.parent, directory, "manifest.json")):
            # §45 D-127: a cycle that never closed does not stop the campaign from
            # closing; only a route that is still open does.  Its row says what is
            # there: no manifest.
            outcome_path = lifecycle.canonical_outcome_path(root, record["route_id"])
            if not os.path.lexists(outcome_path):
                pending_open.append((cid, record["route_id"]))
                continue
            rows.append({"cycle_id": cid, "state": "open", "manifest_digest": None, "index_digest": None,
                         "route_id": record["route_id"], "manifest_id": None, "manifest_revision_id": None,
                         "route_closed": True})
            continue
        document, raw = read_json(root, directory / "manifest.json", reads=reads)
        if reads is not None:
            earlier = [old for _old_raw, old in reads.read_manifest_snapshots(root, cid)
                       if old.get("manifest_revision_id") != document.get("manifest_revision_id")]
        else:
            earlier = [old for _old_raw, old in lifecycle.read_manifest_snapshots(root, cid)
                       if old.get("manifest_revision_id") != document.get("manifest_revision_id")]
        report = manifest.validate(document)
        if not report.ok and earlier:
            # The next document of a cycle the runtime refreshed after it closed.
            report = manifest.validate_update(document, preserved=earlier, published=True)
        if not report.ok:
            raise CampaignError("campaign-manifest-invalid", cid + ": " + str(report.violations[:3]))
        # The row records the manifest as it is now.  Legacy approved merges kept
        # noncanonical JSON formatting; their bytes are never rewritten, so the
        # digest is of the bytes on disk.  Whether the record, the index and the
        # files still agree with it is the refresh's business, not the close's.
        mdigest = "sha256:" + hashlib.sha256(raw).hexdigest()
        cycle = document["cycle"]
        if (cycle.get("cycle_id") != cid or cycle.get("campaign_id") != campaign["campaign_id"]
                or document["campaign"]["campaign_id"] != campaign["campaign_id"]
                or document["artifact_root_id"] != root_id.artifact_root_id
                or document["producer"]["producer_id"] != record.get("producer_id")):
            raise CampaignError("campaign-seal-mismatch", cid)
        row = {"cycle_id": cid, "state": cycle["state"], "manifest_digest": mdigest,
               "index_digest": manifest.manifest_digest(document),
               "route_id": record["route_id"],
               "manifest_id": document["manifest_id"],
               "manifest_revision_id": document["manifest_revision_id"]}
        if cycle["state"] == "active":
            outcome = _route_outcome(root, record, document, reads=reads)
            if outcome is None:
                pending_open.append((cid, record["route_id"]))
                continue
            outcome_doc, outcome_raw = outcome
            gates = outcome_doc.get("terminal_gates")
            reasons = []
            if isinstance(gates, dict):
                for node_id, gate_row in gates.items():
                    if not isinstance(gate_row, dict) or gate_row.get("passed") is not True:
                        reason = gate_row.get("reason") if isinstance(gate_row, dict) else None
                        reasons.append(f"{node_id}:{reason or 'unknown'}")
                reasons.sort()
            row["route_closed"] = True
            row["terminal_gate_proven"] = outcome_doc.get("terminal_gate_proven")
            row["terminal_gate_reasons"] = reasons
            row["route_outcome_digest"] = "sha256:" + hashlib.sha256(outcome_raw).hexdigest()
        rows.append(row)
    if pending_open:
        raise CampaignError("campaign-cycle-provisional-active", _open_refusal_detail(pending_open))
    return root_id, rows


def _snapshot(root, path, reads=None):
    campaign, _ = read_json(root, path, reads=reads)
    effective = fold_campaign(root, path, campaign, reads=reads)
    if effective.get("state") != "active":
        raise CampaignError("campaign-not-active", effective.get("state"))
    criterion = campaign.get("completion_criterion", {}).get("statement")
    if (not isinstance(criterion, str) or not criterion.strip()
            or not isinstance(campaign.get("goal"), str) or not campaign["goal"].strip()
            or not identity.is_well_formed(campaign.get("campaign_id"), "campaign")):
        raise CampaignError("campaign-criterion-missing")
    root_id, rows = _cycle_rows(root, path, campaign, reads=reads)
    return {"artifact_root_id": root_id.artifact_root_id, "repository_id": root_id.repository_id,
            "root": str(root),
            "campaign": campaign, "cycles": rows}


def status(root, selection):
    # Pure read: no reconcile, no history flush, no index heal. The pending
    # projection and recovery command are displayed, never repaired here;
    # writer commands (campaign-close/compose/campaign-recover) own repairs.
    root = Path(root).resolve()
    path = campaign_path(root, selection, heal=False)
    record, _ = read_json(root, path)
    folded = campaign_state(root, path, record)
    event_rows = [{"sequence": seq, "event_type": event["event_type"],
                   "event_id": event["event_id"], "recorded_at": event["recorded_at"],
                   "actor": event["actor"]} for seq, _origin, event in folded.events]
    recovery = ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                "campaign-recover", "--artifact-root", str(root), "--campaign", str(path)]
    if folded.state == "satisfied":
        return {"status": "satisfied", "state": "satisfied", "campaign_id": record["campaign_id"],
                "satisfied": True, "closable": False, "satisfied_on": folded.satisfied_on,
                "satisfaction_event_id": folded.satisfaction_event_id, "events": event_rows,
                "projection_pending": folded.projection_pending,
                "reopen_command": ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                                   "campaign-reopen", "--artifact-root", str(root), "--campaign", str(path)],
                "recovery_command": recovery if folded.projection_pending else None,
                "message": "같은 key/ID/parent로 begin하면 자동으로 다시 열린다"}
    if folded.state in {"abandoned", "superseded"}:
        return {"status": folded.state, "state": folded.state, "campaign_id": record["campaign_id"],
                "canonical": record.get("relocation"),
                "satisfied": False, "closable": False,
                "close_refusal": {"reason": "campaign-not-active"}, "events": event_rows,
                "projection_pending": folded.projection_pending}
    try:
        snapshot = _snapshot(root, path)
        result = {"status": "active", "state": "active", "campaign_id": record["campaign_id"],
                  "goal": record.get("goal"), "completion_criterion": record.get("completion_criterion"),
                  "cycles": [{**row, "disposition": disposition(row)} for row in snapshot["cycles"]],
                  "detached_cycles": detached_rows(campaign_records(root, record["campaign_id"])[1]),
                  "satisfied": False, "closable": True, "reason_required": False,
                  "close_command": ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                                    "campaign-close", "--artifact-root", str(root), "--campaign", str(path)],
                  "events": event_rows, "projection_pending": folded.projection_pending}
        unproven = [row for row in result["cycles"] if row["disposition"] == PROVISIONAL_DISPOSITION]
        if unproven:
            result["unproven_cycles"] = {"count": len(unproven),
                                         "without_terminal_proof": sum(row.get("terminal_gate_proven") is not True for row in unproven)}
        return result
    except CampaignError as exc:
        return {"status": "active", "state": "active", "campaign_id": record["campaign_id"],
                "satisfied": False, "closable": False,
                "close_refusal": {"reason": exc.code, "detail": exc.detail}, "events": event_rows,
                "projection_pending": folded.projection_pending}



def _publish_event(root, path, event):
    """Publish a numbered event without replacing an existing sequence."""
    directory = path.parent / EVENTS_DIR
    _safe(root, directory)
    existed = directory.exists()
    directory.mkdir(exist_ok=True)
    if not existed:
        parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    target = directory / ("%06d.json" % event["stream_sequence"])
    _safe(root, target)
    if not _exact_campaign_control(root, path, target, prospective=True):
        raise CampaignError("campaign-event-invalid", {"path": str(target), "detail": "control-path-required"})
    fd, tmp = tempfile.mkstemp(prefix=".campaign-event-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(event) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise CampaignError("campaign-event-sequence-invalid", target) from exc
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(tmp)
    return target


def _index_update(fn, root, campaign_id):
    try:
        fn(root, [campaign_id])
    except locator.LocatorError as exc:
        raise CampaignError(exc.code, exc.detail) from exc


def _materialize(root, path):
    import artifact_producer as producer
    current, _ = read_json(root, path)
    folded = campaign_state(root, path, current)
    projected = project(current, folded)
    if current != projected:
        producer._write_campaign(root, projected, exclusive=False)
    return {"status": folded.state, "campaign_id": current["campaign_id"],
            "event_id": folded.events[-1][2]["event_id"] if folded.events else None,
            "projection_pending": False}


def _agent_actor():
    # The calling harness and session come from the one shared resolver; an
    # ambiguous or mislabeled environment is an unknown actor, never a guess.
    from dispatch_contract import DispatchContractError
    from route_authority import caller_identity
    try:
        harness, session = caller_identity()
    except DispatchContractError:
        harness, session = "", ""
    if not harness or not session:
        return "agent:unknown", {"harness": None, "session_id": None}
    return "agent:%s:%s" % (harness, session), {"harness": harness, "session_id": session}


def _new_event(root, path, state, event_type, actor, payload, provenance):
    event = {"stream_id": state.stream_id or identity.IdAllocator().allocate("stream"),
             "stream_sequence": state.last_sequence + 1, "event_type": event_type,
             "target_id": state.record["campaign_id"], "actor": actor,
             "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             "provenance": provenance, "evidence_ids": [], "payload": payload}
    event["event_id"] = _id("evt", event)
    violations = []
    manifest._v_event_row(event, "$", violations)
    if violations:
        raise CampaignError("campaign-event-invalid", violations[:3])
    return event


def _reconcile(root):
    """§45 D-126: a cycle or campaign folder moved, renamed or removed by hand is found before the
    campaign is read.  Never an error for the caller."""
    try:
        import artifact_producer as producer
        producer.reconcile_root(root)
    except Exception:  # noqa: BLE001
        pass


def _state_line(root, event, action):
    """§45 D-125: the close or reopen leaves one `state` line; a recorder that cannot take it keeps it
    in the campaign's runtime record.  Never fails the close."""
    try:
        import artifact_producer as producer
        payload = event.get("payload") or {}
        reason = (payload.get("closure") or {}).get("reason") if action == "close" else payload.get("reason")
        before, after = ("active", "satisfied") if action == "close" else ("satisfied", "active")
        producer.record_campaign_state_line(
            root, event["target_id"], before=before, after=after, reason=reason if isinstance(reason, str) else None,
            event_id=event.get("event_id"), command="campaign-" + action)
    except Exception:  # noqa: BLE001
        pass


def _commit_event(root, path, event, action):
    campaign_id = event["target_id"]
    _index_update(locator.prepare_index_update, root, campaign_id)
    try:
        _publish_event(root, path, event)
        result = _materialize(root, path)
        _index_update(locator.update_indexes, root, campaign_id)
        _state_line(root, event, action)
        return result
    except Exception as exc:
        try:
            committed = campaign_state(root, path)
        except CampaignError:
            committed = None
        if committed and committed.last_sequence >= event["stream_sequence"]:
            recovery = ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                        "campaign-recover", "--artifact-root", str(root), "--campaign", str(path)]
            raise CampaignError("campaign-%s-committed-recovery-required" % action,
                                json.dumps({"recovery_command": recovery, "error": str(exc)})) from exc
        if isinstance(exc, CampaignError):
            raise
        raise CampaignError("campaign-%s-not-committed" % action, str(exc)) from exc


def _close_locked(root, path, *, reason=None, before_publish=None):
    state = campaign_state(root, path)
    if state.state == "satisfied":
        _index_update(locator.prepare_index_update, root, state.record["campaign_id"])
        result = _materialize(root, path)
        _index_update(locator.update_indexes, root, state.record["campaign_id"])
        return result
    if state.state != "active":
        raise CampaignError("campaign-not-active", state.state)
    snapshot = _snapshot(root, path)
    criterion = snapshot["campaign"].get("completion_criterion", {}).get("statement")
    # §45 D-127: the reason records why the campaign was judged done; it is never a
    # condition.  Without one the criterion sentence (or `campaign-close`) stands in.
    closure_reason = (reason.strip() if isinstance(reason, str) and reason.strip()
                      else criterion if isinstance(criterion, str) and criterion.strip() else "campaign-close")
    actor_id, closed_by = _agent_actor()
    first = next((row for row in snapshot["cycles"] if row.get("manifest_id")), None)
    if first is None:
        raise CampaignError("campaign-cycle-not-sealed", {
            "cycle_id": snapshot["cycles"][0]["cycle_id"],
            "next_step": "close-one-cycle-of-the-campaign-first"})
    payload = {"contract": CONTRACT, "root": str(root), "snapshot": snapshot,
               "closure": {"reason": closure_reason, "closed_by": closed_by}}
    provenance = {"source_manifest_id": first["manifest_id"],
                  "source_revision_id": first["manifest_revision_id"],
                  "producer_route_id": first["route_id"], "schema_version": 1,
                  "algorithm_version": CONTRACT, "source_digest": digest(snapshot)}
    event = _new_event(root, path, state, "campaign.satisfied",
                       {"kind": "producer", "id": actor_id}, payload, provenance)
    if before_publish is not None:
        before_publish(event)
    return _commit_event(root, path, event, "close")


def close(root, selection, *, reason=None):
    root = Path(root).resolve()
    _reconcile(root)
    path = campaign_path(root, selection)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        return _close_locked(root, path, reason=reason)
    finally:
        admission._release_lock(root, lock)


def completion_head(root, path):
    state = campaign_state(root, path)
    current = lifecycle.read_root_identity(root)
    if current is None:
        raise CampaignError("root-identity-missing")
    return {"artifact_root_id": current.artifact_root_id, "repository_id": current.repository_id,
            "campaign_id": state.record["campaign_id"], "state": state.state,
            "last_sequence": state.last_sequence,
            "last_event_id": state.events[-1][2]["event_id"] if state.events else None,
            "campaign_digest": digest({key: state.record.get(key) for key in
                ("campaign_id", "cycles", "goal", "completion_criterion")})}


def close_for_completion(root, selection, *, intent, before_publish, load_intent=None):
    """Replay one terminal transaction's optional goal decision under the writer lock.

    The transaction records the exact event before publication. A later begin
    cannot reuse an older decision, even if publication preceded interruption.
    The ordinary manual close surface and historical event schema stay intact.
    """
    root = Path(root).resolve()
    _reconcile(root)
    path = campaign_path(root, selection)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        if load_intent is not None:
            intent = load_intent()
        state = campaign_state(root, path)
        prepared = intent.get("event")
        if isinstance(prepared, dict):
            event_id = prepared.get("event_id")
            if any(event.get("event_id") == event_id for _, _, event in state.events):
                # Current projection may already be reopened: materialize that
                # fold, never replay the old close as a fresh event.
                _index_update(locator.prepare_index_update, root, state.record["campaign_id"])
                _materialize(root, path)
                _index_update(locator.update_indexes, root, state.record["campaign_id"])
                _state_line(root, prepared, "close")
                return {"event_id": event_id, "replayed": True}
        if completion_head(root, path) != intent.get("head"):
            return {"skipped": "campaign-advanced", "event_id": None}
        if state.state != "active":
            return {"skipped": "campaign-not-active", "event_id": None}
        if isinstance(prepared, dict):
            _validate_event_row(root, path, prepared, canonical(prepared) + b"\n",
                                state.last_sequence + 1, state.record["campaign_id"], state.stream_id)
            if (prepared.get("event_type") != "campaign.satisfied"
                    or digest(_snapshot(root, path)) != digest(prepared.get("payload", {}).get("snapshot"))):
                return {"skipped": "campaign-advanced", "event_id": None}
            return _commit_event(root, path, prepared, "close")
        return _close_locked(root, path, reason=intent["goal"].get("reason"),
                             before_publish=before_publish)
    finally:
        admission._release_lock(root, lock)


def _reopen_locked(root, path, *, actor_id=None, reason="manual", route_id=None,
                   requested_selection=None):
    state = campaign_state(root, path)
    if state.state != "satisfied":
        raise CampaignError("campaign-event-transition-invalid", "reopen-while-active")
    previous = next(event for _seq, _origin, event in reversed(state.events)
                    if event.get("event_type") == "campaign.satisfied")
    if route_id is not None:
        actor = {"kind": "producer", "id": "route:" + route_id}
        payload = {"contract": CONTRACT, "reason": "cycle-begin",
                   "reopens_event_id": previous["event_id"], "route_id": route_id,
                   "requested_selection": requested_selection or {"by": "campaign_id", "value": state.record["campaign_id"]}}
    else:
        if actor_id is None:
            actor_id, _closed_by = _agent_actor()
        actor = {"kind": "producer", "id": actor_id}
        payload = {"contract": CONTRACT, "reason": reason,
                   "reopens_event_id": previous["event_id"]}
    source = previous.get("provenance", {})
    provenance = {"source_manifest_id": source.get("source_manifest_id"),
                  "source_revision_id": source.get("source_revision_id"),
                  "producer_route_id": route_id or source.get("producer_route_id"),
                  "schema_version": 1, "algorithm_version": CONTRACT,
                  "source_digest": digest(payload)}
    event = _new_event(root, path, state, "campaign.reopened", actor, payload, provenance)
    return _commit_event(root, path, event, "reopen")


def reopen(root, selection, *, reason=None):
    root = Path(root).resolve()
    _reconcile(root)
    path = campaign_path(root, selection)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        return _reopen_locked(root, path, reason=reason or "manual")
    finally:
        admission._release_lock(root, lock)


def _recover_locked(root, path):
    state = campaign_state(root, path)
    if not state.events:
        raise CampaignError("campaign-no-committed-close")
    _index_update(locator.prepare_index_update, root, state.record["campaign_id"])
    result = _materialize(root, path)
    _index_update(locator.update_indexes, root, state.record["campaign_id"])
    return result


def recover(root, selection):
    root = Path(root).resolve()
    path = campaign_path(root, selection)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        return _recover_locked(root, path)
    finally:
        admission._release_lock(root, lock)


def supersede_locked(root, path, *, operation_id, target_root, target_campaign):
    """Append the terminal merge transition under the existing admission lock.

    Replay uses the published operation identity, even if projection was interrupted.
    """
    import artifact_producer as producer
    state = campaign_state(root, path)
    for _, _, event in state.events:
        if event["event_type"] == "campaign.superseded":
            if event["payload"]["operation_id"] != operation_id:
                raise CampaignError("campaign-not-active", path)
            _materialize(root, path)
            _supersede_history(root, path, event)
            return event
    actor, _ = _agent_actor()
    event = _new_event(root, path, state, "campaign.superseded",
                       {"kind": "producer", "id": actor},
                       {"contract": CONTRACT, "operation_id": operation_id, "previous_state": state.state,
                        "canonical": {"artifact_root": str(target_root), "campaign_id": target_campaign}},
                       {"schema_version": 1, "algorithm_version": CONTRACT, "source_root": str(root),
                        "operation_id": operation_id, "source_digest": digest({"operation_id": operation_id})})
    _publish_event(root, path, event)
    _materialize(root, path)
    _supersede_history(root, path, event)
    return event


def _supersede_history(root, path, event):
    import artifact_producer as producer
    line = producer._command_line(command="cycle-move", stamp=event["event_id"], target_type="campaign",
        target_id=event["target_id"], target_path=str(path.parent.relative_to(root)), operation="update", field="state",
        before={"value": event["payload"]["previous_state"]}, after={"value": "superseded"},
        reason="cross-root campaign merge", now=datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00")).timestamp(), by="rule")
    # The automatic projection is a rule; the initiating actor stays in the
    # stream event. Its recorder row stays byte-stable across caller changes.
    line["actor"] = {"by": "rule", "session": None, "harness": None, "route": None, "attempt": None}
    producer._campaign_lines_locked(root, event["target_id"], [line])


CONTRACT_CURRENT = "artifact-campaign-current/v1"
SCHEMA_VERSION_CURRENT = 1


class CampaignReads:
    """Immutable capture used by validation and its exported evidence.

    Files and listings are first-read cached. Final byte and stat checks detect
    replacement, in-place changes and change-and-restore observations without
    changing the bytes already used by the fold. No global reader is patched.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files = {}
        self.listings = {}
        self.changed = set()
        self.nodes = {}
        self.used = set()

    def _path(self, path):
        path = Path(path)
        return path if path.is_absolute() else self.root / path

    def _rel(self, path):
        return self._path(path).relative_to(self.root).as_posix()

    @staticmethod
    def _signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def _stat(self, path):
        try:
            return self._signature(path.lstat())
        except FileNotFoundError:
            return None
        except OSError as exc:
            return ("error", exc.errno)

    def _capture(self, path):
        path = self._path(path)
        rel = self._rel(path)
        self.used.add(("file", rel))
        if rel in self.files:
            return self.files[rel]
        signature = self._stat(path)
        info = {"signature": signature, "raw": None, "error": None}
        self.files[rel] = info
        if signature is None:
            return info
        try:
            _safe(self.root, path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON:
                    raise CampaignError("campaign-input-kind-or-size", path)
                raw = stream.read(MAX_JSON + 1)
                after = os.fstat(stream.fileno())
                if len(raw) > MAX_JSON:
                    raise CampaignError("campaign-input-kind-or-size", path)
                if (self._signature(before) != signature
                        or self._signature(after) != signature):
                    self.changed.add(rel)
            info["raw"] = raw
        except CampaignError as exc:
            info["error"] = exc.code
        except OSError:
            info["error"] = "campaign-input-unreadable"
        if self._stat(path) != signature:
            self.changed.add(rel)
        return info

    def is_missing(self, path):
        rel = self._rel(path)
        info = self.listings.get(rel) or self.files.get(rel)
        return info is not None and info["signature"] is None

    def read_raw(self, root, path):
        info = self._capture(path)
        if info["error"]:
            raise CampaignError(info["error"], path)
        return info["raw"]

    def read_json(self, root, path):
        raw = self.read_raw(root, path)
        if raw is None:
            raise CampaignError("campaign-input-missing", path)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("object required")
        except (ValueError, UnicodeError) as exc:
            raise CampaignError("campaign-input-invalid", path) from exc
        return value, raw

    def read_root_identity(self, root):
        path = Path(root) / admission.ADMISSION_REL / "root-identity.json"
        raw = self.read_raw(root, path)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
                    or payload["schema_version"] != 1):
                raise ValueError("root identity schema")
            if not isinstance(payload.get("producer_contract_version"), str) or not payload["producer_contract_version"]:
                raise ValueError("producer contract version")
            issued = payload.get("issued_at")
            if not isinstance(issued, str):
                raise ValueError("root issuance time")
            issued_time = datetime.fromisoformat(issued.replace("Z", "+00:00"))
            if issued_time.tzinfo is None or issued_time.utcoffset() != timezone.utc.utcoffset(issued_time):
                raise ValueError("root issuance time must be UTC")
            return identity.RootIdentity.parse(payload)
        except (ValueError, UnicodeError, identity.IdentityError) as exc:
            raise CampaignError("root-identity-invalid", path) from exc

    def entries(self, path):
        path = self._path(path)
        rel = self._rel(path)
        self.used.add(("directory", rel))
        if rel not in self.listings:
            signature = self._stat(path)
            info = {"signature": signature, "entries": [], "error": None}
            self.listings[rel] = info
            if signature is not None:
                try:
                    _safe(self.root, path)
                    info["entries"] = sorted(p.name for p in path.iterdir())
                    for name in info["entries"]:
                        child = path / name
                        self.nodes.setdefault(self._rel(child), self._stat(child))
                except CampaignError as exc:
                    info["error"] = exc.code
                except OSError:
                    info["error"] = "campaign-directory-unreadable"
                if self._stat(path) != signature:
                    self.changed.add(rel)
        info = self.listings[rel]
        if info["error"]:
            raise CampaignError(info["error"], path)
        return [path / name for name in info["entries"]]

    def list_dir(self, path):
        return [entry.name for entry in self.entries(path)]

    def cycle_dirs(self, campaign):
        # Same supported layouts as artifact_locator.iter_cycle_dirs.
        for entry in self.entries(campaign):
            if entry.name.startswith(".") or not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name == "cycles":
                for child in self.entries(entry):
                    if not child.name.startswith(".") and child.is_dir() and not child.is_symlink():
                        yield child, "legacy-id"
            elif entry.name != EVENTS_DIR:
                yield entry, "readable"

    def inputs(self, used=None):
        rows = []
        for kind, rel in sorted(self.used if used is None else used):
            info = (self.files if kind == "file" else self.listings)[rel]
            row = {"path": rel}
            if info["signature"] is None:
                row["missing"] = True
            elif info["error"]:
                row["error"] = info["error"]
            elif kind == "directory":
                row.update(entries=info["entries"], listing_sha256="sha256:" +
                           hashlib.sha256(canonical(info["entries"])).hexdigest())
            else:
                raw = info["raw"]
                row.update(sha256="sha256:" + hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            rows.append(row)
        return rows

    def check_mutation(self):
        changed = set(self.changed)
        for rel, signature in self.nodes.items():
            if self._stat(self.root / rel) != signature:
                changed.add(str(Path(rel).parent))
        for rel, info in self.files.items():
            path = self.root / rel
            if self._stat(path) != info["signature"]:
                changed.add(rel)
                continue
            if info["raw"] is not None:
                try:
                    if path.read_bytes() != info["raw"]:
                        changed.add(rel)
                except OSError:
                    changed.add(rel)
        for rel, info in self.listings.items():
            path = self.root / rel
            if self._stat(path) != info["signature"]:
                changed.add(rel)
            elif info["signature"] is not None and not info["error"]:
                try:
                    if sorted(p.name for p in path.iterdir()) != info["entries"]:
                        changed.add(rel)
                except OSError:
                    changed.add(rel)
        return sorted(changed)


def _export_reason(error):
    context = error.context if isinstance(error.context, dict) else {}
    detail = context.get("detail")
    if detail in ("rename-evidence-missing", "rename-binding-mismatch", "rename-evidence-tampered"):
        return "campaign-" + detail
    return error.code


def _export_row_status(code):
    if code in ("campaign-input-missing", "campaign-record-missing", "root-identity-missing", "campaigns-directory-missing", "campaign-rename-evidence-missing"):
        return "missing"
    if code in ("campaign-projection-conflict", "campaign-duplicate-id", "campaign-identity-mismatch", "campaign-rename-binding-mismatch", "input-changed"):
        return "conflict"
    return "invalid"


def export_current(root):
    """Pure root observation, using the shared campaign fold and metadata view."""
    import artifact_meta as meta
    root = Path(root).resolve()
    reads = CampaignReads(root)
    root_reason = None
    try:
        root_identity = reads.read_root_identity(root)
        if root_identity is None:
            root_reason = "root-identity-missing"
    except CampaignError as exc:
        root_identity, root_reason = None, exc.code
    shared = set(reads.used)
    try:
        directories = reads.entries(root / "campaigns")
        if reads.is_missing(root / "campaigns"):
            root_reason = root_reason or "campaigns-directory-missing"
        shared.update(reads.used)
    except CampaignError as exc:
        directories = []
        root_reason = root_reason or exc.code
    rows, seen = [], {}
    for directory in directories:
        if directory.name.startswith("."):
            continue
        # Symlinks and unexpected files must not hide campaigns from the observation.
        if directory.is_symlink():
            root_reason = root_reason or "campaign-directory-invalid"
            continue
        if not directory.is_dir():
            continue  # ordinary index/control files are not campaigns
        reads.used = set(shared)
        record_path = directory / "campaign.json"
        row = {"campaign_id": None, "locator": directory.name, "record_state": None,
               "state": None, "status": "valid", "reason": None, "stream_id": None,
               "last_sequence": 0, "last_event_id": None, "projection_pending": False,
               "presentation_kind": None, "presentation_status": "absent",
               "presentation_reason": None, "inputs": []}
        try:
            reads.entries(directory)
            record, _ = reads.read_json(root, record_path)
            campaign_id = record.get("campaign_id")
            row.update(campaign_id=campaign_id if identity.is_well_formed(campaign_id, "campaign") else None,
                       record_state=record.get("state"))
            folded = campaign_state(root, record_path, record, reads=reads)
            if root_reason:
                raise CampaignError(root_reason)
            if any(key in record and record[key] != expected for key, expected in (
                    ("artifact_root_id", root_identity.artifact_root_id),
                    ("repository_id", root_identity.repository_id))):
                raise CampaignError("campaign-identity-mismatch")
            row.update(state=folded.state, stream_id=folded.stream_id,
                       last_sequence=folded.last_sequence,
                       last_event_id=folded.events[-1][2]["event_id"] if folded.events else None,
                       projection_pending=folded.projection_pending)
        except CampaignError as exc:
            reason = _export_reason(exc)
            row.update(status=_export_row_status(reason), reason=reason)
        try:
            raw = reads.read_raw(root, directory / "meta.json")
            view = meta.presentation_view(raw, root_id=root_identity.artifact_root_id if root_identity else None,
                                          repository_id=root_identity.repository_id if root_identity else None,
                                          campaign_id=row["campaign_id"], members={})
            row.update(view)
        except CampaignError as exc:
            row.update(presentation_status="invalid", presentation_reason=exc.code)
        row["inputs"] = reads.inputs()
        rows.append(row)
        if isinstance(row["campaign_id"], str):
            seen.setdefault(row["campaign_id"], []).append(row)
    for duplicate in seen.values():
        if len(duplicate) > 1:
            for row in duplicate:
                row.update(status="conflict", reason="campaign-duplicate-id", state=None)
    changed = reads.check_mutation()
    if changed:
        root_status, root_reason = "conflict", "input-changed"
        for row in rows:
            if set(changed) & {item["path"] for item in row["inputs"]}:
                row.update(status="conflict", reason="input-changed", state=None)
                if any(item["path"].endswith("/meta.json") and item["path"] in changed for item in row["inputs"]):
                    row.update(presentation_status="invalid", presentation_reason="input-changed")
    elif root_reason:
        root_status = _export_row_status(root_reason)
    else:
        priority = {"valid": 0, "missing": 1, "invalid": 2, "conflict": 3}
        worst = max(rows, key=lambda row: priority[row["status"]], default=None)
        root_status, root_reason = (worst["status"], worst["reason"]) if worst else ("valid", None)
    return {"contract": CONTRACT_CURRENT, "schema_version": SCHEMA_VERSION_CURRENT,
            "artifact_root_id": root_identity.artifact_root_id if root_identity else None,
            "repository_id": root_identity.repository_id if root_identity else None,
            "artifact_root_path": str(root), "status": root_status, "reason": root_reason,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "inputs": reads.inputs(set(("file", rel) for rel in reads.files) |
                                   set(("directory", rel) for rel in reads.listings)),
            "campaigns": sorted(rows, key=lambda row: str(row["campaign_id"] or row["locator"]))}
