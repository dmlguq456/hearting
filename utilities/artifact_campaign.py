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
DEFAULT_COMPLETION_CRITERION = "every cycle sealed with a manifest"
STATE_FIELDS = ("state", "satisfied_on", "satisfaction_event_id")
_RUNTIME_SESSION_ENV = ("CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID")
MAX_JSON = 16 * 1024 * 1024

# A cycle sealed `state: active` (route open at seal time, D-6) whose route has
# since closed. Never written to a record or event; computed for display only,
# so relabeling it later costs nothing against a signed statement.
PROVISIONAL_DISPOSITION = "sealed-unproven"


class CampaignError(Exception):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, str(detail)
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
    return isinstance(record, dict) and record.get("state") not in DETACHED_STATES


def campaign_records(root, campaign_id):
    """Split this campaign's producer records into (members, detached)."""
    records = Path(root) / ".runtime/artifact-producer/v1/cycles"
    members, detached = [], []
    for entry in sorted(records.glob("*.json")):
        record, _ = read_json(root, entry)
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


def read_json(root, path):
    path = _safe(root, path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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


def campaign_path(root, selection):
    root = Path(root).resolve()
    if identity.is_well_formed(str(selection), "campaign"):
        directory = locator.find_path_by_id(root, str(selection))
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


def _validate_v1(root, path, record):
    event_path = path.parent / LEGACY_EVENT_NAME
    if event_path.is_symlink():
        raise CampaignError("campaign-symlink", event_path)
    if not event_path.exists():
        return None
    event, raw = read_json(root, event_path)
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
            or approval.get("actor_id") != actor_id
            or payload.get("root") != str(Path(root).resolve())):
        raise CampaignError("campaign-event-invalid", {"path": str(event_path), "violations": violations[:3]})
    return event


def _read_stream(root, path, record, start_sequence, stream_id):
    directory = path.parent / EVENTS_DIR
    if directory.is_symlink():
        raise CampaignError("campaign-symlink", directory)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise CampaignError("campaign-event-invalid", {"path": str(directory), "detail": "not-directory"})
    rows = []
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("."):
            continue
        match = re.fullmatch(r"(\d{6})\.json", entry.name)
        if not match:
            raise CampaignError("campaign-event-invalid", {"path": str(entry), "detail": "unexpected-entry"})
        sequence = int(match.group(1))
        if entry.is_symlink():
            raise CampaignError("campaign-symlink", entry)
        event, raw = read_json(root, entry)
        _validate_event_row(root, entry, event, raw, sequence, record.get("campaign_id"), stream_id)
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
            raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "payload.contract"})
        event_type = event.get("event_type")
        if event_type == "campaign.satisfied":
            snapshot = payload.get("snapshot")
            closure = payload.get("closure")
            if (set(payload) != {"contract", "root", "snapshot", "closure"}
                    or payload.get("root") != str(Path(root).resolve())
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
        else:
            raise CampaignError("campaign-event-invalid", {"path": str(entry), "field": "event_type"})
        rows.append((sequence, "stream", event))
    expected_numbers = list(range(start_sequence, start_sequence + len(rows)))
    if [row[0] for row in rows] != expected_numbers:
        raise CampaignError("campaign-event-sequence-invalid", "gap-or-duplicate")
    return rows


def campaign_state(root, path, record=None):
    root, path = Path(root).resolve(), Path(path)
    if not path.is_absolute():
        path = root / path
    _safe(root, path)
    if record is None:
        record, _ = read_json(root, path)
    if not isinstance(record, dict):
        raise CampaignError("campaign-input-invalid", path)
    legacy = _validate_v1(root, path, record)
    rows = []
    stream_id = None
    if legacy is not None:
        rows.append((1, "v1", legacy))
        stream_id = legacy.get("stream_id")
    rows.extend(_read_stream(root, path, record, 2 if legacy else 1, stream_id))
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
        if record.get("state") in {"abandoned", "superseded"}:
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


def fold_campaign(root, path, campaign):
    folded = campaign_state(root, path, campaign)
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


def _route_outcome(root, record, document):
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
    outcome, raw = read_json(root, outcome_path)
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
            "then close --route <route_file>; or close --route <route_file> --allow-unproven "
            "(disclosed as unproven); then rerun campaign-status")


