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
``requests/by-session/<harness>/<sid>.json`` lists the request ids a session
sent (`out`) and received (`in`). The index is a discovery hint; every id it
names is re-read from its record before use.

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
_TITLE_LIMIT = 80
_RESULT_LIMIT = 120
_LOCK_WAIT_S = 10.0
SHOW_COMMAND = 'python3 "$AGENT_HOME/utilities/peer-steward.py" show'
REPORT_COMMAND = 'python3 "$AGENT_HOME/utilities/peer-steward.py" report'

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


def _index_path(harness, sid, *, create=False):
    if harness not in _HARNESSES or not isinstance(sid, str) or not _SAFE.fullmatch(sid):
        raise ValueError("request-index-key-invalid")
    directory = requests_dir(create=create) / "by-session" / harness
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / (sid + ".json")


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


def _read_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > _MAX_RECORD_BYTES):
            raise ValueError("peer-request-file-unsafe")
        return json.loads(handle.read(_MAX_RECORD_BYTES + 1))


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
            and _endpoint(record.get("requester")) and _endpoint(record.get("recipient")))


def _endpoint(value):
    return (isinstance(value, dict) and value.get("harness") in _HARNESSES
            and isinstance(value.get("session_id"), str) and bool(_SAFE.fullmatch(value["session_id"])))


def load(request_id):
    """The validated record, or None when absent, invalid or still preparing a seal."""
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

def _index_read(harness, sid):
    try:
        data = _read_json(_index_path(harness, sid))
    except (OSError, ValueError):
        return {"out": [], "in": []}
    if not isinstance(data, dict):
        return {"out": [], "in": []}
    return {side: [i for i in (data.get(side) or []) if isinstance(i, str) and _ID.fullmatch(i)]
            for side in ("out", "in")}


def _index_update(harness, sid, side, request_id, *, add):
    with request_lock("index"):
        data = _index_read(harness, sid)
        ids = [i for i in data[side] if i != request_id]
        if add:
            ids.append(request_id)
        data[side] = ids[-512:]
        if not any(data.values()):
            with contextlib.suppress(OSError, ValueError):
                _index_path(harness, sid).unlink()
            return
        _atomic_write(_index_path(harness, sid, create=True),
                      {"schema": "peer-request-index-v1", **data})


def _index_record(record, *, add):
    _index_update(record["requester"]["harness"], record["requester"]["session_id"],
                  "out", record["id"], add=add)
    _index_update(record["recipient"]["harness"], record["recipient"]["session_id"],
                  "in", record["id"], add=add)


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


# --- creation and revisions -------------------------------------------------

def reserve(requester, recipient, *, title, address=""):
    """Take an id and write a `preparing` record. It is in no index and no panel."""
    if not (_endpoint(requester) and _endpoint(recipient)):
        raise ValueError("request-endpoint-invalid")
    directory = requests_dir(create=True)
    for _attempt in range(8):
        request_id = "rq-" + secrets.token_hex(3)
        path = directory / (request_id + ".json")
        record = {"schema": SCHEMA, "id": request_id, "title": clean(title, _TITLE_LIMIT),
                  "created": time.time(), "updated": time.time(),
                  "requester": _identity(requester), "recipient": _identity(recipient),
                  "address": clean(address, 200), "state": "preparing",
                  "revisions": [], "reports": [], "cancel": None, "events": []}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        return request_id
    raise ValueError("request-id-exhausted")


def _identity(endpoint):
    return {"harness": endpoint["harness"], "session_id": endpoint["session_id"],
            "name": clean(endpoint.get("name") or "", 80)}


def commit_open(request_id, transfer_ref, *, label=""):
    """After the first revision is sealed: open the request and index it."""
    with request_lock(request_id):
        record = _load_for_update(request_id)
        if record["state"] != "preparing":
            if record["revisions"] and record["revisions"][0].get("transfer_ref") == transfer_ref:
                return record  # a retry of the same sealed message
            raise ValueError("request-not-preparing")
        record["state"] = "open"
        record["revisions"] = [_revision(1, label, transfer_ref)]
        _event(record, "opened", revision=1)
        _store(record)
        _index_record(record, add=True)
        return record


def abort_preparing(request_id):
    """Drop a `preparing` record whose seal failed. Anything else stays."""
    with request_lock(request_id):
        try:
            record = _read_json(_record_path(request_id))
        except (OSError, ValueError):
            return False
        if not _valid_record(record, request_id) or record["state"] != "preparing":
            return False
        with contextlib.suppress(OSError):
            _record_path(request_id).unlink()
        return True


def _revision(number, label, transfer_ref):
    return {"n": number, "label": clean(label, 80), "transfer_ref": transfer_ref,
            "created": time.time(), "state": "current", "followups": []}


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


def add_revision(request_id, transfer_ref, *, label=""):
    """Seal-then-commit: a new revision supersedes the current one."""
    with request_lock(request_id):
        record = _load_for_update(request_id)
        for revision in record["revisions"]:
            if revision.get("transfer_ref") == transfer_ref:
                return record, revision["n"]  # a retry of the same sealed message
        if record["state"] not in _OPEN_STATES:
            raise ValueError("request-closed")
        for revision in record["revisions"]:
            if revision.get("state") == "current":
                revision["state"] = "superseded"
        number = len(record["revisions"]) + 1
        record["revisions"].append(_revision(number, label, transfer_ref))
        _event(record, "revised", revision=number)
        _store(record)
        return record, number


def add_followup(request_id, transfer_ref):
    """A further message about the current revision (no new revision)."""
    with request_lock(request_id):
        record = _load_for_update(request_id)
        if record["state"] not in _OPEN_STATES:
            raise ValueError("request-closed")
        revision = current_revision(record)
        if transfer_ref in revision["followups"]:
            return record, revision["n"]
        revision["followups"].append(transfer_ref)
        _event(record, "followup", revision=revision["n"])
        _store(record)
        return record, revision["n"]


