"""When does a reader *show* the value an event changed (T1 projection, T2 Fleet screen model).

An event is counted as arrived only when the reader's own output holds the value
the event changes, never because something was published after the write:

======================  ===========================================================
event                   value (``expect`` of the event)
======================  ===========================================================
route-start             the route's row (id) with its owner attempt id
stage-transition        the route's row names the new stage as its active stage
route-end               the route's row reads ``outcome`` success (closed), or the
                        row is gone after it had been shown
worker-tool             the route's row carries the tool name
answer-wait             the item id is among the shown questions / items
owner-dead              the route's row is no longer ``working``, or is gone after
                        it had been shown
======================  ===========================================================

T1 reads ``projection.json`` (``route_panels[<harness>\\0<session>]`` rows, ``items``);
T2 reads the digest the pass shim takes of the Fleet explorer model after each ``_draw``
returns: the model that frame was drawn from (``render._EXPLORER_MODEL.routes`` / ``.questions``). A row that was never shown before
an "is gone" event proves nothing, so those need ``expect["was_present"]`` (T1) and
``expect["was_present_fleet"]`` (T2), taken from the readers just before the write.
"""
from __future__ import annotations

CLAIM_KINDS = ("route-start", "stage-transition", "route-end", "worker-tool", "answer-wait", "owner-dead")


def panel_rows(projection: dict, expect: dict) -> list[dict]:
    panels = (projection or {}).get("route_panels") or {}
    rows = panels.get("claude\0" + str(expect.get("session_id")))
    return [r for r in rows if isinstance(r, dict) and r.get("id") == expect.get("route_id")] \
        if isinstance(rows, list) else []


def route_shown(projection: dict, expect: dict) -> bool:
    """True when the route has a row in the session's panel (used for the ``was_present`` baseline)."""
    return bool(panel_rows(projection, expect))


def t1(ev: dict, projection: dict) -> bool:
    """Does this ``projection.json`` carry the value ``ev`` changed?"""
    kind, expect = ev["kind"], ev.get("expect") or {}
    if not isinstance(projection, dict):
        return False
    if kind == "answer-wait":
        return any(isinstance(i, dict) and i.get("id") == expect.get("item_id")
                   for i in projection.get("items") or ())
    rows = panel_rows(projection, expect)
    if kind == "route-start":
        return any(r.get("owner_attempt") == expect.get("attempt_id") for r in rows)
    if kind == "stage-transition":
        stage = expect.get("stage")
        return any(r.get("worker_stage") == stage
                   and any(isinstance(s, dict) and s.get("name") == stage and s.get("st") == "active"
                           for s in r.get("stages") or ()) for r in rows)
    if kind == "route-end":
        return (any(r.get("outcome") == "success" for r in rows)
                or (not rows and expect.get("was_present") is True))
    if kind == "worker-tool":
        return any(r.get("tool") == expect.get("tool") for r in rows)
    if kind == "owner-dead":
        return (any(r.get("state") != "working" for r in rows)
                or (not rows and expect.get("was_present") is True))
    return False


def t2(ev: dict, digest: dict | None) -> bool:
    """Does this Fleet screen-model digest carry the value ``ev`` changed?"""
    kind, expect = ev["kind"], ev.get("expect") or {}
    if not isinstance(digest, dict) or "routes" not in digest:
        return False
    if kind == "answer-wait":
        return expect.get("item_id") in (digest.get("questions") or ())
    row = (digest["routes"] or {}).get(expect.get("route_id"))
    if kind == "route-start":
        return bool(row) and row.get("attempt") == expect.get("attempt_id")
    if kind == "stage-transition":
        return bool(row) and expect.get("stage") in (row.get("active") or ())
    if kind == "route-end":
        return (bool(row) and row.get("outcome") == "success") or (
            row is None and expect.get("was_present_fleet") is True)
    if kind == "worker-tool":
        return bool(row) and row.get("tool") == expect.get("tool")
    if kind == "owner-dead":
        return (bool(row) and row.get("state") != "working") or (
            row is None and expect.get("was_present_fleet") is True)
    return False
