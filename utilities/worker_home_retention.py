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

import contextlib
import contextvars
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

IDLE_SECONDS = 3600
DUE_SECONDS = 300
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
MAX_DELIVERY_RECORDS = 4096
MAX_DEPENDENCY_STAMPS = MAX_DELIVERY_RECORDS * 8
MAX_HOME_SCAN_PIDS = 8192
MAX_HOME_SCAN_FDS = 2048
MAX_TREE_DEPTH = 128
REGISTRY_LOCK_WAIT_SECONDS = 0.100

KEY_RE = re.compile(r"[0-9a-f]{32}")
ATTEMPT_RE = attempt_reference.ATTEMPT_RE
NOTICE_RE = re.compile(r"notice-[0-9a-f]{32}")
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


def home_reference_relevant(root, attempt_ids) -> bool | None:
    """Whether an incoming attempt reference can name an existing worker HOME.

    This bounded path lookup runs before delivery record admission. Returning
    ``None`` means the relation could not be ruled out safely.
    """
    attempts = tuple(item for item in attempt_ids if ATTEMPT_RE.fullmatch(item or ""))
    if not attempts:
        return False
    jobs = Path(root).resolve() / "jobs.log"
    try:
        fd = os.open(jobs, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        fd = None
    if fd is not None:
        try:
            before = os.fstat(fd)
            if stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid():
                offset = max(0, before.st_size - 4 * 1024 * 1024)
                raw = os.pread(fd, before.st_size - offset, offset)
                after = os.fstat(fd)
                if (len(raw) == before.st_size - offset
                        and (before.st_dev, before.st_ino, before.st_size) ==
                            (after.st_dev, after.st_ino, after.st_size)):
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
                    if set(attempts).issubset(found):
                        return False
        except OSError:
            pass
        finally:
            os.close(fd)
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
        with os.scandir(workers_fd) as listing:
            entries = []
            for entry in listing:
                if len(entries) >= MAX_DELIVERY_RECORDS:
                    return None
                if KEY_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    entries.append(entry.name)
        from worker_runtime_home import _builder
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
                for attempt in attempts:
                    if identity is not None and identity != attempt:
                        continue
                    if _builder.worker_home_key(source, harness, worker_type, attempt) == name:
                        return True
            except _Unreadable:
                return None
            finally:
                os.close(home)
        return False
    except OSError:
        return None
    finally:
        for fd in reversed(directory_fds):
            os.close(fd)


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


def _delivery_index(root: Path, *, observer=None, activity=None, records=None,
                    groups_seen=None):
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
            if groups_seen is not None:
                groups_seen.add(group.name)
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
            if records is not None:
                digest = hashlib.sha256(json.dumps(
                    value, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                fact = (str(Path(entry.path)), digest, mtime, _stamp(entry.path))
                for item in named:
                    records.setdefault(item, {})[fact[0]] = fact[1:]
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
        self.delivery_records: dict[str, dict[str, tuple]] = {}
        self.delivery_unknown_global = False
        self.delivery_groups: set[str] = set()
        self.candidate_delivery_baseline: _CandidateDeliveryBaseline | None = None
        self.candidate_binding: _HomeBinding | None = None
        self.by_key: dict[str, set[str]] = {}
        self.legacy: dict[tuple, dict[str, set[str]]] = {}
        self.legacy_scan_unknown = False
        self.legacy_dirs: dict[Path, object] = {}
        self.home_process_scan = None

    def delivery_index(self):
        if self.delivery is None:
            def observe(kind, path):
                table = self.group_stamps if kind == "group" else self.discovery_stamps
                table[path] = _stamp(path)
            self.delivery, self.delivery_unknown_global = _delivery_index(
                self.root, observer=observe, activity=self.delivery_activity,
                records=self.delivery_records, groups_seen=self.delivery_groups)
        return self.delivery, self.delivery_unknown_global

    def delivery_projection(self, attempt: str | None,
                            binding: _HomeBinding | None = None) -> tuple:
        related = _candidate_related_attempts(self.root, self.snapshot, attempt)
        binding = binding or self.candidate_binding
        if binding is not None:
            related.update(identity for identity in self.delivery_records
                           if _attempt_maps_to_binding(identity, binding))
        return tuple(sorted(
            (identity, path, *fact)
            for identity in related
            for path, fact in self.delivery_records.get(identity, {}).items()
        ))

    def legacy_matches(self, source: str, harness: str, worker_type: str) -> dict[str, set[str]]:
        table = self.legacy.get((source, harness, worker_type))
        if table is None:
            from worker_runtime_home import _builder
            table = {}
            # Append-only delivery and recovery records can outlive their
            # registry row. They retain the exact attempt id needed to verify a
            # legacy key in the same forward direction used by the builder.
            _refs, delivery_unknown = self.delivery_index()
            self.legacy_scan_unknown |= delivery_unknown
            attempts = set(self.snapshot.rows) | set(self.delivery_records)
            directories = [
                (self.root / "automatic-replacements" / "by-source", "attempt-record"),
                (self.root / "recovery-attention" / "by-source", "attempt-record"),
                (self.root / "supervisor-state", "supervisor-state"),
            ]
            try:
                import dispatch_summary
                state_root = dispatch_summary._state_root()
                directories.extend((
                    (dispatch_summary.owner_root() / harness, "summary-owner"),
                    (state_root / harness, "summary-provider"),
                ))
            except (OSError, RuntimeError, ValueError):
                self.legacy_scan_unknown = True

            visited = 0
            for directory, kind in directories:
                directory = Path(directory)
                self.legacy_dirs[directory] = _stamp(directory)
                try:
                    parent = os.stat(directory, follow_symlinks=False)
                    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
                        self.legacy_scan_unknown = True
                        continue
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            visited += 1
                            if visited > MAX_DELIVERY_RECORDS * 5:
                                self.legacy_scan_unknown = True
                                break
                            attempt = None
                            relevant = False
                            if kind == "attempt-record" and entry.name.endswith(".json"):
                                relevant, attempt = True, entry.name[:-5]
                            elif kind == "supervisor-state":
                                suffix = ".lease" if entry.name.endswith(".lease") else ".json"
                                if entry.name.endswith(suffix):
                                    relevant, attempt = True, entry.name[:-len(suffix)]
                            elif kind == "summary-owner" and entry.name.endswith(".json"):
                                relevant, attempt = True, entry.name[:-5]
                            elif kind == "summary-provider" and entry.name.startswith(".lock-dispatch-"):
                                relevant, attempt = True, entry.name[len(".lock-dispatch-"):]
                            if not relevant:
                                continue
                            if entry.is_symlink():
                                self.legacy_scan_unknown = True
                                continue
                            if kind == "summary-provider":
                                if not entry.is_dir(follow_symlinks=False):
                                    self.legacy_scan_unknown = True
                                    continue
                            elif not entry.is_file(follow_symlinks=False):
                                self.legacy_scan_unknown = True
                                continue
                            if not ATTEMPT_RE.fullmatch(attempt or ""):
                                self.legacy_scan_unknown = True
                                continue
                            attempts.add(attempt)
                            if len(attempts) > MAX_DELIVERY_RECORDS * 2:
                                self.legacy_scan_unknown = True
                                break
                except FileNotFoundError:
                    continue
                except OSError:
                    self.legacy_scan_unknown = True
            for attempt in attempts:
                key = _builder.worker_home_key(source, harness, worker_type, attempt)
                table.setdefault(key, set()).add(attempt)
            self.legacy[(source, harness, worker_type)] = table
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
    from dispatch_pending_delivery import PendingDeliveryError, _validate_record
    for attempt in range(2):
        try:
            read_path = path.name if dir_fd is not None else str(path)
            found = _read_json(read_path, MAX_DELIVERY_BYTES, dir_fd=dir_fd)
            if found is None:
                return "unrelated"
            value = found[0]
            _validate_record(value)
            if (value.get("delivery_id") != path.name[:-5]
                    or value.get("recipient_digest") != path.parent.name):
                return "unknown"
            ids = value.get("attempt_ids")
            parent = value.get("parent_attempt_id")
            if (not isinstance(ids, list) or not isinstance(parent, str)
                    or not all(isinstance(item, str)
                               and (ATTEMPT_RE.fullmatch(item) or NOTICE_RE.fullmatch(item))
                               for item in ids)):
                return "unknown"
            identities = (item for item in (*ids, parent) if ATTEMPT_RE.fullmatch(item))
            if related.intersection(identities):
                return "related"
            if binding is not None and any(
                    _attempt_maps_to_binding(item, binding)
                    for item in (*ids, parent) if ATTEMPT_RE.fullmatch(item)):
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
                if (len(entries) >= MAX_DELIVERY_RECORDS
                        or time.monotonic() >= deadline):
                    return None
                entries.append(entry)
    except OSError:
        return None
    return entries


def _bounded_directory_entries_fd(dir_fd: int, deadline: float) -> list | None:
    entries = []
    try:
        with os.scandir(dir_fd) as listing:
            for entry in listing:
                if (len(entries) >= MAX_DELIVERY_RECORDS
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
            if _stamp(path) != fact[-1]:
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
        groups_now = list(os.scandir(base)) if base_after is not None else []
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
                entries = list(os.scandir(group_path))
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
            entries = list(os.scandir(group_path))
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
    related_file_stamps: tuple[tuple[Path, object], ...]
    baseline_files: tuple[tuple[Path, object], ...]
    baseline_file_paths: frozenset[Path]
    group_stamps: tuple[tuple[Path, object], ...]
    group_names: frozenset[str]
    base: Path
    base_stamp: object


def _late_candidate_group_change(baseline: _CandidateDeliveryBaseline,
                                 related: frozenset[str],
                                 binding: _HomeBinding | None,
                                 baseline_files: frozenset[Path],
                                 deadline: float) -> str | None:
    """Freshly inspect changed groups relative to one pinned delivery directory."""
    if not baseline.group_stamps:
        return None
    try:
        base_fd = os.open(baseline.base, _DIR_FLAGS)
    except FileNotFoundError:
        return None
    except OSError:
        return "unknown"
    try:
        opened_base = _stamp_from_stat(os.fstat(base_fd))
        path_base = _stamp(baseline.base)
        if (path_base is None or not stat.S_ISDIR(path_base[2])
                or path_base[:4] != opened_base[:4]):
            return "unknown"

        for group, before in baseline.group_stamps:
            if time.monotonic() >= deadline:
                return "unknown"
            after = _stamp_at(base_fd, group.name)
            if after == before or after is None:
                continue
            if not stat.S_ISDIR(after[2]):
                return "unknown"
            try:
                group_fd = os.open(group.name, _DIR_FLAGS, dir_fd=base_fd)
            except OSError:
                return "unknown"
            try:
                if _stamp_from_stat(os.fstat(group_fd)) != after:
                    return "unknown"
                entries = _bounded_directory_entries_fd(group_fd, deadline)
                if entries is None or _stamp_at(base_fd, group.name) != after:
                    return "unknown"
                for entry in entries:
                    if time.monotonic() >= deadline:
                        return "unknown"
                    path = group / entry.name
                    if not entry.name.endswith(".json") or path in baseline_files:
                        continue
                    relation = _delivery_record_relation(
                        path, related, binding, dir_fd=group_fd)
                    if relation == "related":
                        return "changed"
                    if relation == "unknown":
                        return "unknown"
            finally:
                os.close(group_fd)

        # The path must still name the directory descriptor used above. Its
        # metadata may have changed because new groups appeared; the later
        # base-membership pass handles those just as before.
        path_base = _stamp(baseline.base)
        if (path_base is None or not stat.S_ISDIR(path_base[2])
                or path_base[:4] != opened_base[:4]):
            return "unknown"
    finally:
        os.close(base_fd)
    return None


def _prepare_candidate_delivery_baseline(ctx: _Context, attempt: str | None,
                                         deadline: float) -> _CandidateDeliveryBaseline | None:
    """Prepare immutable lookup sets outside jobs.log.lock for final freshness."""
    binding = ctx.candidate_binding
    related = _candidate_related_attempts(ctx.root, ctx.snapshot, attempt)
    if time.monotonic() >= deadline:
        return None
    related_file_stamps = []
    for identity in related:
        for raw_path, fact in ctx.delivery_records.get(identity, {}).items():
            if time.monotonic() >= deadline:
                return None
            related_file_stamps.append((Path(raw_path), fact[-1]))
    baseline_files = []
    for path, stamp in ctx.discovery_stamps.items():
        if time.monotonic() >= deadline:
            return None
        if isinstance(path, Path) and path.suffix == ".json":
            baseline_files.append((path, stamp))
    group_stamps = tuple(ctx.group_stamps.items())
    if time.monotonic() >= deadline:
        return None
    base = ctx.root / "pending-delivery"
    return _CandidateDeliveryBaseline(
        attempt=attempt,
        binding=binding,
        related=frozenset(related),
        related_file_stamps=tuple(related_file_stamps),
        baseline_files=tuple(baseline_files),
        baseline_file_paths=frozenset(path for path, _stamp_value in baseline_files),
        group_stamps=group_stamps,
        group_names=frozenset(ctx.delivery_groups),
        base=base,
        base_stamp=ctx.discovery_stamps.get(base),
    )


def _late_candidate_delivery_change(ctx: _Context, attempt: str | None,
                                   deadline: float,
                                   binding: _HomeBinding | None = None) -> str | None:
    """Compare prepared delivery metadata, parsing only changed/new records.

    _detach calls this once outside the registry lock to reject known changes,
    then again inside final admission immediately before journal/rename. The
    immutable baseline is prepared by the first call and reused by the second.
    """
    binding = binding or ctx.candidate_binding
    baseline = ctx.candidate_delivery_baseline
    if (baseline is None or baseline.attempt != attempt
            or baseline.binding != binding):
        baseline = _prepare_candidate_delivery_baseline(ctx, attempt, deadline)
        if baseline is None:
            return "unknown"
        ctx.candidate_delivery_baseline = baseline
    related = baseline.related
    baseline_files = baseline.baseline_file_paths

    # Candidate-related records have already been indexed. A changed record
    # stays fail-closed even if a rewrite would remove its previous relation.
    for path, before in baseline.related_file_stamps:
        if time.monotonic() >= deadline:
            return "unknown"
        if _stamp(path) != before:
            return "changed"

    # Stat the bounded discovery set and parse only changed payloads.
    for path, before in baseline.baseline_files:
        if time.monotonic() >= deadline:
            return "unknown"
        after = _stamp(path)
        if after == before:
            continue
        if after is None:
            continue
        relation = _delivery_record_relation(path, related, binding)
        if relation == "related":
            return "changed"
        if relation == "unknown":
            return "unknown"

    # Fresh per-group metadata observations use one pinned directory handle,
    # avoiding repeated resolution of the shared pending-delivery path.
    group_change = _late_candidate_group_change(
        baseline, related, binding, baseline_files, deadline)
    if group_change is not None:
        return group_change

    if _stamp(baseline.base) != baseline.base_stamp:
        groups = _bounded_directory_entries(baseline.base, deadline)
        if groups is None:
            if _stamp(baseline.base) is None:
                groups = []
            else:
                return "unknown"
        elif _stamp(baseline.base) is None:
            groups = []
        for group in groups:
            if group.name in baseline.group_names:
                continue
            if time.monotonic() >= deadline:
                return "unknown"
            if group.is_symlink() or not group.is_dir(follow_symlinks=False):
                return "unknown"
            group_path = Path(group.path)
            group_stamp = _stamp(group_path)
            entries = _bounded_directory_entries(group_path, deadline)
            if entries is None:
                return "unknown"
            for entry in entries:
                if time.monotonic() >= deadline:
                    return "unknown"
                if not entry.name.endswith(".json"):
                    continue
                relation = _delivery_record_relation(Path(entry.path), related, binding)
                if relation == "related":
                    return "changed"
                if relation == "unknown":
                    return "unknown"
            if _stamp(group_path) != group_stamp:
                return "unknown"
    return None


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
        matches = ctx.legacy_matches(source, harness, worker_type).get(key, set())
        if ctx.legacy_scan_unknown:
            return _Verdict("unknown", "legacy-ledger-unverifiable")
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
    if claimed and _declared_home(ctx.root, ctx.workers_real, claimed) != key:
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
        delivery_index=ctx.delivery_index,
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
    refs, index_unknown = ctx.delivery_index()
    if index_unknown:
        return _Verdict("unknown", "delivery-index-unknown", detail="delivery-scan-incomplete")
    if attempt is not None and attempt in refs:
        return _Verdict("keep", "delivery-open")
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
        try:
            info = os.stat(name, dir_fd=layout.retiring_fd, follow_symlinks=False)
            if (_identity(info) != expected or info.st_uid != os.geteuid()
                    or info.st_dev != os.fstat(layout.retiring_fd).st_dev):
                return "unproven"
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
    ctx.delivery_index()
    binding = binding or ctx.candidate_binding
    prior_registry = _registry_projection(ctx.snapshot, key, attempt, ctx.by_key, binding)
    prior_delivery = ctx.delivery_projection(attempt, binding)
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
        token = _DEPENDENCIES.set(watched)
        try:
            current.by_key = _declared_index(fresh, ctx.root, current.workers_real)
        finally:
            _DEPENDENCIES.reset(token)
        current.delivery_index()
        if (_registry_projection(fresh, key, attempt, current.by_key, binding) != prior_registry
                or current.delivery_projection(attempt, binding) != prior_delivery):
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
                if verdict.kind == "unknown":
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
        return ("late-evidence-unreadable" if late_delivery == "unknown"
                else "late-evidence-changed")
    # The final metadata pass reuses discovery and stays outside jobs.log.lock.
    # Its larger directory set may need the existing removal budget; the lock
    # is acquired only after related new/changed delivery evidence is rejected.
    late_delivery = _late_candidate_delivery_change(current, attempt, deadline)
    if late_delivery is not None:
        return ("late-evidence-unreadable" if late_delivery == "unknown"
                else "late-evidence-changed")

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
                        return ("late-evidence-unreadable" if late_delivery == "unknown"
                                else "late-evidence-changed")
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
              "kept": {}, "unknown": {}, "unknown_causes": {}}
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
                    verdict, ident, binding = _examine(ctx, key)
                except Exception:  # noqa: BLE001 -- one bad home must not stall every pass
                    verdict, ident, binding = _Verdict("unknown", "examine-error"), None, None
                if verdict.kind != "eligible":
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
                else:
                    _count(result["kept"], "within-window")
        finally:
            scan.close()
        result["due"] = len(due)
        for key, ident, verdict, start, binding in due:
            if budget <= 0 or time.monotonic() > deadline:
                _count(result["kept"], "deferred")
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
