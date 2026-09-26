#!/usr/bin/env python3
"""One cached full-registry read, shared by every repeated `jobs.log` scan.

A settle loop ticking every few seconds re-reads and re-splits the same
multi-megabyte registry on each full scan, and a single join tick used to do
that independently at five call sites plus the supervision reader. The
identity check below is a `stat()`, not a hash, so a cache miss costs nothing
extra beyond the read this replaces -- only an unchanged file skips the
`splitlines()` work.
"""

from __future__ import annotations

from pathlib import Path

_CACHE: dict[str, tuple[tuple[object, ...], tuple[str, ...]]] = {}


def registry_lines(jobs: Path) -> tuple[str, ...]:
    """Return `jobs.log`'s lines, reusing the last parse for an unchanged file.

    The cache key is `(st_ino, st_mtime_ns, st_ctime_ns, st_size)`: any real
    write changes at least one of these, and a path reused after the
    underlying file is replaced (different inode) never reuses a stale entry.
    Raises the same `OSError` a direct `jobs.read_text()` would for a missing
    or unreadable file -- callers keep their existing except clauses.
    """

    resolved = str(jobs)
    stat = Path(jobs).stat()
    key = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    cached = _CACHE.get(resolved)
    if cached is not None and cached[0] == key:
        return cached[1]
    lines = tuple(Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines())
    _CACHE[resolved] = (key, lines)
    return lines
