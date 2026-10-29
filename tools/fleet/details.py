"""Live display detail fills joined to exact basic observations.

The ordinary snapshot pump owns existence/liveness/resources. This module runs
on one coalesced RefreshPump worker and can neither create rows nor change those
observations. A slow filesystem read here leaves basic refresh independent.
"""
from dataclasses import dataclass, replace
from collections.abc import Mapping
import copy
import hashlib
import json
import math
import re
import time

from .model import (ActiveNodeProjection, ContextProjection, ProgressProjection,
                    Session, WorkProjection)
from .refresh import _observation_identity, required_details_complete, set_required_details

_METADATA = ("campaign_label", "location_kind", "location_wt", "location_repo",
             "branch", "worktree_count", "branch_ahead", "branch_behind", "qa", "qa_source",
             "peer_last_recv", "peer_last_sent", "steward")
# The steward relation lists travel with the scalar flag from the same optional
# pass. They are display-only and never enter the scalar continuation payload.
_STEWARD_RELATIONS = ("steward_targets", "steward_parents")
_PEER_FIELDS = ("peer_last_recv", "peer_last_sent", "peer_sent_1h",
                "peer_recv_1h", "peer_obligations")


def identity(row):
    # Include process start even for a stable session/attempt: PID reuse must
    # never receive an old title, native NOW, or route projection.
    return ("session" if isinstance(row, Session) else "job",
            getattr(row, "harness", None), getattr(row, "session_id", None),
            getattr(row, "attempt_id", None), getattr(row, "pid", None),
            getattr(row, "proc_start", None), getattr(row, "cwd", None),
            getattr(row, "source", None))


def binding(row):
    return tuple(getattr(row, field, None) for field in (
        "route_id", "route_hash", "route_file", "route_node", "owner_route_id",
        "owner_route_hash", "owner_route_file", "parent_attempt_id", "parent_sid",
        "parent_harness", "parent_slug", "_runtime_session_id"))


def _observed_wait(row):
    observed = ((getattr(row, "state_evidence", None) or {}).get("inputs") or {}).get(
        "observed_liveness")
    stall = getattr(row, "stall_observation", None)
    stall = ({key: stall.get(key) for key in (
        "state", "reason", "attempt_id", "route_id", "route_node",
        "last_progress_at", "episode_id", "delivery_id", "notice_digest")}
        if isinstance(stall, dict) else None)
    return json.dumps((observed, getattr(row, "resource_wait", None),
                       getattr(row, "attention_reason", None), stall, getattr(row, "note", None),
                       getattr(row, "resume_boundary", None)), sort_keys=True, default=str)


def _activity(row):
    # Session observations have no attempt-log stamp. Their native update and
    # context sequence still distinguish current data from a copied old child.
    evidence = getattr(row, "_context_evidence", None)
    return (getattr(row, "_detail_activity_key", None), getattr(row, "updated_at", None),
            getattr(row, "summary_ts", None), getattr(evidence, "sequence", None),
            json.dumps((getattr(row, "exec_child", None), getattr(row, "exec_tool", None),
                        getattr(row, "shell_jobs", None),
                        getattr(row, "shell_job_observation", None)),
                       sort_keys=True, default=str))


def state_key(snapshot):
    """Projection authority can transfer only while the observed roster agrees.

    Activity timestamps/progress text do not gate titles or route details. Live
    job/resource changes and terminal registry evidence do gate route projection.
    """
    rows = tuple((identity(row), binding(row), getattr(row, "liveness", None), _observed_wait(row))
                 for row in snapshot.sessions + snapshot.jobs)
    resources = tuple((r.run_id, r.parent_attempt_id, r.liveness,
                       r.route_id, r.route_hash, r.route_node, r.node,
                       r.pid, r.starttime, r.command_hash, r.exit_code, r.route_file, r.route) for r in snapshot.resources)
    evidence = {rid: json.dumps(value, sort_keys=True, default=str)
                for rid, value in snapshot.node_evidence.items()}
    return {"rows": rows, "resources": resources, "evidence": evidence,
            "activity": {identity(row): _activity(row) for row in snapshot.sessions + snapshot.jobs}}


def _projection_neighbourhood(row, previous, state):
    if not isinstance(state, dict):
        return None
    routes = {getattr(previous.work_projection, "route_id", None), row.route_id,
              getattr(row, "owner_route_id", None)}
    routes.update(node.get("route_id") for node in
                  (getattr(previous, "route_chain", None) or {}).get("nodes", ()))
    routes.discard(None)
    sid, attempt, slug = (getattr(row, "session_id", None),
                          getattr(row, "attempt_id", None), getattr(row, "slug", None))

    observed = [item for item in state["rows"] if item[0] == identity(row)
                or item[1][0] in routes or item[1][4] in routes
                or (sid and item[1][8] == sid)
                or (attempt and item[1][7] == attempt)
                or (slug and item[1][10] == slug)]
    attempts = {item[0][3] for item in observed if item[0][3]}
    resources = [item for item in state["resources"]
                 if item[1] in attempts or item[3] in routes]
    evidence = {rid: state["evidence"].get(rid) for rid in routes}
    return sorted(observed, key=repr), sorted(resources, key=repr), evidence


