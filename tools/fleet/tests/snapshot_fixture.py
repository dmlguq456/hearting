"""Replay scrubbed source bundles into typed Fleet snapshots and render fixtures."""
from dataclasses import fields, is_dataclass
from collections.abc import Mapping
import json
from pathlib import Path
from typing import get_args, get_origin, get_type_hints
from types import UnionType
from typing import Union
import time

import importlib
import sys


def _package_name():
    hinted = __name__.split(".tests.", 1)[0]
    if hinted in ("fleet", "tools.fleet"):
        return hinted
    loaded = [name for name in ("fleet", "tools.fleet")
              if f"{name}.refresh" in sys.modules]
    if len(loaded) == 1:
        return loaded[0]
    return "fleet"


_PACKAGE = _package_name()
_nav = importlib.import_module(f"{_PACKAGE}.explorer.nav")
_collectors = importlib.import_module(f"{_PACKAGE}.collectors")
card_title = _nav.card_title
projection = importlib.import_module(f"{_PACKAGE}.projection")
render = importlib.import_module(f"{_PACKAGE}.render")
dispatch = importlib.import_module(f"{_PACKAGE}.collectors.dispatch")
resolve_parent_edges = _collectors.resolve_parent_edges
LiveSnapshot = importlib.import_module(f"{_PACKAGE}.refresh").LiveSnapshot

# Every class below comes from the package this fixture's LiveSnapshot comes from, so a row built
# here is an instance of the classes the production adapter uses whatever sys.path put it there.
_PACKAGE = LiveSnapshot.__module__.rsplit(".", 1)[0]


def _symbols(module, *names):
    loaded = importlib.import_module(f"{_PACKAGE}.{module}")
    return tuple(getattr(loaded, name) for name in names)


def _typed(value, annotation):
    """Rebuild only declared dataclass fields; unknown values remain provenance."""
    if value is None:
        return None
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        choices = [item for item in args if item is not type(None)]
        for choice in choices:
            if isinstance(choice, type) and is_dataclass(choice) and isinstance(value, dict):
                return _typed(value, choice)
            if get_origin(choice) is not None:
                try:
                    return _typed(value, choice)
                except (TypeError, ValueError):
                    continue
        return value
    if origin in (list, tuple, set):
        if origin is tuple and len(args) > 1 and args[-1] is not Ellipsis:
            return tuple(_typed(item, args[index]) if index < len(args) else item
                         for index, item in enumerate(value))
        item_type = args[0] if args else object
        values = [_typed(item, item_type) for item in value]
        return origin(values) if origin is not tuple else tuple(values)
    if origin in (dict, Mapping):
        kt, vt = args if len(args) == 2 else (object, object)
        return {_typed(k, kt): _typed(v, vt) for k, v in value.items()}
    if isinstance(annotation, type) and is_dataclass(annotation) and isinstance(value, dict):
        if annotation.__name__ == "WorkProjection":
            ActiveNodeProjection, ProgressProjection = _symbols("model", "ActiveNodeProjection", "ProgressProjection")
            value = dict(value)
            value["active_nodes"] = tuple(_typed(item, ActiveNodeProjection)
                                          for item in value.get("active_nodes", ()))
            if value.get("progress") is not None:
                value["progress"] = _typed(value["progress"], ProgressProjection)
        allowed = get_type_hints(annotation)
        return annotation(**{key: _typed(item, allowed[key]) for key, item in value.items()
                             if key in allowed})
    return value


