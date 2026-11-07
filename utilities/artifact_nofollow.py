#!/usr/bin/env python3
"""No-follow, descriptor-relative file access shared by the viewer and the entrance.

Stdlib only and independent of every other hearting module, so the optional
artifact viewer can use it without importing producer code.  Nothing here
reopens a validated pathname: every component is opened with O_NOFOLLOW
relative to the previous descriptor.
"""
from __future__ import annotations

import os
import stat
from typing import Any, Iterable, Sequence


def check_nofollow_platform() -> None:
    """Raise RuntimeError where no-follow, descriptor-relative access is missing."""
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")) \
            or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
        raise RuntimeError("no-follow descriptor-relative file access is unavailable")


def open_root(root: Any) -> int:
    """Open every component of the physical root without following links."""
    check_nofollow_platform()
    path = os.path.abspath(os.fspath(root))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split("/")[1:]:
            if not part:
                continue
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_relative(root_fd: int, parts: Sequence[str], hidden: Iterable[str] = ()) -> int:
    """Return an already-authorized fd for a file or directory below root_fd.

    Every component is opened relative to the previous descriptor with
    O_NOFOLLOW, so a link swapped in after any earlier check is refused; the
    result is a regular file or a directory.  A pathname is never reopened.
    `hidden` names components the caller refuses (viewer policy).
    """
    hidden = frozenset(hidden)
    fd = os.dup(root_fd)
    try:
        for i, part in enumerate(parts):
            if part in hidden or part in ("", ".", "..") or "/" in part or "\\" in part:
                raise FileNotFoundError
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i != len(parts) - 1:
                flags |= os.O_DIRECTORY
            new = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = new
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise FileNotFoundError
        return fd
    except BaseException:
        os.close(fd)
        raise
