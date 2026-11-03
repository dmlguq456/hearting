"""Retire reusable runtime caches that no run, process or recent write still uses.

``<dispatch-state-root>/reusable-runtimes/<cyc_32hex>/<entry>/`` holds a model
cache, virtual environment or source cache that a research cycle prepared once
and reused. Nothing in Hearting creates or removes it, so it stayed forever.

An entry is removed only when every one of these holds; any doubt keeps it:

  * no latest registry row that is not terminal names the cycle id or the path;
  * no process of this user names the path in its argv, environment, cwd or
    open files (an unreadable process table keeps everything);
  * no regular file or link inside has been written or changed for
    ``IDLE_SECONDS`` (directory mtimes are ignored: a lock file or a ``git``
    status refresh touches them without anyone using the cache), and no
    registry row naming it ended inside that window.

Carried by the two existing retention carriers (the post-exit reaper tail and
``reconcile --apply``). There is no command, flag, daemon or gate. ``plan`` is
the read-only view of what the next pass would remove; running this file prints
it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import time

IDLE_SECONDS = 7 * 86400
DUE_SECONDS = 3600
REMOVE_LIMIT = 4
MAX_PASS_SECONDS = 60
MAX_WALK_ENTRIES = 2_000_000
MAX_PROCESSES = 8192
MAX_FDS = 16384
DIR_NAME = "reusable-runtimes"
STATE_NAME = "reusable-runtime-retention.json"
LOCK_NAME = "reusable-runtime-retention.lock"
RETIRING_PREFIX = ".retiring-"
CYCLE_RE = re.compile(r"cyc_[0-9a-f]{32}")
TERMINAL = frozenset({"done", "killed", "cancelled"})


class _Unreadable(Exception):
    pass


def _newest_ns(path, deadline):
    """Newest change among files and links; returns (ns, disk bytes, files)."""
    newest = 0
    size = 0
    files = 0
    seen = 0
    linked = set()
    stack = [path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as listing:
            for entry in listing:
                seen += 1
                if seen > MAX_WALK_ENTRIES or time.monotonic() > deadline:
                    raise _Unreadable("walk-limit")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                    continue
                files += 1
                if info.st_nlink > 1:
                    if (info.st_dev, info.st_ino) in linked:
                        continue
                    linked.add((info.st_dev, info.st_ino))
                size += info.st_blocks * 512
                newest = max(newest, info.st_mtime_ns, info.st_ctime_ns)
    return newest, size, files


def _process_text():
    """What same-user processes say about where they work: (text, blind) or None.

    The command line is world-readable and must be readable for every process;
    environment, cwd and open files are read when the kernel allows it (a
    privilege-separated child or a non-dumpable process refuses). ``blind``
    counts the processes whose extra evidence was refused.
    """
    chunks = []
    blind = 0
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    if len(pids) > MAX_PROCESSES:
        return None
    uid = os.geteuid()
    for pid in pids:
        base = "/proc/" + pid
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(base + "/cmdline", "rb") as handle:
                chunks.append(handle.read().decode("utf-8", "replace"))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return None
        try:
            with open(base + "/environ", "rb") as handle:
                chunks.append(handle.read().decode("utf-8", "replace"))
            chunks.append(os.readlink(base + "/cwd"))
            fds = os.listdir(base + "/fd")
            if len(fds) > MAX_FDS:
                blind += 1
                continue
            for fd in fds:
                with contextlib.suppress(FileNotFoundError):
                    chunks.append(os.readlink(base + "/fd/" + fd))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            blind += 1
        except OSError:
            return None
    return "\0".join(chunks), blind


def _registry(jobs):
    """Latest row text per attempt, or None when the registry cannot be trusted."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from worker_home_retention import _Snapshot
        return list(_Snapshot(jobs).rows.values())
    except Exception:  # noqa: BLE001 -- unreadable evidence keeps the bytes
        return None


def _ended_ns(row):
    try:
        from datetime import datetime, timezone
        ended = datetime.fromisoformat(row.fields[0].replace("Z", "+00:00"))
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        return int(ended.timestamp() * 1_000_000_000)
    except (AttributeError, IndexError, ValueError, OverflowError):
        return None


