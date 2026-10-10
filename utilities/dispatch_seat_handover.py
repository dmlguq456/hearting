#!/usr/bin/env python3
"""Same-seat handover: who now answers for a registered depth-1 attempt.

A cleared window (``/clear``, ``/new``) is a new session id at the same herdr pane.  The
registry row of a depth-1 owner/frame the old session (A) launched still says
``parent_sid=A`` -- that stays, it is the registered identity the receipt, digest, worker
environment and gate paths are signed with.  What changes is *who may resume it and who
receives its completion*: the next session at the same pane (B).

The relation is one fact in two files under the session-tidy state folder:

``handover/<seat>.json``   the snapshot ``session_tidy.py enqueue`` takes: the registered
                           depth-1 attempts the tidying session answers for.  A card's
                           text never grants anything; only this runtime-made snapshot does.
``sessions/<seat>.jsonl``  the seat ledger; ``run_hook`` appends one ``event=handover`` row
                           (A -> B, with the bindings) when it sees B's confirmed start.

Rules: one direction (A -> B), never A -> B and A -> C, B -> C only through B's own next
tidy.  Two ways in: a confirmed clear at the same pane and harness (a ``clear`` start source
or the clear booking's own observation; OpenCode has no start hook, so its first message in
a new session after the snapshot), or the official seat change across panes and harnesses
(``peer-steward start --beside`` -> ACK -> ``retire`` from the pane started beside the
predecessor, :func:`record_retire_handover`).  Everything here is read-only except
:func:`record_locked` / :func:`write_snapshot_locked` (caller holds the seat lock) and
:func:`record_retire_handover`.

Two questions, two functions:

* :func:`effective_parent` / :func:`owns` -- which session may resume the attempt (B).
* :func:`storage_recipients` -- which pending-delivery directories (A's, keyed by the
  registered ``parent_sid``) hold records addressed to B, and for which attempts.

Both answer "the registered parent" whenever there is no relation, so a call without a
handover behaves exactly as before.  No failure here may block a caller: every public
function returns the registered answer on any error.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ROOT = HERE.parent
SCHEMA = 1
ROW_EVENT = "handover"
WORKER_TYPES = frozenset({"owner", "frame", "review"})
OPEN_ROW_STATUSES = frozenset({"open", "running"})
LINE_MAX_BYTES = 520
MAX_BINDINGS = 12
KEEP_ROWS = 40


def _st():
    import session_tidy
    return session_tidy


def _meta(pipe: str) -> dict:
    pairs = {}
    for part in pipe.split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            pairs[key.strip()] = value
    return pairs


def _jobs_key(jobs) -> str:
    return str(Path(jobs).resolve(strict=False))


def route_identity(meta: dict) -> tuple:
    """(route id, route hash, node) of a registry row: owner rows carry the route as ``route_id`` too."""
    return (meta.get("owner_route_id") or meta.get("route_id") or "",
            meta.get("owner_route_hash") or meta.get("route_hash") or "",
            meta.get("route_node") or "")


def eligible_row(meta: dict) -> bool:
    """A route-bound depth-1 owner/frame; depth-2, route-free and unregistered rows never qualify."""
    route, digest, _ = route_identity(meta)
    return (meta.get("dispatch_depth") == "1" and meta.get("worker_type") in WORKER_TYPES
            and bool(meta.get("parent_sid")) and bool(meta.get("attempt_id")) and bool(route) and bool(digest))


def latest_rows(jobs) -> dict:
    """``attempt_id -> (status, metadata)`` from a registry file (last line of an attempt wins)."""
    rows: dict = {}
    try:
        text = Path(jobs).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = _meta(fields[5])
        if meta.get("attempt_id"):
            rows[meta["attempt_id"]] = (fields[1], meta)
    return rows


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _snapshot_dir() -> Path:
    return _st().state_root() / "handover"


def _snapshot_path(seat_key: str) -> Path:
    return _snapshot_dir() / f"{seat_key}.json"


def read_snapshot(seat_key: str) -> Optional[dict]:
    data = _st().read_json(_snapshot_path(seat_key))
    if isinstance(data, dict) and data.get("schema") == SCHEMA and isinstance(data.get("bindings"), list) \
            and isinstance(data.get("from"), dict):
        return data
    return None


def _all_snapshots() -> list[dict]:
    out = []
    try:
        names = sorted(p for p in _snapshot_dir().glob("*.json") if p.is_file() and not p.is_symlink())
    except OSError:
        return out
    for path in names:
        data = read_snapshot(path.stem)
        if data:
            out.append(data)
    return out


def handover_rows(seat) -> list[dict]:
    """The seat ledger's handover rows, oldest first."""
    rows = [e for e in _st()._read_ledger_lines(seat) if e.get("event") == ROW_EVENT and e.get("from")]
    return sorted(rows, key=lambda r: float(r.get("ts", 0) or 0))


