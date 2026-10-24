"""Durable observation ownership for registered completion sidecars."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
from collections import deque
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time

_ID_RE = re.compile(r"batch-[0-9a-f]{32}\Z")
_MAX_BYTES = 128 * 1024
_PENDING = {"observing", "delivery-pending", "unknown"}
_SETTLED_DEPENDENCY_CHECKS_PER_PASS = 64
_DEPENDENCY_WATCH_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 \
                         | 0x00000100 | 0x00000200 | 0x00000400 | 0x00000800
_INOTIFY_EVENT_HEADER = struct.Struct("iIII")
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_WATCH_INVALID = 0x00008C00
_LOCAL_WATCH_FILESYSTEMS = frozenset({
    "btrfs", "ext2", "ext3", "ext4", "f2fs", "overlay", "tmpfs", "xfs", "zfs",
})


class BatchObligationError(ValueError):
    pass


def _root(jobs: str | Path) -> Path:
    path = Path(jobs).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BatchObligationError("batch-obligation-jobs-invalid")
    canonical = path.resolve(strict=True)
    root = canonical.parent / "supervisor-state" / "obligations"
    if root.exists() and root.is_symlink():
        raise BatchObligationError("batch-obligation-root-symlink")
    return root


def _record_path(jobs: str | Path, duty_id: str) -> Path:
    if not isinstance(duty_id, str) or not _ID_RE.fullmatch(duty_id):
        raise BatchObligationError("batch-obligation-id-invalid")
    return _root(jobs) / (duty_id + ".json")


def _read(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise BatchObligationError("batch-obligation-record-oversized")
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BatchObligationError("batch-obligation-record-invalid") from exc
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or not isinstance(record.get("identity"), dict)
            or not isinstance(record.get("arguments"), dict)):
        raise BatchObligationError("batch-obligation-record-invalid")
    return record


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise BatchObligationError("batch-obligation-path-symlink")
    fd, temporary = tempfile.mkstemp(prefix=".batch-obligation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _plain_arguments(args) -> dict:
    output = {}
    for key, value in vars(args).items():
        if key in {"resume_obligation", "ensure_obligations", "obligation_lock_fd", "service_current"}:
            continue
        output[key] = str(value) if isinstance(value, Path) else value
    return output


def create(args) -> tuple[str, dict]:
    jobs = Path(args.jobs).expanduser().resolve(strict=True)
    attempts = sorted(set(args.attempt_id))
    identity = {
        "jobs": str(jobs),
        "sealed_batch_id": args.sealed_batch_id,
        "parent_attempt_id": args.parent_attempt_id or "",
        "parent_session_id": args.parent_session_id or "",
        "attempt_ids": attempts,
        "delivery_parent_id": (args.parent_session_id or args.parent_attempt_id or ""),
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    duty_id = "batch-" + hashlib.sha256(raw).hexdigest()[:32]
    path = _record_path(jobs, duty_id)
    root = path.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    create_lock = root / (duty_id + ".create.lock")
    fd = os.open(create_lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = _read(path)
        if current is not None:
            if current.get("identity") != identity:
                raise BatchObligationError("batch-obligation-identity-conflict")
            return duty_id, current
        now = time.time()
        record = {
            "schema_version": 1,
            "id": duty_id,
            "identity": identity,
            "arguments": _plain_arguments(args),
            "state": "observing",
            "reason": "accepted",
            "accepted_at": now,
            "updated_at": now,
            "effective_attempts": attempts,
            "replacement_lineage": [],
            "outcome": None,
            "delivery": "pending",
            "observer": None,
        }
        _write(path, record)
        return duty_id, record
    finally:
        os.close(fd)


def read(jobs: str | Path, duty_id: str) -> dict | None:
    return _read(_record_path(jobs, duty_id))


def update(jobs: str | Path, duty_id: str, **changes) -> dict:
    path = _record_path(jobs, duty_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".update.lock")
    if lock_path.is_symlink():
        raise BatchObligationError("batch-obligation-update-lock-symlink")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = _read(path)
        if record is None:
            raise BatchObligationError("batch-obligation-missing")
        if changes.get("outcome") not in (None, record.get("outcome")) and record.get("outcome") is not None:
            raise BatchObligationError("batch-obligation-outcome-conflict")
        record.update(changes)
        record["updated_at"] = time.time()
        _write(path, record)
        return record
    finally:
        os.close(fd)


def acquire(jobs: str | Path, duty_id: str, inherited_fd: int | None = None) -> int | None:
    root = _root(jobs)
    lock_path = root / (duty_id + ".lease")
    if inherited_fd is not None:
        try:
            return inherited_fd if _fd_holds_lease(inherited_fd, lock_path) else None
        except (OSError, ValueError, TypeError):
            return None
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise BatchObligationError("batch-obligation-lease-symlink")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not _fd_holds_lease(fd, lock_path):
            release(fd)
            return None
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def _fd_holds_lease(fd: int, lock_path: str | Path) -> bool:
    """Prove this descriptor names the current lease inode and carries its flock."""
    path = Path(lock_path)
    try:
        if path.is_symlink():
            return False
        expected = path.resolve(strict=True)
        target = Path(os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")))
        descriptor = os.fstat(fd)
        current = os.stat(expected, follow_symlinks=False)
        if (target != expected or not stat.S_ISREG(descriptor.st_mode)
                or (descriptor.st_dev, descriptor.st_ino)
                != (current.st_dev, current.st_ino)):
            return False
        device = f"{os.major(current.st_dev):02x}:{os.minor(current.st_dev):02x}"
        inode = str(current.st_ino)
        fdinfo = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii")
        return any(
            len(fields := line.split()) >= 7
            and fields[0] == "lock:"
            and fields[2] == "FLOCK"
            and fields[3] == "ADVISORY"
            and fields[4] == "WRITE"
            and fields[6] == f"{device}:{inode}"
            for line in fdinfo.splitlines()
        )
    except (OSError, ValueError, TypeError):
        return False


def observer_owns_current(jobs: str | Path, duty_id: str, fd: int | None) -> bool:
    """Check the live lease and exact durable PID/start owner for this process."""
    if fd is None:
        return False
    lock_path = _root(jobs) / (duty_id + ".lease")
    if not _fd_holds_lease(fd, lock_path):
        return False
    try:
        record = read(jobs, duty_id)
        observer = record.get("observer") if record else None
        start = _pid_start(os.getpid())
        return (isinstance(observer, dict)
                and observer.get("pid") == os.getpid()
                and isinstance(start, str) and start.isdecimal()
                and observer.get("pid_start") == start)
    except (OSError, ValueError, TypeError, BatchObligationError):
        return False


def update_for_observer(jobs: str | Path, duty_id: str, fd: int,
                        *, claim: bool = False,
                        expected_observer: dict | None = None, **changes) -> dict | None:
    """Update one duty only while this process still owns its exact lease row."""
    path = _record_path(jobs, duty_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    update_lock = path.with_suffix(".update.lock")
    if update_lock.is_symlink():
        raise BatchObligationError("batch-obligation-update-lock-symlink")
    lock_fd = os.open(update_lock, os.O_RDWR | os.O_CREAT
                      | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not _fd_holds_lease(fd, _root(jobs) / (duty_id + ".lease")):
            return None
        record = _read(path)
        if record is None or record.get("state") not in _PENDING:
            return None
        owner = {"pid": os.getpid(), "pid_start": _pid_start(os.getpid())}
        if not owner["pid_start"].isdecimal():
            return None
        if not claim and record.get("observer") != owner:
            return None
        if (claim and expected_observer is not None
                and record.get("observer") not in (expected_observer, owner)):
            return None
        record.update(changes)
        if claim:
            record["observer"] = owner
        record["updated_at"] = time.time()
        _write(path, record)
        return record
    finally:
        os.close(lock_fd)


def release(fd: int | None, expected_path: str | Path | None = None) -> None:
    if fd is None:
        return
    if expected_path is not None and not _fd_references_path(fd, expected_path):
        return
    try:
        if expected_path is None or _fd_holds_lease(fd, expected_path):
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _fd_references_path(fd: int, expected_path: str | Path) -> bool:
    try:
        path = Path(expected_path)
        if path.is_symlink():
            return False
        expected = path.resolve(strict=True)
        target = Path(os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")))
        descriptor = os.fstat(fd)
        current = os.stat(expected, follow_symlinks=False)
        return (target == expected and stat.S_ISREG(descriptor.st_mode)
                and (descriptor.st_dev, descriptor.st_ino)
                == (current.st_dev, current.st_ino))
    except (OSError, ValueError, TypeError):
        return False


def _jobs_paths(jobs: str | Path | None, *, strict: bool = True) -> list[Path]:
    if jobs:
        path = Path(jobs).expanduser()
        return [path.resolve(strict=True) if strict else Path(os.path.abspath(path))]
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    return [Path(root) / "jobs.log" for root in dispatch_state_roots(resolve_agent_home())]


def _observer_root(jobs: str | Path) -> Path | None:
    """Find an obligation root without requiring its compatibility registry.

    Writers continue to use ``_root`` and its strict existing-registry check.
    Read-only observers can still see durable records after jobs.log disappears;
    a missing registry and missing sidecar tree are known-empty, while unsafe or
    unreadable paths remain unknown to callers.
    """
    path = Path(jobs).expanduser()
    if not path.is_absolute():
        raise BatchObligationError("batch-obligation-jobs-invalid")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            parent = path.parent.resolve(strict=True)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BatchObligationError("batch-obligation-jobs-unavailable") from exc
        root = parent / "supervisor-state" / "obligations"
    except OSError as exc:
        raise BatchObligationError("batch-obligation-jobs-unavailable") from exc
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BatchObligationError("batch-obligation-jobs-invalid")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            os.close(fd)
            parent = path.resolve(strict=True).parent
        except OSError as exc:
            raise BatchObligationError("batch-obligation-jobs-unavailable") from exc
        root = parent / "supervisor-state" / "obligations"

    for directory in (root.parent, root):
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BatchObligationError("batch-obligation-root-unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BatchObligationError("batch-obligation-root-invalid")
    return root


def _read_only_records(jobs: str | Path) -> tuple[tuple[Path, dict | None, Exception | None], ...]:
    """Enumerate records with one shared absent-versus-unknown read policy."""
    root = _observer_root(jobs)
    if root is None:
        return ()
    try:
        with os.scandir(root) as entries:
            paths = sorted(root / entry.name for entry in entries
                           if entry.name.startswith("batch-") and entry.name.endswith(".json"))
    except OSError as exc:
        raise BatchObligationError("batch-obligation-root-unavailable") from exc
    records = []
    for path in paths:
        try:
            records.append((path, _read(path), None))
        except (OSError, ValueError, BatchObligationError) as exc:
            records.append((path, None, exc))
    return tuple(records)


def _proc_lifetime(pid):
    """Return (state, start) without treating unreadable /proc as process death."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return "gone", None
    except (OSError, ValueError, TypeError):
        return "unknown", None
    try:
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] in {"Z", "X"}:
            return "gone", None
        return "live", fields[19]
    except (IndexError, ValueError):
        return "unknown", None