def _cycle_rows(root, path, campaign):
    ids = campaign.get("cycles")
    if (not isinstance(ids, list) or not ids or not all(isinstance(cid, str) for cid in ids)
            or len(set(ids)) != len(ids)):
        raise CampaignError("campaign-membership-invalid")
    _validate_runlog(root, path, campaign, ids)
    records = root / ".runtime/artifact-producer/v1/cycles"
    root_id = lifecycle.read_root_identity(root)
    if root_id is None:
        raise CampaignError("root-identity-missing")
    index = admission.load_index(root)
    if index.artifact_root_id != root_id.artifact_root_id:
        raise CampaignError("campaign-index-root-mismatch")
    # A campaign list alone cannot hide an open member or an unregistered tree.
    # Detached records (see `is_member_record`) are reported, never counted.
    member_records, detached = campaign_records(root, campaign["campaign_id"])
    members = {record.get("cycle_id") for record in member_records}
    if members != set(ids):
        raise CampaignError("campaign-membership-drift")
    directories = {}
    for entry, layout in locator.iter_cycle_dirs(path.parent):
        _safe(path.parent, entry)
        if (entry / ".cycle.json").exists():
            binding, _ = read_json(root, entry / ".cycle.json")
        elif (entry / "manifest.json").exists():
            # Historical sealed cycles predate .cycle.json. Their immutable
            # manifest plus producer record and index still prove the binding.
            document, _ = read_json(root, entry / "manifest.json")
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
        record, _ = read_json(root, records / (cid + ".json"))
        if record.get("state") not in {"sealed", "superseded"} or not record.get("sealed_on"):
            raise CampaignError("campaign-cycle-not-sealed", cid)
        directory = directories[cid]
        _safe(path.parent, directory)
        expected_directory = (path.parent / str(record["locator"]) if record.get("locator")
                              else path.parent / "cycles" / cid)
        if directory != expected_directory:
            raise CampaignError("campaign-cycle-locator-invalid", cid)
        document, raw = read_json(root, directory / "manifest.json")
        report = manifest.validate(document)
        if not report.ok:
            raise CampaignError("campaign-manifest-invalid", cid + ": " + str(report.violations[:3]))
        # The stored/indexed seal binds bytes. Legacy approved merges retained
        # noncanonical JSON formatting; never rewrite them to today's encoder.
        mdigest = "sha256:" + hashlib.sha256(raw).hexdigest()
        cycle = document["cycle"]
        if (record.get("manifest_digest") != mdigest
                or cycle.get("cycle_id") != cid or cycle.get("campaign_id") != campaign["campaign_id"]
                or document["campaign"]["campaign_id"] != campaign["campaign_id"]
                or document["artifact_root_id"] != root_id.artifact_root_id
                or document["producer"]["producer_id"] != record.get("producer_id")
                or cycle["state"] not in {"completed", "abandoned", "active"}
                or cycle["state"] != record.get("cycle_state")
                or (cycle["state"] == "active" and record.get("state") != "sealed")):
            raise CampaignError("campaign-seal-mismatch", cid)
        expected = index_module.apply(index_module.empty(root_id.artifact_root_id), document,
                                      cycle_path=str(directory.relative_to(root)),
                                      manifest_digest=manifest.manifest_digest(document), idempotency_key=cid)
        if (index.manifests.get(cid) != expected.manifests[cid]
                or index.cycles.get(cid) != expected.cycles[cid]):
            raise CampaignError("campaign-index-mismatch", cid)
        for revision in document["artifact_revisions"]:
            _safe(directory, directory / revision["locator"]["path"])
        failures = lifecycle.verify_artifact_revisions(document, directory)
        if failures:
            raise CampaignError("campaign-artifact-mismatch", cid + ": " + ";".join(failures[:5]))
        row = {"cycle_id": cid, "state": cycle["state"], "manifest_digest": mdigest,
               "index_digest": manifest.manifest_digest(document),
               "route_id": record["route_id"],
               "manifest_id": document["manifest_id"],
               "manifest_revision_id": document["manifest_revision_id"]}
        if cycle["state"] == "active":
            outcome = _route_outcome(root, record, document)
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


