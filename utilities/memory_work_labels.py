"""Work-style labels a main session's "my way" record can carry (D-82, piece 12b).

A user correction that becomes a durable work-style record is found again by its
exact capsule labels: ``field:<name>`` and ``unit:<family/name>``. Nobody types
them. This module only *reads* what the runtime already holds and answers with
the labels that fit, or with none:

- the field comes from the route sealed for the session's one current route
  (``field`` in the route file, one of the eight English names);
- the unit comes from the newest correction this session sent on that route: to a stage
  of an owner's route (the owner-input request keeps the stage's unit,
  ``dispatch_owner_input``) or to a unit route's worker (the kept text keeps it,
  ``unit_continue``). A refused or never-kept correction left nothing to read.

"Current route" is read from the session's own route-chain ledger
(``tools/fleet/route_chain``), the record every compose and start already
appends to. One route that is still open, or that ended within ``RECENT_ENDED_SECONDS`` with
no direct work since, names it. An open route counts however long it has run, but only while no
newer route followed it. Automatic change records (turns that changed files with no route) are
never routes: they only mark that direct work happened (the ledger has a line for a folder's first
such turn; its later turns are read from the session's own change-record state file). Direct work
after a route's start or end means the record may be about that work, so nothing is labelled.
Several routes, none, or anything unreadable name nothing: the record is still written, just
without labels. A wrong label is worse than none. Nothing here writes, locks, or raises.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# One English set. The design's ``dev`` is ``code``.
FIELDS = ("plan", "design", "code", "lab", "research", "docs", "admin", "other")
# An open route always counts. A route that ended counts only this long after its outcome was
# written (the user's correction normally follows its report within the same sitting); later,
# or after any direct work, the record is no longer evidently about it.
RECENT_ENDED_SECONDS = 10 * 60
_UNIT_FORM = re.compile(r"[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_.-]*")


def unit_label(unit):
    """``unit:<family/name>`` for a real unit; None for the owner's ``_kernel/…`` and anything else."""
    if isinstance(unit, str) and _UNIT_FORM.fullmatch(unit):
        return "unit:" + unit
    return None


def field_label(route_file):
    """``field:<name>`` from the route's sealed field, else None (legacy routes seal none)."""
    try:
        field = json.loads(Path(route_file).read_text(encoding="utf-8")).get("field")
    except (OSError, ValueError, AttributeError):
        return None
    return "field:" + field if field in FIELDS else None


def _chain():
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet import route_chain
    return route_chain


def _ended_at(route_file):
    """The outcome sidecar's mtime when the route has ended, else None (still open)."""
    path = Path(route_file)
    try:
        return path.with_name(path.stem + ".outcome.json").stat().st_mtime
    except OSError:
        return None


def _last_direct_turn(env, anchor):
    """When this session last ended a direct-work turn in any folder, else 0.0.

    The ledger only has a line for a folder's *first* direct turn; later ones are appended to that
    folder's change record, whose state file keeps each record's last turn. Read only, this
    session's own file; unreadable means 0.0 (the ledger's lines alone decide).
    """
    try:
        from turn_change_record import state_path
        state = json.loads(state_path(env, *anchor).read_text(encoding="utf-8"))
        ended = [(entry.get("last_turn") or {}).get("ended") for entry in (state.get("records") or {}).values()]
        return max([t for t in ended if isinstance(t, (int, float)) and not isinstance(t, bool)], default=0.0)
    except Exception:
        return 0.0