def _seat_of(key: str, snapshot: Optional[dict] = None):
    st = _st()
    data = (snapshot or {}).get("seat") if snapshot else None
    if isinstance(data, dict) and data.get("key") == key:
        return st.seat_from_dict(data)
    return st.Seat("pane", key)


def pane_seat(env=None, harness: Optional[str] = None, sid: Optional[str] = None):
    """The proven pane or exact native seat; project seats never hand over."""
    st = _st()
    env = os.environ if env is None else env
    detected, own_sid = st.session_from_env(harness, env)
    seat = st.resolve_seat(harness or detected, env=env, sid=sid or own_sid)
    return seat if seat.kind in ("pane", "native") else None


# ---------------------------------------------------------------------------
# Bindings: which registered attempts a session answers for
# ---------------------------------------------------------------------------

def binding_of(jobs, meta: dict) -> dict:
    """``parent`` is the registered parent, the key its pending records stay stored under."""
    route, digest, node = route_identity(meta)
    return {"jobs": _jobs_key(jobs), "attempt": meta["attempt_id"], "route": route, "hash": digest, "node": node,
            "parent": str(meta.get("parent_sid") or "")}


def _binds(bindings: Iterable[dict], jobs, meta: dict) -> bool:
    """A binding is exact on registry file, route id, route hash and node.  A replacement
    attempt of the same logical node shares all four, so the lineage follows it; the attempt
    id in the binding is the audit record, not a key."""
    key = _jobs_key(jobs)
    route, digest, node = route_identity(meta)
    return any(b.get("jobs") == key and b.get("route") == route and b.get("hash") == digest
               and b.get("node") == node for b in bindings if isinstance(b, dict))


def _effective(meta: dict, jobs=None) -> tuple[str, str]:
    """``(session, harness)`` answering for ``meta``'s attempt now.  The chain may cross panes
    and harnesses (a seat change hands a Codex parent's route to a Claude successor), so the
    rows of every seat whose snapshot binds the attempt are read together, oldest first."""
    registered = str(meta.get("parent_sid") or "")
    harness = str(meta.get("parent_harness") or "")
    if not registered or jobs is None or not eligible_row(meta):
        return registered, harness
    try:
        rows = sorted((row for seat in _seats_for_binding(jobs, meta) for row in handover_rows(seat)),
                      key=lambda r: float(r.get("ts", 0) or 0))
        current, seen = registered, {registered}
        for row in rows:
            if row.get("from") == current and _binds(row.get("bindings") or (), jobs, meta) \
                    and row.get("sid") not in seen:
                current = str(row["sid"])
                harness = str(row.get("harness") or harness)
                seen.add(current)
        return current, harness
    except Exception:  # noqa: BLE001 - the registered answer is always safe
        return registered, str(meta.get("parent_harness") or "")


def effective_parent(meta: dict, jobs=None) -> str:
    """The session that answers for ``meta``'s attempt now: the registered ``parent_sid``, or
    the end of its A -> B (-> C) handover chain.  Any failure answers the registered parent."""
    return _effective(meta, jobs)[0]


def effective_parent_harness(meta: dict, jobs=None) -> str:
    """The harness of :func:`effective_parent` (the registered ``parent_harness`` without a handover)."""
    return _effective(meta, jobs)[1]


def owns(meta: dict, session: str, jobs=None) -> bool:
    """True when ``session`` is the registered parent or its confirmed same-seat successor."""
    if not session:
        return False
    return meta.get("parent_sid") == session or (jobs is not None and effective_parent(meta, jobs) == session)