def _parent_duty_has_recipient(record: dict, jobs: Path) -> bool:
    """Keep a stopped duty dormant until its exact parent or bound successor returns."""
    identity = record.get("identity") or {}
    parent = identity.get("parent_session_id") or ""
    if not parent:
        return True
    binding = record.get("parent_lifetime")
    if not isinstance(binding, dict):
        binding = (record.get("arguments") or {}).get("parent_lifetime")
    if not isinstance(binding, dict):
        return True
    try:
        from dispatch_contract import process_namespace_identity
        namespace = binding.get("pid_namespace")
        if not namespace or process_namespace_identity() != namespace:
            return True
        pid = int(binding.get("pid"))
        start = str(binding.get("pid_start") or "")
    except (ImportError, OSError, TypeError, ValueError):
        return True
    state, observed_start = _proc_lifetime(pid)
    if state == "unknown":
        return True
    if state == "live" and observed_start == start:
        try:
            from pane_ownership import _native_session
            current = _native_session(pid, "codex")
        except Exception:
            return True
        if not current:
            return True
        if current == parent:
            return True

    # A replacement TUI may carry the original native thread under a new PID,
    # so confirm that case using the same pane ownership contract.
    try:
        from pane_ownership import codex_tui_processes_for_session
        bindings = codex_tui_processes_for_session(parent)
    except Exception:
        return True
    if bindings:
        return True

    # The original parent may have handed these exact attempts to a new seat.
    # Keep the observer available for that durable recipient even if it has not
    # started a TUI process yet.
    try:
        import dispatch_seat_handover as handover
        rows = handover.latest_rows(jobs)
        attempts = record.get("effective_attempts") or identity.get("attempt_ids") or []
        for attempt in attempts:
            row = rows.get(attempt)
            if row is None:
                continue
            recipient = handover.effective_parent(row[1], jobs)
            if recipient and recipient != parent:
                return True
    except Exception:
        return True
    return False


