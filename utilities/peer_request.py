#!/usr/bin/env python3
"""Peer requests: the work state of one session asking another for something.

Delivery and work are two different facts. The peer-message pending row says
whether a message reached the receiving session (`pending` -> `queued` ->
`received`); this module keeps the request's own record, `peer-request-v1`,
which says whether the work is open, partly answered, answered, rejected or
cancelled. A delivery receipt never changes the work state and the work state
never changes a delivery obligation, with one exception: a revision that was
superseded or cancelled before it was delivered is not sent (`withdrawn`).

A request is closed only by an explicit final or reject report, or by the
requester's cancel. Nothing here infers completion from text, a first line or a
route ending.

Records live beside the peer-message transfers:
``<peer-state-root>/peer-messages/requests/<id>.json`` (0600, atomic replace).
The record is the one source of truth. ``requests/index.json`` maps each
request id to its two sessions; it is a cache rebuilt from the record files
whenever it lacks one of them or names one that is gone, so a request is found
even when the index was never written, was cut short or is unreadable. Nothing
in it is ever cut for its count.

One message, one reservation. A revision, a follow-up or a report is first
written into the record as a reservation (``sending``) in one locked update
that also fixes its number and its transfer ref; the message is then sealed
under exactly that number and ref, and the reservation is applied (the
revision becomes current, the report closes the request, ...). If the writer
dies after the seal, the next ordinary read of the request - a list, `show`,
`report`, the runner's maintenance pass - finds the sealed transfer and
applies the reservation (`settle`): the seal is the evidence. A reservation
whose seal does not exist stays in the record, shown as not confirmed, in both
sessions' lists; it is never removed for that.

Lock order: the per-request lock is the outermost lock. Nothing seals or
delivers a message while holding it, and code holding a pending or seat lock
only reads request records (they are replaced atomically).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA = "peer-request-v1"
REF_SCHEMA = "peer-request-ref-v1"
RETENTION_SECONDS = 14 * 86400  # the obligation retention (peer_obligations)
_ID = re.compile(r"rq-[0-9a-f]{6}\Z")
_SAFE = re.compile(r"[A-Za-z0-9._-]{1,160}\Z")
_HARNESSES = ("claude", "codex", "opencode")
_OPEN_STATES = ("open", "partial")
_CLOSED_STATES = ("final", "rejected", "cancelled")
_STATES = ("preparing",) + _OPEN_STATES + _CLOSED_STATES
_REVISION_STATES = ("current", "superseded", "cancelled")
_ROLES = ("request", "followup", "report", "late-report", "cancel")
_EVENT_LIMIT = 32
_MAX_RECORD_BYTES = 256 * 1024
_MAX_INDEX_BYTES = 16 * 1024 * 1024
INDEX_SCHEMA = "peer-request-index-v2"
_TITLE_LIMIT = 80
_RESULT_LIMIT = 120
_LOCK_WAIT_S = 10.0
SHOW_COMMAND = 'python3 "$AGENT_HOME/utilities/peer-steward.py" show'
REPORT_COMMAND = 'python3 "$AGENT_HOME/utilities/peer-steward.py" report'


def report_commands(request_id, revision):
    """The three report commands for one revision, ``--rev`` filled in (D8-2).

    A model uses them as written; the revision number is the one the line or body
    it just read belongs to, so a late report names its own revision."""
    base = "%s %s --rev %d" % (REPORT_COMMAND, request_id, revision)
    return {"final": base + ' --result "<one line>"',
            "partial": base + ' --partial --result "<one line>"',
            "reject": base + ' --reject "<reason>"'}

# One table for the three places that read a message's kind. A sealed request
# role wins over the first line; the first line is the fallback for messages
# that carry no request field. (kind, read_only)
_ROLE_KIND = {
    "request": ("request", False),
    "followup": ("request", False),
    "report": ("report", True),
    "late-report": ("report", True),
    "cancel": ("notice", True),
}
# Longest prefix first: `[요청 답` and `[요청 취소` begin with `[요청 `.
_PREFIX_KIND = (
    ("[요청 답 ", ("report", True)),
    ("[요청 취소 ", ("notice", True)),
    ("[요청 ", ("request", False)),
    ("[steer]", ("steer", False)),
    ("[handoff]", ("handoff", False)),
    ("[gate]", ("gate-relay", False)),
    ("[report]", ("report", True)),
    ("[notice]", ("notice", True)),
)
READ_ONLY_PEER_PREFIXES = tuple(prefix for prefix, (_k, ro) in _PREFIX_KIND if ro)


# --- locations -------------------------------------------------------------

def _state_root():
    override = os.environ.get("AGENT_PEER_LEDGER_ROOT")
    if override:
        return Path(override)
    from dispatch_contract import stable_state_root
    return stable_state_root(os.environ)


def requests_dir(*, create=False):
    messages = _state_root() / "peer-messages"
    path = messages / "requests"
    if messages.is_symlink() or path.is_symlink():
        raise ValueError("peer-request-directory-symlink")
    if create:
        messages.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def _record_path(request_id, *, create=False):
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ValueError("request-id-invalid")
    return requests_dir(create=create) / (request_id + ".json")


def index_path(*, create=False):
    """The index file (a cache of each request's two sessions; see the module text)."""
    return requests_dir(create=create) / "index.json"


# --- file primitives -------------------------------------------------------

_LOCKS_HELD = []  # lock names this process holds now (test observation, lock order)


@contextlib.contextmanager
def request_lock(key):
    """Exclusive lock for one request id (or the index). Waits, never spins."""
    directory = requests_dir(create=True)
    path = directory / (".lock-" + key)
    deadline = time.monotonic() + _LOCK_WAIT_S
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        _LOCKS_HELD.append(key)
        try:
            yield
        finally:
            _LOCKS_HELD.remove(key)
    finally:
        os.close(fd)


def _read_json(path, *, limit=_MAX_RECORD_BYTES):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError("peer-request-file-unsafe")
        return json.loads(handle.read(limit + 1))


def _atomic_write(path, payload):
    tmp = path.with_name(".tmp-%s-%s" % (path.name, secrets.token_hex(4)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _valid_record(record, request_id):
    return (isinstance(record, dict) and record.get("schema") == SCHEMA
            and record.get("id") == request_id and record.get("state") in _STATES
            and isinstance(record.get("revisions"), list)
            and isinstance(record.get("reports"), list)
            and isinstance(record.get("sending", []), list)
            and _endpoint(record.get("requester")) and _endpoint(record.get("recipient")))


def _endpoint(value):
    return (isinstance(value, dict) and value.get("harness") in _HARNESSES
            and isinstance(value.get("session_id"), str) and bool(_SAFE.fullmatch(value["session_id"])))


def load(request_id):
    """The validated record (also one whose first revision is not confirmed), or None.

    Read only: it never settles, so it is safe under a pending or seat lock."""
    try:
        record = _read_json(_record_path(request_id))
    except (OSError, ValueError):
        return None
    return record if _valid_record(record, request_id) else None


def _load_for_update(request_id):
    record = _read_json(_record_path(request_id))
    if not _valid_record(record, request_id):
        raise ValueError("request-unknown")
    return record


def _store(record):
    record["updated"] = time.time()
    _atomic_write(_record_path(record["id"], create=True), record)


def _event(record, what, **fields):
    events = record.setdefault("events", [])
    events.append(dict(fields, at=time.time(), event=what))
    del events[:-_EVENT_LIMIT]


# --- index -----------------------------------------------------------------

def _key(endpoint):
    return "%s:%s" % (endpoint["harness"], endpoint["session_id"])


def _record_ids():
    """Every request id that has a record file (the source of truth for the index)."""
    try:
        names = os.listdir(requests_dir())
    except (OSError, ValueError):
        return set()
    return {name[:-5] for name in names if name.endswith(".json") and _ID.fullmatch(name[:-5])}


def _index_load():
    try:
        data = _read_json(index_path(), limit=_MAX_INDEX_BYTES)
    except (OSError, ValueError):
        return {}
    parties = data.get("parties") if isinstance(data, dict) else None
    if not isinstance(parties, dict):
        return {}
    return {request_id: pair for request_id, pair in parties.items()
            if isinstance(request_id, str) and _ID.fullmatch(request_id)
            and isinstance(pair, list) and len(pair) == 2 and all(isinstance(k, str) for k in pair)}


def _index_store(parties):
    _atomic_write(index_path(create=True), {"schema": INDEX_SCHEMA, "parties": parties})


def _index_record(record):
    """Add one request to the index right after its record is published (a hint only)."""
    with request_lock("index"):
        parties = _index_load()
        parties[record["id"]] = [_key(record["requester"]), _key(record["recipient"])]
        _index_store(parties)


def _index():
    """``{id: [requester key, recipient key]}`` for every record on disk.

    The record files are read for any id the index lacks, and ids whose record is
    gone are dropped; the corrected index is written back. A request's two sessions
    never change after it is created, so a cached pair stays right."""
    parties, ids = _index_load(), _record_ids()
    if ids == set(parties):
        return parties
    with contextlib.suppress(OSError, ValueError):
        with request_lock("index"):
            parties, ids = _index_load(), _record_ids()
            changed = False
            for request_id in set(parties) - ids:
                del parties[request_id]
                changed = True
            for request_id in sorted(ids - set(parties)):
                record = load(request_id)
                if record is not None:
                    parties[request_id] = [_key(record["requester"]), _key(record["recipient"])]
                    changed = True
            if changed:
                _index_store(parties)
            return parties
    # The index could not be locked or written: answer from the records alone.
    for request_id in set(parties) - ids:
        del parties[request_id]
    for request_id in ids - set(parties):
        record = load(request_id)
        if record is not None:
            parties[request_id] = [_key(record["requester"]), _key(record["recipient"])]
    return parties


# --- identity following (N3) ------------------------------------------------

def follow(endpoint):
    """The session now answerable for ``endpoint``: a confirmed successor or itself.

    Only the lineage Hearting already confirms moves a request. A read that fails
    (unreadable, forked, unresolved) leaves it on the original session: it is
    neither moved to a guessed session nor treated as lost.
    """
    harness, sid = endpoint["harness"], endpoint["session_id"]
    try:
        import dispatch_seat_handover as handover
        new_sid, new_harness = handover.session_successor(sid, harness=harness)
    except Exception:  # noqa: BLE001 - an unconfirmed observation never moves the request
        return {"harness": harness, "session_id": sid}
    if new_sid and new_harness in _HARNESSES and _SAFE.fullmatch(new_sid):
        return {"harness": new_harness, "session_id": new_sid}
    return {"harness": harness, "session_id": sid}


def current_recipient(record):
    return follow(record["recipient"])


def current_requester(record):
    return follow(record["requester"])


def _same(left, right):
    return (left.get("harness"), left.get("session_id")) == (right.get("harness"), right.get("session_id"))


def _predecessors(harness, sid):
    """Sessions whose confirmed successor is ``sid`` (reverse lineage, verified forward)."""
    try:
        import dispatch_seat_handover as handover
        graph = handover.lineage_graph()
        reverse = {}
        for edge_harness, origin, target in graph.edges:
            if edge_harness == harness:
                reverse.setdefault(target, set()).add(origin)
        found, pending = [], list(reverse.get(sid, ()))
        seen = {sid}
        while pending:
            origin = pending.pop()
            if origin in seen or not _SAFE.fullmatch(origin or ""):
                continue
            seen.add(origin)
            if follow({"harness": harness, "session_id": origin}).get("session_id") == sid:
                found.append(origin)
            pending.extend(reverse.get(origin, ()))
        return found
    except Exception:  # noqa: BLE001
        return []


# --- reservations: number and ref first, seal second, apply third -----------

def _reservation(record, what, role, revision, number, **fields):
    record["seq"] = int(record.get("seq") or 0) + 1
    entry = dict(fields, seq=record["seq"], what=what, role=role, revision=int(revision),
                 n=int(number), ref=secrets.token_hex(16), at=time.time())
    record.setdefault("sending", []).append(entry)
    return entry


def _sending(record, what=None):
    return [entry for entry in record.get("sending") or ()
            if isinstance(entry, dict) and (what is None or entry.get("what") == what)]


def _latest_revision(record):
    """``(n, label)`` of the newest revision that exists: applied, or sealed and about
    to be. A reservation without a seal is not a revision (nothing went to the recipient)."""
    entries = [(r["n"], r.get("label", "")) for r in record["revisions"]]
    entries += [(r["n"], r.get("label", "")) for r in _sending(record, "revision")
                if _sealed(record["id"], r) is True]
    return max(entries) if entries else (0, "")


def _sealed(request_id, entry):
    """Is the transfer sealed under ``entry`` there? ``True``: it is. ``False``: the
    record is confirmed absent (or is not this seal). ``None``: could not be read
    (permission, timeout, a damaged file): unknown is never treated as absent."""
    ref = entry.get("ref")
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{32}", ref):
        return False
    try:
        transfer = _read_json(requests_dir().parent / "transfers" / (ref + ".json"))
    except (OSError, ValueError):
        return None
    return (isinstance(transfer, dict) and transfer.get("message_id") == ref
            and transfer.get("request") == request_seal(request_id, entry["revision"], entry["role"]))


def _apply(record, entry, transfer_ref=None):
    """Apply one reservation to ``record``; return what it became (None: not yet)."""
    ref = transfer_ref or entry["ref"]
    what = entry["what"]
    if what == "revision":
        number = entry["n"]
        newer = any(r["n"] > number for r in record["revisions"])
        state = "cancelled" if record["state"] == "cancelled" else "superseded" if newer else "current"
        if state == "current":
            for revision in record["revisions"]:
                if revision.get("state") == "current":
                    revision["state"] = "superseded"
        applied = _revision(number, entry.get("label", ""), ref, state=state)
        record["revisions"].append(applied)
        record["revisions"].sort(key=lambda r: r["n"])
        if record["state"] == "preparing":
            record["state"] = "open"
        _event(record, "opened" if number == 1 else "revised", revision=number)
    elif what == "followup":
        applied = revision_of(record, entry["revision"])
        if applied is None:
            return None  # its revision is not applied yet; the next settle does both in order
        if ref not in applied["followups"]:
            applied["followups"].append(ref)
        _event(record, "followup", revision=entry["revision"])
    elif what == "report":
        target = revision_of(record, entry["revision"])
        if target is None:
            return None
        late = bool(entry.get("late") or target.get("state") != "current"
                    or record["state"] in _CLOSED_STATES)
        started = target.get("created") or record["created"]
        applied = {"n": entry["n"], "revision": entry["revision"], "kind": entry["kind"],
                   "result": entry.get("result", ""), "remaining": entry.get("remaining", ""),
                   "artifact": entry.get("artifact", ""), "transfer_ref": ref,
                   "reporter": entry.get("reporter"), "at": entry["at"],
                   "elapsed": max(0.0, entry["at"] - started), "late": late}
        record["reports"].append(applied)
        record["reports"].sort(key=lambda r: r["n"])
        if not late:
            record["state"] = {"partial": "partial", "final": "final", "reject": "rejected"}[entry["kind"]]
        _event(record, "report", report=entry["n"], report_kind=entry["kind"], late=late)
    else:
        return None
    record["sending"] = [other for other in _sending(record) if other.get("ref") != entry["ref"]]
    return applied


def _applied(record, entry_ref, transfer_ref):
    """What an already-applied reservation became (a retry of the same confirm)."""
    for revision in record["revisions"]:
        if revision.get("transfer_ref") in (entry_ref, transfer_ref):
            return revision
        if transfer_ref in revision.get("followups", ()) or entry_ref in revision.get("followups", ()):
            return revision
    for report in record["reports"]:
        if report.get("transfer_ref") in (entry_ref, transfer_ref):
            return report
    return None


def _settle_locked(record, *, known=None, transfer_ref=None):
    """Apply every reservation whose seal exists, oldest first. ``known`` is a
    reservation its own writer just sealed (as ``transfer_ref``)."""
    changed, result = False, None
    for entry in sorted(_sending(record), key=lambda e: e.get("seq", 0)):
        mine = entry.get("ref") == known
        if mine or _sealed(record["id"], entry) is True:
            applied = _apply(record, entry, transfer_ref if mine else None)
            if applied is not None:
                changed = True
                if mine:
                    result = applied
    # A revision reservation below an applied one that has no seal (its writer died, or
    # has not sealed yet) is closed as superseded. Unknown (unreadable) is left as it is.
    newest = max([r["n"] for r in record["revisions"]] + [0])
    for lower in _sending(record, "revision"):
        if lower["n"] < newest and _sealed(record["id"], lower) is False:
            record["revisions"].append(dict(_revision(lower["n"], lower.get("label", ""),
                                                      lower["ref"], state="superseded"),
                                            unsealed=True))
            record["revisions"].sort(key=lambda r: r["n"])
            record["sending"] = [o for o in _sending(record) if o.get("ref") != lower["ref"]]
            changed = True
    return changed, result


def settle(request_id):
    """Finish an interrupted confirmation from its seal; the settled record (or None)."""
    record = load(request_id)
    if record is None or not _sending(record):
        return record
    try:
        with request_lock(request_id):
            record = _load_for_update(request_id)
            changed, _result = _settle_locked(record)
            if changed:
                _store(record)
            return record
    except (OSError, ValueError):
        return load(request_id)


def load_settled(request_id):
    """:func:`load` after :func:`settle` (never while a pending or seat lock is held)."""
    try:
        return settle(request_id)
    except ValueError:
        return None


def confirm(request_id, reserved_ref, transfer_ref=None):
    """After a reservation's message is sealed: apply it. ``(record, applied)``.

    Earlier reservations whose seals exist are applied first, in order. A retry
    (already applied) returns what it became; nothing else is written."""
    with request_lock(request_id):
        record = _load_for_update(request_id)
        if not any(entry.get("ref") == reserved_ref for entry in _sending(record)):
            applied = _applied(record, reserved_ref, transfer_ref)
            if applied is None:
                raise ValueError("reservation-unknown")
            return record, applied
        _changed, applied = _settle_locked(record, known=reserved_ref, transfer_ref=transfer_ref)
        _store(record)
        return record, applied


def abort(request_id, reserved_ref):
    """Drop a reservation whose seal failed. A new request with nothing else is removed.

    A reservation whose seal exists is never dropped: the message is sealed, so the
    next ordinary access finishes it (:func:`settle`)."""
    with request_lock(request_id):
        try:
            record = _read_json(_record_path(request_id))
        except (OSError, ValueError):
            return False
        if not _valid_record(record, request_id):
            return False
        mine = [e for e in _sending(record) if e.get("ref") == reserved_ref]
        if not mine or any(_sealed(request_id, e) is not False for e in mine):
            return False  # sealed, or not known to be absent: kept for the next access
        record["sending"] = [e for e in _sending(record) if e.get("ref") != reserved_ref]
        if record["state"] == "preparing" and not record["revisions"] and not record["sending"]:
            with contextlib.suppress(OSError):
                _record_path(request_id).unlink()
            return True
        _store(record)
        return True


# --- creation and revisions -------------------------------------------------

def open_request(requester, recipient, *, title, address="", label=""):
    """Write a new request with revision 1 reserved: ``(record, reservation)``.

    It is listed for both sessions at once (as not confirmed until its seal exists)."""
    if not (_endpoint(requester) and _endpoint(recipient)):
        raise ValueError("request-endpoint-invalid")
    directory = requests_dir(create=True)
    for _attempt in range(8):
        request_id = "rq-" + secrets.token_hex(3)
        now = time.time()
        record = {"schema": SCHEMA, "id": request_id, "title": clean(title, _TITLE_LIMIT),
                  "created": now, "updated": now,
                  "requester": _identity(requester), "recipient": _identity(recipient),
                  "address": clean(address, 200), "state": "preparing",
                  "revisions": [], "reports": [], "cancel": None, "events": [], "sending": []}
        entry = _reservation(record, "revision", "request", 1, 1, label=clean(label, 80))
        if _publish_new(directory / (request_id + ".json"), record):
            with contextlib.suppress(OSError, ValueError):
                _index_record(record)
            return record, entry
    raise ValueError("request-id-exhausted")


def _publish_new(path, record):
    """Create ``path`` with ``record`` at once (never a half-written file); False if taken."""
    tmp = path.with_name(".tmp-%s-%s" % (path.name, secrets.token_hex(4)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def reserve(requester, recipient, *, title, address="", label=""):
    """The id of a new request whose revision 1 is reserved (see :func:`open_request`)."""
    return open_request(requester, recipient, title=title, address=address, label=label)[0]["id"]


def _identity(endpoint):
    return {"harness": endpoint["harness"], "session_id": endpoint["session_id"],
            "name": clean(endpoint.get("name") or "", 80)}


def commit_open(request_id, transfer_ref, *, label=None):
    """Confirm revision 1 of a new request as sealed under ``transfer_ref``."""
    record = load(request_id)
    if record is None:
        raise ValueError("request-unknown")
    first = next((e for e in _sending(record, "revision") if e.get("n") == 1), None)
    if first is None:
        raise ValueError("request-not-preparing")
    if label is not None:
        with request_lock(request_id):
            current = _load_for_update(request_id)
            for entry in _sending(current, "revision"):
                if entry.get("n") == 1:
                    entry["label"] = clean(label, 80)
            _store(current)
    return confirm(request_id, first["ref"], transfer_ref)[0]


def _revision(number, label, transfer_ref, state="current"):
    return {"n": number, "label": clean(label, 80), "transfer_ref": transfer_ref,
            "created": time.time(), "state": state, "followups": []}


def current_revision(record):
    for revision in reversed(record["revisions"]):
        if revision.get("state") == "current":
            return revision
    return record["revisions"][-1] if record["revisions"] else None


def revision_of(record, number):
    for revision in record["revisions"]:
        if revision.get("n") == number:
            return revision
    return None


def reserve_message(request_id, *, label=None):
    """Reserve the requester's next message: ``(record, reservation)``.

    A label that differs from the newest revision's makes a new revision (number
    fixed here, under the lock); otherwise it is a follow-up of the newest one."""
    with request_lock(request_id):
        record = _load_for_update(request_id)
        _settle_locked(record)
        if record["state"] == "preparing":
            raise ValueError("request-incomplete")
        if record["state"] not in _OPEN_STATES:
            raise ValueError("request-closed")
        newest, newest_label = _latest_revision(record)
        if label is not None and label != newest_label:
            # Every send has its own reservation and ref, the next number above every
            # applied and reserved revision. An earlier reservation without a seal is
            # never taken over: it is closed as superseded when a newer revision applies.
            top = max([r["n"] for r in record["revisions"]]
                      + [e["n"] for e in _sending(record, "revision")] + [0])
            entry = _reservation(record, "revision", "request", top + 1, top + 1,
                                 label=clean(label, 80))
        else:
            target = revision_of(record, newest)
            serial = (len(target["followups"]) if target else 0) + len(
                [e for e in _sending(record, "followup") if e.get("revision") == newest]) + 1
            entry = _reservation(record, "followup", "followup", newest, serial,
                                 label=newest_label)
        _store(record)
        return record, entry


def reserve_report(request_id, *, kind, revision, result="", remaining="", artifact="",
                   reporter=None):
    """Reserve one report: ``(record, reservation)``; its number is fixed here.

    The report belongs to the revision it answers. A report for a revision that is
    no longer current, or for a request already closed, is `late`: once applied it
    is kept on that revision and changes neither the request state nor the newest
    result. A partial report keeps the request open. Running the same report
    command again reserves another report: it is a new report, not a retry.
    """
    if kind not in ("partial", "final", "reject"):
        raise ValueError("report-kind-invalid")
    with request_lock(request_id):
        record = _load_for_update(request_id)
        _settle_locked(record)
        if record["state"] == "preparing":
            raise ValueError("request-incomplete")
        target = revision_of(record, revision)
        if target is None:
            raise ValueError("revision-unknown")
        late = target.get("state") != "current" or record["state"] in _CLOSED_STATES
        number = max([r["n"] for r in record["reports"]] + [e["n"] for e in _sending(record, "report")]
                     + [0]) + 1
        entry = _reservation(record, "report", "late-report" if late else "report", revision, number,
                             kind=kind, result=clean(result, _RESULT_LIMIT),
                             remaining=clean(remaining, 400), artifact=clean(artifact, 300),
                             reporter=_identity(reporter) if _endpoint(reporter) else None,
                             late=late)
        _store(record)
        return record, entry


def cancel(request_id, *, reason=""):
    """Cancel the open work. Every non-superseded revision becomes `cancelled`;
    a revision still reserved is cancelled when it is applied."""
    with request_lock(request_id):
        record = _load_for_update(request_id)
        _settle_locked(record)
        if record["state"] not in _OPEN_STATES + ("preparing",):
            raise ValueError("request-closed")
        for revision in record["revisions"]:
            if revision.get("state") == "current":
                revision["state"] = "cancelled"
        record["state"] = "cancelled"
        record["cancel"] = {"at": time.time(), "reason": clean(reason, 200), "notice_ref": None}
        _event(record, "cancelled")
        _store(record)
        return record


def set_cancel_notice(request_id, notice_ref, *, failed=""):
    with request_lock(request_id):
        record = _load_for_update(request_id)
        if record.get("cancel") is None:
            return record
        if notice_ref:
            record["cancel"]["notice_ref"] = notice_ref
        if failed:
            _event(record, "cancel_notice_failed", reason=clean(failed, 120))
        _store(record)
        return record


def note_body_unreadable(request_id, reason):
    """The one thing `show` writes: that the sealed body could not be read."""
    try:
        with request_lock(request_id):
            record = _load_for_update(request_id)
            _event(record, "body_unreadable", reason=clean(reason, 120))
            _store(record)
    except (OSError, ValueError):
        pass


def message_refs(record):
    """Every transfer ref of the requester's messages (revisions, follow-ups, reserved ones)."""
    refs = []
    for revision in record["revisions"]:
        refs.append(revision.get("transfer_ref"))
        refs.extend(revision.get("followups") or ())
    refs.extend(e.get("ref") for e in _sending(record) if e.get("what") in ("revision", "followup"))
    return [ref for ref in refs if isinstance(ref, str) and ref]


def unconfirmed(record):
    """The reservations of ``record`` whose seal is not there (yet): what is not confirmed."""
    return _sending(record)


# --- reads -----------------------------------------------------------------

def session_requests(harness, sid):
    """``{"out": [...], "in": [...]}`` for a session, including its confirmed
    predecessors' requests, read from the records (the index only narrows which).

    Every request of the session is in it, however many there are; an interrupted
    confirmation is finished here first (:func:`settle`). A request whose first
    revision has no seal stays listed (state `preparing`, not confirmed)."""
    keys = {"%s:%s" % (harness, sid)} | {"%s:%s" % (harness, old) for old in _predecessors(harness, sid)}
    me = {"harness": harness, "session_id": sid}
    out, received = [], []
    for request_id, (requester_key, recipient_key) in sorted(_index().items()):
        if requester_key not in keys and recipient_key not in keys:
            continue
        record = load(request_id)
        if record is not None and _sending(record):
            record = load_settled(request_id)
        if record is None:
            continue
        actual = [_key(record["requester"]), _key(record["recipient"])]
        if actual != [requester_key, recipient_key]:
            # The record wins over a wrong index entry, which is put right here.
            requester_key, recipient_key = actual
            with contextlib.suppress(OSError, ValueError):
                with request_lock("index"):
                    parties = _index_load()
                    parties[request_id] = actual
                    _index_store(parties)
        if requester_key in keys and _same(current_requester(record), me):
            out.append(record)
        if recipient_key in keys and _same(current_recipient(record), me):
            received.append(record)
    key = lambda record: (record["state"] not in _OPEN_STATES + ("preparing",),  # noqa: E731
                          -record.get("updated", 0))
    return {"out": sorted(out, key=key), "in": sorted(received, key=key)}


def withdrawn(row):
    """True when a still-undelivered request row belongs to a revision that was
    superseded or cancelled: it must not be sent. Delivered rows are never undone.

    Read only (a carrier calls it under its pending lock): a cancelled request,
    a revision marked superseded or cancelled, or a newer applied revision
    withdraws it."""
    if not isinstance(row, dict) or row.get("state") != "pending":
        return False
    for ref in row.get("refs") or ():
        parsed = parse_ref(ref)
        if parsed is None or parsed["role"] not in ("request", "followup"):
            continue
        record = load(parsed["id"])
        if record is None:
            continue
        if record["state"] == "cancelled":
            return True
        revision = revision_of(record, parsed["revision"])
        if revision is not None and revision.get("state") in ("superseded", "cancelled"):
            return True
        if any(r["n"] > parsed["revision"] for r in record["revisions"]):
            return True
    return False


def ref_for(request_id, revision, role):
    return "peer-request:%s:%d:%s" % (request_id, revision, role)


def parse_ref(ref):
    if not isinstance(ref, str) or not ref.startswith("peer-request:"):
        return None
    parts = ref.split(":")
    if (len(parts) != 4 or not _ID.fullmatch(parts[1]) or not parts[2].isdigit()
            or parts[3] not in _ROLES):
        return None
    return {"id": parts[1], "revision": int(parts[2]), "role": parts[3]}


def request_seal(request_id, revision, role):
    """The `request` field sealed into the transfer record."""
    if role not in _ROLES:
        raise ValueError("request-role-invalid")
    return {"schema": REF_SCHEMA, "id": request_id, "revision": int(revision), "role": role}


def valid_seal(seal):
    return (isinstance(seal, dict) and seal.get("schema") == REF_SCHEMA
            and isinstance(seal.get("id"), str) and bool(_ID.fullmatch(seal["id"]))
            and type(seal.get("revision")) is int and seal.get("role") in _ROLES
            and set(seal) == {"schema", "id", "revision", "role"})


def delivery_ref_refs(row):
    """Every request ref a pending row names."""
    return [p for p in (parse_ref(r) for r in row.get("refs") or ()) if p]


def message_kind(text="", transfer=None):
    """``(kind, read_only)``: the sealed request role first, then the first line.

    One answer for the ledger kind, the Codex follow-through pass and the Claude
    record hook. Ordinary messages stay steer."""
    seal = (transfer or {}).get("request") if isinstance(transfer, dict) else None
    if valid_seal(seal):
        return _ROLE_KIND[seal["role"]]
    first = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    for prefix, answer in _PREFIX_KIND:
        if first.startswith(prefix):
            return answer
    return "steer", False


# --- retention --------------------------------------------------------------

def prune(now=None, *, examine=64):
    """Remove records closed for more than the retention period. An open request, or
    one not yet confirmed (`preparing`), is never removed for its age: it stays until it
    is cancelled. Returns the removed ids.
    A removed record leaves the index by itself (the index follows the files)."""
    now = time.time() if now is None else now
    try:
        directory = requests_dir()
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".json") and _ID.fullmatch(name[:-5]))
    except (OSError, ValueError):
        return []
    removed = []
    for name in names[:examine]:
        request_id = name[:-5]
        record = load(request_id)
        if record is None:
            continue
        stamp = record.get("updated") or record.get("created") or now
        if record["state"] not in _CLOSED_STATES or now - stamp < RETENTION_SECONDS:
            continue
        with contextlib.suppress(OSError, ValueError):
            with request_lock(request_id):
                current = load(request_id)
                if (current is None or current["state"] not in _CLOSED_STATES
                        or now - (current.get("updated") or stamp) < RETENTION_SECONDS):
                    continue
                _record_path(request_id).unlink()
                removed.append(request_id)
    return removed


def maintain(now=None):
    """The runner's due pass: settle every interrupted confirmation, then prune.
    Returns ``{"settled": n, "pruned": n}``."""
    settled = 0
    for request_id in sorted(_record_ids()):
        record = load(request_id)
        if record is not None and _sending(record):
            before = len(_sending(record))
            after = load_settled(request_id)
            if after is not None and len(_sending(after)) < before:
                settled += 1
    return {"settled": settled, "pruned": len(prune(now))}


def load_any(request_id):
    """Same as :func:`load` (kept for retention readers)."""
    return load(request_id)


# --- rendering ---------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean(text, limit):
    """Single-line display text: no control characters, `│` becomes `|`, cut at ``limit``."""
    value = _CONTROL.sub(" ", str(text or "")).replace("│", "|").strip()
    return value if len(value) <= limit else value[:max(0, limit - 1)].rstrip() + "…"


def elapsed_text(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 90 * 60:
        return "%dm" % round(seconds / 60)
    if seconds < 48 * 3600:
        return "%dh" % round(seconds / 3600)
    return "%dd" % round(seconds / 86400)


def _name(endpoint):
    return endpoint.get("name") or endpoint["session_id"][:8]


def render_line(record, role, *, revision=None, label="", report=None, body_lines=0):
    """The one line a conversation window gets. The sealed body is not in it."""
    request_id = record["id"]
    title = clean(record.get("title"), _TITLE_LIMIT)
    requester, recipient = _name(record["requester"]), _name(record["recipient"])
    if role in ("request", "followup"):
        parts = ["[요청 %s] [%s] → [%s]" % (request_id, requester, recipient), title]
        if revision and (label or revision > 1):
            parts.append(("판 %d %s" % (revision, clean(label, 80))).strip())
        if role == "followup":
            parts.append("추가 확인")
        if body_lines:
            parts.append("본문 %d줄" % body_lines)
        parts.append("열기: %s %s" % (SHOW_COMMAND, request_id))
        return " │ ".join(part for part in parts if part)
    if role in ("report", "late-report"):
        late = ""
        if report and report.get("late"):
            rev = revision_of(record, report["revision"])
            late = " 판 %d(%s)" % (report["revision"], "대체됨" if rev and rev.get("state") == "superseded"
                                    else "취소됨" if rev and rev.get("state") == "cancelled" else "닫힘")
        kind = (report or {}).get("kind")
        label = {"partial": "일부", "final": "최종", "reject": "거절"}.get(kind, "")
        parts = ["[요청 답 %s%s] [%s]" % (request_id, late, recipient), title,
                 ("늦은 답 " if late else "") + label]
        if report:
            if report.get("result"):
                parts.append(report["result"])
            parts.append(elapsed_text(report.get("elapsed") or 0))
            parts.append("본문: %s %s --report %d" % (SHOW_COMMAND, request_id, report["n"]))
        return " │ ".join(part for part in parts if part)
    if role == "cancel":
        parts = ["[요청 취소 %s] [%s]" % (request_id, requester), title]
        reason = (record.get("cancel") or {}).get("reason")
        if reason:
            parts.append("이유 " + reason)
        return " │ ".join(parts)
    raise ValueError("request-role-invalid")


def delivery_word(row):
    """The delivery column from the pending row: 보냄 / 전달됨 / 보임."""
    state = (row or {}).get("state")
    return {"pending": "보냄", "unverified": "보냄", "queued": "전달됨", "received": "보임"}.get(state, "")


def describe(record, *, rows=None, now=None):
    """Plain summary lines for `show`; ``rows`` maps a transfer ref to its pending row."""
    now = time.time() if now is None else now
    rows = rows or {}
    lines = ["request=%s state=%s title=%s" % (record["id"], record["state"], record.get("title", ""))]
    lines.append("from=%s to=%s" % (_name(record["requester"]), _name(record["recipient"])))
    for revision in record["revisions"]:
        line = "revision=%d state=%s label=%s delivery=%s" % (
            revision["n"], revision["state"], revision.get("label", ""),
            delivery_word(rows.get(revision["transfer_ref"])) or "-")
        if revision.get("unsealed") and _sealed(record["id"], {
                "ref": revision["transfer_ref"], "revision": revision["n"], "role": "request"}) is not True:
            line += " unconfirmed reason=no-seal"  # closed as superseded; nothing was ever sent
        lines.append(line)
    for report in record["reports"]:
        lines.append("report=%d kind=%s revision=%d late=%s elapsed=%s result=%s remaining=%s" % (
            report["n"], report["kind"], report["revision"], str(report["late"]).lower(),
            elapsed_text(report.get("elapsed") or 0), report.get("result", ""), report.get("remaining", "")))
    if record.get("cancel"):
        lines.append("cancelled reason=%s" % record["cancel"].get("reason", ""))
    for entry in _sending(record):
        # Reserved, and its sealed message is not there: not confirmed, never dropped.
        what = {"revision": "revision=%d" % entry["n"],
                "followup": "followup revision=%d" % entry["revision"],
                "report": "report=%d kind=%s revision=%d" % (entry["n"], entry.get("kind"),
                                                            entry["revision"])}.get(entry.get("what"))
        if what:
            lines.append("%s state=unconfirmed reason=no-seal" % what)
    lines.append("updated=%s ago" % elapsed_text(now - record.get("updated", now)))
    return lines
