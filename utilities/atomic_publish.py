"""Shared no-replace directory publication, including NFSv3.

Directories cannot be hard-linked. Where RENAME_NOREPLACE is unsupported,
the existing producer protocol is absence-check + atomic rename under a
writer lock. This protects cooperating publishers; it cannot serialize an
unrelated writer that ignores that lock.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from pathlib import Path
import stat


def rename_directory_locked(source, target):
    """Publish under the caller's existing lock covering all target writers."""
    if os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    code = errno.ENOSYS
    if rename is not None:
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
            return
        code = ctypes.get_errno()
    if code not in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
        raise OSError(code, os.strerror(code), str(target))
    # The unsupported syscall may have taken time: recheck even under the lock.
    if os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
    os.rename(source, target)


def rename_directory(source, target):
    """Publish with a persistent parent lock shared by all bundle publishers.

    Never unlink the lock: waiters and later publishers must use the same inode.
    Process exit releases flock, so an interrupted publisher needs no recovery.
    """
    parent = Path(target).parent
    fd = os.open(parent / ".hearting-publish.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, "unsafe publication lock", str(parent))
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            rename_directory_locked(source, target)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
