"""Retire expired per-attempt worker homes without a user step (issue #564).

A home under ``<dispatch-state-root>/homes/workers/<32-hex>`` belongs to exactly
one attempt. It becomes due after one hour without activity, measured from the
best completion estimate (terminal row timestamp or later regular-log mtime), or
from the first proven pass only when that timestamp cannot be parsed. Later
activity, the home mtime and Fleet view markers extend the clock. Fleet markers
refresh every five minutes; an admitted read using a cached marker retains at
least 55 minutes of the one-hour view protection. Once terminal, quiescent and
unused for `IDLE_SECONDS`, the home is detached with one rename and removed
outside every lock. Anything unproven stays: the policy never guesses, and every
uncertain case is counted in ``homes/worker-retention/state.json`` instead of hidden.

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

import bisect
import contextlib
import contextvars
import ctypes
import struct
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import dispatch_attempt_reference as attempt_reference
from dispatch_pending_delivery import LEGACY_NOTICE_ID_RE

IDLE_SECONDS = 3600
DUE_SECONDS = 300
# While the last pass left due homes behind, the next pass may start sooner.
BACKLOG_DUE_SECONDS = 60
EXAMINE_LIMIT = 128
REMOVE_LIMIT = 16
EXAMINE_BUDGET_SECONDS = 10
REMOVE_DEADLINE_SECONDS = 20
VIEW_REFRESH_SECONDS = 300

MAX_REGISTRY_BYTES = 512 * 1024 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_DELIVERY_BYTES = 256 * 1024
# Delivery records one pass reads. It is a per-pass budget, not a store limit:
# past it the candidate is deferred and the next pass resumes from the saved
# position. Tombstones, locks and other directory entries are never read, so
# they cost only time (the pass deadline), not this budget.
DELIVERY_READ_BUDGET = 4096
# Entries one late-change directory listing may hold before it is unknown.
MAX_DIRECTORY_ENTRIES = 131072
MAX_DEPENDENCY_STAMPS = 32768
MAX_HOME_REFERENCE_ENTRIES = 4096
MAX_HOME_SCAN_PIDS = 8192
MAX_HOME_SCAN_FDS = 2048
MAX_TREE_DEPTH = 128
REGISTRY_LOCK_WAIT_SECONDS = 0.100
# A publication that landed after the observation's mark has a file ctime at or
# after it. The slack only covers the coarse kernel clock and a small clock step.
MARK_SLACK_NS = 2 * 10**9
# Bodies the final admission may read; more goes back to the next pass.
ADMISSION_REREAD_LIMIT = 32
_NO_SCAN = object()

KEY_RE = re.compile(r"[0-9a-f]{32}")
# OpenCode rows served by the shared service record this exact token instead of
# a per-attempt home (adapters/opencode/bin/dispatch-headless.py); it claims no
# home. Any other non-matching value remains a conflict.
SHARED_SERVICE_RUNTIME_HOME = "shared-service"
ATTEMPT_RE = attempt_reference.ATTEMPT_RE
NOTICE_RE = LEGACY_NOTICE_ID_RE
ATTEMPT_ID_RE = re.compile(r"(?:^|,)attempt_id=([^,]+)")
LEAF_RE = re.compile(r"([0-9a-f]{32})\.([0-9]{1,20})(?:\.([0-9]{1,5}))?")
DELIVERY_ID_RE = re.compile(r"delivery-(?:[0-9a-f]{32}|[0-9a-f]{64})")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
from dispatch_retention import (MAX_INTENT_CHILDREN, OPEN_STATUSES as OPEN,
                                TERMINAL_STATUSES as TERMINAL,
                                Protection as _Verdict)
ROUTE_KEYS = ("route_file", "route_id", "route_hash", "owner_route_file",
              "owner_route_id", "owner_route_hash", "batch_route_id")
HARNESSES = frozenset({"claude", "codex", "opencode"})
WORKER_TYPES = frozenset({"owner", "stage", "review", "support", "frame"})
SCHEMA = 1

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_DEPENDENCIES = contextvars.ContextVar("retention_dependencies", default=None)
_REGISTRY_DEADLINE = contextvars.ContextVar("retention_registry_deadline", default=None)


def _stamp_from_stat(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _stamp(path):
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _stamp_from_stat(st)


def _stamp_at(dir_fd, name):
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _stamp_from_stat(st)


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


RAW_UNKNOWN_CAP_NS = 7 * 24 * 60 * 60 * 10**9


def _main_runtime_state(row: _Row) -> str:
    """live/dead/unknown for the exact interactive root recorded at launch."""
    meta = row.meta
    harness = meta.get("main_runtime_harness", "")
    pid = meta.get("main_runtime_pid", "")
    start = meta.get("main_runtime_pid_start", "")
    namespace = meta.get("main_runtime_ns", "")
    if not (harness and pid and start and namespace):
        # Legacy Claude rows used the same all-or-none fields for completion.
        harness = "claude" if all(meta.get(key) for key in (
            "parent_runtime_pid", "parent_runtime_pid_start", "parent_runtime_ns")) else ""
        pid, start, namespace = (meta.get("parent_runtime_pid", ""),
                                 meta.get("parent_runtime_pid_start", ""),
                                 meta.get("parent_runtime_ns", ""))
    if harness not in {"claude", "codex", "opencode"} or not pid.isdigit() or not start or not namespace:
        return "unknown"
    current_namespace = ""
    try:
        from dispatch_contract import process_namespace_identity
        current_namespace = process_namespace_identity()
        if not current_namespace or current_namespace != namespace:
            return "unknown"
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        if len(fields) <= 19:
            return "unknown"
        if fields[0] in {"Z", "X"} or fields[19] != start:
            return "dead"
        return "live"
    except FileNotFoundError:
        return "dead" if current_namespace == namespace else "unknown"
    except (OSError, ValueError, IndexError):
        return "unknown"


def _retain_raw_for_main(row: _Row, first_ns: int, now_ns: int) -> bool:
    harness = row.meta.get("harness", "")
    if harness not in {"claude", "opencode"}:
        return False
    state = _main_runtime_state(row)
    return state == "live" or (state == "unknown" and now_ns - first_ns < RAW_UNKNOWN_CAP_NS)


class _Snapshot:
    """One read of ``jobs.log``: the latest row per attempt and open references."""

    def __init__(self, jobs: Path):
        self.jobs = jobs
        _track(jobs)
        self.registry_snapshot: tuple[int, int, int, int, int] | None = None
        self.rows: dict[str, _Row] = {}
        self.row_offsets: dict[str, int] = {}
        self.open_refs: dict[str, set[str]] = {}
        self.log_refs: dict[str, dict[str, _Row]] = {}
        self.unknown_log_refs: set[str] = set()
        invalid_log_refs: set[str] = set()
        # parent attempt -> children whose completion delivery was intended
        self.intents: dict[str, set[str]] = {}
        try:
            handle = open(jobs, "rb")
        except OSError as exc:
            raise _Unreadable("registry") from exc
        with handle:
            try:
                info = os.fstat(handle.fileno())
            except OSError as exc:
                raise _Unreadable("registry") from exc
            size = info.st_size
            if size > MAX_REGISTRY_BYTES:
                raise _Unreadable("registry-oversized")
            self.registry_snapshot = (info.st_dev, info.st_ino, size,
                                      info.st_mtime_ns, info.st_ctime_ns)
            remaining = size
            offset = 0
            while remaining:
                blob = handle.readline(remaining)
                if not blob:
                    raise _Unreadable("registry-unstable")
                line_offset = offset
                offset += len(blob)
                remaining -= len(blob)
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
                    self.row_offsets[found.group(1)] = line_offset
                else:
                    candidate_log = re.search(r"(?:^|,)log_file=([^,]+)", fields[5])
                    if candidate_log:
                        invalid_log_refs.add(candidate_log.group(1))
        self.unknown_log_refs.update(invalid_log_refs)
        for attempt, row in self.rows.items():
            log_match = re.search(r"(?:^|,)log_file=([^,]+)", row.fields[5])
            if not log_match:
                continue
            log_file = log_match.group(1)
            harness_match = re.search(r"(?:^|,)harness=([^,]+)", row.fields[5])
            harness = harness_match.group(1) if harness_match else ""
            if not ATTEMPT_RE.fullmatch(attempt) or harness not in HARNESSES:
                self.unknown_log_refs.add(log_file)
                continue
            self.log_refs.setdefault(log_file, {})[attempt] = row
        for attempt, row in self.rows.items():
            if row.status in OPEN:
                for token in set(ATTEMPT_RE.findall(row.fields[5])):
                    if token != attempt:
                        self.open_refs.setdefault(token, set()).add(attempt)
            elif "delivery_intent=1" in row.fields[5]:
                parent = row.meta.get("parent_attempt_id", "")
                if row.meta.get("delivery_intent") == "1" and ATTEMPT_RE.fullmatch(parent):
                    self.intents.setdefault(parent, set()).add(attempt)


@dataclass(frozen=True)
class _HomeBinding:
    key: str
    attempt: str | None
    identity: str | None
    source: str
    harness: str
    worker_type: str
    schema: int | None


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


def _candidate_related_attempts(root: Path, snapshot: _Snapshot,
                                attempt: str | None) -> set[str]:
    if not attempt:
        return set()
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
    return related


def _candidate_recipient_groups(root: Path, snapshot: _Snapshot, attempt: str) -> set[str]:
    """Producer-bound groups, including absent groups and terminal owners."""
    groups = set()
    watched = _DEPENDENCIES.get()
    for identity in _candidate_related_attempts(root, snapshot, attempt):
        row = snapshot.rows.get(identity)
        if row is not None:
            group = _recipient_group(root, row.meta)
            if group is not None:
                groups.add(group)
                if watched is not None:
                    _track_resolver_parent(group)
    return groups


def _attempt_maps_to_binding(attempt: str, binding: _HomeBinding) -> bool:
    if not ATTEMPT_RE.fullmatch(attempt or ""):
        return False
    from worker_runtime_home import _builder
    return _builder.worker_home_key(
        binding.source, binding.harness, binding.worker_type, attempt
    ) == binding.key


def _candidate_attempt_ids(snapshot: _Snapshot, binding: _HomeBinding) -> set[str]:
    return {attempt for attempt in snapshot.rows
            if _attempt_maps_to_binding(attempt, binding)}


def _legacy_inventory_fresh(ctx: _Context) -> bool:
    """Cheaply fence the bounded inventory directories under final admission."""
    return all(_stamp(path) == before for path, before in ctx.legacy_dirs.items())


@contextlib.contextmanager
def _home_view_scope(jobs, *, blocking: bool):
    """Use the existing retention view lock as path-based HOME admission."""
    jobs = Path(jobs).resolve()
    root = _state_root(jobs)
    with _Layout(root) as layout:
        mode = fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB)
        with _flock(layout.ret_fd, "view.lock", mode) as acquired:
            yield layout, acquired


@contextlib.contextmanager
def home_view_admission(jobs, *, blocking: bool = True):
    """Admission for a path-based writer that can introduce HOME references."""
    root = _state_root(Path(jobs).resolve())
    try:
        homes = os.stat(root / "homes", follow_symlinks=False)
    except FileNotFoundError:
        yield True
        return
    if not stat.S_ISDIR(homes.st_mode) or homes.st_uid != os.geteuid():
        yield False
        return
    try:
        workers = os.stat(root / "homes" / "workers", follow_symlinks=False)
    except FileNotFoundError:
        yield True
        return
    if not stat.S_ISDIR(workers.st_mode) or workers.st_uid != os.geteuid():
        yield False
        return
    with _home_view_scope(jobs, blocking=blocking) as (_layout, acquired):
        yield acquired


# Every record lock asks this before admission (claim, submission fence, send,
# ACK: four times per notice), and one Codex prompt that attaches several
# notices asked the same questions again each time: a 4 MiB registry tail and,
# for an attempt outside it, every worker HOME manifest (2,277 on the live
# machine). Both answers stay valid while the registry file and the
# worker-home directory keep their identity, so a process keeps each one for
# exactly that. A HOME's key names its attempt, so a listing whose directory
# did not change cannot gain or lose one; a listing taken within a timestamp
# tick of the directory's last change is not kept.
_REFERENCE_TAILS: dict[tuple, frozenset] = {}
_REFERENCE_HOMES: dict[tuple, tuple] = {}
_REFERENCE_SETTLED_NS = 2_000_000_000


def _registry_tail_attempts(jobs: Path):
    """Attempt ids of the last 4 MiB of rows, or None when that cannot be read."""
    try:
        fd = os.open(jobs, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            return None
        key = (str(jobs), before.st_dev, before.st_ino, before.st_size,
               before.st_mtime_ns, before.st_ctime_ns)
        if key in _REFERENCE_TAILS:
            return _REFERENCE_TAILS[key]
        offset = max(0, before.st_size - 4 * 1024 * 1024)
        raw = os.pread(fd, before.st_size - offset, offset)
        after = os.fstat(fd)
        if (len(raw) != before.st_size - offset
                or (before.st_dev, before.st_ino, before.st_size) !=
                    (after.st_dev, after.st_ino, after.st_size)):
            return None
        if offset:
            boundary = raw.find(b"\n")
            raw = raw[boundary + 1:] if boundary >= 0 else b""
        found = set()
        for line in raw.splitlines():
            fields = line.decode("utf-8", "replace").split("\t")
            if len(fields) != 6:
                continue
            match = ATTEMPT_ID_RE.search(fields[5])
            if match:
                found.add(match.group(1))
        found = frozenset(found)
        if (after.st_mtime_ns, after.st_ctime_ns) == (before.st_mtime_ns, before.st_ctime_ns):
            if len(_REFERENCE_TAILS) >= 8:
                _REFERENCE_TAILS.clear()
            _REFERENCE_TAILS[key] = found
        return found
    except OSError:
        return None
    finally:
        os.close(fd)


def _home_manifests(root):
    """Every worker HOME as ``(key, source, harness, worker_type, identity)``.

    ``False`` when there is no worker-home directory, ``None`` when a HOME
    could not be read or judged safely (the caller then cannot rule one out).
    """
    directory_fds = []
    try:
        root_fd = _open_dir(str(Path(root).resolve()), None)
        directory_fds.append(root_fd)
        homes_fd = _open_dir("homes", root_fd)
        directory_fds.append(homes_fd)
        workers_fd = _open_dir("workers", homes_fd)
        directory_fds.append(workers_fd)
    except FileNotFoundError:
        for fd in reversed(directory_fds):
            os.close(fd)
        return False
    except (_Unsafe, OSError):
        for fd in reversed(directory_fds):
            os.close(fd)
        return None
    try:
        listed = os.fstat(workers_fd)
        key = (str(Path(root).resolve()), listed.st_dev, listed.st_ino, listed.st_mtime_ns,
               listed.st_ctime_ns, listed.st_size, listed.st_nlink)
        if key in _REFERENCE_HOMES:
            return _REFERENCE_HOMES[key]
        started_ns = time.time_ns()
        with os.scandir(workers_fd) as listing:
            entries = []
            for entry in listing:
                if len(entries) >= MAX_HOME_REFERENCE_ENTRIES:
                    return None
                if KEY_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    entries.append(entry.name)
        manifests = []
        for name in entries:
            try:
                home = os.open(name, _DIR_FLAGS, dir_fd=workers_fd)
            except FileNotFoundError:
                continue
            except OSError:
                return None
            try:
                if os.fstat(home).st_uid != os.geteuid():
                    return None
                found = _read_json("worker-home.json", MAX_MANIFEST_BYTES, dir_fd=home)
                if found is None:
                    return None
                manifest = found[0]
                schema = manifest.get("schema")
                source = manifest.get("source")
                harness = manifest.get("harness")
                worker_type = manifest.get("worker_type")
                identity = manifest.get("identity")
                if (schema not in (None, 1, 2) or harness not in HARNESSES
                        or worker_type not in WORKER_TYPES or not isinstance(source, str)
                        or not os.path.isabs(source)
                        or manifest.get("destination") is True
                        or ("destination" in manifest
                            and not isinstance(manifest.get("destination"), bool))):
                    return None
                if ("key" in manifest and manifest.get("key") != name
                        or schema == 2 and manifest.get("key") != name):
                    return None
                if identity is not None and not ATTEMPT_RE.fullmatch(identity):
                    return None
                manifests.append((name, source, harness, worker_type, identity))
            except _Unreadable:
                return None
            finally:
                os.close(home)
        manifests = tuple(manifests)
        settled = started_ns - max(listed.st_mtime_ns, listed.st_ctime_ns) >= _REFERENCE_SETTLED_NS
        if settled and _stat_key(os.fstat(workers_fd)) == _stat_key(listed):
            if len(_REFERENCE_HOMES) >= 8:
                _REFERENCE_HOMES.clear()
            _REFERENCE_HOMES[key] = manifests
        return manifests
    except OSError:
        return None
    finally:
        for fd in reversed(directory_fds):
            os.close(fd)


def _stat_key(info):
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_nlink)


def home_reference_relevant(root, attempt_ids) -> bool | None:
    """Whether an incoming attempt reference can name an existing worker HOME.

    This bounded path lookup runs before delivery record admission. Returning
    ``None`` means the relation could not be ruled out safely.
    """
    attempts = tuple(item for item in attempt_ids if ATTEMPT_RE.fullmatch(item or ""))
    if not attempts:
        return False
    found = _registry_tail_attempts(Path(root).resolve() / "jobs.log")
    if found is not None and set(attempts).issubset(found):
        return False
    manifests = _home_manifests(root)
    if manifests is False or manifests is None:
        return manifests
    from worker_runtime_home import _builder
    for name, source, harness, worker_type, identity in manifests:
        for attempt in attempts:
            if identity is not None and identity != attempt:
                continue
            if _builder.worker_home_key(source, harness, worker_type, attempt) == name:
                return True
    return False


@contextlib.contextmanager
def worker_home_read_admission(jobs, runtime_home, transcript, attempt, harness):
    """Pin one worker HOME for a native reader or supervisor lifetime.

    The reader holds a shared flock on that HOME directory; final detach takes
    the same lock exclusively on the exact leaf it renames. Only this HOME is
    affected, so a long-lived reader never blocks detach of another HOME, and a
    reader that loses to detach sees the key gone or replaced and is refused.
    The attempt comes from the existing caller. It is used only to validate the
    legacy key and is never inferred or persisted by retention.
    """
    try:
        root = _state_root(Path(jobs).resolve())
        requested = Path(runtime_home).resolve(strict=False)
        key = requested.name
        if not requested.is_absolute() or not ATTEMPT_RE.fullmatch(attempt or "") or harness not in HARNESSES:
            yield False
            return
        workers = Path(os.path.realpath(root / "homes" / "workers"))
        if requested.parent != workers:
            # These existing callers point at an external native source; the
            # worker-HOME detach lock does not govern that path.
            yield True
            return
        if KEY_RE.fullmatch(key) is None:
            yield False
            return
    except (OSError, RuntimeError, ValueError):
        yield False
        return

    pinned = None
    try:
        with _Layout(root) as layout:
            pinned = _pin_home_for_read(layout, requested, transcript, attempt, harness)
    except (_Unsafe, OSError, RuntimeError, ValueError):
        pinned = None
    try:
        yield pinned is not None
    finally:
        if pinned is not None:
            os.close(pinned)


def _pin_home_for_read(layout: _Layout, requested: Path, transcript,
                       attempt: str, harness: str) -> int | None:
    """The shared-locked HOME directory fd for a valid read, `None` otherwise."""
    key = requested.name
    try:
        home = os.open(key, _DIR_FLAGS, dir_fd=layout.workers_fd)
    except OSError:
        return None
    try:
        fcntl.flock(home, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if _worker_home_read_matches(layout, home, requested, transcript, attempt, harness):
            return home
    except (OSError, RuntimeError, ValueError, _Unreadable):
        pass
    os.close(home)
    return None


def _worker_home_read_matches(layout: _Layout, home: int, requested: Path, transcript,
                              attempt: str, harness: str) -> bool:
    """Revalidate the locked HOME: manifest, attempt key, transcript and inode."""
    key = requested.name
    info = os.fstat(home)
    found = _read_json("worker-home.json", MAX_MANIFEST_BYTES, dir_fd=home)
    if info.st_uid != os.geteuid() or found is None:
        return False
    manifest = found[0]
    source = manifest.get("source")
    worker_type = manifest.get("worker_type")
    schema = manifest.get("schema")
    if (manifest.get("harness") != harness or worker_type not in WORKER_TYPES
            or not isinstance(source, str) or not source or schema not in (None, 1, 2)
            or manifest.get("destination") is True
            or ("key" in manifest and manifest.get("key") != key)):
        return False
    if (schema == 2 and manifest.get("identity") != attempt
            or manifest.get("identity") not in (None, attempt)):
        return False
    binding = _HomeBinding(key, None, None, source, harness, worker_type, schema)
    if not _attempt_maps_to_binding(attempt, binding):
        return False
    if transcript is not None:
        transcript_path = Path(transcript).resolve(strict=True)
        if (not transcript_path.is_absolute()
                or os.path.commonpath((str(requested), str(transcript_path))) != str(requested)
                or not stat.S_ISREG(os.stat(transcript_path, follow_symlinks=False).st_mode)):
            return False
    # A detach that won first renamed this inode away from its key.
    current = os.stat(key, dir_fd=layout.workers_fd, follow_symlinks=False)
    return (stat.S_ISDIR(current.st_mode)
            and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino))


_GETDENTS: list = []


def _getdents64():
    """libc's getdents64, or None where it is missing (macOS, glibc < 2.30)."""
    if not _GETDENTS:
        try:
            function = ctypes.CDLL(None, use_errno=True).getdents64
            function.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t)
            function.restype = ctypes.c_ssize_t
        except (AttributeError, OSError):
            function = None
        _GETDENTS.append(function)
    return _GETDENTS[0]


