"""Retire expired per-attempt worker homes without a user step (issue #564).

A home under ``<dispatch-state-root>/homes/workers/<32-hex>`` belongs to exactly
one attempt. Once that attempt is terminal, quiescent and has been unused for
`IDLE_SECONDS`, the home is detached with one rename and removed outside every
lock. Anything unproven stays: the policy never guesses, and every uncertain
case is counted in ``homes/worker-retention/state.json`` instead of hidden.

Two existing carriers call `sweep`: the post-exit reaper tail and a non-exact
``reconcile --apply``. Fleet calls `note_view` before it reads a displayed
attempt's home. There is no command, flag, daemon or gate here.

State lives under ``<state-root>/homes/worker-retention/``:
  lock              admission (non-blocking); a busy pass is skipped
  view.lock         shared by Fleet markers, exclusive for the final detach
  state.json        clocks and the last result, replaced atomically
  views/<key>       Fleet view markers (mtime is the last view)
  retiring/<name>   detached homes awaiting removal, with a ``.journal`` file
                    that binds each leaf to the identity it was detached with

Directories are opened once and pinned by descriptor; every later lookup,
rename and removal is relative to a pinned descriptor, so a substituted path
component cannot redirect them, and no symlink is ever followed.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

IDLE_SECONDS = 7 * 86400
DUE_SECONDS = 300
EXAMINE_LIMIT = 32
REMOVE_LIMIT = 4
EXAMINE_BUDGET_SECONDS = 10
REMOVE_DEADLINE_SECONDS = 20
VIEW_REFRESH_SECONDS = 3600

MAX_REGISTRY_BYTES = 512 * 1024 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_DELIVERY_BYTES = 256 * 1024
MAX_DELIVERY_RECORDS = 4096
MAX_DEPENDENCY_STAMPS = MAX_DELIVERY_RECORDS * 8
MAX_TREE_DEPTH = 128
MAX_INTENT_CHILDREN = 256

KEY_RE = re.compile(r"[0-9a-f]{32}")
ATTEMPT_RE = re.compile(r"att-[A-Za-z0-9._-]{1,240}")
NOTICE_RE = re.compile(r"notice-[0-9a-f]{32}")
ATTEMPT_ID_RE = re.compile(r"(?:^|,)attempt_id=([^,]+)")
LEAF_RE = re.compile(r"([0-9a-f]{32})\.([0-9]{1,20})")
DELIVERY_ID_RE = re.compile(r"delivery-(?:[0-9a-f]{32}|[0-9a-f]{64})")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TERMINAL = frozenset({"done", "killed", "cancelled"})
OPEN = frozenset({"open", "running"})
ROUTE_KEYS = ("route_file", "route_id", "route_hash", "owner_route_file",
              "owner_route_id", "owner_route_hash", "batch_route_id")
HARNESSES = frozenset({"claude", "codex", "opencode"})
WORKER_TYPES = frozenset({"owner", "stage", "review", "support", "frame"})
SCHEMA = 1

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_DEPENDENCIES = contextvars.ContextVar("retention_dependencies", default=None)


def _stamp(path):
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _track(path):
    watched = _DEPENDENCIES.get()
    if watched is not None:
        path = Path(path)
        _track_resolver_parent(path.parent)
        before = watched.get(path)
        if path not in watched:
            watched[path] = _stamp(path)
        elif isinstance(before, tuple) and before[:1] == ("resolver",):
            current = _stamp(path)
            identity = (current[:4] if current is not None
                        and stat.S_ISDIR(current[2]) else current)
            if identity != before[1]:
                raise _Unreadable("late-evidence-changed")
            watched[path] = current
        if len(watched) > MAX_DEPENDENCY_STAMPS:
            raise _Unreadable("evidence-overflow")


def _resolver_stamp(path):
    """A directory's identity matters to realpath; unrelated children do not."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return (st.st_dev, st.st_ino, st.st_mode, st.st_uid)
    return _stamp(path)


def _track_resolver_parent(parent):
    """Watch the components realpath consumes, including an aliased ancestor."""
    watched = _DEPENDENCIES.get()
    if watched is None:
        return
    path = Path(parent)
    for component in reversed((path, *path.parents)):
        before = watched.get(component)
        if component not in watched:
            watched[component] = ("resolver", _resolver_stamp(component))
        elif isinstance(before, tuple) and before[:1] == ("resolver",):
            if _resolver_stamp(component) != before[1]:
                raise _Unreadable("late-evidence-changed")
    if len(watched) > MAX_DEPENDENCY_STAMPS:
        raise _Unreadable("evidence-overflow")


def _route_observer(kind, path):
    """Watch only authority the shared route resolver actually consults."""
    path = Path(path)
    _track_resolver_parent(path.parent)
    _track(path)


def _fresh(watched):
    return all((_resolver_stamp(path) == before[1]
                if isinstance(before, tuple) and before[:1] == ("resolver",)
                else _stamp(path) == before)
               for path, before in watched.items())


class _Unsafe(Exception):
    """A containment or identity proof failed; nothing is touched."""


class _Unreadable(Exception):
    """Evidence exists but cannot be read as the typed record it must be."""


class _Verdict:
    """One home's outcome: ``keep``/``unknown`` carry a reason, ``eligible`` a clock."""

    __slots__ = ("kind", "reason", "activity_ns", "attempt", "raw")

    def __init__(self, kind, reason="", activity_ns=0, attempt="", raw=""):
        self.kind, self.reason, self.activity_ns = kind, reason, activity_ns
        self.attempt, self.raw = attempt, raw


# --- containment ---------------------------------------------------------


def _state_root(jobs) -> Path:
    from dispatch_contract import dispatch_state_root
    return dispatch_state_root(jobs)


def _open_dir(name, dir_fd=None, *, create=False) -> int:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        if not create or dir_fd is None:
            raise _Unsafe(str(name))
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _Unsafe(str(name)) from exc
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            raise _Unsafe(str(name)) from exc
    except OSError as exc:
        raise _Unsafe(str(name)) from exc
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise _Unsafe(f"{name}: foreign owner")
    except BaseException:
        os.close(fd)
        raise
    return fd


class _Layout:
    """The pinned directory chain of one registry's worker homes."""

    def __init__(self, root: Path, *, create: bool = True):
        self._fds: list[int] = []
        try:
            self.root_fd = self._pin(str(root), None)
            self.homes_fd = self._pin("homes", self.root_fd)
            self.workers_fd = self._pin("workers", self.homes_fd)
            self.ret_fd = self._pin("worker-retention", self.homes_fd, create=create)
            self.retiring_fd = self._pin("retiring", self.ret_fd, create=create)
            self.views_fd = self._pin("views", self.ret_fd, create=create)
        except BaseException:
            self.close()
            raise

    def _pin(self, name, dir_fd, *, create=False) -> int:
        fd = _open_dir(name, dir_fd, create=create)
        self._fds.append(fd)
        return fd

    def close(self) -> None:
        while self._fds:
            with contextlib.suppress(OSError):
                os.close(self._fds.pop())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@contextlib.contextmanager