def _spawn_observer(jobs: Path, duty_id: str, fd: int, launch, *, expected_observer=None):
    """Publish the PID before a resumed child can claim its inherited lease.

    The child uses this same update lock. Holding it across fork/exec and the
    small record write closes the spawn/publication race with no extra wire
    arguments. Closing a parent's descriptor copy never unlocks the child's
    shared open file description.
    """
    path = _record_path(jobs, duty_id)
    lock_path = path.with_suffix(".update.lock")
    if lock_path.is_symlink():
        raise BatchObligationError("batch-obligation-update-lock-symlink")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lease_path = _root(jobs) / (duty_id + ".lease")
        if not _fd_holds_lease(fd, lease_path):
            return None
        record = _read(path)
        if not record or record.get("state") not in _PENDING:
            return None
        if expected_observer is not None and record.get("observer") != expected_observer:
            return None
        script, child_env, binding = launch
        command = [sys.executable, str(script), "--jobs", str(jobs), "--resume-obligation", duty_id,
                   "--obligation-lock-fd", str(fd)]
        if binding:
            command.extend(("--service-current", binding))
        from dispatch_contract import governance_environment

        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(fd,), start_new_session=True,
            cwd=str(jobs.parent), env=governance_environment(child_env))
        if not _fd_holds_lease(fd, lease_path):
            return None
        record["observer"] = {"pid": process.pid, "pid_start": _pid_start(process.pid)}
        record["updated_at"] = time.time()
        _write(path, record)
        return process
    finally:
        os.close(lock_fd)


def ensure_observers(jobs: str | Path | None = None, *, service_current=None,
                     observer_snapshot=None, settlement_context=None) -> int:
    """Resume unfinished exact batches through the existing completion sidecar."""
    launched = 0
    import peer_obligations
    script, child_env, binding = peer_obligations.service_script(
        "utilities/codex-managed-completion.py", service_current)
    jobs_paths = _jobs_paths(jobs)
    settlement_signatures = []
    # Discovery returns the canonical writer first, followed by compatibility
    # roots that are read-only under dispatch_state_roots(). Owner settlement
    # mutates the registry, so only the explicitly selected path (or the
    # canonical discovered path at index 0) may reach that writer. Sidecar
    # discovery below still visits every root independently.
    for jobs_path in jobs_paths[:1]:
        try:
            settlement_signatures.extend(
                _resume_owner_settlements(jobs_path, context=settlement_context))
        except (OSError, UnicodeError):
            # Keep this root's sidecar scan and later independent roots reachable.
            settlement_signatures.append((str(jobs_path), "owner-settlement-unavailable"))
            continue
    if observer_snapshot is not None and settlement_signatures:
        observer_snapshot["signature"] = tuple(sorted(
            (*observer_snapshot.get("signature", ()), *settlement_signatures)))
    for jobs_path in jobs_paths:
        try:
            root = _root(jobs_path)
            snapshot_paths = None
            if observer_snapshot is not None:
                key = str(jobs_path.resolve(strict=False))
                snapshot_paths = observer_snapshot.get("paths_by_jobs", {}).get(key)
            records = (sorted(root.glob("batch-*.json")) if snapshot_paths is None
                       else list(snapshot_paths)) if root.is_dir() else []
        except (OSError, BatchObligationError):
            continue
        for path in records:
            try:
                if path.parent.resolve(strict=True) != root.resolve(strict=True):
                    continue
                # A per-pass snapshot selects candidates only. Re-read the
                # exact durable row before acquiring or replacing its owner.
                record = _read(path)
                if not record or record.get("state") not in _PENDING:
                    continue
                if (record.get("state") == "unknown"
                        and record.get("reason") == "parent-session-ended"
                        and not _parent_duty_has_recipient(record, jobs_path)):
                    continue
                duty_id = record["id"]
                fd = acquire(jobs_path, duty_id)
                if fd is None:
                    observer = record.get("observer") or {}
                    if (not isinstance(observer, dict) or not binding
                            or not observer.get("pid")):
                        continue
                    if "pid_start" in observer:
                        observer_start = observer["pid_start"]
                        if (not isinstance(observer_start, str)
                                or not observer_start.isdecimal()):
                            continue
                    else:
                        observer_start = None
                    replaced = peer_obligations.replace_managed_observer(
                        observer["pid"], observer_start,
                        root / (duty_id + ".lease"),
                        "utilities/codex-managed-completion.py",
                        ("--jobs", str(jobs_path), "--resume-obligation", duty_id),
                        binding, lock_fd_option="--obligation-lock-fd",
                        exact_arguments=True,
                    )
                    if replaced != "replaced":
                        continue
                    fd = acquire(jobs_path, duty_id)
                    if fd is None:
                        continue
                try:
                    if _spawn_observer(jobs_path, duty_id, fd, (script, child_env, binding)) is not None:
                        launched += 1
                finally:
                    # This also safely releases a failed pre-spawn acquisition;
                    # after a spawn it preserves the child's inherited flock.
                    os.close(fd)
            except (OSError, ValueError, subprocess.SubprocessError, BatchObligationError):
                continue
    return launched


def handoff_observer(jobs: str | Path, duty_id: str, fd: int, *, service_current=None) -> bool:
    """Pass a pending duty to one successor while this process still holds its lease."""
    if not observer_owns_current(jobs, duty_id, fd):
        return False
    parent = {"pid": os.getpid(), "pid_start": _pid_start(os.getpid())}
    if not parent["pid_start"].isdecimal():
        return False
    try:
        import peer_obligations
        script, child_env, binding = peer_obligations.service_script(
            "utilities/codex-managed-completion.py", service_current)
        jobs_path = Path(jobs).resolve(strict=True)
        return _spawn_observer(jobs_path, duty_id, fd, (script, child_env, binding),
                               expected_observer=parent) is not None
    except (OSError, subprocess.SubprocessError, BatchObligationError):
        return False


def request_runner(jobs: str | Path) -> bool:
    """Return a pending duty to the existing obligation runner.

    The carrier uses the common detached recovery request; the runner's
    ``ensure_observers`` pass relaunches the duty under its bounded backoff.
    A request made while that runner is alive changes nothing.
    """
    try:
        import peer_obligations
        return peer_obligations.request_dispatch_reconciliation(
            Path(jobs).resolve(strict=True).parent)
    except Exception:
        return False