def _seats_for_binding(jobs, meta: dict) -> list:
    """Every seat whose snapshot binds this exact registry/route/node.  No recency, cwd or pane
    name decides it: only an exact binding.  A snapshot is written only by a session that
    answered for the attempt, so each one belongs to the same chain."""
    return [_seat_of(str(s["seat"].get("key") or ""), s) for s in _all_snapshots()
            if isinstance(s.get("seat"), dict) and _binds(s["bindings"], jobs, meta)]


def storage_recipients(session: str, env=None, harness: Optional[str] = None) -> list:
    """``[(storage key, attempt ids | None)]``: where records addressed to ``session`` live.

    The session's own key first (all its records); then one entry per registered parent it
    took over from, limited to the attempts bound by the handover.  A pending record is
    stored under the *registered* parent (A) and keeps its delivery id, digest, lease and
    ack there; only the receiving session changes."""
    out: list = [(session, None)]
    try:
        seat = pane_seat(env, harness, session)
        if seat is None:
            return out
        owed: dict = {}
        for row in _chain_to(handover_rows(seat), session):     # A -> B -> ... -> session
            bindings = [b for b in row.get("bindings") or () if isinstance(b, dict) and b.get("attempt")]
            owed.setdefault(str(row["from"]), set()).update(b["attempt"] for b in bindings)
            for binding in bindings:     # a chain that began at another seat: the registered key
                if binding.get("parent"):
                    owed.setdefault(str(binding["parent"]), set()).add(binding["attempt"])
        for key, attempts in owed.items():
            if key != session:
                out.append((key, frozenset(attempts)))
    except Exception:  # noqa: BLE001
        return [(session, None)]
    return out


def _chain_to(rows: list, session: str) -> list:
    """Rows of the handover chain that ends at ``session`` (the row into it, then the rows into its from)."""
    chain, want, seen = [], session, set()
    while want and want not in seen:
        seen.add(want)
        row = next((r for r in reversed(rows) if r.get("sid") == want), None)
        if row is None:
            break
        chain.append(row)
        want = str(row.get("from") or "")
    return chain


def record_for_session(record: dict, allowed: Optional[frozenset]) -> bool:
    """Whether a record found under a storage key may be delivered to the taker: every attempt
    of it is one the handover bound (or the key is the session's own)."""
    if allowed is None:
        return True
    ids = record.get("attempt_ids")
    return isinstance(ids, list) and bool(ids) and all(i in allowed for i in ids)


# ---------------------------------------------------------------------------
# Snapshot (at enqueue) and the ledger row (at the successor's confirmed start)
# ---------------------------------------------------------------------------

def _default_jobs() -> Optional[Path]:
    env = os.environ.get("AGENT_DISPATCH_JOBS")
    if env:
        return Path(env)
    try:
        from dispatch_contract import resolve_dispatch_state_root
        return resolve_dispatch_state_root(ROOT, None) / "jobs.log"
    except Exception:  # noqa: BLE001
        return None


def _delivery_open(jobs, meta: dict) -> bool:
    """A finished attempt still owes its completion when a pending record for it is open."""
    try:
        import dispatch_pending_delivery as pending
        directory = pending.record_directory(Path(jobs).resolve(strict=False).parent, meta["parent_sid"])
        for path in directory.glob("delivery-*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and meta["attempt_id"] in (value.get("attempt_ids") or ()) \
                    and value.get("state") in pending.OPEN_STATES:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _route_open(jobs, meta: dict) -> bool:
    """An acknowledged terminal attempt can still own an unfinished route."""
    route, digest, _ = route_identity(meta)
    filename = meta.get("owner_route_file") or meta.get("route_file")
    try:
        if meta.get("worker_type") == "owner" and meta.get("owner_route_file"):
            # Follow the existing verified owner generation, not an old closed anchor.
            from owner_route_binding import resolve_owner_route_lifecycle
            binding, status = resolve_owner_route_lifecycle(jobs, owner_attempt_id=meta["attempt_id"])
            if binding is None or status not in {"owner-route-launch-binding",
                    "owner-route-post-launch-attachment", "owner-route-advance-current"}:
                return False
            filename, route, digest = binding.route_file, binding.route_id, binding.route_hash
        if not filename:
            return False
        path = Path(filename)
        value = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(value, dict) and value.get("route_id") == route
                and value.get("route_hash") == digest
                and not os.path.lexists(path.with_suffix(".outcome.json")))
    except Exception:  # noqa: BLE001 - unreadable identity grants no new ownership
        return False