def plan(jobs, *, now_ns=None, only=None):
    """Read-only: one record per entry with ``verdict`` eligible/keep and why.

    ``only`` limits the walk to one entry path (the pre-removal re-check).
    """
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    jobs = Path(jobs).expanduser().resolve(strict=False)
    root = jobs.parent / DIR_NAME
    records = []
    if not root.is_dir() or root.is_symlink():
        return records
    deadline = time.monotonic() + MAX_PASS_SECONDS
    rows = _registry(jobs)
    seen_procs = _process_text()
    procs, blind = seen_procs if seen_procs is not None else (None, 0)
    for cycle_dir in sorted(p for p in root.iterdir()):
        if cycle_dir.name.startswith(RETIRING_PREFIX):
            continue
        entries = []
        if (CYCLE_RE.fullmatch(cycle_dir.name) and cycle_dir.is_dir()
                and not cycle_dir.is_symlink()):
            entries = sorted(p for p in cycle_dir.iterdir()
                             if not p.name.startswith(RETIRING_PREFIX))
        else:
            records.append({"path": str(cycle_dir), "verdict": "keep",
                            "reason": "unrecognised-name", "bytes": 0})
        for entry in entries:
            if only is not None and str(entry) != only:
                continue
            rec = {"path": str(entry), "cycle": cycle_dir.name, "bytes": 0}
            records.append(rec)
            try:
                info = entry.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                    rec.update(verdict="keep", reason="not-an-owned-directory")
                    continue
                newest, rec["bytes"], rec["files"] = _newest_ns(entry, deadline)
            except (OSError, _Unreadable) as exc:
                rec.update(verdict="keep", reason="unreadable:" + type(exc).__name__)
                continue
            newest = newest or info.st_mtime_ns
            if rows is None:
                rec.update(verdict="keep", reason="registry-unreadable")
                continue
            named = [row for row in rows
                     if cycle_dir.name in row.raw or str(entry) in row.raw]
            if any(row.status not in TERMINAL for row in named):
                rec.update(verdict="keep", reason="open-run")
                continue
            for row in named:
                ended = _ended_ns(row)
                if ended is None:
                    rec.update(verdict="keep", reason="run-time-unreadable")
                    break
                newest = max(newest, ended)
            if "reason" in rec:
                continue
            real = os.path.realpath(entry)
            if procs is None:
                rec.update(verdict="keep", reason="process-table-unreadable")
            elif str(entry) in procs or real in procs:
                rec.update(verdict="keep", reason="process-uses-it")
            else:
                rec["blind_processes"] = blind
                rec["idle_days"] = round(max(0, now_ns - newest) / 86400e9, 2)
                rec["newest_ns"] = newest
                if now_ns - newest >= IDLE_SECONDS * 1_000_000_000:
                    rec.update(verdict="eligible", reason="idle")
                else:
                    rec.update(verdict="keep", reason="recently-used")
    return records


@contextlib.contextmanager
def _lock(root):
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _retire(entry, expected_ino):
    """Detach with one rename, then delete the detached tree; never follows links."""
    info = os.lstat(entry)
    if not stat.S_ISDIR(info.st_mode) or info.st_ino != expected_ino:
        return False
    detached = entry.parent / (RETIRING_PREFIX + secrets.token_hex(8))
    os.rename(entry, detached)
    shutil.rmtree(detached, ignore_errors=True)
    return True


def _finish_leftovers(root):
    """A pass interrupted after the rename leaves a detached tree; finish it."""
    for parent in (root, *[p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]):
        for leftover in parent.iterdir():
            if (leftover.name.startswith(RETIRING_PREFIX) and leftover.is_dir()
                    and not leftover.is_symlink()):
                shutil.rmtree(leftover, ignore_errors=True)


def sweep(jobs, *, now_ns=None, force=False):
    """One best-effort pass; unknown evidence and failures always keep the bytes."""
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    jobs = Path(jobs).expanduser().resolve(strict=False)
    state_root = jobs.parent
    root = state_root / DIR_NAME
    if not root.is_dir() or root.is_symlink() or root.lstat().st_uid != os.geteuid():
        return {"skipped": "no-reusable-runtimes"}
    try:
        with _lock(state_root) as locked:
            if not locked:
                return {"skipped": "busy"}
            state_file = state_root / STATE_NAME
            try:
                last = int(json.loads(state_file.read_text()).get("last_pass_ns", 0))
            except (OSError, ValueError, AttributeError):
                last = 0
            if not force and 0 <= now_ns - last < DUE_SECONDS * 1_000_000_000:
                return {"skipped": "not-due"}
            _finish_leftovers(root)
            records = plan(jobs, now_ns=now_ns)
            removed = removed_bytes = 0
            for rec in records:
                if rec.get("verdict") != "eligible" or removed >= REMOVE_LIMIT:
                    continue
                entry = Path(rec["path"])
                # Re-read the verdict for this entry right before detaching it.
                fresh = plan(jobs, now_ns=now_ns, only=rec["path"])
                if not fresh or fresh[0].get("verdict") != "eligible":
                    continue
                if _retire(entry, entry.lstat().st_ino):
                    removed += 1
                    removed_bytes += rec["bytes"]
                    with contextlib.suppress(OSError):
                        entry.parent.rmdir()
            result = {"entries": len(records), "removed": removed,
                      "removed_bytes": removed_bytes,
                      "eligible_left": sum(1 for r in records
                                           if r.get("verdict") == "eligible") - removed}
            tmp = state_root / (STATE_NAME + ".tmp")
            tmp.write_text(json.dumps({"last_pass_ns": now_ns, "last_result": result}))
            os.replace(tmp, state_file)
            return result
    except Exception as exc:  # noqa: BLE001
        return {"skipped": "error", "error": type(exc).__name__}


def sweep_best_effort(jobs):
    try:
        return sweep(jobs)
    except Exception:  # noqa: BLE001 -- never change the caller's result
        return None


def main(argv):
    """Print what the next pass would remove; nothing is changed."""
    if len(argv) != 2:
        print("usage: reusable_runtime_retention.py <jobs.log>   (read-only plan)", file=sys.stderr)
        return 2
    for rec in plan(argv[1]):
        print(json.dumps(rec, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