def _listing_mode() -> str:
    """What a saved directory cookie means; a saved observation is only resumed in the same mode."""
    return "getdents64" if _getdents64() is not None else "sorted"


def _sorted_page(fd: int, path, before, cookie: int, limit: int):
    """Portable page: the cookie is a 1-based index into the sorted names.

    It stays valid while the directory's stamp is unchanged, and a changed
    directory restarts at 0 (the caller's generation check), as with d_off.
    """
    found = []
    with os.scandir(fd) as listing:
        for entry in listing:
            try:
                dtype = (10 if entry.is_symlink() else 4 if entry.is_dir(follow_symlinks=False)
                         else 8 if entry.is_file(follow_symlinks=False) else 0)
            except OSError:
                dtype = 0
            found.append((entry.name, dtype))
    found.sort()
    if not 0 <= cookie <= len(found):
        raise _Unreadable("directory-cookie-invalid")
    entries = [(name, index + 1, dtype)
               for index, (name, dtype) in enumerate(found[cookie:cookie + limit], cookie)]
    if (_stamp_from_stat(os.fstat(fd)) != before or _stamp(path) != before):
        raise _Unreadable("directory-changed")
    return entries, not entries, before


def _directory_page(path: Path, cookie: int, limit: int, deadline: float):
    """One bounded directory read; the cookie is an opaque restart position.

    Linux uses getdents64 (d_off is the cookie). Where libc has no getdents64
    (macOS, glibc < 2.30) a sorted listing gives index cookies instead.
    Reopen on every page (and process). Never use scandir's read-ahead offset.
    No guessed syscall numbers or portable/NFS resume claim is made here.
    """
    if time.monotonic() >= deadline or limit <= 0:
        return [], False, None
    try:
        fd = os.open(path, _DIR_FLAGS)
    except FileNotFoundError:
        return [], True, None
    try:
        before = _stamp_from_stat(os.fstat(fd))
        if before[3] != os.geteuid():
            raise _Unreadable("directory-owner")
        getdents = _getdents64()
        if getdents is None:
            return _sorted_page(fd, path, before, cookie, limit)
        if cookie:
            os.lseek(fd, cookie, os.SEEK_SET)
        buf = ctypes.create_string_buffer(8192)
        count = getdents(fd, buf, len(buf))
        if count < 0:
            raise _Unreadable("directory-cookie-unavailable")
        entries, pos, last_cookie = [], 0, cookie
        while pos < count and len(entries) < limit:
            if count - pos < 20:
                raise _Unreadable("directory-entry-truncated")
            _ino, next_cookie, reclen, dtype = struct.unpack_from("=QqHB", buf.raw, pos)
            if (reclen < 20 or pos + reclen > count or dtype not in (0, 1, 2, 4, 6, 8, 10, 12, 14)):
                raise _Unreadable("directory-entry-invalid")
            name = buf.raw[pos + 19:pos + reclen].split(b"\0", 1)
            if len(name) != 2 or not name[0] or b"/" in name[0] or next_cookie == last_cookie:
                raise _Unreadable("directory-cookie-invalid")
            entries.append((os.fsdecode(name[0]), next_cookie, dtype))
            pos += reclen
            last_cookie = next_cookie
        if (_stamp_from_stat(os.fstat(fd)) != before or _stamp(path) != before):
            raise _Unreadable("directory-changed")
        return entries, count == 0, before
    finally:
        os.close(fd)