def current_bindings(harness: str, sid: str, jobs=None) -> list:
    """The depth-1 attempts ``sid`` answers for, whatever harness registered them.
    A finished one counts while its route or completion record is still open.
    ``harness`` is kept for callers; ownership alone decides."""
    jobs = jobs or _default_jobs()
    if jobs is None or not Path(jobs).is_file():
        return []
    found = []
    for aid, (status, meta) in latest_rows(jobs).items():
        if not eligible_row(meta):
            continue
        if not owns(meta, sid, jobs):
            continue
        if status not in OPEN_ROW_STATUSES and not (status == "done" and
                (_delivery_open(jobs, meta) or _route_open(jobs, meta))):
            continue
        found.append(binding_of(jobs, meta))
    return found[-MAX_BINDINGS:]


def write_snapshot_locked(seat, harness: str, sid: str, *, now: Optional[float] = None, jobs=None) -> int:
    """Take (or drop) the seat's snapshot at enqueue; returns the number of bindings.  Caller holds the seat lock."""
    st = _st()
    now = st.now_epoch() if now is None else now
    if seat.kind not in ("pane", "native"):
        return 0
    bindings = current_bindings(harness, sid, jobs)
    if not bindings:
        with contextlib.suppress(OSError):
            os.unlink(_snapshot_path(seat.key))
        return 0
    st.atomic_write_json(_snapshot_path(seat.key), {
        "schema": SCHEMA, "seat": seat.as_dict(), "from": {"harness": harness, "sid": sid},
        "at": now, "bindings": bindings})
    return len(bindings)


def record_locked(seat, harness: str, sid: str, event: str, source: str, now: float) -> Optional[dict]:
    """Append the A -> B row when ``sid`` is the confirmed successor of the snapshot's session
    (caller holds the seat lock).  Returns the row, or None (nothing to do / not confirmed)."""
    st = _st()
    if seat.kind not in ("pane", "native") or event not in ("start", "prompt"):
        return None
    snap = read_snapshot(seat.key)
    if not snap:
        return None
    origin = snap["from"]
    old = str(origin.get("sid") or "")
    if not old or old == sid or origin.get("harness") != harness:
        return None
    rows = handover_rows(seat)
    if any(r.get("sid") == sid for r in rows):
        return None                                    # the same A -> B again: nothing to do
    summary = st.session_summary(seat).get((harness, sid))
    if summary is None or float(summary.get("first_seen", 0) or 0) + 1.0 < float(snap.get("at", 0) or 0):
        return None                                    # a session older than the snapshot is not its successor
    if not _confirmed_clear(seat, harness, old, sid, event, source):
        return None
    taken = {(b.get("jobs"), b.get("route"), b.get("hash"), b.get("node")) for r in rows
             if r.get("from") == old for b in r.get("bindings") or () if isinstance(b, dict)}
    bindings = [b for b in snap["bindings"] if isinstance(b, dict)
                and (b.get("jobs"), b.get("route"), b.get("hash"), b.get("node")) not in taken]
    if not bindings or len(bindings) != len(snap["bindings"]):
        return None                                    # A -> C while A -> B stands: refused
    row = {"ts": now, "harness": harness, "sid": sid, "event": ROW_EVENT, "from": old,
           "source": source or ("first-message" if harness == "opencode" else "reservation"),
           "bindings": bindings, "epoch": int((summary or {}).get("epoch", 0) or 0)}
    path = st._ledger_path(seat)
    st.ensure_dir(path.parent)
    st._reject_symlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return row


