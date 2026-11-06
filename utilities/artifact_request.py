"""Physical request membership derived from an existing sealed frame decision.

No request IDs or store: producer records retain a checked reference, and each
logical execution retains the normal cycle controls and manifest history.
"""
from pathlib import Path

import artifact_locator as locator
import route_identity
import route_plan

EXECUTIONS = ("_internal", "executions")


def scope_anchor(path):
    """Physical shape only; callers still verify the record/marker identity."""
    path = Path(path)
    if (path.parent.name == EXECUTIONS[1] and path.parent.parent.name == EXECUTIONS[0]
            and locator._CYCLE_ID.fullmatch(path.name)):
        return path.parents[2]
    return None


def _safe(root, path):
    from artifact_journal import safe_path
    return safe_path(root, path)


def binding_for_route(root, route):
    """Validated grouping facts, or None for unplanned/cross-root work.

    Different roots retain their own artifact ownership. Invalid same-root
    proof raises rather than choosing another folder or granting membership.
    """
    root = Path(root).resolve()
    sealed = route.get("route_plan")
    if sealed is None:
        return None
    sealed = route_plan.validate_sealed(sealed)
    decision_path = Path(sealed["decision"])
    decision_path = decision_path if decision_path.is_absolute() else root / decision_path
    if not decision_path.absolute().is_relative_to(root):
        return None
    decision_path = _safe(root, decision_path)
    plan = route_plan.read_route_plan(f"{decision_path}#{sealed['index']}", root)
    if plan["digest"] != sealed["digest"]:
        raise ValueError("request-decision-digest-mismatch")
    frame = plan["record"]["decision"]["frame_route"]
    if not all(isinstance(frame.get(key), str) for key in ("cycle_id", "route_id", "route_hash")):
        raise ValueError("request-frame-cycle-mismatch")
    record = locator.read_cycle_record(root, frame["cycle_id"])
    if (not record or record.get("deleted_at") or record.get("journal_migration")
            or record.get("route_id") != frame["route_id"]
            or record.get("route_hash") != frame["route_hash"]):
        raise ValueError("request-frame-cycle-mismatch")
    frame_path = _safe(root, root / ".runtime/routes" / (frame["route_id"] + ".json"))
    source = locator._read_json(frame_path)
    if (not source or source.get("route_id") != frame["route_id"]
            or source.get("route_hash") != frame["route_hash"]
            or route_identity.route_hash(source) != frame["route_hash"]
            or Path(source.get("artifact_root", "")).resolve() != root
            or not route_plan.is_framed_route(source)):
        raise ValueError("request-frame-route-mismatch")
    # The decision must be in this exact first frame, not a copied record
    # with the same digest. Its parent is a verified cycle locator/marker.
    suffix = ("artifacts", *Path(route_plan.RECORD_RELATIVE).parts)
    if decision_path.parts[-len(suffix):] != suffix:
        raise ValueError("request-decision-location-mismatch")
    directory = decision_path.parents[len(suffix) - 1]
    marker = locator.read_cycle_binding(directory)
    if (not marker or marker["cycle_id"] != record["cycle_id"]
            or marker["campaign_id"] != record["campaign_id"]
            or not locator._verify_candidate(root, directory, record["cycle_id"])):
        raise ValueError("request-frame-location-mismatch")
    return {"cycle_id": frame["cycle_id"], "route_id": frame["route_id"],
            "route_hash": frame["route_hash"], "decision": plan["decision"],
            "digest": plan["digest"], "index": plan["index"],
            "path": directory.relative_to(root).as_posix(),
            "campaign_id": record["campaign_id"]}