def _flock(dir_fd, name, mode):
    """flock a regular file under a pinned directory; yields False if unavailable."""
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Unsafe(name)
        try:
            fcntl.flock(fd, mode)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_regular(path, limit, *, dir_fd=None):
    """(bytes, mtime_ns) of a regular file read without following it; None if absent."""
    if dir_fd is None:
        _track(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unreadable(str(path)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise _Unreadable(str(path))
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise _Unreadable(str(path))
            chunks.append(chunk)
        return b"".join(chunks), st.st_mtime_ns
    except OSError as exc:
        raise _Unreadable(str(path)) from exc
    finally:
        os.close(fd)


def _read_json(path, limit=MAX_EVIDENCE_BYTES, *, dir_fd=None):
    """(object, mtime_ns), or None if absent; `_Unreadable` if not a JSON object."""
    found = _read_regular(path, limit, dir_fd=dir_fd)
    if found is None:
        return None
    try:
        value = json.loads(found[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _Unreadable(str(path)) from exc
    if not isinstance(value, dict):
        raise _Unreadable(str(path))
    return value, found[1]


# --- registry snapshot ---------------------------------------------------


class _Row:
    __slots__ = ("raw", "fields", "status", "_meta")

    def __init__(self, raw, fields):
        self.raw, self.fields, self.status, self._meta = raw, fields, fields[1], None

    @property
    def meta(self):
        if self._meta is None:
            from dispatch_contract import parse_registry_metadata
            self._meta = parse_registry_metadata(self.fields[5])
        return self._meta


def _ended_ns(row: _Row) -> int | None:
    """Return the best wall-clock completion estimate carried by a registry row."""
    try:
        ended = datetime.fromisoformat(row.fields[0].replace("Z", "+00:00"))
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        ended = ended.astimezone(timezone.utc)
        delta = ended - datetime(1970, 1, 1, tzinfo=timezone.utc)
        registry_ns = ((delta.days * 86400 + delta.seconds) * 10**9
                       + delta.microseconds * 1000)
    except (AttributeError, IndexError, OverflowError, TypeError, ValueError):
        return None

    log_file = row.meta.get("log_file", "")
    if log_file and os.path.isabs(log_file):
        try:
            _track(log_file)
            st = os.stat(log_file, follow_symlinks=False)
        except (OSError, ValueError):
            pass
        else:
            if stat.S_ISREG(st.st_mode):
                registry_ns = max(registry_ns, st.st_mtime_ns)
    return registry_ns


class _Snapshot:
    """One read of ``jobs.log``: the latest row per attempt and open references."""

    def __init__(self, jobs: Path):
        self.jobs = jobs
        _track(jobs)
        self.rows: dict[str, _Row] = {}
        self.open_refs: dict[str, set[str]] = {}
        # parent attempt -> children whose completion delivery was intended
        self.intents: dict[str, set[str]] = {}
        try:
            size = jobs.stat().st_size
        except OSError as exc:
            raise _Unreadable("registry") from exc
        if size > MAX_REGISTRY_BYTES:
            raise _Unreadable("registry-oversized")
        try:
            handle = open(jobs, "rb")
        except OSError as exc:
            raise _Unreadable("registry") from exc
        with handle:
            for blob in handle:
                line = blob.decode("utf-8", "replace").rstrip("\n")
                fields = line.split("\t")
                if not blob.endswith(b"\n"):
                    # A torn latest append may be a newer state for any attempt.
                    raise _Unreadable("registry-malformed")
                if len(fields) != 6:
                    continue
                found = ATTEMPT_ID_RE.search(fields[5])
                if found:
                    self.rows[found.group(1)] = _Row(line, fields)
        for attempt, row in self.rows.items():
            if row.status in OPEN:
                for token in set(ATTEMPT_RE.findall(row.fields[5])):
                    if token != attempt:
                        self.open_refs.setdefault(token, set()).add(attempt)
            elif "delivery_intent=1" in row.fields[5]:
                parent = row.meta.get("parent_attempt_id", "")
                if row.meta.get("delivery_intent") == "1" and ATTEMPT_RE.fullmatch(parent):
                    self.intents.setdefault(parent, set()).add(attempt)


def _declared_home(root: Path, workers_real: str, value: str, parents: dict | None = None):
    """The key a declared ``runtime_home`` names, `None` if it names a different place.

    Only the *parent* of the declared path is resolved. The leaf is compared as a
    name, so a link inside or at the home is never followed to establish identity.
    """
    if not value or not os.path.isabs(value):
        return None
    norm = os.path.normpath(value)
    parent, name = os.path.split(norm)
    if not KEY_RE.fullmatch(name):
        return None
    try:
        _track_resolver_parent(parent)
        resolved = parents.get(parent) if parents is not None else None
        if resolved is None:
            resolved = os.path.realpath(parent)
            if parents is not None:
                parents[parent] = resolved
        if resolved != workers_real:
            return None
    except OSError:
        return None
    return name


# --- evidence ------------------------------------------------------------


def _flock_probe(path: str) -> str:
    """held | free | missing | unknown for a declared lease file; never takes it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "unknown"
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)


def _lease_state(jobs: Path, meta: dict) -> str:
    """none | free | held | unknown over the supervisor and review worker leases."""
    from dispatch_contract import (REVIEW_GOVERNED_LEASE_KIND, DispatchContractError,
                                   review_governed_lease_path, supervisor_lease_path)
    states = []
    declared = meta.get("supervisor_lease_file", "")
    if declared or meta.get("supervisor_lease"):
        try:
            canonical = supervisor_lease_path(jobs, meta.get("attempt_id", ""))
        except DispatchContractError:
            return "unknown"
        if not declared or str(canonical) != declared:
            return "unknown"
        states.append(_flock_probe(str(canonical)))
    if meta.get("review_governed_lease") == REVIEW_GOVERNED_LEASE_KIND:
        try:
            path = review_governed_lease_path(
                meta.get("artifact_root", ""), meta.get("review_cycle_id", ""),
                meta.get("attempt_id", ""))
        except DispatchContractError:
            return "unknown"
        states.append(_flock_probe(str(path)))
    if "held" in states:
        return "held"
    if "unknown" in states:
        return "unknown"
    return "free" if states else "none"


def _delivery_binding(value: dict, digest: str, delivery: str, attempt: str, parent: str) -> bool:
    """A terminal word is useful only in its typed, exact obligation."""
    from dispatch_pending_delivery import PendingDeliveryError, _validate_record
    try:
        _validate_record(value)
    except (PendingDeliveryError, TypeError, ValueError):
        return False
    ids = value.get("attempt_ids")
    return (value.get("delivery_id") == delivery
            and value.get("recipient_digest") == digest
            and isinstance(ids, list) and attempt in ids
            and all(isinstance(item, str) and (ATTEMPT_RE.fullmatch(item) or NOTICE_RE.fullmatch(item))
                    for item in ids)
            and value.get("parent_attempt_id") == parent)


def _pruned_delivery_binding(value: dict, digest: str, delivery: str,
                             attempt: str, parent: str) -> bool:
    from dispatch_pending_delivery import PRUNE_TOMBSTONE_SCHEMA, TERMINAL_STATES
    ids = value.get("attempt_ids")
    return (set(value) == {"schema", "delivery_id", "recipient_digest", "attempt_ids",
                           "parent_attempt_id", "terminal_state"}
            and value.get("schema") == PRUNE_TOMBSTONE_SCHEMA
            and value.get("delivery_id") == delivery
            and value.get("recipient_digest") == digest
            and isinstance(ids, list) and attempt in ids
            and all(isinstance(item, str) and (ATTEMPT_RE.fullmatch(item) or NOTICE_RE.fullmatch(item))
                    for item in ids)
            and value.get("parent_attempt_id") == parent
            and isinstance(value.get("terminal_state"), str)
            and value.get("terminal_state") in TERMINAL_STATES)


def _delivery_state(root: Path, meta: dict):
    """(state, activity_ns): none | resolved | open | pending | unknown."""
    if meta.get("delivery_intent") != "1":
        return "none", 0
    digest, delivery = meta.get("delivery_recipient_digest", ""), meta.get("delivery_id", "")
    if not SHA256_RE.fullmatch(digest) or not DELIVERY_ID_RE.fullmatch(delivery):
        return "unknown", 0
    from dispatch_completion_join import JoinContractError, pending_record_identity
    try:
        # Only the parent slot binds this home. Route lifecycle resolution can
        # parse the whole registry; route protection is checked separately.
        _route, _node, parent = pending_record_identity(meta)
    except (JoinContractError, OSError, ValueError):
        return "unknown", 0
    record = root / "pending-delivery" / digest / f"{delivery}.json"
    try:
        found = _read_json(str(record), MAX_DELIVERY_BYTES)
        if found is None:
            tomb = _read_json(str(record) + ".pruned", MAX_DELIVERY_BYTES)
            if tomb is None:
                return "pending", 0
            return (("resolved", tomb[1]) if _pruned_delivery_binding(
                tomb[0], digest, delivery, meta.get("attempt_id", ""),
                parent) else ("unknown", 0))
    except _Unreadable:
        return "unknown", 0
    value, mtime = found
    from dispatch_pending_delivery import OPEN_STATES, TERMINAL_STATES
    if not _delivery_binding(value, digest, delivery, meta.get("attempt_id", ""),
                             parent):
        return "unknown", 0
    state = value.get("state")
    if state in OPEN_STATES:
        return "open", mtime
    if state in TERMINAL_STATES:
        return "resolved", mtime
    return "unknown", 0


def _recipient_group(root: Path, meta: dict):
    parent = meta.get("parent_sid", "")
    if not isinstance(parent, str) or not parent:
        return None
    from dispatch_pending_delivery import recipient_digest
    return root / "pending-delivery" / recipient_digest(parent)


def _candidate_recipient_groups(root: Path, snapshot: _Snapshot, attempt: str,
                                discovered: dict | None = None):
    """Producer-bound delivery groups, including absent groups and terminal owners.

    A group discovery already scanned keeps its discovery-time stamp, so a
    record published into it after discovery invalidates this detach.
    """
    related = {attempt, *snapshot.intents.get(attempt, ())}
    # Replacement successor obligations use the same recipient identities as
    # the exact delivery reads in _replacement_state.
    index = _read_json(str(root / "automatic-replacements" / "by-source" /
                           f"{attempt}.json"), 64 * 1024)
    if index is not None:
        family = index[0].get("family_id")
        if isinstance(family, str) and SHA256_RE.fullmatch(family):
            claim = _read_json(str(root / "automatic-replacements" / "claims" /
                                    f"{family}.json"))
            if claim is not None:
                successor = claim[0].get("replacement_attempt_id")
                if isinstance(successor, str) and successor in snapshot.rows:
                    related.add(successor)
                    related.update(snapshot.intents.get(successor, ()))
    watched = _DEPENDENCIES.get()
    for identity in related:
        row = snapshot.rows.get(identity)
        if row is not None:
            group = _recipient_group(root, row.meta)
            if group is not None:
                if watched is not None and discovered and group in discovered:
                    watched.setdefault(group, discovered[group])
                _track(group)


def _delivery_index(root: Path, *, observer=None, activity=None):
    """(attempt ids in open records, unknown evidence).

    `activity`, when given, receives the newest mtime of resolved records per
    attempt they name.
    """
    from dispatch_pending_delivery import OPEN_STATES, PendingDeliveryError, _validate_record
    refs: set[str] = set()
    seen = 0
    base = root / "pending-delivery"
    if observer is not None:
        observer("members", base)
    try:
        groups = list(os.scandir(base))
    except FileNotFoundError:
        return refs, False
    except OSError:
        return refs, True
    for group in groups:
        try:
            if group.is_symlink() or not group.is_dir(follow_symlinks=False):
                return refs, True
            if observer is not None:
                # Before the scan: a record published into this group later
                # must still change the stamp a candidate's detach compares.
                observer("group", Path(group.path))
            entries = list(os.scandir(group.path))
        except OSError:
            return refs, True
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            if observer is not None:
                observer("file", Path(entry.path))
            seen += 1
            if seen > MAX_DELIVERY_RECORDS:
                return refs, True
            try:
                found = _read_json(entry.path, MAX_DELIVERY_BYTES)
            except _Unreadable:
                return refs, True
            if found is None:
                continue
            value, mtime = found
            try:
                _validate_record(value)
            except (PendingDeliveryError, TypeError, ValueError):
                return refs, True
            if (not SHA256_RE.fullmatch(group.name)
                    or not DELIVERY_ID_RE.fullmatch(entry.name[:-5])
                    or value.get("recipient_digest") != group.name
                    or value.get("delivery_id") != entry.name[:-5]):
                return refs, True
            ids = value.get("attempt_ids")
            parent = value.get("parent_attempt_id")
            if (not isinstance(ids, list) or not all(
                    isinstance(item, str) and (ATTEMPT_RE.fullmatch(item) or NOTICE_RE.fullmatch(item))
                    for item in ids)
                    or not isinstance(parent, str) or not parent):
                return refs, True
            named = [item for item in (*ids, parent) if ATTEMPT_RE.fullmatch(item)]
            if value.get("state") in OPEN_STATES:
                refs.update(named)
            elif activity is not None:
                for item in named:
                    activity[item] = max(activity.get(item, 0), mtime)
    return refs, False


def _replacement_state(root: Path, snapshot: _Snapshot, attempt: str):
    """(state, activity_ns): none | resolved | unresolved | unknown.

    A by-source reservation is written first and the claim record, the successor
    row and the source annotation follow, so a reservation alone is unresolved.
    """
    base = root / "automatic-replacements"
    activity = 0
    try:
        index = _read_json(str(base / "by-source" / f"{attempt}.json"), 64 * 1024)
    except _Unreadable:
        return "unknown", 0
    if index is not None:
        value, mtime = index
        family = value.get("family_id")
        if (value.get("original_attempt_id") != attempt or not isinstance(family, str)
                or not SHA256_RE.fullmatch(family)):
            return "unknown", 0
        activity = mtime
        try:
            claim = _read_json(str(base / "claims" / f"{family}.json"))
        except _Unreadable:
            return "unknown", 0
        if claim is None:
            return "unresolved", activity
        record, mtime = claim
        activity = max(activity, mtime)
        successor = record.get("replacement_attempt_id")
        if (record.get("original_attempt_id") != attempt or not isinstance(successor, str)
                or not ATTEMPT_RE.fullmatch(successor)):
            return "unknown", 0
        row = snapshot.rows.get(successor)
        if row is None or row.status not in TERMINAL:
            return "unresolved", activity
        successor_route, route_mtime = _route_state(snapshot.jobs, row.meta)
        successor_delivery, delivery_mtime = _delivery_state(root, row.meta)
        if successor_route in ("open", "unknown") or successor_delivery in ("open", "pending", "unknown"):
            return "unresolved", activity
        activity = max(activity, route_mtime, delivery_mtime)
        children = snapshot.intents.get(successor, ())
        if len(children) > MAX_INTENT_CHILDREN:
            return "unknown", 0
        for child in children:
            child_state, child_mtime = _delivery_state(root, snapshot.rows[child].meta)
            if child_state in ("open", "pending", "unknown"):
                return "unresolved", activity
            activity = max(activity, child_mtime)
    try:
        attention = _read_json(str(root / "recovery-attention" / "by-source" / f"{attempt}.json"),
                               64 * 1024)
    except _Unreadable:
        return "unknown", 0
    if attention is not None:
        value, mtime = attention
        recovery = value.get("recovery_id")
        if value.get("original_attempt_id") != attempt or not isinstance(recovery, str) or not recovery:
            return "unknown", 0
        try:
            record = _read_json(str(root / "recovery-attention" /
                                    (hashlib.sha256(recovery.encode()).hexdigest() + ".json")), 64 * 1024)
        except _Unreadable:
            return "unknown", 0
        if record is None:
            return "unresolved", max(activity, mtime)
        detail, detail_mtime = record
        if (detail.get("schema") != "recovery-attention-v1"
                or detail.get("original_attempt_id") != attempt
                or detail.get("recovery_id") != recovery
                or detail.get("state") != "needs-attention"):
            return "unknown", 0
        return "unresolved", max(activity, mtime, detail_mtime)
    return ("resolved" if activity else "none"), activity


def _route_state(jobs: Path, meta: dict):
    """(state, activity_ns): none | closed | open | unknown."""
    if not any(key in meta for key in ROUTE_KEYS):
        return "none", 0
    if not any(meta.get(key) for key in ROUTE_KEYS):
        return "unknown", 0
    for name in ("route_file", "owner_route_file"):
        if meta.get(name):
            _track(meta[name])
    from dispatch_notice_state import bound_route, closed_outcome
    try:
        bound = bound_route(meta, jobs, observer=_route_observer)
        if bound is None:
            return "unknown", 0
        path, route = bound
        _track(path)
        _track(path.with_name(path.stem + ".outcome.json"))
        artifact_root, route_id = route.get("artifact_root"), route.get("route_id")
        if (isinstance(artifact_root, str) and os.path.isabs(artifact_root)
                and isinstance(route_id, str) and route_id
                and route_id not in (".", "..") and "/" not in route_id):
            _track(Path(artifact_root) / ".runtime" / "inline-finish" / "v1" /
                   route_id / "finish.json")
        outcome = closed_outcome(path, route)
    except FileNotFoundError:
        return "unknown", 0
    except Exception:  # noqa: BLE001 -- unreadable/conflicting evidence is unknown
        return "unknown", 0
    if outcome is None:
        return "unknown", 0
    if outcome.get("finish_pending"):
        return "open", 0
    try:
        mtime = os.stat(path.with_name(path.stem + ".outcome.json")).st_mtime_ns
    except OSError:
        mtime = 0
    return "closed", mtime


def _stat_mtime(path, **kwargs) -> int:
    try:
        return os.stat(path, **kwargs).st_mtime_ns
    except OSError:
        return 0


class _Context:
    """Everything one examination reads that is shared by every home."""

    def __init__(self, jobs: Path, root: Path, layout: _Layout, snapshot: _Snapshot, now_ns: int):
        self.jobs, self.root, self.layout, self.snapshot, self.now_ns = jobs, root, layout, snapshot, now_ns
        self.workers_real = os.path.realpath(root / "homes" / "workers")
        self.delivery = None
        self.discovery_stamps = {}
        # Every group's discovery-time stamp; a detach seeds only its own groups.
        self.group_stamps = {}
        self.delivery_activity: dict[str, int] = {}
        self.by_key: dict[str, set[str]] = {}
        self.legacy: dict[tuple, dict[str, set[str]]] = {}

    def delivery_index(self):
        if self.delivery is None:
            def observe(kind, path):
                table = self.group_stamps if kind == "group" else self.discovery_stamps
                table[path] = _stamp(path)
            self.delivery = _delivery_index(self.root, observer=observe,
                                            activity=self.delivery_activity)
        return self.delivery

    def legacy_matches(self, source: str, harness: str, worker_type: str) -> dict[str, set[str]]:
        table = self.legacy.get((source, harness, worker_type))
        if table is None:
            from worker_runtime_home import _builder
            table = {}
            for attempt in self.snapshot.rows:
                key = _builder.worker_home_key(source, harness, worker_type, attempt)
                table.setdefault(key, set()).add(attempt)
            self.legacy[(source, harness, worker_type)] = table
        return table


def _split_credential(home_fd: int) -> bool:
    """A Claude refresh replaced the credential link; the file may be the only token copy."""
    try:
        st = os.stat(".credentials.json", dir_fd=home_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return not stat.S_ISLNK(st.st_mode)


def _bind(ctx: _Context, key: str, home_fd: int):
    """(attempt_id, manifest_identity_or_None) or a keep/unknown `_Verdict`."""
    if _split_credential(home_fd):
        return _Verdict("keep", "split-credential")
    try:
        found = _read_json("worker-home.json", MAX_MANIFEST_BYTES, dir_fd=home_fd)
    except _Unreadable:
        return _Verdict("unknown", "manifest-unreadable")
    if found is None:
        return _Verdict("unknown", "manifest-missing")
    manifest = found[0]
    harness, worker_type, source = manifest.get("harness"), manifest.get("worker_type"), manifest.get("source")
    if harness not in HARNESSES or worker_type not in WORKER_TYPES or not isinstance(source, str) or not source:
        return _Verdict("unknown", "manifest-invalid")
    if manifest.get("destination") is True:
        return _Verdict("unknown", "destination-home")
    if manifest.get("schema") == 2 and manifest.get("key") != key:
        return _Verdict("unknown", "manifest-key-conflict")
    identity = manifest.get("identity") if manifest.get("schema") == 2 else None
    if identity is not None and (not isinstance(identity, str) or not ATTEMPT_RE.fullmatch(identity)):
        return _Verdict("unknown", "manifest-identity-invalid")
    from worker_runtime_home import _builder

    declared = ctx.by_key.get(key, set())
    if len(declared) > 1:
        return _Verdict("unknown", "runtime-home-ambiguous")
    if declared:
        attempt = next(iter(declared))
        if identity is not None and identity != attempt:
            return _Verdict("unknown", "identity-conflict")
    elif identity is not None:
        attempt = identity
    else:
        matches = ctx.legacy_matches(source, harness, worker_type).get(key, set())
        if len(matches) != 1:
            return _Verdict("unknown", "unmapped" if not matches else "ambiguous")
        attempt = next(iter(matches))
    if not ATTEMPT_RE.fullmatch(attempt):
        return _Verdict("unknown", "attempt-invalid")
    if _builder.worker_home_key(source, harness, worker_type, attempt) != key:
        return _Verdict("unknown", "key-mismatch")
    row = ctx.snapshot.rows.get(attempt)
    if row is None:
        return _Verdict("unknown", "no-row")
    claimed = row.meta.get("runtime_home", "")
    if claimed and _declared_home(ctx.root, ctx.workers_real, claimed) != key:
        return _Verdict("unknown", "runtime-home-conflict")
    return attempt, identity


def _declared_index(ctx_snapshot: _Snapshot, root: Path, workers_real: str) -> dict[str, set[str]]:
    by_key: dict[str, set[str]] = {}
    parents: dict[str, str] = {}
    for attempt, row in ctx_snapshot.rows.items():
        claimed = row.fields[5]
        if "runtime_home=" not in claimed:
            continue
        key = _declared_home(root, workers_real, row.meta.get("runtime_home", ""), parents)
        if key:
            by_key.setdefault(key, set()).add(attempt)
    return by_key


def _protections(ctx: _Context, key: str, attempt: str, *, probe_processes: bool):
    """Read every protection fresh. `eligible` carries the newest activity time."""
    row = ctx.snapshot.rows[attempt]
    meta = row.meta
    if row.status not in TERMINAL:
        return _Verdict("keep", "not-terminal")
    if attempt in ctx.snapshot.open_refs:
        return _Verdict("keep", "open-reference")
    activity = 0
    state, mtime = _delivery_state(ctx.root, meta)
    if state == "open":
        return _Verdict("keep", "delivery-open")
    if state == "pending":
        return _Verdict("keep", "delivery-pending")
    if state == "unknown":
        return _Verdict("unknown", "delivery-unknown")
    activity = max(activity, mtime)
    # A child's undelivered completion is addressed through this attempt.
    children = ctx.snapshot.intents.get(attempt, ())
    if len(children) > MAX_INTENT_CHILDREN:
        return _Verdict("unknown", "delivery-unknown")
    for child in children:
        state, mtime = _delivery_state(ctx.root, ctx.snapshot.rows[child].meta)
        if state in ("open", "pending"):
            return _Verdict("keep", "delivery-" + state)
        if state == "unknown":
            return _Verdict("unknown", "delivery-unknown")
        activity = max(activity, mtime)
    state, mtime = _replacement_state(ctx.root, ctx.snapshot, attempt)
    if state == "unresolved":
        return _Verdict("keep", "replacement-unresolved")
    if state == "unknown":
        return _Verdict("unknown", "replacement-unknown")
    activity = max(activity, mtime)
    state, mtime = _route_state(ctx.jobs, meta)
    if state == "open":
        return _Verdict("keep", "route-open")
    if state == "unknown":
        return _Verdict("unknown", "route-unknown")
    activity = max(activity, mtime)
    from dispatch_summary import owner_process_state
    owner = owner_process_state(meta)
    if owner == "live":
        return _Verdict("keep", "summary-live")
    if owner == "unknown":
        return _Verdict("unknown", "summary-owner-unknown")
    lease = _lease_state(ctx.jobs, meta)
    if lease == "held":
        return _Verdict("keep", "lease-held")
    if lease == "unknown":
        return _Verdict("unknown", "lease-unknown")
    refs, index_unknown = ctx.delivery_index()
    if index_unknown:
        return _Verdict("unknown", "delivery-index-unknown")
    if attempt in refs:
        return _Verdict("keep", "delivery-open")
    activity = max(activity, ctx.delivery_activity.get(attempt, 0))
    activity = max(activity, _stat_mtime("supervisor-state/%s.json" % attempt,
                                         dir_fd=ctx.layout.root_fd, follow_symlinks=False))
    if probe_processes:
        from dispatch_contract import attempt_process_quiescence
        try:
            quiescence = attempt_process_quiescence(meta, terminal_receipt=True)
        except Exception:  # noqa: BLE001 -- an unobservable process is not quiet
            return _Verdict("unknown", "quiescence-unverifiable")
        if quiescence.state == "live":
            return _Verdict("keep", "not-quiescent")
        if quiescence.state != "quiescent":
            return _Verdict("unknown", "quiescence-unverifiable")
    return _Verdict("eligible", activity_ns=activity, attempt=attempt, raw=row.raw)


# --- state ---------------------------------------------------------------


def _load_state(layout: _Layout) -> dict:
    try:
        found = _read_json("state.json", MAX_STATE_BYTES, dir_fd=layout.ret_fd)
    except _Unreadable:
        return {"schema": SCHEMA, "eligible_since": {}, "cursor": "", "last_pass_ns": 0}
    if found is None or found[0].get("schema") != SCHEMA:
        return {"schema": SCHEMA, "eligible_since": {}, "cursor": "", "last_pass_ns": 0}
    value = found[0]
    since = value.get("eligible_since")
    value["eligible_since"] = {
        key: stamp for key, stamp in (since.items() if isinstance(since, dict) else ())
        if isinstance(key, str) and KEY_RE.fullmatch(key) and isinstance(stamp, int)}
    if not isinstance(value.get("cursor"), str):
        value["cursor"] = ""
    if not isinstance(value.get("last_pass_ns"), int):
        value["last_pass_ns"] = 0
    return value


# destructive-ok: reason=replace the retention state record; boundary=the pinned homes/worker-retention directory
def _store_state(layout: _Layout, state: dict) -> None:
    tmp = f".state.{os.getpid()}.{time.time_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=layout.ret_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(state, sort_keys=True, separators=(",", ":")).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, "state.json", src_dir_fd=layout.ret_fd, dst_dir_fd=layout.ret_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=layout.ret_fd)
        raise


# --- removal -------------------------------------------------------------


def _symlink_safe_removal_supported() -> bool:
    return (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd and os.rmdir in os.supports_dir_fd
            and os.rename in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
            and os.scandir in os.supports_fd and os.stat in os.supports_follow_symlinks)


def _identity(st) -> list[int]:
    return [st.st_dev, st.st_ino, st.st_uid]


def _mount_id(fd: int) -> str | None:
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
            for line in info:
                if line.startswith("mnt_id:\t"):
                    value = line.split(":", 1)[1].strip()
                    return value if value.isdecimal() else None
    except OSError:
        pass
    return None


# destructive-ok: reason=empty one detached expired worker home by descriptor; boundary=one validated leaf directly under homes/worker-retention/retiring
def _empty_directory(dir_fd: int, device: int, deadline: float, depth: int = 0) -> bool:
    """Remove every entry below `dir_fd`; links are unlinked, never followed.

    Returns False when the deadline stopped the walk (the leaf is resumed by a
    later pass). A different device or a changed entry raises `_Unsafe`.
    """
    if depth > MAX_TREE_DEPTH:
        raise _Unsafe("tree-too-deep")
    parent_mount = _mount_id(dir_fd)
    if parent_mount is None:
        raise _Unsafe("mount-unknown")
    with os.scandir(dir_fd) as listing:
        names = [entry.name for entry in listing]
    for name in names:
        if time.monotonic() > deadline:
            return False
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            os.unlink(name, dir_fd=dir_fd)
            continue
        if st.st_dev != device:
            raise _Unsafe(f"{name}: other device")
        child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                raise _Unsafe(f"{name}: replaced")
            if _mount_id(child) != parent_mount:
                raise _Unsafe(f"{name}: other mount")
            if not _empty_directory(child, device, deadline, depth + 1):
                return False
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=dir_fd)
    return True


# destructive-ok: reason=finish removing one detached expired worker home; boundary=one validated leaf directly under homes/worker-retention/retiring
def _remove_leaf(layout: _Layout, name: str, expected: list[int], deadline: float) -> str:
    """removed | partial | unproven for one retiring leaf, by pinned descriptor."""
    try:
        leaf = os.open(name, _DIR_FLAGS, dir_fd=layout.retiring_fd)
    except FileNotFoundError:
        return "removed"
    except OSError:
        return "unproven"
    try:
        opened = os.fstat(leaf)
        if _identity(opened) != expected or opened.st_uid != os.geteuid():
            return "unproven"
        try:
            if _mount_id(leaf) is None or _mount_id(leaf) != _mount_id(layout.retiring_fd):
                raise _Unsafe("retiring mount boundary")
            finished = _empty_directory(leaf, opened.st_dev, deadline)
        except (_Unsafe, OSError):
            return "partial"
    finally:
        os.close(leaf)
    if not finished:
        return "partial"
    try:
        os.rmdir(name, dir_fd=layout.retiring_fd)
    except FileNotFoundError:
        pass
    except OSError:
        return "partial"
    with contextlib.suppress(OSError):
        os.unlink(name + ".journal", dir_fd=layout.retiring_fd)
    return "removed"


def _journal(layout: _Layout, name: str):
    """The identity this leaf was detached with, or None if unproven."""
    try:
        found = _read_json(name + ".journal", 4096, dir_fd=layout.retiring_fd)
    except _Unreadable:
        return None
    if found is None:
        return None
    value = found[0]
    ident, key = value.get("identity"), value.get("key")
    match = LEAF_RE.fullmatch(name)
    if (value.get("name") != name or not match or key != match.group(1) or not isinstance(ident, list)
            or len(ident) != 3 or not all(isinstance(item, int) for item in ident)):
        return None
    return ident


def _resume_leftovers(layout: _Layout, state: dict, budget: int, deadline: float, result: dict) -> int:
    """Spend one slot per touched leaf and rotate around partial leaves."""
    spent = attempts = 0
    try:
        with os.scandir(layout.retiring_fd) as listing:
            names = sorted(entry.name for entry in listing)
    except OSError:
        return 0
    live = set(names)
    cursor = state.get("leftover_cursor", "")
    names = [name for name in names if name > cursor] + [name for name in names if name <= cursor]
    for name in names:
        if name.endswith(".journal"):
            if name[:-len(".journal")] not in live:
                with contextlib.suppress(OSError):
                    # destructive-ok: reason=drop a journal whose leaf is gone; boundary=one journal file directly under homes/worker-retention/retiring
                    os.unlink(name, dir_fd=layout.retiring_fd)
            continue
        if not LEAF_RE.fullmatch(name):
            result["ignored"] += 1
            continue
        ident = _journal(layout, name)
        if ident is None:
            result["unproven_leftover"] += 1
            continue
        if spent >= budget or attempts >= EXAMINE_LIMIT or time.monotonic() > deadline:
            break
        attempts += 1
        spent += 1
        state["leftover_cursor"] = name
        outcome = _remove_leaf(layout, name, ident, deadline)
        if outcome == "removed":
            result["detached_leftover"] += 1
        else:
            result["unproven_leftover" if outcome == "unproven" else "partial"] += 1
    return spent


@contextlib.contextmanager
def _registry_lock(jobs: Path):
    """Probe the writers' lock without delaying a carrier or registry writer."""
    fd = os.open(str(jobs) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Unsafe("registry-lock-invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# destructive-ok: reason=detach one expired worker home by pinned rename; boundary=one 32-hex leaf of homes/workers into homes/worker-retention/retiring
def _detach(ctx: _Context, key: str, ident: list[int], attempt: str, raw: str,
            first_ns: int) -> str:
    """Prepare evidence outside the lock; validate its stamps before rename."""
    layout = ctx.layout
    lock_file = os.open(str(ctx.jobs) + ".lock",
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.close(lock_file)
    ctx.delivery_index()
    watched = dict(ctx.discovery_stamps)
    if len(watched) > MAX_DEPENDENCY_STAMPS:
        return "late-evidence-overflow"
    token = _DEPENDENCIES.set(watched)
    try:
        # Out-of-protocol new files in unrelated historical recipient groups
        # are caught by next-pass discovery, not per-candidate preparation.
        _candidate_recipient_groups(ctx.root, ctx.snapshot, attempt, ctx.group_stamps)
        _track(ctx.root / "supervisor-state" / f"{attempt}.json")
        _track(ctx.root / "homes" / "workers" / key / "worker-home.json")
        try:
            fresh = _Snapshot(ctx.jobs)
        except _Unreadable:
            return "registry-unreadable"
        row = fresh.rows.get(attempt)
        if row is None:
            return "row-changed"
        current = _Context(ctx.jobs, ctx.root, layout, fresh, ctx.now_ns)
        current.by_key = _declared_index(fresh, ctx.root, current.workers_real)
        current.delivery = ctx.delivery
        current.delivery_activity = ctx.delivery_activity
        _candidate_recipient_groups(ctx.root, fresh, attempt, ctx.group_stamps)
        verdict = _protections(current, key, attempt, probe_processes=False)
        if row.raw != raw or verdict.kind != "eligible":
            if verdict.kind == "keep":
                return "protected:" + verdict.reason
            if verdict.kind == "unknown":
                return "defer:" + verdict.reason
            return "activity:row-changed"
        ended = _ended_ns(row)
        if ended is not None:
            first_ns = max(first_ns, ended)
        try:
            home = os.open(key, _DIR_FLAGS, dir_fd=layout.workers_fd)
        except OSError:
            return "home-gone"
        try:
            rebound = _bind(current, key, home)
        finally:
            os.close(home)
        if isinstance(rebound, _Verdict) or rebound[0] != attempt:
            return "late-binding"
    except (OSError, _Unreadable):
        return "late-evidence-unreadable"
    finally:
        _DEPENDENCIES.reset(token)
    if not _fresh(watched):
        return "late-evidence-changed"
    with _registry_lock(ctx.jobs) as admitted:
        if admitted is False:
            return "registry-lock-busy"
        if not _fresh(watched):
            return "late-evidence-changed"
        with _flock(layout.ret_fd, "view.lock", fcntl.LOCK_EX | fcntl.LOCK_NB) as acquired:
            if not acquired:
                return "view-lock-busy"
            # The summary owner was proven dead or absent during preparation. A
            # dead pid/start cannot return and the unchanged row declares no
            # other, so its /proc walk stays out of this lock.
            lease = _lease_state(ctx.jobs, row.meta)
            if lease in ("held", "unknown"):
                return ("protected:" if lease == "held" else "defer:") + "lease-" + lease
            try:
                home = os.open(key, _DIR_FLAGS, dir_fd=layout.workers_fd)
            except OSError:
                return "home-gone"
            try:
                rebound = _bind(current, key, home)
            finally:
                os.close(home)
            if isinstance(rebound, _Verdict) or rebound[0] != attempt:
                return "late-binding"
            mark = _stat_mtime(key, dir_fd=layout.views_fd, follow_symlinks=False)
            if mark and ctx.now_ns - mark < IDLE_SECONDS * 10**9:
                return "recent-view"
            try:
                leaf = os.open(key, _DIR_FLAGS, dir_fd=layout.workers_fd)
            except OSError:
                return "home-gone"
            try:
                st = os.fstat(leaf)
                if _identity(st) != ident:
                    return "identity-changed"
                idle_from = max(first_ns, verdict.activity_ns, mark, st.st_mtime_ns)
                if ctx.now_ns - idle_from < IDLE_SECONDS * 10**9:
                    return "late-activity"
                source_mount = _mount_id(layout.workers_fd)
                target_mount = _mount_id(layout.retiring_fd)
                leaf_mount = _mount_id(leaf)
                if (source_mount is None or source_mount != target_mount
                        or leaf_mount != source_mount):
                    return "mount-unproven"
            finally:
                os.close(leaf)
            name = f"{key}.{ctx.now_ns}"
            journal = json.dumps({"schema": SCHEMA, "key": key, "name": name, "identity": ident},
                                 sort_keys=True).encode()
            try:
                fd = os.open(name + ".journal",
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=layout.retiring_fd)
            except OSError:
                return "journal-failed"
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(journal)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(key, name, src_dir_fd=layout.workers_fd, dst_dir_fd=layout.retiring_fd)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(name + ".journal", dir_fd=layout.retiring_fd)
                return "detach-failed"
            try:
                moved = os.stat(name, dir_fd=layout.retiring_fd, follow_symlinks=False)
            except OSError:
                moved = None
            if moved is None or _identity(moved) != ident:
                # Something else sat at the name when it moved: never delete it.
                with contextlib.suppress(OSError):
                    os.unlink(name + ".journal", dir_fd=layout.retiring_fd)
                return "detach-substituted"
    return "detached:" + name


# --- carriers ------------------------------------------------------------


def _count(table: dict, reason: str) -> None:
    table[reason] = table.get(reason, 0) + 1


def _renewed_protection(ctx: _Context, key: str, attempt: str) -> bool:
    """Only a fresh, positive protection can restart an established clock."""
    try:
        fresh = _Snapshot(ctx.jobs)
        if attempt not in fresh.rows:
            return False
        current = _Context(ctx.jobs, ctx.root, ctx.layout, fresh, ctx.now_ns)
        current.by_key = _declared_index(fresh, ctx.root, current.workers_real)
        current.delivery = ctx.delivery
        return _protections(current, key, attempt, probe_processes=False).kind == "keep"
    except (OSError, _Unreadable):
        return False


def sweep(jobs, *, now_ns: int | None = None, force: bool = False) -> dict:
    """One bounded retention pass. Never raises for an ordinary failure."""
    started = time.monotonic()
    now_ns = time.time_ns() if now_ns is None else now_ns
    try:
        jobs = Path(jobs).resolve()
        return _sweep(jobs, now_ns, force, started)
    except _Unsafe:
        return {"skipped": "containment"}
    except _Unreadable as exc:
        return {"skipped": str(exc) if str(exc) == "registry-oversized" else "registry-unreadable"}
    except OSError as exc:
        return {"skipped": "io-error", "errno": exc.errno}
    except Exception as exc:  # noqa: BLE001 -- a retention bug must not escape a carrier
        return {"skipped": "error", "error": type(exc).__name__}


def sweep_best_effort(jobs) -> None:
    """The carrier entry: whatever happens here never changes the caller's result."""
    try:
        sweep(jobs)
    except Exception:  # noqa: BLE001
        pass


def _sweep(jobs: Path, now_ns: int, force: bool, started: float) -> dict:
    root = _state_root(jobs)
    if not _symlink_safe_removal_supported():
        return {"skipped": "removal-unsupported"}
    with _Layout(root) as layout:
        with _flock(layout.ret_fd, "lock", fcntl.LOCK_EX | fcntl.LOCK_NB) as admitted:
            if not admitted:
                return {"skipped": "busy"}
            state = _load_state(layout)
            if not force and now_ns - state["last_pass_ns"] < DUE_SECONDS * 10**9 and state["last_pass_ns"] <= now_ns:
                return {"skipped": "not-due"}
            return _pass(jobs, root, layout, state, now_ns, started)


def _pass(jobs: Path, root: Path, layout: _Layout, state: dict, now_ns: int, started: float) -> dict:
    result = {"examined": 0, "eligible": 0, "due": 0, "removed": 0, "partial": 0,
              "ignored": 0, "detached_leftover": 0, "unproven_leftover": 0,
              "kept": {}, "unknown": {}}
    deadline = time.monotonic() + REMOVE_DEADLINE_SECONDS
    budget = REMOVE_LIMIT
    # Keep one slot for newly due homes even if every interrupted leaf is stuck.
    budget -= _resume_leftovers(layout, state, budget - 1, deadline, result)

    keys, ignored = [], 0
    with os.scandir(layout.workers_fd) as listing:
        for entry in listing:
            try:
                ok = (KEY_RE.fullmatch(entry.name) is not None
                      and entry.is_dir(follow_symlinks=False)
                      and entry.stat(follow_symlinks=False).st_uid == os.geteuid())
            except OSError:
                ok = False
            if ok:
                keys.append(entry.name)
            else:
                ignored += 1
    result["ignored"] += ignored
    keys.sort()
    present = set(keys)
    after = [key for key in keys if key > state["cursor"]]
    batch = (after + [key for key in keys if key <= state["cursor"]])[:EXAMINE_LIMIT]

    since = {key: stamp for key, stamp in state["eligible_since"].items() if key in present}
    cursor = state["cursor"]
    due: list[tuple[str, list[int], _Verdict, int]] = []
    if batch:
        try:
            snapshot = _Snapshot(jobs)
        except _Unreadable as exc:
            skipped = {"skipped": "registry-oversized" if str(exc) == "registry-oversized"
                       else "registry-unreadable"}
            state.update(last_pass_ns=now_ns, eligible_since=since, last_result=skipped)
            _store_state(layout, state)
            return skipped
        ctx = _Context(jobs, root, layout, snapshot, now_ns)
        ctx.by_key = _declared_index(snapshot, root, ctx.workers_real)
        scan = contextlib.ExitStack()
        try:
            from dispatch_contract import process_table_scan_scope
            scan.enter_context(process_table_scan_scope())
            for key in batch:
                if time.monotonic() - started > EXAMINE_BUDGET_SECONDS:
                    break
                cursor = key
                result["examined"] += 1
                try:
                    verdict, ident = _examine(ctx, key)
                except Exception:  # noqa: BLE001 -- one bad home must not stall every pass
                    verdict, ident = _Verdict("unknown", "examine-error"), None
                if verdict.kind != "eligible":
                    if verdict.kind == "keep":
                        since.pop(key, None)
                    _count(result["unknown" if verdict.kind == "unknown" else "kept"], verdict.reason)
                    continue
                result["eligible"] += 1
                first = since.setdefault(key, now_ns)
                ended = _ended_ns(snapshot.rows[verdict.attempt])
                start = ended if ended is not None else first
                mark = _stat_mtime(key, dir_fd=layout.views_fd, follow_symlinks=False)
                idle_from = max(start, verdict.activity_ns, mark, ident[3])
                if now_ns - idle_from >= IDLE_SECONDS * 10**9:
                    due.append((key, ident[:3], verdict, start))
                else:
                    _count(result["kept"], "within-window")
        finally:
            scan.close()
        result["due"] = len(due)
        for key, ident, verdict, start in due:
            if budget <= 0 or time.monotonic() > deadline:
                _count(result["kept"], "deferred")
                continue
            budget -= 1
            outcome = _detach(ctx, key, ident, verdict.attempt, verdict.raw, start)
            if not outcome.startswith("detached:"):
                if (outcome.startswith("protected:") or outcome in ("home-gone", "identity-changed")
                        or (outcome == "late-evidence-changed" and
                            _renewed_protection(ctx, key, verdict.attempt))):
                    since.pop(key, None)
                _count(result["kept"], outcome)
                continue
            name = outcome.split(":", 1)[1]
            since.pop(key, None)
            with contextlib.suppress(OSError):
                # destructive-ok: reason=drop the view marker of a detached home; boundary=one marker file directly under homes/worker-retention/views
                os.unlink(key, dir_fd=layout.views_fd)
            if _remove_leaf(layout, name, ident, deadline) == "removed":
                result["removed"] += 1
            else:
                result["partial"] += 1
    for name in _view_names(layout):
        if name not in present:
            with contextlib.suppress(OSError):
                # destructive-ok: reason=drop a view marker whose home no longer exists; boundary=one marker file directly under homes/worker-retention/views
                os.unlink(name, dir_fd=layout.views_fd)
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    state.update(schema=SCHEMA, last_pass_ns=now_ns, cursor=cursor, eligible_since=since,
                 last_result=result)
    _store_state(layout, state)
    return result


def _view_names(layout: _Layout) -> list[str]:
    try:
        with os.scandir(layout.views_fd) as listing:
            return [entry.name for entry in listing if KEY_RE.fullmatch(entry.name)]
    except OSError:
        return []


def _examine(ctx: _Context, key: str):
    """(verdict, [dev, ino, uid, mtime_ns]) for one enumerated home."""
    try:
        home = os.open(key, _DIR_FLAGS, dir_fd=ctx.layout.workers_fd)
    except OSError:
        return _Verdict("keep", "home-gone"), None
    try:
        st = os.fstat(home)
        if st.st_uid != os.geteuid():
            return _Verdict("unknown", "foreign-owner"), None
        bound = _bind(ctx, key, home)
    finally:
        os.close(home)
    if isinstance(bound, _Verdict):
        return bound, None
    attempt, _identity_value = bound
    verdict = _protections(ctx, key, attempt, probe_processes=True)
    return verdict, _identity(st) + [st.st_mtime_ns]


# --- fleet view marker ---------------------------------------------------


def note_view(jobs, runtime_home, *, now: float | None = None) -> bool:
    """Admit excluded homes directly; candidate reads require a valid marker.

    The marker is written under a shared lock the final detach holds
    exclusively, so a view either lands before the detach's last check (and
    keeps the home) or after the rename (and finds no home to read).
    """
    try:
        if not runtime_home or not os.path.isabs(str(runtime_home)):
            return False
        root = _state_root(Path(jobs).resolve())
        parent, key = os.path.split(os.path.normpath(str(runtime_home)))
        resolved_parent = Path(parent).resolve(strict=True)
        workers = (root / "homes" / "workers").resolve(strict=False)
        if resolved_parent != workers or not KEY_RE.fullmatch(key):
            home = os.stat(runtime_home, follow_symlinks=False)
            return stat.S_ISDIR(home.st_mode)
        now = time.time() if now is None else now
        with _Layout(root) as layout:
            with _flock(layout.ret_fd, "view.lock", fcntl.LOCK_SH | fcntl.LOCK_NB) as held:
                if not held:
                    return False
                home = os.stat(key, dir_fd=layout.workers_fd, follow_symlinks=False)
                if not stat.S_ISDIR(home.st_mode):
                    return False
                marker = _read_regular(key, 1, dir_fd=layout.views_fd)
                if marker is not None:
                    if marker[0] != b"":
                        return False
                    if 0 <= now - marker[1] / 1e9 < VIEW_REFRESH_SECONDS:
                        return True
                fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                             dir_fd=layout.views_fd)
                try:
                    stamp = int(now * 1e9)
                    os.utime(fd, ns=(stamp, stamp))
                finally:
                    os.close(fd)
        return True
    except Exception:  # noqa: BLE001 -- display must never fail on bookkeeping
        return False
