"""Shared terminal and outstanding-consumer protection for dispatch artifacts.

Home and transcript retention have different ownership and file-removal rules,
but they must agree on whether an attempt is still observable or consumable.
The adapters below supply their evidence readers; this module owns the ordered
decision so one artifact cannot be retired under weaker lifecycle semantics.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
import stat


QUARANTINE_PREFIX = ".hearting-retention-quarantine-"
QUARANTINE_LOCK = ".hearting-retention-quarantine.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _quarantine_lock(parent_fd):
    """Serialize quarantine moves and recovery across both retention callers."""
    try:
        fd = os.open(QUARANTINE_LOCK,
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()):
            os.close(fd)
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None
    return fd


def _same_inode(left, right):
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _stable_file_identity(left, right):
    """Compare file identity across a rename, which may update ctime."""
    return (left.st_dev, left.st_ino, left.st_mode, left.st_nlink, left.st_size,
            left.st_mtime_ns, left.st_uid) == (
                right.st_dev, right.st_ino, right.st_mode, right.st_nlink,
                right.st_size, right.st_mtime_ns, right.st_uid)


def _remove_empty_quarantine(parent_fd, quarantine_name, quarantine_fd):
    try:
        with os.scandir(quarantine_fd) as entries:
            if next(entries, None) is not None:
                return False
        os.rmdir(quarantine_name, dir_fd=parent_fd)
        return True
    except OSError:
        return False


def _recover_quarantine(parent_fd, quarantine_name):
    """Restore moved entries without replacing a writer at the original path."""
    try:
        quarantine_fd = os.open(quarantine_name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError:
        return
    try:
        qinfo = os.fstat(quarantine_fd)
        if (qinfo.st_uid != os.geteuid() or qinfo.st_mode & 0o077
                or not stat.S_ISDIR(qinfo.st_mode)):
            return
        try:
            entries = list(os.scandir(quarantine_fd))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if name in ("", ".", ".."):
                continue
            try:
                moved = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
                if moved.st_uid != os.geteuid():
                    continue
                try:
                    original = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if moved.st_nlink != 1:
                        continue
                    try:
                        # link is atomic and fails if a new writer owns the name.
                        os.link(name, name, src_dir_fd=quarantine_fd,
                                dst_dir_fd=parent_fd, follow_symlinks=False)
                    except OSError:
                        continue
                    original = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not _same_inode(original, moved):
                    continue
                current = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
                if _same_inode(current, moved) and current.st_uid == os.geteuid():
                    os.unlink(name, dir_fd=quarantine_fd)
            except OSError:
                continue
        _remove_empty_quarantine(parent_fd, quarantine_name, quarantine_fd)
    finally:
        os.close(quarantine_fd)


def recover_quarantines(parent_fd):
    """Recover interrupted moves during the next existing retention cadence."""
    lock_fd = _quarantine_lock(parent_fd)
    if lock_fd is None:
        return {"pending_files": 0, "pending_bytes": 0, "busy": True}
    try:
        try:
            with os.scandir(parent_fd) as entries:
                names = sorted(entry.name for entry in entries
                               if entry.name.startswith(QUARANTINE_PREFIX)
                               and len(entry.name) == len(QUARANTINE_PREFIX) + 32
                               and all(ch in "0123456789abcdef"
                                       for ch in entry.name[len(QUARANTINE_PREFIX):]))
        except OSError:
            return {"pending_files": 0, "pending_bytes": 0, "busy": False}
        for name in names[:64]:
            _recover_quarantine(parent_fd, name)
        pending_files = pending_bytes = 0
        for name in names[:64]:
            try:
                qfd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
                try:
                    with os.scandir(qfd) as entries:
                        for entry in entries:
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            pending_files += 1
                            pending_bytes += max(0, info.st_size)
                finally:
                    os.close(qfd)
            except OSError:
                continue
        return {"pending_files": pending_files, "pending_bytes": pending_bytes,
                "busy": False}
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def unlink_same_file(parent_fd, name, expected, stamp_fn):
    """Move, verify, then remove one pinned file through a unique quarantine."""
    if not isinstance(name, str) or name in ("", ".", "..") or os.path.basename(name) != name:
        return False
    lock_fd = _quarantine_lock(parent_fd)
    if lock_fd is None:
        return False
    opened_fd = quarantine_fd = moved_fd = None
    quarantine_name = None
    moved_entry = False
    try:
        try:
            opened_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                dir_fd=parent_fd)
            opened = os.fstat(opened_fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_uid != os.geteuid() or stamp_fn(opened) != expected
                or stamp_fn(current) != expected):
            return False

        for _attempt in range(8):
            candidate = QUARANTINE_PREFIX + secrets.token_hex(16)
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
                quarantine_name = candidate
                break
            except FileExistsError:
                continue
        if quarantine_name is None:
            return False
        quarantine_fd = os.open(quarantine_name, _DIR_FLAGS, dir_fd=parent_fd)
        qinfo = os.fstat(quarantine_fd)
        if (qinfo.st_uid != os.geteuid() or qinfo.st_mode & 0o077
                or not stat.S_ISDIR(qinfo.st_mode)):
            return False
        try:
            os.rename(name, name, src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd)
            moved_entry = True
        except OSError:
            return False
        try:
            moved_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                               dir_fd=quarantine_fd)
            moved = os.fstat(moved_fd)
            qcurrent = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
        except OSError:
            _recover_quarantine(parent_fd, quarantine_name)
            return False
        if (not stat.S_ISREG(moved.st_mode) or moved.st_nlink != 1
                or moved.st_uid != os.geteuid()
                or not _stable_file_identity(opened, moved)
                or stamp_fn(qcurrent) != stamp_fn(moved)):
            os.close(moved_fd)
            moved_fd = None
            _recover_quarantine(parent_fd, quarantine_name)
            return False
        os.unlink(name, dir_fd=quarantine_fd)
        moved_entry = False
        with contextlib.suppress(OSError):
            os.fsync(quarantine_fd)
            os.rmdir(quarantine_name, dir_fd=parent_fd)
            quarantine_name = None
        return True
    except OSError:
        return False
    finally:
        if quarantine_name is not None and not moved_entry and quarantine_fd is not None:
            _remove_empty_quarantine(parent_fd, quarantine_name, quarantine_fd)
        for fd in (moved_fd, quarantine_fd, opened_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


class Protection:
    __slots__ = ("kind", "reason", "activity_ns", "attempt", "raw", "detail")

    def __init__(self, kind, reason="", activity_ns=0, attempt="", raw="", detail=""):
        self.kind, self.reason, self.activity_ns = kind, reason, activity_ns
        self.attempt, self.raw = attempt, raw
        self.detail = detail


TERMINAL_STATUSES = frozenset({"done", "killed", "cancelled"})
OPEN_STATUSES = frozenset({"open", "running"})
MAX_INTENT_CHILDREN = 256


def terminal_protection(ctx, attempt, *, probe_processes, delivery_state,
                        replacement_state, route_state, owner_state, lease_state,
                        delivery_index, supervisor_activity, process_quiescence,
                        protect_open_route=True, owner_detail=None):
    """Return ``eligible`` only when the exact attempt has no live consumer.

    Callback arguments are read-only evidence resolvers. ``unknown`` always
    keeps data, and activity timestamps extend retention instead of expiring an
    outstanding obligation.
    """
    snapshot = ctx.snapshot
    row = snapshot.rows.get(attempt)
    if row is None:
        return Protection("unknown", "no-row")
    if row.status not in TERMINAL_STATUSES:
        return Protection("keep", "not-terminal")
    if attempt in snapshot.open_refs:
        return Protection("keep", "open-reference")

    activity = 0
    state, mtime = delivery_state(row.meta)
    if state in ("open", "pending"):
        return Protection("keep", "delivery-" + state)
    if state == "unknown":
        return Protection("unknown", "delivery-unknown")
    activity = max(activity, mtime)

    children = snapshot.intents.get(attempt, ())
    if len(children) > MAX_INTENT_CHILDREN:
        return Protection("unknown", "delivery-unknown")
    for child in children:
        child_row = snapshot.rows.get(child)
        if child_row is None:
            return Protection("unknown", "delivery-unknown")
        state, mtime = delivery_state(child_row.meta)
        if state in ("open", "pending"):
            return Protection("keep", "delivery-" + state)
        if state == "unknown":
            return Protection("unknown", "delivery-unknown")
        activity = max(activity, mtime)

    state, mtime = replacement_state(attempt)
    if state == "unresolved":
        return Protection("keep", "replacement-unresolved")
    if state == "unknown":
        return Protection("unknown", "replacement-unknown")
    activity = max(activity, mtime)

    route = route_state(row.meta)
    if len(route) == 3:
        state, mtime, route_detail = route
    else:
        state, mtime = route
        route_detail = "route-state-unavailable"
    if state == "pending" or (state == "open" and protect_open_route):
        return Protection("keep", "route-finish-pending" if state == "pending" else "route-open")
    if state == "unknown":
        return Protection("unknown", "route-unknown", detail=route_detail)
    if state == "open":
        # A route's administrative lifetime does not itself consume a worker HOME.
        # The home caller continues through exact continuation, owner, lease and
        # process evidence below. Other shared consumers keep the legacy rule.
        pass
    activity = max(activity, mtime)

    owner_evidence = owner_state(row.meta)
    if isinstance(owner_evidence, tuple) and len(owner_evidence) == 2:
        owner, owner_cause = owner_evidence
    else:
        owner, owner_cause = owner_evidence, "owner-state-unavailable"
    if owner == "live":
        return Protection("keep", "summary-live")
    if owner == "unknown":
        detail = (owner_cause if owner_cause != "owner-state-unavailable"
                  else owner_detail(row.meta) if owner_detail is not None
                  else owner_cause)
        return Protection("unknown", "summary-owner-unknown", detail=detail)
    lease = lease_state(row.meta)
    if lease == "held":
        return Protection("keep", "lease-held")
    if lease == "unknown":
        return Protection("unknown", "lease-unknown")

    refs, index_unknown = delivery_index()
    if index_unknown:
        return Protection("unknown", "delivery-index-unknown",
                          detail=index_unknown if isinstance(index_unknown, str) else "")
    if attempt in refs:
        return Protection("keep", "delivery-open")
    activity = max(activity, ctx.delivery_activity.get(attempt, 0))
    activity = max(activity, supervisor_activity(attempt))

    if probe_processes:
        try:
            quiescence = process_quiescence(row.meta)
        except Exception as exc:  # noqa: BLE001 -- an unobservable process is not quiet
            return Protection("unknown", "quiescence-unverifiable",
                              detail=type(exc).__name__.lower())
        if quiescence.state == "live":
            return Protection("keep", "not-quiescent")
        if quiescence.state != "quiescent":
            detail = getattr(quiescence, "reason", "") or "process-state-unavailable"
            return Protection("unknown", "quiescence-unverifiable", detail=detail)

    return Protection("eligible", activity_ns=activity, attempt=attempt, raw=row.raw)