def record_retire_handover(predecessor_sid: str, predecessor_harness: str, successor_sid: str,
                           successor_harness: str, *, env=None, jobs=None,
                           now: Optional[float] = None, successor_seat=None) -> Optional[dict]:
    """Hand a retired predecessor's routes to its seat successor, across harnesses.

    Called after ``peer-steward retire`` proved the predecessor exited and the caller runs in the
    pane started beside it.  The predecessor's unfinished depth-1 work (registered or inherited)
    are bound to the successor in the successor's seat: one ledger row, whose bindings name the
    registered parent their pending records stay stored under, and the seat's snapshot (now
    naming the successor) that lets :func:`effective_parent` find it.  Registry rows are untouched.
    Returns the row (the existing one when repeated), or None when there is nothing to hand over
    -- including attempts the predecessor already handed to another session.
    A deferred retire supplies the seat proven by its immutable requester and
    original successor mark; its observer need not own that physical pane.
    """
    if not predecessor_sid or not successor_sid or predecessor_sid == successor_sid:
        return None
    st = _st()
    seat = successor_seat or pane_seat(env, successor_harness, successor_sid)
    if seat is None or seat.kind != "pane":
        return None
    jobs = jobs or _default_jobs()
    now = st.now_epoch() if now is None else now
    with st.seat_lock(seat.key):
        existing = next((r for r in handover_rows(seat)
                         if r.get("from") == predecessor_sid and r.get("sid") == successor_sid), None)
        if existing is not None:
            return existing
        # Only what the predecessor answers for now: a registered parent that already handed an
        # attempt on keeps its name but no longer answers for it (never A -> B and A -> C).
        rows = latest_rows(jobs) if jobs else {}
        bindings = [b for b in current_bindings(predecessor_harness, predecessor_sid, jobs)
                    if b["attempt"] in rows and effective_parent(rows[b["attempt"]][1], jobs) == predecessor_sid]
        if not bindings:
            return None
        # The seat's snapshot names the session now at it (the successor), so its own later
        # /clear hands everything on; what its own snapshot already bound is kept.
        own = read_snapshot(seat.key)
        kept = own["bindings"] if own and (own["from"].get("sid") == successor_sid) else []
        merged: dict = {}
        for binding in [*kept, *bindings]:
            if isinstance(binding, dict):
                merged[(binding.get("jobs"), binding.get("route"), binding.get("hash"), binding.get("node"))] = binding
        st.atomic_write_json(_snapshot_path(seat.key), {
            "schema": SCHEMA, "seat": seat.as_dict(),
            "from": {"harness": successor_harness, "sid": successor_sid}, "at": now,
            "bindings": list(merged.values())[-MAX_BINDINGS:]})
        row = {"ts": now, "harness": successor_harness, "sid": successor_sid, "event": ROW_EVENT,
               "from": predecessor_sid, "source": "retire", "bindings": bindings}
        path = st._ledger_path(seat)
        st.ensure_dir(path.parent)
        st._reject_symlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return row


def _confirmed_clear(seat, harness: str, old: str, sid: str, event: str, source: str) -> bool:
    if harness == "opencode":
        return True                # no start hook: the first message of a new session after the snapshot
    if event == "start" and source == "clear":
        return True
    try:
        import session_tidy_clear as clear
        booking = clear.read_reservation(seat.key)
    except Exception:  # noqa: BLE001
        return False
    return bool(booking) and booking.get("sid") == old and booking.get("harness") == harness and (
        (booking.get("observed") or {}).get("sid") == sid or booking.get("new_session") == sid)


# ---------------------------------------------------------------------------
# The card line: the verified route and the existing resume command
# ---------------------------------------------------------------------------

def resume_line(seat, harness: str, sid: str) -> str:
    """One line for the successor's card: the routes it took over and the existing resume command.
    Only attempts the ledger shows handed to ``sid`` and the registry still shows open count."""
    try:
        rows = [r for r in handover_rows(seat) if r.get("sid") == sid]
        if not rows:
            return ""
        wanted = [b for r in rows for b in r.get("bindings") or () if isinstance(b, dict)]
        shown: dict = {}
        for b in wanted:
            jobs = b.get("jobs")
            if not jobs or not Path(jobs).is_file():
                continue
            for aid, (status, meta) in latest_rows(jobs).items():
                if not eligible_row(meta) or not _binds([b], jobs, meta) or not owns(meta, sid, jobs):
                    continue
                if status not in OPEN_ROW_STATUSES and not (status == "done" and _delivery_open(jobs, meta)):
                    continue
                shown.setdefault((jobs, route_identity(meta)[0]), meta.get("route_file", ""))
        parts = []
        for (jobs, route), route_file in list(shown.items())[:2]:
            if route_file:
                from parent_next_directive import resume_command
                command = resume_command(route_file, jobs, agent_home=ROOT)
                parts.append(f"route={route} 이어서: {command}")
        if not parts:
            return ""
        text = "[이어받은 진행 작업] " + " | ".join(parts)
        return _st()._cut_bytes(text, LINE_MAX_BYTES)[0]
    except Exception:  # noqa: BLE001
        return ""