def record_report(request_id, *, kind, revision, result="", remaining="", artifact="",
                  transfer_ref="", reporter=None):
    """Attach one report; return ``(record, report)``.

    The report belongs to the revision it answers. A report for a revision that is
    no longer current, or for a request already closed, is `late`: it is kept on
    that revision and changes neither the request state nor the newest result.
    A partial report keeps the request open.
    """
    if kind not in ("partial", "final", "reject"):
        raise ValueError("report-kind-invalid")
    with request_lock(request_id):
        record = _load_for_update(request_id)
        if record["state"] == "preparing":
            raise ValueError("request-incomplete")
        for earlier in record["reports"]:
            if transfer_ref and earlier.get("transfer_ref") == transfer_ref:
                return record, earlier  # a retry of the same sealed message
        target = revision_of(record, revision)
        if target is None:
            raise ValueError("revision-unknown")
        late = target.get("state") != "current" or record["state"] in _CLOSED_STATES
        started = target.get("created") or record["created"]
        report = {"n": len(record["reports"]) + 1, "revision": revision, "kind": kind,
                  "result": clean(result, _RESULT_LIMIT), "remaining": clean(remaining, 400),
                  "artifact": clean(artifact, 300), "transfer_ref": transfer_ref,
                  "reporter": _identity(reporter) if _endpoint(reporter) else None,
                  "at": time.time(), "elapsed": max(0.0, time.time() - started), "late": late}
        record["reports"].append(report)
        if not late:
            record["state"] = {"partial": "partial", "final": "final", "reject": "rejected"}[kind]
        _event(record, "report", report=report["n"], report_kind=kind, late=late)
        _store(record)
        return record, report


def cancel(request_id, *, reason=""):
    """Cancel the open work. Every non-superseded revision becomes `cancelled`."""
    with request_lock(request_id):
        record = _load_for_update(request_id)
        if record["state"] not in _OPEN_STATES:
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


# --- reads -----------------------------------------------------------------

def session_requests(harness, sid):
    """``{"out": [...], "in": [...]}`` for a session, including its confirmed
    predecessors' requests. Index hints are verified against the record."""
    sources = [(harness, sid)] + [(harness, old) for old in _predecessors(harness, sid)]
    me = {"harness": harness, "session_id": sid}
    seen, out, received = set(), [], []
    for source_harness, source_sid in sources:
        index = _index_read(source_harness, source_sid)
        for side in ("out", "in"):
            for request_id in index[side]:
                if (side, request_id) in seen:
                    continue
                seen.add((side, request_id))
                record = load(request_id)
                if record is None or record["state"] == "preparing":
                    continue
                if side == "out" and _same(current_requester(record), me):
                    out.append(record)
                elif side == "in" and _same(current_recipient(record), me):
                    received.append(record)
    key = lambda record: (record["state"] not in _OPEN_STATES, -record.get("updated", 0))  # noqa: E731
    return {"out": sorted(out, key=key), "in": sorted(received, key=key)}


def withdrawn(row):
    """True when a still-undelivered request row belongs to a revision that was
    superseded or cancelled: it must not be sent. Delivered rows are never undone."""
    if not isinstance(row, dict) or row.get("state") != "pending":
        return False
    for ref in row.get("refs") or ():
        parsed = parse_ref(ref)
        if parsed is None or parsed["role"] not in ("request", "followup"):
            continue
        record = load(parsed["id"])
        revision = revision_of(record, parsed["revision"]) if record else None
        if revision is not None and revision.get("state") in ("superseded", "cancelled"):
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
    """Remove records closed (or never sealed) for more than the retention period.
    An open request is never removed for its age. Returns the removed ids."""
    now = time.time() if now is None else now
    try:
        directory = requests_dir()
        names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
    except (OSError, ValueError):
        return []
    removed = []
    for name in names[:examine]:
        request_id = name[:-5]
        record = load_any(request_id)
        if record is None:
            continue
        stamp = record.get("updated") or record.get("created") or now
        if record["state"] in _OPEN_STATES or now - stamp < RETENTION_SECONDS:
            continue
        with contextlib.suppress(OSError, ValueError):
            with request_lock(request_id):
                current = load_any(request_id)
                if (current is None or current["state"] in _OPEN_STATES
                        or now - (current.get("updated") or stamp) < RETENTION_SECONDS):
                    continue
                if current["state"] != "preparing":
                    _index_record(current, add=False)
                _record_path(request_id).unlink()
                removed.append(request_id)
    return removed


def load_any(request_id):
    """Like :func:`load` but includes `preparing` records (retention reads this)."""
    try:
        record = _read_json(_record_path(request_id))
    except (OSError, ValueError):
        return None
    return record if _valid_record(record, request_id) else None


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
        lines.append("revision=%d state=%s label=%s delivery=%s" % (
            revision["n"], revision["state"], revision.get("label", ""),
            delivery_word(rows.get(revision["transfer_ref"])) or "-"))
    for report in record["reports"]:
        lines.append("report=%d kind=%s revision=%d late=%s elapsed=%s result=%s remaining=%s" % (
            report["n"], report["kind"], report["revision"], str(report["late"]).lower(),
            elapsed_text(report.get("elapsed") or 0), report.get("result", ""), report.get("remaining", "")))
    if record.get("cancel"):
        lines.append("cancelled reason=%s" % record["cancel"].get("reason", ""))
    lines.append("updated=%s ago" % elapsed_text(now - record.get("updated", now)))
    return lines