def _fingerprint(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def projection_current(row, previous, current, source):
    """Judge only this row's route/ownership neighbourhood, not unrelated work."""
    current_scope = _projection_neighbourhood(row, previous, current)
    old_scope = _projection_neighbourhood(row, previous, source)
    return current_scope is not None and old_scope is not None and current_scope == old_scope


@dataclass
class DetailSnapshot:
    source_key: object
    snapshot: object
    continuation_complete: bool = False
    continued_count: int = 0


@dataclass(frozen=True)
class RestoredContinuation:
    payload: dict


_CONTINUATION_SCHEMA = 1
_CONTINUATION_FIELD_KEYS = frozenset({
    *_METADATA,
    "title", "title_ts", "summary", "summary_ts", "context", "stage",
    "resource_wait", "route_chain", "work_projection",
})
_WORK_PROJECTION_FIELDS = (
    "route_id", "route_hash", "route_node", "attempt_id", "assigned_contract",
    "unit", "stage_label", "node_state", "scope_node_ids", "ambiguity",
)
_ACTIVE_NODE_FIELDS = (
    "id", "depends_on", "level", "unit", "unit_choices", "gate", "state",
    "progress", "parallel_group", "replica_group", "model_profile", "perspective",
)
_ROUTE_CHAIN_FIELDS = ("v", "visible", "plan", "plan_source", "nodes", "current", "folded_done")
_ROUTE_NODE_FIELDS = (
    "label", "capability", "capability_mode", "route_id", "state", "shape",
    "intensity", "round", "handoff_to", "handoff_from", "ambiguity", "ts",
    "source_route_id", "worker_stage", "model_profile", "mode",
)


def _safe_continuation_scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        from .explorer.view_model import _safe_text
        return _safe_text(value, limit=512)
    return None


def _safe_route_node(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in _ROUTE_NODE_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key in ("handoff_to", "handoff_from") and isinstance(item, dict):
            handoff = {name: _safe_continuation_scalar(item.get(name))
                       for name in ("harness", "session_id")
                       if item.get(name) is not None}
            if handoff:
                result[key] = handoff
        elif key == "ambiguity" and isinstance(item, (tuple, list)):
            result[key] = [text for part in item[:16]
                           if (text := _safe_continuation_scalar(part)) is not None]
        elif key in ("round", "ts"):
            clean = _safe_continuation_scalar(item)
            if clean is not None:
                result[key] = clean
        else:
            clean = _safe_continuation_scalar(item)
            if clean is not None:
                result[key] = clean
    return result


def _safe_route_chain(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in _ROUTE_CHAIN_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key == "nodes":
            result[key] = [node for part in (item if isinstance(item, (list, tuple)) else ())[:16]
                           if (node := _safe_route_node(part)) is not None]
        elif key == "current":
            result[key] = _safe_route_node(item) if isinstance(item, dict) else None
        elif key == "plan":
            result[key] = [text for part in (item if isinstance(item, (list, tuple)) else ())[:12]
                           if (text := _safe_continuation_scalar(part)) is not None]
        else:
            clean = _safe_continuation_scalar(item)
            if clean is not None:
                result[key] = clean
    return result


def _safe_work_projection(value):
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        return None
    result = {}
    for key in _WORK_PROJECTION_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key == "scope_node_ids" and isinstance(item, (tuple, list)):
            result[key] = [text for part in item[:64]
                           if (text := _safe_continuation_scalar(part)) is not None]
        elif key == "ambiguity":
            parts = item if isinstance(item, (tuple, list)) else (() if item is None else (item,))
            result[key] = [text for part in parts[:16]
                           if (text := _safe_continuation_scalar(part)) is not None]
        elif key == "active_nodes":
            # Older/public adapters may include active nodes in their mapping.
            result[key] = item
        else:
            clean = _safe_continuation_scalar(item)
            if clean is not None:
                result[key] = clean
    active_nodes = value.get("active_nodes")
    if isinstance(active_nodes, (list, tuple)):
        safe_nodes = []
        for node in active_nodes[:64]:
            if hasattr(node, "to_dict"):
                node = node.to_dict()
            if not isinstance(node, dict):
                continue
            clean_node = {}
            for key in _ACTIVE_NODE_FIELDS:
                item = node.get(key)
                if key in ("depends_on", "unit_choices") and isinstance(item, (tuple, list)):
                    clean_node[key] = [text for part in item[:32]
                                       if (text := _safe_continuation_scalar(part)) is not None]
                elif key == "progress" and isinstance(item, dict):
                    clean_node[key] = {name: clean for name in ("done", "total")
                                       if (clean := _safe_continuation_scalar(item.get(name))) is not None}
                else:
                    clean = _safe_continuation_scalar(item)
                    if clean is not None:
                        clean_node[key] = clean
            if not isinstance(clean_node.get("id"), str):
                continue
            safe_nodes.append(clean_node)
        result["active_nodes"] = safe_nodes
    progress = value.get("progress")
    if hasattr(progress, "to_dict"):
        progress = progress.to_dict()
    if isinstance(progress, dict):
        result["progress"] = {name: clean for name in ("done", "total")
                              if (clean := _safe_continuation_scalar(progress.get(name))) is not None}
    return result


def _continuation_fields(row):
    fields = {}
    for name in _METADATA + ("title", "title_ts", "summary", "summary_ts", "stage", "resource_wait"):
        value = getattr(row, name, None)
        clean = _safe_continuation_scalar(value)
        if clean is not None:
            fields[name] = clean
    context = getattr(row, "context", None)
    if context is not None:
        used = _safe_continuation_scalar(getattr(context, "used_pct", None))
        band = _safe_continuation_scalar(getattr(context, "band", None))
        source = _safe_continuation_scalar(getattr(context, "source", None))
        fields["context"] = [used, band, source]
    projection = _safe_work_projection(getattr(row, "work_projection", None))
    if projection is not None:
        fields["work_projection"] = projection
    chain = _safe_route_chain(getattr(row, "route_chain", None))
    if chain is not None:
        fields["route_chain"] = chain
    return fields


def _associated_child(row, sessions):
    process = (getattr(row, "harness", None), getattr(row, "pid", None),
               getattr(row, "proc_start", None))
    sid = getattr(row, "_runtime_session_id", None)
    return [child for child in sessions if child is not row and getattr(child, "is_child", False) and
            ((all(part is not None for part in process) and
              (child.harness, child.pid, child.proc_start) == process)
             or (sid and row.harness == child.harness and sid == child.session_id))]


def _child_witness(row, sessions, activities, expected=None):
    associated = (bool(expected.get("associated")) if isinstance(expected, dict)
                  else bool(getattr(row, "_child_session_associated", False)))
    matches = _associated_child(row, sessions)
    if not associated:
        return {"associated": False} if not matches else None
    if len(matches) != 1:
        return None
    child = matches[0]
    child_identity = _fingerprint(identity(child))
    if expected is None:
        activity = activities.get(identity(child)) if isinstance(activities, dict) else None
        if activity is None:
            return None
        return {"associated": True, "identity": child_identity,
                "activity": _fingerprint(activity)}
    activity = activities.get(identity(child)) if isinstance(activities, dict) else None
    if (child_identity != expected.get("identity") or activity is None
            or _fingerprint(activity) != expected.get("activity")):
        return None
    return expected


def _valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_continuation(payload):
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "captured_at", "rows"}:
        return False
    stamp = payload.get("captured_at")
    rows = payload.get("rows")
    if (isinstance(payload.get("schema_version"), bool)
            or payload.get("schema_version") != _CONTINUATION_SCHEMA
            or isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp) or not isinstance(rows, list) or len(rows) > 512):
        return False
    expected = {"identity", "binding", "liveness", "activity", "child_activity",
                "route_neighbourhood", "fields"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            return False
        if not all(_valid_hash(row.get(key)) for key in
                   ("identity", "binding", "activity", "route_neighbourhood")):
            return False
        if row.get("liveness") is not None and _safe_continuation_scalar(row["liveness"]) != row["liveness"]:
            return False
        child = row.get("child_activity")
        if not isinstance(child, dict) or child.get("associated") not in (True, False):
            return False
        if child["associated"] and (set(child) != {"associated", "identity", "activity"}
                                     or not all(_valid_hash(child.get(key))
                                                for key in ("identity", "activity"))):
            return False
        if not child["associated"] and set(child) != {"associated"}:
            return False
        fields = row.get("fields")
        if (not isinstance(fields, dict) or len(fields) > len(_CONTINUATION_FIELD_KEYS)
                or set(fields) - _CONTINUATION_FIELD_KEYS):
            return False
        if "context" in row["fields"] and (
                not isinstance(row["fields"]["context"], list)
                or len(row["fields"]["context"]) != 3):
            return False
        if "work_projection" in row["fields"] and not isinstance(row["fields"]["work_projection"], dict):
            return False
        if "route_chain" in row["fields"] and not isinstance(row["fields"]["route_chain"], dict):
            return False
        if any(not _valid_continuation_value(value) for value in fields.values()):
            return False
    return True


def _valid_continuation_value(value, depth=0):
    if depth > 8:
        return False
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= 2**53
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value) <= 512 and _safe_continuation_scalar(value) == value
    if isinstance(value, list):
        return len(value) <= 512 and all(_valid_continuation_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return (len(value) <= 128 and all(isinstance(key, str) and key not in {
            "source", "source_records", "raw", "snapshot", "dialogue", "config",
            "environment", "secret", "token", "password", "transcript", "pane",
        } and _valid_continuation_value(item, depth + 1) for key, item in value.items()))
    return False


def export_continuation(detail):
    """Export only bounded display values and hashed evidence from an all-new pass."""
    if not isinstance(detail, DetailSnapshot):
        return None
    snapshot, source = detail.snapshot, detail.source_key
    observations = getattr(snapshot, "observations", None)
    required = observations.get("required-details") if isinstance(observations, dict) else None
    if (not isinstance(source, dict) or not isinstance(required, dict)
            or str(required.get("state", "")).lower() not in ("ok", "complete", "completed", "success")
            or required.get("all_new", True) is not True):
        return None
    rows = []
    source_activity = source.get("activity", {})
    for row in snapshot.sessions + snapshot.jobs:
        captured_activity = source_activity.get(identity(row))
        if captured_activity is None:
            return None
        witness = _child_witness(row, snapshot.sessions, source_activity)
        if witness is None:
            return None
        rows.append({
            "identity": _fingerprint(identity(row)),
            "binding": _fingerprint(binding(row)),
            "liveness": _safe_continuation_scalar(getattr(row, "liveness", None)),
            "activity": _fingerprint(captured_activity),
            "child_activity": witness,
            "route_neighbourhood": _fingerprint(_projection_neighbourhood(row, row, source)),
            "fields": _continuation_fields(row),
        })
    observed_at = required.get("observed_at", time.time())
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at):
        observed_at = time.time()
    payload = {"schema_version": _CONTINUATION_SCHEMA,
               "captured_at": float(observed_at), "rows": rows}
    if not _valid_continuation(payload):
        return None
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return payload if len(encoded) <= 256 * 1024 else None


def _restore_work_projection(value):
    if not isinstance(value, dict):
        return None
    fields = {key: value[key] for key in _WORK_PROJECTION_FIELDS if key in value}
    active = []
    for item in value.get("active_nodes", ()):
        if not isinstance(item, dict):
            continue
        values = {key: item[key] for key in _ACTIVE_NODE_FIELDS if key in item}
        for key in ("depends_on", "unit_choices"):
            if key in values:
                values[key] = tuple(values[key])
        if isinstance(values.get("progress"), dict):
            values["progress"] = ProgressProjection(**values["progress"])
        active.append(ActiveNodeProjection(**values))
    fields["active_nodes"] = tuple(active)
    progress = value.get("progress")
    fields["progress"] = ProgressProjection(**progress) if isinstance(progress, dict) else None
    for key in ("scope_node_ids",):
        if key in fields:
            fields[key] = tuple(fields[key])
    ambiguity = fields.get("ambiguity")
    if isinstance(ambiguity, list):
        fields["ambiguity"] = "; ".join(ambiguity) if ambiguity else None
    return WorkProjection(**fields)


def restore_continuation(payload):
    """Validate a persisted DTO without constructing a LiveSnapshot or source object."""
    if not _valid_continuation(payload):
        raise ValueError("invalid detail continuation")
    return RestoredContinuation(payload=copy.deepcopy(payload))


def merge_continuation(basic, restored):
    """Apply only rows whose complete fresh identity and local evidence still match."""
    if not isinstance(restored, RestoredContinuation):
        return None
    payload = restored.payload
    current_state = state_key(basic)
    saved = {}
    for item in payload["rows"]:
        saved.setdefault(item["identity"], []).append(item)
    value = copy.copy(basic)
    value.sessions, value.jobs = [], []
    total = len(basic.sessions) + len(basic.jobs)
    accepted = 0
    current_counts = {}
    for row in basic.sessions + basic.jobs:
        key = _fingerprint(identity(row))
        current_counts[key] = current_counts.get(key, 0) + 1
    for target, source_rows in ((value.sessions, basic.sessions), (value.jobs, basic.jobs)):
        for row in source_rows:
            current = copy.copy(row)
            identity_key = _fingerprint(identity(row))
            candidates = saved.get(identity_key, ())
            if current_counts.get(identity_key) == 1 and len(candidates) == 1:
                item = candidates[0]
                previous = copy.copy(row)
                fields = item["fields"]
                for name in fields:
                    if name == "work_projection":
                        previous.work_projection = _restore_work_projection(fields[name])
                    elif name == "route_chain":
                        previous.route_chain = fields[name]
                    elif name == "context":
                        context = fields[name]
                        previous.context = ContextProjection(used_pct=context[0], band=context[1],
                                                            source=context[2])
                    else:
                        setattr(previous, name, fields[name])
                activity = current_state.get("activity", {}).get(identity(row))
                child = _child_witness(row, basic.sessions, current_state.get("activity", {}),
                                       item["child_activity"])
                valid = (
                    item["binding"] == _fingerprint(binding(row))
                    and item["liveness"] == _safe_continuation_scalar(getattr(row, "liveness", None))
                    and activity is not None and item["activity"] == _fingerprint(activity)
                    and child is not None
                    and item["route_neighbourhood"] == _fingerprint(
                        _projection_neighbourhood(row, previous, current_state))
                )
                if valid:
                    for name in fields:
                        setattr(current, name, getattr(previous, name))
                    current._child_session_associated = item["child_activity"]["associated"]
                    current._details_pending = False
                    current._required_detail_error = False
                    current._continuation_validated = True
                    accepted += 1
            target.append(current)
    complete = accepted == total
    value._continuation_source_key = current_state
    if complete:
        set_required_details(value, "continued", all_new=False,
                             observed_at=payload["captured_at"], continuation_validated=True)
    else:
        set_required_details(value, "partial", reason="continuation-row-not-validated",
                             all_new=False, observed_at=time.time())
    return DetailSnapshot(source_key=current_state, snapshot=value,
                          continuation_complete=complete, continued_count=accepted)


def _strip_report_verification(snapshot):
    for row in snapshot.sessions + snapshot.jobs + list(snapshot.route_entities or ()):
        work = getattr(row, "work_projection", None)
        if work is not None and getattr(work, "report_verification", None) is not None:
            row.work_projection = replace(work, report_verification=None)


def _copy_for_enrichment(snapshot, *, required_only):
    """Copy only the mutable required-detail graph for the first completion pass."""
    if not required_only:
        return copy.deepcopy(snapshot)
    value = copy.copy(snapshot)
    # Required projection reads resource rows and attaches them to copied jobs,
    # but does not mutate the resource observations. Keep those immutable rows
    # shared so a large host census is not deep-copied just to enrich a few jobs.
    memo = {id(resource): resource for resource in (snapshot.resources or ())}
    value.sessions, value.jobs, value.route_entities = copy.deepcopy(
        (snapshot.sessions, snapshot.jobs, snapshot.route_entities), memo)
    value.observations = dict(snapshot.observations or {})
    return value


def _apply_peer_observation(collect, sessions):
    """Apply peer rows only when the collector can attest to a complete scan."""
    import inspect

    try:
        parameters = inspect.signature(collect).parameters.values()
        supports_health = any(parameter.name == "include_health"
                              or parameter.kind == inspect.Parameter.VAR_KEYWORD
                              for parameter in parameters)
    except (TypeError, ValueError):
        supports_health = True
    try:
        result = collect(include_health=True) if supports_health else collect()
        if isinstance(result, tuple) and len(result) == 2:
            peer, health = result
            state = health.get("state") if isinstance(health, Mapping) else getattr(health, "state", None)
            state = str(state or "unknown").lower()
            if state not in ("success", "failed", "unknown"):
                state = "unknown"
        else:
            peer, state = result, "unknown"
        if not isinstance(peer, Mapping):
            return "failed"
        records, by_session = peer.get("records"), peer.get("by_session")
        if not isinstance(records, (list, tuple)) or not isinstance(by_session, Mapping):
            return "failed"
        if not (isinstance(result, tuple) and len(result) == 2):
            # Compatibility for older collectors and mapping-only test doubles:
            # positive rows are useful; an empty mapping cannot prove a clear.
            state = "success" if records or by_session else "unknown"
        if state != "success":
            return state

        fields = _PEER_FIELDS
        before = [{field: copy.deepcopy(getattr(session, field, None)) for field in fields}
                  for session in sessions]
        try:
            for session in sessions:
                session.peer_last_recv = None
                session.peer_last_sent = None
                session.peer_sent_1h = 0
                session.peer_recv_1h = 0
                session.peer_obligations = []
            from .collectors import apply_peer_rows
            apply_peer_rows(sessions, dict(by_session))
        except Exception:
            for session, values in zip(sessions, before):
                for field, value in values.items():
                    setattr(session, field, value)
            return "failed"
        return "success"
    except Exception:
        return "failed"


def observe_peers(snapshot):
    """Read only peer detail on copied rows, independently of optional metadata work."""
    from .collectors import peer_messages
    value = copy.copy(snapshot)
    value.sessions = [copy.copy(row) for row in snapshot.sessions]
    state = _apply_peer_observation(peer_messages.collect, value.sessions)
    value.observations = dict(snapshot.observations or {})
    value.observations["optional:peers"] = {
        "state": state, "observed_at": time.time(),
        "roster": tuple((identity(row), binding(row)) for row in value.sessions),
    }
    return DetailSnapshot(state_key(snapshot), value)


def _enrich(snapshot, *, include_report_verification=True, required=True,
            required_only=False):
    """Fill copied rows with display detail; never rescan or reclassify work."""
    from .collectors import dispatch, _adopt_child_titles
    from . import projection, route_chain, titles
    source_key = getattr(snapshot, "_continuation_source_key", None) or state_key(snapshot)
    continued = {identity(row) for row in snapshot.sessions + snapshot.jobs
                 if getattr(row, "_continuation_validated", False)}
    value = _copy_for_enrichment(snapshot, required_only=required_only)
    sessions, jobs = value.sessions, value.jobs
    detail_sessions = [row for row in sessions if identity(row) not in continued]
    detail_jobs = [row for row in jobs if identity(row) not in continued]
    route_entities = list(value.route_entities or ())
    detail_entities = detail_sessions + detail_jobs + route_entities
    dispatch._fill_locations(detail_jobs)
    if not required_only:
        dispatch._campaign_labels(jobs)
    for job in detail_jobs:
        job._details_error = False
        if required:
            job._required_detail_error = False
        try:
            dispatch._note_home_view(job)
        except Exception:
            job._details_error = True
        if required:
            try:
                dispatch._enrich_claude_stream_session(job)
                dispatch._enrich_codex_attempt_session(job)
                dispatch._enrich_opencode_attempt_session(job)
                dispatch._enrich_attempt_summary(job)
            except Exception:
                job._details_error = True
                job._required_detail_error = True
        try:
            if job.qa_source in (None, "default") and (job.liveness != "dead" or job.afterglow
                    or getattr(job, "_dead_terminal_owner", False)):
                job.qa, job.qa_source = dispatch.effective_qa(
                    None, None, job.cwd, job.slug, job.key,
                    getattr(job, "capability_owner", None), job.worker_role,
                    artifact_root=job.artifact_root, job=job)
        except Exception:
            job._details_error = True
    if not required_only:
        _adopt_child_titles(sessions, jobs)
        titles.annotate(sessions + jobs)
    if required:
        route_chain_ready = True
        if detail_sessions or not continued:
            try:
                route_chain.enrich(
                    sessions, jobs=jobs, node_evidence=value.node_evidence, now=time.time(),
                    only_sessions=detail_sessions if continued else None)
            except Exception:
                route_chain_ready = False
        degradations_ready = True
        try:
            routes = set()
            for entity in detail_entities:
                for field in ("route_id", "owner_route_id"):
                    route_id = getattr(entity, field, None)
                    if route_id:
                        routes.add(route_id)
                work = getattr(entity, "work_projection", None)
                route_id = getattr(work, "route_id", None)
                if route_id:
                    routes.add(route_id)
                chain = getattr(entity, "route_chain", None) or {}
                routes.update(node.get("route_id") for node in chain.get("nodes", ())
                              if isinstance(node, dict) and node.get("route_id"))
                neighbourhood = _projection_neighbourhood(entity, entity, source_key)
                if neighbourhood is not None:
                    routes.update(neighbourhood[2])
            degradations = dispatch._scan_degradations(
                (routes if continued else
                 set(value.node_evidence) | {j.route_id for j in jobs if j.route_id}),
                jobs=jobs)
        except Exception:
            degradations_ready = False
            degradations = value.degradations or {}
        projection_ready = True
        try:
            projection.attach_projections(
                sessions, jobs, node_evidence=value.node_evidence,
                degradations=degradations, resources=value.resources, now=time.time(),
                include_report_verification=include_report_verification,
                only_entities=detail_entities if continued else None)
        except Exception:
            projection_ready = False
        detail_ready = (route_chain_ready and degradations_ready and projection_ready
                        and projection.required_projection_details_complete(
                            sessions + jobs + route_entities))
        set_required_details(
            value, "ok" if detail_ready else "partial",
            reason=None if detail_ready else "required-detail-source-incomplete",
            all_new=detail_ready and not continued, observed_at=time.time())
        value.degradations = degradations
    else:
        # The complete basic pass already owns current routes and summaries.
        # This pass only fills optional display metadata below.
        pass
    if not required_only:
        from .work_titles import annotate
        annotate(sessions, jobs, value.resources)
    # Optional header details retain their last observed value across cheap
    # ticks. Initial absence stays unknown; these observers never classify work.
    if not required_only:
        try:
            value.pending_delivery = dispatch._pending_delivery_counts(
                dispatch._candidate_jobs_paths())
        except Exception:
            value.pending_delivery = None
        try:
            from .collectors import steward
            steward.enrich(sessions)
            steward_state = "success"
        except Exception:
            steward_state = "failed"
        try:
            from .collectors import peer_messages
            peer_state = _apply_peer_observation(peer_messages.collect, sessions)
        except Exception:
            peer_state = "failed"
        observations = dict(getattr(value, "observations", None) or {})
        observations["optional:steward"] = {"state": steward_state, "observed_at": time.time()}
        observations["optional:peers"] = {
            "state": peer_state,
            "roster": tuple((identity(row), binding(row)) for row in sessions),
            "observed_at": time.time(),
        }
        value.observations = observations
        # Pane metadata only: no runtime identity/lifecycle writes. The input was
        # published with basic rows; slow panes occupy this existing detail worker.
        from .herdr_projection import refresh_observed_tag_metadata
        refresh_observed_tag_metadata(value.tag_metadata)
    if not include_report_verification:
        _strip_report_verification(value)
    for row in sessions + jobs:
        if hasattr(row, "_continuation_validated"):
            del row._continuation_validated
    if hasattr(value, "_continuation_source_key"):
        del value._continuation_source_key
    return DetailSnapshot(source_key, value)


_DETAIL_READ_CACHE = None


def enrich(snapshot, *, include_report_verification=True, required_only=False):
    """One artifact inventory/cache scope for the entire passive detail pass."""
    global _DETAIL_READ_CACHE
    from .collectors import dispatch
    from contextlib import ExitStack
    import artifact_producer
    import dispatch_registry_cache
    required = not required_details_complete(getattr(snapshot, "observations", None))
    reader = dispatch.artifact_reader
    if reader is not None and _DETAIL_READ_CACHE is None:
        _DETAIL_READ_CACHE = reader.ReadCache()
    with ExitStack() as stack:
        if reader is not None:
            stack.enter_context(reader.read_scope(_DETAIL_READ_CACHE))
        collection_read_scope = getattr(dispatch, "collection_read_scope", None)
        if callable(collection_read_scope):
            stack.enter_context(collection_read_scope())
        stack.enter_context(artifact_producer.artifact_index_observation_scope())
        stack.enter_context(dispatch_registry_cache.registry_read_scope())
        return _enrich(snapshot, include_report_verification=include_report_verification,
                       required=required, required_only=required_only)


def _child_activity_current(row, previous, basic, source):
    if not getattr(previous, "_child_session_associated", False):
        return True
    if not isinstance(source, dict):
        return False
    process = (row.harness, row.pid, row.proc_start)
    sid = getattr(row, "_runtime_session_id", None)
    children = [child for child in basic.sessions if child.is_child and
                ((all(part is not None for part in process)
                  and (child.harness, child.pid, child.proc_start) == process)
                 or (sid and row.harness == child.harness and sid == child.session_id))]
    return (len(children) == 1 and
            source.get("activity", {}).get(identity(children[0])) == _activity(children[0]))


def _rebind_projections(rows):
    # Route cards consume backing node.job, not only the top-level merged row.
    # Rebind both backing shapes without mutating the cached detail snapshot.
    exact = {}
    for row in rows:
        exact.setdefault((identity(row), binding(row)), []).append(row)
    for row in rows:
        projection = row.work_projection
        backing = getattr(projection, "_route_view", None)
        if not backing:
            continue
        def nodes(values):
            result = []
            for node in values or ():
                if hasattr(node, "to_dict"):
                    node = node.to_dict()
                node = dict(node)
                old_job = node.get("job")
                if old_job is not None:
                    candidates = exact.get((identity(old_job), binding(old_job)), ())
                    node["job"] = candidates[0] if len(candidates) == 1 else None
                result.append(node)
            return tuple(result)
        attached = dict(backing, nodes=nodes(backing.get("nodes")))
        if isinstance(backing.get("view"), dict):
            attached["view"] = dict(backing["view"], nodes=nodes(backing["view"].get("nodes")))
        row.work_projection = replace(projection, _route_view=attached)


def merge(basic, detail):
    """Join description only; the newest basic observation always owns state."""
    if detail is None:
        return basic
    value = detail.snapshot
    current_state = state_key(basic)
    observations = dict(getattr(basic, "observations", None) or {})
    old = {}
    for row in value.sessions + value.jobs:
        old.setdefault(identity(row), []).append(row)
    sessions, jobs = [], []
    for target, rows in ((sessions, basic.sessions), (jobs, basic.jobs)):
        for row in rows:
            candidates = old.get(identity(row), ())
            if len(candidates) != 1 or binding(row) != binding(candidates[0]):
                target.append(copy.copy(row))
                continue
            previous = candidates[0]
            current = copy.copy(row)
            for field in _METADATA + _STEWARD_RELATIONS:
                observed = getattr(previous, field, None)
                if observed is not None:
                    setattr(current, field, observed)
            # A title already observed on the current basic row wins. None is
            # the unobserved placeholder; do not replace a native current title.
            if not getattr(current, "title", None):
                current.title = getattr(previous, "title", None)
                current.title_ts = getattr(previous, "title_ts", None)
            elif (current.title == getattr(previous, "title", None)
                  and getattr(previous, "title_ts", None) is not None):
                current.title_ts = previous.title_ts
            # Enrichment can advance its bounded native read (or fill summary
            # timestamps). Compare with the captured input as well as output,
            # so an unchanged basic tick does not reject its own detail result.
            captured_activity = (detail.source_key.get("activity", {}).get(identity(row))
                                 if isinstance(detail.source_key, dict) else None)
            row_activity = _activity(row)
            if isinstance(row, Session):
                activity_current = row_activity in (captured_activity, _activity(previous))
            else:
                activity_current = (captured_activity is not None and
                                    row_activity[0] == captured_activity[0]) or (
                                    row_activity[0] == _activity(previous)[0])
            same_activity = (getattr(row, "liveness", None) == getattr(previous, "liveness", None)
                             and activity_current
                             and _child_activity_current(row, previous, basic, detail.source_key))
            if same_activity:
                if current.subagents is None and getattr(previous, "subagents", None) is not None:
                    current.subagents = previous.subagents
                context = getattr(current, "context", None)
                if getattr(previous, "_dispatch_context_owned", False) and (
                        context is None or getattr(context, "used_pct", None) is None):
                    current._dispatch_context_owned = True
                    for field in ("context", "_context_evidence", "ctx_pct", "active_context_tokens",
                                  "context_window_tokens", "session_input_tokens", "session_cached_input_tokens",
                                  "session_output_tokens", "session_reasoning_output_tokens", "session_total_tokens"):
                        if hasattr(previous, field) and (getattr(current, field, None) is None
                                or field in ("context", "_context_evidence")):
                            setattr(current, field, getattr(previous, field))
                summary = getattr(previous, "summary", None)
                old_ts, current_ts = getattr(previous, "summary_ts", None), getattr(row, "summary_ts", None)
                if summary and (not getattr(row, "summary", None) or
                                (old_ts is not None and old_ts > (current_ts or 0))):
                    current.summary, current.summary_ts = summary, old_ts
                # Exact child fallback and bounded native reads can add a tool
                # to an unchanged observation, but cannot erase current evidence.
                if not isinstance(row, Session) and current.exec_tool is None:
                    current.exec_tool = getattr(previous, "exec_tool", None)
                if not getattr(row, "shell_jobs", None):
                    current.shell_jobs = list(getattr(previous, "shell_jobs", None) or [])
                if (not getattr(row, "shell_job_observation", None)
                        and getattr(previous, "shell_job_observation", None)):
                    current.shell_job_observation = previous.shell_job_observation
            # Empty success means no summary is available yet, not detail failure.
            # Route-neighbourhood changes gate projections separately from NOW.
            current._details_pending = not same_activity
            current._details_error = same_activity and getattr(previous, "_details_error", False)
            if projection_current(row, previous, current_state, detail.source_key):
                current.work_projection = previous.work_projection
                current.route_chain = getattr(previous, "route_chain", None)
                current.cap_grounding = getattr(previous, "cap_grounding", None)
                if not isinstance(current, Session):
                    current.stage = previous.stage
                    if previous.resource_wait is not None:
                        current.resource_wait = previous.resource_wait
            target.append(current)
    _rebind_projections(sessions + jobs)
    from .work_titles import resource_identity
    previous_resources = {resource_identity(child): child for child in value.resources}
    resources = []
    for child in basic.resources:
        current = copy.copy(child)
        previous = previous_resources.get(resource_identity(child))
        if previous is not None:
            current.display_title = previous.display_title
        resources.append(current)
    titled = {resource_identity(child): child for child in resources}
    for job in jobs:
        job.resource_children = [titled.get(resource_identity(child), child)
                                 for child in job.resource_children]
    evidence = {}
    if current_state == detail.source_key:
        evidence = {"degradations": value.degradations,
                    "pending_delivery": value.pending_delivery,
                    "route_entities": value.route_entities}
        detail_observations = getattr(value, "observations", None)
        required = (detail_observations.get("required-details")
                    if isinstance(detail_observations, dict) else None)
        basic_identity = _observation_identity(observations.get("basic"))
        if (isinstance(required, dict) and basic_identity is not None
                and _observation_identity(required) == basic_identity):
            observations["required-details"] = copy.deepcopy(required)
        if isinstance(detail_observations, dict):
            for name, item in detail_observations.items():
                if str(name).startswith("optional:"):
                    observations[str(name)] = copy.deepcopy(item)
        try:
            from .display_snapshot import merge as merge_display_snapshot
            display_snapshot = merge_display_snapshot(
                basic.display_snapshot, value.display_snapshot)
        except Exception:
            display_snapshot = basic.display_snapshot
    else:
        display_snapshot = basic.display_snapshot
    return replace(basic, sessions=sessions, jobs=jobs, resources=resources,
                   memory=value.memory, governor=value.governor,
                   hearting=value.hearting or basic.hearting,
                   display_snapshot=display_snapshot, observations=observations, **evidence)


def retain_optional_last_good(previous, current):
    """Carry optional data across absent detail passes without crossing row identities."""
    if not isinstance(previous, DetailSnapshot) or not isinstance(current, DetailSnapshot):
        return current
    old, new = previous.snapshot, current.snapshot
    old_observations = getattr(old, "observations", None) or {}
    new_observations = dict(getattr(new, "observations", None) or {})

    old_memory = old_observations.get("optional:memory", {})
    new_memory = new_observations.get("optional:memory", {})
    if (new_memory.get("state") not in ("success", "absent")
            and old_memory.get("state") == "success"):
        if new.memory is None:
            new.memory = old.memory
        new_observations["optional:memory"] = copy.deepcopy(old_memory)

    old_db = old_observations.get("optional:memory-db", {})
    new_db = new_observations.get("optional:memory-db", {})
    if (new_db.get("state") is not None
            and new_db.get("state") not in ("success", "absent", "unobserved")
            and old_db.get("state") == "success"):
        old_data = old.memory if isinstance(old.memory, dict) else {}
        current_data = new.memory if isinstance(new.memory, dict) else old_data
        old_database = old_data.get("database") if isinstance(old_data.get("database"), dict) else {}
        current_database = current_data.get("database") if isinstance(current_data.get("database"), dict) else {}
        current_database = dict(current_database)
        current_database["state"] = str(new_db.get("state"))
        current_database["last_good_by_repo"] = copy.deepcopy(old_database.get("by_repo") or {})
        current_database["last_good"] = True
        new.memory = dict(current_data, database=current_database)
        new_db = dict(new_db, last_good=True)
        new_observations["optional:memory-db"] = new_db

    old_rows = {}
    for row in old.sessions:
        old_rows.setdefault((identity(row), binding(row)), []).append(row)

    # Only the optional pass judges the steward role; a required pass leaves the
    # model default False, which means "not judged here", never "not a steward".
    old_steward = old_observations.get("optional:steward", {})
    new_steward = new_observations.get("optional:steward", {})
    if (new_steward.get("state") != "success"
            and (old_steward.get("state") == "success" or old_steward.get("last_good"))):
        for row in new.sessions:
            matches = old_rows.get((identity(row), binding(row)), ())
            if len(matches) != 1:
                continue
            prior = matches[0]
            for field in ("steward", *_STEWARD_RELATIONS):
                setattr(row, field, copy.deepcopy(getattr(prior, field, None)))
        if new_steward.get("state") is None:
            new_observations["optional:steward"] = copy.deepcopy(old_steward)
        else:
            new_observations["optional:steward"] = dict(
                new_steward, last_good=True,
                last_good_observed_at=old_steward.get("observed_at",
                                                     old_steward.get("last_good_observed_at")))

    old_peers = old_observations.get("optional:peers", {})
    new_peers = new_observations.get("optional:peers", {})
    if new_peers.get("state") != "success":
        for row in new.sessions:
            matches = old_rows.get((identity(row), binding(row)), ())
            if len(matches) != 1:
                continue
            prior = matches[0]
            for field in ("peer_last_recv", "peer_last_sent", "peer_sent_1h",
                          "peer_recv_1h", "peer_obligations"):
                setattr(row, field, copy.deepcopy(getattr(prior, field, None)))
        if new_peers.get("state") is None:
            if old_peers:
                new_observations["optional:peers"] = copy.deepcopy(old_peers)
        elif old_peers.get("state") == "success" or old_peers.get("last_good"):
            new_observations["optional:peers"] = dict(
                new_peers, last_good=True,
                last_good_observed_at=old_peers.get("observed_at",
                                                   old_peers.get("last_good_observed_at")))

    new.observations = new_observations
    return current