def current_route(environ=None, now=None):
    """``(route_id, route_file)`` of the session's one open or just-ended route, else None.

    An open route with no usable start time, or one the session did direct work after, is not
    named either: with a doubt there is no label.
    """
    env = os.environ if environ is None else environ
    rc = _chain()
    anchor = rc.writer_identity(env)  # None for a worker or an unknown session
    if anchor is None:
        return None
    now = time.time() if now is None else now
    newest, continued, direct = {}, set(), 0.0
    # The whole ledger, not a tail: an open route that started long ago must still be counted.
    for line in rc.read_tail(*anchor, max_bytes=None):
        if line.get("automatic"):
            # Direct work with no route: not a route, only the time it last happened.
            direct = max(direct, line.get("ts") or 0)
            continue
        if line.get("capability") == "route-frame":
            continue  # the frame only proposes; its first leg is the work
        newest[line["route_id"]] = line
        if line.get("source_route_id"):
            continued.add(line["source_route_id"])
    direct = max(direct, _last_direct_turn(env, anchor))
    # A route that opened after this one means the session moved on: an open route left behind
    # is then doubtful, not "the current route".
    latest = max(((line.get("ts") or 0) for rid, line in newest.items() if rid not in continued), default=0)
    found, doubt = [], False
    for route_id, line in newest.items():
        if route_id in continued or not Path(line["route_file"]).is_file():
            continue
        ended = _ended_at(line["route_file"])
        if ended is None:  # still open: counts at any age
            since = line.get("ts")
            doubt = (doubt or not isinstance(since, (int, float))
                     or since < direct or since < latest)
        elif now - ended > RECENT_ENDED_SECONDS:
            continue  # over: it no longer names the session's work
        else:
            doubt = doubt or ended < direct
        found.append((route_id, line["route_file"]))
    # Direct work after a route that still counts makes the record's subject uncertain.
    return found[0] if len(found) == 1 and not doubt else None


def _correction_unit(route_id, session, environ):
    """The unit label of this session's newest correction on ``route_id``, else None."""
    from dispatch_contract import supervisor_lease_path
    from dispatch_contract_roots import stable_state_root
    jobs = environ.get("AGENT_DISPATCH_JOBS")
    jobs = Path(jobs) if jobs else stable_state_root(environ) / "jobs.log"
    newest = (0.0, None)
    for path in supervisor_lease_path(jobs, "att-labels").parent.glob("*.input.json"):
        if path.is_symlink():
            continue
        try:
            requests = json.loads(path.read_text()).get("requests", [])
        except (OSError, ValueError, AttributeError):
            continue
        for item in requests:
            if (isinstance(item, dict) and item.get("route_id") == route_id
                    and item.get("source_session") == session):
                events = item.get("events") or [{}]
                at = events[0].get("time") if isinstance(events[0], dict) else 0
                if isinstance(at, (int, float)) and at >= newest[0]:
                    newest = (at, item.get("unit"))
    # A unit route has no owner: its correction is the text kept for its next turn, with the sender and unit.
    import unit_continue
    for item in unit_continue.entries(jobs, route_id):
        if isinstance(item, dict) and item.get("source_session") == session:
            at = item.get("at")
            if isinstance(at, (int, float)) and not isinstance(at, bool) and at >= newest[0]:
                newest = (at, item.get("unit"))
    stored = newest[1]  # kept as the label itself
    return unit_label(stored[5:]) if isinstance(stored, str) and stored.startswith("unit:") else None


def labels_for_session(environ=None, now=None):
    """The labels to put on a ``user-correction`` record from this session; ``[]`` when unsure."""
    env = os.environ if environ is None else environ
    try:
        route = current_route(env, now)
        if route is None:
            return []
        route_id, route_file = route
        labels = [field_label(route_file)]
        try:
            from session_identity import session_label
            session = session_label(env, default="")
            labels.append(_correction_unit(route_id, session, env) if session else None)
        except Exception:
            pass  # no unit is no label; the field still stands
        return [label for label in labels if label]
    except Exception:
        return []


def stage_unit_label(jobs, attempt_id, find_row):
    """The unit label of a stage attempt a correction was addressed to; None for an owner."""
    row = find_row(Path(jobs), attempt_id)
    if not row or row.get("worker_type") == "owner" or row.get("dispatch_depth") != "2":
        return None
    return unit_label(row.get("unit"))


def correction_labels(route_file, unit=None):
    """The labels a correction just sent to ``route_file`` (and a stage's ``unit``) can carry."""
    return [label for label in (field_label(route_file), unit) if label]