def replay_snapshot(payload, *, detail_bundle=None, now=None):
    """Return a LiveSnapshot and ExplorerDetail from the allow-listed bundle schema.

    A bundle may be a mapping or a path to JSON. Unsupported top-level keys are retained in
    ``source_records`` so replay never silently erases source provenance.
    """
    if isinstance(payload, (str, Path)):
        payload = json.loads(Path(payload).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("snapshot bundle must be a mapping or JSON path")
    Session, DispatchJob, ResourceJob = _symbols("model", "Session", "DispatchJob", "ResourceJob")
    snapshot_fields = get_type_hints(LiveSnapshot)
    rows = {
        "sessions": Session, "jobs": DispatchJob, "resources": ResourceJob,
    }
    snapshot = LiveSnapshot()
    unknown = {key: value for key, value in payload.items() if key not in snapshot_fields}
    for key, value in payload.items():
        if key not in snapshot_fields:
            continue
        if key in rows:
            setattr(snapshot, key, [_typed(item, rows[key]) for item in value or ()])
        else:
            setattr(snapshot, key, _typed(value, snapshot_fields[key]))
    for job in snapshot.jobs:                    # a job's resource children are resource jobs, not bare records
        children = getattr(job, "resource_children", None)
        if children:
            job.resource_children = [_typed(child, ResourceJob) if isinstance(child, dict) else child
                                     for child in children]
    if unknown:
        snapshot.observations = dict(snapshot.observations or {}, replay_unknown=unknown)

    ExplorerDetail, FocusResult, FocusKey = _symbols("explorer.types", "ExplorerDetail", "FocusResult", "FocusKey")
    detail_data = detail_bundle or payload.get("detail") or {}
    focus_data = detail_data.get("focus") if isinstance(detail_data, dict) else None
    focus = None
    if isinstance(focus_data, FocusResult):
        focus = focus_data
    elif isinstance(focus_data, dict):
        key_data = focus_data.get("key", {})
        key = FocusKey(**{name: key_data[name] for name in ("harness", "sid", "generation")
                          if name in key_data})
        allowed = {field.name for field in fields(FocusResult)} - {"key"}
        focus_values = {name: value for name, value in focus_data.items() if name in allowed}
        focus_values["key"] = key
        focus = _typed(focus_values, FocusResult)
    detail = ExplorerDetail(focus=focus,
                            seats=detail_data.get("seats", {}),
                            config=detail_data.get("config"),
                            panes=detail_data.get("panes", {}))
    if now is not None:
        snapshot.observations = dict(snapshot.observations or {}, replay_now=now)
    return snapshot, detail


def _document(value):
    if isinstance(value, (str, Path)):
        return json.loads(Path(value).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("replay source must be a mapping or JSON path")
    return value


def _source_epoch(value, now=None):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            import datetime
            stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            return stamp.timestamp()
        except ValueError:
            try:
                import datetime
                stamp = datetime.datetime.strptime(value, "%m-%d %H:%M")
                year = datetime.datetime.fromtimestamp(now).year if isinstance(now, (int, float)) else datetime.datetime.now().year
                return stamp.replace(year=year).timestamp()
            except (ValueError, OSError, OverflowError):
                return None
    return None


def replay_source_bundle(state, history_data, logs_hist, raw_f5, *, now=None):
    """Join the four original capture records and rebuild typed production inputs.

    ``source_records`` retains JSON-pointer provenance for each attached detail value.
    The joined private bundle is evidence-only; checked fixtures stay scrubbed.
    """
    state_doc = _document(state)
    history_doc = _document(history_data)
    logs_doc = _document(logs_hist)
    raw_doc = _document(raw_f5)
    normalized = dict(state_doc)
    normalized["resources"] = state_doc.get("resource_jobs", ())
    route_rows = state_doc.get("route") or ()
    normalized["node_evidence"] = {
        str(row.get("route_id")): row for row in route_rows
        if isinstance(row, dict) and row.get("route_id")
    }
    handovers = history_doc.get("_recent_handovers", ())
    historical_tags = {}
    for history_tag, history in history_doc.items():
        if str(history_tag).startswith("_") or not isinstance(history, dict):
            continue
        for card in list(history.get("prior_cards") or ()) + list(history.get("cards") or ()):
            if isinstance(card, dict) and card.get("sid"):
                historical_tags[str(card["sid"])] = str(history_tag)
    normalized["observations"] = dict(state_doc.get("observations") or {},
        replay_sources={"peer_messages": state_doc.get("peer_messages"),
                        "history_handovers": handovers,
                        "historical_tags": historical_tags,
                        "unregistered_gpu": state_doc.get("unregistered_gpu"),
                        "source_records": {"peer_messages": "/state.json/peer_messages",
                                           "history_handovers": "/history_data.json/_recent_handovers",
                                           "historical_tags": "/history_data.json/*/cards",
                                           "unregistered_gpu": "/state.json/unregistered_gpu"}})
    detail = {"seats": {}, "panes": {}, "source_records": {}}
    focus_target = None
    for index, session in enumerate(state_doc.get("sessions") or ()):
        if not isinstance(session, dict):
            continue
        sid = session.get("session_id")
        harness = session.get("harness")
        tag = session.get("session_tag")
        if tag == "f5" or (focus_target is None and sid and raw_doc.get("sid") == sid):
            focus_target = (session, index)
        aliases = [tag, sid, *(session.get("session_aliases") or ())]
        history_key = next((str(alias) for alias in aliases
                            if alias and isinstance(history_doc.get(str(alias)), dict)), None)
        source = history_doc.get(history_key) if history_key is not None else None
        if source is not None:
            for field_name in ("seat", "cards", "handovers", "prior_cards"):
                if field_name in source:
                    pointer = f"/history_data.json/{history_key}/{field_name}"
                    detail["source_records"][f"{tag or sid}.{field_name}"] = pointer
            if harness and sid and source.get("seat") is not None:
                hand = {(str(h.get("frm")), str(h.get("to"))): h for h in source.get("handovers") or ()
                        if isinstance(h, dict)}
                entries, previous_sid = [], None
                ordered = [(True, card) for card in source.get("prior_cards") or ()] + \
                          [(False, card) for card in source.get("cards") or ()]
                for prior, card in ordered:
                    card_sid = card.get("sid") if isinstance(card, dict) else None
                    card_harness = card.get("harness") if isinstance(card, dict) else None
                    lines = list(card.get("lines") or ()) if isinstance(card, dict) else []
                    recorded = dict(card) if isinstance(card, dict) else {}
                    recorded.update({"generation": recorded.get("gen", recorded.get("generation")),
                                     "status": "available" if lines else "unavailable",
                                     "at": _source_epoch(recorded.get("at"), now),
                                     "title": card_title(lines), "lines": lines})
                    into = hand.get((str(previous_sid), str(card_sid))) if previous_sid else None
                    entries.append({"harness": card_harness or harness, "sid": card_sid or sid,
                                    "started_at": _source_epoch(into.get("at"), now) if into else None,
                                    "source": into.get("source") if into else None,
                                    "cards": [recorded], "prior": prior, "seat": source.get("seat"),
                                    "handovers": source.get("handovers") or (), "relation": "recorded generation",
                                    "transcript": {}, "current": False})
                    previous_sid = card_sid
                into = hand.get((str(previous_sid), str(sid)[:8])) if previous_sid else None
                cleared_at = _source_epoch(into.get("at"), now) if into else None
                if into is None and entries and str(entries[-1]["sid"]) != str(sid)[:8]:
                    cleared_at = entries[-1]["cards"][0].get("at")      # the last card is written as the seat is cleared
                entries.append({"harness": harness, "sid": sid, "cards": [], "prior": False,
                                "seat": source.get("seat"), "handovers": source.get("handovers") or (),
                                "source": (into or {}).get("source") or ("clear" if cleared_at else None),
                                "relation": "current session",
                                "started_at": cleared_at or _source_epoch(session.get("started_at"), now),
                                "transcript": {}, "current": True})
                detail["seats"][f"{harness}:{sid}"] = entries
        if tag and sid:
            detail["source_records"][f"session.{tag}"] = f"/state.json/sessions/{index}"
    detail["handovers"] = handovers
    detail["logs"] = logs_doc
    detail["raw"] = raw_doc
    detail["source_records"]["handovers"] = "/history_data.json/_recent_handovers"
    detail["source_records"]["logs"] = "/logs_hist.json"
    detail["source_records"]["raw"] = "/raw_f5.json/lines"
    for key, entry in logs_doc.items():
        if isinstance(entry, dict):
            detail["source_records"][f"logs.{key}"] = f"/logs_hist.json/{key}/lines"
    if raw_doc.get("lines"):
        detail["source_records"]["raw.lines"] = "/raw_f5.json/lines"
    if focus_target is not None:
        session, index = focus_target
        sid, harness = session.get("session_id"), session.get("harness")
        tag = session.get("session_tag") or "f5"
        seat_entries = detail["seats"].get(f"{harness}:{sid}")
        dialogue = next((entry for entry in logs_doc.values()
                         if isinstance(entry, dict) and entry.get("sid") == sid), None)
        source_records = {"raw": raw_doc, "logs": logs_doc}
        if seat_entries is not None:
            source_records["seat"] = seat_entries
        if dialogue is not None:
            source_records["dialogue"] = {"index": 0, "harness": dialogue.get("harness"),
                                           "sid": dialogue.get("sid"), "lines": dialogue.get("lines")}
        FocusKey, FocusResult, Health = _symbols("explorer.types", "FocusKey", "FocusResult", "Health")
        focus = FocusResult(key=FocusKey(str(harness), str(sid)), epoch=0,
            snapshot_generation=0, output=tuple(raw_doc.get("lines") or ()),
            health=Health("success"), events=tuple(), source_records=source_records)
        detail["focus"] = focus
        detail["source_records"][f"focus.{tag}"] = f"/state.json/sessions/{index}"
    normalized["observations"]["replay_source_records"] = {
        "state": "/state.json", "history": "/history_data.json",
        "logs": "/logs_hist.json", "raw": "/raw_f5.json"}
    snapshot, explorer_detail = replay_snapshot(normalized, detail_bundle=detail, now=now)
    snapshot.compute_hosts = state_doc.get("compute_hosts")
    snapshot.unregistered_gpu = state_doc.get("unregistered_gpu", ())
    snapshot.peer_messages = state_doc.get("peer_messages")
    snapshot.usage = state_doc.get("usage")
    snapshot.route = route_rows
    snapshot.summary = state_doc.get("summary")
    from dataclasses import replace
    from types import SimpleNamespace
    (WorkProjection,) = _symbols("model", "WorkProjection")
    routes_by_id = {str(row["route_id"]): row for row in route_rows
                    if isinstance(row, dict) and row.get("route_id")}
    for entity in (*snapshot.sessions, *snapshot.jobs):
        work = getattr(entity, "work_projection", None)
        route = routes_by_id.get(str(getattr(work, "route_id", ""))) if work is not None else None
        if route is not None:
            view = dict(route)
            setattr(entity, "work_projection", replace(work, _route_view={"record": route, "view": view}))
    snapshot.route_entities = [SimpleNamespace(
        work_projection=WorkProjection(source="replay-route", route_id=rid, route_hash=row.get("route_hash"),
            _route_view={"record": row, "view": row}, result=row.get("result")))
        for rid, row in routes_by_id.items()]
    snapshot.observations = dict(snapshot.observations or {}, replay_sources=normalized["observations"]["replay_sources"],
                                 replay_source_records=normalized["observations"]["replay_source_records"])
    (ExplorerDetail,) = _symbols("explorer.types", "ExplorerDetail")
    explorer_detail = ExplorerDetail(
        focus=explorer_detail.focus, seats=explorer_detail.seats,
        config=explorer_detail.config, panes=explorer_detail.panes,
        catalogs=explorer_detail.catalogs, source_records=detail["source_records"])
    return snapshot, explorer_detail


def observed_fixture(sessions, jobs, resources=None, node_evidence=None):
    evidence = dispatch.collect.last_route_nodes if node_evidence is None else node_evidence
    snapshot = LiveSnapshot(sessions=list(sessions), jobs=list(jobs),
                            resources=list(resources or []), node_evidence=evidence or {})
    resolve_parent_edges(snapshot.sessions, [job for job in snapshot.jobs
                         if (job.parent_managed_dir or getattr(job, "_registry_metadata", None))
                         and not hasattr(job, "_parent_edge_promoted_orphan")])
    if jobs and all(row.work_projection is None for row in snapshot.sessions + snapshot.jobs):
        projection.attach_projections(snapshot.sessions, snapshot.jobs,
                                      node_evidence=snapshot.node_evidence,
                                      degradations=dispatch.collect.last_degradations,
                                      resources=snapshot.resources, now=time.time())
    if not sessions and not jobs:
        snapshot.route_entities = projection.terminal_route_entities(snapshot.node_evidence)
    return snapshot


def build_observed_lines(sessions, jobs, *args, **kwargs):
    snapshot = observed_fixture(sessions, jobs, kwargs.get("resources"), kwargs.get("node_evidence"))
    kwargs.update(node_evidence=snapshot.node_evidence, route_entities=snapshot.route_entities)
    return render._build_lines(sessions, jobs, *args, **kwargs)


def draw_observed(stdscr, sessions, jobs, *args, **kwargs):
    kwargs["snapshot"] = observed_fixture(sessions, jobs, kwargs.get("resources"))
    return render._draw(stdscr, sessions, jobs, *args, **kwargs)