def _registry_stamp(path: Path):
    try:
        info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_stamp(path: Path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


class OwnerSettlementReadContext:
    """Reuse exact settled-owner observations while rechecking their dependencies."""

    def __init__(self):
        self._seen = {}
        self._settled = {}
        self._active_owners = {}
        self._route_owners = {}
        self._cursors = {}
        self._invalidated_owners = set()
        self._pass_file_generations = {}
        self._dependency_queues = {}
        self._dependency_queued = set()
        self._dependency_watch_fd = None
        self._dependency_watch_add = None
        self._dependency_watch_remove = None
        self._dependency_watch_dirs = {}
        self._dependency_watch_paths = {}
        self._dependency_watch_keys = {}
        self._dependency_watch_fs_support = {}
        self._dependency_dirty_paths = set()
        self._dependency_watcher_fallback = False
        self._metrics = {"historical_checks": 0, "settled_cache_hits": 0,
                         "dependency_checks": 0, "dependency_invalidations": 0}

    @staticmethod
    def _fingerprint_rows(rows):
        return tuple((row.order, row.status, row.raw) for row in rows)

    @staticmethod
    def _current_generations(paths, cache=None):
        generations = []
        for path in paths:
            key = os.fspath(path)
            if not os.path.isabs(key):
                key = os.path.abspath(os.path.expanduser(key))
            if cache is not None and key in cache:
                generation = cache[key]
            else:
                # stat follows symlinks and captures replacement or rewrites;
                # resolving every dependency first adds two path walks per
                # check without changing the observed file generation.
                info = os.stat(key)
                generation = (key, (info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns))
                if cache is not None:
                    cache[key] = generation
            generations.append(generation)
        return tuple(generations)

    def _dependencies_current(self, entry, cache=None):
        _row, _related, paths, generations, _aliases = entry
        try:
            current = self._current_generations(paths, cache)
        except OSError:
            return False
        self._metrics["dependency_checks"] += len(paths)
        return current == generations

    def begin_dependency_pass(self):
        self._pass_file_generations.clear()

    def _ensure_dependency_watcher(self):
        if self._dependency_watcher_fallback:
            return False
        if self._dependency_watch_fd is not None:
            return self._dependency_watch_fd >= 0
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1
            init.argtypes = (ctypes.c_int,)
            init.restype = ctypes.c_int
            add = libc.inotify_add_watch
            add.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
            add.restype = ctypes.c_int
            remove = libc.inotify_rm_watch
            remove.argtypes = (ctypes.c_int, ctypes.c_int)
            remove.restype = ctypes.c_int
            fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                raise OSError(ctypes.get_errno(), "inotify-init-failed")
            self._dependency_watch_fd = fd
            self._dependency_watch_add = add
            self._dependency_watch_remove = remove
            self._dependency_watch_libc = libc
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            self._dependency_watch_fd = -1
            self._dependency_watcher_fallback = True
            return False

    def _clear_dependency_watch_state(self):
        self._dependency_watch_dirs.clear()
        self._dependency_watch_paths.clear()
        self._dependency_watch_keys.clear()
        self._dependency_watch_fs_support.clear()
        self._dependency_dirty_paths.clear()

    def _close_dependency_watch_descriptor(self, closed_value):
        fd = self._dependency_watch_fd
        self._dependency_watch_fd = closed_value
        self._dependency_watch_add = None
        self._dependency_watch_remove = None
        self._dependency_watch_libc = None
        if isinstance(fd, int) and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def _switch_dependency_watcher_to_fallback(self):
        self._dependency_watcher_fallback = True
        self._close_dependency_watch_descriptor(-1)
        self._clear_dependency_watch_state()

    def close(self):
        """Retire this runner-owned read context and release its watcher."""
        self._switch_dependency_watcher_to_fallback()

    def _dependency_watch_supported(self, directory):
        supported = self._dependency_watch_fs_support.get(directory)
        if supported is not None:
            return supported
        try:
            path = os.path.realpath(directory)
            best_mount = ""
            filesystem = None
            for line in Path("/proc/self/mountinfo").read_text(
                    encoding="utf-8").splitlines():
                fields = line.split()
                separator = fields.index("-")
                mount = (fields[4].replace(r"\040", " ").replace(r"\011", "\t")
                         .replace(r"\012", "\n").replace(r"\134", "\\"))
                if ((path == mount or path.startswith(mount.rstrip(os.sep) + os.sep))
                        and len(mount) >= len(best_mount)):
                    best_mount = mount
                    filesystem = fields[separator + 1]
            supported = filesystem in _LOCAL_WATCH_FILESYSTEMS
        except (OSError, ValueError, IndexError):
            supported = False
        self._dependency_watch_fs_support[directory] = supported
        return supported

    def _register_dependency_watch(self, key, paths):
        if not self._ensure_dependency_watcher():
            return
        watched = set()
        for path in paths:
            targets = (path, os.path.realpath(path))
            for target in targets:
                directory, name = os.path.split(target)
                directory = directory or "."
                if not self._dependency_watch_supported(directory):
                    self._switch_dependency_watcher_to_fallback()
                    return
                wd = self._dependency_watch_dirs.get(directory)
                if wd is None:
                    wd = self._dependency_watch_add(
                        self._dependency_watch_fd, os.fsencode(directory),
                        _DEPENDENCY_WATCH_MASK)
                    if wd < 0:
                        self._switch_dependency_watcher_to_fallback()
                        return
                    self._dependency_watch_dirs[directory] = wd
                pair = (wd, name)
                self._dependency_watch_paths.setdefault(pair, set()).add(key)
                watched.add(pair)
        self._dependency_watch_keys[key] = tuple(watched)

    def _forget_dependency_watch(self, key):
        for pair in self._dependency_watch_keys.pop(key, ()):
            owners = self._dependency_watch_paths.get(pair)
            if owners is not None:
                owners.discard(key)
                if not owners:
                    self._dependency_watch_paths.pop(pair, None)
                    self._dependency_dirty_paths.discard(pair)
                    wd, _name = pair
                    if not any(watched_wd == wd
                               for watched_wd, _watched_name in self._dependency_watch_paths):
                        self._remove_dependency_directory_watch(wd)

    def _remove_dependency_directory_watch(self, wd):
        fd = self._dependency_watch_fd
        remove = self._dependency_watch_remove
        if fd is None or fd < 0 or remove is None:
            self._switch_dependency_watcher_to_fallback()
            return
        if remove(fd, wd) < 0 and self._dependency_watch_libc is not None:
            import ctypes
            if ctypes.get_errno() != errno.EINVAL:
                self._switch_dependency_watcher_to_fallback()
                return
        directories = [directory for directory, watched_wd
                       in self._dependency_watch_dirs.items() if watched_wd == wd]
        for directory in directories:
            self._dependency_watch_dirs.pop(directory, None)
            self._dependency_watch_fs_support.pop(directory, None)
        # inotify queues IN_IGNORED for an explicit removal. Drain it while the
        # retired watch descriptor cannot be confused with a later registration.
        self._poll_dependency_events()

    def _poll_dependency_events(self):
        fd = self._dependency_watch_fd
        if fd is None or fd < 0 or self._dependency_watcher_fallback:
            return
        event_header = _INOTIFY_EVENT_HEADER
        while True:
            try:
                events = os.read(fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                self._switch_dependency_watcher_to_fallback()
                break
            if not events:
                break
            offset = 0
            while offset + event_header.size <= len(events):
                wd, mask, _cookie, name_length = event_header.unpack_from(events, offset)
                offset += event_header.size
                name = os.fsdecode(events[offset:offset + name_length].split(b"\0", 1)[0])
                offset += name_length
                if mask & _IN_IGNORED:
                    if wd not in self._dependency_watch_dirs.values():
                        continue
                    self._switch_dependency_watcher_to_fallback()
                    break
                if mask & _IN_Q_OVERFLOW:
                    self._switch_dependency_watcher_to_fallback()
                    break
                if mask & _IN_WATCH_INVALID:
                    self._switch_dependency_watcher_to_fallback()
                    break
                if not name:
                    continue
                pair = (wd, name)
                if pair in self._dependency_watch_paths:
                    self._dependency_dirty_paths.add(pair)
            if self._dependency_watcher_fallback:
                break

    def settled_owner_current(self, jobs: Path, status: str, raw: str, metadata: dict,
                              related_rows) -> bool:
        key = (str(jobs.resolve(strict=False)), metadata.get("attempt_id", ""))
        entry = self._settled.get(key)
        if entry is None:
            return False
        if (entry[0] != (status, raw)
                or entry[1] != self._fingerprint_rows(related_rows)):
            self.forget_owner(jobs, metadata.get("attempt_id", ""))
            return False
        if not self._dependencies_current(entry, self._pass_file_generations):
            self.forget_owner(jobs, metadata.get("attempt_id", ""))
            self._metrics["dependency_invalidations"] += 1
            return False
        self._metrics["settled_cache_hits"] += 1
        return True

    def remember_settled_owner(self, jobs: Path, status: str, raw: str,
                               metadata: dict, related_rows, dependencies) -> bool | None:
        key = (str(jobs.resolve(strict=False)), metadata.get("attempt_id", ""))
        paths = tuple(dict.fromkeys(
            os.path.abspath(os.path.expanduser(os.fspath(path)))
            for path in dependencies))
        try:
            before_registration = self._current_generations(
                paths, self._pass_file_generations)
        except OSError:
            self.forget_owner(jobs, metadata.get("attempt_id", ""))
            return None
        aliases = tuple(sorted({metadata.get(field, "") for field in
            ("route_id", "route_hash", "owner_route_id", "owner_route_hash") if metadata.get(field)}))
        self.forget_owner(jobs, metadata.get("attempt_id", ""))
        self._register_dependency_watch(key, paths)
        try:
            # This snapshot must bypass the per-pass cache: it confirms the
            # generation observed before watch installation did not change in
            # the registration window.
            generations = self._current_generations(paths)
        except OSError:
            self.forget_owner(jobs, metadata.get("attempt_id", ""))
            return None
        for path, generation in zip(paths, generations):
            self._pass_file_generations[path] = generation
        if generations != before_registration:
            self._metrics["dependency_invalidations"] += 1
            self.forget_owner(jobs, metadata.get("attempt_id", ""))
            return False
        self._settled[key] = ((status, raw), self._fingerprint_rows(related_rows),
                              paths, generations, aliases)
        self._queue_settled_dependency_check(key)
        for alias in aliases:
            self._route_owners.setdefault((key[0], alias), set()).add(key[1])
        if len(self._settled) > 4096:
            oldest = next(iter(self._settled))
            self.forget_owner(Path(oldest[0]), oldest[1])
        return True

    def _queue_settled_dependency_check(self, key):
        if key in self._dependency_queued:
            return
        self._dependency_queues.setdefault(key[0], deque()).append(key)
        self._dependency_queued.add(key)

    def _validate_settled_dependency_batch(self, jobs: Path) -> bool:
        """Rotate a fixed-size dependency check batch through settled owners."""
        self._poll_dependency_events()
        root = str(jobs.resolve(strict=False))
        queue = self._dependency_queues.get(root)
        if not queue:
            return any(item_root == root for item_root, _attempt in self._invalidated_owners)
        if not self._dependency_watcher_fallback and not self._dependency_dirty_paths:
            queue.rotate(-min(_SETTLED_DEPENDENCY_CHECKS_PER_PASS, len(queue)))
            return bool(queue)
        self.begin_dependency_pass()
        examined = 0
        invalidated = False
        generations = self._pass_file_generations
        while queue and examined < _SETTLED_DEPENDENCY_CHECKS_PER_PASS:
            key = queue.popleft()
            self._dependency_queued.discard(key)
            examined += 1
            entry = self._settled.get(key)
            if entry is None:
                continue
            if not self._dependency_watcher_fallback:
                dirty = any(pair in self._dependency_dirty_paths
                            for pair in self._dependency_watch_keys.get(key, ()))
                if dirty:
                    self._metrics["dependency_invalidations"] += 1
                    self._invalidated_owners.add(key)
                    self.forget_owner(jobs, key[1])
                    invalidated = True
                    continue
                self._queue_settled_dependency_check(key)
                continue
            paths, expected = entry[2], entry[3]
            current = []
            try:
                for path, expected_generation in zip(paths, expected):
                    generation = generations.get(path)
                    if generation is None:
                        info = os.stat(path)
                        generation = (path, (info.st_dev, info.st_ino, info.st_size,
                                             info.st_mtime_ns, info.st_ctime_ns))
                        generations[path] = generation
                    current.append(generation)
                    if generation != expected_generation:
                        break
            except OSError:
                current = []
            self._metrics["dependency_checks"] += len(current)
            if tuple(current) != expected:
                self._metrics["dependency_invalidations"] += 1
                self._invalidated_owners.add(key)
                self.forget_owner(jobs, key[1])
                invalidated = True
                continue
            self._queue_settled_dependency_check(key)
        return bool(queue) or invalidated

    def forget_owner(self, jobs: Path, attempt_id: str):
        key = (str(jobs.resolve(strict=False)), attempt_id)
        entry = self._settled.pop(key, None)
        self._forget_dependency_watch(key)
        if entry is not None:
            if key in self._dependency_queued:
                queue = self._dependency_queues.get(key[0])
                if queue is not None:
                    try:
                        queue.remove(key)
                    except ValueError:
                        pass
                self._dependency_queued.discard(key)
            for alias in entry[4]:
                owners = self._route_owners.get((key[0], alias))
                if owners is not None:
                    owners.discard(attempt_id)
                    if not owners:
                        self._route_owners.pop((key[0], alias), None)

    def remember_active_owner(self, jobs: Path, record):
        key = (str(jobs.resolve(strict=False)), record.completion_metadata().get("attempt_id", ""))
        self._active_owners[key] = record

    def forget_active_owner(self, jobs: Path, attempt_id: str):
        self._active_owners.pop((str(jobs.resolve(strict=False)), attempt_id), None)

    def active_owner_ids(self, jobs: Path):
        key = str(jobs.resolve(strict=False))
        return tuple(attempt for (root, attempt) in self._active_owners if root == key)

    def affected_settled_owner_ids(self, jobs: Path, records):
        root = str(jobs.resolve(strict=False))
        affected = set()
        for record in records:
            metadata = record.completion_metadata()
            parent = metadata.get("parent_attempt_id")
            if parent and (root, parent) in self._settled:
                affected.add(parent)
            for field in ("route_id", "route_hash", "owner_route_id", "owner_route_hash"):
                alias = metadata.get(field)
                if alias:
                    affected.update(self._route_owners.get((root, alias), ()))
            attempt = metadata.get("attempt_id")
            if attempt and (root, attempt) in self._settled:
                affected.add(attempt)
        return tuple(affected)

    def take_invalidated_owner_ids(self, jobs: Path):
        root = str(jobs.resolve(strict=False))
        affected = tuple(attempt for item_root, attempt in self._invalidated_owners
                         if item_root == root)
        self._invalidated_owners.difference_update((root, attempt) for attempt in affected)
        return affected

    def owner_candidate_floor(self, jobs: Path, view):
        key = str(jobs.resolve(strict=False))
        prior = self._cursors.get(key)
        if (prior is not None and view.append_only_generation
                and view.path_identity == prior[0] and view.resolved_path == prior[1]
                and tuple(view.signature[:2]) == prior[2]
                and view.last_complete_order >= prior[3]):
            return prior[3]
        return -1

    def remember_owner_cursor(self, jobs: Path, view):
        key = str(jobs.resolve(strict=False))
        self._cursors[key] = (view.path_identity, view.resolved_path,
                              tuple(view.signature[:2]), view.last_complete_order)

    def metrics(self):
        return dict(self._metrics)

    def record_historical_check(self):
        self._metrics["historical_checks"] += 1

    def needs_scan(self, jobs: Path, lease_is_held) -> bool:
        key = str(jobs.resolve(strict=False))
        prior = self._seen.get(key)
        stamp = _registry_stamp(jobs)
        scan = prior is None or stamp is None or stamp != prior[0]
        active_owner = any(root == key for root, _attempt in self._active_owners)
        if active_owner:
            scan = True
        # A full candidate scan below rechecks each candidate's lease itself;
        # only the unchanged fast path needs this cached lease probe.
        if prior is not None and not scan:
            for metadata in prior[1]:
                try:
                    if not lease_is_held(jobs, metadata):
                        scan = True
                except Exception:
                    scan = True
        # Advance the bounded rotating batch on every pass. Registry changes
        # and active owners also need full candidate selection, but neither can
        # defer dependency invalidation indefinitely.
        dependency_changed = self._validate_settled_dependency_batch(jobs)
        return scan or dependency_changed

    def cached_signatures(self, jobs: Path):
        prior = self._seen.get(str(jobs.resolve(strict=False)))
        return () if prior is None else prior[2]

    def remember(self, jobs: Path, before_stamp, live_leases, retry_required=False,
                 signatures=()):
        key = str(jobs.resolve(strict=False))
        after_stamp = _registry_stamp(jobs)
        if retry_required or before_stamp is None or after_stamp != before_stamp:
            self._seen.pop(key, None)
            return
        self._seen[key] = (after_stamp, tuple(live_leases), tuple(signatures))


def _owner_settlement_dependency_paths(jobs: Path, status: str, metadata: dict):
    """Return the exact cheap file generations that guard a settled cache entry."""
    try:
        import dispatch_contract
        import dispatch_terminal_commit as terminal
        request = terminal._historical_request(jobs, status, metadata)
        if request is None:
            return None
        route = json.loads(Path(request.route_file).read_text(encoding="utf-8"))
        route_id = route["route_id"]
        attempt_id = metadata["attempt_id"]
        slot = terminal.terminal_slot(request.artifact_root, route_id, attempt_id)
        route_module = terminal._route_module()
        binding_path = terminal.producer_binding_path(
            request.artifact_root, route_id, attempt_id)
        paths = [Path(request.route_file), terminal._commit_state_path(request),
                 route_module.outcome_path(Path(request.route_file)),
                 dispatch_contract.terminal_claim_path(jobs, route_id, attempt_id),
                 binding_path, terminal.campaign_goal_path(request),
                 slot / "owner-envelope.json", slot / "owner-envelope.txt"]
        for node in route.get("nodes", []):
            if node.get("terminal") is not True:
                continue
            marker_path = route_module.completion_dir(
                route_id, jobs=request.jobs) / (str(node.get("id")) + ".json")
            paths.append(marker_path)
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                evidence_path = (marker.get("evidence") or {}).get("path")
                if isinstance(evidence_path, str) and evidence_path:
                    evidence = Path(evidence_path)
                    paths.append(evidence if evidence.is_absolute()
                                 else request.artifact_root / evidence)
            except (OSError, ValueError, TypeError):
                pass
        try:
            envelope = json.loads((slot / "owner-envelope.json").read_text(encoding="utf-8"))
            primary = envelope.get("primary_path")
            if isinstance(primary, str) and primary:
                paths.append(Path(primary))
        except (OSError, ValueError, TypeError):
            pass

        if binding_path.is_file():
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            cycle_id = (binding.get("binding") or {}).get("cycle_id")
            if isinstance(cycle_id, str) and cycle_id:
                import artifact_producer as producer
                cycle = producer.read_cycle_record(request.artifact_root, cycle_id)
                if cycle is None:
                    return None
                paths.extend((producer.cycle_record_path(request.artifact_root, cycle_id),
                              producer._record_cycle_manifest_path(request.artifact_root, cycle),
                              producer.artifact_admission._index_path(request.artifact_root),
                              request.artifact_root / producer.artifact_admission.ADMISSION_REL
                                  / "root-identity.json"))
                if route_id:
                    canonical_route = producer.route_lineage.canonical_route_path(
                        request.artifact_root, route_id)
                    paths.extend((canonical_route,
                                  producer.artifact_lifecycle.canonical_outcome_path(
                                      request.artifact_root, route_id)))
                    producer_route = json.loads(canonical_route.read_text(encoding="utf-8"))
                    paths.extend(route_module.completion_dir(route_id) /
                                 (str(node["id"]) + ".json")
                                 for node in producer_route.get("nodes", [])
                                 if node.get("terminal") is True)
                snapshots = producer.artifact_lifecycle.manifest_snapshot_dir(
                    request.artifact_root, cycle_id)
                paths.append(snapshots)
                if snapshots.is_dir():
                    paths.extend(path for path in snapshots.iterdir()
                                 if path.suffix == ".json")
        # Missing optional files depend on their nearest existing parent, so
        # their later creation invalidates the observation without a tree scan.
        observed = []
        seen = set()
        for dependency in paths:
            candidate = Path(dependency).expanduser()
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            key = str(candidate.absolute())
            if key not in seen:
                seen.add(key)
                observed.append(candidate)
        return tuple(observed) if observed else None
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _pending_owner_settlements(jobs: Path, *, context=None):
    """Return exact owner rows whose existing terminal path still owes work."""
    from dispatch_contract import (
        SUPERVISOR_LEASE_KIND, parse_registry_metadata, supervisor_lease_is_held,
    )
    from dispatch_terminal_commit import owner_completion_pending

    try:
        from dispatch_registry_cache import registry_view
        view = registry_view(jobs, append_only=True)
        view.require_complete_text()
        if context is None:
            candidates = view.settlement_owner_candidates()
        else:
            floor = context.owner_candidate_floor(jobs, view)
            selected = {record.completion_metadata().get("attempt_id", ""): record
                        for record in (view.settlement_owner_candidates()
                                       if floor < 0 else
                                       view.settlement_owner_candidates_since(floor))
                        if record.completion_metadata().get("attempt_id")}
            changed_rows = view.records_since(floor) if floor >= 0 else ()
            impacted = set(context.affected_settled_owner_ids(jobs, changed_rows))
            impacted.update(context.take_invalidated_owner_ids(jobs))
            impacted.update(context.active_owner_ids(jobs))
            for attempt_id in impacted:
                current = view.settlement_owner_candidate(attempt_id)
                if current is None:
                    context.forget_owner(jobs, attempt_id)
                    context.forget_active_owner(jobs, attempt_id)
                else:
                    previous = selected.get(attempt_id)
                    if previous is None or current.order > previous.order:
                        selected[attempt_id] = current
            candidates = tuple(selected.values())
    except FileNotFoundError:
        return ()
    pending = []
    if context is not None:
        context.begin_dependency_pass()
    for candidate in candidates:
        order, raw, fields = candidate.order, candidate.raw, candidate.fields
        if fields[1] not in {"open", "running", "done"}:
            continue
        meta = candidate.completion_metadata()
        if (meta.get("worker_type") != "owner" or meta.get("launch_started") != "1"
                or meta.get("supervisor_lease") != SUPERVISOR_LEASE_KIND
                or not meta.get("supervisor_lease_file") or not meta.get("log_file")
                or not meta.get("attempt_id")):
            if context is not None and meta.get("attempt_id"):
                context.forget_active_owner(jobs, meta["attempt_id"])
                context.forget_owner(jobs, meta["attempt_id"])
            continue
        attempt_id = meta["attempt_id"]
        if fields[1] in {"open", "running"} and context is not None:
            context.remember_active_owner(jobs, candidate)
        if fields[1] == "done":
            if meta.get("workflow_completion") != "runtime-v1":
                if context is not None:
                    context.forget_active_owner(jobs, attempt_id)
                    context.forget_owner(jobs, attempt_id)
                continue
            related_rows = view.completion_related(
                meta.get("attempt_id", ""),
                meta.get("owner_route_id") or meta.get("route_id", ""),
                meta.get("owner_route_hash") or meta.get("route_hash", ""))
            if (context is not None and context.settled_owner_current(
                    jobs, fields[1], raw, meta, related_rows)):
                context.forget_active_owner(jobs, attempt_id)
                continue
            try:
                if context is not None:
                    context.record_historical_check()
                pending_state = owner_completion_pending(jobs, fields[1], meta)
                if not pending_state:
                    if context is not None:
                        dependencies = _owner_settlement_dependency_paths(jobs, fields[1], meta)
                        if dependencies:
                            remembered = context.remember_settled_owner(
                                jobs, fields[1], raw, meta, related_rows, dependencies)
                            if remembered is not True:
                                # An invalidated or unavailable snapshot cannot
                                # prove settlement; keep the row on its normal
                                # settlement path instead of caching it.
                                context.forget_active_owner(jobs, attempt_id)
                            else:
                                context.forget_active_owner(jobs, attempt_id)
                                continue
                        else:
                            context.forget_active_owner(jobs, attempt_id)
                            continue
                    else:
                        continue
                if context is not None:
                    context.forget_owner(jobs, meta["attempt_id"])
                    context.remember_active_owner(jobs, candidate)
            except Exception:
                # An unavailable observation remains a retryable obligation.
                if context is not None:
                    context.forget_owner(jobs, attempt_id)
                    context.remember_active_owner(jobs, candidate)
        try:
            lease_held = supervisor_lease_is_held(jobs, meta)
        except (OSError, ValueError):
            lease_held = True
        pending.append((order, fields[1], raw, meta, lease_held))
    if context is not None:
        context.remember_owner_cursor(jobs, view)
    return tuple(pending)


def _resume_owner_settlements(jobs: Path, *, context=None) -> None:
    """The open registry row retains a failed supervisor settlement itself.

    Resume through the same writer on ordinary reconnection, including older
    owners with no batch sidecar record. Storage failure keeps it open for
    the next existing callback; this never launches model work.
    """
    from dispatch_contract import supervisor_lease_is_held
    from dispatch_completion_join import (
        ChildRow, materialize_after_terminal_close, settle_finished_attempt,
    )
    lease_is_held = supervisor_lease_is_held
    if context is not None and not context.needs_scan(jobs, lease_is_held):
        return context.cached_signatures(jobs)
    before_stamp = _registry_stamp(jobs)
    try:
        pending = _pending_owner_settlements(jobs, context=context)
    except (OSError, UnicodeError):
        if context is not None:
            context.remember(jobs, before_stamp, (), retry_required=True)
        return ((str(jobs), "owner-settlement-unavailable"),)
    live_leases = []
    retry_required = False
    signatures = []
    for order, status, raw, meta, lease_held in pending:
        if lease_held:
            live_leases.append(meta)
            if status == "done":
                signatures.append((str(jobs), "owner-settlement",
                                   meta.get("attempt_id", ""), status,
                                   meta.get("workflow_completion", ""),
                                   meta.get("note", "")))
            continue
        try:
            settlement_stamp = _registry_stamp(jobs)
            if status == "done":
                materialize_after_terminal_close(jobs, meta["attempt_id"])
                from dispatch_terminal_commit import owner_completion_pending
                try:
                    if owner_completion_pending(jobs, status, meta):
                        retry_required = True
                        signatures.append((str(jobs), "owner-settlement",
                                           meta.get("attempt_id", ""), status,
                                           meta.get("workflow_completion", ""),
                                           meta.get("note", "")))
                except Exception:
                    retry_required = True
                    signatures.append((str(jobs), "owner-settlement",
                                       meta.get("attempt_id", ""), status,
                                       meta.get("workflow_completion", ""),
                                       meta.get("note", "")))
            else:
                result = settle_finished_attempt(
                    jobs, ChildRow(order, status, raw.split("\t")[4],
                                   meta["attempt_id"], raw, meta))
                if not isinstance(result, dict) or not result.get("closed"):
                    signatures.append((str(jobs), "owner-settlement",
                                       meta.get("attempt_id", ""), status,
                                       meta.get("workflow_completion", ""),
                                       meta.get("note", "")))
            if _registry_stamp(jobs) == settlement_stamp:
                retry_required = True
        except (OSError, UnicodeError, ValueError, RuntimeError):
            retry_required = True
            signatures.append((str(jobs), "owner-settlement",
                               meta.get("attempt_id", ""), status,
                               meta.get("workflow_completion", ""),
                               meta.get("note", "")))
            continue
    if context is not None:
        context.remember(jobs, before_stamp, live_leases, retry_required, signatures)
    return tuple(signatures)


def has_unowned_work(jobs: str | Path | None = None) -> bool:
    """Read-only check for batch or owner settlement work without a live owner."""
    for jobs_path in _jobs_paths(jobs, strict=False):
        try:
            owner_settlements = _pending_owner_settlements(jobs_path)
        except (OSError, UnicodeError):
            return True
        if any(not lease_held for _order, _status, _raw, _meta, lease_held in owner_settlements):
            return True

        try:
            records = _read_only_records(jobs_path)
        except (OSError, UnicodeError, BatchObligationError):
            return True
        for _path, record, error in records:
            if error is not None:
                return True
            if not record or record.get("state") not in _PENDING:
                continue
            observer = record.get("observer") or {}
            pid = observer.get("pid")
            start = observer.get("pid_start")
            if (not isinstance(pid, int) or not isinstance(start, str) or not start
                    or _pid_start(pid) != start):
                return True
    return False


def pending_observer_snapshot(jobs: str | Path | None = None, *,
                              include_owner_settlements: bool = False):
    """Read one pass of pending records for reconciliation and idle signature.

    This is read-only liveness input. It carries no delivery or execution
    authority and deliberately excludes sealed arguments and route identity.
    ``paths_by_jobs`` only selects candidates; ``ensure_observers`` rereads each
    selected record before acquiring its lease or replacing an observer.
    """
    signature = []
    paths_by_jobs = {}
    roots_by_jobs = {}
    root_stamps = {}
    cacheable = True
    for jobs_path in _jobs_paths(jobs, strict=False):
        key = str(jobs_path.resolve(strict=False))
        root_path = jobs_path.resolve(strict=False).parent / "supervisor-state" / "obligations"
        roots_by_jobs[key] = root_path
        before_stamp = _directory_stamp(root_path)
        unavailable = False
        owner_settlements = ()
        if include_owner_settlements:
            try:
                owner_settlements = _pending_owner_settlements(jobs_path)
            except (OSError, UnicodeError):
                signature.append((str(jobs_path), "unavailable"))
                unavailable = True
                cacheable = False
        try:
            records = _read_only_records(jobs_path)
        except (OSError, UnicodeError, BatchObligationError):
            if not unavailable:
                signature.append((str(jobs_path), "unavailable"))
            paths_by_jobs[key] = None
            root_stamps[key] = _directory_stamp(root_path)
            cacheable = False
            continue
        after_stamp = _directory_stamp(root_path)
        root_stamps[key] = after_stamp
        if before_stamp is None or before_stamp != after_stamp:
            cacheable = False
        for _order, status, _raw, meta, lease_held in owner_settlements:
            if not lease_held:
                signature.append((str(jobs_path), "owner-settlement",
                                  meta.get("attempt_id", ""), status,
                                  meta.get("workflow_completion", ""),
                                  meta.get("note", "")))
        pending_paths = []
        for path, record, error in records:
            if error is not None:
                signature.append((str(jobs_path), path.name, "unreadable"))
                pending_paths.append(path)
                cacheable = False
                continue
            if not record or record.get("state") not in _PENDING:
                continue
            pending_paths.append(path)
            # No observer identity here: each pass relaunches an unowned duty
            # anyway, and a relaunch (a new PID, alive or already gone) is
            # not new work that may reset the runner's bounded backoff.
            signature.append((
                str(jobs_path), record.get("id", path.stem), record.get("state"),
                record.get("delivery"),
            ))
        paths_by_jobs[key] = tuple(pending_paths)
    return {
        "signature": tuple(sorted(signature)), "paths_by_jobs": paths_by_jobs,
        "roots_by_jobs": roots_by_jobs, "root_stamps": root_stamps,
        "cacheable": cacheable,
    }


def pending_observer_signature(jobs: str | Path | None = None) -> tuple[tuple, ...]:
    """Describe pending completion duties without granting mutation authority."""
    return pending_observer_snapshot(jobs, include_owner_settlements=True)["signature"]


def _pid_start(pid: int) -> str:
    try:
        from dispatch_contract import process_start_ticks
        return str(process_start_ticks(pid) or "")
    except Exception:
        return ""