def _generation(stamp):
    return hashlib.sha256(json.dumps(stamp, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _reference_fingerprint(root, identities, *, initialize=False, deadline=None):
    """The candidate's reference tokens, or None (busy, deadline, or they cannot
    be read or created, e.g. on a full disk): None defers only this candidate."""
    from dispatch_pending_delivery import PendingDeliveryError, reference_token
    values = []
    try:
        scope = (contextlib.nullcontext(True) if set(identities).issubset(attempt_reference.held_attempts())
                 else attempt_reference.admission(root, identities, blocking=False))
        with scope as admitted:
            if not admitted:
                return None
            for identity in sorted(identities):
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                value = reference_token(root, identity, initialize=initialize)
                if value is None:
                    return None
                values.append((identity, value))
    except (OSError, attempt_reference.AttemptReferenceError, PendingDeliveryError):
        return None
    return _generation(values)


def _cohort_identity(root, subjects):
    """A subject's related attempts are not part of it: when they change, the
    subject's reference token changes and only that subject is deferred."""
    return _generation((str(root), _listing_mode(),
                        [{k: v for k, v in s.items() if k != "related"} for s in subjects]))


def _directory_identity(path):
    stamp = _stamp(path)
    return None if stamp is None else [stamp[0], stamp[1], stamp[3]]


class _DeliveryObservation:
    """A resumable read for one bounded cohort, owned by existing pass state.

    Durable data is a directory position and candidate protection/activity
    reductions. Payloads and history file stamps live only in this invocation.
    Normal writers invalidate only related candidates. A completed observation
    is consumed in this pass, then cold discovery runs again as the backstop.
    """
    def __init__(self, ctx, subjects, saved, deadline):
        self.ctx, self.subjects, self.deadline = ctx, subjects, deadline
        self.visits = self.reads = self.metadata = 0
        self.advanced = False
        # Subjects whose recorded evidence moved after their verdict was read.
        self.fresh_related: set[str] = set()
        self.identity = _cohort_identity(ctx.root, subjects)
        self.tokens = {}
        for subject in subjects:
            refs = subject["related"]
            self.tokens[subject["id"]] = (_reference_fingerprint(ctx.root, refs, initialize=True, deadline=deadline)
                                           if refs else self._view_token(initialize=True))
        valid = (isinstance(saved, dict) and saved.get("schema") == 1 and saved.get("started") is True
                 and saved.get("integrity") == _generation({k: v for k, v in saved.items() if k != "integrity"})
                 and saved.get("identity") == self.identity and not saved.get("complete")
                 and isinstance(saved.get("results"), dict)
                 and set(saved["results"]) == {s["id"] for s in subjects}
                 and isinstance(saved.get("position"), dict))
        self.resumed = valid
        self.data = saved if valid else {
            "schema": 1, "identity": self.identity, "complete": False, "started": False,
            # One filesystem-clock reading taken before the first group is read.
            "mark": time.time_ns(),
            # Recipient groups are walked in name order: `root_done` is the last
            # finished group, `cookie` the position inside the current `group`.
            "position": {"root_done": "", "group": "", "cookie": 0,
                         "root_generation": _generation(_directory_identity(ctx.root / "pending-delivery")),
                         "generation": "", "inventory": 0},
            # "unknown" holds the first cause that left the read incomplete.
            "results": {s["id"]: {"token": self.tokens[s["id"]], "open": False,
                                    "unknown": "", "activity": 0, "matches": []}
                        for s in subjects}}
        try:
            pos = self.data["position"]
            mark = self.data.get("mark")
            if (type(mark) is not int or mark <= 0 or mark > time.time_ns() + MARK_SLACK_NS
                    or type(pos.get("cookie")) is not int or not -(1 << 63) <= pos["cookie"] < (1 << 63)
                    or not isinstance(pos.get("root_done"), str)
                    or (pos["root_done"] and SHA256_RE.fullmatch(pos["root_done"]) is None)
                    or type(pos.get("inventory")) is not int or pos["inventory"] < 0
                    or not isinstance(pos.get("root_generation"), str)
                    or SHA256_RE.fullmatch(pos["root_generation"]) is None
                    or not isinstance(pos.get("generation"), str)
                    or (pos["generation"] and SHA256_RE.fullmatch(pos["generation"]) is None)
                    or type(pos.get("delivery_done", False)) is not bool
                    or not isinstance(pos.get("group"), str)
                    or (pos["group"] and SHA256_RE.fullmatch(pos["group"]) is None)
                    or any(not isinstance(row, dict) or set(row) != {
                        "token", "open", "unknown", "activity", "matches"}
                        or type(row["open"]) is not bool or type(row["unknown"]) is not str
                        or len(row["unknown"]) > 64
                        or type(row["activity"]) is not int or row["activity"] < 0
                        or not isinstance(row["matches"], list)
                        or len(row["matches"]) > MAX_INTENT_CHILDREN
                        or any(not isinstance(a, str) or not ATTEMPT_RE.fullmatch(a)
                               for a in row["matches"])
                        for row in self.data["results"].values())):
                raise ValueError("observation-shape")
        except (KeyError, TypeError, ValueError):
            # A torn/untrusted cursor never supplies negative evidence.
            self.__init__(ctx, subjects, None, deadline)
            return
        self.inventories = []
        if any(s.get("legacy") for s in subjects):
            self.inventories = [
                (ctx.root / "automatic-replacements/by-source", "record"),
                (ctx.root / "recovery-attention/by-source", "record"),
                (ctx.root / "supervisor-state", "supervisor"),
            ]
            import dispatch_summary
            for harness in sorted({s["binding"][1] for s in subjects if s.get("binding")}):
                self.inventories.extend([
                    (dispatch_summary.owner_root() / harness, "record"),
                    (dispatch_summary._state_root() / harness, "provider")])

    def _view_token(self, *, initialize=False):
        from dispatch_pending_delivery import PendingDeliveryError, _change_token
        path = self.ctx.root / "homes/worker-retention/view.lock"
        try:
            if initialize:
                with _flock(self.ctx.layout.ret_fd, "view.lock", fcntl.LOCK_EX | fcntl.LOCK_NB) as held:
                    if not held:
                        return None
                    value = _change_token(path)
                    return _generation(value or _change_token(path, advance=True))
            value = _change_token(path)
            return _generation(value) if value else None
        except (OSError, AttributeError, PendingDeliveryError):
            return None

    def _match(self, subject, identity):
        if identity in subject["related"]:
            return True
        binding = subject.get("binding")
        if binding is None:
            return False
        source, harness, worker_type, key = binding
        from worker_runtime_home import _builder
        return _builder.worker_home_key(source, harness, worker_type, identity) == key

    def _unknown(self, cause, *, legacy_only=False):
        # The first cause is kept; it is reported, never used as evidence.
        # Ledger inventories are read only for identity-less (legacy) subjects.
        for subject in self.subjects:
            if legacy_only and not subject.get("legacy"):
                continue
            result = self.data["results"][subject["id"]]
            result["unknown"] = result["unknown"] or cause

    def _record(self, path, dir_fd=None, *, flag=False):
        """Fold one record into the cohort; `_Unreadable(<cause>)` if it cannot be."""
        from dispatch_pending_delivery import record_references, OPEN_STATES, PendingDeliveryError
        self.reads += 1
        try:
            found = (_read_json(str(path), MAX_DELIVERY_BYTES) if dir_fd is None
                     else _read_json(path.name, MAX_DELIVERY_BYTES, dir_fd=dir_fd))
        except _Unreadable as exc:
            raise _Unreadable("record-unreadable") from exc
        if found is None:
            return
        value, mtime = found
        try:
            named = record_references(value, group=path.parent.name, delivery_id=path.name[:-5])
        except (PendingDeliveryError, TypeError, ValueError) as exc:
            raise _Unreadable("record-path-mismatch" if getattr(exc, "detail", "") == "record-path-mismatch"
                              else "record-invalid") from exc
        for subject in self.subjects:
            matches = {a for a in named if self._match(subject, a)}
            if not matches:
                continue
            result = self.data["results"][subject["id"]]
            seen = (result["open"], result["unknown"], result["activity"], tuple(result["matches"]))
            result["matches"] = sorted(set(result["matches"]) | (matches - set(subject["related"])))
            if len(result["matches"]) > MAX_INTENT_CHILDREN:
                result["matches"] = result["matches"][:MAX_INTENT_CHILDREN]
                result["unknown"] = result["unknown"] or "match-limit"
            if value["state"] in OPEN_STATES:
                result["open"] = True
            else:
                result["activity"] = max(result["activity"], mtime)
            if flag and seen != (result["open"], result["unknown"], result["activity"],
                                 tuple(result["matches"])):
                self.fresh_related.add(subject["id"])
            # Only bounded candidate-bound direct evidence is kept in memory.
            if len(self.ctx.delivery_records) < MAX_INTENT_CHILDREN:
                for identity in matches:
                    records = self.ctx.delivery_records.setdefault(identity, {})
                    if len(records) < 8:
                        records[str(path)] = _stamp(path)

    def checkpoint(self):
        # A damaged but still numeric opaque cookie can otherwise skip entries.
        # This detects cache corruption; only a real scan supplies completeness.
        self.data["integrity"] = _generation({k: v for k, v in self.data.items() if k != "integrity"})

    def refresh(self, deadline):
        """Read what was published after the saved mark, outside any final admission.

        Every new record is checked against the whole cohort, so evidence for a
        later candidate is not consumed unseen. On success the mark moves to the
        reading taken before this scan; its cost then follows the publications
        since the previous refresh. The read budget counts only entries that pass
        the ctime filter and are not already known, so unlinks and untouched old
        entries in a changed group are free; the window is one observation.
        Returns None, "unknown" or "deferred".
        """
        from dispatch_pending_delivery import PendingDeliveryError
        mark = self.data.get("mark")
        if type(mark) is not int:
            return "unknown"
        next_mark = time.time_ns()
        base = self.ctx.root / "pending-delivery"

        def visit(group_fd, group, name, stamp):
            path = base / group / name
            self._record(path, group_fd, flag=True)
            # Only a record that was read is known; a rejected one stays new for
            # every later candidate in this pass.
            self.ctx.discovery_stamps[path] = stamp
            return None

        try:
            outcome = _scan_published(base, mark - MARK_SLACK_NS, deadline, DELIVERY_READ_BUDGET,
                                      self.ctx.discovery_stamps, visit)
        except (_Unreadable, PendingDeliveryError, OSError, ValueError, TypeError):
            return "unknown"
        if outcome is not None:
            return outcome
        self.data["mark"] = next_mark
        return None

    def _bound_state(self):
        while len(json.dumps(self.data, separators=(",", ":")).encode()) > self.state_budget:
            if len(self.subjects) <= 1:
                self.data["complete"] = False
                return False
            self.subjects = self.subjects[:len(self.subjects) // 2]
            ids = {s["id"] for s in self.subjects}
            self.data["results"] = {k: v for k, v in self.data["results"].items() if k in ids}
            self.data["subjects"] = [s["id"] for s in self.subjects]
            self.identity = self.data["identity"] = _cohort_identity(self.ctx.root, self.subjects)
        return True

    def _groups(self, base):
        """Recipient-group names in name order, listed whole for this pass.

        None at the deadline. A store that changes between pages is listed again.
        """
        cookie, names, before = 0, [], _stamp(base)
        while time.monotonic() < self.deadline:
            self.metadata += 1
            try:
                entries, eof, observed = _directory_page(base, cookie, DELIVERY_READ_BUDGET, self.deadline)
            except _Unreadable as exc:
                if str(exc) != "directory-changed":
                    raise
                entries, eof, observed = [], False, object()
            if observed != before:
                cookie, names, before = 0, [], _stamp(base)
                continue
            if eof:
                return sorted(names)
            for name, next_cookie, dtype in entries:
                self.visits += 1
                if name in (".", ".."):
                    pass
                elif not SHA256_RE.fullmatch(name) or dtype not in (0, 4):
                    self._unknown("group-untrusted")
                else:
                    names.append(name)
                cookie = next_cookie
        return None

    def advance(self):
        if self.advanced:
            return
        self.advanced = True
        self.data["started"] = True
        pos = self.data["position"]
        base = self.ctx.root / "pending-delivery"
        from dispatch_pending_delivery import PendingDeliveryError
        # Only a replaced store restarts the traversal. Groups are walked in name
        # order, so a group added later cannot shift the position, and its
        # records are newer than the mark, where refresh and the final look
        # read them. Strong protections already found remain until a new
        # complete cold scan.
        root = _generation(_directory_identity(base))
        if root != pos["root_generation"]:
            pos.update(root_done="", cookie=0, group="", generation="", root_generation=root, delivery_done=False)
            # The traversal starts over, so every group is read after this reading.
            self.data["mark"] = time.time_ns()
        self.ctx.discovery_stamps[base] = _stamp(base)
        kind = "groups"
        groups = None
        try:
            # Only record reads spend the budget; every other entry costs time.
            while time.monotonic() < self.deadline:
                if pos.get("delivery_done"):
                    if pos["inventory"] >= len(self.inventories):
                        self.data["complete"] = True
                        break
                    directory, kind = self.inventories[pos["inventory"]]
                elif pos["group"]:
                    directory, kind = base / pos["group"], "delivery"
                else:
                    kind = "groups"
                    if groups is None:
                        groups = self._groups(base)
                        if groups is None:
                            return
                    index = bisect.bisect_right(groups, pos["root_done"])
                    if index < len(groups):
                        pos.update(group=groups[index], cookie=0, generation="")
                    else:
                        pos.update(delivery_done=True, cookie=0, generation="")
                    if not self._bound_state():
                        return
                    continue
                before = _stamp(directory)
                self.metadata += 1
                generation = _generation(before)
                if pos["generation"] and pos["generation"] != generation:
                    pos["cookie"] = 0
                pos["generation"] = generation
                entries, eof, observed = _directory_page(
                    directory, pos["cookie"], DELIVERY_READ_BUDGET, self.deadline)
                if observed != before:
                    break
                if kind == "delivery":
                    self.ctx.group_stamps[directory] = before
                    self.ctx.delivery_groups.add(directory.name)
                else:
                    self.ctx.legacy_dirs[directory] = before
                for name, cookie, dtype in entries:
                    if time.monotonic() >= self.deadline:
                        return
                    if (kind == "delivery" and name.endswith(".json")
                            and self.reads >= DELIVERY_READ_BUDGET):
                        # The budget is spent: the next pass reads this record first.
                        self._bound_state()
                        return
                    self.visits += 1
                    if name not in (".", ".."):
                        path = directory / name
                        if kind == "delivery":
                            if name.endswith(".json"):
                                stamp = _stamp(path)
                                self._record(path)
                                self.ctx.discovery_stamps[path] = stamp
                        else:
                            identity = None
                            if kind == "provider" and name.startswith(".lock-dispatch-"):
                                identity = name[len(".lock-dispatch-"):]
                            elif kind in ("record", "supervisor") and name.endswith(".json"):
                                identity = name[:-5]
                            elif kind == "supervisor" and name.endswith(".lease"):
                                identity = name[:-6]
                            if identity is not None:
                                try:
                                    info = os.stat(path, follow_symlinks=False)
                                except FileNotFoundError:
                                    info = None  # removed since the listing: nothing to match
                                if info is None:
                                    pass
                                elif (not ATTEMPT_RE.fullmatch(identity) or info.st_uid != os.geteuid()
                                        or (not stat.S_ISDIR(info.st_mode) if kind == "provider"
                                            else not stat.S_ISREG(info.st_mode))):
                                    self._unknown("ledger-entry-untrusted", legacy_only=True)
                                else:
                                    for subject in self.subjects:
                                        if self._match(subject, identity):
                                            result = self.data["results"][subject["id"]]
                                            result["matches"] = sorted(set(result["matches"]) | {identity})
                    pos["cookie"] = cookie
                if not self._bound_state():
                    return
                if eof:
                    if kind == "delivery":
                        pos.update(root_done=pos["group"], group="", cookie=0, generation="")
                    else:
                        pos.update(inventory=pos["inventory"] + 1, cookie=0, generation="")
            self._bound_state()
            if _generation(_directory_identity(base)) != pos["root_generation"]:
                self.data["complete"] = False
        except (OSError, _Unreadable, ValueError, TypeError, PendingDeliveryError) as exc:
            if str(exc) in ("directory-changed", "directory-cookie-unavailable"):
                return
            text = str(exc) if isinstance(exc, _Unreadable) else ""
            place = {"groups": "store", "delivery": "group"}.get(kind, "ledger")
            self._unknown(text if text.startswith("record-")
                          else place + ("-untrusted" if text == "directory-owner" else "-unreadable"),
                          legacy_only=place == "ledger")
            self.data["complete"] = True

    def result(self, subject_id):
        self.advance()
        result = self.data["results"].get(subject_id)
        if result is None:
            return "deferred", False, 0, ""
        if result["unknown"]:
            return "unknown", result["open"], result["activity"], result["unknown"]
        if not result["token"] or result["token"] != self.tokens.get(subject_id):
            return "deferred", result["open"], result["activity"], ""
        if result["open"]:
            return "complete", True, result["activity"], ""
        if not self.data["complete"]:
            return "deferred", False, result["activity"], ""
        return "complete", False, result["activity"], ""


def _delivery_index(root: Path, *, observation):
    """(attempts in open records, first unknown cause or "") after this pass's read."""
    observation.advance()
    refs = set()
    unknown = ""
    for result in observation.data["results"].values():
        if result["open"]:
            refs.update(result["matches"])
        unknown = unknown or result["unknown"]
    return refs, unknown


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
        successor_route, route_mtime, _route_reason = _route_evidence(snapshot.jobs, row.meta)
        successor_delivery, delivery_mtime = _delivery_state(root, row.meta)
        if (successor_route in ("unknown", "pending")
                or successor_delivery in ("open", "pending", "unknown")):
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


def _route_evidence(jobs: Path, meta: dict):
    """(state, activity_ns, cause): none | closed | open | pending | unknown.

    A missing outcome is the ordinary state of a valid open route. Binding and
    read failures remain unknown so callers can distinguish policy from loss of
    evidence without changing route or attempt state.
    """
    if not any(key in meta for key in ROUTE_KEYS):
        return "none", 0, "route-not-bound"
    if not any(meta.get(key) for key in ROUTE_KEYS):
        return "unknown", 0, "route-binding-empty"
    for name in ("route_file", "owner_route_file"):
        if meta.get(name):
            _track(meta[name])
    from dispatch_notice_state import bound_route, closed_outcome
    try:
        bound = bound_route(meta, jobs, observer=_route_observer)
        if bound is None:
            return "unknown", 0, "route-binding-missing"
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
        return "unknown", 0, "route-file-missing"
    except json.JSONDecodeError:
        return "unknown", 0, "route-json-invalid"
    except ValueError:
        return "unknown", 0, "route-binding-mismatch"
    except OSError:
        return "unknown", 0, "route-unreadable"
    except Exception as exc:  # noqa: BLE001 -- unreadable/conflicting evidence is unknown
        return "unknown", 0, "route-resolver-" + type(exc).__name__.lower()
    if outcome is None:
        return "open", 0, "outcome-absent"
    if outcome.get("finish_pending"):
        return "pending", 0, "finish-pending"
    try:
        mtime = os.stat(path.with_name(path.stem + ".outcome.json")).st_mtime_ns
    except OSError:
        mtime = 0
    return "closed", mtime, "closed"


def _route_state(jobs: Path, meta: dict):
    """Compatibility pair for callers that need only state and activity."""
    state, activity, _cause = _route_evidence(jobs, meta)
    return state, activity


def _stat_mtime(path, **kwargs) -> int:
    try:
        return os.stat(path, **kwargs).st_mtime_ns
    except OSError:
        return 0


def _delivery_subject(ctx, subject_id, attempt, binding=None):
    return {"id": subject_id, "legacy": binding is not None and binding.identity is None,
            "related": sorted(_candidate_related_attempts(ctx.root, ctx.snapshot, attempt)),
            "binding": ([binding.source, binding.harness, binding.worker_type, binding.key]
                        if binding is not None else None)}


def _home_delivery_subjects(ctx, keys):
    subjects = []
    for key in keys:
        try:
            home = os.open(key, _DIR_FLAGS, dir_fd=ctx.layout.workers_fd)
            try:
                found = _read_json("worker-home.json", MAX_MANIFEST_BYTES, dir_fd=home)
            finally:
                os.close(home)
            if found is None:
                continue
            manifest = found[0]
            source, harness, kind = (manifest.get(k) for k in ("source", "harness", "worker_type"))
            if (not isinstance(source, str) or not os.path.isabs(source)
                    or harness not in HARNESSES or kind not in WORKER_TYPES):
                continue
            declared = ctx.by_key.get(key, set())
            attempt = next(iter(declared)) if len(declared) == 1 else manifest.get("identity")
            if attempt is not None and (not isinstance(attempt, str) or not ATTEMPT_RE.fullmatch(attempt)):
                continue
            binding = _HomeBinding(key, attempt, manifest.get("identity"), source,
                                   harness, kind, manifest.get("schema"))
            if attempt is None:
                matched = _candidate_attempt_ids(ctx.snapshot, binding)
                if len(matched) == 1:
                    attempt = next(iter(matched))
            subjects.append(_delivery_subject(ctx, key, attempt, binding))
        except (OSError, _Unreadable):
            continue
    return subjects


class _Context:
    """Everything one examination reads that is shared by every home."""

    def __init__(self, jobs: Path, root: Path, layout: _Layout, snapshot: _Snapshot, now_ns: int):
        self.jobs, self.root, self.layout, self.snapshot, self.now_ns = jobs, root, layout, snapshot, now_ns
        self.workers_real = os.path.realpath(root / "homes" / "workers")
        self.delivery = None
        self.observation = None
        self.discovery_stamps = {}
        # Every group's discovery-time stamp; a detach seeds only its own groups.
        self.group_stamps = {}
        self.delivery_activity: dict[str, int] = {}
        self.delivery_records: dict[str, dict[str, tuple]] = {}
        self.delivery_unknown_global = ""
        self.delivery_groups: set[str] = set()
        self.candidate_delivery_baseline: _CandidateDeliveryBaseline | None = None
        # The one look taken just after the final reference admission was won.
        self.admission_scan = _NO_SCAN
        self.admission_reason: str | None = None
        self.candidate_binding: _HomeBinding | None = None
        self.delivery_subject_id = None
        self.by_key: dict[str, set[str]] = {}
        self.legacy: dict[tuple, dict[str, set[str]]] = {}
        self.legacy_scan_unknown = False
        self.legacy_dirs: dict[Path, object] = {}
        self.home_process_scan = None

    def configure_delivery(self, state, subjects, deadline, *, state_budget=MAX_STATE_BYTES // 2):
        # Keep a pending cohort until its traversal completes, even while the
        # ordinary artifact cursor visits other candidates. Shrink oversized
        # cohorts automatically rather than persisting history or dropping data.
        saved = state.get("delivery_observation")
        if isinstance(saved, dict) and saved.get("started") and not saved.get("complete"):
            wanted = saved.get("subjects", [])
            available = {s["id"]: s for s in subjects}
            if (isinstance(wanted, list) and wanted
                    and all(isinstance(key, str) and key in available for key in wanted)):
                subjects = [available[key] for key in wanted]
        subjects = list(subjects)
        while subjects:
            observation = _DeliveryObservation(self, subjects, saved, deadline)
            observation.data["subjects"] = [s["id"] for s in subjects]
            observation.state_budget = state_budget
            if len(json.dumps(observation.data, separators=(",", ":")).encode()) <= state_budget:
                self.observation = observation
                state["delivery_observation"] = observation.data
                if observation.data.get("started"):
                    self.delivery_index()
                return
            subjects = subjects[:len(subjects) // 2]
        self.observation = None
        state.pop("delivery_observation", None)

    def delivery_index(self):
        if self.delivery is None:
            if self.observation is None:
                return set(), ""
            self.delivery, self.delivery_unknown_global = _delivery_index(
                self.root, observation=self.observation)
        return self.delivery, self.delivery_unknown_global

    def delivery_observation(self, attempt=None, binding=None):
        binding = binding or self.candidate_binding
        subject_id = self.delivery_subject_id or (binding.key if binding is not None else attempt)
        if self.observation is None:
            subject = _delivery_subject(self, subject_id, attempt, binding)
            self.configure_delivery({}, [subject], time.monotonic() + EXAMINE_BUDGET_SECONDS)
        self.delivery_index()
        if self.observation is None:
            return "deferred", False, 0, ""
        result = self.observation.result(subject_id)
        # A verdict read now already includes whatever a refresh recorded.
        self.observation.fresh_related.discard(subject_id)
        if attempt:
            self.delivery_activity[attempt] = result[2]
        return result

    def legacy_matches(self, source, harness, worker_type):
        cache_key = (source, harness, worker_type)
        if cache_key in self.legacy:
            return self.legacy[cache_key]
        table = {}
        from worker_runtime_home import _builder
        attempts = set(self.snapshot.rows)
        if self.observation is not None:
            self.delivery_index()
            for result in self.observation.data["results"].values():
                attempts.update(result["matches"])
                self.legacy_scan_unknown |= bool(result["unknown"])
        for attempt in attempts:
            key = _builder.worker_home_key(source, harness, worker_type, attempt)
            table.setdefault(key, set()).add(attempt)
        self.legacy[cache_key] = table
        return table

    def home_process_evidence(self, key: str) -> tuple[str, str]:
        """live | absent | unknown for a process that can address this HOME."""
        if self.home_process_scan is None:
            named: set[str] = set()
            linked: set[str] = set()
            reason = ""
            try:
                entries = tuple(Path("/proc").iterdir())
            except OSError:
                entries, reason = (), "process-table-unavailable"
            if len(entries) > MAX_HOME_SCAN_PIDS:
                entries, reason = entries[:MAX_HOME_SCAN_PIDS], "process-table-over-limit"
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != os.geteuid():
                        continue
                    raw = (entry / "stat").read_text(encoding="ascii")
                    tail = raw[raw.rfind(")") + 2:].split()
                    if len(tail) <= 19:
                        reason = reason or "process-identity-unverifiable"
                        continue
                    if tail[0] == "Z":
                        continue
                    start_time = tail[19]
                    environ = (entry / "environ").read_bytes()
                    for item in environ.split(b"\0"):
                        name, sep, value = item.partition(b"=")
                        if sep and name in (b"HEARTING_WORKER_HOME", b"CODEX_HOME",
                                            b"CLAUDE_CONFIG_DIR", b"OPENCODE_CONFIG_DIR",
                                            b"HOME") and value:
                            named.add(os.fsdecode(value))
                    cmdline = (entry / "cmdline").read_bytes()
                    named.update(os.fsdecode(item) for item in cmdline.split(b"\0") if item)
                    linked.add(os.readlink(entry / "cwd"))
                    fd_dir = entry / "fd"
                    fds = tuple(fd_dir.iterdir())
                    if len(fds) > MAX_HOME_SCAN_FDS:
                        reason = reason or "process-fd-over-limit"
                    else:
                        for fd in fds:
                            try:
                                linked.add(os.readlink(fd))
                            except FileNotFoundError:
                                continue
                    after = (entry / "stat").read_text(encoding="ascii")
                    after_tail = after[after.rfind(")") + 2:].split()
                    if len(after_tail) <= 19 or after_tail[19] != start_time:
                        reason = reason or "process-identity-changed"
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    if exc.errno in (errno.ESRCH, errno.ENOENT):
                        continue
                    reason = reason or "process-reference-unreadable"
            # cwd and fd links already name canonical paths; only values a process was
            # handed can reach a HOME through a symlink. Resolve those once per pass --
            # a walk per open file per home outlasts the examine budget on network storage.
            refs: set[str] = set()
            for value in linked | named:
                clean = value.removesuffix(" (deleted)")
                if os.path.isabs(clean):
                    refs.add(os.path.realpath(clean) if value in named else clean)
            self.home_process_scan = (refs, reason)
        refs, reason = self.home_process_scan
        home = os.path.realpath(self.workers_real + os.sep + key)
        prefix = home + os.sep
        if any(ref == home or ref.startswith(prefix) for ref in refs):
            return "live", "home-process-reference"
        from dispatch_contract import _current_observer_is_host_like
        if not _current_observer_is_host_like():
            return "unknown", "process-namespace-unverifiable"
        if reason:
            return "unknown", reason
        return "absent", "process-home-references-absent"


def _registry_projection(snapshot: _Snapshot, key: str, attempt: str | None,
                         by_key: dict[str, set[str]],
                         binding: _HomeBinding | None = None) -> tuple:
    row = snapshot.rows.get(attempt) if attempt else None
    mapped = tuple(sorted(_candidate_attempt_ids(snapshot, binding))) if binding else ()
    if row is None:
        return (None, (), (), (), tuple(sorted(by_key.get(key, ()))), mapped)
    related_rows = set(snapshot.open_refs.get(attempt, ()))
    children = set(snapshot.intents.get(attempt, ()))
    log_file = row.meta.get("log_file", "")
    aliases = snapshot.log_refs.get(log_file, {}) if log_file else {}
    return (
        row.raw,
        tuple(sorted((identity, snapshot.rows[identity].raw)
                     for identity in related_rows if identity in snapshot.rows)),
        tuple(sorted((identity, snapshot.rows[identity].raw)
                     for identity in children if identity in snapshot.rows)),
        tuple(sorted((identity, alias.raw) for identity, alias in aliases.items())),
        tuple(sorted(by_key.get(key, ()))),
        mapped,
    )


def _registry_delta_is_candidate_change(snapshot: _Snapshot, jobs: Path, key: str,
                                        attempt: str | None, workers_real: str,
                                        by_key: dict[str, set[str]],
                                        deadline: float,
                                        binding: _HomeBinding | None = None) -> bool | None:
    """Inspect only bytes appended after the outside-lock registry snapshot.

    ``True`` means a candidate fact changed, ``False`` means only unrelated
    complete rows were appended, and ``None`` means the delta was not safely
    interpretable in the bounded lock window.
    """
    before = snapshot.registry_snapshot
    if before is None:
        return None
    try:
        fd = os.open(jobs, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        current = os.fstat(fd)
        dev, ino, size, mtime_ns, ctime_ns = before
        if (current.st_dev != dev or current.st_ino != ino or current.st_size < size):
            return None
        row = snapshot.rows.get(attempt) if attempt else None
        related = ({attempt, *snapshot.intents.get(attempt, ())}
                   if attempt else set())
        if attempt:
            related.update(snapshot.open_refs.get(attempt, ()))
        if binding is not None:
            related.update(_candidate_attempt_ids(snapshot, binding))
        related.update(snapshot.log_refs.get(row.meta.get("log_file", ""), {}) if row else ())
        related.update(by_key.get(key, ()))
        if len(related) > 1024:
            return None
        for identity in related:
            old = snapshot.rows.get(identity)
            offset = snapshot.row_offsets.get(identity)
            if old is None or offset is None:
                continue
            expected = old.raw.encode("utf-8") + b"\n"
            observed = os.pread(fd, len(expected), offset)
            if observed != expected:
                return True
        if current.st_size == size:
            return False if (current.st_mtime_ns == mtime_ns
                             and current.st_ctime_ns == ctime_ns) else None
        delta_size = current.st_size - size
        if delta_size > 4 * 1024 * 1024 or time.monotonic() >= deadline:
            return None
        os.lseek(fd, size, os.SEEK_SET)
        chunks, remaining = [], delta_size
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
            if time.monotonic() >= deadline:
                return None
        data = b"".join(chunks)
        if data and not data.endswith(b"\n"):
            return None
        candidate_log = row.meta.get("log_file", "") if row else ""
        for blob in data.splitlines(keepends=True):
            if time.monotonic() >= deadline:
                return None
            fields = blob.decode("utf-8", "replace").rstrip("\n").split("\t")
            if len(fields) != 6:
                continue
            metadata = fields[5]
            identity = ATTEMPT_ID_RE.search(metadata)
            if identity and identity.group(1) in related:
                return True
            if (binding is not None
                    and (identity and _attempt_maps_to_binding(identity.group(1), binding)
                         or _attempt_maps_to_binding(fields[4], binding))):
                return True
            named = set(ATTEMPT_RE.findall(metadata))
            if fields[1] in OPEN and named.intersection(related):
                return True
            try:
                parsed = _parse_metadata(metadata)
            except Exception:  # noqa: BLE001 -- malformed metadata cannot prove irrelevance
                if any(item in metadata for item in related):
                    return None
                parsed = {}
            if (parsed.get("delivery_intent") == "1"
                    and parsed.get("parent_attempt_id") in related):
                return True
            raw_home = parsed.get("runtime_home", "")
            if raw_home and _declared_home(jobs.parent, workers_real, raw_home) == key:
                return True
            if candidate_log and parsed.get("log_file") == candidate_log:
                return True
        return False
    except OSError:
        return None
    finally:
        os.close(fd)


def _parse_metadata(value: str) -> dict[str, str]:
    from dispatch_contract import parse_registry_metadata
    return parse_registry_metadata(value)


def _delivery_record_relation(path: Path, related: set[str] | frozenset[str],
                              binding: _HomeBinding | None = None, *,
                              dir_fd: int | None = None) -> str:
    """related | unrelated | unknown for a changed delivery record."""
    from dispatch_pending_delivery import PendingDeliveryError, record_references
    for attempt in range(2):
        try:
            read_path = path.name if dir_fd is not None else str(path)
            found = _read_json(read_path, MAX_DELIVERY_BYTES, dir_fd=dir_fd)
            if found is None:
                return "unrelated"
            identities = record_references(found[0], group=path.parent.name, delivery_id=path.name[:-5])
            if related.intersection(identities):
                return "related"
            if binding is not None and any(
                    _attempt_maps_to_binding(item, binding)
                    for item in identities):
                return "related"
            return "unrelated"
        except (_Unreadable, PendingDeliveryError, TypeError, ValueError):
            if attempt == 0:
                time.sleep(0.001)
    return "unknown"


def _bounded_directory_entries(path: Path, deadline: float) -> list | None:
    entries = []
    try:
        with os.scandir(path) as listing:
            for entry in listing:
                if (len(entries) >= MAX_DIRECTORY_ENTRIES
                        or time.monotonic() >= deadline):
                    return None
                entries.append(entry)
    except OSError:
        return None
    return entries


def _late_delivery_change(ctx: _Context, attempt: str | None, deadline: float,
                           binding: _HomeBinding | None = None) -> str | None:
    """Return changed/unknown when candidate evidence moved after discovery.

    The global JSON index is prepared outside the jobs lock. At the final
    boundary we compare its file generations, then read only changed records
    and memberships whose directories changed.
    """
    binding = binding or ctx.candidate_binding
    if (ctx.observation is not None and ctx.observation.resumed
            or len(ctx.discovery_stamps) > 32 or len(ctx.group_stamps) > 32):
        return _late_candidate_delivery_change(ctx, attempt, deadline, binding)
    base = ctx.root / "pending-delivery"
    related = _candidate_related_attempts(ctx.root, ctx.snapshot, attempt)
    candidate_groups = _candidate_recipient_groups(ctx.root, ctx.snapshot, attempt)
    baseline_files = {path for path in ctx.discovery_stamps
                      if isinstance(path, Path) and path.suffix == ".json"}
    group_by_file = {path: path.parent.name for path in baseline_files}
    candidate_group_names = {path.name for path in candidate_groups}

    # An already-known candidate record is always checked, even if its parent
    # directory did not change (writers replace record files in place).
    for identity in related:
        for raw_path, fact in ctx.delivery_records.get(identity, {}).items():
            if time.monotonic() >= deadline:
                return "unknown"
            path = Path(raw_path)
            if _stamp(path) != fact:
                return "changed"

    # A file rewrite can turn an unrelated record into a reference without
    # changing its directory. Stat the previously discovered bounded file set,
    # and parse only changed records.
    for path in baseline_files:
        if time.monotonic() >= deadline:
            return "unknown"
        before = ctx.discovery_stamps.get(path)
        after = _stamp(path)
        if after == before:
            continue
        if after is None:
            if any(str(path) in ctx.delivery_records.get(identity, {}) for identity in related):
                return "changed"
            continue
        relation = _delivery_record_relation(path, related, binding)
        if relation == "related":
            return "changed"
        if relation == "unknown" and group_by_file.get(path) in candidate_group_names:
            return "unknown"

    # New recipient groups and new files in known groups are identified by
    # directory membership changes. Unrelated records are read and ignored.
    try:
        base_after = _stamp(base)
        base_before = ctx.discovery_stamps.get(base)
        groups_now = _bounded_directory_entries(base, deadline) if base_after is not None else []
        if groups_now is None:
            return "unknown"
    except OSError:
        return "unknown"
    if base_after != base_before:
        for group in groups_now:
            if group.name in ctx.delivery_groups:
                continue
            if time.monotonic() >= deadline:
                return "unknown"
            if group.is_symlink() or not group.is_dir(follow_symlinks=False):
                return "unknown"
            group_path = Path(group.path)
            group_stamp = _stamp(group_path)
            try:
                entries = _bounded_directory_entries(group_path, deadline)
                if entries is None:
                    return "unknown"
            except OSError:
                return "unknown"
            for entry in entries:
                if time.monotonic() >= deadline:
                    return "unknown"
                if not entry.name.endswith(".json"):
                    continue
                record_path = Path(entry.path)
                record_before = _stamp(record_path)
                relation = _delivery_record_relation(record_path, related, binding)
                record_after = _stamp(record_path)
                if (time.monotonic() >= deadline or record_before is None
                        or record_before != record_after):
                    return "unknown"
                # Keep the generation for the payload observation above. A
                # later in-place rewrite must be visible even though the
                # recipient directory's membership stamp stays unchanged.
                ctx.discovery_stamps[record_path] = record_before
                if relation == "related":
                    return "changed"
                if relation == "unknown":
                    return "unknown"
            if _stamp(group_path) != group_stamp:
                return "unknown"
            ctx.group_stamps[group_path] = group_stamp
        if _stamp(base) != base_after:
            return "unknown"
        ctx.discovery_stamps[base] = base_after
        ctx.delivery_groups = {group.name for group in groups_now}

    for group_path, before in ctx.group_stamps.items():
        if time.monotonic() >= deadline:
            return "unknown"
        after = _stamp(group_path)
        if after == before:
            continue
        try:
            entries = _bounded_directory_entries(group_path, deadline)
            if entries is None:
                return "unknown"
        except FileNotFoundError:
            continue
        except OSError:
            return "unknown"
        for entry in entries:
            if time.monotonic() >= deadline:
                return "unknown"
            path = Path(entry.path)
            if not entry.name.endswith(".json") or path in baseline_files:
                continue
            relation = _delivery_record_relation(path, related, binding)
            if relation == "related":
                return "changed"
            if relation == "unknown" and group_path.name in candidate_group_names:
                return "unknown"
    return None


@dataclass(frozen=True)
class _CandidateDeliveryBaseline:
    attempt: str | None
    binding: _HomeBinding | None
    related: frozenset[str]
    token: str | None
    subject_id: str
    related_file_stamps: tuple
    baseline_files: tuple
    base: Path


def _prepare_candidate_delivery_baseline(ctx, attempt, deadline):
    binding = ctx.candidate_binding
    if ctx.observation is None or time.monotonic() >= deadline:
        return None
    subject_id = ctx.delivery_subject_id or (binding.key if binding else attempt)
    result = ctx.observation.data["results"].get(subject_id)
    if result is None or ctx.observation.result(subject_id)[:2] != ("complete", False):
        return None
    subject = next(s for s in ctx.observation.subjects if s["id"] == subject_id)
    related = set(subject["related"]) | set(result["matches"])
    # A small in-command backstop preserves descriptor-bound legacy checks of an
    # in-place rewrite. Publications are rechecked from the observation mark
    # instead (`_scan_published`); history file stamps are never persisted and
    # never walked again at detach.
    small_history = not ctx.observation.resumed and len(ctx.discovery_stamps) <= 33
    small_files = (tuple((p, v) for p, v in ctx.discovery_stamps.items() if p.suffix == ".json")
                   if small_history else ())
    direct = tuple((Path(path), fact) for identity in related
                   for path, fact in ctx.delivery_records.get(identity, {}).items())[:8]
    return _CandidateDeliveryBaseline(
        attempt, binding, frozenset(related), result["token"], subject_id,
        direct, small_files, ctx.root / "pending-delivery")


def _scan_published(base, low, deadline, limit, known, visit, *, stat_limit=None):
    """Visit each `.json` that may have been published at or after `low`.

    A publication lands by rename, so it leaves its recipient group's mtime and
    its own ctime at or after the moment it happened. Only groups whose mtime
    changed contribute entries newer than `low`; a group that merely had its
    ctime changed (a replaced or re-moded directory) contributes all of its
    entries. Costs one stat per recipient group and per entry of a changed
    group, with no cap on the number of groups. `limit` bounds the entries that
    pass the ctime filter and are not already known, i.e. the ones `visit` is
    asked to read, so churn that only unlinks or leaves old entries in a changed
    group never exhausts it. The stats that precede that filter are bounded by
    the deadline, or by `stat_limit` where the caller holds something while it
    looks (the final admission).
    `visit(group_fd, group, name, stamp)` returns None or a result to stop on.
    Returns None, "unknown", "deferred" or a visit result.
    """
    try:
        base_fd = os.open(base, _DIR_FLAGS)
    except FileNotFoundError:
        return None
    except OSError:
        return "unknown"
    try:
        changed = []
        with os.scandir(base_fd) as listing:
            for index, entry in enumerate(listing):
                if index & 1023 == 1023 and time.monotonic() >= deadline:
                    return "deferred"
                name = entry.name
                if len(name) != 64 or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if info.st_mtime_ns >= low and SHA256_RE.fullmatch(name):
                    changed.append((name, True))
                elif info.st_ctime_ns >= low and SHA256_RE.fullmatch(name):
                    changed.append((name, False))
        budget, stats = limit, stat_limit
        for group, membership in changed:
            if time.monotonic() >= deadline:
                return "deferred"
            try:
                group_fd = os.open(group, _DIR_FLAGS, dir_fd=base_fd)
            except FileNotFoundError:
                continue
            try:
                with os.scandir(group_fd) as listing:
                    for entry in listing:
                        if not entry.name.endswith(".json"):
                            continue
                        if stats is not None:
                            stats -= 1
                            if stats < 0:
                                return "deferred"
                        if time.monotonic() >= deadline:
                            return "deferred"
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if membership and info.st_ctime_ns < low:
                            continue
                        stamp = _stamp_from_stat(info)
                        if known.get(base / group / entry.name) == stamp:
                            continue
                        budget -= 1
                        if budget < 0:
                            return "deferred"
                        stopped = visit(group_fd, group, entry.name, stamp)
                        if stopped is not None:
                            return stopped
            except FileNotFoundError:
                continue
            finally:
                os.close(group_fd)
    except OSError:
        return "unknown"
    finally:
        os.close(base_fd)
    return None


def _late_candidate_group_change(baseline, related, binding, known, deadline, *, mark):
    """The final admission's one look: published since `mark`, read through the scanned group."""
    reads = [0]

    def visit(group_fd, group, name, stamp):
        reads[0] += 1
        if reads[0] > ADMISSION_REREAD_LIMIT:
            return "deferred"
        relation = _delivery_record_relation(baseline.base / group / name, related, binding,
                                             dir_fd=group_fd)
        if relation == "related":
            return "changed"
        if relation == "unknown":
            return "unknown"
        return None

    # The look holds the reference admission, so its stats are capped as well.
    return _scan_published(baseline.base, mark - MARK_SLACK_NS, deadline, DELIVERY_READ_BUDGET,
                           known, visit, stat_limit=DELIVERY_READ_BUDGET)


def _admission_scan(ctx, deadline):
    """Look once, as soon as the candidate's reference admission is held.

    Every writer that can name a related attempt holds that reference for the
    whole of its publication, so after this point none can publish; before it,
    its rename already landed and is newer than the observation mark.
    """
    baseline = ctx.candidate_delivery_baseline
    mark = ctx.observation.data.get("mark") if ctx.observation is not None else None
    if baseline is None or type(mark) is not int:
        return "unknown"
    return _late_candidate_group_change(baseline, baseline.related, baseline.binding,
                                        ctx.discovery_stamps, deadline, mark=mark)


def _late_outcome(value, budget_reason):
    if value == "unknown":
        return "late-evidence-unreadable"
    return "defer:" + budget_reason if value == "deferred" else "late-evidence-changed"


def _late_candidate_delivery_change(ctx, attempt, deadline, binding=None):
    binding = binding or ctx.candidate_binding
    baseline = ctx.candidate_delivery_baseline
    if baseline is None or baseline.attempt != attempt or baseline.binding != binding:
        baseline = _prepare_candidate_delivery_baseline(ctx, attempt, deadline)
        if baseline is None:
            return "unknown"
        ctx.candidate_delivery_baseline = baseline
    if time.monotonic() >= deadline:
        return "unknown"
    subject = next(s for s in ctx.observation.subjects if s["id"] == baseline.subject_id)
    fresh = (_reference_fingerprint(ctx.root, subject["related"], deadline=deadline)
             if subject["related"] else ctx.observation._view_token())
    if fresh is None or fresh != baseline.token:
        return "changed"
    for path, before in baseline.related_file_stamps:
        if time.monotonic() >= deadline:
            return "unknown"
        if _stamp(path) != before:
            return "changed"
    for path, before in baseline.baseline_files:
        if _stamp(path) != before and _stamp(path) is not None:
            relation = _delivery_record_relation(path, baseline.related, binding)
            if relation != "unrelated":
                return "changed" if relation == "related" else "unknown"
    if ctx.admission_scan is _NO_SCAN:
        # Outside the final admission: fold what was published since the mark into
        # the cohort (a normal pinned writer publishes without advancing a token).
        refreshed = ctx.observation.refresh(deadline)
        if refreshed is not None:
            return refreshed
    elif ctx.admission_scan is not None:
        return ctx.admission_scan
    return "changed" if baseline.subject_id in ctx.observation.fresh_related else None


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
    """Return a checked binding, including an optional legacy attempt identity."""
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
    schema = manifest.get("schema")
    if (harness not in HARNESSES or worker_type not in WORKER_TYPES
            or not isinstance(source, str) or not source or not os.path.isabs(source)
            or (schema is not None and (type(schema) is not int or schema not in (1, 2)))):
        return _Verdict("unknown", "manifest-invalid")
    if (manifest.get("destination") is True
            or ("destination" in manifest and not isinstance(manifest.get("destination"), bool))):
        return _Verdict("unknown", "destination-home")
    if "key" in manifest and manifest.get("key") != key:
        return _Verdict("unknown", "manifest-key-conflict")
    identity = manifest.get("identity")
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
        status, _open, _activity, _cause = ctx.delivery_observation(None, _HomeBinding(
            key, None, None, source, harness, worker_type, schema))
        matches = ctx.legacy_matches(source, harness, worker_type).get(key, set())
        if ctx.legacy_scan_unknown:
            return _Verdict("unknown", "legacy-ledger-unverifiable")
        if status == "deferred":
            return _Verdict("deferred", "delivery-read-deferred")
        if len(matches) > 1:
            return _Verdict("unknown", "ambiguous")
        if matches:
            attempt = next(iter(matches))
        elif schema in (None, 1):
            attempt = None
        else:
            return _Verdict("unknown", "unmapped")
    if attempt is not None:
        if not ATTEMPT_RE.fullmatch(attempt):
            return _Verdict("unknown", "attempt-invalid")
        if _builder.worker_home_key(source, harness, worker_type, attempt) != key:
            return _Verdict("unknown", "key-mismatch")
    binding = _HomeBinding(key, attempt, identity, source, harness, worker_type, schema)
    ctx.candidate_binding = binding
    row = ctx.snapshot.rows.get(attempt)
    if row is None:
        # The manifest's identity or a matching existing ledger key is enough
        # to investigate this orphan; it is not itself proof of completion or
        # permission to detach. An identity-less legacy binding instead checks
        # every path-based consumer surface and the complete process scan.
        return binding
    claimed = row.meta.get("runtime_home", "")
    if (claimed and claimed != SHARED_SERVICE_RUNTIME_HOME
            and _declared_home(ctx.root, ctx.workers_real, claimed) != key):
        return _Verdict("unknown", "runtime-home-conflict")
    return binding


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


def _protections(ctx: _Context, key: str, attempt: str, *, probe_processes: bool,
                 protect_open_route: bool = False):
    """Use the shared read-only terminal/consumer decision for this home."""
    from dispatch_retention import terminal_protection
    from dispatch_contract import attempt_process_quiescence
    from dispatch_summary import home_consumer_evidence

    return terminal_protection(
        ctx, attempt, probe_processes=probe_processes,
        delivery_state=lambda meta: _delivery_state(ctx.root, meta),
        replacement_state=lambda identity: _replacement_state(ctx.root, ctx.snapshot, identity),
        route_state=lambda meta: _route_evidence(ctx.jobs, meta),
        owner_state=home_consumer_evidence,
        lease_state=lambda meta: _lease_state(ctx.jobs, meta),
        delivery_observation=lambda identity: ctx.delivery_observation(identity),
        supervisor_activity=lambda identity: _stat_mtime(
            "supervisor-state/%s.json" % identity,
            dir_fd=ctx.layout.root_fd, follow_symlinks=False),
        process_quiescence=lambda meta: attempt_process_quiescence(meta, terminal_receipt=True),
        protect_open_route=protect_open_route,
    )


def _orphan_lease_state(jobs: Path, attempt: str) -> str:
    """Probe an attempt-derived supervisor lease when its registry row is gone."""
    from dispatch_contract import (SUPERVISOR_LEASE_KIND, SUPERVISOR_LEASE_NONCE_RE,
                                   supervisor_lease_path)
    try:
        path = supervisor_lease_path(jobs, attempt)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "none"
    except OSError:
        return "unknown"
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > 512):
            return "unknown"
        try:
            payload = os.read(fd, 513).decode("ascii")
        except (OSError, UnicodeDecodeError):
            return "unknown"
        fields = dict(line.split("=", 1) for line in payload.splitlines() if "=" in line)
        expected = (f"kind={SUPERVISOR_LEASE_KIND}\nattempt_id={attempt}\n"
                    f"nonce={fields.get('nonce', '')}\n")
        if (len(payload) > 512 or payload != expected
                or set(fields) != {"kind", "attempt_id", "nonce"}
                or fields.get("kind") != SUPERVISOR_LEASE_KIND
                or fields.get("attempt_id") != attempt
                or SUPERVISOR_LEASE_NONCE_RE.fullmatch(fields.get("nonce", "")) is None):
            return "unknown"
        return _flock_probe(str(path))
    finally:
        os.close(fd)


def _orphan_protection(ctx: _Context, binding: _HomeBinding):
    """Prove a valid no-row HOME unused through the existing evidence surfaces."""
    key, attempt, harness = binding.key, binding.attempt, binding.harness
    activity = 0
    if attempt is not None:
        if attempt in ctx.snapshot.open_refs:
            return _Verdict("keep", "open-reference")
        children = ctx.snapshot.intents.get(attempt, ())
        if len(children) > MAX_INTENT_CHILDREN:
            return _Verdict("unknown", "delivery-unknown", detail="intent-limit")
        for child in children:
            row = ctx.snapshot.rows.get(child)
            if row is None:
                return _Verdict("unknown", "delivery-unknown", detail="intent-row-missing")
            state, mtime = _delivery_state(ctx.root, row.meta)
            if state in ("open", "pending"):
                return _Verdict("keep", "delivery-" + state)
            if state == "unknown":
                return _Verdict("unknown", "delivery-unknown", detail="intent-state-unreadable")
            activity = max(activity, mtime)
    status, is_open, delivery_activity, cause = ctx.delivery_observation(attempt, binding)
    if status == "unknown":
        return _Verdict("unknown", "delivery-index-unknown", detail=cause)
    if status == "deferred":
        return _Verdict("deferred", "delivery-read-deferred")
    if is_open:
        return _Verdict("keep", "delivery-open")
    activity = max(activity, delivery_activity)
    if attempt is not None:
        state, mtime = _replacement_state(ctx.root, ctx.snapshot, attempt)
        if state == "unresolved":
            return _Verdict("keep", "replacement-unresolved")
        if state == "unknown":
            return _Verdict("unknown", "replacement-unknown", detail="replacement-record-unreadable")
        activity = max(activity, mtime, ctx.delivery_activity.get(attempt, 0))
        from dispatch_summary import home_consumer_for_identity
        summary, summary_reason = home_consumer_for_identity(
            attempt, harness, str(ctx.workers_real + os.sep + key))
        if summary == "live":
            return _Verdict("keep", "summary-live")
        if summary == "unknown":
            return _Verdict("unknown", "summary-owner-unknown", detail=summary_reason)
        lease = _orphan_lease_state(ctx.jobs, attempt)
        if lease == "held":
            return _Verdict("keep", "lease-held")
        if lease == "unknown":
            return _Verdict("unknown", "lease-unknown", detail="orphan-lease-unverifiable")
    else:
        matches = ctx.legacy_matches(binding.source, harness, binding.worker_type).get(key, set())
        if ctx.legacy_scan_unknown:
            return _Verdict("unknown", "legacy-ledger-unverifiable")
        if matches:
            return _Verdict("unknown", "unmapped", detail="identity-appeared")
    process, process_reason = ctx.home_process_evidence(key)
    if process == "live":
        return _Verdict("keep", "not-quiescent")
    if process == "unknown":
        return _Verdict("unknown", "quiescence-unverifiable", detail=process_reason)
    return _Verdict("eligible", activity_ns=activity, attempt=attempt, raw="")


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


_FREED: contextvars.ContextVar = contextvars.ContextVar("retention_freed_bytes", default=None)


def _note_freed(info: os.stat_result) -> None:
    """Add the blocks a removal frees to the running pass (a file with other links frees none)."""
    box = _FREED.get()
    if box is not None and (stat.S_ISDIR(info.st_mode) or info.st_nlink == 1):
        box[0] += info.st_blocks * 512


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
            _note_freed(st)
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
        _note_freed(st)
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
        try:
            info = os.stat(name, dir_fd=layout.retiring_fd, follow_symlinks=False)
            if (_identity(info) != expected or info.st_uid != os.geteuid()
                    or info.st_dev != os.fstat(layout.retiring_fd).st_dev):
                return "unproven"
            _note_freed(info)
            os.unlink(name, dir_fd=layout.retiring_fd)
        except FileNotFoundError:
            pass
        except OSError:
            return "partial"
        with contextlib.suppress(OSError):
            os.unlink(name + ".journal", dir_fd=layout.retiring_fd)
        return "removed"
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
        _note_freed(opened)
        os.rmdir(name, dir_fd=layout.retiring_fd)
    except FileNotFoundError:
        pass
    except OSError:
        return "partial"
    with contextlib.suppress(OSError):
        os.unlink(name + ".journal", dir_fd=layout.retiring_fd)
    return "removed"


def _partial_keep_paths(harness: str) -> tuple[set[str], set[str]] | None:
    if harness == "claude":
        return {"projects", "worker-home.json"}, {"projects"}
    if harness == "opencode":
        base = "runtime/data/opencode"
        keep = {"worker-home.json", *(f"{base}/opencode.db{suffix}" for suffix in ("", "-wal", "-shm"))}
        return keep, set()
    return None


def _partial_raw_state(home_fd: int, harness: str) -> bool | None:
    """True when raw data is safely present, False when absent, None if ambiguous."""
    if harness == "claude":
        try:
            info = os.stat("projects", dir_fd=home_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                return None
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return None
    parent = home_fd
    owned = []
    try:
        for name in ("runtime", "data", "opencode"):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                return None
            child = os.open(name, _DIR_FLAGS, dir_fd=parent)
            owned.append(child)
            parent = child
        try:
            info = os.stat("opencode.db", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return None
        return True
    except OSError:
        return None
    finally:
        for fd in reversed(owned):
            os.close(fd)


def _journal_and_retire(layout: _Layout, source_fd: int, name: str, source_relative: str, key: str,
                        now_ns: int, serial: int, deadline: float) -> str:
    """Detach one fd-relative home entry through the existing retiring journal."""
    try:
        before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
    except OSError:
        return "deferred"
    if before.st_dev != os.fstat(source_fd).st_dev:
        return "deferred"
    target = f"{key}.{now_ns}.{serial}"
    journal = json.dumps({"schema": SCHEMA, "key": key, "name": target,
                          "identity": _identity(before), "operation": "partial-entry",
                          "source": source_relative}, sort_keys=True).encode()
    try:
        fd = os.open(target + ".journal", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=layout.retiring_fd)
    except OSError:
        return "deferred"
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(journal)
            handle.flush()
            os.fsync(handle.fileno())
        current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if _identity(current) != _identity(before):
            os.unlink(target + ".journal", dir_fd=layout.retiring_fd)
            return "deferred"
        if stat.S_ISDIR(current.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=source_fd)
            try:
                same_mount = _mount_id(child) == _mount_id(source_fd)
            finally:
                os.close(child)
            if not same_mount:
                os.unlink(target + ".journal", dir_fd=layout.retiring_fd)
                return "deferred"
        os.rename(name, target, src_dir_fd=source_fd, dst_dir_fd=layout.retiring_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(target + ".journal", dir_fd=layout.retiring_fd)
        return "deferred"
    result = _remove_leaf(layout, target, _identity(before), deadline)
    return "removed" if result == "removed" else "retired"


def _prune_home_entries(layout: _Layout, home_fd: int, key: str, harness: str,
                        now_ns: int, deadline: float) -> str:
    """Keep only the transcript allowlist and detach each other entry separately."""
    allow = _partial_keep_paths(harness)
    if allow is None or _partial_raw_state(home_fd, harness) is not True:
        return "deferred"
    keep, opaque = allow
    serial = 0

    def visit(parent_fd, prefix="", depth=0):
        nonlocal serial
        if depth > MAX_TREE_DEPTH or time.monotonic() > deadline:
            return False
        try:
            with os.scandir(parent_fd) as listing:
                names = [entry.name for entry in listing]
        except OSError:
            return False
        for name in names:
            path = f"{prefix}/{name}" if prefix else name
            if path in keep:
                try:
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    return False
                if (path == "projects" and not stat.S_ISDIR(info.st_mode)
                        or path != "projects" and not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()):
                    return False
                continue
            if any(path.startswith(item + "/") for item in opaque):
                continue
            descendants = any(item.startswith(path + "/") for item in keep)
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                return False
            if descendants:
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                    return False
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
                except OSError:
                    return False
                try:
                    opened = os.fstat(child)
                    if (_identity(opened) != _identity(info)
                            or _mount_id(child) != _mount_id(parent_fd)
                            or not visit(child, path, depth + 1)):
                        return False
                finally:
                    os.close(child)
                continue
            serial += 1
            outcome = _journal_and_retire(layout, parent_fd, name, path, key, now_ns,
                                          serial, deadline)
            if outcome not in {"removed", "retired"}:
                return False
        return True

    return "partial-retained" if visit(home_fd) else "partial-deferred"


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
    operation, source = value.get("operation"), value.get("source")
    if operation is not None and (
            operation != "partial-entry" or not isinstance(source, str) or not source
            or source.startswith("/") or ".." in Path(source).parts or "\\" in source):
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
    """Acquire the registry lock with a brief bounded wait, never a long stall."""
    fd = os.open(str(jobs) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Unsafe("registry-lock-invalid")
        deadline = _REGISTRY_DEADLINE.get()
        local_deadline = time.monotonic() + REGISTRY_LOCK_WAIT_SECONDS
        if isinstance(deadline, (float, int)):
            local_deadline = min(local_deadline, deadline)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = local_deadline - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(0.01, remaining))
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _registry_lock_until(jobs: Path, deadline: float):
    token = _REGISTRY_DEADLINE.set(deadline)
    try:
        with _registry_lock(jobs) as admitted:
            yield admitted
    finally:
        _REGISTRY_DEADLINE.reset(token)


# destructive-ok: reason=detach one expired worker home by pinned rename; boundary=one 32-hex leaf of homes/workers into homes/worker-retention/retiring
def _detach(ctx: _Context, key: str, ident: list[int], attempt: str | None, raw: str,
            first_ns: int, deadline: float | None = None, _rechecks: int = 0,
            binding: _HomeBinding | None = None) -> str:
    """Prepare broad evidence outside the lock; recheck candidate facts at rename."""
    layout = ctx.layout
    if deadline is None:
        deadline = time.monotonic() + REMOVE_DEADLINE_SECONDS
    lock_file = os.open(str(ctx.jobs) + ".lock",
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.close(lock_file)
    binding = binding or ctx.candidate_binding
    prior_registry = _registry_projection(ctx.snapshot, key, attempt, ctx.by_key, binding)
    watched = {}
    try:
        try:
            fresh = _Snapshot(ctx.jobs)
        except _Unreadable:
            return "registry-unreadable"
        row = fresh.rows.get(attempt) if attempt else None
        if (row is None and raw) or (row is not None and not raw):
            return "row-changed"
        current = _Context(ctx.jobs, ctx.root, layout, fresh, ctx.now_ns)
        current.candidate_binding = binding
        current.observation = ctx.observation
        current.delivery = ctx.delivery
        current.delivery_records = ctx.delivery_records
        current.discovery_stamps = ctx.discovery_stamps
        current.group_stamps = ctx.group_stamps
        current.delivery_groups = ctx.delivery_groups
        current.legacy_dirs = ctx.legacy_dirs
        current.home_process_scan = ctx.home_process_scan
        token = _DEPENDENCIES.set(watched)
        try:
            current.by_key = _declared_index(fresh, ctx.root, current.workers_real)
        finally:
            _DEPENDENCIES.reset(token)
        if _registry_projection(fresh, key, attempt, current.by_key, binding) != prior_registry:
            return "late-evidence-changed"
        token = _DEPENDENCIES.set(watched)
        try:
            _candidate_recipient_groups(ctx.root, fresh, attempt)
            _track(ctx.root / "supervisor-state" / f"{attempt}.json")
            _track(ctx.root / "homes" / "workers" / key / "worker-home.json")
            try:
                home = os.open(key, _DIR_FLAGS, dir_fd=layout.workers_fd)
            except OSError:
                return "home-gone"
            try:
                rebound = _bind(current, key, home)
                found = _read_json("worker-home.json", MAX_MANIFEST_BYTES, dir_fd=home)
            finally:
                os.close(home)
            if (isinstance(rebound, _Verdict) or rebound != binding or found is None):
                return "late-binding"
            verdict = (_protections(current, key, attempt, probe_processes=False)
                       if row is not None
                       else _orphan_protection(current, binding))
            if (row is not None and row.raw != raw) or verdict.kind != "eligible":
                if verdict.kind == "keep":
                    return "protected:" + verdict.reason
                if verdict.kind in ("unknown", "deferred"):
                    return "defer:" + verdict.reason
                return "activity:row-changed"
            ended = _ended_ns(row) if row is not None else None
            if ended is not None:
                first_ns = max(first_ns, ended)
        finally:
            _DEPENDENCIES.reset(token)
    except (OSError, _Unreadable):
        return "late-evidence-unreadable"
    if not _fresh(watched):
        return "late-evidence-changed"
    # First validate the registry delta and release jobs.log.lock before the
    # broad delivery generation pass. That pass can inspect changed unrelated
    # records without extending the writers' critical section.
    rescan_registry = False
    with _registry_lock_until(ctx.jobs, deadline) as admitted:
        if admitted is False:
            return "registry-lock-busy"
        initial_deadline = min(deadline, time.monotonic() + 0.15)
        if not _fresh(watched):
            return "late-evidence-changed"
        delta = _registry_delta_is_candidate_change(
            fresh, ctx.jobs, key, attempt, current.workers_real,
            current.by_key, initial_deadline, binding)
        if delta is True:
            return "late-evidence-changed"
        if delta is None:
            rescan_registry = True
    if rescan_registry:
        if _rechecks >= 1:
            return "late-evidence-changed"
        return _detach(ctx, key, ident, attempt, raw, first_ns, deadline,
                       _rechecks + 1, binding)
    late_delivery = _late_delivery_change(current, attempt, deadline)
    if late_delivery is not None:
        return _late_outcome(late_delivery, "delivery-refresh-budget")
    # The final metadata pass reuses discovery and stays outside jobs.log.lock.
    # Its larger directory set may need the existing removal budget; the lock
    # is acquired only after related new/changed delivery evidence is rejected.
    late_delivery = _late_candidate_delivery_change(current, attempt, deadline)
    if late_delivery is not None:
        return _late_outcome(late_delivery, "delivery-refresh-budget")

    baseline = current.candidate_delivery_baseline
    if (baseline is None or baseline.attempt != attempt
            or baseline.binding != binding):
        return "late-evidence-unreadable"
    # Registry -> sorted candidate references -> retention view is the final
    # lock order. The reference admission is nonblocking for GC, and remains
    # held from the final proof through the journal and rename linearization.
    leaf_held: list[int] = []
    try:
        with _registry_lock_until(ctx.jobs, deadline) as admitted:
            if admitted is False:
                return "registry-lock-busy"
            with attempt_reference.admission(
                    ctx.root, baseline.related, blocking=False) as reference_admitted:
                if not reference_admitted:
                    return "delivery-reference-busy"
                final_deadline = min(deadline, time.monotonic() + 0.15)
                if not _fresh(watched):
                    return "late-evidence-changed"
                delta = _registry_delta_is_candidate_change(
                    fresh, ctx.jobs, key, attempt, current.workers_real,
                    current.by_key, final_deadline, binding)
                if delta is not False:
                    return "late-evidence-changed"
                with _flock(layout.ret_fd, "view.lock", fcntl.LOCK_EX | fcntl.LOCK_NB) as acquired:
                    if not acquired:
                        return "view-lock-busy"
                    # Held for the rest of this detach: look once for what was
                    # published since the refresh, before anything else.
                    # Its cost is one stat per recipient group, so it follows the
                    # group count under the removal deadline instead of a count cap.
                    current.admission_scan = _admission_scan(current, deadline)
                    if current.admission_scan is not None:
                        return _late_outcome(current.admission_scan, "delivery-reread-budget")
                    final_deadline = max(final_deadline, min(deadline, time.monotonic() + 0.05))
                    if not _legacy_inventory_fresh(current):
                        return "late-evidence-changed"
                    # Process-table observation stays outside the registry lock.
                    # Recheck only the exact existing lease and owner/provider
                    # admission records here; native provider starts share the
                    # attempt-reference lock held through this rename.
                    if row is not None:
                        lease = _lease_state(ctx.jobs, row.meta)
                        if lease in ("held", "unknown"):
                            return ("protected:" if lease == "held" else "defer:") + "lease-" + lease
                        from dispatch_summary import home_consumer_evidence
                        summary, _summary_reason = home_consumer_evidence(row.meta)
                        if summary in ("live", "unknown"):
                            return ("protected:" if summary == "live" else "defer:") + \
                                ("summary-live" if summary == "live" else "summary-owner-unknown")
                    else:
                        orphan_verdict = _orphan_protection(current, binding)
                        if orphan_verdict.kind != "eligible":
                            prefix = "protected:" if orphan_verdict.kind == "keep" else "defer:"
                            return prefix + orphan_verdict.reason
                    try:
                        home = os.open(key, _DIR_FLAGS, dir_fd=layout.workers_fd)
                    except OSError:
                        return "home-gone"
                    try:
                        rebound = _bind(current, key, home)
                    finally:
                        os.close(home)
                    if isinstance(rebound, _Verdict) or rebound != binding:
                        return "late-binding"
                    mark = _stat_mtime(key, dir_fd=layout.views_fd, follow_symlinks=False)
                    if mark and ctx.now_ns - mark < IDLE_SECONDS * 10**9:
                        return "recent-view"
                    try:
                        leaf = os.open(key, _DIR_FLAGS, dir_fd=layout.workers_fd)
                    except OSError:
                        return "home-gone"
                    leaf_held.append(leaf)
                    st = os.fstat(leaf)
                    if _identity(st) != ident:
                        return "identity-changed"
                    # A native reader or supervisor pins exactly this HOME with a
                    # shared flock; the exclusive lock stays held through the rename.
                    try:
                        fcntl.flock(leaf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return "protected:home-reader-active"
                    except OSError:
                        return "late-evidence-unreadable"
                    idle_from = max(first_ns, verdict.activity_ns, mark, st.st_mtime_ns)
                    if ctx.now_ns - idle_from < IDLE_SECONDS * 10**9:
                        return "late-activity"
                    source_mount = _mount_id(layout.workers_fd)
                    target_mount = _mount_id(layout.retiring_fd)
                    leaf_mount = _mount_id(leaf)
                    if (source_mount is None or source_mount != target_mount
                            or leaf_mount != source_mount):
                        return "mount-unproven"
                    if not _fresh(watched):
                        return "late-evidence-changed"
                    late_delivery = _late_candidate_delivery_change(
                        current, attempt, final_deadline)
                    if late_delivery is not None:
                        return _late_outcome(late_delivery, "delivery-reread-budget")
                    row = fresh.rows.get(attempt) if attempt else None
                    if (row is not None and binding.harness in {"claude", "opencode"}
                            and _retain_raw_for_main(row, first_ns, ctx.now_ns)):
                        raw_state = _partial_raw_state(leaf, binding.harness)
                        if raw_state is None:
                            return "defer:partial-retention-unsafe"
                        if raw_state:
                            pruned = _prune_home_entries(
                                layout, leaf, key, binding.harness, ctx.now_ns, deadline)
                            if pruned == "partial-retained":
                                return "partial-retained"
                            return "defer:partial-retention-unsafe"
                    name = f"{key}.{ctx.now_ns}"
                    journal = json.dumps({"schema": SCHEMA, "key": key, "name": name,
                                          "identity": ident}, sort_keys=True).encode()
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
                        os.rename(key, name, src_dir_fd=layout.workers_fd,
                                  dst_dir_fd=layout.retiring_fd)
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
    except attempt_reference.AttemptReferenceError:
        return "late-evidence-unreadable"
    finally:
        for fd in leaf_held:
            with contextlib.suppress(OSError):
                os.close(fd)


# --- carriers ------------------------------------------------------------


def _count(table: dict, reason: str) -> None:
    table[reason] = table.get(reason, 0) + 1


def _cause_bucket(value: str) -> str:
    """Bound diagnostic cardinality and strip proc identifiers and messages."""
    token = str(value or "unknown").split(":", 1)[0].lower()
    token = re.sub(r"[^a-z0-9_-]", "-", token)[:64].strip("-")
    return token or "unknown"


def _renewed_protection(ctx: _Context, key: str, attempt: str | None) -> bool:
    """Only a fresh, positive protection can restart an established clock."""
    try:
        fresh = _Snapshot(ctx.jobs)
        if attempt not in fresh.rows:
            return False
        current = _Context(ctx.jobs, ctx.root, ctx.layout, fresh, ctx.now_ns)
        current.by_key = _declared_index(fresh, ctx.root, current.workers_real)
        current.delivery = ctx.delivery
        current.observation = ctx.observation
        current.candidate_binding = ctx.candidate_binding
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


def sweep_best_effort(jobs) -> bool:
    """The carrier entry: whatever happens here never changes the caller's result.

    True only when a retention pass actually ran (not skipped, not failed).
    """
    try:
        return "skipped" not in sweep(jobs)
    except Exception:  # noqa: BLE001
        return False


def _sweep(jobs: Path, now_ns: int, force: bool, started: float) -> dict:
    root = _state_root(jobs)
    if not _symlink_safe_removal_supported():
        return {"skipped": "removal-unsupported"}
    with _Layout(root) as layout:
        with _flock(layout.ret_fd, "lock", fcntl.LOCK_EX | fcntl.LOCK_NB) as admitted:
            if not admitted:
                return {"skipped": "busy"}
            state = _load_state(layout)
            last = state.get("last_result")
            gap = BACKLOG_DUE_SECONDS if isinstance(last, dict) and last.get("more") else DUE_SECONDS
            if not force and now_ns - state["last_pass_ns"] < gap * 10**9 and state["last_pass_ns"] <= now_ns:
                return {"skipped": "not-due"}
            # The delivery-record prune runs under the retention lock and DUE
            # gate (they are its cadence), before any delivery index is built,
            # so this pass's HOME judgments see the pruned records. The HOME
            # examine budget restarts after it: the prune has its own budget.
            delivery_prune = _prune_delivery_records(root, now_ns)
            started = time.monotonic()
            return _pass(jobs, root, layout, state, now_ns, started,
                         delivery_prune=delivery_prune)


def _prune_delivery_records(root: Path, now_ns: int) -> dict:
    """Bounded SD-111 (7) prune of aged terminal delivery records.

    Removal-only and bounded (``dispatch_pending_delivery.prune_bounded``): it
    reads no jobs.log tail and no HOME manifest, so it cannot stall this
    pass. Like every retention step it never fails its carrier."""
    try:
        from dispatch_pending_delivery import prune_bounded
        return prune_bounded(root, now=now_ns / 1e9)
    except Exception as exc:  # noqa: BLE001 -- a prune bug must not escape a carrier
        return {"error": type(exc).__name__}


def _pass(jobs: Path, root: Path, layout: _Layout, state: dict, now_ns: int, started: float,
          delivery_prune: dict | None = None) -> dict:
    token = _FREED.set([0])
    try:
        return _run_pass(jobs, root, layout, state, now_ns, started, delivery_prune)
    finally:
        _FREED.reset(token)


def _run_pass(jobs: Path, root: Path, layout: _Layout, state: dict, now_ns: int, started: float,
              delivery_prune: dict | None = None) -> dict:
    result = {"delivery_prune": delivery_prune, "examined": 0, "eligible": 0, "due": 0, "removed": 0, "partial": 0,
              "ignored": 0, "detached_leftover": 0, "unproven_leftover": 0,
              "freed_bytes": 0, "kept": {}, "unknown": {}, "unknown_causes": {}, "deferred": 0}
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
    due: list[tuple[str, list[int], _Verdict, int, _HomeBinding]] = []
    if batch:
        try:
            snapshot = _Snapshot(jobs)
        except _Unreadable as exc:
            skipped = {"skipped": "registry-oversized" if str(exc) == "registry-oversized"
                       else "registry-unreadable"}
            # The returned skip shape is a fixed contract; the prune
            # observation is recorded only in the persisted last_result.
            state.update(last_pass_ns=now_ns, eligible_since=since,
                         last_result={**skipped, "delivery_prune": delivery_prune})
            _store_state(layout, state)
            return skipped
        ctx = _Context(jobs, root, layout, snapshot, now_ns)
        ctx.by_key = _declared_index(snapshot, root, ctx.workers_real)
        # Resume the previous cohort before rotating the candidate cursor.
        saved = state.get("delivery_observation")
        if isinstance(saved, dict) and saved.get("started") and not saved.get("complete"):
            pending = [s for s in saved.get("subjects", []) if isinstance(s, str)]
            batch = list(dict.fromkeys([k for k in pending if k in present] + batch))[:EXAMINE_LIMIT]
        subjects = _home_delivery_subjects(ctx, batch)
        subject_ids = {s["id"] for s in subjects}
        ctx.configure_delivery(state, subjects, started + EXAMINE_BUDGET_SECONDS)
        if ctx.observation is not None:
            cohort = {s["id"] for s in ctx.observation.subjects}
            batch = [key for key in batch if key in cohort or key not in subject_ids]
        scan = contextlib.ExitStack()
        # Examination stopped at a full removal budget. That is backlog only if
        # the budget then removed something: due homes that `_detach` refuses
        # again (renewed protection, unsafe partial retention) are not a reason
        # to come back sooner (RV-1041 R2).
        examination_cut = False
        try:
            from dispatch_contract import process_table_scan_scope
            from dispatch_summary import provider_reference_scan_scope
            scan.enter_context(process_table_scan_scope())
            scan.enter_context(provider_reference_scan_scope())
            for key in batch:
                if time.monotonic() - started > EXAMINE_BUDGET_SECONDS:
                    break
                if ctx.observation is None or key in cohort:
                    cursor = key
                result["examined"] += 1
                try:
                    verdict, ident, binding = _examine(ctx, key)
                except Exception:  # noqa: BLE001 -- one bad home must not stall every pass
                    verdict, ident, binding = _Verdict("unknown", "examine-error"), None, None
                if verdict.kind != "eligible":
                    if verdict.kind == "deferred":
                        result["deferred"] += 1
                    if verdict.kind == "keep":
                        since.pop(key, None)
                    _count(result["unknown" if verdict.kind == "unknown" else "kept"], verdict.reason)
                    if verdict.kind == "unknown" and verdict.detail:
                        causes = result["unknown_causes"].setdefault(verdict.reason, {})
                        _count(causes, _cause_bucket(verdict.detail))
                    continue
                result["eligible"] += 1
                first = since.setdefault(key, now_ns)
                candidate_row = snapshot.rows.get(verdict.attempt)
                ended = _ended_ns(candidate_row) if candidate_row is not None else None
                start = ended if ended is not None else first
                mark = _stat_mtime(key, dir_fd=layout.views_fd, follow_symlinks=False)
                idle_from = max(start, verdict.activity_ns, mark, ident[3])
                if now_ns - idle_from >= IDLE_SECONDS * 10**9:
                    due.append((key, ident[:3], verdict, start, binding))
                    # Each detach re-reads the registry, so removal is the slow
                    # step. Once the removal budget is full, stop examining:
                    # the cursor stays on the last candidate and the next pass
                    # starts there, rather than skipping a whole cycle of due
                    # homes this pass could not remove anyway.
                    if len(due) >= budget:
                        examination_cut = True
                        break
                else:
                    _count(result["kept"], "within-window")
        finally:
            scan.close()
        result["due"] = len(due)
        for key, ident, verdict, start, binding in due:
            if budget <= 0 or time.monotonic() > deadline:
                _count(result["kept"], "deferred")
                result["more"] = 1
                continue
            budget -= 1
            outcome = _detach(ctx, key, ident, verdict.attempt, verdict.raw, start,
                              deadline, binding=binding)
            if outcome == "partial-retained":
                result["partial"] += 1
                _count(result["kept"], "transcript-retained")
                continue
            if outcome == "defer:partial-retention-unsafe":
                _count(result["unknown"], "partial-retention-unsafe")
                continue
            if not outcome.startswith("detached:"):
                if (outcome.startswith("protected:") or outcome in ("home-gone", "identity-changed")
                        or (outcome == "late-evidence-changed" and
                            _renewed_protection(ctx, key, verdict.attempt))):
                    since.pop(key, None)
                _count(result["kept"], outcome)
                continue
            name = outcome.split(":", 1)[1]
            since.pop(key, None)
            if examination_cut:
                result["more"] = 1
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
    if batch and ctx.observation is not None:
        obs = ctx.observation
        result["delivery_reads"] = obs.reads
        result["delivery_entries"] = obs.visits
        result["delivery_metadata"] = obs.metadata
        obs.checkpoint()
        if len(json.dumps(obs.data).encode()) > MAX_STATE_BYTES // 2:
            state.pop("delivery_observation", None)
    result["freed_bytes"] = _FREED.get()[0]
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
    """(verdict, [dev, ino, uid, mtime_ns], binding) for one enumerated home."""
    try:
        home = os.open(key, _DIR_FLAGS, dir_fd=ctx.layout.workers_fd)
    except OSError:
        return _Verdict("keep", "home-gone"), None, None
    try:
        st = os.fstat(home)
        if st.st_uid != os.geteuid():
            return _Verdict("unknown", "foreign-owner"), None, None
        bound = _bind(ctx, key, home)
        harness = ""
        if not isinstance(bound, _Verdict):
            found = _read_json("worker-home.json", MAX_MANIFEST_BYTES, dir_fd=home)
            if found is None:
                return _Verdict("unknown", "manifest-missing"), None, None
            harness = found[0].get("harness", "")
    finally:
        os.close(home)
    if isinstance(bound, _Verdict):
        return bound, None, None
    binding = bound
    attempt = binding.attempt
    verdict = (_protections(ctx, key, attempt, probe_processes=True)
               if attempt is not None and attempt in ctx.snapshot.rows
               else _orphan_protection(ctx, binding))
    return verdict, _identity(st) + [st.st_mtime_ns], binding


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