def _snapshot(root, path):
    campaign, _ = read_json(root, path)
    effective = fold_campaign(root, path, campaign)
    if effective.get("state") != "active":
        raise CampaignError("campaign-not-active", effective.get("state"))
    criterion = campaign.get("completion_criterion", {}).get("statement")
    if (not isinstance(criterion, str) or not criterion.strip()
            or not isinstance(campaign.get("goal"), str) or not campaign["goal"].strip()
            or not identity.is_well_formed(campaign.get("campaign_id"), "campaign")):
        raise CampaignError("campaign-criterion-missing")
    root_id, rows = _cycle_rows(root, path, campaign)
    return {"artifact_root_id": root_id.artifact_root_id, "root": str(root),
            "campaign": campaign, "cycles": rows}


def status(root, selection):
    root = Path(root).resolve()
    path = campaign_path(root, selection)
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
                "satisfied": False, "closable": False,
                "close_refusal": {"reason": "campaign-not-active"}, "events": event_rows}
    try:
        snapshot = _snapshot(root, path)
        criterion = record.get("completion_criterion", {}).get("statement")
        required = criterion == DEFAULT_COMPLETION_CRITERION
        result = {"status": "active", "state": "active", "campaign_id": record["campaign_id"],
                  "goal": record.get("goal"), "completion_criterion": record.get("completion_criterion"),
                  "cycles": [{**row, "disposition": disposition(row)} for row in snapshot["cycles"]],
                  "detached_cycles": detached_rows(campaign_records(root, record["campaign_id"])[1]),
                  "satisfied": False, "closable": True, "reason_required": required,
                  "close_command": ["python3", str(Path(__file__).with_name("artifact_producer.py")),
                                    "campaign-close", "--artifact-root", str(root), "--campaign", str(path)]
                                   + (["--reason", "<one-sentence completion condition>"] if required else []),
                  "events": event_rows, "projection_pending": folded.projection_pending}
        unproven = [row for row in result["cycles"] if row["disposition"] == PROVISIONAL_DISPOSITION]
        if unproven:
            result["unproven_cycles"] = {"count": len(unproven),
                                         "without_terminal_proof": sum(row.get("terminal_gate_proven") is not True for row in unproven)}
        return result
    except CampaignError as exc:
        return {"status": "active", "state": "active", "campaign_id": record["campaign_id"],
                "satisfied": False, "closable": False,
                "close_refusal": {"reason": exc.code, "detail": exc.detail}, "events": event_rows}



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
    harness = os.environ.get("AGENT_HARNESS")
    session = None
    if not harness:
        for key, name in (("CLAUDE_SESSION_ID", "claude"), ("CODEX_THREAD_ID", "codex"),
                          ("OPENCODE_SESSION_ID", "opencode")):
            if os.environ.get(key):
                harness, session = name, os.environ[key]
                break
    if session is None and harness:
        session = next((os.environ.get(key) for key in _RUNTIME_SESSION_ENV if os.environ.get(key)), None)
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


def _commit_event(root, path, event, action):
    campaign_id = event["target_id"]
    _index_update(locator.prepare_index_update, root, campaign_id)
    try:
        _publish_event(root, path, event)
        result = _materialize(root, path)
        _index_update(locator.update_indexes, root, campaign_id)
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


def _close_locked(root, path, *, reason=None):
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
    if criterion == DEFAULT_COMPLETION_CRITERION and (not isinstance(reason, str) or not reason.strip()):
        raise CampaignError("campaign-close-reason-required")
    closure_reason = reason.strip() if isinstance(reason, str) and reason.strip() else criterion
    actor_id, closed_by = _agent_actor()
    first = snapshot["cycles"][0]
    payload = {"contract": CONTRACT, "root": str(root), "snapshot": snapshot,
               "closure": {"reason": closure_reason, "closed_by": closed_by}}
    provenance = {"source_manifest_id": first["manifest_id"],
                  "source_revision_id": first["manifest_revision_id"],
                  "producer_route_id": first["route_id"], "schema_version": 1,
                  "algorithm_version": CONTRACT, "source_digest": digest(snapshot)}
    event = _new_event(root, path, state, "campaign.satisfied",
                       {"kind": "producer", "id": actor_id}, payload, provenance)
    return _commit_event(root, path, event, "close")


def close(root, selection, *, reason=None):
    root = Path(root).resolve()
    path = campaign_path(root, selection)
    lock = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT)
    try:
        return _close_locked(root, path, reason=reason)
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