def execution_dir(root, record, *, anchor=None):
    """Read a previously admitted storage binding against its sealed route.

    Like the begin cycle/route tuple, this is durable producer identity. The
    live parent input is not completion authority and may later be edited or
    removed. New membership is checked by binding_for_route, never inferred
    from these storage fields.
    """
    saved = record.get("request_binding")
    if saved is None:
        return None
    strings = ("cycle_id", "route_id", "route_hash", "decision", "digest", "path", "campaign_id")
    if (not isinstance(saved, dict) or not all(isinstance(saved.get(k), str) for k in strings)
            or type(saved.get("index")) is not int
            or not all(isinstance(record.get(k), str) for k in ("cycle_id", "route_id", "route_hash", "campaign_id"))):
        raise ValueError("request-execution-binding-mismatch")
    root = Path(root).resolve()
    path = _safe(root, root / ".runtime/routes" / (record["route_id"] + ".json"))
    route = locator._read_json(path)
    if (not route or route_identity.route_hash(route) != record.get("route_hash")
            or route.get("route_id") != record["route_id"]):
        raise ValueError("request-execution-route-mismatch")
    sealed = route_plan.validate_sealed(route.get("route_plan"))
    decision = Path(sealed["decision"])
    decision = decision if decision.is_absolute() else root / decision
    expected = root / saved["path"] / "artifacts" / route_plan.RECORD_RELATIVE
    if (any(saved.get(key) != sealed[key] for key in ("decision", "digest", "index"))
            or decision != expected
            or not locator._CYCLE_ID.fullmatch(record["cycle_id"])):
        raise ValueError("request-execution-binding-mismatch")
    if anchor is None:
        from artifact_producer_store import cycle_dir
        frame = locator.read_cycle_record(root, saved["cycle_id"])
        if not frame or frame.get("request_binding"):
            raise ValueError("request-frame-cycle-mismatch")
        anchor = cycle_dir(root, frame["campaign_id"], frame["cycle_id"], frame)
    marker = locator.read_cycle_binding(anchor)
    identity = (marker or {}).get("cycle_id")
    if identity is None:
        cycle = (locator._read_json(Path(anchor) / "manifest.json") or {}).get("cycle")
        identity = cycle.get("cycle_id") if isinstance(cycle, dict) else None
    if identity != saved["cycle_id"]:
        raise ValueError("request-frame-location-mismatch")
    return _safe(root, Path(anchor) / Path(*EXECUTIONS) / record["cycle_id"])


def members(root, directory, *, strict=True, include_deleted=False):
    """Only verified direct members of a request; unknown directories stay unknown."""
    directory = Path(directory)
    # Existing arbitrary internal support (including symlinks) is not a
    # request container. Do not add a grouping refusal to ordinary cycles.
    marker = locator.read_cycle_binding(directory)
    identity = (marker or {}).get("cycle_id")
    if identity is None:
        cycle = (locator._read_json(directory / "manifest.json") or {}).get("cycle")
        identity = cycle.get("cycle_id") if isinstance(cycle, dict) else None
    frame = locator.read_cycle_record(root, identity) if identity else None
    if not frame or frame.get("route_capability") != route_plan.CAPABILITY:
        return
    try:
        parent = _safe(root, directory / Path(*EXECUTIONS))
        paths = sorted(parent.iterdir()) if parent.is_dir() else []
    except (OSError, ValueError, locator.LocatorError) as exc:
        if strict:
            raise locator.LocatorError("request-members-unreadable", str(directory)) from exc
        return
    for path in paths:
        if not locator._CYCLE_ID.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            continue
        record = locator.read_cycle_record(root, path.name)
        try:
            if record and (include_deleted or not record.get("deleted_at")) and execution_dir(root, record, anchor=directory) == path:
                marker = locator.read_cycle_binding(path)
                if marker and marker["cycle_id"] == path.name and marker["campaign_id"] == record["campaign_id"]:
                    yield path
        except (OSError, ValueError, KeyError, locator.LocatorError) as exc:
            if strict:
                raise locator.LocatorError("request-binding-invalid", str(path)) from exc


def owning_scope(root, target):
    """The deepest verified cycle marker enclosing a prospective payload path."""
    root, target = Path(root).resolve(), Path(target).resolve(strict=False)
    for parent in target.parents:
        if parent == root:
            break
        if not parent.is_relative_to(root):
            break
        marker = locator.read_cycle_binding(parent)
        if marker and locator._verify_candidate(root, parent, marker["cycle_id"]):
            return marker["cycle_id"], parent
    return None
